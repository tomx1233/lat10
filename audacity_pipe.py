"""
Automation bridge for Audacity 2.3.3 (Windows) via mod-script-pipe.

Important background
-------------------
Audacity 2.3.3 does NOT support --batch command-line processing.  Its
command line accepts only -b/-d/-h/-t/-v plus positional file names
(verified against the Audacity 2.3.3 source, src/AudacityApp.cpp,
ParseCommandLine).  The official automation interface for 2.3.x is the
"mod-script-pipe" module, which is SHIPPED with Audacity 2.3.3 but must
be enabled once in Edit > Preferences > Modules, then Audacity restarted.

mod-script-pipe exposes two named pipes on Windows:
    \\\\.\\pipe\\ToSrvPipe   (client writes commands here)
    \\\\.\\pipe\\FromSrvPipe (client reads replies here)
Commands are terminated by CR LF NUL; a reply ends with a blank line.

Every macro (.txt) effect line is replayed as one script command, so the
exact chain the Macro Manager would run is executed: effects and
AudacityCommands are resolved by the same identifier lookup the macro
runner uses (src/BatchCommands.cpp, ApplyCommand).
"""

import json
import os
import re
import subprocess
import threading
import time

WRITE_PIPE = r"\\.\pipe\ToSrvPipe"
READ_PIPE = r"\\.\pipe\FromSrvPipe"
EOL = "\r\n\0"


class PipeError(RuntimeError):
    """Raised for any failure talking to Audacity over the scripting pipe."""


def _effect_key(name):
    """Normalize an effect name for comparison.

    The macro runner matches effects by command id, but LADSPA and VST
    effects are only reported in the Effect menu (e.g. "SC4..."), so keys
    are compared case- and separator-insensitively (Sc4 == SC4...).
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def close_vst_windows(title_substrings, pid=None):
    """Close top-level windows whose title contains one of the substrings.

    Used to release the scripting pipe when a VST editor window opened
    instead of the effect applying (see AudacitySession._send_vst_step).
    Returns the list of closed window titles.
    """
    subs = [s.lower() for s in title_substrings if s]
    if not subs:
        return []
    try:
        import ctypes

        user32 = ctypes.windll.user32
        wndenumproc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        closed = []

        @wndenumproc
        def _callback(hwnd, _lparam):
            owner = ctypes.c_uint()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if pid is None or owner.value == pid:
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, 256)
                low = title.value.lower()
                if title.value and any(s in low for s in subs):
                    user32.SendMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                    closed.append(title.value)
            return True

        user32.EnumWindows(_callback, None)
        return closed
    except Exception:  # noqa: BLE001 - recovery must never raise
        return []


# Audacity 2.3.3's mod-script-pipe reads commands into a 1024-byte buffer and
# writes a NUL one byte past the end on a full read (PipeServer.cpp, nBuff),
# which fail-fasts the whole process (c0000409).  Any command that long is
# refused up front; ~1000 leaves margin for the CR LF NUL terminator.
SAFE_COMMAND_BYTES = 1000

# Some 2.3.3 sessions open a VST's editor window instead of applying it over
# the scripting pipe; the reply never arrives.  Detect that within this many
# seconds, close the window to release the pipe, and fail the file cleanly.
VST_REPLY_TIMEOUT = 30.0


def hide_vst_windows(title_substrings, pid=None):
    """Hide top-level windows whose title contains one of the substrings.

    Same matching as close_vst_windows, but only hides the window — safe
    to do while Audacity is mid-apply.  Returns the number hidden.
    """
    subs = [s.lower() for s in title_substrings if s]
    if not subs:
        return 0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        wndenumproc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        hidden = 0

        @wndenumproc
        def _callback(hwnd, _lparam):
            nonlocal hidden
            owner = ctypes.c_uint()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if pid is None or owner.value == pid:
                # Match hidden windows too: a window the host re-shows lands
                # at its last position, so moving it off-screen while hidden
                # keeps re-shows invisible as well.
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, 256)
                low = title.value.lower()
                if title.value and any(s in low for s in subs):
                    user32.ShowWindow(hwnd, 0)  # SW_HIDE
                    user32.SetWindowPos(
                        hwnd, 0, -32000, -32000, 0, 0,
                        0x0001 | 0x0002 | 0x0010,  # SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE
                    )
                    hidden += 1
            return True

        user32.EnumWindows(_callback, None)
        return hidden
    except Exception:  # noqa: BLE001 - recovery must never raise
        return 0


class PipeClient:
    """Minimal named-pipe client for Audacity's mod-script-pipe.

    Modeled on the official pipeclient.py shipped in the Audacity repo
    (au3/scripts/piped-work/pipeclient.py): the write pipe is opened
    first, then a daemon reader thread drains the read pipe and stores
    the last complete reply.
    """

    def __init__(self, connect_timeout=5.0, reply_timeout=300.0):
        self.reply_timeout = reply_timeout
        self._reply = ""
        self._ready = threading.Event()
        self._dead = None

        deadline = time.time() + connect_timeout
        while True:
            try:
                # Match Audacity's ANSI codepage on the write side too, so
                # non-ASCII file paths / effect names reach it byte-exact.
                self._write_pipe = open(WRITE_PIPE, "w", newline="",
                                        encoding="cp1252", errors="replace")
                break
            except OSError as exc:
                if time.time() >= deadline:
                    raise PipeError(
                        "Cannot connect to Audacity's scripting pipe. "
                        "Start Audacity (Edit > Preferences > Modules > "
                        "mod-script-pipe = Enabled, then restart) and try again. "
                        f"({exc})"
                    ) from exc
                time.sleep(0.4)

        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            # Audacity 2.3.3 writes replies in the ANSI codepage (cp1252), not
            # UTF-8 -- effect/menu labels can carry bytes like 0xd7 ("x").
            # Strict UTF-8 killed the reader on the first such byte and every
            # later command then failed ("did not return its effect list");
            # ids are matched after _effect_key strips non-alphanumerics, so a
            # replaced byte in a label is harmless.
            with open(READ_PIPE, "r", newline="", encoding="cp1252",
                      errors="replace") as read_pipe:
                while True:
                    lines = []
                    while True:
                        line = read_pipe.readline()
                        if line == "":
                            # EOF on the pipe: Audacity quit or crashed.
                            self._die("Audacity closed the scripting pipe")
                            return
                        if line == "\n":
                            break  # blank line terminates a reply
                        lines.append(line)
                    self._reply = "".join(lines)
                    self._ready.set()
        except Exception as exc:  # noqa: BLE001 - report any reader failure
            self._die(str(exc))

    def _die(self, message):
        self._dead = message
        self._reply = message
        self._ready.set()

    def send(self, command, timeout=None):
        """Send one command; return the reply text. Raises PipeError on failure."""
        if self._dead:
            raise PipeError(self._dead)
        self._reply = ""
        self._ready.clear()
        try:
            self._write_pipe.write(command + EOL)
            self._write_pipe.flush()
        except OSError as exc:
            raise PipeError(f"Write to Audacity failed: {exc}") from exc
        if not self._ready.wait(timeout or self.reply_timeout):
            raise PipeError(
                "Timed out waiting for Audacity to answer: "
                + command.split(":", 1)[0]
            )
        if self._dead:
            raise PipeError(self._dead)
        return self._reply

    def close(self):
        try:
            self._write_pipe.close()
        except OSError:
            pass


def parse_macro(path):
    """Parse an Audacity macro (.txt) exactly like 2.3.3's ReadMacro.

    Each non-empty line is ``Command:param1="v" param2="v"``.  The command
    is everything before the first colon (trimmed); parameters are
    everything after it (trailing whitespace trimmed).  Lines without a
    colon (comments/blank lines) are skipped.

    Returns a list of (command, params) tuples.
    """
    steps = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            split_at = line.find(":")
            if split_at < 0:
                continue
            command = line[:split_at].strip()
            params = line[split_at + 1 :].rstrip()
            if command:
                steps.append((command, params))
    return steps


class AudacitySession:
    """Drives one Audacity 2.3.3 instance over the scripting pipe.

    Launches Audacity via subprocess if the pipe is not already present,
    waits for the pipe, validates that every macro effect is installed,
    then runs the macro file-by-file.
    """

    def __init__(self, exe_path, log=None, connect_timeout=30.0, reply_timeout=300.0):
        self.exe_path = exe_path
        self.log = log or (lambda message: None)
        self.connect_timeout = connect_timeout
        self.reply_timeout = reply_timeout
        self.client = None
        self.process = None
        self.effect_ids = None
        self.raw_menu_ids = set()
        self._vst_keys = None
        self._vst_symbols = None
        self._hider_stop = None

    # ------------------------------------------------------------------ setup

    def ensure_connected(self):
        """Connect to a running Audacity, or launch it and wait for the pipe."""
        if self.client is not None:
            return
        try:
            self.client = PipeClient(connect_timeout=2.0, reply_timeout=self.reply_timeout)
            self.log("Connected to running Audacity.")
            # An Audacity left wedged by a previous crashed session accepts
            # the connection but never answers, which would fail everything
            # with confusing errors.  Probe it; restart if it is unresponsive.
            try:
                self._send('GetInfo: Type="Tracks" Format="JSON"', timeout=10.0)
                return
            except PipeError:
                self.log(
                    "The running Audacity is not answering; restarting it..."
                )
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self.client = None
                subprocess.run(
                    ["taskkill", "/IM", "audacity.exe"],
                    capture_output=True,
                    timeout=15,
                )
                time.sleep(2)
        except PipeError:
            pass

        if not self.exe_path or not os.path.isfile(self.exe_path):
            raise PipeError(f"Audacity executable not found: {self.exe_path}")
        # A project left by a previous (terminated) session makes Audacity
        # show its "Automatic Crash Recovery" dialog at launch; stale
        # auto-saves are irrelevant to batch runs, so remove them.
        try:
            auto_save_dir = os.path.join(
                os.environ.get("APPDATA", ""), "Audacity", "AutoSave"
            )
            if os.path.isdir(auto_save_dir):
                for name in os.listdir(auto_save_dir):
                    if name.endswith(".autosave"):
                        try:
                            os.remove(os.path.join(auto_save_dir, name))
                        except OSError:
                            pass
        except OSError:
            pass
        self.log(f"Launching Audacity (hidden): {self.exe_path}")
        try:
            # Start fully hidden so batch runs never show a window, not even
            # a taskbar button; the scripting pipe is independent of window
            # visibility.
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            self.process = subprocess.Popen(
                [self.exe_path],
                cwd=os.path.dirname(self.exe_path),
                startupinfo=startupinfo,
            )
        except OSError as exc:
            raise PipeError(f"Could not start Audacity: {exc}") from exc
        self._start_vst_hider()

        deadline = time.time() + self.connect_timeout
        # The pipe server starts before Audacity finishes initializing; a
        # command sent too early (e.g. GetInfo) gets an EMPTY reply and leaves
        # the queue wedged.  A cold start also re-scans plugins first
        # (pluginregistry.cfg is rewritten), which blocks the server for
        # several seconds.  Don't hand the session back on a blind sleep --
        # probe the same GetInfo the effect list needs and wait until
        # Audacity actually answers, reconnecting between tries.
        while time.time() < deadline:
            client = None
            try:
                client = PipeClient(
                    connect_timeout=2.0, reply_timeout=self.reply_timeout
                )
                self.log("Connected to Audacity scripting pipe.")
                reply = client.send(
                    'GetInfo: Type="Commands" Format="JSON"', timeout=15.0
                )
                if '"id"' in reply:
                    self.client = client
                    self.log("Audacity is answering script commands.")
                    return
                self.log(
                    "Audacity's pipe is up but the command list came back "
                    "empty -- it is still starting up; waiting..."
                )
            except PipeError as exc:
                self.log(f"Audacity not ready yet ({exc}); waiting...")
            try:
                if client is not None:
                    client.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.0)
        raise PipeError(
            "Timed out waiting for Audacity. Enable mod-script-pipe once "
            "(Edit > Preferences > Modules > mod-script-pipe = Enabled), "
            "restart Audacity, then try again."
        )

    # ------------------------------------------------------------- inspection

    def get_effect_ids(self):
        """Return the set of effect identifiers Audacity knows.

        Uses GetInfo: Type="Commands" (the lookup the macro runner uses) and
        also collects ids listed under the Effect menu, because LADSPA and
        VST effects (e.g. SC4) appear there but not in the command list.
        Retries once through a fresh pipe connection if the reply is empty
        or fails (a just-started or wedged Audacity can do both).
        """
        ids = set()
        for attempt in range(2):
            error = None
            try:
                reply = self.client.send('GetInfo: Type="Commands" Format="JSON"')
                ids = set(re.findall(r'"id"\s*:\s*"([^"]+)"', reply))
                menus = self.client.send('GetInfo: Type="Menus" Format="JSON"')
                self.raw_menu_ids = set(self._menu_ids(menus))
                ids.update(self.raw_menu_ids)
            except PipeError as exc:
                error = exc
                ids = set()
            if ids:
                break
            if attempt == 0:
                reason = error or "empty effect list"
                self.log(f"Effect list fetch failed ({reason}); retrying once.")
                # A just-started Audacity can return empty replies for a few
                # seconds; give it a beat before the retry reconnects.
                time.sleep(2.0)
                self._reconnect()
        if not ids:
            raise PipeError(
                "Audacity did not return its effect list. It may still be "
                "starting up or needs to be restarted — wait a few seconds "
                "and try again."
            )
        self.effect_ids = {_effect_key(i) for i in ids}
        return self.effect_ids

    @staticmethod
    def _menu_ids(menus):
        """Extract command ids from a Menus reply.

        LADSPA/VST effects (e.g. "SC4...") and editing commands (Duplicate,
        MixAndRender, Select All...) appear in the menus but not in the
        GetInfo command list, so the menu ids are folded into the known set.
        """
        return re.findall(r'"id"\s*:\s*"([^"]+)"', menus)

    def check_effects(self, steps):
        """Return (missing, known) effect names for the macro steps."""
        known = self.get_effect_ids()
        names = [name for name, _ in steps]
        missing = [name for name in names if not self._known_effect(name, known)]
        return missing, [name for name in names if self._known_effect(name, known)]

    @staticmethod
    def _known_effect(name, known):
        """Exact id match, or a path-style listing carrying the effect name.

        Audacity 2.3.3 sometimes lists a VST by its FILE PATH (e.g.
        "C:\\...\\TDR Nova.dll;0") instead of its own name while still
        applying it fine -- the macro runner resolves the step name either
        way (verified 2026-10-05 with TDR Nova). Path listings normalize to
        a string containing "dll", so substring matching is restricted to
        those to avoid false positives against built-in command ids.
        """
        key = _effect_key(name)
        if key in known:
            return True
        return any(key in item and "dll" in item for item in known)

    # ------------------------------------------------------------- processing

    def _send(self, command, timeout=None):
        reply = self.client.send(command, timeout=timeout)
        if reply.strip():
            self.log(f"[Audacity] {command.split(':', 1)[0]} -> {reply.strip()[:300]}")
        if "BatchCommand finished: Failed!" in reply:
            raise PipeError(
                f"Audacity rejected the command: {command.split(':', 1)[0]}"
            )
        return reply

    def _load_vst_registry(self):
        """Parse pluginregistry.cfg for registered VST effects.

        Sections look like:
            [pluginregistry/Effect/base64:...]
            Symbol=TDR Nova
            ...
            EffectFamily=VST
        Populates self._vst_keys (normalized, for step classification) and
        self._vst_symbols (raw names, for matching editor window titles).
        Only entries Audacity marked Enabled=1 and Valid=1 count -- a disabled
        or invalid registration is effectively not installed.
        """
        self._vst_keys = set()
        self._vst_symbols = []
        family = None
        symbol = None
        enabled = valid = True
        cfg = os.path.join(
            os.environ.get("APPDATA", ""), "Audacity", "pluginregistry.cfg"
        )
        try:
            with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("["):
                        if family == "VST" and symbol and enabled and valid:
                            self._vst_keys.add(_effect_key(symbol))
                            self._vst_symbols.append(symbol)
                        family = symbol = None
                        enabled = valid = True
                    elif line.startswith("EffectFamily="):
                        family = line.split("=", 1)[1].strip()
                    elif line.startswith("Symbol="):
                        symbol = line.split("=", 1)[1].strip()
                    elif line.startswith("Enabled="):
                        enabled = line.split("=", 1)[1].strip() != "0"
                    elif line.startswith("Valid="):
                        valid = line.split("=", 1)[1].strip() != "0"
            if family == "VST" and symbol and enabled and valid:
                self._vst_keys.add(_effect_key(symbol))
                self._vst_symbols.append(symbol)
        except OSError:
            self.log(
                "WARNING: could not read pluginregistry.cfg; VST steps will "
                "not get editor-window hang detection."
            )

    def registry_has_effects(self, names):
        """True when every named effect is registered AND enabled in Audacity's
        plugin registry, even though the running instance doesn't list it.

        A fresh instance lists its built-in commands immediately but VSTs only
        appear once the startup plugin scan finishes, and an instance launched
        before a plugin was registered never lists it at all. Only VSTs live
        in pluginregistry.cfg, so a name that is not a registered VST cannot
        be fixed by waiting and stays missing.
        """
        missing_keys = [_effect_key(n) for n in names]
        if not missing_keys:
            return True
        known = self._vst_effect_keys()
        return all(key in known for key in missing_keys)

    def reload_registry(self):
        """Drop the cached plugin-registry parse so the next lookup re-reads
        the file (Audacity rewrites it when a scan finishes or fails)."""
        self._vst_keys = None

    def _vst_effect_keys(self):
        if self._vst_keys is None:
            self._load_vst_registry()
        return self._vst_keys

    def _audacity_pid(self):
        if self.process is not None and self.process.poll() is None:
            return self.process.pid
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq audacity.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            for line in out.splitlines():
                if line.lower().startswith('"audacity.exe"'):
                    return int(line.split('","')[1])
        except Exception:  # noqa: BLE001 - best effort only
            pass
        return None

    def _start_vst_hider(self):
        """Hide VST editor windows that pop up while a macro runs.

        Some VSTs (e.g. TDR Nova) show their editor window even when the
        effect is applied through the macro runner.  Hiding is purely
        visual and safe mid-apply, so a light poller keeps everything
        invisible while our own hidden Audacity processes.
        """
        if self._hider_stop is not None:
            return
        self._load_vst_registry()
        symbols = list(self._vst_symbols)
        self._hider_stop = threading.Event()
        if not symbols:
            return

        def _hider():
            while not self._hider_stop.wait(0.15):
                process = self.process
                if process is None or process.poll() is not None:
                    continue
                try:
                    hide_vst_windows(symbols, process.pid)
                except Exception:  # noqa: BLE001 - never kill the poller
                    pass

        threading.Thread(target=_hider, daemon=True).start()

    def _reconnect(self):
        """Drop the (possibly wedged) pipe and open a fresh connection.

        The stale reply of a timed-out command may still arrive on the old
        connection; a new pipe session guarantees reply ordering.
        """
        try:
            if self.client is not None:
                self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self.client = PipeClient(self.connect_timeout, self.reply_timeout)

    def _send_vst_step(self, name, params):
        """Send a VST effect step, recovering from editor-window hangs.

        Some Audacity 2.3.3 sessions open the VST's editor window instead of
        applying it; the reply then never arrives.  Close the window (which
        releases the pipe), reconnect, and fail just this file with a clear
        message so the batch can continue.
        """
        try:
            return self._send(f"{name}:{params}", timeout=VST_REPLY_TIMEOUT)
        except PipeError as exc:
            if "Timed out" not in str(exc):
                raise
            self.log(
                f"[Audacity] {name} did not answer within "
                f"{VST_REPLY_TIMEOUT:.0f}s; closing its editor window..."
            )
            if self._vst_symbols is None:
                self._load_vst_registry()
            closed = close_vst_windows(self._vst_symbols, self._audacity_pid())
            if closed:
                time.sleep(1.0)
            self._reconnect()
            raise PipeError(
                f"Audacity opened the '{name}' editor window instead of "
                "applying the effect (a 2.3.3 VST automation bug). "
                "This file was skipped; re-run the batch to retry it."
                + (
                    f" Closed window(s): {', '.join(closed)}."
                    if closed
                    else " (No editor window was found.)"
                )
            ) from exc

    def _cleanup_project(self):
        """Best-effort removal of any tracks left in the project."""
        try:
            self._send("SelectAll:")
            self._send("RemoveTracks:")
        except PipeError:
            pass

    def _probe_channels(self):
        """Return the channel count of the imported tracks (0 if none).

        The GetInfo reply should be a JSON array of track objects; if it
        cannot be parsed we assume stereo and let the export step decide.
        """
        reply = self._send('GetInfo: Type="Tracks" Format="JSON"')
        data = None
        try:
            data = json.loads(reply)
        except (ValueError, TypeError):
            match = re.search(r"\[.*\]", reply, re.S)
            if match:
                try:
                    data = json.loads(match.group(0))
                except (ValueError, TypeError):
                    data = None
        if data is None:
            self.log("WARNING: could not parse track info; assuming stereo.")
            return 2
        channels = 0
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict) and "channels" in entry:
                channels = max(channels, int(entry["channels"]))
        return channels

    def _macro_menu_id(self, macro_name):
        """Return the Tools > Apply Macro menu id for a macro name, or None.

        Audacity builds one menu item per installed macro, e.g. the macro
        "Loud Method" becomes the command id "Macro_LoudMethod" (spaces
        removed, case preserved).
        """
        if not macro_name or not self.raw_menu_ids:
            return None
        candidate = "Macro_" + macro_name.replace(" ", "")
        if candidate in self.raw_menu_ids:
            return candidate
        wanted = _effect_key(candidate)
        for raw in self.raw_menu_ids:
            if raw.startswith("Macro_") and _effect_key(raw) == wanted:
                return raw
        return None

    def macro_menu_available(self, macro_name):
        """True if Audacity has a Tools > Apply Macro item for this macro.

        The menu is built when Audacity starts, so a macro created after
        launch has no item until Audacity is restarted (the caller can drop
        the session to force that).
        """
        return self._macro_menu_id(macro_name) is not None

    def clear_project(self):
        """Remove every track so a job starts from an empty project.

        Import2 ADDS to whatever is open; if a previous job crashed before
        its cleanup (or a recovery dialog restored a project), the next run
        would apply the macro to -- and export -- audio from earlier jobs.
        """
        try:
            if self._probe_channels() > 0:
                self._send("SelectAll:")
                self._send("RemoveTracks:")
        except PipeError:
            pass  # best effort; the export step still validates its output

    def apply_macro_to_file(self, src, dst, steps, macro_name=None):
        """Run the macro steps on one file, exporting to dst.

        When macro_name is given and that macro is installed in Audacity
        (it has a Tools > Apply Macro menu item), the whole macro runs
        through Audacity's own macro runner: this avoids the scripting
        pipe's 1024-byte command limit and 2.3.3's flaky per-step VST
        dispatch.  Otherwise the steps are replayed one by one over the
        pipe.

        Returns the reply text of the last command (empty string is fine).
        Raises PipeError if a step fails; the caller checks dst as well.
        """
        if self.client is None:
            self.ensure_connected()
        src = os.path.abspath(src)
        dst = os.path.abspath(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        try:
            self.clear_project()
            self._send(f'Import2: Filename="{src}"')
            channels = self._probe_channels()
            if channels <= 0:
                raise PipeError(
                    f"Import failed or produced no audio tracks: {src}"
                )
            self._send("SelectAll:")
            menu_id = self._macro_menu_id(macro_name)
            if menu_id:
                self.log("Running macro via Audacity's runner.")
                self._send(menu_id + ":")
            else:
                if macro_name:
                    self.log(
                        f"Macro '{macro_name}' is not installed in Audacity's "
                        "Macros folder; replaying steps over the pipe."
                    )
                for name, params in steps:
                    command = f"{name}:{params}"
                    if len(command) > SAFE_COMMAND_BYTES:
                        raise PipeError(
                            f"The macro step '{name}' is {len(command)} characters, "
                            "but Audacity 2.3.3's scripting pipe crashes on any "
                            f"command longer than ~{SAFE_COMMAND_BYTES} characters. "
                            "Copy the macro into Audacity's Macros folder so it "
                            "can run via Tools > Apply Macro, or shorten the "
                            "step's parameter list."
                        )
                    if _effect_key(name) in self._vst_effect_keys():
                        self._send_vst_step(name, params)
                    else:
                        self._send(command)
            # A macro may end with a step that unselects the track (e.g.
            # MixAndRender), which would make Export2 write an empty file.
            self._send("SelectAll:")
            export_reply = self._send(
                f'Export2: Filename="{dst}" NumChannels="{channels}"'
            )
            if not os.path.isfile(dst):
                raise PipeError(
                    f"Audacity did not create the export file: {dst}"
                )
        except PipeError:
            self._cleanup_project()
            raise
        self._send("SelectAll:")
        self._send("RemoveTracks:")
        return export_reply

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self._hider_stop is not None:
            self._hider_stop.set()
        # Leave the Audacity process running if it was already running;
        # only close a process we started ourselves.
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.process = None
