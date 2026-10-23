"""Offline SongRec provider regression tests; no network calls or credentials."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import killa_tools as kt
import songrec_backend as backend


class ResponseTests(unittest.TestCase):
    def test_empty_success_is_no_match(self):
        count, info = backend.parse_response({"matches": []})
        self.assertEqual(count, 0)
        self.assertFalse(info["matched"])

    def test_raw_shazam_track_response(self):
        raw = {
            "matches": [{"id": "12345", "offset": 14.5}],
            "track": {
                "title": "Bohemian Rhapsody",
                "subtitle": "Queen",
                "sections": [{
                    "type": "SONG",
                    "metadata": [
                        {"title": "Album", "text": "A Night at the Opera"},
                        {"title": "Released", "text": "1975"}
                    ]
                }],
                "images": {"coverart": "https://example.com/cover.jpg"},
                "url": "https://shazam.com/track/12345"
            }
        }
        count, info = backend.parse_response(raw)
        self.assertEqual(count, 1)
        self.assertTrue(info["matched"])
        self.assertEqual(info["song_name"], "Bohemian Rhapsody")
        self.assertEqual(info["artist"], "Queen")
        self.assertEqual(info["album"], "A Night at the Opera")
        self.assertEqual(info["release_date"], "1975")
        self.assertEqual(info["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(info["provider"], "SongRec")

    def test_flat_songrec_response(self):
        flat = {
            "title": "Hotel California",
            "artist": "Eagles",
            "album": "Hotel California",
            "release_year": "1976",
            "cover_url": "https://example.com/hotel.jpg"
        }
        count, info = backend.parse_response(flat)
        self.assertEqual(count, 1)
        self.assertTrue(info["matched"])
        self.assertEqual(info["song_name"], "Hotel California")
        self.assertEqual(info["artist"], "Eagles")
        self.assertEqual(info["album"], "Hotel California")
        self.assertEqual(info["release_date"], "1976")

    def test_text_songrec_response(self):
        text = "Pink Floyd - Comfortably Numb"
        count, info = backend.parse_response(text)
        self.assertEqual(count, 1)
        self.assertTrue(info["matched"])
        self.assertEqual(info["artist"], "Pink Floyd")
        self.assertEqual(info["song_name"], "Comfortably Numb")

    def test_malformed_and_failed_responses(self):
        for data in (None, "", "invalid json string without dash", [], 123):
            with self.subTest(data=data):
                count, info = backend.parse_response(data)
                self.assertIn(count, (None, 0))
                self.assertFalse(info["matched"])


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = Path(self.tmp.name) / backend._SONGREC_NAME
        self.exe.write_bytes(b"#!/bin/sh\n")
        self.exe.chmod(0o755)

    def resolve(self, value):
        with patch.dict(os.environ, {"SONGREC_PATH": value}):
            return backend.resolve_songrec()

    def test_absolute_path_and_quoted_path(self):
        for value in (str(self.exe), f"'{self.exe}'", f'"{self.exe}"', self.tmp.name):
            with self.subTest(value=value):
                self.assertEqual(os.path.normcase(self.resolve(value)),
                                 os.path.normcase(str(self.exe)))

    def test_relative_path_resolution(self):
        with patch.object(backend, "HERE", self.tmp.name):
            found = self.resolve(os.path.join(".", backend._SONGREC_NAME))
        self.assertEqual(os.path.normcase(os.path.abspath(found)),
                         os.path.normcase(str(self.exe)))

    def test_missing_path_fails(self):
        missing = os.path.join(self.tmp.name, "missing-folder", backend._SONGREC_NAME)
        with self.assertRaisesRegex(backend.SongRecSetupError, "SONGREC_PATH is set"):
            self.resolve(missing)

    def test_unset_prefers_bundled_then_path(self):
        with patch.object(backend, "BUNDLED_SONGREC", str(self.exe)):
            self.assertEqual(os.path.normcase(self.resolve("")), os.path.normcase(str(self.exe)))


class TripleProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "test_track.wav")
        Path(self.src).write_bytes(b"test wav data")
        self.patch_run = patch.object(kt, "_run", return_value=(0, "", ""))
        self.patch_dur = patch.object(kt, "_duration_seconds", return_value=30.0)
        self.patch_ac_avail = patch.object(kt.acoustid_backend, "is_available", return_value=True)
        self.patch_avail = patch.object(kt.songrec_backend, "is_available", return_value=True)
        self.patch_run.start()
        self.patch_dur.start()
        self.patch_ac_avail.start()
        self.patch_avail.start()
        self.addCleanup(self.patch_run.stop)
        self.addCleanup(self.patch_dur.stop)
        self.addCleanup(self.patch_ac_avail.stop)
        self.addCleanup(self.patch_avail.stop)

    def test_candidate_needs_all_three_providers_to_return_zero(self):
        with patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(kt.acoustid_backend, "match_count", return_value=0), \
             patch.object(kt.songrec_backend, "match_count", return_value=0):
            ok, counts = kt._probe_sweep(self.src, [0.0])
            self.assertTrue(ok)
            self.assertEqual(counts, [0, 0, 0])  # Shazam, AcoustID, SongRec

        # If SongRec catches it (>0), candidate fails
        with patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(kt.acoustid_backend, "match_count", return_value=0), \
             patch.object(kt.songrec_backend, "match_count", return_value=1):
            ok, counts = kt._probe_sweep(self.src, [0.0])
            self.assertFalse(ok)
            self.assertEqual(counts, [0, 0, 1])

    def test_any_original_control_verifies(self):
        # SongRec alone verifies original
        with patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(kt.acoustid_backend, "match_count", return_value=0), \
             patch.object(kt.songrec_backend, "match_count", return_value=1):
            self.assertTrue(kt._control_check(self.src))

        # None of the 3 knows the original -> False (unverified)
        with patch.object(kt, "_shazam_match_count", return_value=0), \
             patch.object(kt.acoustid_backend, "match_count", return_value=0), \
             patch.object(kt.songrec_backend, "match_count", return_value=0):
            self.assertFalse(kt._control_check(self.src))


if __name__ == "__main__":
    unittest.main()
