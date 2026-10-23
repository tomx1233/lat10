r"""Shared Shazam recognition backend for the KILLA'S BOSS audio bot.

`AudioRecognition` is the identification engine used by /lookup.  The /copyr8
auto-fit presets run their own ShazamIO probe loops in killa_tools
(find_undetectable / find_mildest) with the previous version's acceptance
rules -- this module stays the /lookup engine.

Key properties
--------------
- Full-track recognition: the whole file is fingerprinted -- no segment or
  duration cap.  (ShazamIO's segment option is deliberately not set.)
- Serialized request queue: at most one Shazam request in flight; further
  requests queue (bounded by `max_queue`) and report their position, so the
  Shazam API is never hammered.
- Rate limits are RESPECTED, never bypassed: on a rate-limit response the
  backend backs off exponentially and retries within its budget, then fails
  with a plain "rate limited" message.  No proxies, no identity rotation.
- Content-hash cache: identical uploads within `cache_duration_seconds`
  (default 30 minutes) are answered without touching the API.
- Typed errors so commands can present them: InvalidAudioError,
  NetworkError, RateLimitedError, RecognitionError.

Configuration: `lookup_config.json` next to this file (see DEFAULTS for the
keys and fallbacks).  ShazamIO manages its own API endpoints; `language` and
`endpoint_country` are the supported region settings.  Sensitive values
(Discord token etc.) stay in .env -- nothing secret belongs in the JSON.

This module is discord-free: it returns plain dicts, the command layer does
the formatting.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import threading
import time

import aiohttp
try:
    from shazamio import Shazam
except ImportError:
    Shazam = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "lookup_config.json")

DEFAULTS = {
    "language": "en-US",
    "endpoint_country": "US",
    "recognize_timeout_seconds": 60,
    "cache_duration_seconds": 1800,     # 30 minutes
    "cache_file": "lookup_cache.json",
    "max_cache_entries": 500,
    "max_file_size_mb": 25,
    "max_queue": 5,
    "max_retries": 3,
    "backoff_base_seconds": 4,
    "segment_duration_seconds": 0,      # 0 = no segment cap, full track
}


class RecognitionError(RuntimeError):
    """Base class: recognition failed; message is user-presentable."""


class InvalidAudioError(RecognitionError):
    """The file is missing, too large, or not decodable audio."""


class NetworkError(RecognitionError):
    """Shazam could not be reached or timed out."""


class RateLimitedError(RecognitionError):
    """Shazam is rate limiting us; the backoff budget was used up."""


class AudioRecognition:
    """ShazamIO-powered identification engine.

    One instance owns the config, the request queue (one Shazam call in
    flight), the content-hash cache, and the retry/backoff policy.  Create
    one per process and share it -- all public methods are safe to call from
    async code; `recognize_sync` wraps `recognize` for worker threads.
    """

    def __init__(self, config_path=None):
        self.config_path = config_path or CONFIG_PATH
        self._sem = None            # per-event-loop queue primitives
        self._lock = None
        self._loop_id = None
        self._waiting = 0
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------ config

    def load_config(self):
        cfg = dict(DEFAULTS)
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass  # missing/broken config: defaults are fine
        return cfg

    # ------------------------------------------------------------- cache

    def _cache_path(self, cfg):
        name = cfg.get("cache_file") or DEFAULTS["cache_file"]
        if not os.path.isabs(name):
            name = os.path.join(HERE, name)
        return name

    def _cache_load(self, cfg):
        try:
            with open(self._cache_path(cfg), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _cache_store(self, cfg, data):
        # newest first, capped, then written atomically
        items = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0),
                       reverse=True)
        items = items[: max(1, int(cfg.get("max_cache_entries", 500)))]
        tmp = self._cache_path(cfg) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dict(items), fh)
            os.replace(tmp, self._cache_path(cfg))
        except OSError:
            pass

    def _cache_get(self, key, cfg):
        if not key:
            return None
        with self._cache_lock:
            data = self._cache_load(cfg)
            entry = data.get(key)
            if not isinstance(entry, dict):
                return None
            age = time.time() - float(entry.get("ts", 0))
            if age > float(cfg.get("cache_duration_seconds", 0)):
                return None
            result = entry.get("result")
            return dict(result) if isinstance(result, dict) else None

    def _cache_put(self, key, result, cfg):
        if not key:
            return
        with self._cache_lock:
            data = self._cache_load(cfg)
            data[key] = {"ts": time.time(), "result": dict(result)}
            self._cache_store(cfg, data)

    # ------------------------------------------------------ file handling

    @staticmethod
    def file_sha256(path):
        """Content hash for the cache (chunked, big files never load whole)."""
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def validate_audio_file(self, path, cfg=None):
        """Raise InvalidAudioError unless the file exists, fits the size
        cap, and decodes as audio (ffprobe).  No duration limit of any kind."""
        cfg = cfg or self.load_config()
        if not path or not os.path.isfile(path):
            raise InvalidAudioError("That file could not be read from disk.")
        max_mb = float(cfg.get("max_file_size_mb", 25))
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > max_mb:
            raise InvalidAudioError(
                f"File is {size_mb:.0f} MB; the limit is {max_mb:.0f} MB."
            )
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidAudioError(
                f"Could not inspect that file: {exc}") from exc
        if probe.returncode != 0 or '"format_name"' not in (probe.stdout or ""):
            raise InvalidAudioError(
                "That file is not decodable audio (mp3/wav/flac/ogg/m4a...)."
            )

    # -------------------------------------------------------------- queue

    def _ensure_queue(self):
        """Per-event-loop serialization primitives (one call at a time)."""
        loop = asyncio.get_running_loop()
        if self._loop_id != id(loop):
            # A different loop (bot restart, sync wrapper): fresh primitives.
            self._sem = asyncio.Semaphore(1)
            self._lock = asyncio.Lock()
            self._loop_id = id(loop)
            self._waiting = 0
        return self._sem, self._lock

    # -------------------------------------------------------- recognition

    @staticmethod
    def _is_rate_limit(exc):
        text = str(exc).lower()
        return any(m in text for m in ("429", "too many requests", "rate limit",
                                       "throttl"))

    @staticmethod
    def _is_network(exc):
        return isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError,
                                ConnectionError, OSError))

    @staticmethod
    def _parse_track(data):
        """Flatten a Shazam response into our result dict (no track -> no match)."""
        track = data.get("track") if isinstance(data, dict) else None
        if not isinstance(track, dict):
            return {"matched": False}

        album = release = None
        for section in track.get("sections") or []:
            if not isinstance(section, dict) or section.get("type") != "SONG":
                continue
            for meta in section.get("metadata") or []:
                if not isinstance(meta, dict):
                    continue
                title = str(meta.get("title", "")).lower()
                if title == "album" and not album:
                    album = meta.get("text")
                elif title in ("released", "release date") and not release:
                    release = meta.get("text")
            if album and release:
                break

        offset = None
        matches = data.get("matches") or []
        if matches and isinstance(matches[0], dict):
            try:
                offset = float(matches[0].get("offset"))
            except (TypeError, ValueError):
                offset = None

        images = track.get("images") or {}
        return {
            "matched": True,
            "song_name": track.get("title") or "(unknown title)",
            "artist": track.get("subtitle") or "",
            "album": album or "",
            "release_date": release or "",
            "cover_url": images.get("coverarthq") or images.get("coverart") or "",
            "url": track.get("url") or "",
            "offset_seconds": offset,
        }

    async def _recognize_once(self, path, cfg):
        shazam_cls = Shazam
        if shazam_cls is None:
            try:
                from shazamio import Shazam as shazam_cls  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    f"ShazamIO is not installed (pip install shazamio): {exc}"
                ) from exc
        shazam = shazam_cls(
            language=cfg.get("language", "en-US"),
            endpoint_country=cfg.get("endpoint_country", "US"),
        )
        try:
            data = await asyncio.wait_for(
                shazam.recognize(path),
                timeout=float(cfg.get("recognize_timeout_seconds", 60)),
            )
        finally:
            close = getattr(shazam, "close", None)
            if callable(close):
                res = close()
                if asyncio.iscoroutine(res):
                    await res
        return self._parse_track(data)

    async def recognize(self, path, progress=None):
        """Identify an audio file.  Validates, queues, checks the cache,
        then calls Shazam with retry/backoff.  Returns the result dict
        ({"matched": bool, ...} + "cached": True when served from cache)."""
        cfg = self.load_config()
        self.validate_audio_file(path, cfg)

        key = None
        try:
            key = self.file_sha256(path)
        except OSError:
            pass
        cached = self._cache_get(key, cfg)
        if cached is not None:
            if progress:
                progress("Served from the results cache.")
            return {**cached, "cached": True}

        sem, lock = self._ensure_queue()
        position = None
        async with lock:
            if sem.locked():
                if self._waiting >= int(cfg.get("max_queue", 5)):
                    raise RecognitionError(
                        "The recognition queue is full — try again in a moment."
                    )
                self._waiting += 1
                position = self._waiting
        if position:
            if progress:
                progress(f"Queued behind {position} request(s)...")
        async with sem:
            if position:
                self._waiting = max(0, self._waiting - 1)
            if progress:
                progress("Recognizing with Shazam (full track)...")
            retries = max(1, int(cfg.get("max_retries", 3)))
            base = float(cfg.get("backoff_base_seconds", 4))
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    result = await self._recognize_once(path, cfg)
                    self._cache_put(key, result, cfg)
                    return result
                except Exception as exc:  # noqa: BLE001 - classified below
                    last_exc = exc
                    retryable = attempt < retries and (
                        self._is_rate_limit(exc) or self._is_network(exc)
                    )
                    if not retryable:
                        break
                    wait = base * (2 ** (attempt - 1))
                    if self._is_rate_limit(exc):
                        if progress:
                            progress(
                                f"Shazam is rate limiting us — backing off "
                                f"{wait:.0f}s (attempt {attempt}/{retries})..."
                            )
                    else:
                        if progress:
                            progress(
                                f"Shazam did not answer — retrying in "
                                f"{wait:.0f}s (attempt {attempt}/{retries})..."
                            )
                    await asyncio.sleep(wait)
            if isinstance(last_exc, asyncio.TimeoutError):
                raise NetworkError("Shazam took too long to answer. Try again.")
            if self._is_rate_limit(last_exc):
                raise RateLimitedError(
                    "Shazam is rate limiting this bot right now. Try again later."
                )
            if self._is_network(last_exc):
                raise NetworkError(f"Could not reach Shazam: {last_exc}")
            raise RecognitionError(f"Recognition failed: {last_exc}")

    def recognize_sync(self, path, progress=None):
        """Blocking wrapper for non-async callers (worker threads, tests)."""
        return asyncio.run(self.recognize(path, progress))


# Shared instance for module-level convenience callers.
_default = AudioRecognition()


def recognize_file(path, progress=None):
    """Module-level delegate to AudioRecognition.recognize (async)."""
    return _default.recognize(path, progress=progress)


def recognize_file_sync(path, progress=None):
    """Module-level delegate to AudioRecognition.recognize_sync."""
    return _default.recognize_sync(path, progress=progress)
