"""V4's slow-tempo/minimal-pitch policy; original macro and spectral FX stay intact."""

import ast
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from audacity_pipe import parse_macro
import killa_tools as kt

ROOT = Path(__file__).resolve().parent


class V4TempoPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.source = str(Path(self.tmp.name) / "source.wav")
        self.output = str(Path(self.tmp.name) / "out.mp3")
        self.spectrogram = str(Path(self.tmp.name) / "out.png")
        Path(self.source).write_bytes(b"unchanged source audio")
        self.steps = [name for name, _ in parse_macro(ROOT / "macros" / "CopyR8 AutoFit V4.txt")]
        self.logs = []
        self.stack.enter_context(patch.dict(sys.modules, {"shazamio": types.ModuleType("shazamio")}))

        def mock(name, **kwargs):
            return self.stack.enter_context(patch.object(kt, name, **kwargs))

        self.stack.enter_context(patch.object(kt.acoustid_backend, "validate_setup"))
        self.stack.enter_context(patch.object(kt.songrec_backend, "validate_setup"))
        mock("_gapfree_windows", return_value=[0.0, 9.0])
        self.base = mock("_apply_v4_base_fx", side_effect=self.copy)
        self.base_probe = mock("_probe_sweep", return_value=(False, [1]))
        self.probe = mock("_probe_sweep_shifted", return_value=(True, [0, 0]))
        self.control = mock("_control_check", return_value=True)
        self.render = mock("_render_shifted_wav", side_effect=self.render_shift)
        self.export = mock("_match_loudness", side_effect=self.encode)
        mock("_run", side_effect=self.render_picture)
        mock("_brand_spectrogram")
        mock("_result_info", return_value={})

    @staticmethod
    def copy(source, destination, **kwargs):
        if os.path.abspath(source) != os.path.abspath(destination):
            shutil.copyfile(source, destination)
        return destination

    def render_shift(self, source, destination, pitch, tempo):
        self.copy(source, destination)
        return True

    def macro(self, source, destination, progress=None):
        self.copy(source, destination)
        return {"steps": self.steps, "metadata": False}

    def encode(self, source, destination, original, **kwargs):
        self.copy(source, destination)
        return {"source_lufs": None, "out_lufs": None, "out_peak": None, "gain_db": 0.0}

    @staticmethod
    def render_picture(command, **kwargs):
        Path(command[-1]).write_bytes(b"mock picture")
        return 0, "", ""

    def fit(self, with_macro=False):
        return kt.copyr8_autofit_v4(
            self.source, self.output, self.spectrogram,
            progress=self.logs.append, macro_fn=self.macro if with_macro else None)

    def test_profile_exhausts_zero_pitch_before_tiny_detunes_and_never_speeds_up(self):
        rungs = kt._v4_adjust_rungs()
        self.assertEqual(len(rungs), 80)
        self.assertEqual(len(set(rungs)), len(rungs))
        self.assertEqual(rungs[0][1:3], (0.0, 0.99))
        self.assertEqual(rungs[-1][1:3], (-0.20, 0.84))
        self.assertEqual([r[1] for r in rungs], sorted([r[1] for r in rungs], reverse=True))
        for pitch in kt.V4_PITCH_STEPS:
            tier = [r for r in rungs if r[1] == pitch]
            self.assertEqual([r[2] for r in tier], list(kt.V4_TEMPO_STEPS))
        for cost, pitch, tempo, depth, rate in rungs:
            self.assertTrue(-0.20 <= pitch <= 0.0)
            self.assertTrue(0.84 <= tempo < 1.0)
            self.assertEqual((depth, rate), (0.0, 0.0))
            self.assertAlmostEqual(cost, abs(pitch) + 10 * abs(tempo - 1.0))

    def test_prefers_first_working_slowdown_without_changing_pitch(self):
        self.probe.side_effect = lambda source, windows, pitch, tempo, *a, **kw: (
            (True, [0, 0]) if tempo <= 0.96 else (False, [1]))
        result = self.fit()
        self.assertEqual((result["pitch"], result["tempo"]), (0.0, 0.96))
        self.assertEqual([call.args[2:4] for call in self.probe.call_args_list],
                         [(0.0, 0.99), (0.0, 0.98), (0.0, 0.97), (0.0, 0.96)])
        self.assertTrue(result["verified"])
        self.assertFalse(result["tremolo_applied"])
        self.assertTrue(any("testing +0.00 st / -1.0%" in line for line in self.logs))
        self.assertFalse(any("+-" in line for line in self.logs))

    def test_pitch_nudges_wait_until_all_unchanged_pitch_tempos_fail(self):
        self.probe.side_effect = lambda source, windows, pitch, tempo, *a, **kw: (
            (True, [0, 0]) if (pitch, tempo) == (-0.05, 0.98) else (False, [1]))
        result = self.fit(with_macro=True)
        tried = [call.args[2:4] for call in self.probe.call_args_list]
        self.assertEqual(tried, [(0.0, t) for t in kt.V4_TEMPO_STEPS]
                         + [(-0.05, 0.99), (-0.05, 0.98)])
        self.assertEqual((result["pitch"], result["tempo"]), (-0.05, 0.98))
        self.assertTrue(result["macro"]["applied"])
        self.assertEqual(result["macro"]["steps"], self.steps)
        self.assertTrue(any("-0.05 st / -2.0%" in line for line in self.logs))

    def test_unknown_original_search_fallback_uses_slow_tempo_and_zero_pitch(self):
        self.control.return_value = False
        result = self.fit()
        self.assertEqual((result["pitch"], result["tempo"]), (0.0, 0.94))
        self.assertAlmostEqual(result["cost"], 0.60)
        self.assertIs(result["verified"], False)
        self.render.assert_called_once()
        self.assertEqual(self.render.call_args.args[2:], (0.0, 0.94))
        self.assertTrue(any("slow-tempo fallback" in line for line in self.logs))

    def test_unknown_original_macro_only_fallback_is_also_slow_and_zero_pitch(self):
        self.base_probe.return_value = (True, [0, 0])
        self.control.return_value = False
        result = self.fit(with_macro=True)
        self.assertEqual((result["pitch"], result["tempo"]), (0.0, 0.94))
        self.assertIs(result["verified"], False)
        self.probe.assert_not_called()
        self.assertEqual(self.render.call_args.args[2:], (0.0, 0.94))
        self.assertTrue(result["macro"]["applied"])

    def test_verified_macro_does_not_get_an_unnecessary_tempo_or_pitch_change(self):
        self.base_probe.return_value = (True, [0, 0])
        result = self.fit(with_macro=True)
        self.assertEqual((result["pitch"], result["tempo"]), (0.0, 1.0))
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["macro"]["steps"]), 17)
        self.render.assert_not_called()
        self.probe.assert_not_called()
        self.base.assert_called_once()
        self.assertTrue(self.base.call_args.kwargs["already_macro"])
        self.assertTrue(self.export.call_args.kwargs["v4_sculpt"])

    def test_recognized_best_effort_never_reverts_to_fast_v3_presets(self):
        self.probe.return_value = (False, [1])
        result = self.fit()
        self.assertEqual(self.probe.call_count, len(kt._v4_adjust_rungs()))
        self.assertEqual((result["pitch"], result["tempo"]), (-0.20, 0.84))
        self.assertFalse(result["clean"])
        self.assertIsNone(result["counts"])
        self.assertEqual(self.render.call_args.args[2:], (-0.20, 0.84))

    def test_inconclusive_original_control_keeps_the_slow_candidate(self):
        self.control.return_value = None
        result = self.fit()
        self.assertEqual((result["pitch"], result["tempo"]), (0.0, 0.99))
        self.assertIsNone(result["verified"])

    def test_no_api_answers_still_refuses_to_export(self):
        self.probe.return_value = (False, [None])
        with self.assertRaisesRegex(RuntimeError, "never returned a usable answer"):
            self.fit()
        self.assertFalse(Path(self.output).exists())
        self.render.assert_not_called()
        self.export.assert_not_called()

    def test_v3_legacy_unverified_fallback_is_not_changed(self):
        self.control.return_value = False
        result = kt.copyr8_autofit_v3(self.source, self.output, self.spectrogram)
        self.assertEqual((result["pitch"], result["tempo"]), (2.0, 1.07))
        self.assertAlmostEqual(result["cost"], 2.7)
        self.base.assert_not_called()
        self.assertFalse(self.export.call_args.kwargs["v4_sculpt"])


class V4SlowRenderAndReplyTests(unittest.TestCase):
    def test_real_slow_renderer_keeps_tempo_independent_of_pitch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = 48000
            t = np.arange(sr * 4) / sr
            source = str(Path(tmp) / "source.wav")
            output = str(Path(tmp) / "slow.wav")
            tone = 0.18 * np.sin(2 * np.pi * 440 * t)
            sf.write(source, np.column_stack([tone, tone]), sr, subtype="FLOAT")
            for pitch, tempo in ((0.0, 0.94), (-0.20, 0.84)):
                with self.subTest(pitch=pitch, tempo=tempo):
                    kt._render_shifted_wav(source, output, pitch, tempo)
                    audio, rate = sf.read(output, always_2d=True)
                    self.assertGreater(len(audio) / rate, 4.0)
                    self.assertAlmostEqual(len(audio) / rate, 4.0 / tempo, delta=0.08)
                    stable = audio[rate // 2:-rate // 2, 0]
                    spectrum = np.abs(np.fft.rfft(stable * np.hanning(len(stable))))
                    frequencies = np.fft.rfftfreq(len(stable), 1.0 / rate)
                    peak = float(frequencies[np.argmax(spectrum)])
                    self.assertAlmostEqual(peak, 440.0 * 2 ** (pitch / 12), delta=0.6)

    def test_real_slow_probes_keep_pitch_and_test_the_in_between_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = 48000
            t = np.arange(sr * 20) / sr
            source = str(Path(tmp) / "source.wav")
            sf.write(source, 0.18 * np.sin(2 * np.pi * 440 * t), sr, subtype="FLOAT")
            probes, progress = [], []

            def identify(clip, progress=None):
                audio, rate = sf.read(clip)
                probes.append(len(audio) / rate)
                self.assertGreaterEqual(probes[-1], kt.SHA_PROBE_CLIP_SECONDS)
                stable = audio[rate // 2:-rate // 2]
                frequencies = np.fft.rfftfreq(len(stable), 1.0 / rate)
                spectrum = np.abs(np.fft.rfft(stable * np.hanning(len(stable))))
                self.assertAlmostEqual(float(frequencies[np.argmax(spectrum)]), 440.0, delta=0.3)
                return 0

            with patch.object(kt, "_shazam_match_count", side_effect=identify), \
                 patch.object(kt.acoustid_backend, "is_available", return_value=False), \
                 patch.object(kt.songrec_backend, "is_available", return_value=False):
                ok, counts = kt._probe_sweep_shifted(
                    source, [0.0, 8.0], 0.0, 0.94, progress=progress.append)
            self.assertTrue(ok)
            self.assertEqual(counts, [0, 0])
            self.assertEqual(len(probes), 2)
            self.assertTrue(any("in-between audio window 2/2" in line for line in progress))
            self.assertFalse((Path(tmp) / "shift_probe.wav").exists())
            self.assertFalse((Path(tmp) / "acoustid_candidate.wav").exists())

    def test_discord_reply_uses_actual_slow_fallback_and_speed_direction(self):
        # Execute just the reply branches, without logging into Discord.
        tree = ast.parse((ROOT / "audio_bot.py").read_text())
        command = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "copyr8_slash")
        verdict = next(n for n in ast.walk(command) if isinstance(n, ast.If)
                       and ast.unparse(n.test) == "not result['clean']")
        speed = next(n for n in ast.walk(command) if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "is_v4 and result['tempo'] < 1.0")
        for is_v4, pitch, tempo, label in (
            (True, 0.0, 0.94, "slow-tempo fallback"),
            (False, 2.0, 1.07, "balanced protection shift"),
        ):
            with self.subTest(is_v4=is_v4):
                fields = []
                namespace = {
                    "result": {"clean": True, "verified": False, "pitch": pitch, "tempo": tempo},
                    "is_v4": is_v4, "apis_list": "the recognition services",
                    "embed": types.SimpleNamespace(add_field=lambda **kwargs: fields.append(kwargs)),
                }
                module = ast.Module(body=[verdict, speed], type_ignores=[])
                exec(compile(module, "audio_bot.py", "exec"), namespace)
                self.assertIn(label, namespace["verdict"])
                self.assertIn(f"{pitch:+.2f} st / {tempo:.2f}x", namespace["verdict"])
                if is_v4:
                    self.assertNotIn("1.07x", namespace["verdict"])
                    self.assertEqual(len(fields), 1)
                    self.assertIn("6% slower", fields[0]["value"])
                else:
                    self.assertEqual(fields, [])


if __name__ == "__main__":
    unittest.main()
