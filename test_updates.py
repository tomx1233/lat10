"""Comprehensive regression tests for all 9 bot updates and bug fixes."""

import asyncio
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from PIL import Image
import soundfile as sf

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DISCORD_TOKEN", "test-token-placeholder")
os.environ.setdefault("ROBLOX_COOKIE", "")

import audio_bot  # noqa: E402
import grant_audio  # noqa: E402
import killa_tools as kt  # noqa: E402
import shazam_backend  # noqa: E402
import songrec_backend  # noqa: E402
import verify_audio  # noqa: E402


def _make_stereo_music_wav(path: str, duration_s: float = 6.0, sr: int = 48000):
    """Synthesize a rich stereo harmonic test clip with bass, mids, highs, and air."""
    t = np.linspace(0.0, duration_s, int(sr * duration_s), endpoint=False)
    env = 0.65 + 0.30 * np.sin(2 * np.pi * 1.5 * t)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for idx, freq in enumerate((110, 220, 440, 880, 1760, 3520, 6200, 9800, 13800, 16500)):
        amp = 0.14 / (1.0 + 0.18 * idx)
        left += amp * np.sin(2 * np.pi * freq * t + 0.3 * idx)
        right += amp * np.cos(2 * np.pi * (freq * 1.002) * t + 0.5 * idx)
    rng = np.random.default_rng(42)
    hiss_l = rng.normal(0.0, 0.015, size=t.shape)
    hiss_r = rng.normal(0.0, 0.015, size=t.shape)
    stereo = np.stack([(left + hiss_l) * env, (right + hiss_r) * env], axis=1)
    stereo = np.clip(stereo, -0.92, 0.92).astype(np.float32)
    sf.write(path, stereo, sr)
    return path


class VerifyAudioAndHelperFixesTests(unittest.TestCase):
    def test_extract_asset_id_supports_ids_uris_and_creator_hub_urls(self):
        self.assertEqual(verify_audio.extract_asset_id("1848354536"), 1848354536)
        self.assertEqual(verify_audio.extract_asset_id("rbxassetid://1848354536"), 1848354536)
        self.assertEqual(
            verify_audio.extract_asset_id("https://www.roblox.com/library/1848354536/Track"),
            1848354536,
        )
        self.assertEqual(
            verify_audio.extract_asset_id(
                "<https://create.roblox.com/store/asset/1848354536/Audio>"
            ),
            1848354536,
        )
        with self.assertRaises(verify_audio.AssetError):
            verify_audio.extract_asset_id("not-an-id")

    def test_killa_tools_get_returns_2_tuple_on_network_error_by_default(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            status, body = kt._get("https://example.invalid/test", return_headers=False)
            self.assertIsNone(status)
            self.assertEqual(body, "")
            status3, body3, hdrs3 = kt._get("https://example.invalid/test", return_headers=True)
            self.assertIsNone(status3)
            self.assertEqual(body3, "")
            self.assertEqual(hdrs3, {})

    def test_grant_audio_require_config_accepts_supported_groups_json(self):
        with patch.object(grant_audio, "SUPPORTED_GROUP_IDS", set()), \
             patch.object(grant_audio, "supported_groups", return_value={101224973}):
            grant_audio._require_config(cookie="fake_cookie_token")

    def test_songrec_py_execution_has_sys_and_never_fakes_clean_on_missing_shazamio(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "sample.wav")
            _make_stereo_music_wav(wav, duration_s=1.5)
            # Simulate missing shazamio inside a child script: must return None (inconclusive), NOT 0!
            fake_script = os.path.join(tmp, "fake_songrec.py")
            Path(fake_script).write_text(
                "import sys\nsys.stderr.write('missing shazamio\\n')\nsys.exit(2)\n",
                encoding="utf-8",
            )
            rc, out, _err = songrec_backend.run_songrec(wav, executable=fake_script, timeout=10)
            self.assertNotEqual(rc, 0)
            self.assertEqual(out, "")
            with patch.object(songrec_backend, "validate_setup", return_value=fake_script):
                self.assertIsNone(songrec_backend.match_count(wav))

    def test_shazam_backend_awaits_async_close(self):
        closed = {"awaited": False}

        class FakeAsyncShazam:
            def __init__(self, **kwargs):
                pass

            async def recognize(self, path):
                return {"matches": [], "track": None}

            async def close(self):
                closed["awaited"] = True

        svc = shazam_backend.AudioRecognition()
        with patch.object(shazam_backend, "Shazam", FakeAsyncShazam):
            res = asyncio.run(svc._recognize_once("dummy.wav", {"recognize_timeout_seconds": 5}))
        self.assertEqual(res, {"matched": False})
        self.assertTrue(closed["awaited"])


class SpotifyAndSoundCloudDownloadTests(unittest.TestCase):
    def test_url_normalization_handles_spotify_uris_shortlinks_and_soundcloud_params(self):
        self.assertEqual(
            kt._normalize_download_url("<spotify:track:4cOdK2wGLETKBW3PvgPWqT>"),
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        )
        self.assertEqual(
            kt._normalize_download_url(
                "https://m.soundcloud.com/artist-name/cool-song?si=abc123&utm_source=clipboard&secret_token=s-xyz"
            ),
            "https://soundcloud.com/artist-name/cool-song?secret_token=s-xyz",
        )
        with patch.object(
            kt,
            "_resolve_redirect_url",
            return_value="https://open.spotify.com/intl-es/track/7GhIk7Il098yCjg4BQjzvb?si=1",
        ):
            resolved = kt._normalize_download_url("https://spotify.link/AbCdEf123")
            self.assertTrue(kt._is_spotify_url(resolved))

    def test_spotify_embed_json_parses_track_and_playlist(self):
        track_html = (
            '<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({
                "props": {
                    "pageProps": {
                        "state": {
                            "data": {
                                "entity": {
                                    "name": "Midnight City",
                                    "artists": [{"name": "M83"}],
                                    "duration": 243000,
                                }
                            }
                        }
                    }
                }
            })
            + "</script></body></html>"
        )
        meta = kt._parse_spotify_embed_json(track_html)
        self.assertEqual(meta["title"], "Midnight City")
        self.assertEqual(meta["artist"], "M83")
        self.assertAlmostEqual(meta["duration_secs"], 243.0)

        playlist_html = (
            '<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({
                "props": {
                    "pageProps": {
                        "state": {
                            "data": {
                                "entity": {
                                    "title": "Synthwave Mix",
                                    "trackList": [
                                        {
                                            "title": "Nightcall",
                                            "subtitle": "Kavinsky",
                                            "duration": 258000,
                                        }
                                    ],
                                }
                            }
                        }
                    }
                }
            })
            + "</script></body></html>"
        )
        pmeta = kt._parse_spotify_embed_json(playlist_html)
        self.assertEqual(pmeta["title"], "Nightcall")
        self.assertEqual(pmeta["artist"], "Kavinsky")

    def test_download_media_spotify_falls_back_to_soundcloud_search_and_strips_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample_mp3 = os.path.join(tmp, "seed.mp3")
            seed_wav = os.path.join(tmp, "seed.wav")
            _make_stereo_music_wav(seed_wav, duration_s=2.0)
            subprocess.run(
                [kt._FFMPEG_BIN, "-y", "-loglevel", "error", "-i", seed_wav,
                 "-metadata", "comment=Made with Suno AI", "-c:a", "libmp3lame", sample_mp3],
                check=True,
            )

            seen_targets = []

            def fake_run_yt(cmd, timeout, what="yt-dlp"):
                target = cmd[-1]
                seen_targets.append(target)
                if target.startswith("scsearch1:"):
                    out_idx = cmd.index("-o") + 1
                    dst = cmd[out_idx].replace("%(ext)s", "mp3")
                    Path(dst).write_bytes(Path(sample_mp3).read_bytes())
                    return 0, "", ""
                return 1, "", "HTTP Error 403: Forbidden"

            out_dir = os.path.join(tmp, "dl")
            os.makedirs(out_dir)
            with patch.object(kt, "_ytdlp_bin", return_value="yt-dlp"), \
                 patch.object(kt, "resolve_spotify_metadata", return_value={
                     "title": "Starboy",
                     "artist": "The Weeknd",
                     "duration_secs": 230.0,
                     "canonical_url": "https://open.spotify.com/track/7MXVkk9YMctZqd1Srtv4MB",
                     "item_type": "track",
                 }), \
                 patch.object(kt, "_run_yt", side_effect=fake_run_yt):
                info = kt.download_media(
                    "https://open.spotify.com/track/7MXVkk9YMctZqd1Srtv4MB?si=xyz",
                    out_dir,
                    kind="audio",
                )

            self.assertTrue(any(t.startswith("ytsearch3:") for t in seen_targets))
            self.assertTrue(any(t.startswith("scsearch1:") for t in seen_targets))
            self.assertEqual(info["title"], "The Weeknd - Starboy")
            self.assertEqual(info["duration"], "3:50")
            self.assertTrue(os.path.isfile(info["path"]))
            raw_bytes = Path(info["path"]).read_bytes()
            self.assertNotIn(b"Suno", raw_bytes)
            self.assertNotIn(b"Lavf", raw_bytes)

    def test_download_media_soundcloud_sets_and_shortlink_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_wav = os.path.join(tmp, "seed.wav")
            sample_mp3 = os.path.join(tmp, "seed.mp3")
            _make_stereo_music_wav(seed_wav, duration_s=50.0, sr=24000)
            subprocess.run(
                [kt._FFMPEG_BIN, "-y", "-loglevel", "error", "-i", seed_wav,
                 "-c:a", "libmp3lame", "-b:a", "128k", sample_mp3],
                check=True,
            )
            calls = []

            def fake_run_yt(cmd, timeout, what="yt-dlp"):
                calls.append(list(cmd))
                if "--skip-download" in cmd:
                    return 0, "Cloud Track\t50\tDJ Test\n", ""
                out_idx = cmd.index("-o") + 1
                dst = cmd[out_idx].replace("%(ext)s", "mp3")
                Path(dst).write_bytes(Path(sample_mp3).read_bytes())
                return 0, "", ""

            out_dir = os.path.join(tmp, "sc_dl")
            os.makedirs(out_dir)
            with patch.object(kt, "_ytdlp_bin", return_value="yt-dlp"), \
                 patch.object(
                     kt, "_resolve_redirect_url",
                     return_value="https://soundcloud.com/djtest/sets/summer-ep?si=123",
                 ), \
                 patch.object(kt, "_run_yt", side_effect=fake_run_yt):
                info = kt.download_media("https://on.soundcloud.com/XyZ987", out_dir, kind="audio")

            self.assertIn("--playlist-items", calls[0])
            self.assertNotIn("youtube:player_client=android,web_embedded", calls[0])
            self.assertEqual(info["title"], "Cloud Track")
            self.assertEqual(info["uploader"], "DJ Test")
            self.assertTrue(os.path.isfile(info["path"]))


class CleanMetadataAndV4AudioTests(unittest.TestCase):
    def test_clean_export_stem_strips_ai_tokens_and_preserves_normal_words(self):
        self.assertEqual(
            kt.clean_export_stem("Rain in the Studio (Main Chain).mp3"),
            "Rain in the Studio (Main Chain)",
        )
        cleaned = kt.clean_export_stem("Neon_Dreams_AI_Generated_Suno_v2.mp3")
        self.assertNotIn("ai", cleaned.lower())
        self.assertNotIn("suno", cleaned.lower())
        self.assertIn("Neon_Dreams", cleaned)

    def test_v4_produces_clean_stereo_audio_zero_ai_tags_and_transformed_spectrogram(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_wav = os.path.join(tmp, "orig.wav")
            tagged_mp3 = os.path.join(tmp, "My_AI_Generated_Song.mp3")
            out_mp3 = os.path.join(tmp, "out_v4.mp3")
            orig_spec = os.path.join(tmp, "orig_spec.png")
            v4_spec = os.path.join(tmp, "v4_spec.png")

            _make_stereo_music_wav(src_wav, duration_s=8.0, sr=48000)
            subprocess.run(
                [
                    kt._FFMPEG_BIN, "-y", "-loglevel", "error", "-i", src_wav,
                    "-metadata", "title=AI Generated Song",
                    "-metadata", "comment=Created with Suno AI v4",
                    "-metadata", "artist=Suno AI",
                    "-c:a", "libmp3lame", "-b:a", "256k", tagged_mp3,
                ],
                check=True,
            )

            subprocess.run(
                [kt._FFMPEG_BIN, "-y", "-loglevel", "error", "-i", tagged_mp3,
                 "-lavfi", "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                 orig_spec],
                check=True,
            )
            kt._brand_spectrogram(orig_spec)

            def fake_shazam(clip, progress=None):
                return 1 if "My_AI_Generated_Song" in clip else 0

            with patch.dict(sys.modules, {"shazamio": types.ModuleType("shazamio")}), \
                 patch.object(kt, "_shazam_match_count", side_effect=fake_shazam), \
                 patch.object(kt.acoustid_backend, "is_available", return_value=False), \
                 patch.object(kt.songrec_backend, "is_available", return_value=False):
                res = kt.copyr8_autofit_v4(tagged_mp3, out_mp3, v4_spec, macro_fn=None)

            self.assertFalse(res["tremolo_applied"])
            self.assertEqual(res["info"]["channels"], 2)
            self.assertIsNotNone(res["loudness"]["out_lufs"])
            self.assertAlmostEqual(
                res["loudness"]["out_lufs"], res["loudness"]["source_lufs"], delta=1.5
            )

            # Verify zero AI/encoder metadata bytes in exported MP3
            raw_out = Path(out_mp3).read_bytes()
            for forbidden in (b"Suno", b"Created with", b"AI Generated", b"Lavf", b"Lavc", b"Xing"):
                self.assertNotIn(forbidden, raw_out)

            # Verify spectrogram looks meaningfully transformed (ultrasonic helix + comb lanes)
            img_orig = np.asarray(Image.open(orig_spec).convert("L"), dtype=np.float32)
            img_v4 = np.asarray(Image.open(v4_spec).convert("L"), dtype=np.float32)
            mean_abs_diff = float(np.mean(np.abs(img_v4 - img_orig)))
            # Ultrasonic band (rows 15..45 from top of 280px spectrogram) has new double-helix energy
            ultra_orig = float(img_orig[15:45, :].std())
            ultra_v4 = float(img_v4[15:45, :].std())
            self.assertGreater(mean_abs_diff, 4.0)
            self.assertGreater(ultra_v4, ultra_orig + 2.0)


class PacingAndInterAudioTestingTests(unittest.TestCase):
    def test_pacing_line_displays_api_cooldown_in_parentheses(self):
        paced = audio_bot._copyr8_pacing_line(
            {"rate_limit_remaining": 0.0, "breath_remaining": 3.0, "breath_seconds": 6.0}
        )
        self.assertIn("API pacing (API Cooldown)", paced)
        rl = audio_bot._copyr8_pacing_line(
            {"rate_limit_remaining": 25.0, "consecutive_limits": 1}
        )
        self.assertIn("(API Cooldown)", rl)

    def test_shazam_probe_enforces_cooldown_after_completion_and_tests_in_between_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "long_track.wav")
            _make_stereo_music_wav(wav, duration_s=32.0, sr=24000)
            windows = kt._gapfree_windows(wav)
            # 32s track must include start, in-between middle window, and end window
            self.assertGreaterEqual(len(windows), 3)
            self.assertTrue(any(6.0 <= w <= 14.0 for w in windows))

            logs = []
            with patch.object(kt, "_shazam_match_count", return_value=0), \
                 patch.object(kt.acoustid_backend, "is_available", return_value=False), \
                 patch.object(kt.songrec_backend, "is_available", return_value=False):
                ok, counts = kt._probe_sweep_shifted(
                    wav, windows, -0.2, 1.05, progress=logs.append
                )
            self.assertTrue(ok)
            self.assertEqual(counts, [0] * len(windows))
            self.assertTrue(any("in-between audio window" in line for line in logs))


class MonitorAnalyzeAndWhitelistDiscordTests(unittest.TestCase):
    def test_monitor_responses_all_use_embeds_and_dm_set_uses_attachments(self):
        sent = []

        class Resp:
            async def send_message(self, content=None, **kw):
                sent.append(("send", content, kw))

            async def defer(self, **kw):
                sent.append(("defer", None, kw))

        class Follow:
            async def send(self, content=None, **kw):
                sent.append(("followup", content, kw))

        inter = types.SimpleNamespace(
            user=types.SimpleNamespace(id=1, send=AsyncMock()),
            channel=None,
            guild=None,
            response=Resp(),
            followup=Follow(),
        )

        with patch.object(audio_bot, "_require_trusted", AsyncMock(return_value=True)), \
             patch.object(audio_bot, "USAGE_LOG", os.devnull), \
             patch.object(kt, "ROBLOX_COOKIE", "valid_cookie"):
            # 1) /monitor list (empty)
            sent.clear()
            asyncio.run(audio_bot.monitor_slash.callback(inter, "list"))
            self.assertIsNone(sent[0][1])
            self.assertIsNotNone(sent[0][2].get("embed"))

            # 2) /monitor stop 99999
            sent.clear()
            asyncio.run(audio_bot.monitor_slash.callback(inter, "stop 99999"))
            self.assertIsNone(sent[0][1])
            self.assertEqual(sent[0][2]["embed"].title, "Moderation Monitor Updated")

        # 3) _MonitorSession._dm_set edits using `attachments=` (discord.py 2.7 signature)
        edited_kwargs = {}

        async def fake_edit(**kw):
            edited_kwargs.update(kw)

        session = audio_bot._MonitorSession(user=None, channel=None)
        session.dm_msgs["123"] = types.SimpleNamespace(edit=fake_edit)
        asyncio.run(session._dm_set("123", audio_bot.discord.Embed(title="T"), files=[]))
        self.assertIn("attachments", edited_kwargs)
        self.assertNotIn("files", edited_kwargs)

    def test_analyze_group_has_file_and_roblox_subcommands_and_shows_warning_on_download_failure(self):
        cmd_names = {c.name for c in audio_bot.analyze_group.commands}
        self.assertEqual(cmd_names, {"file", "roblox"})

        # Subcommand gating recognizes root_parent == "analyze" in FREE_COMMANDS
        sub_cmd = audio_bot.analyze_group.get_command("roblox")
        fake_inter = types.SimpleNamespace(command=sub_cmd)
        self.assertTrue(audio_bot.is_free_command(fake_inter))

        followups = []
        inter = types.SimpleNamespace(
            user=types.SimpleNamespace(id=1),
            command=sub_cmd,
            response=types.SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=types.SimpleNamespace(
                send=AsyncMock(side_effect=lambda *a, **kw: followups.append(kw))
            ),
        )

        # Case A: Roblox asset cannot be downloaded -> basic warning embed
        with patch.object(audio_bot, "_require_trusted", AsyncMock(return_value=True)), \
             patch.object(audio_bot, "USAGE_LOG", os.devnull), \
             patch.object(kt, "lookup_roblox_asset", return_value={
                 "id": 1848354536, "name": "Locked Song", "creator": "Builder",
                 "asset_type_id": 3, "asset_type": "Audio", "is_audio": True, "is_banned": False,
             }), \
             patch.object(kt, "download_asset_audio", side_effect=RuntimeError("403 private")):
            asyncio.run(audio_bot.analyze_roblox_slash.callback(inter, "1848354536"))
        self.assertEqual(len(followups), 1)
        warn_embed = followups[0]["embed"]
        self.assertIn("Cannot Download Audio", warn_embed.title)
        self.assertIn("could not be downloaded", warn_embed.description)

        # Case B: Downloadable gzipped Roblox audio -> embed + waveform.png + audio file
        followups.clear()
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "rbx.wav")
            ogg = os.path.join(tmp, "rbx.ogg")
            _make_stereo_music_wav(wav, duration_s=2.0)
            subprocess.run(
                [kt._FFMPEG_BIN, "-y", "-loglevel", "error", "-i", wav, "-c:a", "libvorbis", ogg],
                check=True,
            )
            ogg_bytes = Path(ogg).read_bytes()
            gz_bytes = gzip.compress(ogg_bytes)
            self.assertEqual(kt._unpack_delivery_bytes(gz_bytes), ogg_bytes)
            with patch.object(audio_bot, "_require_trusted", AsyncMock(return_value=True)), \
                 patch.object(audio_bot, "USAGE_LOG", os.devnull), \
                 patch.object(kt, "lookup_roblox_asset", return_value={
                     "id": 1848354536, "name": "Town Theme", "creator": "Roblox",
                     "asset_type_id": 3, "asset_type": "Audio", "is_audio": True, "is_banned": False,
                 }), \
                 patch.object(kt, "download_asset_audio", return_value=ogg_bytes):
                asyncio.run(audio_bot.analyze_roblox_slash.callback(inter, "rbxassetid://1848354536"))
        self.assertEqual(len(followups), 1)
        ok_embed = followups[0]["embed"]
        self.assertEqual(ok_embed.title, "Roblox Audio Analysis")
        self.assertEqual(len(followups[0]["files"]), 2)

    def test_whitelist_user_id_tags_user_and_delivers_verification_dm(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_file = os.path.join(tmp, "staff_whitelist.json")
            dm_sent = []

            class FakeDMChannel:
                async def send(self, **kw):
                    dm_sent.append(kw)

            target_user = types.SimpleNamespace(
                id=1495422390505570537,
                name="friend_account",
                display_name="BestFriend",
                mention="<@1495422390505570537>",
                create_dm=AsyncMock(return_value=FakeDMChannel()),
            )

            followups = []
            inter = types.SimpleNamespace(
                user=types.SimpleNamespace(id=99, display_name="Owner"),
                guild=None,
                client=types.SimpleNamespace(
                    guilds=[],
                    get_user=lambda uid: None,
                    fetch_user=AsyncMock(return_value=target_user),
                ),
                response=types.SimpleNamespace(defer=AsyncMock()),
                followup=types.SimpleNamespace(
                    send=AsyncMock(side_effect=lambda *a, **kw: followups.append(kw))
                ),
            )

            with patch.object(audio_bot, "STAFF_WHITELIST_FILE", wl_file), \
                 patch.object(audio_bot, "USAGE_LOG", os.devnull), \
                 patch.object(audio_bot, "_require_owner", AsyncMock(return_value=True)):
                # Add by user_id (even pasted with <@!...> wrapper)
                asyncio.run(
                    audio_bot.whitelist_slash.callback(
                        inter, "add", None, "<@!1495422390505570537>"
                    )
                )
                # Confirmation message tags user in `content` AND embed, and DM is sent via create_dm()
                self.assertEqual(followups[0]["content"], "<@1495422390505570537>")
                self.assertIn("<@1495422390505570537>", followups[0]["embed"].description)
                self.assertEqual(len(dm_sent), 1)
                self.assertEqual(dm_sent[0]["embed"].title, "You've been verified")
                # Cold DM must NOT contain discord.gg invite link that triggers Discord anti-spam
                dm_fields_text = " ".join(f.value for f in dm_sent[0]["embed"].fields)
                self.assertNotIn("discord.gg", dm_fields_text)

                # Re-running /whitelist add for an already-whitelisted friend STILL re-sends DM!
                asyncio.run(
                    audio_bot.whitelist_slash.callback(
                        inter, "add", None, "1495422390505570537"
                    )
                )
                self.assertEqual(len(dm_sent), 2)


if __name__ == "__main__":
    unittest.main()
