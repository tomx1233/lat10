"""
KILLA'S BOSS -- Killa tools engine.

A faithful port of the useful Scope bot (github.com/typicaalusername/scope)
commands, written for the Python discord.py audio bot. Everything here is a
SYNCHRONOUS helper (subprocess ffmpeg/ffprobe + urllib), meant to be called
through `asyncio.to_thread` so it never blocks the Discord event loop.

Command surface this module backs (the slash wrappers live in audio_bot.py):
  /analyze      -> analyze_audio() + render_waveform()
  /copyr8 (CopyR8) -> copyr8_autofit() / copyr8_autofit_v2() /
                   copyr8_autofit_v3() (the auto-fit searches probe ShazamIO
                   and AcoustID until the song is no longer
                   identifiable) + copyr8_process() (the subtle / balanced /
                   stealth / aggressive fixed shift presets)
  /roblox       -> roblox_simulate()                     (two-pass ogg compression)
  /monitor      -> get_asset_moderation()                (poll a Roblox asset's moderation)
  /download     -> download_media() (yt-dlp CLI: audio mp3 or <=720p video mp4)
  /ping         -> get_system_stats()
  /shazam       -> shazam_recognize() (placeholder disabled, kept for future song checking)

Attribution: this bot is a fork of typicaalusername's Scope bot (GPL-3.0). The
ffmpeg filter recipes (ebur128 loudness, showwavespic overlay, anequalizer notch,
libvorbis two-pass) and the two Roblox polling endpoints are derived from it.
"""

import glob
import gzip
import json
import math
import os
import threading
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import acoustid_backend
import songrec_backend

# The grant account session used for user-auth / toolbox endpoints. Loaded from
# .env the same way grant_audio does (audio_bot.py calls load_dotenv before
# importing this module, so the var is already present).
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "")

# Workspace root reference (kept for potential future modules).
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0 Safari/537.36 RobloxApp")

# Endpoints (same as the original Scope bot; both confirmed against the live API).
MODERATION_URL = "https://apis.roblox.com/assets/user-auth/v1/assets/{id}"
TOOLBOX_URL = ("https://apis.roblox.com/toolbox-service/v1/marketplace/3"
               "?keyword={artist}&limit=999999&uiSortIntent=10")
ASSET_DELIVERY_URL = "https://assetdelivery.roblox.com/v1/asset/?id={id}"

GREEN = 0x4CAF50
RED = 0xE53935


# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #
def _run(args, timeout=180):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        # capture_output + text should always yield strings; keep it that way
        # explicitly -- callers do out.strip()/err.strip() and a stray None
        # surfaced as "'NoneType' object has no attribute 'strip'".
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        raise RuntimeError("ffmpeg/ffprobe is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out processing that file (it may be too long).")


_FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
_FFPROBE = ["ffprobe", "-v", "error"]


def _ffmpeg_probe_json(path):
    """Build an ffprobe-shaped dict from ffmpeg's own input report.

    In some environments ffprobe fails on files ffmpeg reads without trouble
    (fresh downloads / renders, empty error output -- seen 2026-10-05).
    ffmpeg prints the same format/stream facts to stderr for any input, so
    parse those into the shape ffprobe_json's callers expect. Raises
    RuntimeError when ffmpeg can't identify the file either (the message
    carries the tail of ffmpeg's output so the cause is visible in logs).
    """
    last = ""
    for attempt in range(3):
        rc, _out, err = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                              "-f", "null", "-"])
        text = err or ""
        # ffmpeg's report re-lists the stream in a trailing Output section with
        # the muxer's own format (e.g. pcm_s16le for the null muxer); only the
        # input report carries the real codec, so cut everything after it.
        text = text.split("Output #0", 1)[0]
        if text.strip():
            last = text
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        duration = None
        if m:
            duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        br = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
        # The codec can carry a parenthetical qualifier ("mp3 (mp3float)",
        # "aac (LC)"); the comma only follows it.
        sm = re.search(r"Stream #\d+:\d+[^\n]*?: Audio:\s*([\w.]+)"
                       r"(?:\s*\([^)]*\))?,\s*(\d+)\s*Hz,\s*([a-z0-9.]+)", text)
        if sm or duration is not None:
            stream = {}
            if sm:
                layout = sm.group(3).lower()
                channels = {"mono": 1, "stereo": 2}.get(layout)
                if channels is None:
                    digits = re.findall(r"\d+", layout)
                    channels = sum(int(d) for d in digits) or None
                stream = {"codec_type": "audio", "codec_name": sm.group(1),
                          "sample_rate": sm.group(2), "channels": channels}
            return {
                "format": {"duration": str(duration) if duration is not None else None,
                           "bit_rate": str(int(br.group(1)) * 1000) if br else None},
                "streams": [stream],
            }
        if attempt < 2:
            time.sleep(1.0 if attempt == 0 else 3.0)
    tail = " ".join(last.strip().splitlines()[-4:])[:400]
    raise RuntimeError("ffmpeg could not identify that file either."
                       + (f" ({tail})" if tail else ""))


def ffprobe_json(path):
    """Probe an audio file -> dict with 'format' and 'streams'."""
    # A freshly written file can fail its first probe with no usable detail
    # (an antivirus scan / lock window); back off and retry a couple of times.
    rc = out = err = None
    for attempt in range(3):
        rc, out, err = _run(_FFPROBE + ["-print_format", "json", "-show_format",
                                        "-show_streams", path])
        if rc == 0 and out.strip():
            return json.loads(out)
        if attempt < 2:
            time.sleep(1.0 if attempt == 0 else 3.0)
    # ffprobe keeps failing while ffmpeg demonstrably reads the same file
    # (the rest of this pipeline ran on it) -- fall back to ffmpeg's report.
    return _ffmpeg_probe_json(path)


def _loudness(path):
    """Integrated loudness (LUFS) + true peak (dBFS) via ffmpeg ebur128."""
    # ebur128 prints its summary at info level, so we can't pass -loglevel error.
    rc, _out, err = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                          "-af", "ebur128=peak=true", "-f", "null", "-"])
    if not err:
        raise RuntimeError("ffmpeg gave no loudness output.")
    m_lufs = __import__("re").search(r"Integrated loudness[\s\S]*?I:\s+(-?[\d.]+) LUFS", err)
    m_peak = __import__("re").search(r"Peak:\s+(-?[\d.]+) dBFS", err)
    return (float(m_lufs.group(1)) if m_lufs else None,
            float(m_peak.group(1)) if m_peak else None)


def _sample_rate(path):
    """Pull the audio stream sample rate (ffprobe), defaulting sensibly."""
    try:
        data = ffprobe_json(path)
        stream = next(s for s in data.get("streams", [])
                      if s.get("codec_type") == "audio")
        return int(stream.get("sample_rate") or 44100) or 44100
    except Exception:
        return 48000


# The loudness restore the auto-fit applies on export. A phase vocoder rebuilds a
# waveform without the flat-topped peaks a loud master is built from, so the same
# music comes back quieter: measured on a real track, -8.7 LUFS going in and
# -17.7 LUFS coming out, with the render already sitting on the peak ceiling, so
# no gain change closes that gap on its own. Getting the loudness back means
# putting those peaks back, which is a clipper's job rather than a limiter's --
# measured on that same track at a matched loudness, a limiter flattened the
# song's range from 9.1 LU to 3.1, while a soft clipper held 8.0 and landed within
# 2 LU of the source. A clipper distorts the peaks the way the source's own master
# already does; a limiter squashes the whole song, which is the "horrid" this
# stage exists to avoid.
# The clipper runs oversampled so its harmonics don't fold back. It also applies
# a fixed -9.7 dB of its own (measured the same at every level, so it is a static
# gain rather than the knee), which is why the correction below is larger than the
# loudness gap it closes: the loop measures the file it actually produced and
# compensates, so what the filter does internally never has to be guessed at. The
# loop also backs the loudness off if the copy would end up harder clipped than
# the record it came from.
AUTOFIT_CLIP_THRESHOLD = 0.9
AUTOFIT_LOUDNESS_PASSES = 6
AUTOFIT_MAX_GAIN_DB = 24.0        # sanity bound on one correction step
AUTOFIT_LOUDNESS_TOLERANCE = 0.3  # dB; close enough to stop iterating
AUTOFIT_PEAK_SLACK_DB = 1.0       # allowed above the source's own true peak
AUTOFIT_PEAK_CAP_DB = 3.0         # cap used when the source can't be measured


def _match_loudness(work_wav, output_path, source_path, progress=None):
    """Re-encode `work_wav` to MP3 at `output_path`, matching the source's loudness.

    Returns {"source_lufs", "out_lufs", "out_peak", "gain_db"}; the loudness
    figures are None when ffmpeg can't measure the file, in which case the plain
    re-encode is kept. An unmeasurable source is not an error -- the export must
    always produce a file.
    """
    try:
        target, source_peak = _loudness(source_path)
    except Exception:
        target = source_peak = None
    peak_cap = (source_peak + AUTOFIT_PEAK_SLACK_DB if source_peak is not None
                else AUTOFIT_PEAK_CAP_DB)

    def _encode(dst, gain_db):
        af = (f"volume={gain_db:.2f}dB,asoftclip=type=atan:oversample=4"
              f":threshold={AUTOFIT_CLIP_THRESHOLD}")
        rc, _out, err = _run(_FFMPEG + ["-i", work_wav, "-af", af,
                                        "-c:a", "libmp3lame", "-b:a", "192k", dst])
        if rc != 0:
            raise RuntimeError("auto-fit mp3 re-encode failed."
                               + (f" ({err.strip()})" if err.strip() else ""))

    # Always encode from the engine's WAV, never from a previous mp3, so the
    # iterations don't stack lossy generations on each other.
    tmp = output_path + ".lvl.mp3"
    total_gain = 0.0
    out_lufs = out_peak = None
    for _ in range(AUTOFIT_LOUDNESS_PASSES if target is not None else 1):
        _encode(tmp, total_gain)
        os.replace(tmp, output_path)
        if target is None:
            break
        try:
            out_lufs, out_peak = _loudness(output_path)
        except Exception:
            break
        if out_lufs is None:
            break
        step = target - out_lufs
        if out_peak is not None and out_peak > peak_cap + AUTOFIT_LOUDNESS_TOLERANCE:
            step -= out_peak - peak_cap
        if abs(step) <= AUTOFIT_LOUDNESS_TOLERANCE:
            break
        step = max(-AUTOFIT_MAX_GAIN_DB, min(AUTOFIT_MAX_GAIN_DB, step))
        if progress:
            progress(f"level: the copy measures {out_lufs:.1f} LUFS against the "
                     f"source's {target:.1f}, applying {step:+.1f} dB")
        total_gain += step

    return {"source_lufs": target, "out_lufs": out_lufs, "out_peak": out_peak,
            "gain_db": round(total_gain, 2)}


# --------------------------------------------------------------------------- #
# /analyze
# --------------------------------------------------------------------------- #
def analyze_audio(path):
    """Return a dict of format/loudness info for an audio file."""
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    stream = next((s for s in data.get("streams", [])
                   if s.get("codec_type") == "audio"), {})
    lufs, peak = _loudness(path)

    duration = float(fmt.get("duration") or 0)
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    bitrate = fmt.get("bit_rate")
    return {
        "duration": f"{minutes}:{seconds:02d}",
        "duration_secs": duration,
        "bitrate_kbps": round(int(bitrate) / 1000) if bitrate else None,
        "sample_rate": stream.get("sample_rate"),
        "codec": stream.get("codec_name"),
        "channels": stream.get("channels"),
        "lufs": lufs,
        "peak": peak,
    }


def _result_info(output_path, progress=None, loudness=None):
    """Format/loudness stats for a copyr8 reply embed -- never fatal.

    The finished export is the product; these fields only decorate it and the
    embed renders dashes for whatever is missing. ffprobe has been seen to
    fail on a freshly written export with no usable detail (2026-10-05) even
    though the file is fine -- the spectrogram step read it seconds before --
    so a stats failure here degrades the reply instead of discarding the
    finished file.
    """
    try:
        return analyze_audio(output_path)
    except (RuntimeError, ValueError) as exc:
        if progress:
            progress("the finished file's format stats could not be read; "
                     "delivering it anyway")
        return {"duration_secs": None,
                "lufs": (loudness or {}).get("out_lufs"),
                "peak": (loudness or {}).get("out_peak")}


def render_waveform(path, out_path, size="1920x660"):
    """Render a peak+rms waveform PNG (blue peak over darker purple rms)."""
    peak_color, rms_color = "2986CC", "5C3D8C"
    fx = (f"[0:a]showwavespic=s={size}:colors={peak_color}:filter=peak"
          f":split_channels=1[peaks];"
          f"[0:a]showwavespic=s={size}:colors={rms_color}:filter=average"
          f":split_channels=1[rms];[peaks][rms]overlay")
    rc, _out, err = _run(_FFMPEG + ["-i", path, "-filter_complex", fx,
                                    "-update", "1", out_path])
    if rc != 0:
        raise RuntimeError("waveform render failed." + (f" ({err.strip()})" if err.strip() else ""))
    return out_path


# Brand tint for the spectrogram image: recolor the ffmpeg luminance ramp onto the
# KILLA'S BOSS blue-purple palette so the graph matches the embeds + waveform
# (dark violet -> brand purple -> peak blue) instead of the default plasma ramp.
_BRAND_LOW = (58, 25, 76)     # deep violet  #3A194C
_BRAND_MID = (92, 61, 140)    # rms purple   #5C3D8C
_BRAND_HIGH = (41, 134, 204)  # peak blue    #2986CC


def _brand_spectrogram(path):
    """Recolor `path` (an ffmpeg spectrogram PNG) onto the brand blue/purple ramp.

    ffmpeg's showspectrumpic `color` option only accepts named colormaps (plasma,
    magma, ...) -- none is the KILLA'S BOSS palette. So we keep any valid colormap,
    take its grayscale luminance as the energy ramp, and redraw it as a
    dark-violet -> purple -> blue gradient that matches the waveform + embeds.
    """
    import numpy as np
    from PIL import Image

    lum = np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0
    low, mid, high = (np.array(_BRAND_LOW, np.float32),
                      np.array(_BRAND_MID, np.float32),
                      np.array(_BRAND_HIGH, np.float32))
    t = np.clip(lum, 0.0, 1.0)[..., None]            # (h, w, 1)
    down = low + (mid - low) * np.clip(t * 2.0, 0.0, 1.0)
    up = mid + (high - mid) * np.clip((t - 0.5) * 2.0, 0.0, 1.0)
    rgb = np.where(t <= 0.5, down, up).astype("uint8")
    Image.fromarray(rgb, "RGB").save(path)


# --------------------------------------------------------------------------- #
# /copyr8 -- the auto-fit engines (ShazamIO + AcoustID + SongRec)
# --------------------------------------------------------------------------- #
# The existing search ladders now use three independent recognition APIs.
# Acceptance rules:
#   * probe windows are cut as 12s clips (Shazam only needs a short clip);
#   * a window is clean only when Shazam returns a definite empty match list --
#     a body without a `matches` list is the classic throttle signature and is
#     NEVER read as "0 matches" (that would accept a shift Shazam can still
#     identify);
#   * after EVERY Shazam window passes, AcoustID must return zero matches
#     for the candidate file fingerprint; any inconclusive check rejects it;
#   * a clean sweep is corroborated with a control probe of the untouched
#     original (a soft-throttled server answers with an empty match list, which
#     reads exactly like a genuine miss) -- see the `verified` result flag.
# Both searches escalate the mildest shift first, so the accepted copy stays as
# close as possible to the original song. Neither API is a copyright verdict.

# The tremolo Auto-Fit V2 layers on top of the shift it settles on, gentlest
# first. The search itself can never return a wobble: tremolo candidates cost
# more than the pure shift with the same pitch/tempo, so the walk always stops on
# the pure one first. The wobble therefore has to be added afterwards -- and
# re-verified, because whether a wobble breaks a working setting is a property of
# the track. The first rung that comes back clean is the one that ships, so a
# track that only tolerates a hint of wobble gets a hint of wobble, and a track
# that refuses every rung keeps the pure shift and says so in the reply.
#
# Order is the whole point. These used to run strongest-first (0.5 @ 5 Hz), and
# 0.5 depth is a 50% volume swing -- a helicopter pumping that is unmistakably
# "processed" -- which shipped whenever it verified clean. A clean verdict only
# means the matcher lost the track; it says nothing about whether the file still
# sounds like the song, so the depth here stays in the range that reads as a slow
# swell rather than a broken volume knob. The two firmer rungs exist because a
# stubborn track can refuse every gentle one (one measured track refused all
# three), and a slightly stronger wobble on the same pitch/tempo still sounds
# closer to the original than the speed-up the search would otherwise escalate
# to. 0.25 @ 5 Hz is the ceiling; past that it is the helicopter again.
AUTOFIT_V2_TREMOLO = [(0.06, 1.0), (0.10, 2.0), (0.15, 3.0),
                      (0.20, 4.0), (0.25, 5.0)]


# /copyr8 preset table: name -> (pitch_semitones, tempo_factor).
# These just re-encode a file with a key/speed shift; the names stay neutral.
COPYR8_PRESETS = {
    "subtle":     (1.5, 1.04),
    "balanced":   (2.0, 1.07),
    "stealth":    (2.5, 1.10),
    "aggressive": (3.0, 1.12),
}


def copyr8_process(input_path, output_path, spectrogram_path, preset_name="balanced"):
    """Re-encode a file with an independent pitch + tempo shift preset.

    Uses `asetrate` to shift the pitch and `atempo` to restore or change the
    tempo, so a file can be shifted in key and/or speed independently of its
    duration. Returns the rebuilt info dict for the processed file.
    """
    if preset_name not in COPYR8_PRESETS:
        raise RuntimeError(f"unknown preset {preset_name!r}")
    semi, tempo = COPYR8_PRESETS[preset_name]
    pitch_factor = 2.0 ** (semi / 12.0)
    sr = _sample_rate(input_path)

    # asetrate=sr*pitch_factor raises pitch (and speed) by pitch_factor;
    # aresample returns to the source rate; atempo=1/pitch_factor restores the
    # original tempo while keeping the shifted pitch; atempo=tempo applies the
    # preset's tempo change. Each factor stays inside atempo's 0.5-2.0 range.
    af = (
        f"asetrate={sr * pitch_factor:.6f},"
        f"aresample={sr},"
        f"atempo={1.0 / pitch_factor:.6f},"
        f"atempo={tempo:.6f}"
    )

    rc, _out, err = _run(_FFMPEG + ["-i", input_path, "-af", af,
                                    "-c:a", "libmp3lame", "-b:a", "192k", output_path])
    if rc != 0:
        raise RuntimeError("copyr8 pass failed." + (f" ({err.strip()})" if err.strip() else ""))

    rc, _out, err = _run(_FFMPEG + ["-i", output_path, "-lavfi",
                                    "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                                    spectrogram_path])
    if rc != 0:
        raise RuntimeError("spectrogram render failed." + (f" ({err.strip()})" if err.strip() else ""))
    _brand_spectrogram(spectrogram_path)
    return _result_info(output_path)

# Auto-Fit (V1) fixed ladder: the previous version's UNDETECTABLE_LADDER. These
# are strong-ish uniform shifts, mildest first. A half-semitone flat plus a
# fifth faster is the verified near-identical setting: a human can still name the
# song easily while the matcher gets a different fingerprint. It only ever uses
# this one method -- a clean uniform pitch + tempo shift (no detune, no warble,
# no scrambling) -- so the output always sounds like the same song.
# Applied when NEITHER recognition service knows the original (control False).
# Every candidate then sweeps "clean" trivially -- the walk would stop at its
# mildest rung (-0.1 st / 1.05x) and ship an effectively unprotected copy.
# Instead the output gets the balanced preset as real protection.
UNVERIFIED_PROTECTION = COPYR8_PRESETS["balanced"]   # (+2.00 st, 1.07x)


def _protection_cost():
    pitch, tempo = UNVERIFIED_PROTECTION
    return round(abs(pitch) + 10.0 * abs(tempo - 1.0), 2)


DEFAULT_PITCH = -0.5
DEFAULT_TEMPO = 1.22
SPREAD_WINDOWS = 8
UNDETECTABLE_LADDER = [
    (DEFAULT_PITCH, DEFAULT_TEMPO),  # the verified near-identical setting
    (-0.5, 1.25),
    (-0.5, 1.28),
    (-1.0, 1.25),
    (-1.0, 1.30),
]

# Auto-Fit V2 walk bounds. The walk drops the KEY (pitch goes negative) and
# speeds the track up: measured on real runs, every setting that cleared was in
# that family. The key drops in 0.05 st steps from -0.10 to -0.50 ("as far as
# it goes" before the reply admits to speeding up); the speed runs +5% to +22%
# in 1% steps (+22% is the verified near-identical ceiling).
MILDEST_PITCH_START = -0.10
MILDEST_PITCH_FLOOR = -0.50
MILDEST_PITCH_STEP = 0.05
MILDEST_TEMPO_MIN = 1.05
MILDEST_TEMPO_MAX = 1.22
MILDEST_TEMPO_STEP = 0.01
# Tempo cap: the walk only accepts a setting past the cap once every slower
# split has failed -- it drops the key as far as it goes at/below the cap before
# speeding up further (that is what the /copyr8 reply's Speed note explains).
MILDEST_TEMPO_CAP = 1.081
# The one wobble candidate the walk itself probes (charged full perceptual
# cost, so a pure shift that sounds closer always wins): the mildest pure rung
# plus a 0.10 @ 4 Hz swell, exactly as the previous version's runs logged it.
MILDEST_WOBBLE = (0.10, 4.0)

# Shazam probe pace-keeping and rate-limit cool-down policy.
# Shazam rate-limits by device id and repeated hits return an empty body, so
# every POST is spaced out, and a throttle starts a growing cool-down window
# instead of aborting the fit.
SHA_PROBE_CLIP_SECONDS = 12.0     # Shazam only needs a short clip to recognise
SHA_PROBE_RETRIES = 3             # attempts per window before "inconclusive"
SHA_PROBE_TIMEOUT_SECONDS = 60
_SHA_BREATH_SECONDS = 6.0         # starting pause between Shazam POSTs
_SHA_BREATH_MIN = float(os.getenv("COPYR8_PROBE_BREATH_MIN", "2.5") or 2.5)
_SHA_RATE_LIMIT_BASE = 30.0       # first cool-down after a throttle
_SHA_RATE_LIMIT_MAX = 180.0       # cap on the cool-down
_SHA_RATE_LIMIT_EXPONENT = 2      # cool-down doubles each consecutive throttle

_last_post_time = time.time() - _SHA_BREATH_SECONDS
_rate_limit_until = 0.0
_consecutive_limits = 0
_breath_seconds = _SHA_BREATH_SECONDS
_ok_streak = 0

# One ShazamIO client + event loop for the whole process. Probes reuse the
# HTTPS connection instead of paying a fresh handshake per call.
_sha_loop = None
_sha_client = None
_sha_lock = threading.Lock()


def _shazam_api():
    """The shared (event loop, ShazamIO client) pair, created on first use."""
    global _sha_loop, _sha_client
    if _sha_loop is None:
        import asyncio  # noqa: PLC0415
        _sha_loop = asyncio.new_event_loop()
    if _sha_client is None:
        from shazamio import Shazam  # noqa: PLC0415
        _sha_client = Shazam()
    return _sha_loop, _sha_client


def _mark_rate_limited(progress=None):
    """Record a detected throttle and raise the global cool-down window."""
    global _rate_limit_until, _consecutive_limits, _last_post_time
    global _breath_seconds, _ok_streak
    _consecutive_limits += 1
    wait = min(
        _SHA_RATE_LIMIT_MAX,
        _SHA_RATE_LIMIT_BASE * (_SHA_RATE_LIMIT_EXPONENT ** (_consecutive_limits - 1)),
    )
    _rate_limit_until = time.time() + wait
    _last_post_time = time.time()  # don't double-wait for the base spacing
    _breath_seconds = _SHA_BREATH_SECONDS
    _ok_streak = 0
    if progress:
        progress(f"Shazam rate limit from the API -- pausing {wait:.0f}s for the "
                 "cool-down before testing again")


def _mark_ok():
    """Reset the back-off and ease the breath back while answers stay clean.

    The breath starts conservative and decays toward _SHA_BREATH_MIN only
    after a streak of usable answers; any rate-limit signal snaps it straight
    back. That is the speed-up that does not gamble with the API's limit.
    """
    global _consecutive_limits, _breath_seconds, _ok_streak
    _consecutive_limits = 0
    _ok_streak += 1
    if _ok_streak >= 3 and _breath_seconds > _SHA_BREATH_MIN:
        _breath_seconds = max(_SHA_BREATH_MIN, _breath_seconds - 0.5)


def pacing_status(now=None):
    """Live snapshot of the shared Shazam pace-keeper, for status displays.

    Read-only and thread-safe enough for a UI poll (plain float reads). The bot
    polls this while an auto-fit runs so Discord can show a live countdown of
    the rate-limit reset and the pause before the next probe, instead of a
    message that looks frozen for minutes during a cool-down.

    Returns {"rate_limited": bool, "rate_limit_remaining": s,
             "breath_remaining": s, "breath_seconds": s,
             "consecutive_limits": n}.
    """
    now = time.time() if now is None else now
    rl_remaining = max(0.0, _rate_limit_until - now)
    breath_remaining = max(0.0, _breath_seconds - (now - _last_post_time))
    return {
        "rate_limited": rl_remaining > 0.0,
        "rate_limit_remaining": rl_remaining,
        "breath_remaining": breath_remaining,
        "breath_seconds": _breath_seconds,
        "consecutive_limits": _consecutive_limits,
    }


def _throttle(progress=None):
    """Sleep past the rate-limit cool-down, then keep the base POST spacing."""
    global _last_post_time
    now = time.time()
    if _rate_limit_until > now:
        wait = _rate_limit_until - now
        if progress:
            progress(f"still waiting on Shazam's rate limit -- {wait:.0f}s of the "
                     "cool-down left")
        time.sleep(wait)
        now = time.time()
    elapsed = now - _last_post_time
    if elapsed < _breath_seconds:
        time.sleep(_breath_seconds - elapsed)
    _last_post_time = time.time()


def _window_points(duration, n=SPREAD_WINDOWS, clip=SHA_PROBE_CLIP_SECONDS):
    """Spread n probe windows evenly across the song, staying inside the file.

    A track is only "not recognised" if the recogniser fails on every part, so
    we probe many evenly-spaced spots rather than trusting one or two clips.
    With n ~= duration/clip the windows overlap slightly, which is what makes
    the V2 sweep gap-free: a sparse sample can step over the one passage the
    matcher still knows.
    """
    if duration <= clip:
        return [0.0]
    usable = duration - clip
    if n <= 1:
        return [round(usable * 0.5, 1)]
    # Skip the very start/end (often silence or a fadeout) and spread evenly.
    return [round((0.05 + 0.90 * (i / (n - 1))) * usable, 1) for i in range(n)]


def _duration_seconds(path):
    """Audio duration in seconds via ffprobe (probe windows are cut from it)."""
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    try:
        return float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def active_recognition_providers():
    providers = ["ShazamIO"]
    try:
        acoustid_backend.validate_setup()
        providers.append("AcoustID")
    except Exception:
        pass
    if songrec_backend.is_available():
        providers.append("SongRec")
    return providers


def _shazam_match_count(path, progress=None):
    """One ShazamIO tag call -> how many matches Shazam reports for this clip.

    Returns an int match count, or None when the answer is not usable: a
    network/timeout failure, a non-answer, or a response with no `matches`
    LIST -- which is Shazam's classic throttle signature and must NOT be read
    as "0 matches" (that is what would let an auto-fit accept a shift Shazam
    can still identify). Retries within the pace-keeper's cool-down policy;
    None is only returned after every attempt failed.
    """
    import asyncio  # noqa: PLC0415
    try:
        from shazamio import Shazam  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "The auto-fit needs the ShazamIO package (pip install shazamio). "
            "(%s)" % e)

    async def _go(shazam):
        return await asyncio.wait_for(
            shazam.recognize(path), timeout=SHA_PROBE_TIMEOUT_SECONDS)

    for _attempt in range(SHA_PROBE_RETRIES):
        _throttle(progress)
        try:
            loop, shazam = _shazam_api()
            with _sha_lock:
                data = loop.run_until_complete(_go(shazam))
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001 -- network-level failure, retry below
            _mark_rate_limited(progress)
            continue
        if not isinstance(data, dict):
            _mark_rate_limited(progress)
            continue
        matches = data.get("matches")
        if not isinstance(matches, list):
            # A 200 whose JSON is parseable but has NO matches list is a
            # throttle signature, and must NOT be treated as "0 matches".
            _mark_rate_limited(progress)
            continue
        _mark_ok()
        return len(matches)
    return None


def _probe_sweep(path, windows, progress=None, *, include_acoustid=True, include_songrec=True):
    """(ok, match counts) for one candidate, probing every window.

    Counts are Shazam window results followed by AcoustID and SongRec file
    results when preceding checks pass (unless disabled for controls).
    True only if EVERY check gives a definite result of 0 matches.
    Any None (inconclusive) or >0 makes the whole test fail, because a failed
    request is not proof of a clean match -- that short-sightedness is what let
    preset-based fits slip through. A window that still identifies the track
    (or comes back inconclusive) stops the sweep early: the setting is already
    rejected and the rest of the windows would only burn API calls.
    """
    counts = []
    for start in windows:
        clip = f"{path}.probe.wav"
        rc, _out, err = _run(_FFMPEG + ["-i", path, "-ss", f"{start:.3f}",
                                        "-t", f"{SHA_PROBE_CLIP_SECONDS:.3f}",
                                        "-ac", "1", clip])
        if rc != 0:
            raise RuntimeError("probe clip render failed."
                               + (f" ({err.strip()})" if err.strip() else ""))
        try:
            count = _shazam_match_count(clip, progress=progress)
        finally:
            try:
                os.remove(clip)
            except OSError:
                pass
        counts.append(count)
        if count is None or count > 0:
            break
    shazam_ok = bool(counts) and all(c == 0 for c in counts)
    if shazam_ok and include_acoustid and acoustid_backend.is_available():
        counts.append(acoustid_backend.match_count(path, progress=progress))
    acoustid_ok = bool(counts) and all(c == 0 for c in counts)
    if acoustid_ok and include_songrec and songrec_backend.is_available():
        counts.append(songrec_backend.match_count(path, progress=progress))
    ok = bool(counts) and all(c == 0 for c in counts)
    return ok, counts


def _source_clip_seconds(tempo_factor):
    """Source seconds to cut so the clip is SHA_PROBE_CLIP_SECONDS after atempo."""
    try:
        factor = float(tempo_factor)
    except (TypeError, ValueError):
        factor = 1.0
    return SHA_PROBE_CLIP_SECONDS * max(1.0, factor)


def _probe_sweep_shifted(input_path, windows, pitch_st, tempo_factor,
                         depth=0.0, rate=0.0, progress=None,
                         *, include_acoustid=True, include_songrec=True):
    """(ok, counts) with cheap Shazam windows before full AcoustID / SongRec renders.

    Each probe window is cut straight from the input with the candidate's
    filter chain applied -- the same audio the full render would produce, at
    a fraction of the cost. A rejected rung usually fails on its first window,
    so a walk step costs one tiny clip render instead of a full-song render.
    Only candidates that clear Shazam need a full render for AcoustID and SongRec.
    """
    counts = []
    workdir = os.path.dirname(input_path) or "."
    clip = os.path.join(workdir, "shift_probe.wav")
    sr = _sample_rate(input_path)
    pitch_factor = 2.0 ** (pitch_st / 12.0)
    chain = (
        f"asetrate={sr * pitch_factor:.6f},"
        f"aresample={sr},"
        f"atempo={1.0 / pitch_factor:.6f},"
        f"atempo={tempo_factor:.6f}"
    )
    if depth:
        chain += f",tremolo=f={rate:g}:d={depth}"
    # The pitch stage is duration-neutral (asetrate*p then atempo 1/p), but
    # atempo=tempo_factor shortens the clip: a 12s source cut at 1.22x came
    # out as ~9.8s, below the length Shazam's probe was sized for. Cut
    # SHA_PROBE_CLIP_SECONDS * tempo_factor of source so every probe clip is
    # still >= SHA_PROBE_CLIP_SECONDS long after the chain.
    src_clip = _source_clip_seconds(tempo_factor)
    try:
        duration = _duration_seconds(input_path)
    except (RuntimeError, ValueError):
        duration = 0.0  # unknown length: cut as asked, ffmpeg stops at EOF
    for start in windows:
        if duration > src_clip:
            # Keep the (longer) source cut inside the file so the tail window
            # is not truncated back under the probe length.
            start = min(start, round(duration - src_clip, 3))
        # -ss/-t MUST be input options (before -i). With them after -i, ffmpeg
        # runs the atempo chain over the stream first and then seeks in the
        # FILTERED timeline, so a probe at `start` sampled source
        # start*tempo_factor onward -- measured: the window at 252s of a 1.10x
        # rung sampled 277-290s, tail windows ran off the end of the song, and
        # the passages the sweep believed it cleared were never tested at all.
        # Input seeking cuts the source at `start` first, then applies the
        # chain, so the clip is exactly the audio the full render would
        # produce at that position.
        rc, _out, err = _run(_FFMPEG + ["-ss", f"{start:.3f}",
                                        "-t", f"{src_clip:.3f}",
                                        "-i", input_path,
                                        "-af", chain, "-ac", "1", clip])
        if rc != 0:
            raise RuntimeError("probe clip render failed."
                               + (f" ({err.strip()})" if err.strip() else ""))
        try:
            count = _shazam_match_count(clip, progress=progress)
        finally:
            try:
                os.remove(clip)
            except OSError:
                pass
        counts.append(count)
        if count is None or count > 0:
            break
    if counts and all(c == 0 for c in counts):
        candidate = os.path.join(workdir, "acoustid_candidate.wav")
        try:
            rc, _out, err = _run(_FFMPEG + ["-i", input_path, "-af", chain,
                                           "-ac", "1", candidate])
            if rc != 0:
                raise RuntimeError("Candidate render failed.")
            if include_acoustid and acoustid_backend.is_available():
                counts.append(acoustid_backend.match_count(candidate, progress=progress))
            if counts and all(c == 0 for c in counts) and include_songrec and songrec_backend.is_available():
                counts.append(songrec_backend.match_count(candidate, progress=progress))
        finally:
            try:
                os.remove(candidate)
            except OSError:
                pass
    ok = bool(counts) and all(c == 0 for c in counts)
    return ok, counts


def _gapfree_windows(path):
    """Probe windows covering the whole song gap-free (overlapping 12s clips).

    The starts span [0, duration - clip] with ~10% overlap between clips, so
    EVERY passage of the song -- the intro and the fade-out included -- is in
    some probe window. The 5%/95% inset of the sparse spread sampler must not
    be reused here: it left the head and tail un-probed (14.4s of blind zone
    at each end of a five-minute track), and a match sitting there produced a
    false "clean" -- the exact false acceptance this sweep exists to stop.

    An unmeasurable file (ffprobe can choke on an internally generated file
    even when ffmpeg reads it fine, e.g. after a metadata remux) falls back
    to a single window at the start: the sweep still runs, just without the
    spread, instead of failing the whole search over a probe convenience.
    """
    try:
        duration = _duration_seconds(path)
    except (RuntimeError, ValueError):
        duration = 0.0
    clip = SHA_PROBE_CLIP_SECONDS
    if duration <= clip:
        return [0.0]
    # n windows of `clip` seconds overlapping by ~10%: n-1 steps of
    # (duration - clip) / (n - 1) < clip span the file end to end.
    n = max(2, int(math.ceil(duration / (0.9 * clip))))
    return [round(i * (duration - clip) / (n - 1), 1) for i in range(n)]


def _control_check(original_path, progress=None):
    """True when AT LEAST ONE provider identifies the original.

    A clean sweep of a shifted candidate only means something if the
    recognition databases know the original track: then a miss on the shifted
    copy is real evidence. One provider knowing the track is enough evidence --
    requiring all to know it mislabeled every song that only Shazam (or AcoustID
    or SongRec) catalogues as "unverified" even though the sweep was checked
    against a service that really does know it.

    True  = Shazam, AcoustID, OR SongRec definitively matched the original.
    False = ALL providers answered definitively with 0 matches (the track is
            not in any database, so a clean sweep proves nothing).
    None  = no provider matched and at least one answer was inconclusive.
    Never call a network failure a clean miss.
    """
    windows = _gapfree_windows(original_path)
    _ok, counts = _probe_sweep(original_path, windows, progress=progress,
                               include_acoustid=False, include_songrec=False)
    shazam_known = any(c is not None and c > 0 for c in counts)
    shazam_clear = bool(counts) and all(c == 0 for c in counts)
    if shazam_known:
        if progress:
            progress("Original control: Shazam matched -- the recogniser knows "
                     "the original.")
        return True
    acoustic = None
    acoust_clear = True
    if acoustid_backend.is_available():
        acoustic = acoustid_backend.match_count(original_path, progress=progress)
        if acoustic is not None and acoustic > 0:
            if progress:
                progress("Original control: Shazam "
                         + ("no match" if shazam_clear else "inconclusive")
                         + "; AcoustID matched -- the recogniser knows the original.")
            return True
        acoust_clear = acoustic == 0

    songrec_count = None
    songrec_clear = True
    if songrec_backend.is_available():
        songrec_count = songrec_backend.match_count(original_path, progress=progress)
        if songrec_count is not None and songrec_count > 0:
            if progress:
                progress("Original control: Shazam "
                         + ("no match" if shazam_clear else "inconclusive")
                         + ", AcoustID "
                         + ("no match" if acoust_clear else "inconclusive")
                         + "; SongRec matched -- the recogniser knows the original.")
            return True
        songrec_clear = songrec_count == 0

    if shazam_clear and acoust_clear and songrec_clear:
        if progress:
            progress("Original control: "
                     + " + ".join(active_recognition_providers())
                     + " no match -- no service knows the original.")
        return False
    if progress:
        progress("Original control: inconclusive (a service did not answer).")
    return None


def _render_overlay(wav_path, out_path, depth, rate):
    """Layer a tremolo wobble on a rendered candidate."""
    rc, _out, err = _run(_FFMPEG + ["-i", wav_path, "-af",
                                    f"tremolo=f={rate:g}:d={depth}", out_path])
    return rc == 0, err


def _render_shifted_wav(input_path, work_wav, pitch_st, tempo_factor):
    """Render a pitch/tempo shifted WAV using ffmpeg.

    Uses asetrate + aresample + atempo to shift pitch independently of tempo.
    Returns True on success, raises on failure.
    """
    pitch_factor = 2.0 ** (pitch_st / 12.0)
    sr = _sample_rate(input_path)
    af = (
        f"asetrate={sr * pitch_factor:.6f},"
        f"aresample={sr},"
        f"atempo={1.0 / pitch_factor:.6f},"
        f"atempo={tempo_factor:.6f}"
    )
    rc, _out, err = _run(_FFMPEG + ["-i", input_path, "-af", af, work_wav])
    if rc != 0:
        raise RuntimeError("auto-fit render failed." + (f" ({err.strip()})" if err.strip() else ""))
    return True


def find_undetectable(input_path, output_path, progress=None):
    """Closed-loop auto-fit: the mildest uniform shift the recogniser can't ID.

    The previous version's "Auto-Fit": it probes a candidate shift at points
    spread across the whole song, renders a candidate only when the shift is
    clean at every probed point, and escalates to
    a stronger (but still uniform pitch/tempo) shift until the real recogniser
    (ShazamIO, AcoustID, and SongRec) can't identify any part. It only ever uses that one
    method, so the output always sounds like the same song, just a touch faster
    and/or a half-step flat.

    Nothing is written to `output_path` until a shift clears every window; the
    accepted result is moved there at the very end. `progress(msg)` is called
    between steps (from this worker thread).

    Returns {"pitch", "tempo", "counts", "windows", "info", "verified"}.
    counts holds Shazam window results plus the AcoustID file result, or None
    when the search fell back to
    the strongest shift as a best effort. verified: True = at least one service
    (Shazam or AcoustID) identifies the original, so the clean sweep is real;
    False = neither service knows the original, so the balanced protection
    shift (UNVERIFIED_PROTECTION, +2.00 st / 1.07x) is applied instead of the
    trivially clean rung; None = an original control was inconclusive.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")
    acoustid_backend.validate_setup(required=False)
    songrec_backend.validate_setup(required=False)
    try:
        import shazamio  # noqa: F401,PLC0415
    except ImportError as e:
        raise RuntimeError(
            "The auto-fit needs the ShazamIO package (pip install shazamio). "
            "(%s)" % e)

    winner = None      # (temp_path, pitch, tempo, counts, windows)
    got_answer = False  # did the recogniser ever return a definite answer?

    workdir = os.path.dirname(output_path) or "."
    cand_path = os.path.join(workdir, "autofit_candidate.wav")
    try:
        for pitch, tempo in UNDETECTABLE_LADDER:
            pct = (tempo - 1.0) * 100.0
            if progress:
                progress("testing uniform shift %+.1f st / +%.0f%%..." % (
                    pitch, pct))

            # Render the candidate in full and confirm every window on the
            # FINISHED file, so the acceptance test matches what the user hears.
            windows = _window_points(_duration_seconds(input_path))
            ok, counts = _probe_sweep_shifted(input_path, windows, pitch, tempo,
                                              0.0, 0.0, progress=progress)
            if counts is not None and None not in counts:
                got_answer = True
                if ok:
                    winner = (pitch, tempo, counts, windows)
                    break
            # Inconclusive (throttle/network): escalate and keep trying;
            # we never accept a shift we couldn't verify.

        verified = None
        if winner:
            control = _control_check(input_path, progress=progress)
            if control is False:
                # Neither database knows the track, so the clean sweep proves
                # nothing. Ship real protection instead of the trivial rung.
                p_pitch, p_tempo = UNVERIFIED_PROTECTION
                if progress:
                    progress("control failed: neither recogniser knows the "
                             "original -- applying the balanced protection shift "
                             f"({p_pitch:+.2f} st / {p_tempo:.2f}x), unverified")
                winner = (p_pitch, p_tempo, winner[2], winner[3])
                verified = False
            elif control is True:
                if progress:
                    progress("control ok: a recogniser knows the original, "
                             "so a clean sweep of this setting is real")
                verified = True
            # else: control inconclusive -- the sweep is clean but couldn't be
            # corroborated; keep the winner and report it as not attempted.

        if winner:
            pitch, tempo, counts, windows = winner
            _render_shifted_wav(input_path, output_path, pitch, tempo)
            return {"pitch": pitch, "tempo": tempo, "counts": counts,
                    "windows": windows, "verified": verified,
                    "info": {"pitch": pitch, "tempo": tempo,
                             "level_gain_db": 0.0}}
    finally:
        try:
            os.remove(cand_path)
        except OSError:
            pass

    # If nothing on the ladder cleared, fall back to the strongest shift -- but
    # only if the recogniser actually responded during the search. If it never
    # returned a definitive answer (even after back-off retries), don't write a
    # file we have no evidence about; tell the caller plainly instead.
    if not got_answer:
        raise RuntimeError(
            "The recogniser never returned a usable answer even after pausing "
            "and retrying (a recognition API is unavailable / the network can't "
            "reach it). No file was written. Try again in a few minutes.")
    pitch, tempo = UNDETECTABLE_LADDER[-1]
    if progress:
        progress("strongest shift still not fully clean; using it anyway")
    _render_shifted_wav(input_path, output_path, pitch, tempo)
    windows = _window_points(_duration_seconds(output_path))
    return {"pitch": pitch, "tempo": tempo, "counts": None,
            "windows": windows, "verified": None,
            "info": {"pitch": pitch, "tempo": tempo, "level_gain_db": 0.0}}


def _mildest_rungs():
    """The Auto-Fit V2 walk: cost-ordered settings, closest-sounding first.

    Uniform shifts from the key-drop / speed-up family (pitch <= 0, tempo >= 1),
    in two tiers: everything at/below the tempo cap first (dropping the key as
    far as it goes before speeding up further), then the faster splits. Cost is
    the deviation from the original in semitone-equivalents
    (|pitch| + 10 * |tempo - 1|; lower = sounds closer).

    Rungs are 0.05 apart on the cost axis: the settings that clear do not form
    a smooth boundary but a COMB -- clean and failing settings alternate
    between neighbouring costs -- so a coarse ladder steps over working
    settings and answers with a stronger shift than the track needed. One rung
    per 0.05 cost bucket is kept (the mildest-sounding one wins ties).
    """
    rungs = []
    pitches = [round(MILDEST_PITCH_START - MILDEST_PITCH_STEP * i, 2)
               for i in range(int(round(
                   (MILDEST_PITCH_START - MILDEST_PITCH_FLOOR) / MILDEST_PITCH_STEP)) + 1)]
    tempos = [round(MILDEST_TEMPO_MIN + MILDEST_TEMPO_STEP * i, 2)
              for i in range(int(round(
                  (MILDEST_TEMPO_MAX - MILDEST_TEMPO_MIN) / MILDEST_TEMPO_STEP)) + 1)]
    for p in pitches:
        for t in tempos:
            cost = round(abs(p) + 10.0 * abs(t - 1.0), 2)
            tier = 0 if t <= MILDEST_TEMPO_CAP else 1
            rungs.append((tier, cost, False, abs(p), p, t, 0.0, 0.0))
    # The one wobble probe of the walk (charged its full perceptual cost), on
    # the mildest pure rung -- inserted with the pure settings and competing on
    # cost alone, so a pure shift that sounds closer always wins.
    depth, rate = MILDEST_WOBBLE
    base_p, base_t = pitches[0], tempos[0]
    wobble_cost = round(abs(base_p) + 10.0 * abs(base_t - 1.0) + depth * 2.0, 2)
    rungs.append((0, wobble_cost, True, abs(base_p), base_p, base_t, depth, rate))

    rungs.sort(key=lambda r: (r[0], r[1], not r[2], r[3]))
    # Comb: one rung per 0.05 cost bucket (rounded), first wins.
    seen = set()
    out = []
    for tier, cost, is_wobble, _ap, p, t, depth, rate in rungs:
        bucket = round(round(cost / 0.05) * 0.05, 2)
        if bucket in seen:
            continue
        seen.add(bucket)
        out.append((bucket, p, t, depth, rate))
    return out


def find_mildest(input_path, output_path, progress=None,
                 use_tremolo=True, overlay_tremolo=None,
                 rungs=None, control_path=None):
    """Auto-Fit V2: search for the CLOSEST-SOUNDING shift the recogniser can't ID.

    The previous version's "Auto-Fit V2": it starts at almost no change and
    walks cost-ordered rungs from the mildest upward, stopping at the first one
    ShazamIO misses on EVERY probe window and AcoustID misses on the file. The
    result sounds as close to the original as the track allows, instead of as
    strong as the ladder happens to be.

    Why the search is built the way it is (measured on real runs against the
    real recogniser, not assumed):
      * Rungs are 0.05 apart on the cost axis -- the settings that clear form a
        COMB, so a coarse ladder steps over working settings.
      * The walk starts near zero change because where a track's boundary sits
        is a property of the track (measured at cost 1.10 on one and 2.18 on
        another). Starting higher made the walk blind to the milder answer.
      * Probes sweep the whole song gap-free. A sparse sample can step over the
        one passage the matcher still knows, which produced false acceptances.
      * The tempo cap: only once every slower split has failed does the walk
        accept a setting past the cap -- the key is dropped as far as it goes
        before speeding up further.
      * A wobble probe is searched alongside the pure settings but charged its
        full perceptual cost, so a pure shift that sounds closer always wins.
      * Tremolo is layered on TOP of the accepted shift afterwards (gentlest
        first, `overlay_tremolo`) and the layered file is re-probed before it is
        kept -- whether a wobble breaks a working setting is a property of the
        track, so it is verified rather than assumed.

    Returns {"pitch", "tempo", "cost", "counts", "overlay_applied",
             "overlay_reason", "verified", "info"} where clean is counts not
    None, `cost` is the deviation from the original in semitone-equivalents
    (lower = sounds closer; it includes the wobble when one was applied), and
    `verified`: True = confirmed against the recogniser, False = neither
    service knows the original (the balanced protection shift is applied and
    the tremolo overlay pass is skipped), None = not attempted/inconclusive.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")
    acoustid_backend.validate_setup(required=False)
    songrec_backend.validate_setup(required=False)
    try:
        import shazamio  # noqa: F401,PLC0415
    except ImportError as e:
        raise RuntimeError(
            "The auto-fit needs the ShazamIO package (pip install shazamio). "
            "(%s)" % e)

    if rungs is None:
        rungs = _mildest_rungs()
    if progress:
        progress(f"searching {len(rungs)} settings, closest-sounding first...")

    workdir = os.path.dirname(output_path) or "."
    work_wav = os.path.join(workdir, "autofit_v2_candidate.wav")
    winner = None      # (cost, pitch, tempo, counts, windows, depth, rate)
    got_answer = False

    try:
        for cost, pitch, tempo, depth, rate in rungs:
            if progress:
                pct = (tempo - 1.0) * 100.0
                if depth:
                    progress("testing %+.1f st / +%.1f%% + tremolo "
                             "%.2f@%gHz..." % (pitch, pct, depth, rate))
                else:
                    progress("testing %+.1f st / +%.1f%%..." % (pitch, pct))

            # Gap-free whole-song probe sweep on the candidate -- cut straight
            # from the input with the shift (and wobble) filters applied, so a
            # rejected rung costs one clip render, not a full-song render.
            windows = _gapfree_windows(input_path)
            ok, counts = _probe_sweep_shifted(input_path, windows, pitch, tempo,
                                              depth, rate, progress=progress)
            if counts is not None and None not in counts:
                got_answer = True
                if ok:
                    winner = (cost, pitch, tempo, counts, windows, depth, rate)
                    break
            # Inconclusive: never accept a shift we couldn't verify; keep walking.

        if winner:
            control = _control_check(control_path or input_path, progress=progress)
            if control is False:
                # Neither database knows the track: every rung sweeps clean
                # trivially, so the mildest rung would ship an effectively
                # unprotected copy. Apply the balanced protection shift.
                p_pitch, p_tempo = UNVERIFIED_PROTECTION
                if progress:
                    progress("control failed: neither recogniser knows the "
                             "original -- applying the balanced protection shift "
                             f"({p_pitch:+.2f} st / {p_tempo:.2f}x), unverified")
                winner = (_protection_cost(), p_pitch, p_tempo, winner[3],
                          winner[4], 0.0, 0.0)
                verified = False
            elif control is True:
                if progress:
                    progress("control ok: a recogniser knows the original, "
                             "so a clean sweep of this setting is real")
                verified = True
            else:
                # Control inconclusive -- the sweep is clean but couldn't be
                # corroborated; keep the winner, report it as not attempted.
                verified = None

        if winner:
            cost, pitch, tempo, counts, windows, depth, rate = winner
            # Re-render the accepted setting. The search's own wobble probe can
            # win (its pure base failed but the wobbled file cleared); in that
            # case the wobble is part of the accepted setting and is kept.
            _render_shifted_wav(input_path, work_wav, pitch, tempo)
            if depth:
                ok_render, err = _render_overlay(work_wav, work_wav + ".base.wav",
                                                 depth, rate)
                if ok_render:
                    os.replace(work_wav + ".base.wav", work_wav)
        else:
            counts = None
            cost, pitch, tempo = rungs[-1][0], rungs[-1][1], rungs[-1][2]
            depth, rate = 0.0, 0.0
            windows = None
            verified = None
            if not got_answer:
                raise RuntimeError(
                    "The recogniser never returned a usable answer even after "
                    "pausing and retrying (a recognition API is unavailable / the "
                    "network can't reach it). No file was written. Try again "
                    "in a few minutes.")
            if progress:
                progress("no rung cleared every probe; using the strongest "
                         "shift as a best effort")
            _render_shifted_wav(input_path, work_wav, pitch, tempo)

        # Tremolo overlay on top of the accepted shift, gentlest first,
        # re-verified against the recogniser before it is kept. Only pure
        # winners get the overlay pass -- a wobble probe that won the search
        # already carries its own verified wobble.
        overlay_applied = bool(depth)
        overlay_reason = None
        if counts is not None and use_tremolo and not depth and verified is not False:
            for odepth, orate in sorted(overlay_tremolo or AUTOFIT_V2_TREMOLO,
                                        key=lambda x: x[0]):
                if progress:
                    pct = (tempo - 1.0) * 100.0
                    progress("adding tremolo %+.1f st / +%.1f%% + tremolo "
                             "%.2f@%gHz on top of the accepted setting..." % (
                                 pitch, pct, odepth, orate))
                trem_wav = work_wav + ".trem.wav"
                ok_render, err = _render_overlay(work_wav, trem_wav, odepth, orate)
                if not ok_render:
                    overlay_reason = ("tremolo filter not available, keeping "
                                      "the pure shift")
                    break
                windows = _gapfree_windows(trem_wav)
                ok, ocounts = _probe_sweep(trem_wav, windows, progress=progress)
                if not ok:
                    try:
                        os.remove(trem_wav)
                    except OSError:
                        pass
                    overlay_reason = ("the tremolo version was still "
                                      "recognised -- keeping the pure shift")
                    continue
                verified_overlay = _control_check(control_path or input_path, progress=progress)
                if verified_overlay is not True:
                    try:
                        os.remove(trem_wav)
                    except OSError:
                        pass
                    overlay_reason = ("the tremolo check couldn't be "
                                      "corroborated -- keeping the pure shift")
                    continue
                if progress:
                    progress(f"tremolo verified: clean on all {len(windows)} "
                             f"windows")
                os.replace(trem_wav, work_wav)
                overlay_applied = True
                depth, rate = odepth, orate
                cost += odepth * 2.0
                counts = ocounts
                break

        shutil.move(work_wav, output_path)
        return {"pitch": pitch, "tempo": tempo, "cost": cost,
                "counts": counts,
                "tremolo_depth": depth,
                "tremolo_rate": rate,
                "overlay_applied": overlay_applied,
                "overlay_reason": overlay_reason,
                "verified": verified,
                "info": {"pitch": pitch, "tempo": tempo,
                         "level_gain_db": 0.0}}
    finally:
        for leftover in (work_wav, work_wav + ".trem.wav", work_wav + ".base.wav"):
            try:
                os.remove(leftover)
            except OSError:
                pass


def copyr8_autofit(input_path, output_path, spectrogram_path, progress=None):
    """Closed-loop auto-fit: the mildest uniform shift the recogniser can't ID.

    The CopyR8 Auto-Fit wrapper: it delegates the search to
    find_undetectable (ShazamIO + AcoustID + SongRec), then re-encodes the accepted WAV
    to MP3 at the source's own loudness and renders the spectrogram.

    `progress(msg)` is called between steps (from this worker thread) so the bot
    can show what it's trying. Returns:
        {"pitch", "tempo", "clean", "counts", "verified", "info", "loudness"}
    where `clean` is True only when every probe window confirmed a clean result
    (counts is None when the search fell back to the strongest shift as a best
    effort because nothing on the ladder cleared).
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")

    work_wav = os.path.join(os.path.dirname(output_path) or ".", "autofit_work.wav")
    if progress:
        progress("starting the auto-fit search (it checks ShazamIO + AcoustID + SongRec and may take a while)")

    try:
        result = find_undetectable(input_path, work_wav, progress=progress)
    except RuntimeError as e:
        raise RuntimeError(str(e))

    pitch = result["pitch"]
    tempo = result["tempo"]
    counts = result.get("counts")
    clean = counts is not None
    verified = result.get("verified")

    # The engine writes a WAV; re-encode to MP3 so the Discord attachment stays
    # small enough to send -- at the source's own loudness, so the copy is not
    # audibly quieter than the file it came from.
    loudness = _match_loudness(work_wav, output_path, input_path, progress=progress)
    rc, _out, err = _run(_FFMPEG + ["-i", output_path, "-lavfi",
                                    "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                                    spectrogram_path])
    if rc != 0:
        raise RuntimeError("spectrogram render failed." + (f" ({err.strip()})" if err.strip() else ""))
    _brand_spectrogram(spectrogram_path)

    if progress:
        progress(f"done: used {pitch:+.1f} st / tempo {tempo:.2f}x")
    return {
        "pitch": pitch,
        "tempo": tempo,
        "clean": clean,
        "counts": counts,
        "verified": verified,
        "loudness": loudness,
        "info": _result_info(output_path, progress=progress, loudness=loudness),
    }


def copyr8_autofit_v2(input_path, output_path, spectrogram_path, progress=None):
    """Auto-Fit V2: search for the CLOSEST-SOUNDING shift the recogniser can't ID.

    The CopyR8 Auto-Fit V2 wrapper: it delegates the search to
    find_mildest (ShazamIO + AcoustID + SongRec), then re-encodes the accepted WAV to MP3
    at the source's own loudness and renders the spectrogram.

    Slower than the fixed-ladder Auto-Fit, because it must rule out the milder
    settings first: roughly ten to fifteen minutes on a six-minute track, plus
    the tremolo verification pass.

    `progress(msg)` is called between steps (from this worker thread). Returns:
        {"pitch", "tempo", "cost", "clean", "counts", "info",
         "tremolo_depth", "tremolo_rate", "tremolo_applied", "tremolo_reason",
         "level_gain_db", "loudness", "verified"}
    where `clean` is True only when every probe window confirmed a clean result,
    and `cost` is the deviation from the original in semitone-equivalents (lower
    = sounds closer; it includes the wobble when one was applied). `counts` is
    None when the search fell back to the strongest shift as a best effort
    because no rung cleared. `tremolo_applied` says whether the wobble made it
    onto the result; when it is False `tremolo_reason` says why. `level_gain_db`
    is the correction the engine applied to keep the output as loud as the input.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")

    work_wav = os.path.join(os.path.dirname(output_path) or ".", "autofit_v2_work.wav")
    if progress:
        progress("starting the Auto-Fit V2 search (checks ShazamIO + AcoustID + SongRec; several "
                 "minutes on a long track)")

    try:
        result = find_mildest(input_path, work_wav, progress=progress,
                              use_tremolo=True,
                              overlay_tremolo=AUTOFIT_V2_TREMOLO)
    except RuntimeError as e:
        raise RuntimeError(str(e))

    pitch = result["pitch"]
    tempo = result["tempo"]
    counts = result.get("counts")
    clean = counts is not None
    # "Clean" means the recogniser failed on every probe window -- but only a
    # corroborated clean is real evidence. The engine checks its own sweep
    # against the untouched original, because a soft-throttled server answers
    # 200 with an empty match list, which reads exactly like a genuine miss and
    # is how an accepted setting once turned out to still be identifiable.
    # True = confirmed, False = the recogniser doesn't know the track (so the
    # check proves nothing either way), None = not attempted.
    verified = result.get("verified")
    cost = result.get("cost")
    tremolo_depth = result.get("tremolo_depth", 0.0) or 0.0
    tremolo_rate = result.get("tremolo_rate", 5.0) or 5.0
    tremolo_applied = bool(result.get("overlay_applied"))
    # How much level had to be put back to reach the source's loudness: the
    # vocoder chain and the wobble both cost some, and the engine corrects for it
    # so a processed file is never quieter than the one that went in.
    level_db = (result.get("info") or {}).get("level_gain_db")

    # The engine writes a WAV; re-encode to MP3 so the Discord attachment stays
    # small enough to send -- at the source's own loudness, so the copy is not
    # audibly quieter than the file it came from.
    loudness = _match_loudness(work_wav, output_path, input_path, progress=progress)
    rc, _out, err = _run(_FFMPEG + ["-i", output_path, "-lavfi",
                                    "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                                    spectrogram_path])
    if rc != 0:
        raise RuntimeError("spectrogram render failed." + (f" ({err.strip()})" if err.strip() else ""))
    _brand_spectrogram(spectrogram_path)

    if progress:
        progress(f"done: used {pitch:+.1f} st / tempo {tempo:.2f}x"
                 + (f" + tremolo {tremolo_depth:.2f}@{tremolo_rate:g}Hz"
                    if tremolo_applied else "")
                 + (f", level {loudness['out_lufs']:.1f} LUFS vs source "
                    f"{loudness['source_lufs']:.1f}"
                    if loudness.get("out_lufs") is not None
                    and loudness.get("source_lufs") is not None else "")
                 + (""
                    if verified is None else
                    ", confirmed against the recogniser" if verified else
                    ", unverified (neither service knows the original; balanced "
                    "protection applied)"))
    return {
        "pitch": pitch,
        "tempo": tempo,
        "cost": cost,
        "clean": clean,
        "counts": counts,
        "tremolo_depth": tremolo_depth,
        "tremolo_rate": tremolo_rate,
        "tremolo_applied": tremolo_applied,
        "tremolo_reason": result.get("overlay_reason"),
        "verified": verified,
        "level_gain_db": level_db,
        "loudness": loudness,
        "info": _result_info(output_path, progress=progress, loudness=loudness),
    }


def _v3_adjust_rungs():
    """V3's adjustment stream: the preset line first, the proven pool after.

    The four presets sit on one strength line (key up a little, a touch
    faster). V3 walks that line in fine adjustments, mildest first -- the four
    preset settings themselves are stops along the way -- and tests each
    attempt against the API before trying the next. Only if the whole line is
    still recognised does the stream continue into the settings both
    auto-fits use, so V3 never reaches for those first but the run always
    ends at "no recognition".
    """
    line = []
    for i in range(1, 19):
        pitch = round(0.25 * i, 2)               # +0.25 .. +4.50 st
        tempo = round(1.0 + 0.04 * pitch, 2)      # +1% .. +18%
        line.append((pitch, tempo))
    for preset in ("subtle", "balanced", "stealth", "aggressive"):
        line.append(COPYR8_PRESETS[preset])
    seen, out = set(), []
    for pitch, tempo in sorted(line, key=lambda x: (abs(x[0]) + 10.0 * abs(x[1] - 1.0), x[0])):
        key = (pitch, tempo)
        if key in seen:
            continue
        seen.add(key)
        cost = round(abs(pitch) + 10.0 * abs(tempo - 1.0), 2)
        out.append((cost, pitch, tempo, 0.0, 0.0))
    return out + _mixed_v3_rungs()


def _mixed_v3_rungs():
    """V3's candidate pool: Auto-Fit's fixed ladder mixed into V2's walk.

    Every setting the fixed ladder uses and every setting the closest-sounding
    walk uses, one cost-ordered stream, deduped by setting. The ladder's strong
    fallbacks extend the tail past the walk's range, so a track that outlasts
    both playbooks still gets the strongest known shift as a best effort. The
    wobble probe rides along exactly where V2 puts it.
    """
    mixed = list(_mildest_rungs())
    for pitch, tempo in UNDETECTABLE_LADDER:
        mixed.append((round(abs(pitch) + 10.0 * abs(tempo - 1.0), 2),
                      pitch, tempo, 0.0, 0.0))
    seen, out = set(), []
    for cost, pitch, tempo, depth, rate in sorted(
            mixed, key=lambda r: (r[0], r[3] > 0, abs(r[1]))):
        key = (pitch, tempo, depth, rate)
        if key in seen:
            continue
        seen.add(key)
        out.append((cost, pitch, tempo, depth, rate))
    return out


def copyr8_autofit_v3(input_path, output_path, spectrogram_path, progress=None,
                      macro_fn=None):
    """Auto-Fit V3: the macro chain plus a mixed closed-loop search.

    Stage 1 runs the configured macro chain (when the caller provides a macro
    runner), so the effect chain is applied exactly as authored. Stage 2 probes
    that base with the ShazamIO + AcoustID + SongRec checks and ships it untouched when it is
    already unreadable AND a service still knows the original -- the closest
    result there is. Stage 2 never ships the unshifted original: it is skipped
    when no macro output exists, and a clean-but-unverified base gets the
    balanced protection shift (+2.00 st / 1.07x). Stage 3 otherwise adds
    preset adjustments on top of the base (the four presets' strength line,
    fine-stepped, including a wobble probe) on top
    of the base, mildest first, and keeps trying until the recogniser can't
    detect it: the four presets' own strength line, adjusted step by step and
    tested against the API after every attempt, escalating into the settings
    both auto-fits use only if the whole line is still recognised.

    The acceptance rules are Auto-Fit V2's exactly: a clean sweep corroborated
    against the untouched original (True/False/None), the balanced protection
    shift when neither service knows the original, the strongest as a best effort,
    and no file written when the recogniser never answers. The verified tremolo
    overlay pass runs on the winning shift. Pacing is the shared probe layer's
    policy (a breath between probes, a cool-down that grows while Shazam is
    rate-limiting and shrinks again the moment it answers), so the API's rate
    limit is respected without wasting time on blind waits.

    `macro_fn(input_path, output_path, progress)` runs one macro chain and
    returns {"steps": [...], "metadata": bool} (it must write `output_path`).
    Returns the Auto-Fit V2 result dict plus `macro`:
    {"applied", "steps", "metadata", "reason"}.
    """
    return _copyr8_autofit_macro_search(
        input_path, output_path, spectrogram_path, progress=progress,
        macro_fn=macro_fn, tag="v3", env_hint="COPYR8_V3_MACRO")


def copyr8_autofit_v4(input_path, output_path, spectrogram_path, progress=None,
                      macro_fn=None):
    """Auto-Fit V4: V3's search driven by the V4 (mono wobble) macro chain.

    Identical machinery to copyr8_autofit_v3 -- macro base, probe-then-ship,
    the unverified protection shift, the mixed rung walk with the verified
    tremolo overlay, and the loudness-matched export -- but Stage 1 runs the
    V4 chain (Stereo to Mono -> duplicate panned L/R -> Tremolo x2 on the
    right -> Mix and Render -> SC1 x2), bundled as
    macros/CopyR8 AutoFit V4.txt and overridable with COPYR8_V4_MACRO.
    The macro already carries its own tremolo; the engine's verified overlay
    pass still runs on top of the winning shift, as in V3.
    """
    return _copyr8_autofit_macro_search(
        input_path, output_path, spectrogram_path, progress=progress,
        macro_fn=macro_fn, tag="v4", env_hint="COPYR8_V4_MACRO")


def _copyr8_autofit_macro_search(input_path, output_path, spectrogram_path,
                                 progress=None, macro_fn=None, tag="v3",
                                 env_hint="COPYR8_V3_MACRO"):
    """Shared Auto-Fit V3/V4 engine (see copyr8_autofit_v3's docstring).

    `tag` only names the temp work files (autofit_<tag>_base.wav / _work.wav)
    so the two fits never collide; `env_hint` names the macro env var in the
    "no macro configured" message.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")
    acoustid_backend.validate_setup(required=False)
    songrec_backend.validate_setup(required=False)

    workdir = os.path.dirname(output_path) or "."
    work_wav = os.path.join(workdir, f"autofit_{tag}_work.wav")
    base_wav = os.path.join(workdir, f"autofit_{tag}_base.wav")

    macro_info = {"applied": False, "steps": [], "metadata": False, "reason": None}
    try:
        base_path = input_path
        if macro_fn is not None:
            if progress:
                progress("running the configured macro chain (hidden Audacity)...")
            try:
                macro_result = macro_fn(input_path, base_wav, progress) or {}
                if os.path.isfile(base_wav):
                    macro_info["applied"] = True
                    macro_info["steps"] = list(macro_result.get("steps") or [])
                    macro_info["metadata"] = bool(macro_result.get("metadata"))
                    base_path = base_wav
                else:
                    macro_info["reason"] = ("the macro ran but wrote no output; "
                                            "searching shifts on the original")
                    if progress:
                        progress(macro_info["reason"])
            except Exception as e:  # noqa: BLE001
                macro_info["reason"] = (f"the macro failed ({e}); "
                                        "searching shifts on the original")
                if progress:
                    progress(macro_info["reason"])
        else:
            macro_info["reason"] = (f"no macro configured (set {env_hint}); "
                                    "searching shifts on the original")
            if progress:
                progress(macro_info["reason"])

        # Stage 2: the base may already be unreadable -- nothing could be
        # closer to the original than that, so it is checked before any shift.
        # Only a MACRO-PROCESSED base may ship at 0.0 st / 1.00x. Without a
        # macro the base IS the untouched original, and a clean sweep of it
        # only means the databases don't know the track -- shipping it would
        # hand back the original file with no protection at all. That case goes
        # straight to the Stage 3 walk, whose control check applies the
        # balanced protection shift when the track is unverified.
        ok, counts = False, None
        if macro_info["applied"]:
            windows = _gapfree_windows(base_path)
            ok, counts = _probe_sweep(base_path, windows, progress=progress)
        elif progress:
            progress("no macro output to test on its own; the original is never "
                     "shipped unshifted -- going straight to the shift search")
        if ok:
            verified = _control_check(input_path, progress=progress)
            p_pitch, p_tempo, p_cost = 0.0, 1.0, 0.0
            final_wav = base_path
            if verified is False:
                # Neither service knows the original, so the base sweeping
                # clean proves nothing: protect it with the balanced shift.
                p_pitch, p_tempo = UNVERIFIED_PROTECTION
                p_cost = _protection_cost()
                if progress:
                    progress("control failed: neither service knows the original "
                             "-- applying the balanced protection shift "
                             f"({p_pitch:+.2f} st / {p_tempo:.2f}x) on top of the "
                             "macro output, unverified")
                _render_shifted_wav(base_path, work_wav, p_pitch, p_tempo)
                final_wav = work_wav
            elif verified is True:
                if progress:
                    progress("control ok: both services missed the base "
                             "and a service still knows the original")
            result = {"pitch": p_pitch, "tempo": p_tempo, "cost": p_cost,
                      "counts": counts,
                      "tremolo_depth": 0.0, "tremolo_rate": 0.0,
                      "overlay_applied": False, "overlay_reason": None,
                      "verified": verified,
                      "info": {"pitch": p_pitch, "tempo": p_tempo,
                               "level_gain_db": 0.0}}
        else:
            rungs = _v3_adjust_rungs()
            if progress:
                progress("the filter still detects the base; adding preset "
                         f"adjustments ({len(rungs)} settings), mildest first, "
                         "testing each one against the API...")
            result = find_mildest(base_path, work_wav, progress=progress,
                                  use_tremolo=True,
                                  overlay_tremolo=AUTOFIT_V2_TREMOLO,
                                  rungs=rungs,
                                  control_path=input_path)
            final_wav = work_wav

        pitch = result["pitch"]
        tempo = result["tempo"]
        counts = result.get("counts")
        clean = counts is not None
        verified = result.get("verified")
        cost = result.get("cost")
        tremolo_depth = result.get("tremolo_depth", 0.0) or 0.0
        tremolo_rate = result.get("tremolo_rate", 5.0) or 5.0
        tremolo_applied = bool(result.get("overlay_applied"))
        level_db = (result.get("info") or {}).get("level_gain_db")

        loudness = _match_loudness(final_wav, output_path, input_path, progress=progress)
        rc, _out, err = _run(_FFMPEG + ["-i", output_path, "-lavfi",
                                        "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                                        spectrogram_path])
        if rc != 0:
            raise RuntimeError("spectrogram render failed." + (f" ({err.strip()})" if err.strip() else ""))
        _brand_spectrogram(spectrogram_path)

        if progress:
            progress(f"done: used {pitch:+.1f} st / tempo {tempo:.2f}x"
                     + (f" + tremolo {tremolo_depth:.2f}@{tremolo_rate:g}Hz"
                        if tremolo_applied else "")
                     + (f", level {loudness['out_lufs']:.1f} LUFS vs source "
                        f"{loudness['source_lufs']:.1f}"
                        if loudness.get("out_lufs") is not None
                        and loudness.get("source_lufs") is not None else "")
                     + (""
                        if verified is None else
                        ", confirmed against the recogniser" if verified else
                        ", unverified (neither service knows the original; balanced "
                    "protection applied)"))
        return {
            "pitch": pitch,
            "tempo": tempo,
            "cost": cost,
            "clean": clean,
            "counts": counts,
            "tremolo_depth": tremolo_depth,
            "tremolo_rate": tremolo_rate,
            "tremolo_applied": tremolo_applied,
            "tremolo_reason": result.get("overlay_reason"),
            "verified": verified,
            "level_gain_db": level_db,
            "loudness": loudness,
            "info": _result_info(output_path, progress=progress,
                                 loudness=loudness),
            "macro": macro_info,
        }
    finally:
        for leftover in (work_wav, work_wav + ".trem.wav", work_wav + ".base.wav",
                         base_wav):
            try:
                os.remove(leftover)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# /shazam -- song identification placeholder (kept, disabled)
# --------------------------------------------------------------------------- #
def shazam_recognize(input_path, progress=None):
    """Placeholder for song identification -- currently disabled.

    Kept so the /shazam command still exists as a placeholder for song checking.
    The original identification logic is preserved below but not executed.

    Raises to indicate disabled state.
    """
    raise RuntimeError(
        "Song identification is currently disabled and kept only as a placeholder "
        "for future song checking features. Original implementation is preserved "
        "in code but not executed."
    )


def _shazam_recognize_original_preserved(input_path, progress=None):
    """Original song identification implementation (preserved, not used).

    Kept for reference, not called at runtime.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")
    return {"matched": False}




def search_links(title, artist):
    """Web search URLs for a title/artist.

    Provides search links so the user can look up a track.
    Returns {} when there's nothing to search for.
    """
    q = " ".join(x for x in (artist or "", title or "") if x).strip()
    if not q:
        return {}
    enc = urllib.parse.quote_plus(q)
    return {
        "YouTube": f"https://www.youtube.com/results?search_query={enc}",
        "Spotify": f"https://open.spotify.com/search/{enc}",
        "Apple Music": f"https://music.apple.com/us/search?term={enc}",
    }


# --------------------------------------------------------------------------- #
# /roblox  (two-pass libvorbis compression simulation)
# --------------------------------------------------------------------------- #
def roblox_simulate(input_path, pass1_path, pass2_path, waveform_path, quality=0.5):
    """Re-encode via two libvorbis passes (Roblox-style), then analyze + waveform.

    Returns (info_dict, waveform_path).
    """
    rc, _out, err = _run(_FFMPEG + ["-i", input_path, "-af", "aformat=sample_fmts=s16",
                                    "-c:a", "libvorbis", "-q:a", str(quality), pass1_path])
    if rc != 0:
        raise RuntimeError("first compression pass failed." + (f" ({err.strip()})" if err.strip() else ""))
    rc, _out, err = _run(_FFMPEG + ["-i", pass1_path, "-c:a", "libvorbis",
                                    "-q:a", str(quality), pass2_path])
    if rc != 0:
        raise RuntimeError("second compression pass failed." + (f" ({err.strip()})" if err.strip() else ""))
    info = analyze_audio(pass2_path)
    render_waveform(pass2_path, waveform_path)
    return info, waveform_path


# --------------------------------------------------------------------------- #
# /ping  (system stats)
# --------------------------------------------------------------------------- #
def get_system_stats():
    """Return a small dict of CPU / RAM / GPU / OS info for /ping."""
    stats = {"host": platform.node(), "os": platform.platform(),
             "cpu": platform.processor() or platform.machine(),
             "ram": None, "gpu": "Integrated"}

    try:
        import psutil  # noqa: PLC0415
        vm = psutil.virtual_memory()
        stats["ram"] = f"{vm.used / 1024**3:.2f}GB / {vm.total / 1024**3:.2f}GB"
    except Exception:
        stats["ram"] = "unknown"

    if os.name == "nt":
        try:
            rc, out, _err = _run(["nvidia-smi", "--query-gpu=name",
                                  "--format=csv,noheader"])
            if rc == 0 and out.strip():
                stats["gpu"] = out.strip().splitlines()[0]
        except Exception:
            pass
    return stats


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _cookie_header(cookie):
    """Return a valid Cookie header value.

    The env var may hold just the .ROBLOSECURITY *token* (what a browser/dev
    tool hands you) or already a full 'name=value' cookie string. The Cookie
    header needs the cookie NAME, so wrap a bare token in '.ROBLOSECURITY='.
    """
    if not cookie:
        return ""
    if cookie.lstrip().startswith(".ROBLOSECURITY="):
        return cookie
    return ".ROBLOSECURITY=" + cookie


def _get(url, cookie=ROBLOX_COOKIE, timeout=15, return_headers=False):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Referer": "https://create.roblox.com/", "Origin": "https://create.roblox.com"}
    if cookie:
        h["Cookie"] = _cookie_header(cookie)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            if return_headers:
                return resp.status, body, dict(resp.headers)
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if return_headers:
            return e.code, body, dict(e.headers)
        return e.code, body
    except Exception as e:
        return None, "", {}


def _get_bytes(url, cookie=ROBLOX_COOKIE, timeout=30):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Referer": "https://create.roblox.com/", "Origin": "https://create.roblox.com"}
    if cookie:
        h["Cookie"] = _cookie_header(cookie)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return None, b""


# --------------------------------------------------------------------------- #
# /monitor  (asset moderation)
# --------------------------------------------------------------------------- #
def require_cookie():
    """Raise a clear message when there's no cookie (ending with the scoped reply)."""
    if not ROBLOX_COOKIE:
        raise RuntimeError("no Roblox cookie detected -- set ROBLOX_COOKIE in .env")


def get_asset_moderation(asset_id):
    """GET an asset's moderation state. Returns a dict, or raises on the reasons
    scope surfaced (401 bad cookie, non-200, unreadable state)."""
    require_cookie()
    status, text, _headers = _get(MODERATION_URL.format(id=asset_id), return_headers=True)
    if status == 401:
        raise RuntimeError(
            "authentication failed: the ROBLOX_COOKIE in .env isn't a valid session for the "
            "moderation endpoint. /monitor needs a full .ROBLOSECURITY session cookie to read "
            "moderation state; the raw cookie string alone won't work.")
    if status is None:
        raise RuntimeError("could not reach Roblox to check moderation (network/CSRF). try again shortly.")
    if status != 200:
        raise RuntimeError(f"failed to fetch asset (status {status}). check the asset ID")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("could not read a response from Roblox.")
    state = (data.get("moderationResult") or {}).get("moderationState")
    if not state:
        raise RuntimeError("could not read moderation state.")
    return {
        "state": state,
        "display_name": data.get("displayName") or data.get("name") or asset_id,
        "description": data.get("description") or "",
    }


def download_asset_audio(asset_id):
    """Download an audio asset's bytes via the delivery endpoint (follows redirects).

    assetdelivery serves the body GZIP-COMPRESSED (every asset type comes back
    wrapped in a `1f 8b` header, audio OggS bytes included), so it's gunzipped
    here before returning — otherwise ffmpeg/ffprobe see a gzip container, not
    audio, and the re-encode/analysis fails.
    """
    require_cookie()
    status, data = _get_bytes(ASSET_DELIVERY_URL.format(id=asset_id))
    if status == 200 and data:
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass  # not a real gzip stream; hand back the bytes as-is
        return data
    if status in (401, 403, 409):
        # 401 = no valid session; 403/409 = account genuinely not allowed (a
        # private / group-locked / archived or non-audio asset). No downloader
        # can fetch those, so say so instead of a generic failure.
        if status == 401:
            raise RuntimeError(
                f"asset {asset_id} needs Roblox authentication, but the session "
                "isn't valid (401). refresh ROBLOX_COOKIE in .env.")
        raise RuntimeError(
            f"this asset ({asset_id}) isn't publicly downloadable — it returned "
            f"{status} 'not authorized' (private / group-locked or archived). No "
            "downloader can fetch it.")
    if status is None:
        raise RuntimeError("could not reach the Roblox delivery endpoint (network).")
    raise RuntimeError(f"could not download audio (status {status})")


# --------------------------------------------------------------------------- #
# /download  (yt-dlp)
# --------------------------------------------------------------------------- #
# yt-dlp is a separate CLI (installed via `winget install yt-dlp.yt-dlp`) and is
# NOT a Python module in the bot's interpreter, so it's driven as a subprocess.
# This fallback is the WinGet shim path in case the process PATH does not include
# the WinGet Links directory.
_YTDLP_FALLBACK = r"C:\Users\james\AppData\Local\Microsoft\WinGet\Links\yt-dlp"
# Discord's message-attachment ceiling (~25MB). Anything bigger is re-encoded.
_DISCORD_MAX_BYTES = 24 * 1024 * 1024


def _ytdlp_bin():
    """Locate the yt-dlp CLI (prefer PATH, else the known WinGet shim)."""
    p = shutil.which("yt-dlp")
    if p:
        return p
    if os.path.isfile(_YTDLP_FALLBACK):
        return _YTDLP_FALLBACK
    raise RuntimeError(
        "yt-dlp is not installed or not on PATH (install with: winget install yt-dlp.yt-dlp).")


def _run_yt(args, timeout, what="yt-dlp"):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        raise RuntimeError(f"{what} is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out (the file may be too long to download in time).")


def _cookies_args():
    """Build yt-dlp cookie args from opt-in env config (used for gated/region pages).

    Real content DRM (Widevine/PlayReady) is NOT acceptable here -- yt-dlp refuses
    to decrypt those and this code does nothing to help. What these cookies DO fix
    are the sites that simply want a logged-in session (age/login walls, some
    region-locked or bot-protected pages) and otherwise report a vague error.

      YT_DLP_COOKIES_BROWSER=chrome|firefox|edge   -> --cookies-from-browser <x>
      YT_DLP_COOKIES=<path to a cookies.txt>      -> --cookies <path>
    """
    args = []
    browser = (os.getenv("YT_DLP_COOKIES_BROWSER") or "").strip().lower()
    if browser:
        args += ["--cookies-from-browser", browser]
    cpath = (os.getenv("YT_DLP_COOKIES") or "").strip().strip('"')
    if cpath:
        args += ["--cookies", cpath]
    return args


def _err_hint(err):
    """Turn a raw yt-dlp error blob into a short, honest explanation."""
    e = err or ""
    el = e.lower()
    if any(k in el for k in ("drm", "widevine", "playready", "fairplay")):
        return ("That source is real DRM-protected content (Widevine/PlayReady). "
                "yt-dlp does not decrypt DRM, so no flag can download it. If it is "
                "only BEHIND a login or region gate instead, add your browser cookies "
                "and retry.")
    if any(k in el for k in ("sign in", "login required", "auth required", "cookies",
                             "cookie", "401", "403", "not authorized")):
        return ("The site is holding the file behind a login/region session. Set "
                "YT_DLP_COOKIES_BROWSER to chrome/firefox/edge (or point YT_DLP_COOKIES "
                "at a cookies.txt) and retry.")
    if any(k in el for k in ("geo", "your country", "your region", "unavailable in")):
        return ("That content is geo-restricted and is not being served in your region. "
                "A matching proxy/VPN would be needed.")
    return ""


def _safe_name(name, ext):
    """Turn a media title into a filesystem-safe attachment filename."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()
    base = base[:60].strip() or "download"
    return f"{base}.{ext}"


def _find_output(out_dir):
    """Pick the finished download file (skip yt-dlp temp/partial artifacts)."""
    cands = [
        f for f in glob.glob(os.path.join(out_dir, "download.*"))
        if os.path.splitext(f)[1].lower() not in (".part", ".ytdl", ".tmp", ".json")
    ]
    if not cands:
        raise RuntimeError("yt-dlp finished but produced no output file.")
    return max(cands, key=os.path.getsize)


def _shrink(path, max_bytes, kind):
    """Re-encode a too-large file to progressively smaller sizes to fit Discord."""
    ext = os.path.splitext(path)[1].lower()
    out = os.path.splitext(path)[0] + "_small" + ext
    if kind == "audio":
        plans = [["-vn", "-codec:a", "libmp3lame", "-b:a", br] for br in ("128k", "96k", "64k")]
    else:
        plans = [[
            "-vf", f"scale=-2:{h}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", crf, "-c:a", "aac", "-b:a", "128k",
        ] for h, crf in (("480", "28"), ("360", "30"), ("240", "30"))]
    for plan in plans:
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path] + plan + [out]
        try:
            p = subprocess.run(
                args, capture_output=True, text=True, timeout=150,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            if p.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) <= max_bytes:
                os.replace(out, path)   # keep the original filename, swap the content
                return path
        except Exception:
            pass
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
    return None


def download_media(url, out_dir, kind="audio", max_bytes=_DISCORD_MAX_BYTES):
    """Download a video/audio URL via the yt-dlp CLI into out_dir.

    kind='audio' extracts the best audio as mp3; kind='video' grabs the best
    <=720p stream and merges it to mp4. Returns a dict with the media title,
    duration, uploader, source url, local path, size and any shrink note.
    Raises RuntimeError if yt-dlp can't read or download the link.
    """
    ytdlp = _ytdlp_bin()
    if not url or not str(url).strip().lower().startswith(("http://", "https://")):
        raise RuntimeError("That doesn't look like a URL (needs http:// or https://).")

    # Let yt-dlp find ffmpeg explicitly (it needs it to extract/merge audio video).
    # YouTube's default client frequently answers the bestaudio/media request with
    # HTTP 403 (throttling); the android / web_embedded players are allowed and
    # download fine, so pin those. They only apply to the youtube extractor, so
    # non-YouTube links are unaffected.
    base = [ytdlp, "--no-warnings", "--no-playlist",
            "--extractor-args", "youtube:player_client=android,web_embedded"]
    # Cookies let gated / region-locked / bot-protected pages work when the user
    # enables them (see _cookies_args). Real DRM still refuses -- that is intended.
    base += _cookies_args()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        base += ["--ffmpeg-location", os.path.dirname(ffmpeg)]

    # Relaxed flags used as a second attempt, for pages that block the default
    # client or serve a self-signed cert. Harmless when the first pass succeeds.
    relaxed = ["--user-agent", UA, "--no-check-certificate",
               "--extractor-retries", "3", "--retries", "3"]

    def _probe(cmd):
        return _run_yt(cmd, timeout=60)

    # Phase A -- fast metadata probe (validates the link, gives us the title).
    rc, out, err = _probe(
        base + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", url])
    if rc != 0:
        rc, out, err = _probe(
            base + relaxed + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", url])
    if rc != 0:
        hint = _err_hint(err)
        raise RuntimeError("yt-dlp could not read that link."
                           + (f" {err.strip()}" if err.strip() else "")
                           + (f"\n{hint}" if hint else ""))
    parts = (out.strip().split("\t") + ["", "", ""])[:3]
    title, duration, uploader = parts[0], parts[1], parts[2]

    # Phase B -- the actual download into a fixed basename in out_dir.
    out_tmpl = os.path.join(out_dir, "download.%(ext)s")
    if kind == "audio":
        dl = base + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                     "-o", out_tmpl, url]
    else:
        dl = base + ["-f", "bv*[height<=720]+ba/b[height<=720]",
                     "--merge-output-format", "mp4", "-o", out_tmpl, url]
    rc, _out, err = _run_yt(dl, timeout=240)
    if rc != 0:
        rc, _out, err = _run_yt(dl + relaxed, timeout=240)
    if rc != 0:
        hint = _err_hint(err)
        raise RuntimeError("yt-dlp download failed."
                           + (f" {err.strip()}" if err.strip() else "")
                           + (f"\n{hint}" if hint else ""))

    path = _find_output(out_dir)
    note = ""
    if os.path.getsize(path) > max_bytes:
        if _shrink(path, max_bytes, kind):
            note = "Compressed to fit Discord's file-size limit."
        else:
            note = (f"Final file is {os.path.getsize(path) / 1e6:.1f} MB, "
                    "larger than Discord's attachment limit — you may need to trim it.")

    ext = os.path.splitext(path)[1].lstrip(".") or "download"
    final = os.path.join(out_dir, _safe_name(title, ext))
    if os.path.abspath(path) != os.path.abspath(final):
        os.replace(path, final)
        path = final

    return {
        "title": title or os.path.basename(path),
        "duration": duration or "—",
        "uploader": uploader or "—",
        "webpage_url": url,
        "path": path,
        "size": os.path.getsize(path),
        "ext": ext,
        "kind": kind,
        "note": note,
    }
