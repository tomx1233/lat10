"""AcoustID lookup for CopyR8 auto-fit (no submissions, no audio uploads).

Fingerprint the candidate FILE from its beginning (Chromaprint's standard first
120 seconds), with the whole-file duration. Never feed it Shazam's arbitrary
12-second windows. All returned AcoustID IDs count, even without MusicBrainz
metadata. Missing/invalid replies are inconclusive, never zero matches.
"""
import json
import math
import os
import shutil
import subprocess
import threading
import time

API_BASE_URL = "https://api.acoustid.org/v2/"
REQUEST_TIMEOUT = 25
FINGERPRINT_TIMEOUT = 120
ATTEMPTS = 3
REQUEST_INTERVAL = 1.0  # Conservative: below AcoustID's 3 requests/second limit.
_lock = threading.Lock()
_last_request = 0.0


class AcoustIDSetupError(RuntimeError):
    """Operator action required; messages are safe for Discord/logs."""


def is_available():
    """Return True if AcoustID key and fpcalc are configured."""
    try:
        ac, exe = validate_setup(required=False)
        return bool(ac and exe)
    except AcoustIDSetupError:
        return False


def validate_setup(required=True):
    """Check local dependencies without contacting either recognition API."""
    key = os.getenv("ACOUSTID_API_KEY", "").strip()
    if not key:
        if required:
            raise AcoustIDSetupError(
                "Set ACOUSTID_API_KEY in the bot's local .env to an AcoustID "
                "application/client key (not a user/submission key).")
        return None, None
    try:
        import acoustid
    except ImportError:
        if required:
            raise AcoustIDSetupError(
                "AcoustID needs pyacoustid. Run python -m pip install -r requirements.txt.") from None
        return None, None
    try:
        fpcalc = resolve_fpcalc()
    except AcoustIDSetupError:
        if required:
            raise
        return None, None
    return acoustid, fpcalc


HERE = os.path.dirname(os.path.abspath(__file__))
_FPCALC_NAME = "fpcalc.exe" if os.name == "nt" else "fpcalc"
BUNDLED_FPCALC = os.path.join(HERE, "tools", "chromaprint", _FPCALC_NAME)


def resolve_fpcalc():
    """Locate fpcalc: FPCALC from .env, else tools/chromaprint, else PATH.

    FPCALC may be absolute, relative to the bot folder, or a folder containing
    fpcalc(.exe). Messages never include the configured path.
    """
    raw = os.getenv("FPCALC", "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    if raw:
        if any(ord(ch) < 32 for ch in raw):
            # python-dotenv expands \t, \n, \r... inside DOUBLE-quoted values,
            # so FPCALC="C:\tools\new\fpcalc.exe" silently becomes garbage.
            raise AcoustIDSetupError(
                "FPCALC in .env contains a tab/newline. Windows paths in double "
                "quotes are escape-processed by python-dotenv: remove the quotes, "
                "use single quotes, or use forward slashes.")
        path = os.path.expanduser(os.path.expandvars(raw))
        if not os.path.isabs(path) and (os.sep in path or "/" in path):
            path = os.path.join(HERE, path)
        if os.path.isdir(path):
            path = os.path.join(path, _FPCALC_NAME)
        found = shutil.which(path)
        if not found:
            raise AcoustIDSetupError(
                "FPCALC is set in .env, but no runnable fpcalc executable was found "
                "there. Set it to the full path of fpcalc.exe (for example "
                "tools/chromaprint/fpcalc.exe inside the bot folder).")
        return found
    found = shutil.which(BUNDLED_FPCALC) or shutil.which("fpcalc")
    if not found:
        raise AcoustIDSetupError(
            "AcoustID needs Chromaprint's fpcalc. Put fpcalc.exe in "
            "tools/chromaprint/, install it on PATH, or set FPCALC in .env to "
            "its full path.")
    return found


def fingerprint_file(path, executable):
    """Bound the subprocess; do not leak raw decoder output or local paths."""
    result = subprocess.run(
        [executable, "-json", "-length", "120", os.path.abspath(path)],
        capture_output=True, text=True, timeout=FINGERPRINT_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    if result.returncode != 0:
        raise ValueError("fingerprint generation failed")
    data = json.loads(result.stdout)
    duration = data.get("duration")
    fingerprint = data.get("fingerprint")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration < 1
            or not isinstance(fingerprint, str) or not fingerprint.strip()):
        raise ValueError("incomplete fingerprint")
    return duration, fingerprint


def parse_response(data):
    """Return a count or None; do not mistake API errors for no matches."""
    if not isinstance(data, dict):
        return None
    if data.get("status") != "ok":
        error = data.get("error")
        if isinstance(error, dict) and error.get("code") == 4:
            raise AcoustIDSetupError(
                "AcoustID rejected the application key. Set ACOUSTID_API_KEY "
                "to an application/client key, not a user/submission key.")
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            return None
        score = row.get("score")
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            return None
    return len(results)


def match_count(path, progress=None):
    """Return count/None with bounded retries; configuration failures raise.

    Requests are serialized and paced independently from Shazam. The API key
    travels only in the HTTPS POST body; raw exceptions/responses are never
    echoed, as they can contain request credentials.
    """
    global _last_request
    acoustid, executable = validate_setup(required=False)
    if not acoustid or not executable:
        return None
    try:
        duration, fingerprint = fingerprint_file(path, executable)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        if progress:
            progress("AcoustID: fingerprinting failed; this check is inconclusive.")
        return None

    key = os.environ["ACOUSTID_API_KEY"].strip()
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            with _lock:
                wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
                if wait > 0:
                    time.sleep(wait)
                _last_request = time.monotonic()
                # pyacoustid defaults to HTTP; explicitly require HTTPS.
                acoustid.set_base_url(API_BASE_URL)
                data = acoustid.lookup(key, fingerprint, duration, meta="", timeout=REQUEST_TIMEOUT)
            count = parse_response(data)
        except AcoustIDSetupError:
            raise
        except Exception:  # pyacoustid/network/JSON failures: never print credentials.
            count = None
        if count is not None:
            if progress:
                progress(f"AcoustID: {count} fingerprint match(es).")
            return count
        if progress:
            progress(f"AcoustID: no usable answer (attempt {attempt + 1}/{ATTEMPTS}).")
    return None
