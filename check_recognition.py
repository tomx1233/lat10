"""Host-side diagnostics. Never prints keys, raw API errors, or full local paths.

  python check_recognition.py --setup
  python check_recognition.py --key-only
  python check_recognition.py path/to/original-song.mp3

--setup is offline: it checks the interpreter/.venv, packages, .env settings
(set / not set only), fpcalc, ffmpeg and the Audacity V3 prerequisites. Its
output is safe to paste into a support chat.

--key-only checks the AcoustID application key using the public track ID from
AcoustID's web-service documentation (no audio, no fingerprint).

The audio mode generates a real Chromaprint fingerprint of your file, then asks
both APIs; use an original song you have permission to test. A no-match on
arbitrary audio validates a response, not database coverage. No mode submits
fingerprints to AcoustID.
"""
import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv  # noqa: E402
except ImportError:
    def load_dotenv(*args, **kwargs):
        pass

import acoustid_backend  # noqa: E402
import songrec_backend  # noqa: E402

_PACKAGES = [
    # (import name, pip name, required-for)
    ("discord", "discord.py", "the bot"),
    ("dotenv", "python-dotenv", "the bot"),
    ("numpy", "numpy", "the bot"),
    ("PIL", "pillow", "the bot"),
    ("requests", "requests", "/lookup, AcoustID key check"),
    ("librosa", "librosa", "/copyr8 auto-fit"),
    ("soundfile", "soundfile", "/copyr8 auto-fit"),
    ("shazamio", "shazamio", "/lookup, /copyr8 auto-fit"),
    ("acoustid", "pyacoustid", "/copyr8 auto-fit"),
]


def _line(ok, label, detail=""):
    tag = {True: "OK  ", False: "FAIL", None: "WARN"}[ok]
    print(f"[{tag}] {label}" + (f" -- {detail}" if detail else ""))
    return ok is not False


def _short_version(executable, flag):
    try:
        out = subprocess.run(
            [executable, flag], capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        text = (out.stdout or out.stderr or "").strip().splitlines()
        return text[0][:120] if text else f"exit code {out.returncode}"
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_setup():
    """Offline environment report. Returns 0 when AcoustID prerequisites are met."""
    ok = True
    print(f"KILLA'S BOSS setup check (bot folder: {HERE.name})")
    print(f"Python {sys.version.split()[0]} ({'64' if sys.maxsize > 2**32 else '32'}-bit)")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    local_venv = (HERE / ".venv").resolve()
    uses_local = in_venv and Path(sys.prefix).resolve() == local_venv
    if uses_local:
        _line(True, "Interpreter", "running from this bot folder's .venv")
    elif in_venv:
        _line(None, "Interpreter", "a virtual environment, but not this folder's .venv")
    else:
        _line(None, "Interpreter", "system Python, not .venv (run_audio_bot.bat uses .venv)")
    if sys.version_info[:2] != (3, 10):
        _line(None, "Python version", "the bot has been run on 3.10; other versions are untested")

    print("\nPackages (requirements.txt):")
    for mod, pip_name, used_by in _PACKAGES:
        try:
            module = importlib.import_module(mod)
            version = getattr(module, "__version__", None) or getattr(module, "version", "")
            _line(True, pip_name, str(version) if isinstance(version, str) else "")
        except Exception:  # noqa: BLE001 -- ImportError or a broken install
            ok = _line(False, pip_name, f"not importable; needed for {used_by}") and ok

    print("\n.env (values are never printed):")
    env_file = HERE / ".env"
    if env_file.is_file():
        _line(True, ".env file", "present in the bot folder")
    else:
        ok = _line(False, ".env file", "missing -- copy your private .env into the bot folder") and ok
    for name, required in (("DISCORD_TOKEN", True), ("ACOUSTID_API_KEY", True),
                           ("FPCALC", False), ("SONGREC_PATH", False),
                           ("COPYR8_V3_MACRO", False), ("AUDACITY_EXE", False)):
        value = os.getenv(name, "")
        if value.strip():
            detail = "set"
            if value != value.strip() or value.strip()[:1] in "\"'":
                detail += " (has stray spaces/quotes; check the .env line)"
            if name == "COPYR8_V3_MACRO":
                detail += " -- overrides the bundled V3 macro"
            _line(True, name, detail)
        elif required:
            ok = _line(False, name, "not set") and ok
        else:
            default = {"FPCALC": "not set (uses tools/chromaprint or PATH)",
                       "SONGREC_PATH": "not set (uses tools/songrec or PATH)",
                       "COPYR8_V3_MACRO": "not set (uses the bundled V3 macro -- normal)",
                       "AUDACITY_EXE": "not set (uses the standard install path)"}[name]
            _line(True, name, default)

    print("\nChromaprint / SongRec / ffmpeg:")
    try:
        fpcalc = acoustid_backend.resolve_fpcalc()
        where = ("tools/chromaprint" if Path(fpcalc).resolve() == Path(acoustid_backend.BUNDLED_FPCALC).resolve()
                 else "FPCALC" if os.getenv("FPCALC", "").strip() else "PATH")
        version = _short_version(fpcalc, "-version")
        if version and "fpcalc" in version.lower():
            _line(True, "fpcalc", f"{version} (found via {where})")
        else:
            ok = _line(False, "fpcalc", f"found via {where} but did not run; re-download the official build") and ok
    except acoustid_backend.AcoustIDSetupError as exc:
        ok = _line(False, "fpcalc", str(exc)) and ok
    try:
        songrec_bin = songrec_backend.resolve_songrec()
        if songrec_bin:
            where_sr = ("tools/songrec" if Path(songrec_bin).resolve() == Path(songrec_backend.BUNDLED_SONGREC).resolve()
                        else "SONGREC_PATH" if os.getenv("SONGREC_PATH", "").strip() else "PATH")
            sr_ver = _short_version(songrec_bin, "--version") or "executable present"
            _line(True, "songrec", f"{sr_ver} (found via {where_sr})")
        else:
            _line(None, "songrec", "not found on PATH or tools/songrec/ (optional /lookup & /copyr8 engine)")
    except songrec_backend.SongRecSetupError as exc:
        _line(None, "songrec", str(exc))
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        if found:
            _line(True, tool, "on PATH")
        else:
            _line(None, tool, "not on PATH (needed by /analyze, /copyr8, /shazam)")

    print("\nAudacity (Auto-Fit V3 only):")
    macro = HERE / "macros" / "CopyR8 AutoFit V3.txt"
    _line(macro.is_file(), "Bundled V3 macro", "present" if macro.is_file() else "missing from macros/")
    if os.name != "nt":
        _line(None, "Audacity checks", "skipped (not Windows)")
    else:
        try:
            import audacity_processor
            exe = audacity_processor.find_audacity_exe()
            _line(True if os.path.isfile(exe) else None, "audacity.exe",
                  "found" if os.path.isfile(exe) else "not found; set AUDACITY_EXE")
        except Exception:  # noqa: BLE001
            _line(None, "audacity.exe", "could not be checked")
        appdata = Path(os.environ.get("APPDATA", "")) / "Audacity"
        cfg_text = ""
        try:
            cfg_text = (appdata / "audacity.cfg").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
        if "mod-script-pipe=1" in cfg_text.replace(" ", ""):
            _line(True, "mod-script-pipe", "enabled in audacity.cfg")
        else:
            _line(None, "mod-script-pipe", "not confirmed; enable it in Preferences > Modules")
        try:
            registry = (appdata / "pluginregistry.cfg").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            registry = ""
        for label, needles in (("TDR Nova", ("tdr nova", "tdrnova")),
                               ("Blue Cat's Triple EQ", ("triple eq", "tripleeq"))):
            found = any(n in registry for n in needles)
            _line(True if found else None, label,
                  "registered in Audacity" if found else "not found in pluginregistry.cfg")
        installed = appdata / "Macros" / macro.name
        _line(True if installed.is_file() else None, "Installed macro copy",
              "present" if installed.is_file() else "not yet installed (the first V3 run installs it)")

    print("\nOffline checks only; the AcoustID key and live recognition are NOT tested here.")
    print("Next: check_recognition.py --key-only, then check_recognition.py <original song>.")
    return 0 if ok else 1


def check_key():
    import requests
    key = os.getenv("ACOUSTID_API_KEY", "").strip()
    if not key:
        print("Missing ACOUSTID_API_KEY in .env.")
        return 1
    try:
        response = requests.post(
            acoustid_backend.API_BASE_URL + "lookup",
            data={"client": key, "format": "json",
                  "trackid": "9ff43b6a-4f16-427c-93c2-92307ca505e0"},
            timeout=acoustid_backend.REQUEST_TIMEOUT,
        )
        data = response.json()
        if response.status_code != 200:
            # AcoustID reports a bad key as HTTP 400 with error code 4.
            acoustid_backend.parse_response(data)
            print(f"AcoustID returned HTTP {response.status_code}; key not verified.")
            return 1
        count = acoustid_backend.parse_response(data)
        if count is None:
            print("AcoustID returned an unusable response; key not verified.")
            return 1
    except acoustid_backend.AcoustIDSetupError as exc:
        print(str(exc))
        return 1
    except Exception:
        print("AcoustID connection/response failed; key not verified. Check network access and TLS.")
        return 1
    print(f"AcoustID accepted the application key ({count} reference-track result(s)).")
    return 0


def main():
    load_dotenv(HERE / ".env")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", nargs="?")
    parser.add_argument("--key-only", action="store_true")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()
    if args.setup:
        return check_setup()
    if args.key_only:
        return check_key()
    if not args.audio or not Path(args.audio).is_file():
        parser.error("provide an existing audio file, --setup, or --key-only")
    try:
        _, fpcalc = acoustid_backend.validate_setup()
        import shazamio  # noqa: F401 -- check before the first API request
        import killa_tools
        try:
            duration, _fp = acoustid_backend.fingerprint_file(args.audio, fpcalc)
            print(f"Chromaprint: fingerprint generated (whole-file duration {duration:.1f}s, "
                  "first 120s fingerprinted).")
        except Exception:  # noqa: BLE001
            print("Chromaprint: fpcalc could not fingerprint this file (format/decoder problem).")
            return 1
        acoustic = acoustid_backend.match_count(args.audio, progress=print)
        shazam = killa_tools._shazam_match_count(args.audio, progress=print)
        songrec_cnt = None
        try:
            songrec_cnt = songrec_backend.match_count(args.audio, progress=print)
        except Exception:  # noqa: BLE001
            songrec_cnt = None
    except acoustid_backend.AcoustIDSetupError as exc:
        print(str(exc))
        return 1
    except Exception:
        print("Recognition check failed. Check requirements, audio format, and API connectivity.")
        return 1
    print(f"AcoustID: {'inconclusive' if acoustic is None else str(acoustic) + ' match(es)'}")
    print(f"ShazamIO: {'inconclusive' if shazam is None else str(shazam) + ' match(es)'}")
    print(f"SongRec: {'inconclusive' if songrec_cnt is None else str(songrec_cnt) + ' match(es)'}")
    if acoustic is None or shazam is None:
        return 1
    print("All APIs returned usable responses. A zero count is not proof of database coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
