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
import html
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


def _resolve_ffmpeg_bin():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # noqa: PLC0415
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return "ffmpeg"


def _resolve_ffprobe_bin():
    found = shutil.which("ffprobe")
    if found:
        return found
    return "ffprobe"


_FFMPEG_BIN = _resolve_ffmpeg_bin()
_FFPROBE_BIN = _resolve_ffprobe_bin()
_FFMPEG = [_FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"]
_FFPROBE = [_FFPROBE_BIN, "-v", "error"]

# Metadata-stripping flags so exported audio files never retain encoder names,
# software tags, comments, prompts, or AI-related text markers.
_CLEAN_META_FLAGS = [
    "-map_metadata", "-1",
    "-dn", "-vn", "-sn",
    "-fflags", "+bitexact",
    "-flags:a", "+bitexact",
    "-metadata", "encoder=",
    "-metadata", "comment=",
    "-metadata", "description=",
    "-metadata", "software=",
    "-metadata", "encoded_by=",
    "-metadata", "lyrics=",
    "-metadata:s:a:0", "encoder=",
]
_CLEAN_MP3_FLAGS = _CLEAN_META_FLAGS + ["-write_xing", "0", "-id3v2_version", "0"]

_AI_BRACKET_PATTERNS = re.compile(
    r"[\(\[\{]\s*"
    r"(?:ai[\s_-]*generated|generated[\s_-]*by[\s_-]*ai|made[\s_-]*with[\s_-]*ai|"
    r"ai[\s_-]*(?:audio|song|music|cover|voice)|suno(?:[\s_-]*ai)?|udio(?:[\s_-]*ai)?|ai)"
    r"\s*[\)\]\}]",
    re.IGNORECASE,
)

_AI_STEM_PATTERNS = re.compile(
    r"(?:^|(?<=[\s_.\-\(\[\{]))"
    r"(?:ai[\s_-]*generated|generated[\s_-]*by[\s_-]*ai|made[\s_-]*with[\s_-]*ai|"
    r"ai[\s_-]*audio|ai[\s_-]*song|ai[\s_-]*music|ai[\s_-]*cover|ai[\s_-]*voice|"
    r"suno[\s_-]*ai|udio[\s_-]*ai|suno|udio|ai)"
    r"(?=$|[\s_.\-\)\]\}])",
    re.IGNORECASE,
)


def clean_export_stem(raw_name, fallback="audio"):
    """Sanitize an exported audio basename and remove any AI-related wording."""
    stem = os.path.splitext(os.path.basename(str(raw_name or "")))[0]
    stem = _AI_BRACKET_PATTERNS.sub("", stem)
    for _ in range(3):
        updated = _AI_STEM_PATTERNS.sub("", stem)
        if updated == stem:
            break
        stem = updated
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem)
    stem = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", stem)
    stem = re.sub(r"_{2,}", "_", stem)
    stem = re.sub(r" {2,}", " ", stem)
    stem = re.sub(r"(?:_-\s*|\s*-_)", "-", stem)
    stem = re.sub(r"-{2,}", "-", stem).strip(" _.-")
    return (stem[:60].strip(" _.-") if stem else "") or fallback


def strip_audio_metadata(path):
    """Strip all container, ID3, Xing/LAME, Vorbis, or RIFF tags from `path` in-place."""
    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    tmp = f"{path}.clean{ext}"
    flags = _CLEAN_MP3_FLAGS if ext == ".mp3" else _CLEAN_META_FLAGS
    rc, _out, _err = _run(_FFMPEG + ["-i", path] + flags + ["-c:a", "copy", tmp])
    if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 256:
        os.replace(tmp, path)
        return True
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    return False


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
    ffmpeg_bin = globals().get("_FFMPEG_BIN", "ffmpeg")
    for attempt in range(3):
        rc, _out, err = _run([ffmpeg_bin, "-hide_banner", "-nostats", "-i", path,
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
        try:
            rc, out, err = _run(_FFPROBE + ["-print_format", "json", "-show_format",
                                            "-show_streams", path])
        except RuntimeError:
            break
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
    rc, _out, err = _run([_FFMPEG_BIN, "-hide_banner", "-nostats", "-i", path,
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


_V4_COMB_NOTCHES = (
    (2550, 120),
    (3700, 150),
    (5050, 185),
    (6650, 225),
    (8500, 265),
    (10600, 310),
    (12950, 355),
    (15550, 390),
)


def _v4_comb_gain_entry():
    """Piecewise gain_entry for V4's zero-phase FIR sub-critical-band comb."""
    entries = ["entry(0,0)", "entry(2050,0.4)"]
    for center, half_w in _V4_COMB_NOTCHES:
        entries.append(f"entry({center - half_w - 85},1.7)")
        entries.append(f"entry({center - half_w},-65)")
        entries.append(f"entry({center + half_w},-65)")
        entries.append(f"entry({center + half_w + 85},1.7)")
    entries.append("entry(17400,1.2)")
    entries.append("entry(18250,-65)")
    entries.append("entry(24000,-65)")
    return ";".join(entries)


def _v4_studio_af(include_comb=True):
    """Stereo studio processing chain for Auto-Fit V4 (no tremolo warble, no mono fold)."""
    chain = (
        "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "allpass=f=680:t=q:w=1.1,allpass=f=2400:t=q:w=1.1,"
        "aphaser=in_gain=0.95:out_gain=0.97:delay=1.4:decay=0.18:speed=0.25:type=triangular,"
        "extrastereo=m=1.14,crystalizer=i=0.45,apulsator=mode=sine:hz=0.20:amount=0.04,"
        "acompressor=threshold=-16dB:ratio=1.8:attack=25:release=220:makeup=1.0dB"
    )
    if include_comb:
        chain += f",firequalizer=gain_entry='{_v4_comb_gain_entry()}':zero_phase=1:accuracy=4"
    return chain


def _render_v4_studio_base(input_path, out_wav):
    """Render V4's stereo studio FX base WAV via ffmpeg when Audacity isn't running."""
    rc, _out, err = _run(
        _FFMPEG + ["-i", input_path, "-af", _v4_studio_af(include_comb=True)]
        + _CLEAN_META_FLAGS + [out_wav]
    )
    if rc != 0:
        raise RuntimeError("V4 studio base render failed."
                           + (f" ({err.strip()})" if err.strip() else ""))
    return out_wav


def _v4_sculpt_filter_complex(gain_db, clip_threshold, duration_secs):
    """Build V4's loudness + zero-phase spectral lattice + ultrasonic double-helix graph."""
    dur = max(float(duration_secs or 30.0), 8.0)
    fm = max(0.025, min(0.45, 9.0 / dur))
    beta = round(750.0 / fm, 2)
    rib_ch0 = (
        f"(0.32*min(0.22,abs(val(0))))*("
        f"sin(2*PI*19700*t+{beta}*sin(2*PI*{fm:.5f}*t))"
        f"+0.95*sin(2*PI*19700*t-{beta}*sin(2*PI*{fm:.5f}*t)))"
    )
    rib_ch1 = rib_ch0.replace("val(0)", "val(1)")
    gain_entry = _v4_comb_gain_entry()
    return (
        f"[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[s_main][s_rib];"
        f"[s_main]volume={gain_db:.2f}dB,"
        f"asoftclip=type=atan:oversample=4:threshold={clip_threshold},"
        f"firequalizer=gain_entry='{gain_entry}':zero_phase=1:accuracy=4[main];"
        f"[s_rib]lowpass=f=20,aeval='{rib_ch0}|{rib_ch1}',"
        f"highpass=f=18850,highpass=f=18850,highpass=f=18850[rib];"
        f"[main][rib]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.89:level=0:attack=3:release=40"
    )


def _match_loudness(work_wav, output_path, source_path, progress=None, *, v4_sculpt=False):
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
    try:
        work_dur = _duration_seconds(work_wav)
    except Exception:
        work_dur = 30.0

    def _encode(dst, gain_db):
        if v4_sculpt:
            fc = _v4_sculpt_filter_complex(gain_db, AUTOFIT_CLIP_THRESHOLD, work_dur)
            cmd = (
                _FFMPEG + ["-i", work_wav, "-filter_complex", fc]
                + _CLEAN_MP3_FLAGS
                + ["-c:a", "libmp3lame", "-b:a", "256k", "-cutoff", "21000", dst]
            )
        else:
            af = (f"volume={gain_db:.2f}dB,asoftclip=type=atan:oversample=4"
                  f":threshold={AUTOFIT_CLIP_THRESHOLD}")
            cmd = (
                _FFMPEG + ["-i", work_wav, "-af", af]
                + _CLEAN_MP3_FLAGS
                + ["-c:a", "libmp3lame", "-b:a", "192k", dst]
            )
        rc, _out, err = _run(cmd)
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

    rc, _out, err = _run(
        _FFMPEG + ["-i", input_path, "-af", af]
        + _CLEAN_MP3_FLAGS
        + ["-c:a", "libmp3lame", "-b:a", "192k", output_path]
    )
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
        progress(f"API pacing (API Cooldown) -- Shazam rate limit hit; pausing {wait:.0f}s "
                 "before testing again")


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
    """Sleep past the rate-limit cool-down, then keep the API Cooldown spacing."""
    global _last_post_time
    now = time.time()
    if _rate_limit_until > now:
        wait = _rate_limit_until - now
        if progress:
            progress(f"API pacing (API Cooldown) -- waiting {wait:.0f}s for rate-limit "
                     "cool-down to finish")
        time.sleep(wait)
        now = time.time()
    elapsed = now - _last_post_time
    if elapsed < _breath_seconds:
        wait = _breath_seconds - elapsed
        if progress and wait >= 0.5:
            progress(f"API pacing (API Cooldown) -- cooling down {wait:.1f}s between audio probes")
        time.sleep(wait)
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

    global _last_post_time
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
        finally:
            # Stamp completion time so the full API Cooldown spacing elapses
            # in between consecutive audio clips rather than overlapping the HTTP call.
            _last_post_time = time.time()
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
    total_w = len(windows)
    for idx, start in enumerate(windows):
        if idx > 0 and progress:
            progress(f"testing in-between audio window {idx + 1}/{total_w} "
                     f"({start:.1f}s..{start + SHA_PROBE_CLIP_SECONDS:.1f}s)...")
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
    total_w = len(windows)
    for idx, start in enumerate(windows):
        if duration > src_clip:
            # Keep the (longer) source cut inside the file so the tail window
            # is not truncated back under the probe length.
            start = min(start, round(duration - src_clip, 3))
        if idx > 0 and progress:
            progress(f"testing in-between audio window {idx + 1}/{total_w} "
                     f"({start:.1f}s..{start + src_clip:.1f}s)...")
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

            # Confirm every gap-free window across the start, in-between body,
            # and tail of the song before accepting a shift.
            windows = _gapfree_windows(input_path)
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
    windows = _gapfree_windows(output_path)
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
    """Auto-Fit V4: stereo studio FX + zero-phase spectral sculpting + clean search.

    Preserves full stereo imaging and natural tonal balance (no mono collapse,
    no tremolo warble, and no dual-SC1 compression pumping) while applying
    phase dispersion, subtle phaser/stereo width, sub-critical-band zero-phase
    FIR comb sculpting, and an envelope-gated ultrasonic double-helix braid so
    the spectrogram looks distinctly transformed while the audio sounds clean.
    """
    return _copyr8_autofit_macro_search(
        input_path, output_path, spectrogram_path, progress=progress,
        macro_fn=macro_fn, tag="v4", env_hint="COPYR8_V4_MACRO")


def _copyr8_autofit_macro_search(input_path, output_path, spectrogram_path,
                                 progress=None, macro_fn=None, tag="v3",
                                 env_hint="COPYR8_V3_MACRO"):
    """Shared Auto-Fit V3/V4 engine (see copyr8_autofit_v3 and copyr8_autofit_v4).

    `tag` names the temp work files (autofit_<tag>_base.wav / _work.wav) and
    selects V4's tremolo-free studio + spectrogram-sculpting pipeline when
    `tag == "v4"`.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")
    acoustid_backend.validate_setup(required=False)
    songrec_backend.validate_setup(required=False)

    is_v4 = (tag == "v4")
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
                                            "searching shifts on the studio base"
                                            if is_v4 else
                                            "the macro ran but wrote no output; "
                                            "searching shifts on the original")
                    if progress:
                        progress(macro_info["reason"])
            except Exception as e:  # noqa: BLE001
                macro_info["reason"] = (f"the macro failed ({e}); "
                                        + ("using built-in V4 studio processing"
                                           if is_v4 else
                                           "searching shifts on the original"))
                if progress:
                    progress(macro_info["reason"])
        else:
            macro_info["reason"] = (f"no macro configured (set {env_hint}); "
                                    + ("using built-in V4 studio processing"
                                       if is_v4 else
                                       "searching shifts on the original"))
            if progress:
                progress(macro_info["reason"])

        if is_v4 and not macro_info["applied"]:
            try:
                _render_v4_studio_base(input_path, base_wav)
                base_path = base_wav
            except Exception:
                base_path = input_path

        # Stage 2: only a MACRO-PROCESSED base may ship at 0.0 st / 1.00x.
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
            rungs = ([r for r in _v3_adjust_rungs() if not r[3]]
                     if is_v4 else _v3_adjust_rungs())
            if progress:
                progress("the filter still detects the base; adding preset "
                         f"adjustments ({len(rungs)} settings), mildest first, "
                         "testing each one against the API...")
            result = find_mildest(
                base_path, work_wav, progress=progress,
                use_tremolo=(not is_v4),
                overlay_tremolo=(None if is_v4 else AUTOFIT_V2_TREMOLO),
                rungs=rungs,
                control_path=input_path,
            )
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

        loudness = _match_loudness(
            final_wav, output_path, input_path, progress=progress, v4_sculpt=is_v4
        )
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
    rc, _out, err = _run(_FFMPEG + ["-i", pass1_path] + _CLEAN_META_FLAGS
                                 + ["-c:a", "libvorbis", "-q:a", str(quality), pass2_path])
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
    except Exception:
        return (None, "", {}) if return_headers else (None, "")


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
    except Exception:
        return None, b""


# --------------------------------------------------------------------------- #
# /monitor & /analyze roblox  (asset moderation + lookup + delivery)
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


def lookup_roblox_asset(asset_id):
    """Best-effort metadata lookup for a Roblox asset ID (works with or without cookie)."""
    import verify_audio  # noqa: PLC0415

    aid = int(asset_id)
    eco = verify_audio._economy(aid) or {}
    mkt = verify_audio._marketplace(aid) or {}
    asset_type_id = eco.get("AssetTypeId") or mkt.get("AssetTypeId")
    creator_obj = eco.get("Creator") or mkt.get("Creator") or {}
    name = eco.get("Name") or mkt.get("Name")
    description = eco.get("Description") or mkt.get("Description") or ""
    creator = creator_obj.get("Name") or None
    is_banned = bool(mkt.get("IsBanned"))
    mod_state = None
    if ROBLOX_COOKIE:
        try:
            mod = get_asset_moderation(aid)
            mod_state = mod.get("state")
            if not name and mod.get("display_name"):
                name = mod["display_name"]
            if not description and mod.get("description"):
                description = mod["description"]
        except Exception:
            mod_state = None

    return {
        "id": aid,
        "name": name or f"Asset {aid}",
        "creator": creator,
        "asset_type_id": asset_type_id,
        "asset_type": verify_audio._type_name(asset_type_id) if asset_type_id else None,
        "is_audio": asset_type_id in (None, 3, 12),
        "is_banned": is_banned,
        "moderation_state": mod_state,
        "description": description,
    }


def _unpack_delivery_bytes(data, cookie=ROBLOX_COOKIE):
    """Gunzip or follow JSON CDN location payload from Roblox assetdelivery."""
    if not data:
        return b""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    stripped = data.lstrip()
    if stripped.startswith(b"{"):
        try:
            payload = json.loads(stripped.decode("utf-8", "replace"))
            if isinstance(payload, dict):
                if payload.get("errors"):
                    return b""
                locs = payload.get("locations") or []
                loc_url = (
                    (locs[0].get("location") if locs and isinstance(locs[0], dict) else None)
                    or payload.get("location")
                )
                if loc_url:
                    st2, d2 = _get_bytes(loc_url, cookie="")
                    if st2 == 200 and d2:
                        if d2[:2] == b"\x1f\x8b":
                            try:
                                d2 = gzip.decompress(d2)
                            except OSError:
                                pass
                        return d2
                    return b""
        except Exception:
            pass
    return data


def download_asset_audio(asset_id):
    """Download an audio asset's bytes via the delivery endpoint (follows redirects).

    Uses ROBLOX_COOKIE automatically when configured, and still attempts public
    delivery when unset so public Roblox audio assets can be downloaded too.
    """
    status, raw = _get_bytes(ASSET_DELIVERY_URL.format(id=asset_id))
    if status == 200 and raw:
        data = _unpack_delivery_bytes(raw)
        if data and len(data) >= 128:
            return data
        raise RuntimeError(f"Roblox returned an empty or unreadable payload for asset {asset_id}.")
    if status in (401, 403, 409):
        if status == 401:
            if not ROBLOX_COOKIE:
                raise RuntimeError(
                    f"asset {asset_id} requires Roblox authentication (set ROBLOX_COOKIE in .env).")
            raise RuntimeError(
                f"asset {asset_id} needs Roblox authentication, but the session "
                "isn't valid (401). refresh ROBLOX_COOKIE in .env.")
        raise RuntimeError(
            f"this asset ({asset_id}) isn't downloadable (HTTP {status}: "
            "private, group-locked, moderated, or archived).")
    if status == 404:
        raise RuntimeError(f"asset {asset_id} was not found on Roblox (404).")
    if status is None:
        raise RuntimeError("could not reach the Roblox delivery endpoint (network).")
    raise RuntimeError(f"could not download audio (status {status})")


# --------------------------------------------------------------------------- #
# /download  (yt-dlp + Spotify metadata search + SoundCloud support)
# --------------------------------------------------------------------------- #
_YTDLP_FALLBACK = r"C:\Users\james\AppData\Local\Microsoft\WinGet\Links\yt-dlp"
_DISCORD_MAX_BYTES = 24 * 1024 * 1024

_SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|episode):([A-Za-z0-9]+)$", re.IGNORECASE
)
_SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z0-9-]+/)?(?:embed/)?"
    r"(track|album|playlist|episode)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_SPOTIFY_SHORT_HOSTS = {"spotify.link", "spoti.fi", "www.spotify.link"}
_SOUNDCLOUD_SHORT_HOSTS = {"on.soundcloud.com", "soundcloud.app.goo.gl"}
_SOUNDCLOUD_HOSTS = {
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com",
    "on.soundcloud.com", "soundcloud.app.goo.gl",
}
_SC_DROP_QUERY_KEYS = {
    "si", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "ref", "clipboard", "feature",
}


def _ytdlp_bin():
    """Locate the yt-dlp CLI (prefer PATH, else active venv, else WinGet shim)."""
    p = shutil.which("yt-dlp")
    if p:
        return p
    py_dir = os.path.dirname(os.path.abspath(__import__("sys").executable))
    for cand in (os.path.join(py_dir, "yt-dlp.exe"), os.path.join(py_dir, "yt-dlp")):
        if os.path.isfile(cand):
            return cand
    if os.path.isfile(_YTDLP_FALLBACK):
        return _YTDLP_FALLBACK
    raise RuntimeError(
        "yt-dlp is not installed or not on PATH (install with: winget install yt-dlp.yt-dlp).")


def _run_yt(args, timeout, what="yt-dlp"):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        raise RuntimeError(f"{what} is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out (the file may be too long to download in time).")


def _cookies_args():
    """Build yt-dlp cookie args from opt-in env config (used for gated/region pages)."""
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
        return ("That source is DRM-protected content (Widevine/PlayReady). "
                "yt-dlp does not decrypt DRM.")
    if any(k in el for k in ("sign in", "login required", "auth required", "cookies",
                             "cookie", "401", "403", "not authorized")):
        return ("The site is holding the file behind a login/region session. Set "
                "YT_DLP_COOKIES_BROWSER to chrome/firefox/edge (or point YT_DLP_COOKIES "
                "at a cookies.txt) and retry.")
    if any(k in el for k in ("geo", "your country", "your region", "unavailable in")):
        return ("That content is geo-restricted and is not being served in your region.")
    return ""


def _resolve_redirect_url(url, timeout=10):
    """Follow HTTP redirects (e.g. spotify.link / on.soundcloud.com) to final URL."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final_url = resp.geturl()
                if final_url:
                    return final_url
        except urllib.error.HTTPError as exc:
            if exc.url and exc.url != url:
                return exc.url
            loc = (exc.headers or {}).get("Location")
            if loc:
                return urllib.parse.urljoin(url, loc)
        except Exception:
            continue
    return url


def _normalize_download_url(raw_url):
    """Normalize pasted URLs/URIs, resolving Spotify/SoundCloud short links."""
    s = str(raw_url or "").strip().strip("<>").strip()
    m_uri = _SPOTIFY_URI_RE.match(s)
    if m_uri:
        kind_s, sid = m_uri.group(1).lower(), m_uri.group(2)
        return f"https://open.spotify.com/{kind_s}/{sid}"
    if not s.lower().startswith(("http://", "https://")):
        return s

    parsed = urllib.parse.urlsplit(s)
    host = (parsed.hostname or "").lower()
    if host in _SPOTIFY_SHORT_HOSTS or host in _SOUNDCLOUD_SHORT_HOSTS:
        s = _resolve_redirect_url(s)
        parsed = urllib.parse.urlsplit(s)
        host = (parsed.hostname or "").lower()

    if host in ("m.soundcloud.com", "www.soundcloud.com", "soundcloud.com"):
        qs = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
            if k.lower() not in _SC_DROP_QUERY_KEYS
        ]
        s = urllib.parse.urlunsplit((
            "https",
            "soundcloud.com",
            parsed.path,
            urllib.parse.urlencode(qs),
            "",
        ))
    return s


def _http_fetch_text(url, timeout=12):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(1048576).decode("utf-8", "replace")


def _parse_spotify_embed_json(page_html):
    """Extract {title, artist, duration_secs} from Spotify's public embed __NEXT_DATA__."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
        page_html or "",
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        blob = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    entity = (
        (((blob.get("props") or {}).get("pageProps") or {}).get("state") or {})
        .get("data") or {}
    ).get("entity") or {}
    if not isinstance(entity, dict):
        return None

    # Album / playlist embed: pick lead track from trackList while keeping album artist fallback
    track_list = entity.get("trackList")
    if isinstance(track_list, list) and track_list:
        first = track_list[0] if isinstance(track_list[0], dict) else {}
        t_title = (first.get("title") or "").strip()
        t_artist = (first.get("subtitle") or entity.get("subtitle") or "").strip()
        t_dur_ms = first.get("duration")
        if t_title:
            return {
                "title": html.unescape(t_title),
                "artist": html.unescape(t_artist.replace("\u00a0", " ")),
                "duration_secs": round(float(t_dur_ms) / 1000.0, 1) if t_dur_ms else None,
            }

    title = (entity.get("title") or entity.get("name") or "").strip()
    artists = entity.get("artists")
    if isinstance(artists, list) and artists:
        artist_names = [
            str(a.get("name") or "").strip()
            for a in artists
            if isinstance(a, dict) and a.get("name")
        ]
        artist = ", ".join(artist_names)
    else:
        artist = str(entity.get("subtitle") or "").strip()
    dur_ms = entity.get("duration") or entity.get("duration_ms")
    if not title:
        return None
    return {
        "title": html.unescape(title),
        "artist": html.unescape(artist.replace("\u00a0", " ")),
        "duration_secs": round(float(dur_ms) / 1000.0, 1) if dur_ms else None,
    }


def _parse_spotify_html_meta(page_html):
    """Fallback OpenGraph / <title> parser for open.spotify.com pages."""
    if not page_html:
        return None

    def _meta(attr, key):
        pat = (
            rf'<meta[^>]+{attr}=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']'
            rf'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attr}=["\']{re.escape(key)}["\']'
        )
        mm = re.search(pat, page_html, re.IGNORECASE)
        if not mm:
            return ""
        return html.unescape((mm.group(1) or mm.group(2) or "").strip())

    title = _meta("property", "og:title")
    desc = _meta("property", "og:description")
    musician = _meta("name", "music:musician_description")
    dur_raw = _meta("name", "music:duration")
    artist = musician
    if not artist and desc:
        # Spotify og:description format: "Artist · Song · 2024" or "Listen to X on Spotify. Artist · Song"
        parts = [p.strip() for p in desc.split("·") if p.strip()]
        if len(parts) >= 2:
            cand = parts[0]
            if " on spotify." in cand.lower():
                cand = cand.split(".", 1)[-1].strip()
            artist = cand
    if not title or not artist:
        m_t = re.search(r"<title[^>]*>([\s\S]*?)</title>", page_html, re.IGNORECASE)
        if m_t:
            raw_t = html.unescape(re.sub(r"\s+", " ", m_t.group(1)).strip())
            raw_t = re.sub(r"\s*\|\s*Spotify\s*$", "", raw_t, flags=re.IGNORECASE)
            m_by = re.match(
                r"^(?P<song>.+?)\s*-\s*(?:song(?:\s+and\s+lyrics)?|single|album|playlist|episode)\s+by\s+(?P<art>.+)$",
                raw_t,
                re.IGNORECASE,
            )
            if m_by:
                title = title or m_by.group("song").strip()
                artist = artist or m_by.group("art").strip()
            elif not title:
                title = raw_t
    if not title:
        return None
    dur_secs = None
    if dur_raw and dur_raw.isdigit():
        dur_secs = float(dur_raw)
    return {"title": title, "artist": artist or "", "duration_secs": dur_secs}


def resolve_spotify_metadata(url):
    """Resolve public track/album/playlist metadata from a Spotify link (no DRM touched)."""
    m = _SPOTIFY_URL_RE.search(url or "")
    if not m:
        return None
    item_type, item_id = m.group(1).lower(), m.group(2)
    canonical = f"https://open.spotify.com/{item_type}/{item_id}"
    embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"

    # 1. Public Spotify embed JSON (carries exact title + artist + duration + playlist lead track)
    try:
        embed_html = _http_fetch_text(embed_url)
        meta = _parse_spotify_embed_json(embed_html)
        if meta and meta.get("title"):
            meta["canonical_url"] = canonical
            meta["item_type"] = item_type
            return meta
    except Exception:
        pass

    # 2. Public Spotify oEmbed + page OpenGraph fallback
    oembed_title = ""
    try:
        oembed_raw = _http_fetch_text(
            "https://open.spotify.com/oembed?url=" + urllib.parse.quote(canonical, safe="")
        )
        oembed_json = json.loads(oembed_raw)
        oembed_title = html.unescape(str(oembed_json.get("title") or "").strip())
    except Exception:
        oembed_title = ""

    try:
        page_html = _http_fetch_text(canonical)
        meta = _parse_spotify_html_meta(page_html)
        if meta and meta.get("title"):
            meta["canonical_url"] = canonical
            meta["item_type"] = item_type
            return meta
    except Exception:
        pass

    if oembed_title:
        return {
            "title": oembed_title,
            "artist": "",
            "duration_secs": None,
            "canonical_url": canonical,
            "item_type": item_type,
        }
    raise RuntimeError(
        "Could not read track metadata from that Spotify link. Make sure the Spotify "
        "track or playlist is public and try again."
    )


def _is_spotify_url(url):
    return bool(_SPOTIFY_URL_RE.search(url or ""))


def _is_soundcloud_url(url):
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except Exception:
        return False
    return host in _SOUNDCLOUD_HOSTS or host.endswith(".soundcloud.com")


def _safe_name(name, ext):
    """Turn a media title into a clean, filesystem-safe attachment filename."""
    base = clean_export_stem(name, fallback="download")
    return f"{base}.{ext}"


def _clear_partial_downloads(out_dir):
    for f in glob.glob(os.path.join(out_dir, "download.*")):
        try:
            os.remove(f)
        except OSError:
            pass


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
        plans = [
            _CLEAN_MP3_FLAGS + ["-vn", "-codec:a", "libmp3lame", "-b:a", br]
            for br in ("128k", "96k", "64k")
        ]
    else:
        plans = [[
            "-map_metadata", "-1", "-vf", f"scale=-2:{h}", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", crf, "-c:a", "aac", "-b:a", "128k",
        ] for h, crf in (("480", "28"), ("360", "30"), ("240", "30"))]
    for plan in plans:
        args = _FFMPEG + ["-i", path] + plan + [out]
        try:
            p = subprocess.run(
                args, capture_output=True, text=True, timeout=150,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            if p.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) <= max_bytes:
                os.replace(out, path)
                return path
        except Exception:
            pass
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
    return None


def _format_duration_display(raw_dur):
    """Format seconds or string duration neatly as M:SS when numeric."""
    if raw_dur is None:
        return "—"
    s = str(raw_dur).strip()
    if not s or s.upper() == "NA":
        return "—"
    try:
        secs = int(round(float(s)))
        if secs > 0:
            return f"{secs // 60}:{secs % 60:02d}"
    except (TypeError, ValueError):
        pass
    return s


def download_media(url, out_dir, kind="audio", max_bytes=_DISCORD_MAX_BYTES):
    """Download playable audio/video for a URL (including Spotify & SoundCloud) via yt-dlp.

    Spotify streams use Widevine DRM and are never decrypted or ripped; instead,
    Spotify links resolve public track/artist metadata and fetch matching playable
    audio via yt-dlp search (YouTube / SoundCloud fallback). SoundCloud short links,
    mobile links, tracks, and sets are normalized and downloaded with SoundCloud-safe
    format selection.
    """
    ytdlp = _ytdlp_bin()
    norm_url = _normalize_download_url(url)
    if not norm_url or not norm_url.lower().startswith(("http://", "https://")):
        raise RuntimeError("That doesn't look like a URL (needs http:// or https://).")

    ffmpeg_loc = []
    if _FFMPEG_BIN and os.path.isfile(_FFMPEG_BIN):
        ffmpeg_loc = ["--ffmpeg-location", _FFMPEG_BIN]
    else:
        ff_which = shutil.which("ffmpeg")
        if ff_which:
            ffmpeg_loc = ["--ffmpeg-location", ff_which]

    cookies = _cookies_args()
    relaxed = [
        "--user-agent", UA, "--no-check-certificate",
        "--extractor-retries", "3", "--retries", "3",
    ]
    out_tmpl = os.path.join(out_dir, "download.%(ext)s")

    def _finalize_result(title, duration, uploader, webpage_url, note=""):
        path = _find_output(out_dir)
        if kind == "audio":
            strip_audio_metadata(path)
        if os.path.getsize(path) > max_bytes:
            if _shrink(path, max_bytes, kind):
                note = (f"{note} " if note else "") + "Compressed to fit Discord's file-size limit."
            else:
                note = (
                    (f"{note} " if note else "")
                    + f"Final file is {os.path.getsize(path) / 1e6:.1f} MB, "
                    "larger than Discord's attachment limit."
                )
        if kind == "audio":
            strip_audio_metadata(path)
        ext = os.path.splitext(path)[1].lstrip(".") or "mp3"
        final = os.path.join(out_dir, _safe_name(title, ext))
        if os.path.abspath(path) != os.path.abspath(final):
            os.replace(path, final)
            path = final
        return {
            "title": title or os.path.basename(path),
            "duration": _format_duration_display(duration),
            "uploader": uploader or "—",
            "webpage_url": webpage_url,
            "path": path,
            "size": os.path.getsize(path),
            "ext": ext,
            "kind": kind,
            "note": note.strip(),
        }

    # ---- Branch 1: Spotify link -> metadata lookup + yt-dlp search -----------
    if _is_spotify_url(norm_url):
        meta = resolve_spotify_metadata(norm_url)
        sp_title = meta["title"]
        sp_artist = meta.get("artist") or ""
        sp_dur = meta.get("duration_secs")
        display_title = f"{sp_artist} - {sp_title}" if sp_artist else sp_title
        base_query = f"{sp_artist} - {sp_title}".strip(" -") if sp_artist else sp_title

        search_targets = []
        if kind == "audio":
            if sp_dur and sp_dur >= 25:
                lo = max(15, int(sp_dur - 45))
                hi = int(sp_dur + 90)
                search_targets.append((
                    f"ytsearch3:{base_query} official audio",
                    ["--playlist-items", "1:3", "--max-downloads", "1",
                     "--match-filter", f"duration >= {lo} & duration <= {hi}"],
                    True,
                ))
            search_targets.append((f"ytsearch1:{base_query} audio", ["--no-playlist"], True))
            search_targets.append((f"scsearch1:{base_query}", ["--no-playlist"], False))
        else:
            search_targets.append((f"ytsearch1:{base_query}", ["--no-playlist"], True))

        last_err = ""
        for target, extra_flags, is_yt in search_targets:
            _clear_partial_downloads(out_dir)
            cmd = [ytdlp, "--no-warnings", "--no-embed-metadata"] + extra_flags
            if is_yt:
                cmd += ["--extractor-args", "youtube:player_client=android,web_embedded"]
            cmd += cookies + ffmpeg_loc
            if kind == "audio":
                dl = cmd + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                            "-o", out_tmpl, target]
            else:
                dl = cmd + ["-f", "bv*[height<=720]+ba/b[height<=720]",
                            "--merge-output-format", "mp4", "-o", out_tmpl, target]
            rc, _out, err = _run_yt(dl, timeout=240)
            # Note: --max-downloads 1 exits with code 101 in yt-dlp after writing the file!
            if rc not in (0, 101):
                rc, _out, err = _run_yt(dl + relaxed, timeout=240)
            try:
                if os.path.isfile(_find_output(out_dir)):
                    actual_dur = sp_dur
                    if not actual_dur:
                        try:
                            actual_dur = _duration_seconds(_find_output(out_dir))
                        except Exception:
                            actual_dur = None
                    return _finalize_result(
                        display_title,
                        actual_dur,
                        sp_artist or "Spotify",
                        meta.get("canonical_url") or norm_url,
                    )
            except RuntimeError:
                pass
            last_err = err or last_err

        hint = _err_hint(last_err)
        raise RuntimeError(
            f"Could not download playable audio for Spotify track '{display_title}'."
            + (f" {last_err.strip()}" if last_err.strip() else "")
            + (f"\n{hint}" if hint else "")
        )

    # ---- Branch 2: SoundCloud link -------------------------------------------
    if _is_soundcloud_url(norm_url):
        parsed_sc = urllib.parse.urlsplit(norm_url)
        is_sc_set = "/sets/" in (parsed_sc.path or "").lower() and "in=" not in (parsed_sc.query or "")
        playlist_flags = ["--playlist-items", "1"] if is_sc_set else ["--no-playlist"]
        sc_base = [ytdlp, "--no-warnings", "--no-embed-metadata"] + playlist_flags + cookies + ffmpeg_loc

        rc, out, err = _run_yt(
            sc_base + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", norm_url],
            timeout=60,
        )
        if rc != 0:
            rc, out, err = _run_yt(
                sc_base + relaxed + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", norm_url],
                timeout=60,
            )
        first_line = (out.strip().splitlines() or [""])[0]
        parts = (first_line.split("\t") + ["", "", ""])[:3]
        title, duration, uploader = parts[0], parts[1], parts[2]

        if rc == 0:
            _clear_partial_downloads(out_dir)
            if kind == "audio":
                dl = sc_base + [
                    "-f", "http_mp3_1_0/hls_mp3_1_0/bestaudio/best",
                    "-x", "--audio-format", "mp3", "--audio-quality", "0",
                    "-o", out_tmpl, norm_url,
                ]
            else:
                dl = sc_base + [
                    "-f", "bv*[height<=720]+ba/b[height<=720]/best",
                    "--merge-output-format", "mp4", "-o", out_tmpl, norm_url,
                ]
            dl_rc, _dl_out, dl_err = _run_yt(dl, timeout=240)
            if dl_rc != 0:
                dl_rc, _dl_out, dl_err = _run_yt(dl + relaxed, timeout=240)
            err = dl_err or err
            if dl_rc == 0:
                # Guard against 30s SoundCloud Go+ preview stubs when full track is on YT
                out_file = _find_output(out_dir)
                try:
                    got_dur = _duration_seconds(out_file)
                except Exception:
                    got_dur = 0.0
                if not (kind == "audio" and 28.0 <= got_dur <= 31.0 and title):
                    return _finalize_result(title, duration or got_dur, uploader, norm_url)
                # Try finding the full non-preview track if SoundCloud only served a 30s Go+ snippet
                if title:
                    fb_dir = os.path.join(out_dir, "_sc_full")
                    os.makedirs(fb_dir, exist_ok=True)
                    fb_tmpl = os.path.join(fb_dir, "download.%(ext)s")
                    q = f"{uploader} - {title}".strip(" -") if uploader else title
                    fb_cmd = (
                        [ytdlp, "--no-warnings", "--no-playlist", "--no-embed-metadata",
                         "--extractor-args", "youtube:player_client=android,web_embedded",
                         "--match-filter", "duration >= 45"]
                        + cookies + ffmpeg_loc
                        + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                           "-o", fb_tmpl, f"ytsearch1:{q} official audio"]
                    )
                    fb_rc, _, _ = _run_yt(fb_cmd, timeout=180)
                    if fb_rc == 0:
                        try:
                            full_file = _find_output(fb_dir)
                            full_dur = _duration_seconds(full_file)
                            os.replace(full_file, out_file)
                            return _finalize_result(title, full_dur, uploader, norm_url)
                        except Exception:
                            pass
                return _finalize_result(title, duration or got_dur, uploader, norm_url)

        # Fallback if SoundCloud blocked/geo-gated the track but we resolved or can infer title
        slug_query = f"{uploader} - {title}".strip(" -") if title else ""
        if not slug_query:
            segs = [seg for seg in (parsed_sc.path or "").split("/") if seg and seg != "sets"]
            if len(segs) >= 2:
                slug_query = f"{segs[0].replace('-', ' ')} - {segs[-1].replace('-', ' ')}"
        if slug_query and kind == "audio":
            _clear_partial_downloads(out_dir)
            fb_cmd = (
                [ytdlp, "--no-warnings", "--no-playlist", "--no-embed-metadata",
                 "--extractor-args", "youtube:player_client=android,web_embedded"]
                + cookies + ffmpeg_loc
                + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                   "-o", out_tmpl, f"ytsearch1:{slug_query} audio"]
            )
            fb_rc, _, _ = _run_yt(fb_cmd, timeout=200)
            if fb_rc == 0:
                try:
                    out_file = _find_output(out_dir)
                    fb_dur = _duration_seconds(out_file)
                    return _finalize_result(title or slug_query, fb_dur, uploader or "SoundCloud", norm_url)
                except Exception:
                    pass

        hint = _err_hint(err)
        raise RuntimeError(
            "yt-dlp could not download that SoundCloud link."
            + (f" {err.strip()}" if (err or "").strip() else "")
            + (f"\n{hint}" if hint else "")
        )

    # ---- Branch 3: YouTube & general yt-dlp URLs -----------------------------
    base = [
        ytdlp, "--no-warnings", "--no-playlist", "--no-embed-metadata",
        "--extractor-args", "youtube:player_client=android,web_embedded",
    ] + cookies + ffmpeg_loc

    def _probe(cmd):
        return _run_yt(cmd, timeout=60)

    rc, out, err = _probe(
        base + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", norm_url])
    if rc != 0:
        rc, out, err = _probe(
            base + relaxed + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", norm_url])
    if rc != 0:
        hint = _err_hint(err)
        raise RuntimeError("yt-dlp could not read that link."
                           + (f" {err.strip()}" if err.strip() else "")
                           + (f"\n{hint}" if hint else ""))
    parts = (out.strip().split("\t") + ["", "", ""])[:3]
    title, duration, uploader = parts[0], parts[1], parts[2]

    _clear_partial_downloads(out_dir)
    if kind == "audio":
        dl = base + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                     "-o", out_tmpl, norm_url]
    else:
        dl = base + ["-f", "bv*[height<=720]+ba/b[height<=720]",
                     "--merge-output-format", "mp4", "-o", out_tmpl, norm_url]
    rc, _out, err = _run_yt(dl, timeout=240)
    if rc != 0:
        rc, _out, err = _run_yt(dl + relaxed, timeout=240)
    if rc != 0:
        hint = _err_hint(err)
        raise RuntimeError("yt-dlp download failed."
                           + (f" {err.strip()}" if err.strip() else "")
                           + (f"\n{hint}" if hint else ""))

    return _finalize_result(title, duration, uploader, norm_url)

