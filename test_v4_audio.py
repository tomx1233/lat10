"""Regressions for V4's ADDITIONAL spectral layer, not a replacement macro.

The original macro intentionally contains mono conversion, Tremolo x2 and SC1
x2. These tests ensure the extra layer preserves supplied modulation and adds
no new whistles/sweeps or channel changes. ffmpeg runs for real; Audacity/SC1
and recognition API boundaries are mocked.
"""

import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import audacity_processor as processor
from audacity_pipe import parse_macro
import killa_tools as kt


class V4SpectralLayerSafetyTests(unittest.TestCase):
    def test_base_and_export_graphs_have_no_generated_tones_or_sweeping_fx(self):
        graphs = [kt._v4_sculpt_filter_complex(0.0, kt.AUTOFIT_CLIP_THRESHOLD, 10.0)]
        for already_macro in (False, True):
            with patch.object(kt, "_duration_seconds", return_value=10.0), \
                 patch.object(kt, "_run", return_value=(0, "", "")) as run:
                kt._apply_v4_base_fx("source.wav", "destination.wav", already_macro=already_macro)
            command = run.call_args.args[0]
            graphs.append(command[command.index("-filter_complex") + 1])
            if already_macro:
                self.assertNotIn("acompressor", graphs[-1])

        for graph in graphs:
            with self.subTest(graph=graph):
                for forbidden in (
                    "aeval", "aevalsrc", "sine=", "anoisesrc", "sin(", "cos(",
                    "flanger", "aphaser", "vibrato", "tremolo", "crystalizer",
                    "allpass", "extrastereo", "amix",
                ):
                    self.assertNotIn(forbidden, graph)
                self.assertIn("firequalizer", graph)
                self.assertIn("zero_phase=1", graph)

    def test_spectral_curve_is_strictly_ordered_and_never_boosts_treble(self):
        entries = [
            (float(freq), float(gain))
            for freq, gain in re.findall(r"entry\(([^,]+),([^\)]+)\)", kt._v4_comb_gain_entry())
        ]
        self.assertGreater(len(entries), 20)
        self.assertTrue(all(a[0] < b[0] for a, b in zip(entries, entries[1:])))
        self.assertTrue(all(gain <= 0.0 for _, gain in entries))
        self.assertEqual(entries[-1][0], 24000.0)

    def test_unknown_work_duration_does_not_truncate_export_to_a_default_length(self):
        with patch.object(kt, "_duration_seconds", side_effect=RuntimeError("unmeasurable")), \
             patch.object(kt, "_loudness", return_value=(None, None)), \
             patch.object(kt, "_run", return_value=(0, "", "")) as run, \
             patch.object(kt.os, "replace"):
            kt._match_loudness("work.wav", "export.mp3", "source.wav", v4_sculpt=True)
        command = run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("atrim=start=0,", graph)
        self.assertNotIn(":end=", graph)

    def test_original_macro_runs_first_then_spectral_layer_probes_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = 48000
            t = np.arange(sr * 3) / sr
            source_audio = 0.15 * np.sin(2 * np.pi * 440 * t)
            source = os.path.join(tmp, "source.wav")
            output = os.path.join(tmp, "output.mp3")
            spectrogram = os.path.join(tmp, "spectrogram.png")
            base = os.path.join(tmp, "autofit_v4_base.wav")
            sf.write(source, np.column_stack([source_audio, source_audio]), sr, subtype="FLOAT")
            original_bytes = Path(source).read_bytes()
            steps = [name for name, _ in parse_macro(processor.BUNDLED_V4_MACRO)]
            events = []
            real_base_fx, real_export = kt._apply_v4_base_fx, kt._match_loudness

            def macro(src, dst, progress=None):
                events.append("macro")
                # Stand-in for the Windows/Audacity boundary, not an SC1 emulation.
                # The exact original commands/parameters are locked in macro tests.
                audio, rate = sf.read(src, always_2d=True)
                audio[:, 1] *= 0.65 + 0.25 * np.sin(2 * np.pi * 4 * t)
                sf.write(dst, audio, rate, subtype="FLOAT")
                return {"steps": steps, "metadata": False}

            def spectral(src, dst, **kwargs):
                events.append("spectral")
                return real_base_fx(src, dst, **kwargs)

            def probe(path, windows, progress=None):
                events.append("probe")
                return True, [0] * len(windows)

            def export(*args, **kwargs):
                events.append("export")
                return real_export(*args, **kwargs)

            with patch.object(kt.acoustid_backend, "validate_setup"), \
                 patch.object(kt.songrec_backend, "validate_setup"), \
                 patch.object(kt, "_apply_v4_base_fx", side_effect=spectral) as apply_spectral, \
                 patch.object(kt, "_probe_sweep", side_effect=probe) as probe_sweep, \
                 patch.object(kt, "_control_check", return_value=True), \
                 patch.object(kt, "_match_loudness", side_effect=export) as match_loudness:
                progress = []
                result = kt.copyr8_autofit_v4(
                    source, output, spectrogram, macro_fn=macro, progress=progress.append)

            self.assertEqual(events, ["macro", "spectral", "probe", "export"])
            self.assertTrue(any("separate V4 spectrogram layer" in line for line in progress))
            apply_spectral.assert_called_once_with(base, base, already_macro=True)
            self.assertEqual(probe_sweep.call_args.args[0], base)
            self.assertEqual(match_loudness.call_args.args[0], base)
            self.assertTrue(match_loudness.call_args.kwargs["v4_sculpt"])
            self.assertTrue(result["macro"]["applied"])
            self.assertEqual(result["macro"]["steps"], steps)
            self.assertEqual(len(steps), 17)
            # No EXTRA tremolo overlay: the original macro's own effects stay.
            self.assertFalse(result["tremolo_applied"])
            self.assertEqual(Path(source).read_bytes(), original_bytes)
            self.assertGreater(Path(output).stat().st_size, 1000)
            self.assertTrue(Path(spectrogram).is_file())

    def test_spectral_addon_preserves_existing_macro_tremolo_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = 48000
            t = np.arange(sr * 3) / sr
            carrier = 0.15 * np.sin(2 * np.pi * 440 * t)
            # Representative pre-existing macro modulation, deliberately in a
            # pass band. The add-on must not replace it or flatten its envelope.
            original = np.column_stack([
                carrier,
                carrier * (0.65 + 0.25 * np.sin(2 * np.pi * 4 * t)),
            ])
            source = os.path.join(tmp, "macro_output.wav")
            output = os.path.join(tmp, "macro_plus_spectral.wav")
            sf.write(source, original, sr, subtype="FLOAT")
            kt._apply_v4_base_fx(source, output, already_macro=True)
            actual, rate = sf.read(output, always_2d=True)
            self.assertEqual(rate, sr)
            self.assertEqual(actual.shape, original.shape)
            trim = sr // 10
            np.testing.assert_allclose(actual[trim:-trim], original[trim:-trim], atol=1e-6, rtol=2e-5)

    def test_spectral_layer_adds_no_whine_wobble_or_new_mono_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = 48000
            t = np.arange(sr * 8) / sr
            tone = 0.22 * np.sin(2 * np.pi * 440 * t)
            tone[(t < 1.0) | (t >= 7.0)] = 0.0
            source = os.path.join(tmp, "source.wav")
            sf.write(source, np.column_stack([tone, tone]), sr, subtype="FLOAT")

            for already_macro in (False, True):
                with self.subTest(already_macro=already_macro):
                    base = os.path.join(tmp, "base.wav")
                    exported = os.path.join(tmp, "export.mp3")
                    decoded = os.path.join(tmp, "decoded.wav")
                    kt._apply_v4_base_fx(source, base, already_macro=already_macro)
                    kt._match_loudness(base, exported, source, v4_sculpt=True)
                    rc, _, err = kt._run(
                        kt._FFMPEG + ["-i", exported, "-c:a", "pcm_f32le", decoded]
                    )
                    self.assertEqual(rc, 0, err)

                    for path in (base, decoded):
                        with self.subTest(path=Path(path).name):
                            audio, actual_sr = sf.read(path, always_2d=True)
                            self.assertEqual(actual_sr, sr)
                            self.assertEqual(audio.shape[1], 2)
                            self.assertTrue(np.isfinite(audio).all())
                            self.assertLess(float(np.max(np.abs(audio))), 0.35)
                            # Identical input channels must stay identical, not become
                            # out-of-phase and disappear on mono speakers.
                            self.assertLess(float(np.max(np.abs(audio[:, 0] - audio[:, 1]))), 1e-5)

                            stable = audio[int(1.5 * sr):int(6.5 * sr)]
                            spectrum = np.abs(np.fft.rfft(stable * np.hanning(len(stable))[:, None], axis=0)) ** 2
                            frequencies = np.fft.rfftfreq(len(stable), 1.0 / sr)
                            high_ratio_db = 10 * np.log10(
                                float(np.sum(spectrum[frequencies >= 4000]))
                                / max(float(np.sum(spectrum)), 1e-30) + 1e-30
                            )
                            # A bass/mid-only source must not acquire the old 4.8/7.8/
                            # 11.4/16.8 kHz synthesized whistles in either rendering path.
                            self.assertLess(high_ratio_db, -65.0)

                            # 50 ms windows contain exactly 22 cycles of the 440 Hz tone.
                            windows = stable[:, 0].reshape(-1, int(sr * 0.05))
                            rms = np.sqrt(np.mean(windows ** 2, axis=1))
                            modulation_db = 20 * np.log10(float(rms.max() / rms.min()))
                            self.assertLess(modulation_db, 0.25)

                            # Silence well away from transitions must stay silent,
                            # including after MP3 delay/padding and FIR tails.
                            quiet = np.concatenate([
                                audio[int(0.2 * sr):int(0.7 * sr)],
                                audio[int(7.3 * sr):int(7.8 * sr)],
                            ])
                            self.assertLess(float(np.max(np.abs(quiet))), 1e-6)

    def test_channel_isolation_survives_mono_and_stereo_source_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sr in (44100, 48000):
                for already_macro in (False, True):
                    with self.subTest(sr=sr, already_macro=already_macro):
                        t = np.arange(sr * 2) / sr
                        left = 0.15 * np.sin(2 * np.pi * 440 * t)
                        source = os.path.join(tmp, "left_only.wav")
                        base = os.path.join(tmp, "base.wav")
                        sf.write(source, np.column_stack([left, np.zeros_like(left)]), sr, subtype="FLOAT")
                        kt._apply_v4_base_fx(source, base, already_macro=already_macro)
                        audio, actual_sr = sf.read(base, always_2d=True)
                        self.assertEqual(actual_sr, 48000)
                        self.assertEqual(audio.shape[1], 2)
                        self.assertGreater(float(np.sqrt(np.mean(audio[:, 0] ** 2))), 0.03)
                        self.assertLess(float(np.max(np.abs(audio[:, 1]))), 1e-8)

            # A true mono input is duplicated safely, not split into different phases.
            source = os.path.join(tmp, "mono.wav")
            base = os.path.join(tmp, "mono_base.wav")
            sf.write(source, left, sr, subtype="FLOAT")
            kt._apply_v4_base_fx(source, base)
            audio, _ = sf.read(base, always_2d=True)
            self.assertEqual(audio.shape[1], 2)
            self.assertLess(float(np.max(np.abs(audio[:, 0] - audio[:, 1]))), 1e-8)

    def test_real_carved_bands_and_safe_in_place_float_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = 48000
            source = os.path.join(tmp, "base.wav")
            rng = np.random.default_rng(128)
            original = rng.normal(0.0, 0.06, size=(sr * 3, 2))
            sf.write(source, original, sr, subtype="FLOAT")
            # Mirrors the post-Audacity in-place base-render path.
            kt._apply_v4_base_fx(source, source, already_macro=True)
            output, actual_sr = sf.read(source, always_2d=True)
            self.assertEqual(actual_sr, sr)
            self.assertEqual(sf.info(source).subtype, "FLOAT")
            self.assertEqual(output.shape, original.shape)
            self.assertFalse(os.path.exists(source + ".v4fx.wav"))

            trim = int(0.2 * sr)
            original, output = original[trim:-trim], output[trim:-trim]
            window = np.hanning(len(output))[:, None]
            before = np.abs(np.fft.rfft(original * window, axis=0)) ** 2
            after = np.abs(np.fft.rfft(output * window, axis=0)) ** 2
            frequencies = np.fft.rfftfreq(len(output), 1.0 / sr)

            def change_db(mask):
                return 10 * np.log10(float(np.sum(after[mask]) / np.sum(before[mask])))

            # Low mids stay intact; the visible notches are actual source cuts.
            self.assertAlmostEqual(change_db((frequencies >= 300) & (frequencies <= 700)), 0.0, delta=0.2)
            for center, half_width in kt._V4_SCULPT_NOTCHES:
                mask = np.abs(frequencies - center) <= half_width * 0.25
                with self.subTest(center=center):
                    self.assertLess(change_db(mask), -60.0)
            self.assertLess(change_db(frequencies >= 15000), -75.0)


if __name__ == "__main__":
    unittest.main()
