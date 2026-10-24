"""Smoke test for the /copyr8 auto-fit engines (killa_tools).

Shazam's API and ffprobe are unavailable in this sandbox, so:
  * `_shazam_match_count` is monkeypatched with scripted fakes (and once with
    a fake shazamio.Shazam to test the parse rules),
  * `ffprobe_json` is monkeypatched (duration/sample rate from soundfile),
  * `_throttle`/`_mark_rate_limited` sleeps are stubbed out.
AcoustID calls/setup are also stubbed; test_acoustid.py covers dual-provider rules.
Everything else -- ffmpeg rendering (full and per-clip probes), the
ladder/walk/mixed V3 pools, the acceptance rules, the control check, the
tremolo overlay, the loudness pass, the spectrogram, the fixed presets --
runs for real.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ROBLOX_COOKIE", "")

import imageio_ffmpeg  # noqa: E402
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
os.makedirs("/tmp/ffbin", exist_ok=True)
link = "/tmp/ffbin/ffmpeg"
if not os.path.exists(link):
    os.symlink(FFMPEG, link)
os.environ["PATH"] = "/tmp/ffbin:" + os.environ["PATH"]

import killa_tools as kt  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

WORK = "/tmp/copyr8_test2"
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)

sr = 44100
t = np.linspace(0, 30, sr * 30, endpoint=False)
y = 0.35 * np.sin(2 * np.pi * 220 * t) + 0.25 * np.sin(2 * np.pi * 277 * t)
y += 0.15 * np.sin(2 * np.pi * 330 * t)
src = os.path.join(WORK, "song.wav")
sf.write(src, y.astype(np.float32), sr)


def fake_ffprobe_json(path):
    info = sf.info(path)
    return {
        "format": {"duration": str(info.duration), "bit_rate": "192000"},
        "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le",
                     "sample_rate": str(info.samplerate), "channels": 1}],
    }

kt.ffprobe_json = fake_ffprobe_json
kt._throttle = lambda progress=None: None
kt._mark_rate_limited = lambda progress=None: None
kt._mark_ok = lambda: None
kt.acoustid_backend.validate_setup = lambda *a, **k: None
# is_available unpacks validate_setup's (None, None); with the stub above it
# would crash on the unpack, so it is stubbed too -- True, so the probe sweep
# keeps its AcoustID entry (match_count is stubbed to 0) like SongRec's does.
kt.acoustid_backend.is_available = lambda: True
kt.acoustid_backend.match_count = lambda path, progress=None: 0
kt.songrec_backend.validate_setup = lambda *a, **k: None
kt.songrec_backend.is_available = lambda: True
kt.songrec_backend.match_count = lambda path, progress=None: 0

# ---- 1. window helpers ----------------------------------------------------
w = kt._window_points(30.0, n=8)
assert len(w) == 8 and w[0] == 0.9 and w[-1] == 17.1, w
assert kt._window_points(10.0) == [0.0]
wg = kt._gapfree_windows(src)
assert len(wg) == 3
assert wg[0] == 0.0 and wg[-1] + 12.0 >= 30.0 - 0.05, wg  # head AND tail covered
assert all(b < a + 12.0 for a, b in zip(wg, wg[1:])), wg   # overlapping, not abutting
print("1. windows OK:", w[:3], "... gapfree:", wg)

# ---- 2. ShazamIO parse rules ---------------------------------------------
try:
    import shazamio  # noqa: E402
except ImportError:
    import types as _types
    shazamio = _types.ModuleType("shazamio")
    sys.modules["shazamio"] = shazamio

class FakeShazam:
    answer = {}
    def __init__(self, *a, **k):
        pass
    async def recognize(self, path):
        return self.answer
    def close(self):
        pass

shazamio.Shazam = FakeShazam
real_match_count = kt._shazam_match_count
FakeShazam.answer = {"matches": []}
assert real_match_count(src) == 0
FakeShazam.answer = {"matches": [{"offset": 1.0}, {"offset": 2.0}]}
assert real_match_count(src) == 2
FakeShazam.answer = {}
assert real_match_count(src) is None
FakeShazam.answer = {"matches": "bogus"}
assert real_match_count(src) is None
print("2. ShazamIO parse rules OK (matches list only, throttle != clean)")

# ---- 3. probe sweep + control check --------------------------------------
calls = {"plan": []}

def scripted(clip, progress=None):
    return calls["plan"].pop(0)

kt._shazam_match_count = scripted
calls["plan"] = [0, 0, 0, 0, 0, 0, 0, 0]
ok, counts = kt._probe_sweep(src, kt._window_points(30.0, n=8))
assert ok and counts == [0] * 10  # eight Shazam windows + AcoustID + SongRec
calls["plan"] = [0, 0, 1, 0, 0, 0, 0, 0]
ok, counts = kt._probe_sweep(src, kt._window_points(30.0, n=8))
assert not ok and counts == [0, 0, 1]
calls["plan"] = [None, 0]
ok, counts = kt._probe_sweep(src, kt._window_points(30.0, n=8))
assert not ok and counts == [None]
print("3. probe sweep (all-zero / early-exit / inconclusive) OK")

kt.acoustid_backend.match_count = lambda path, progress=None: 1 if path == src else 0
kt.songrec_backend.match_count = lambda path, progress=None: 0
calls["plan"] = [0, 0, 2, 0, 0, 0, 0, 0]
assert kt._control_check(src) is True           # Shazam alone verifies
calls["plan"] = [0] * 8
assert kt._control_check(src) is True           # AcoustID alone verifies
kt.acoustid_backend.match_count = lambda path, progress=None: 0
kt.songrec_backend.match_count = lambda path, progress=None: 1 if path == src else 0
calls["plan"] = [0] * 8
assert kt._control_check(src) is True           # SongRec alone verifies
kt.songrec_backend.match_count = lambda path, progress=None: 0
calls["plan"] = [0] * 8
assert kt._control_check(src) is False          # none of the three knows the original
calls["plan"] = [None]
assert kt._control_check(src) is None           # nothing matched + inconclusive
kt.acoustid_backend.match_count = lambda path, progress=None: 1 if path == src else 0
print("4. control check OK (any provider verifies)")

# ---- 4. find_undetectable (V1 fixed ladder) -------------------------------
state = {"blind_after": 2}
rendered = {}

orig_shifted = kt._probe_sweep_shifted

def spy_shifted(inp, windows, pitch, tempo, depth=0.0, rate=0.0, progress=None):
    rendered["rung"] = rendered.get("rung", 0) + 1
    rendered["last"] = (pitch, tempo)
    rendered["depth_now"] = depth
    return orig_shifted(inp, windows, pitch, tempo, depth, rate, progress)

kt._probe_sweep_shifted = spy_shifted

orig_overlay = kt._render_overlay

def spy_overlay(inp, out, depth, rate):
    rendered["depth_now"] = depth
    return orig_overlay(inp, out, depth, rate)

kt._render_overlay = spy_overlay

def v1_probe(clip, progress=None):
    if "song.wav" in clip:
        return 1
    return 1 if rendered["rung"] <= state["blind_after"] else 0

kt._shazam_match_count = v1_probe
out_wav = os.path.join(WORK, "v1_out.wav")
log = []
res = kt.find_undetectable(src, out_wav, progress=log.append)
assert res["verified"] is True
assert (res["pitch"], res["tempo"]) == (-0.5, 1.28), res
assert res["counts"] is not None and os.path.isfile(out_wav)
assert "testing uniform shift -0.5 st / +22%..." in log[0], log[0]
assert any("control ok" in x for x in log)
print("5. find_undetectable ladder OK:", res["pitch"], res["tempo"],
      "verified =", res["verified"])

def v1_blind_original(clip, progress=None):
    return 0
kt._shazam_match_count = v1_blind_original
kt.acoustid_backend.match_count = lambda path, progress=None: 0
res = kt.find_undetectable(src, out_wav, progress=log.append)
assert res["verified"] is False and (res["pitch"], res["tempo"]) == (2.0, 1.07), res
assert os.path.isfile(out_wav)
kt.acoustid_backend.match_count = lambda path, progress=None: 1 if path == src else 0
print("6. find_undetectable verified=False -> balanced protection (+2.0/1.07) OK")

def v1_all_match(clip, progress=None):
    return 1
kt._shazam_match_count = v1_all_match
res = kt.find_undetectable(src, out_wav, progress=log.append)
assert res["counts"] is None and (res["pitch"], res["tempo"]) == (-1.0, 1.30)
print("7. find_undetectable fallback OK: strongest (-1.0/1.30), clean =",
      res["counts"] is not None)

def v1_none(clip, progress=None):
    return None
kt._shazam_match_count = v1_none
try:
    kt.find_undetectable(src, out_wav, progress=log.append)
except RuntimeError as e:
    assert "never returned a usable answer" in str(e)
    print("8. find_undetectable inconclusive-everywhere raises OK")
else:
    raise AssertionError("should have raised")

# ---- 5. find_mildest (V2 walk) -------------------------------------------
rungs = kt._mildest_rungs()
print(f"   walk has {len(rungs)} settings; first 6:")
for r in rungs[:6]:
    print("     ", r)
assert rungs[0][1:3] == (-0.1, 1.05), rungs[0]
tiers = [0 if t <= kt.MILDEST_TEMPO_CAP else 1 for _c, _p, t, _d, _r in rungs]
assert tiers == sorted(tiers), "tier-1 must all come before tier-2"
assert any(d > 0 for _c, _p, _t, d, _r in rungs), "wobble probe missing"
wob = next(r for r in rungs if r[3] > 0)
assert abs(wob[0] - 0.8) < 1e-9 and wob[1:3] == (-0.1, 1.05) and (wob[3], wob[4]) == (0.10, 4.0), wob
print("9. _mildest_rungs order OK (start -0.1/1.05, cap tiering, wobble @0.8)")

state2 = {"gate": 1.0, "order": []}

def v2_gate(clip, progress=None):
    if "song.wav" in clip:
        return 1
    if ".trem." in clip:
        return 1
    pitch, tempo = rendered["last"]
    cost = abs(pitch) + 10.0 * abs(tempo - 1.0) + 2.0 * rendered.get("depth_now", 0.0)
    state2["order"].append((pitch, tempo, round(cost, 3)))
    return 1 if cost < state2["gate"] else 0

kt._shazam_match_count = v2_gate
out2 = os.path.join(WORK, "v2_out.wav")
log2 = []
res = kt.find_mildest(src, out2, progress=log2.append, overlay_tremolo=[(0.06, 1.0)])
assert res["verified"] is True
assert res["cost"] >= 0.95, res
assert res["overlay_applied"] is False
assert res["overlay_reason"] == ("the tremolo version was still recognised -- "
                                 "keeping the pure shift")
assert "searching" in log2[0] and "closest-sounding first" in log2[0]
assert "testing -0.10 st / +5.0%..." in log2[1], log2[1]
costs = [c for _p, _t, c in state2["order"]]
assert costs == sorted(costs), costs
print("10. find_mildest mildest-first OK: cost", res["cost"],
      "->", res["pitch"], res["tempo"])

state2["order"] = []

def v2_tier(clip, progress=None):
    if "song.wav" in clip:
        return 1
    if ".trem." in clip:
        return 1
    pitch, tempo = rendered["last"]
    state2["order"].append(tempo)
    return 1 if tempo <= kt.MILDEST_TEMPO_CAP else 0

kt._shazam_match_count = v2_tier
res = kt.find_mildest(src, out2, progress=log2.append, use_tremolo=False)
assert res["tempo"] > kt.MILDEST_TEMPO_CAP, res
first_faster = next(i for i, t in enumerate(state2["order"]) if t > kt.MILDEST_TEMPO_CAP)
pre = state2["order"][:first_faster]
assert pre and all(t <= kt.MILDEST_TEMPO_CAP for t in pre), pre
print("11. find_mildest tier walk OK: exhausted tempo<=1.081 first, then",
      res["pitch"], res["tempo"])

def v2_overlay_ok(clip, progress=None):
    if "song.wav" in clip:
        return 1
    pitch, tempo = rendered["last"]
    cost = abs(pitch) + 10.0 * abs(tempo - 1.0) + 2.0 * rendered.get("depth_now", 0.0)
    return 1 if cost < 1.0 else 0

kt._shazam_match_count = v2_overlay_ok
res = kt.find_mildest(src, out2, progress=log2.append,
                      overlay_tremolo=[(0.06, 1.0), (0.10, 2.0)])
assert res["overlay_applied"] is True and res["tremolo_depth"] == 0.06
assert abs(res["cost"] - (abs(res["pitch"]) + 10 * (res["tempo"] - 1) + 0.12)) < 1e-6
assert any("tremolo verified: clean on all" in x for x in log2)
print("12. find_mildest overlay OK: tremolo 0.06@1Hz verified, cost", res["cost"])

def v2_all_match(clip, progress=None):
    return 1
kt._shazam_match_count = v2_all_match
res = kt.find_mildest(src, out2, progress=log2.append, use_tremolo=False)
assert res["counts"] is None and (res["pitch"], res["tempo"]) == (-0.5, 1.22)
print("13. find_mildest fallback OK: strongest (-0.5/1.22), clean = False")

# ---- 6. V1/V2 wrappers end-to-end ----------------------------------------
kt._shazam_match_count = v1_probe
rendered["rung"] = 0
mp3 = os.path.join(WORK, "wrap1.mp3")
spec = os.path.join(WORK, "wrap1.png")
res = kt.copyr8_autofit(src, mp3, spec, progress=log.append)
assert os.path.isfile(mp3) and os.path.isfile(spec)
assert res["clean"] is True and res["verified"] is True
assert res["loudness"]["out_lufs"] is not None
assert res["info"]["duration"]
print("14. copyr8_autofit wrapper OK:", res["pitch"], res["tempo"],
      "| out", round(res["loudness"]["out_lufs"], 1), "LUFS vs source",
      round(res["loudness"]["source_lufs"], 1))

mp3b = os.path.join(WORK, "wrap2.mp3")
specb = os.path.join(WORK, "wrap2.png")
kt._shazam_match_count = v2_overlay_ok
res = kt.copyr8_autofit_v2(src, mp3b, specb, progress=log2.append)
assert os.path.isfile(mp3b) and os.path.isfile(specb)
assert res["clean"] is True and res["verified"] is True
assert res["tremolo_applied"] is True and res["tremolo_reason"] is None
assert res["cost"] is not None and res["level_gain_db"] == 0.0
assert res["loudness"]["out_lufs"] is not None
print("15. copyr8_autofit_v2 wrapper OK:", res["pitch"], res["tempo"],
      "cost", res["cost"], "| tremolo", res["tremolo_depth"], "@",
      res["tremolo_rate"], "Hz")

# ---- 7. fixed presets + Auto-Fit V3 (macro first, preset adjust) ----------
assert kt.COPYR8_PRESETS == {"subtle": (1.5, 1.04), "balanced": (2.0, 1.07),
                             "stealth": (2.5, 1.10), "aggressive": (3.0, 1.12)}
mp3p = os.path.join(WORK, "preset.mp3")
specp = os.path.join(WORK, "preset.png")
info = kt.copyr8_process(src, mp3p, specp, "subtle")
assert os.path.isfile(mp3p) and os.path.isfile(specp) and info.get("lufs") is not None
print("16. copyr8_process presets OK (subtle: +1.5 st / 1.04x)")

mixed = kt._mixed_v3_rungs()
costs_m = [c for c, _p, _t, _d, _r in mixed]
assert costs_m == sorted(costs_m)
assert mixed[-1][1:3] == (-1.0, 1.30), mixed[-1]
assert len(mixed) >= 45, len(mixed)
sump = [r for r in mixed if r[1:3] == (-0.5, 1.22)]
assert len(sump) == 1, sump
adj = kt._v3_adjust_rungs()
assert adj[0][1:3] == (0.25, 1.01), adj[0]                    # mildest adjustment
assert all(any(r[1:3] == kt.COPYR8_PRESETS[k] for r in adj) for k in kt.COPYR8_PRESETS)
assert adj[-1][1:3] == (-1.0, 1.30)                           # escalation tail
print("17. pools OK:", len(mixed), "mixed settings /", len(adj),
      "adjustments, presets on the line, tail", adj[-1][1:3])

def fake_macro(in_path, out_path, prog):
    shutil.copy(in_path, out_path)
    return {"macro": "test", "steps": ["eq", "compress"], "metadata": True}

probes = {"seen": []}

def v3_base_clean(clip, progress=None):
    probes["seen"].append(clip)
    if "song.wav" in clip:
        return 1
    return 0

kt._shazam_match_count = v3_base_clean
mp3v = os.path.join(WORK, "v3a.mp3")
specv = os.path.join(WORK, "v3a.png")
logv = []
res = kt.copyr8_autofit_v3(src, mp3v, specv, progress=logv.append, macro_fn=fake_macro)
assert os.path.isfile(mp3v) and os.path.isfile(specv)
assert res["macro"]["applied"] is True and res["macro"]["metadata"] is True
assert res["pitch"] == 0.0 and res["tempo"] == 1.0 and res["cost"] == 0.0
assert res["clean"] is True and res["verified"] is True
assert not any("shift_probe" in c for c in probes["seen"])
print("18. copyr8_autofit_v3 base-clean OK: macro kept, pitch 0 / cost 0")

probes["seen"] = []

def v3_walk(clip, progress=None):
    probes["seen"].append(clip)
    if "song.wav" in clip:
        return 1
    if "autofit_v3_base" in clip:
        return 1
    if ".trem." in clip:
        return 1
    pitch, tempo = rendered["last"]
    cost = abs(pitch) + 10.0 * abs(tempo - 1.0) + 2.0 * rendered.get("depth_now", 0.0)
    return 1 if cost < 1.0 else 0

kt._shazam_match_count = v3_walk
res = kt.copyr8_autofit_v3(src, mp3v, specv, progress=logv.append, macro_fn=fake_macro)
assert res["verified"] is True and res["macro"]["applied"] is True
assert (res["pitch"], res["tempo"]) == (0.75, 1.03), res     # 3rd adjustment clears
assert any("preset adjustments" in x for x in logv)
assert any("shift_probe" in c for c in probes["seen"])
print("19. copyr8_autofit_v3 preset-adjust OK: 0.75 st / 1.03x clears after",
      "per-attempt API tests")

logv = []
kt._shazam_match_count = v1_all_match
res = kt.copyr8_autofit_v3(src, mp3v, specv, progress=logv.append, macro_fn=None)
assert res["macro"]["applied"] is False
assert res["macro"]["reason"].startswith("no macro configured")
assert (res["pitch"], res["tempo"]) == (-1.0, 1.30)
assert res["counts"] is None and res["clean"] is False
print("20. copyr8_autofit_v3 no-macro fallback OK: escalates to (-1.0/1.30)")

# ---- 7b. V3 must never ship the untouched original ------------------------
# Neither service knows the track (every probe 0, AcoustID 0 on the original).
kt._shazam_match_count = lambda clip, progress=None: 0
kt.acoustid_backend.match_count = lambda path, progress=None: 0
kt.songrec_backend.match_count = lambda path, progress=None: 0
logv = []
res = kt.copyr8_autofit_v3(src, mp3v, specv, progress=logv.append, macro_fn=None)
assert res["macro"]["applied"] is False
assert (res["pitch"], res["tempo"]) != (0.0, 1.0), res
assert (res["pitch"], res["tempo"]) == (2.0, 1.07) and res["verified"] is False, res
assert any("never shipped unshifted" in x for x in logv)
print("20b. V3 no-macro + unverified OK: protection shift, never 0.0 st / 1.00x")

logv = []
res = kt.copyr8_autofit_v3(src, mp3v, specv, progress=logv.append, macro_fn=fake_macro)
assert res["macro"]["applied"] is True
assert (res["pitch"], res["tempo"]) == (2.0, 1.07) and res["verified"] is False, res
assert abs(sf.info(mp3v).duration - 30.0 / 1.07) < 0.5, sf.info(mp3v).duration
print("20c. V3 macro base clean but unverified OK: protection shift on the macro output")
kt.acoustid_backend.match_count = lambda path, progress=None: 1 if path == src else 0

# ---- 8. filtration coverage: head/tail hooks must be flagged -------------
# A recognisable passage in the intro or the fade-out used to sit in the
# sweep's blind zones -- the gap-free grid inherited the sparse sampler's 5%
# inset, and shifted probe clips were cut with output-side -ss/-t while the
# atempo chain re-timed the stream (the window at X actually probed X*tempo
# onward) -- so both commands mis-flagged the run "clean".
sr2, dur2 = 22050, 60.0
t2 = np.linspace(0, dur2, int(sr2 * dur2), endpoint=False)
body2 = 0.10 * np.sin(2 * np.pi * 220 * t2)

def hook_file(name, t0, t1):
    y2 = body2.copy()
    m2 = (t2 >= t0) & (t2 < t1)
    y2[m2] = 0.9 * np.sin(2 * np.pi * 440 * t2[m2])
    p2 = os.path.join(WORK, name)
    sf.write(p2, y2.astype(np.float32), sr2)
    return p2

def hook_probe(clip, progress=None):
    # Fake filter: the clip is still caught whenever it contains the hook.
    data, _ = sf.read(clip)
    return 1 if (len(data) and float(np.max(np.abs(data))) > 0.5) else 0

kt._shazam_match_count = hook_probe
kt.acoustid_backend.match_count = lambda path, progress=None: 0
kt.songrec_backend.match_count = lambda path, progress=None: 0

# 8a. TAIL hook (58-59.5s): V3's base sweep must not ship the base "clean".
tail = hook_file("hooked_tail.wav", 58.0, 59.5)
real_rungs = kt._v3_adjust_rungs
kt._v3_adjust_rungs = lambda: [(0.5, -0.2, 1.10, 0.0, 0.0)]
try:
    mp3h = os.path.join(WORK, "hook_tail.mp3")
    spech = os.path.join(WORK, "hook_tail.png")
    res = kt.copyr8_autofit_v3(tail, mp3h, spech, progress=log.append,
                               macro_fn=fake_macro)
    assert res["clean"] is False and res["counts"] is None, res
    assert res["macro"]["applied"] is True
    assert (res["pitch"], res["tempo"]) == (-0.2, 1.10), res  # walk escalated
    assert os.path.isfile(mp3h) and os.path.isfile(spech)
finally:
    kt._v3_adjust_rungs = real_rungs
print("21. V3 tail-hook flagged OK: base rejected, walk escalated, clean = False")

# 8b. HEAD hook (0.5-2.2s) on a tempo-1.10 rung: the probe at the intro must
# really sample the intro (old output-side seek probed start*1.10 onward).
head = hook_file("hooked_head.wav", 0.5, 2.2)
out21 = os.path.join(WORK, "hook_head.wav")
res = kt.find_mildest(head, out21, progress=log.append, use_tremolo=False,
                      rungs=[(0.5, -0.2, 1.10, 0.0, 0.0)])
assert res["counts"] is None, res   # counts None == the wrapper's clean=False
assert (res["pitch"], res["tempo"]) == (-0.2, 1.10), res
assert os.path.isfile(out21)
print("22. V2 head-hook flagged OK: intro probe aligned, clean = False")

# ---- 9. Auto-Fit V4: original macro + spectral add-on + V3's search -------
def v4_base_clean(clip, progress=None):
    probes["seen"].append(clip)
    if "song.wav" in clip:
        return 1
    return 0

kt._shazam_match_count = v4_base_clean
mp34 = os.path.join(WORK, "v4a.mp3")
spec4 = os.path.join(WORK, "v4a.png")
probes["seen"] = []
log4 = []
res = kt.copyr8_autofit_v4(src, mp34, spec4, progress=log4.append, macro_fn=fake_macro)
assert os.path.isfile(mp34) and os.path.isfile(spec4)
assert res["macro"]["applied"] is True and res["macro"]["metadata"] is True
assert res["pitch"] == 0.0 and res["tempo"] == 1.0 and res["cost"] == 0.0
assert res["clean"] is True and res["verified"] is True
assert any("autofit_v4_base" in c for c in probes["seen"]), probes["seen"][:3]
assert not any("autofit_v3_base" in c for c in probes["seen"])
print("23. copyr8_autofit_v4 base-clean OK: V4 macro base probed, shipped at 0/0")

log4 = []
kt._shazam_match_count = v1_all_match
res = kt.copyr8_autofit_v4(src, mp34, spec4, progress=log4.append, macro_fn=None)
assert res["macro"]["applied"] is False
assert res["macro"]["reason"].startswith("no macro configured")
assert "COPYR8_V4_MACRO" in res["macro"]["reason"], res["macro"]["reason"]
assert (res["pitch"], res["tempo"]) == (-0.20, 0.84)
assert res["clean"] is False
assert abs(sf.info(mp34).duration - sf.info(src).duration / 0.84) < 0.12
print("24. copyr8_autofit_v4 no-macro fallback OK: stays slower-only (-0.20/0.84)")

print()
print("ALL ENGINE TESTS PASSED")
