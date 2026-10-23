"""Offline regression tests: python -m unittest -v test_copyr8_macro.

No Discord login, Audacity, audio libraries, or recognition API calls required.
The Audacity boundary is mocked; actual Windows/plugin processing needs a host
with Audacity 2.3.3, mod-script-pipe, Blue Cat's Triple EQ and TDR Nova.
"""
import ast
import asyncio
import inspect
import math
import os
import types
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch

import audacity_processor as processor
from audacity_pipe import AudacitySession, parse_macro, SAFE_COMMAND_BYTES

ROOT = Path(__file__).resolve().parent


class BundledMacroTests(unittest.TestCase):
    def test_exact_step_order_and_settings(self):
        steps = parse_macro(processor.BUNDLED_V3_MACRO)
        self.assertEqual([name for name, _ in steps], [
            "ClipFix", "ChangeTempo", "ChangePitch", "BassAndTreble",
            "BcTripleEq4(stereo)", "TdrNova", "BassAndTreble",
        ])
        self.assertEqual([params for i, (_, params) in enumerate(steps) if i != 5], [
            'gain="-10.5" threshold="85"',
            'Percentage="2.5" SBSMS="0"',
            'Percentage="1.4" SBSMS="0"',
            'Bass="-17" Gain="0" Link_Sliders="0" Treble="0"',
            'Bandwidth="0.01369863" Bypass="0" Frequency="0.88573867" '
            'Gain="0.46250001" High="0.89565217" Low="0.59565216" Mid="0.073913045"',
            'Bass="0" Gain="0" Link_Sliders="0" Treble="-17"',
        ])
        nova = dict(param.split("=", 1) for param in shlex.split(steps[5][1]))
        self.assertEqual(nova["Band_3_Q"], "0.94682223")
        self.assertEqual(nova["Band_3_Threshold"], "0.31286213")
        self.assertEqual(nova["Wide_Band_Threshold"], "0.9493553")
        self.assertEqual(nova["Output_Gain"], "0.63857782")
        # This step MUST run via the installed macro, not the scripting pipe.
        self.assertGreater(len(":".join(steps[5])), SAFE_COMMAND_BYTES)

    def test_v4_exact_step_order_and_settings(self):
        # The user's original 17-step macro is not a replacement EQ chain.
        # Spectrogram shaping is added by the engine AFTER this whole macro.
        sc1 = ('Attack_time_(ms)="400" Knee_radius_(dB)="2" Makeup_gain_(dB)="5" '
               'Ratio_(1_n)="10" Release_time_(ms)="800" Threshold_level_(dB)="-15"')
        self.assertEqual(parse_macro(processor.BUNDLED_V4_MACRO), [
            ("SelectAll", ""),
            ("StereoToMono", 'Use_Preset="<Factory Defaults>"'),
            ("Duplicate", ""),
            ("SelectTracks", 'Mode="Set" Track="1" TrackCount="1"'),
            ("PanRight", ""),
            ("SelectTracks", 'Mode="Set" Track="0" TrackCount="1"'),
            ("PanLeft", ""),
            ("SelectTracks", 'Mode="Set" Track="1" TrackCount="1"'),
            ("Tremolo", 'lfo="4" phase="60" wave="Sine" wet="50"'),
            ("SelectTracks", 'Mode="Set" Track="1" TrackCount="1"'),
            ("Tremolo", 'lfo="4" phase="0" wave="Sine" wet="40"'),
            ("SelectAll", ""),
            ("MixAndRender", ""),
            ("SelectAll", ""),
            ("Sc1", sc1),
            ("SelectAll", ""),
            ("Sc1", sc1),
        ])

    def test_v4_install_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp):
            destination = Path(tmp) / processor.BUNDLED_V4_MACRO.name
            self.assertTrue(processor._install_bundled_macro(processor.BUNDLED_V4_MACRO))
            self.assertEqual(destination.read_bytes(), processor.BUNDLED_V4_MACRO.read_bytes())
            with patch.object(processor.os, "replace") as replace:
                self.assertFalse(processor._install_bundled_macro(processor.BUNDLED_V4_MACRO))
                replace.assert_not_called()

    def test_v4_install_precedes_session_and_macro_receives_all_steps(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp):
            output = Path(tmp) / "result.wav"
            installed = Path(tmp) / processor.BUNDLED_V4_MACRO.name
            # Simulate upgrading from the mistakenly substituted macro.
            installed.write_text('SelectAll:\nClipFix:gain="-4" threshold="92"\n')
            custom = Path(tmp) / "My custom chain.txt"
            custom.write_text('ChangeTempo:Percentage="1"')
            session = Mock()
            session.check_effects.return_value = ([], [])
            session.macro_menu_available.return_value = True
            session.apply_macro_to_file.side_effect = lambda *a, **kw: output.write_bytes(b"audio")

            def get_session():
                self.assertEqual(installed.read_bytes(), processor.BUNDLED_V4_MACRO.read_bytes())
                return session

            with patch.object(processor, "_get_session", side_effect=get_session), \
                 patch.object(processor, "_drop_session") as drop, \
                 patch.object(processor, "preserve_metadata", return_value=True):
                result = processor.run_macro_blocking(
                    "input.wav", str(output), str(processor.BUNDLED_V4_MACRO))
                drop.assert_called_once_with()
                self.assertEqual(len(result["steps"]), 17)
                self.assertTrue(result["metadata"])
                self.assertEqual(custom.read_text(), 'ChangeTempo:Percentage="1"')
                session.apply_macro_to_file.assert_called_once_with(
                    "input.wav", str(output), parse_macro(processor.BUNDLED_V4_MACRO),
                    macro_name=processor.BUNDLED_V4_MACRO.stem,
                )

    def test_install_is_exact_idempotent_and_updates_only_managed_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp):
            custom = Path(tmp) / "My custom chain.txt"
            custom.write_text("custom settings")
            destination = Path(tmp) / processor.BUNDLED_V3_MACRO.name
            self.assertTrue(processor._install_bundled_macro(processor.BUNDLED_V3_MACRO))
            self.assertEqual(destination.read_bytes(), processor.BUNDLED_V3_MACRO.read_bytes())
            with patch.object(processor.os, "replace") as replace:
                self.assertFalse(processor._install_bundled_macro(processor.BUNDLED_V3_MACRO))
                replace.assert_not_called()
            destination.write_text("outdated")
            self.assertTrue(processor._install_bundled_macro(processor.BUNDLED_V3_MACRO))
            self.assertEqual(destination.read_bytes(), processor.BUNDLED_V3_MACRO.read_bytes())
            self.assertEqual(custom.read_text(), "custom settings")
            self.assertEqual(len(list(Path(tmp).iterdir())), 2)

    def test_custom_macro_is_not_installed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp):
            custom = Path(tmp) / "Custom.txt"
            custom.write_text('ChangeTempo:Percentage="1"')
            self.assertFalse(processor._install_bundled_macro(custom))
            self.assertEqual(processor.resolve_macro("Custom"), str(custom))
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_install_failure_is_actionable_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp):
            with patch.object(processor.os, "replace", side_effect=PermissionError):
                with self.assertRaisesRegex(processor.MacroProcessingError, "folder permissions"):
                    processor._install_bundled_macro(processor.BUNDLED_V3_MACRO)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_missing_appdata_does_not_install_into_working_directory(self):
        with patch.object(processor, "MACRO_DIR", "Audacity/Macros"):
            with self.assertRaisesRegex(processor.MacroProcessingError, "APPDATA"):
                processor._install_bundled_macro(processor.BUNDLED_V3_MACRO)

    def test_install_precedes_session_and_macro_receives_all_steps(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp):
            output = Path(tmp) / "result.wav"
            session = Mock()
            session.check_effects.return_value = ([], [])
            session.macro_menu_available.return_value = True
            session.apply_macro_to_file.side_effect = lambda *a, **kw: output.write_bytes(b"audio")

            def get_session():
                installed = Path(tmp) / processor.BUNDLED_V3_MACRO.name
                self.assertEqual(installed.read_bytes(), processor.BUNDLED_V3_MACRO.read_bytes())
                return session

            with patch.object(processor, "_get_session", side_effect=get_session), \
                 patch.object(processor, "_drop_session") as drop, \
                 patch.object(processor, "preserve_metadata", return_value=True):
                result = processor.run_macro_blocking("input.wav", str(output), str(processor.BUNDLED_V3_MACRO))
                drop.assert_called_once_with()
                self.assertEqual(len(result["steps"]), 7)
                self.assertTrue(result["metadata"])
                session.apply_macro_to_file.assert_called_once_with(
                    "input.wav", str(output), parse_macro(processor.BUNDLED_V3_MACRO),
                    macro_name=processor.BUNDLED_V3_MACRO.stem,
                )
                # Repeated runs leave the installed file and cached session alone.
                drop.reset_mock()
                processor.run_macro_blocking("input.wav", str(output), str(processor.BUNDLED_V3_MACRO))
                drop.assert_not_called()

    def test_path_listed_vst_counts_as_installed(self):
        # Audacity 2.3.3 sometimes lists a VST by its file path instead of its
        # own name while still applying it via the macro runner (TDR Nova,
        # 2026-10-05); check_effects must treat that listing as installed.
        known = {"clipfix", "changetempo", "bctripleeq4stereo",
                 "cusersjamesvstpluginstdrnovadll0",
                 "cusersjamesappdataroamingaudacityplugintdrnovadll0"}
        self.assertTrue(AudacitySession._known_effect("TdrNova", known))
        self.assertTrue(AudacitySession._known_effect("BcTripleEq4(stereo)", known))
        self.assertTrue(AudacitySession._known_effect("ClipFix", known))
        self.assertFalse(AudacitySession._known_effect("NotARealPlugin", known))
        # substring matching must not hit non-path command ids
        self.assertFalse(AudacitySession._known_effect("Nova", {"companion"}))

    def test_missing_plugin_is_reported_without_partial_processing(self):
        session = Mock()
        session.check_effects.return_value = (["TdrNova"], [])
        session.registry_has_effects.return_value = False
        session.process = None
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp), \
             patch.object(processor, "_get_session", return_value=session), \
             patch.object(processor, "_drop_session"):
            with self.assertRaisesRegex(processor.MacroProcessingError, "TdrNova"):
                processor.run_macro_blocking("input.wav", "output.wav", str(processor.BUNDLED_V3_MACRO))
        session.apply_macro_to_file.assert_not_called()

    def test_stale_instance_restarts_when_registry_still_has_the_effect(self):
        # A PRE-EXISTING Audacity omits TdrNova from its effect list while
        # pluginregistry.cfg still has it enabled+valid: the processor must
        # kill that stale instance, launch a fresh one, and re-check instead
        # of failing the run. A fresh instance's list completes at once here.
        session = Mock()
        session.process = None                 # pre-existing: not launched by us
        session.check_effects.side_effect = [(["TdrNova"], []), ([], [])]
        session.registry_has_effects.return_value = True
        session.macro_menu_available.return_value = True
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp), \
             patch.object(processor, "_get_session", return_value=session) as get_session, \
             patch.object(processor, "_drop_session") as drop, \
             patch.object(processor.subprocess, "run") as kill, \
             patch.object(processor.time, "sleep"), \
             patch.object(processor, "preserve_metadata", return_value=True):
            output = Path(tmp) / "output.wav"
            session.apply_macro_to_file.side_effect = lambda *a, **kw: output.write_bytes(b"audio")
            processor.run_macro_blocking("input.wav", str(output), str(processor.BUNDLED_V3_MACRO))
        kill.assert_called_once()
        self.assertEqual(get_session.call_count, 2)
        session.apply_macro_to_file.assert_called_once()

    def test_our_own_fresh_instance_is_polled_until_the_scan_finishes(self):
        # A fresh launch we own lists VSTs only once its plugin scan finishes:
        # the processor must wait and re-check instead of restarting or failing.
        session = Mock()
        session.process = Mock()               # we launched this instance
        session.check_effects.side_effect = [(["TdrNova"], []),
                                             (["TdrNova"], []),
                                             ([], [])]
        session.registry_has_effects.return_value = True
        session.macro_menu_available.return_value = True
        with tempfile.TemporaryDirectory() as tmp, patch.object(processor, "MACRO_DIR", tmp), \
             patch.object(processor, "_get_session", return_value=session) as get_session, \
             patch.object(processor, "_drop_session") as drop, \
             patch.object(processor.subprocess, "run") as kill, \
             patch.object(processor.time, "sleep"), \
             patch.object(processor, "preserve_metadata", return_value=True):
            output = Path(tmp) / "output.wav"
            session.apply_macro_to_file.side_effect = lambda *a, **kw: output.write_bytes(b"audio")
            processor.run_macro_blocking("input.wav", str(output), str(processor.BUNDLED_V3_MACRO))
        kill.assert_not_called()               # our own instance is never killed
        drop.assert_called_once()              # only the install-sync drop
        self.assertEqual(get_session.call_count, 1)
        self.assertEqual(session.check_effects.call_count, 3)
        session.apply_macro_to_file.assert_called_once()


class PreserveMetadataTests(unittest.TestCase):
    """A metadata remux that breaks the file must roll back, not kill the run."""

    def _fake_run(self, calls, remux_writes, probe_ok):
        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[0].endswith("ffprobe"):
                return types.SimpleNamespace(
                    returncode=0 if probe_ok else 1,
                    stdout=b"{}" if probe_ok else b"",
                    stderr="")
            if remux_writes and cmd[0].endswith("ffmpeg"):
                Path(cmd[-1]).write_bytes(b"tagged wav" * 200)  # > 1KB size guard
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return fake_run

    def test_unreadable_remux_rolls_back_to_clean_export(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "orig.mp3"; original.write_bytes(b"mp3")
            processed = Path(tmp) / "out.wav"; processed.write_bytes(b"clean wav")
            with patch.object(processor.subprocess, "run",
                              side_effect=self._fake_run(calls, True, probe_ok=False)):
                ok = processor.preserve_metadata(str(original), str(processed))
            self.assertFalse(ok)
            self.assertEqual(processed.read_bytes(), b"clean wav")  # rolled back
        self.assertTrue(any(c[0].endswith("ffmpeg") for c in calls))

    def test_readable_remux_is_kept(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "orig.mp3"; original.write_bytes(b"mp3")
            processed = Path(tmp) / "out.wav"; processed.write_bytes(b"clean wav")
            with patch.object(processor.subprocess, "run",
                              side_effect=self._fake_run(calls, True, probe_ok=True)):
                ok = processor.preserve_metadata(str(original), str(processed))
            self.assertTrue(ok)
            self.assertEqual(processed.read_bytes(), b"tagged wav" * 200)
            self.assertEqual([p.name for p in Path(tmp).iterdir()],
                             ["orig.mp3", "out.wav"])  # backup cleaned up


class GapfreeWindowsTests(unittest.TestCase):
    """An unmeasurable file probes from the start instead of failing the run."""

    def _load(self, duration_fn):
        tree = ast.parse((ROOT / "killa_tools.py").read_text())
        nodes = [n for n in tree.body
                 if getattr(n, "name", None) == "_gapfree_windows"]
        self.assertEqual(len(nodes), 1)
        ns = {"math": math, "os": os, "SHA_PROBE_CLIP_SECONDS": 12.0,
              "_duration_seconds": duration_fn}
        exec(compile(ast.Module(body=nodes, type_ignores=[]),
                     "killa_tools.py", "exec"), ns)
        return ns["_gapfree_windows"]

    def test_unmeasurable_file_probes_from_the_start(self):
        def boom(path):
            raise RuntimeError("Could not read that file with ffprobe.")
        self.assertEqual(self._load(boom)("whatever.wav"), [0.0])

    def test_measurable_file_spreads_overlapping_windows(self):
        windows = self._load(lambda path: 252.0)("x.wav")
        self.assertEqual(windows[0], 0.0)
        self.assertLessEqual(max(windows), 252.0 - 12.0 + 0.1)
        self.assertGreater(len(windows), 2)


class FFProbeFallbackTests(unittest.TestCase):
    """ffprobe_json must fall back to ffmpeg's report when ffprobe fails."""

    def _load(self, run_fn):
        tree = ast.parse((ROOT / "killa_tools.py").read_text())
        names = {"ffprobe_json", "_ffmpeg_probe_json"}
        nodes = [n for n in tree.body if getattr(n, "name", None) in names]
        self.assertEqual(len(nodes), 2)
        ns = {"re": __import__("re"), "json": __import__("json"),
              "time": types.SimpleNamespace(sleep=lambda s: None),
              "_FFPROBE": ["ffprobe", "-v", "error"],
              "_run": run_fn}
        exec(compile(ast.Module(body=nodes, type_ignores=[]),
                     "killa_tools.py", "exec"), ns)
        return ns["ffprobe_json"]

    FFMPEG_REPORT = (
        "Input #0, mp3, from 'x.mp3':\n"
        "  Duration: 00:04:12.03, start: 0.025057, bitrate: 192 kb/s\n"
        "    Stream #0:0[0x3](und): Audio: mp3, 44100 Hz, stereo, fltp, 192 kb/s\n"
    )

    def test_ffprobe_failure_falls_back_to_ffmpeg_report(self):
        calls = []

        def run_fn(args, timeout=180):
            calls.append(args[0])
            if args[0].endswith("ffprobe"):
                return 1, "", ""           # the empty-error failure shape
            return 1, "", self.FFMPEG_REPORT

        data = self._load(run_fn)("x.mp3")
        self.assertEqual(calls.count("ffprobe"), 3)   # retried twice
        self.assertEqual(data["format"]["duration"], "252.03")
        self.assertEqual(data["format"]["bit_rate"], "192000")
        self.assertEqual(data["streams"][0]["codec_name"], "mp3")
        self.assertEqual(data["streams"][0]["sample_rate"], "44100")
        self.assertEqual(data["streams"][0]["channels"], 2)

    def test_parenthetical_codec_and_output_section_ignored(self):
        # Real ffmpeg reports "mp3 (mp3float)" and then re-lists the stream
        # in the Output section as the null muxer's pcm_s16le. The input
        # report must win, with the qualifier stripped from the codec name.
        report = (
            "Input #0, mp3, from 'x.mp3':\n"
            "  Duration: 00:04:12.09, start: 0.023021, bitrate: 241 kb/s\n"
            "    Stream #0:0: Audio: mp3 (mp3float), 48000 Hz, stereo,"
            " fltp, 241 kb/s, start 0.023021\n"
            "Output #0, null, to 'pipe:':\n"
            "    Stream #0:0: Audio: pcm_s16le, 48000 Hz, stereo, s16,"
            " 1536 kb/s\n"
        )

        def run_fn(args, timeout=180):
            if args[0].endswith("ffprobe"):
                return 1, "", ""
            return 1, "", report

        data = self._load(run_fn)("x.mp3")
        self.assertEqual(data["streams"][0]["codec_name"], "mp3")
        self.assertEqual(data["streams"][0]["sample_rate"], "48000")
        self.assertEqual(data["streams"][0]["channels"], 2)
        self.assertEqual(data["format"]["duration"], "252.09")
        self.assertEqual(data["format"]["bit_rate"], "241000")

    def test_broken_file_still_raises(self):
        def run_fn(args, timeout=180):
            if args[0].endswith("ffprobe"):
                return 1, "", ""
            return 1, "", "Output #0:"      # no Duration line

        with self.assertRaisesRegex(RuntimeError, "ffmpeg could not identify"):
            self._load(run_fn)("x.mp3")

    def test_na_duration_still_returns_stream_info(self):
        # A truncated/odd container can report "Duration: N/A" while the
        # stream is still identified -- analyze should get codec info and a
        # None duration instead of failing.
        report = (
            "Input #0, ogg, from 'x.ogg':\n"
            "  Duration: N/A, start: 0.000000, bitrate: N/A\n"
            "    Stream #0:0: Audio: vorbis, 48000 Hz, stereo, fltp, 160 kb/s\n"
        )

        def run_fn(args, timeout=180):
            if args[0].endswith("ffprobe"):
                return 1, "", ""
            return 1, "", report

        data = self._load(run_fn)("x.ogg")
        self.assertIsNone(data["format"]["duration"])
        self.assertEqual(data["streams"][0]["codec_name"], "vorbis")
        self.assertEqual(data["streams"][0]["sample_rate"], "48000")

    def test_garbage_file_error_carries_ffmpeg_tail(self):
        # When even ffmpeg can't read it, the error must include the tail of
        # its output so the log says WHY (e.g. a Discord error page, a
        # truncated download) instead of a bare message.
        def run_fn(args, timeout=180):
            if args[0].endswith("ffprobe"):
                return 1, "", ""
            return 1, "", ("x.ogg: Invalid data found when processing input\n"
                           "Error while decoding stream #0:0: Invalid data\n")

        with self.assertRaisesRegex(RuntimeError, "Invalid data found"):
            self._load(run_fn)("x.ogg")


class BotRoutingTests(unittest.TestCase):
    """Execute the bot's actual config/dispatch AST without starting Discord."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((ROOT / "audio_bot.py").read_text())

    def configured_macro(self, environment, name="AUTOFIT_V3_MACRO"):
        assignment = next(node for node in self.tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == name
                                  for t in node.targets))
        namespace = {"os": os, "HERE": str(ROOT)}
        with patch.dict(os.environ, environment, clear=True):
            exec(compile(ast.Module(body=[assignment], type_ignores=[]), "audio_bot.py", "exec"), namespace)
        return namespace[name]

    def test_unset_and_blank_configuration_use_bundled_macro(self):
        for name, bundled in (("AUTOFIT_V3_MACRO", processor.BUNDLED_V3_MACRO),
                              ("AUTOFIT_V4_MACRO", processor.BUNDLED_V4_MACRO)):
            for environment in ({}, {name.replace("AUTOFIT", "COPYR8"): "  "}):
                with self.subTest(name=name, environment=environment):
                    self.assertEqual(self.configured_macro(environment, name), str(bundled))

    def test_explicit_custom_override_is_preserved(self):
        self.assertEqual(self.configured_macro({"COPYR8_V3_MACRO": " Custom "}), "Custom")
        self.assertEqual(
            self.configured_macro({"COPYR8_V4_MACRO": " Custom "}, "AUTOFIT_V4_MACRO"),
            "Custom")

    def test_macro_fits_dispatch_supplies_macro_callback(self):
        handler = next(n for n in self.tree.body if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "copyr8_slash")
        dispatch = next(n for n in ast.walk(handler) if isinstance(n, ast.If)
                        and isinstance(n.test, ast.BoolOp)
                        and any(isinstance(v, ast.Name) and v.id == "is_v3"
                                for v in n.test.values)
                        and any(isinstance(child, ast.FunctionDef) and child.name == "fit_fn"
                                for child in n.body))
        code = compile(ast.Module(body=[dispatch], type_ignores=[]), "audio_bot.py", "exec")
        for preset in ("autofit", "autofit-v2", "autofit-v3", "autofit-v4"):
            with self.subTest(preset=preset), patch.object(processor, "run_macro_blocking") as run:
                tools = Mock()
                namespace = {"is_v3": preset == "autofit-v3", "is_v2": preset == "autofit-v2",
                             "is_v4": preset == "autofit-v4",
                             "killa_tools": tools,
                             "AUTOFIT_V3_MACRO": self.configured_macro({}),
                             "AUTOFIT_V4_MACRO": self.configured_macro({}, "AUTOFIT_V4_MACRO")}
                exec(code, namespace)
                namespace["fit_fn"]("in", "out", "spec", None)
                if preset == "autofit-v3":
                    callback = tools.copyr8_autofit_v3.call_args.kwargs["macro_fn"]
                    callback("in", "base", None)
                    run.assert_called_once_with("in", "base", str(processor.BUNDLED_V3_MACRO), None)
                elif preset == "autofit-v4":
                    callback = tools.copyr8_autofit_v4.call_args.kwargs["macro_fn"]
                    callback("in", "base", None)
                    run.assert_called_once_with("in", "base", str(processor.BUNDLED_V4_MACRO), None)
                else:
                    target = tools.copyr8_autofit_v2 if preset == "autofit-v2" else tools.copyr8_autofit
                    target.assert_called_once_with("in", "out", "spec", None)
                    tools.copyr8_autofit_v3.assert_not_called()
                    tools.copyr8_autofit_v4.assert_not_called()
                    run.assert_not_called()


class WhitelistTargetTests(unittest.TestCase):
    """/whitelist must accept a raw user id as well as a mention."""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "audio_bot.py").read_text())
        node = next(n for n in tree.body
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_resolve_whitelist_target")

        class FakeUser:
            def __init__(self, uid, mention=None):
                self.id = uid
                self.mention = mention or f"<@{uid}>"

        class FakeDiscord:
            class NotFound(Exception):
                pass

            class HTTPException(Exception):
                pass

        ns = {"discord": FakeDiscord}
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     "audio_bot.py", "exec"), ns)
        cls.fn = ns["_resolve_whitelist_target"]
        cls.FakeUser = FakeUser
        cls.FakeDiscord = FakeDiscord

    def _run(self, user, user_id, fetcher):
        class Client:
            async def fetch_user(self, uid):
                r = fetcher(uid)
                if inspect.isawaitable(r):
                    return await r
                return r

        class Interaction:
            client = Client()

        return asyncio.run(type(self).fn(Interaction(), user, user_id))

    def test_mention_wins(self):
        u = self.FakeUser(123)
        uid, target, err = self._run(u, "999999999999999999", lambda _: None)
        self.assertEqual((uid, target, err), (123, u, None))

    def test_user_id_resolves(self):
        u = self.FakeUser(1495422390505570537)
        uid, target, err = self._run(None, "1495422390505570537",
                                     lambda i: u if i == 1495422390505570537
                                     else self._raise_notfound())
        self.assertEqual((uid, target, err), (1495422390505570537, u, None))

    def test_user_id_non_numeric_errors(self):
        uid, target, err = self._run(None, "abc123", lambda _: None)
        self.assertIsNone(uid)
        self.assertIsNone(target)
        self.assertIn("must be a number", err)

    def test_user_id_unknown_allows_removal_by_id(self):
        uid, target, err = self._run(None, "111111111111111111",
                                     self._raise_notfound)
        self.assertEqual(uid, 111111111111111111)
        self.assertIsNone(target)
        self.assertIsNone(err)

    def test_empty_gives_no_target(self):
        self.assertEqual(self._run(None, None, lambda _: None), (None, None, None))

    def _raise_notfound(self, _uid=None):
        raise self.FakeDiscord.NotFound("unknown user")


if __name__ == "__main__":
    unittest.main()
