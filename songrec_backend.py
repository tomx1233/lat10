"""SongRec recognition backend for KILLA'S BOSS audio bot.

Interface to marin-m/SongRec (https://github.com/marin-m/SongRec).
Provides recognition and match counting for /lookup and /copyr8 Auto-Fit presets.
"""

import json
import os
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
_SONGREC_NAME = "songrec.exe" if os.name == "nt" else "songrec"
BUNDLED_SONGREC = os.path.join(HERE, "tools", "songrec", _SONGREC_NAME)

_lock = threading.Lock()


class SongRecSetupError(RuntimeError):
    """Operator action required; messages are safe for Discord/logs."""


def resolve_songrec():
    """Locate songrec executable: SONGREC_PATH from .env, else tools/songrec, else PATH.

    SONGREC_PATH may be absolute, relative to the bot folder, or a folder containing
    songrec(.exe). Messages never include configured secret paths.
    """
    raw = os.getenv("SONGREC_PATH", "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    if raw:
        if any(ord(ch) < 32 for ch in raw):
            raise SongRecSetupError(
                "SONGREC_PATH in .env contains a tab/newline. Windows paths in double "
                "quotes are escape-processed by python-dotenv: remove the quotes, "
                "use single quotes, or use forward slashes.")
        path = os.path.expanduser(os.path.expandvars(raw))
        if not os.path.isabs(path) and (os.sep in path or "/" in path):
            path = os.path.join(HERE, path)
        if os.path.isdir(path):
            path = os.path.join(path, _SONGREC_NAME)
        found = shutil.which(path) or (path if os.path.isfile(path) else None)
        if not found:
            raise SongRecSetupError(
                "SONGREC_PATH is set in .env, but no runnable songrec executable was found "
                "there. Set it to the full path of songrec.exe (for example "
                "tools/songrec/songrec.exe inside the bot folder).")
        return found

    tools_dir = os.path.join(HERE, "tools", "songrec")
    candidates = [
        BUNDLED_SONGREC,
        os.path.join(tools_dir, "songrec.exe"),
        os.path.join(tools_dir, "songrec.py"),
        os.path.join(tools_dir, "songrec"),
        os.path.join(tools_dir, "songrec.bat"),
    ]
    for c in candidates:
        found = shutil.which(c) or (c if os.path.isfile(c) else None)
        if found:
            return found

    return shutil.which("songrec")


def is_available():
    """Return True if a valid songrec executable is installed/configured."""
    try:
        return resolve_songrec() is not None
    except SongRecSetupError:
        return False


def validate_setup(required=False):
    """Check local dependencies for SongRec.

    If required=False and SONGREC_PATH is unset and songrec is not found on PATH
    or in tools/songrec/, returns None instead of raising an error.
    """
    executable = resolve_songrec()
    if not executable and required:
        raise SongRecSetupError(
            "SongRec needs the songrec executable (from https://github.com/marin-m/SongRec). "
            "Put songrec in tools/songrec/, install it on PATH, or set SONGREC_PATH in .env.")
    return executable


def run_songrec(path, executable=None, timeout=60):
    """Run songrec CLI on an audio file and return (returncode, stdout, stderr)."""
    if not executable:
        executable = validate_setup(required=True)

    abspath = os.path.abspath(path)
    if not os.path.isfile(abspath):
        raise ValueError("File does not exist")

    is_py_script = executable.endswith(".py")
    if not is_py_script and os.path.isfile(executable):
        try:
            with open(executable, "rb") as fh:
                head = fh.read(64)
            if head.startswith(b"#!") and b"python" in head.lower():
                is_py_script = True
        except OSError:
            pass
    prefix = [sys.executable, executable] if is_py_script else [executable]

    subcommands = [
        prefix + ["audio-file-to-recognized-song", abspath],
        prefix + ["recognize", "--json", abspath],
        prefix + ["recognize", abspath],
    ]

    for cmd in subcommands:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            stdout = res.stdout.strip() if res.stdout else ""
            if res.returncode == 0 and stdout:
                return res.returncode, stdout, res.stderr or ""
        except (OSError, subprocess.TimeoutExpired):
            continue

    return 1, "", "songrec execution failed"


def parse_response(stdout_or_data):
    """Parse JSON or text output from songrec into (match_count, track_dict).

    Handles both raw Shazam JSON (from audio-file-to-recognized-song) and
    flat JSON/text (from recognize).
    """
    if stdout_or_data is None:
        return None, {"matched": False}

    data = None
    if isinstance(stdout_or_data, dict):
        data = stdout_or_data
    elif isinstance(stdout_or_data, str):
        text = stdout_or_data.strip()
        if not text:
            return None, {"matched": False}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if " - " in text:
                parts = text.split(" - ", 1)
                return 1, {
                    "matched": True,
                    "artist": parts[0].strip(),
                    "song_name": parts[1].strip(),
                    "album": "",
                    "release_date": "",
                    "cover_url": "",
                    "url": "",
                    "provider": "SongRec",
                }
            return None, {"matched": False}

    if not isinstance(data, dict) or data.get("error"):
        return None, {"matched": False}

    track = data.get("track")
    matches = data.get("matches")

    if isinstance(track, dict) or isinstance(matches, list):
        if isinstance(matches, list) and not matches and not track:
            return 0, {"matched": False}

        if isinstance(track, dict):
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
            if matches and isinstance(matches[0], dict):
                try:
                    offset = float(matches[0].get("offset"))
                except (TypeError, ValueError):
                    offset = None

            images = track.get("images") or {}
            match_count_val = len(matches) if isinstance(matches, list) and matches else 1
            return match_count_val, {
                "matched": True,
                "song_name": track.get("title") or "(unknown title)",
                "artist": track.get("subtitle") or "",
                "album": album or "",
                "release_date": release or "",
                "cover_url": images.get("coverarthq") or images.get("coverart") or "",
                "url": track.get("url") or "",
                "offset_seconds": offset,
                "provider": "SongRec",
            }
        elif isinstance(matches, list) and len(matches) > 0:
            return len(matches), {
                "matched": True,
                "song_name": "(unknown title)",
                "artist": "",
                "album": "",
                "release_date": "",
                "cover_url": "",
                "url": "",
                "provider": "SongRec",
            }
        else:
            return 0, {"matched": False}

    song_name = data.get("song_name") or data.get("title") or data.get("track")
    artist = data.get("artist") or data.get("subtitle") or ""
    if song_name or artist:
        return 1, {
            "matched": True,
            "song_name": str(song_name or "(unknown title)"),
            "artist": str(artist),
            "album": str(data.get("album") or ""),
            "release_date": str(data.get("release_date") or data.get("release_year") or data.get("year") or ""),
            "cover_url": str(data.get("cover_url") or data.get("coverart") or ""),
            "url": str(data.get("url") or ""),
            "provider": "SongRec",
        }

    return 0, {"matched": False}


def match_count(path, progress=None):
    """Return match count (int) or None on failure/inconclusive."""
    executable = validate_setup(required=False)
    if not executable:
        return None

    with _lock:
        try:
            rc, stdout, stderr = run_songrec(path, executable=executable)
            count, info = parse_response(stdout if rc == 0 or stdout else None)
            if count is not None:
                if progress:
                    if count > 0:
                        progress(
                            f"SongRec: {count} match(es) "
                            f"({info.get('song_name', 'track')} by {info.get('artist', 'artist')})."
                        )
                    else:
                        progress("SongRec: 0 matches.")
                return count
            else:
                if progress:
                    progress("SongRec: check failed/inconclusive.")
                return None
        except Exception:
            if progress:
                progress("SongRec: execution error.")
            return None


def recognize(path, progress=None):
    """Synchronous song recognition via SongRec."""
    executable = validate_setup(required=False)
    if not executable:
        return {"matched": False}
    with _lock:
        rc, stdout, stderr = run_songrec(path, executable=executable)
        _count, info = parse_response(stdout if rc == 0 or stdout else None)
        return info


async def recognize_async(path, progress=None):
    """Async song recognition helper."""
    import asyncio
    return await asyncio.to_thread(recognize, path, progress)
