r"""CopyR8 Auto-Fit V3/V4 -- run an Audacity macro over an audio file.

Bridges the bot to Audacity 2.3.3's scripting pipe (mod-script-pipe).  The
configured macro runs through Audacity's own macro runner (the same path as
Tools > Apply Macro), so the whole effect chain -- including VSTs such as
TDR Nova and Blue Cat's Triple EQ -- is applied exactly as authored, with
none of the pipe's per-step command limits.

Audacity is started fully hidden and reused across runs; it quits when the
bot process exits.  Only one macro run happens at a time (a lock here, on
top of the CopyR8 already-running check).

Configuration (optional environment variables):
  AUDACITY_EXE      path to audacity.exe.  Default: the standard 2.3.3
                    install location, then the App Paths registry key.

The macro to run is chosen by the caller (see resolve_macro); it must exist
in Audacity's Macros folder (%APPDATA%\Audacity\Macros). The bundled
CopyR8 AutoFit V3 and V4 macros are installed there automatically before
Audacity starts; only those bot-managed files are synchronized, never
custom macros.

Metadata (ID3 tags etc.) is copied from the original file onto the
processed output with an ffmpeg stream-copy remux, so tags survive the
round trip without a re-encode.
"""

import atexit
import os
import re
import shutil
import subprocess
import threading
import tempfile
import time
from pathlib import Path

from audacity_pipe import AudacitySession, PipeError, parse_macro

BUNDLED_V3_MACRO = Path(__file__).resolve().parent / "macros" / "CopyR8 AutoFit V3.txt"
BUNDLED_V4_MACRO = Path(__file__).resolve().parent / "macros" / "CopyR8 AutoFit V4.txt"
BUNDLED_MACROS = (BUNDLED_V3_MACRO, BUNDLED_V4_MACRO)

MACRO_DIR = os.path.join(os.environ.get("APPDATA", ""), "Audacity", "Macros")

# Audacity pipe errors embed the macro's menu id (e.g. "Macro_<macroname>");
# redact it before any message reaches Discord.
_MACRO_ID_RE = re.compile(r"\bMacro_\S+")


def _redact(text):
    return _MACRO_ID_RE.sub("the effect chain", str(text))


class MacroProcessingError(RuntimeError):
    """Audacity macro processing failure with a user-presentable message."""


def find_audacity_exe():
    """Resolve audacity.exe: AUDACITY_EXE env, standard path, then registry."""
    env = os.environ.get("AUDACITY_EXE", "")
    if env and os.path.isfile(env):
        return env
    default = r"C:\Program Files (x86)\Audacity\audacity.exe"
    if os.path.isfile(default):
        return default
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\audacity.exe",
                ) as key:
                    value = winreg.QueryValueEx(key, None)[0]
                    if value and os.path.isfile(value):
                        return value
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - registry is a best-effort fallback
        pass
    return default


def resolve_macro(name):
    """Return the .txt path of an installed macro, or raise with guidance."""
    if os.path.isfile(name):
        return name
    for candidate in (name, name + ".txt"):
        path = os.path.join(MACRO_DIR, candidate)
        if os.path.isfile(path):
            return path
    raise MacroProcessingError(
        "The effect chain is not installed in Audacity's Macros "
        "folder. Open Audacity, check Tools > Macro Manager, then try again."
    )


def _install_bundled_macro(macro_path):
    """Install/sync only our bundled chain. Caller must hold _session_lock.

    Return whether Audacity must reload its macro menu. Use atomic replacement
    so an interrupted write cannot leave a partially installed effect chain.
    """
    source = Path(macro_path).resolve()
    if source not in BUNDLED_MACROS:
        return False
    if not os.path.isabs(MACRO_DIR):
        raise MacroProcessingError(
            "Cannot locate Audacity's Macros folder. Set APPDATA for the Windows "
            "account running the bot."
        )
    temporary = None
    try:
        destination = Path(MACRO_DIR) / source.name
        content = source.read_bytes()
        if destination.is_file() and destination.read_bytes() == content:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".copyr8-", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
        os.replace(temporary, destination)
        return True
    except OSError as exc:
        raise MacroProcessingError(
            "Cannot install the bundled effect chain in Audacity's Macros "
            "folder. Check the bot account's folder permissions."
        ) from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


_session = None
_session_exe = None
_session_lock = threading.Lock()


def _console_log(message):
    """Console-only logger for pipe traffic.

    Raw Audacity logs contain internal identifiers (macro menu ids, command
    names) that must never reach Discord progress messages, so the session
    logs go to stdout and the wrapper emits its own curated progress lines.
    """
    print(f"[audacity] {message}", flush=True)


def _drop_session():
    """Terminate the cached hidden Audacity (used on errors and at exit)."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass
        _session = None


def _get_session():
    global _session, _session_exe
    exe = find_audacity_exe()
    if _session is None or _session_exe != exe:
        _drop_session()
        _session = AudacitySession(
            exe, log=_console_log, connect_timeout=60, reply_timeout=300
        )
        _session.ensure_connected()
        _session_exe = exe
    return _session


@atexit.register
def _shutdown():
    with _session_lock:
        _drop_session()


def _probe_ok(path):
    """True when ffprobe can read the file (returncode 0 + real output)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt" else 0,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def preserve_metadata(original_path, processed_path, progress=None):
    """Copy container tags (ID3 etc.) from the original onto the output.

    Audacity's export does not carry the source file's metadata, so remux
    the processed audio (stream copy, no re-encode) with the original's
    metadata attached.  Returns True when the remux replaced the file;
    falls back silently (returns False) if ffmpeg is missing or fails.

    A remux can also produce a file ffprobe can no longer parse (seen with
    unusual source tags, 2026-10-05) even though ffmpeg wrote it fine.  The
    untagged export is kept until the tagged result has been verified, and
    rolled back in that case -- a broken intermediate file must not kill the
    run that follows.
    """
    if not os.path.isfile(original_path) or not os.path.isfile(processed_path):
        return False
    ext = os.path.splitext(processed_path)[1]
    final = processed_path + ".meta" + ext
    backup = processed_path + ".premeta" + ext
    try:
        shutil.copyfile(processed_path, backup)
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", processed_path, "-i", original_path,
                "-map", "0:a", "-map_metadata", "1", "-map", "0:v?",
                "-fflags", "+bitexact", "-flags:a", "+bitexact",
                "-metadata", "comment=", "-metadata", "description=",
                "-metadata", "software=", "-metadata", "encoder=",
                "-metadata", "encoded_by=", "-metadata", "lyrics=",
                "-metadata", "synopsis=", "-metadata", "purl=",
                "-metadata:s:a:0", "encoder=",
                "-c", "copy", final,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.remove(backup)
        except OSError:
            pass
        return False
    if (
        result.returncode != 0
        or not os.path.isfile(final)
        or os.path.getsize(final) < 1024
    ):
        try:
            os.remove(backup)
        except OSError:
            pass
        return False
    os.replace(final, processed_path)
    if not _probe_ok(processed_path):
        # The tagged file is unreadable; ship the clean untagged export.
        os.replace(backup, processed_path)
        return False
    try:
        os.remove(backup)
    except OSError:
        pass
    if progress:
        progress("Metadata copied from the original file.")
    return True


def run_macro_blocking(input_path, output_path, macro_name, progress=None):
    """Run one Audacity macro over one file.  Blocking; call in a thread.

    Returns a dict: {"macro", "steps", "output", "metadata"} for the reply
    embed.  Raises MacroProcessingError with a Discord-ready message on
    failure.  A single retry with a fresh hidden Audacity covers a wedged
    or stale instance.
    """
    macro_path = resolve_macro(macro_name)
    name = os.path.splitext(os.path.basename(macro_path))[0]
    try:
        steps = parse_macro(macro_path)
    except OSError as exc:
        raise MacroProcessingError(f"Cannot read the effect chain file: {exc}") from exc
    if not steps:
        raise MacroProcessingError("The effect chain has no steps; fix it in Audacity's Macro Manager.")

    with _session_lock:
        if _install_bundled_macro(macro_path):
            # Audacity reads the chain/menu at launch. Never reuse an instance
            # with an older copy after the bundled file is installed or updated.
            _drop_session()
        for attempt in (1, 2):
            try:
                session = _get_session()
                missing, _known = session.check_effects(steps)
                if missing and session.registry_has_effects(missing):
                    # The effect list lags the registry in two ways: a fresh
                    # instance lists VSTs only after its startup plugin scan
                    # finishes, and an instance launched before a plugin was
                    # registered never lists it at all (seen 2026-10-05 with
                    # TDR Nova). Replace a pre-existing instance first -- no
                    # scan will change what it lists -- then poll the live
                    # list while the registry still claims the effect.
                    if session.process is None:
                        if progress:
                            progress("restarting hidden Audacity to pick up "
                                     "the effect chain...")
                        _drop_session()
                        subprocess.run(["taskkill", "/IM", "audacity.exe"],
                                       capture_output=True, timeout=15)
                        time.sleep(2)
                        session = _get_session()
                    if missing:
                        if progress:
                            progress("waiting for Audacity to finish loading "
                                     "its effects...")
                        deadline = time.time() + 90
                        while (missing
                               and session.registry_has_effects(missing)
                               and time.time() < deadline):
                            time.sleep(4)
                            session.reload_registry()
                            missing, _known = session.check_effects(steps)
                if missing:
                    raise MacroProcessingError(
                        "These effects are not installed in Audacity: "
                        + ", ".join(missing)
                    )
                # The Apply Macro menu is built when Audacity starts, so a
                # macro created after launch needs a restart to show up --
                # without it the runner would fall back to per-step piping.
                if not session.macro_menu_available(name):
                    if progress:
                        progress("Restarting hidden Audacity to load the effect chain...")
                    _drop_session()
                    session = _get_session()
                if progress:
                    progress(
                        f"Applying the effect chain ({len(steps)} step(s)) in hidden Audacity..."
                    )
                session.apply_macro_to_file(
                    input_path, output_path, steps, macro_name=name
                )
                if not os.path.isfile(output_path):
                    raise MacroProcessingError(
                        "Audacity did not produce an output file."
                    )
                break
            except PipeError as exc:
                _drop_session()  # dead or wedged: start fresh on the retry
                if attempt == 2:
                    raise MacroProcessingError(
                        f"Audacity macro run failed: {_redact(exc)}"
                    ) from exc
                if progress:
                    progress("Audacity did not answer; restarting it and retrying once...")

    metadata_ok = preserve_metadata(input_path, output_path, progress)
    return {
        "macro": name,
        "steps": [step for step, _params in steps],
        "output": output_path,
        "metadata": metadata_ok,
    }
