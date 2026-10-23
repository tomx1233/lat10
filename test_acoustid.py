"""Offline dual-provider regression tests; no network calls or credentials."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import acoustid_backend as backend
import killa_tools as kt


class ResponseTests(unittest.TestCase):
    def test_empty_success_is_no_match(self):
        self.assertEqual(backend.parse_response({"status": "ok", "results": []}), 0)

    def test_ids_without_musicbrainz_metadata_still_count(self):
        rows = [{"id": "track-one", "score": 0.9}, {"id": "track-two", "score": 0.01}]
        self.assertEqual(backend.parse_response({"status": "ok", "results": rows}), 2)

    def test_malformed_and_failed_responses_are_inconclusive(self):
        for data in (None, [], {}, {"status": "ok"}, {"status": "ok", "results": None},
                     {"status": "error", "results": [], "error": {"code": 3}},
                     {"status": "ok", "results": "[]"},
                     {"status": "ok", "results": [{}]},
                     {"status": "ok", "results": [{"id": "x", "score": float("nan")}]},
                     {"status": "ok", "results": [{"id": "x", "score": True}]}):
            with self.subTest(data=data):
                self.assertIsNone(backend.parse_response(data))

    def test_invalid_key_is_actionable_and_redacted(self):
        with self.assertRaisesRegex(backend.AcoustIDSetupError, "application/client key") as err:
            backend.parse_response({"status": "error", "error": {"code": 4, "message": "secret"}})
        self.assertNotIn("secret", str(err.exception))

    def test_missing_key_fails_setup(self):
        with patch.dict(os.environ, {"ACOUSTID_API_KEY": ""}):
            with self.assertRaisesRegex(backend.AcoustIDSetupError, "ACOUSTID_API_KEY"):
                backend.validate_setup()

    def test_missing_fpcalc_fails_setup(self):
        with patch.dict(os.environ, {"ACOUSTID_API_KEY": "test-only"}), \
             patch.object(backend.shutil, "which", return_value=None), \
             patch.dict("sys.modules", {"acoustid": Mock()}):
            with self.assertRaisesRegex(backend.AcoustIDSetupError, "fpcalc"):
                backend.validate_setup()


class FpcalcResolutionTests(unittest.TestCase):
    """Uses a real temporary executable file; no network, no real fpcalc needed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = Path(self.tmp.name) / backend._FPCALC_NAME
        self.exe.write_bytes(b"#!/bin/sh\n")
        self.exe.chmod(0o755)

    def resolve(self, value):
        with patch.dict(os.environ, {"FPCALC": value}):
            return backend.resolve_fpcalc()

    def test_absolute_path_quoted_path_and_folder(self):
        for value in (str(self.exe), f"'{self.exe}'", f'"{self.exe}"', self.tmp.name):
            with self.subTest(value=value):
                self.assertEqual(os.path.normcase(self.resolve(value)),
                                 os.path.normcase(str(self.exe)))

    def test_relative_path_is_relative_to_the_bot_folder(self):
        with patch.object(backend, "HERE", self.tmp.name):
            found = self.resolve(os.path.join(".", backend._FPCALC_NAME))
        self.assertEqual(os.path.normcase(os.path.abspath(found)),
                         os.path.normcase(str(self.exe)))

    def test_escape_mangled_double_quoted_windows_path_is_explained(self):
        with self.assertRaisesRegex(backend.AcoustIDSetupError, "double") as err:
            self.resolve("C:\tools\new\fpcalc.exe")  # what dotenv makes of "C:\tools\new\..."
        self.assertNotIn("C:", str(err.exception))

    def test_configured_but_missing_path_fails_without_echoing_it(self):
        missing = os.path.join(self.tmp.name, "private-folder", backend._FPCALC_NAME)
        with self.assertRaisesRegex(backend.AcoustIDSetupError, "FPCALC is set") as err:
            self.resolve(missing)
        self.assertNotIn("private-folder", str(err.exception))

    def test_unset_prefers_bundled_tools_copy_then_path(self):
        with patch.object(backend, "BUNDLED_FPCALC", str(self.exe)):
            self.assertEqual(os.path.normcase(self.resolve("")), os.path.normcase(str(self.exe)))
        with patch.object(backend, "BUNDLED_FPCALC", str(self.exe) + ".missing"), \
             patch.dict(os.environ, {"PATH": self.tmp.name}):
            self.assertEqual(os.path.normcase(self.resolve("")), os.path.normcase(str(self.exe)))


class FingerprintTests(unittest.TestCase):
    def test_fingerprints_file_start_with_total_duration_and_timeout(self):
        response = Mock(returncode=0, stdout=json.dumps({"duration": 241.5, "fingerprint": "abc"}))
        with patch.object(backend.subprocess, "run", return_value=response) as run:
            self.assertEqual(backend.fingerprint_file("input.wav", "fpcalc"), (241.5, "abc"))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["fpcalc", "-json", "-length", "120", os.path.abspath("input.wav")])
        self.assertEqual(kwargs["timeout"], backend.FINGERPRINT_TIMEOUT)

    def test_decoder_failure_or_incomplete_output_is_not_a_miss(self):
        responses = [Mock(returncode=1), Mock(returncode=0, stdout="not json"),
                     Mock(returncode=0, stdout='{"duration": 30}'),
                     Mock(returncode=0, stdout='{"duration": 0, "fingerprint": "abc"}')]
        for response in responses:
            with patch.object(backend.subprocess, "run", return_value=response):
                with self.assertRaises(ValueError):
                    backend.fingerprint_file("input.wav", "fpcalc")


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.env = patch.dict(os.environ, {"ACOUSTID_API_KEY": "private-test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for patcher in (patch.object(backend, "validate_setup", return_value=(self.api, "fpcalc")),
                        patch.object(backend, "fingerprint_file", return_value=(240, "fingerprint")),
                        patch.object(backend.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_https_lookup_and_progress(self):
        self.api.lookup.return_value = {"status": "ok", "results": []}
        log = []
        self.assertEqual(backend.match_count("file.wav", log.append), 0)
        self.api.set_base_url.assert_called_with("https://api.acoustid.org/v2/")
        self.api.lookup.assert_called_once_with("private-test-key", "fingerprint", 240,
                                               meta="", timeout=backend.REQUEST_TIMEOUT)
        self.assertNotIn("private-test-key", " ".join(log))

    def test_transient_failures_retry_but_never_pass(self):
        self.api.lookup.side_effect = RuntimeError("do not echo private-test-key")
        log = []
        self.assertIsNone(backend.match_count("file.wav", log.append))
        self.assertEqual(self.api.lookup.call_count, backend.ATTEMPTS)
        self.assertNotIn("private-test-key", " ".join(log))

    def test_throttle_retry_then_success(self):
        self.api.lookup.side_effect = [
            {"status": "error", "error": {"code": 3}},
            {"status": "ok", "results": [{"id": "track", "score": 1.0}]},
        ]
        self.assertEqual(backend.match_count("file.wav"), 1)
        self.assertEqual(self.api.lookup.call_count, 2)

    def test_request_spacing_and_backoff(self):
        self.api.lookup.side_effect = [{}, {"status": "ok", "results": []}]
        with patch.object(backend, "_last_request", 10.0), \
             patch.object(backend.time, "monotonic", return_value=10.0), \
             patch.object(backend.time, "sleep") as sleep:
            self.assertEqual(backend.match_count("file.wav"), 0)
        self.assertIn(((1.0,), {}), sleep.call_args_list)
        self.assertIn(((2,), {}), sleep.call_args_list)

    def test_bad_key_stops_without_retry(self):
        self.api.lookup.return_value = {"status": "error", "error": {"code": 4}}
        with self.assertRaises(backend.AcoustIDSetupError):
            backend.match_count("file.wav")
        self.api.lookup.assert_called_once()

    def test_fingerprint_timeout_does_not_call_api(self):
        with patch.object(backend, "fingerprint_file", side_effect=subprocess.TimeoutExpired("fpcalc", 120)):
            self.assertIsNone(backend.match_count("file.wav"))
        self.api.lookup.assert_not_called()


class ProbeGeometryTests(unittest.TestCase):
    """The sweep must actually probe the passage it claims to probe.

    Two historical defects made /copyr8 flag results wrong: the gap-free grid
    skipped the head and tail of every track (the sparse sampler's 5% inset),
    and shifted probe clips were cut with output-side -ss/-t while the atempo
    chain re-timed the stream, so ffmpeg seeked in the FILTERED timeline and
    sampled source start*tempo_factor onward (tail windows ran off the end of
    the file and read as empty = "no match").
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = str(Path(self.tmp.name) / "song.wav")
        Path(self.source).write_bytes(b"fake audio")

    def windows_for(self, duration):
        with patch.object(kt, "_duration_seconds", return_value=duration):
            return kt._gapfree_windows(self.source)

    def test_gapfree_covers_head_tail_and_overlaps(self):
        clip = kt.SHA_PROBE_CLIP_SECONDS
        for duration in (12.5, 25.0, 30.0, 60.0, 180.0, 300.0, 600.0, 33.3):
            with self.subTest(duration=duration):
                windows = self.windows_for(duration)
                self.assertEqual(windows[0], 0.0,
                                 "the intro must be inside the first probe")
                self.assertGreaterEqual(windows[-1] + clip, duration - 0.1,
                                        "the fade-out must be inside the last probe")
                self.assertLessEqual(windows[-1], duration - clip + 0.1,
                                     "the last probe must start inside the file")
                for a, b in zip(windows, windows[1:]):
                    self.assertLess(b, a + clip,
                                    "consecutive probes must overlap, not abut")

    def test_short_files_probe_the_whole_file_from_zero(self):
        for duration in (3.0, 10.0, 12.0):
            with self.subTest(duration=duration):
                self.assertEqual(self.windows_for(duration), [0.0])

    def test_shifted_probe_seeks_the_input_not_the_filtered_timeline(self):
        def render(args):
            Path(args[-1]).write_bytes(b"audio")
            return 0, "", ""

        with patch.object(kt, "_run", side_effect=render) as run, \
             patch.object(kt, "_sample_rate", return_value=44100), \
             patch.object(kt, "_duration_seconds", return_value=60.0), \
             patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(backend, "is_available", return_value=True), \
             patch.object(backend, "match_count", return_value=0), \
             patch.object(kt.songrec_backend, "is_available", return_value=False):
            ok, counts = kt._probe_sweep_shifted(self.source, [14.4, 25.2],
                                                 -0.5, 1.10)
        self.assertTrue(ok)
        self.assertEqual(counts, [0, 0, 0])
        self.assertEqual(len(run.call_args_list), 3)  # two clips + full render
        for call in run.call_args_list[:2]:
            args = call.args[0]
            self.assertLess(args.index("-ss"), args.index("-i"),
                            "probe clips must seek before -i so the cut lands "
                            "at the window position in the SOURCE timeline")
            self.assertLess(args.index("-t"), args.index("-i"))
            self.assertIn("atempo=", args[args.index("-af") + 1])
        full = run.call_args_list[-1].args[0]
        self.assertNotIn("-ss", full)
        self.assertNotIn("-t", full)

    def test_shifted_probe_clip_stays_full_length_after_atempo(self):
        def render(args):
            Path(args[-1]).write_bytes(b"audio")
            return 0, "", ""

        clip = kt.SHA_PROBE_CLIP_SECONDS
        for tempo in (1.0, 1.07, 1.22, 1.30):
            with self.subTest(tempo=tempo), \
                 patch.object(kt, "_run", side_effect=render) as run, \
                 patch.object(kt, "_sample_rate", return_value=44100), \
                 patch.object(kt, "_duration_seconds", return_value=60.0), \
                 patch.object(kt, "_shazam_match_count", return_value=1), \
                 patch.object(backend, "match_count", return_value=0):
                kt._probe_sweep_shifted(self.source, [48.0], -0.5, tempo)
                args = run.call_args_list[0].args[0]
                cut = float(args[args.index("-t") + 1])
                start = float(args[args.index("-ss") + 1])
                # post-atempo length = cut / tempo must not drop below clip
                self.assertGreaterEqual(round(cut / tempo, 3), clip)
                # the longer cut is pulled back so it still fits in the file
                self.assertLessEqual(start + cut, 60.0 + 1e-6)


class DualProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = str(Path(self.tmp.name) / "source.wav")
        Path(self.source).write_bytes(b"fake audio")
        for patcher in (patch.object(kt, "_run", return_value=(0, "", "")),
                        patch.object(kt, "_sample_rate", return_value=44100),
                        patch.object(kt, "_duration_seconds", return_value=30),
                        patch.object(kt.acoustid_backend, "is_available", return_value=True),
                        patch.object(kt.songrec_backend, "validate_setup"),
                        patch.object(kt.songrec_backend, "is_available", return_value=False),
                        patch.object(kt.songrec_backend, "match_count", return_value=0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_candidate_needs_both_providers_to_return_zero(self):
        for acoustic, expected in ((0, True), (1, False), (None, False)):
            with self.subTest(acoustic=acoustic), \
                 patch.object(kt, "_shazam_match_count", return_value=0), \
                 patch.object(backend, "match_count", return_value=acoustic) as api:
                ok, counts = kt._probe_sweep(self.source, [0, 12])
                self.assertEqual(ok, expected)
                self.assertEqual(counts, [0, 0, acoustic])
                api.assert_called_once_with(self.source, progress=None)

    def test_shazam_rejection_short_circuits_acoustid(self):
        for shazam in (1, None):
            with patch.object(kt, "_shazam_match_count", return_value=shazam), \
                 patch.object(backend, "match_count") as api:
                self.assertFalse(kt._probe_sweep(self.source, [0, 12])[0])
                api.assert_not_called()

    def test_empty_windows_cannot_pass(self):
        with patch.object(backend, "match_count") as api:
            self.assertEqual(kt._probe_sweep(self.source, []), (False, []))
            api.assert_not_called()

    def test_either_original_control_verifies(self):
        # One provider identifying the original is enough evidence; False only
        # when BOTH answer definitively with no match; None when nothing
        # matched and an answer was inconclusive.
        cases = [(1, 1, True), (1, 0, True), (0, 1, True), (0, 0, False),
                 (1, None, True), (None, 1, True), (None, 0, None),
                 (0, None, None), (None, None, None)]
        for shazam, acoustic, expected in cases:
            with self.subTest(shazam=shazam, acoustic=acoustic), \
                 patch.object(kt, "_shazam_match_count", return_value=shazam), \
                 patch.object(backend, "match_count", return_value=acoustic):
                self.assertIs(kt._control_check(self.source), expected)

    def test_shazam_match_on_original_skips_acoustid_control(self):
        with patch.object(kt, "_shazam_match_count", return_value=2), \
             patch.object(backend, "match_count") as api:
            self.assertIs(kt._control_check(self.source), True)
            api.assert_not_called()

    def test_shifted_acoustid_probe_uses_full_candidate_with_tremolo(self):
        candidate = Path(self.tmp.name) / "acoustid_candidate.wav"
        def render(args):
            Path(args[-1]).write_bytes(b"audio")
            return 0, "", ""
        with patch.object(kt, "_run", side_effect=render) as run, \
             patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(backend, "match_count", return_value=0) as api:
            ok, counts = kt._probe_sweep_shifted(self.source, [0, 12], -0.5, 1.03, 0.1, 2.0)
        self.assertTrue(ok)
        self.assertEqual(counts, [0, 0, 0])
        api.assert_called_once_with(str(candidate), progress=None)
        full_render = run.call_args_list[-1].args[0]
        self.assertNotIn("-t", full_render)
        self.assertNotIn("-ss", full_render)
        self.assertIn("tremolo=f=2:d=0.1", full_render[full_render.index("-af") + 1])
        self.assertFalse(candidate.exists())
        self.assertFalse((Path(self.tmp.name) / "shift_probe.wav").exists())

    def test_unverified_original_gets_the_balanced_protection_shift(self):
        output = str(Path(self.tmp.name) / "out.wav")
        rungs = [(0.1, -0.1, 1.0, 0.0, 0.0), (0.2, -0.2, 1.0, 0.0, 0.0)]
        def render(_source, destination, _pitch, _tempo):
            Path(destination).write_bytes(b"audio")
        with patch.dict("sys.modules", {"shazamio": Mock()}), \
             patch.object(backend, "validate_setup"), \
             patch.object(kt, "_probe_sweep_shifted", side_effect=[(False, [1]), (True, [0, 0])]), \
             patch.object(kt, "_control_check", return_value=False), \
             patch.object(kt, "_render_shifted_wav", side_effect=render):
            result = kt.find_mildest(self.source, output, use_tremolo=False, rungs=rungs)
        # Neither database knows the track, so the trivially clean -0.2 st
        # rung is replaced with real protection (+2.00 st / 1.07x).
        self.assertEqual((result["pitch"], result["tempo"]), (2.0, 1.07))
        self.assertEqual(result["cost"], 2.7)
        self.assertIs(result["verified"], False)
        self.assertIsNotNone(result["counts"])

    def test_unverified_original_skips_the_tremolo_overlay(self):
        output = str(Path(self.tmp.name) / "out.wav")
        rungs = [(0.1, -0.1, 1.05, 0.0, 0.0)]
        def render(_source, destination, _pitch, _tempo):
            Path(destination).write_bytes(b"audio")
        with patch.dict("sys.modules", {"shazamio": Mock()}), \
             patch.object(backend, "validate_setup"), \
             patch.object(kt, "_probe_sweep_shifted", return_value=(True, [0, 0])), \
             patch.object(kt, "_control_check", return_value=False), \
             patch.object(kt, "_render_overlay") as overlay, \
             patch.object(kt, "_render_shifted_wav", side_effect=render) as shifted:
            result = kt.find_mildest(self.source, output, use_tremolo=True, rungs=rungs)
        overlay.assert_not_called()
        shifted.assert_called_once()
        self.assertEqual(shifted.call_args.args[2:], (2.0, 1.07))
        self.assertFalse(result["overlay_applied"])

    def test_unavailable_acoustid_cannot_produce_a_clean_winner(self):
        output = str(Path(self.tmp.name) / "out.wav")
        rungs = [(0.1, -0.1, 1.0, 0.0, 0.0)]
        with patch.dict("sys.modules", {"shazamio": Mock()}), \
             patch.object(backend, "validate_setup"), \
             patch.object(kt, "_probe_sweep_shifted", return_value=(False, [0, None])):
            with self.assertRaisesRegex(RuntimeError, "never returned a usable answer"):
                kt.find_mildest(self.source, output, use_tremolo=False, rungs=rungs)
        self.assertFalse(Path(output).exists())

    def test_shifted_candidate_cleanup_on_api_error(self):
        candidate = Path(self.tmp.name) / "acoustid_candidate.wav"
        candidate.write_bytes(b"audio")
        with patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(backend, "match_count", side_effect=backend.AcoustIDSetupError("bad key")):
            with self.assertRaises(backend.AcoustIDSetupError):
                kt._probe_sweep_shifted(self.source, [0], -0.5, 1.03)
        self.assertFalse(candidate.exists())


if __name__ == "__main__":
    unittest.main()
