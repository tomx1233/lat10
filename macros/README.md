# CopyR8 Auto-Fit macros (V3 and V4)

## `CopyR8 AutoFit V3.txt` -- seven-step chain

`CopyR8 AutoFit V3.txt` contains the seven-step Audacity chain, with its
original command names, parameter values, and order preserved:

1. Clip Fix (`gain=-10.5`, `threshold=85`)
2. Change Tempo (`+2.5%`)
3. Change Pitch (`+1.4%`, percentage)
4. Bass and Treble (`Bass=-17`)
5. Blue Cat's Triple EQ (stereo)
6. TDR Nova
7. Bass and Treble (`Treble=-17`)

## `CopyR8 AutoFit V4.txt` -- original seventeen-step macro + spectral add-on

The bundled V4 macro is restored **byte-for-byte to the originally supplied
file**. Its effect order, track layout, tremolo settings, and both SC1 passes
are unchanged. Spectrogram shaping is an **additional engine stage after the
macro**, not a replacement macro or a different compressor/limiter chain.

Original macro steps:

1. Select All
2. Stereo to Mono (factory defaults)
3. Duplicate
4. Select Track 1 (0-based)
5. Pan Right
6. Select Track 0
7. Pan Left
8. Select Track 1
9. Tremolo (`Sine`, `4 Hz`, phase `60`, wet `50%`)
10. Select Track 1
11. Tremolo (`Sine`, `4 Hz`, phase `0`, wet `40%`)
12. Select All
13. Mix and Render
14. Select All
15. SC1 (`attack=400 ms`, `release=800 ms`, `knee=2 dB`, `ratio=10:1`,
    `threshold=-15 dB`, `makeup=5 dB`)
16. Select All
17. SC1 (the same settings again)

### Additional spectrogram shaping

After the complete macro and before recognition probes, ffmpeg applies static
Blackman-Harris zero-phase cuts at `980, 1720, 2680, 3900, 5450, 7350, 9650,
12350 Hz`, with no positive EQ gain and a tapered ceiling above `14.5 kHz`.
The final export reapplies the spectral shape after loudness restoration.
This changes the actual audio spectrum; the PNG is rendered from that export,
not painted with a decorative overlay.

The extra layer does not replace the macro's tremolo or SC1, add another
compressor after a successful macro, or alter its L/R arrangement. There are
no synthesized high-frequency ribbons or added flanger/phaser/vibrato sweeps.
The original macro's intentional 4 Hz tremolo **is preserved**; the search does
not stack another tremolo overlay on top of it.

### V4 slower-tempo / minimal-pitch search

The original 17-step macro and spectral layer are unchanged. Only V4's
additional pitch/tempo search uses the slower approach:

- If the macro plus spectral layer already passes and the original is known,
  keep `0.00 st / 1.00x` (no unnecessary pitch or tempo shift).
- Otherwise try **unchanged pitch first**, testing `0.99x` down to `0.84x` in
  `0.01` steps (1% to 16% slower, least slowdown first).
- Only after all unchanged-pitch slowdowns fail, try tiny downward pitch
  nudges: `-0.05`, `-0.10`, `-0.15`, then `-0.20 st`, with that same slower
  tempo range. No V4 search candidate is faster than the input.
- If no service knows the original, use the explicitly unverified
  `0.00 st / 0.94x` fallback, **not** the old `+2.00 st / 1.07x` preset.
- If every candidate remains recognized, the best-effort fallback stays
  inside V4's slow/minimal-pitch bounds (`-0.20 st / 0.84x`); it is not marked
  clean. If no service gives a usable answer, no file is exported.

Pitch and tempo are processed independently so slowing the tempo does not
also lower the key. Every candidate still uses the existing gap-free probe
windows and API pacing. Auto-Fit V1/V2/V3 and the fixed presets keep their
existing search/preset behavior.

## Scope and defaults

Only **`/copyr8` -> `Auto-Fit V3 (macro + mixed)`** uses the V3 file, and only
**`/copyr8` -> `Auto-Fit V4 (new macro)`** uses the V4 file. Both macros run
before their respective searches; V4 additionally processes the macro output
with its spectral layer and uses the slower-only profile above. V3 and the
other presets are unchanged.

No `.env` change is required: an unset or blank `COPYR8_V3_MACRO` /
`COPYR8_V4_MACRO` selects the bundled chain. A non-empty override still wins
with a custom macro name or path. The next bundled V4 run updates Audacity's
bot-managed macro file and reloads its cached session when needed. Custom
macro files are never overwritten.

If Audacity or SC1 is unavailable, the existing fallback still reports that
the macro did not run and uses the ffmpeg processing/search path. That fallback
is **not** an implementation of the original macro. Check the reply's **Macro**
field: a successful bundled V4 run reports the original **17 steps**.

## Windows host setup

The Audacity integration targets **Audacity 2.3.3 on Windows**:

- Install Audacity; set `AUDACITY_EXE` if it is not in the standard location.
- Enable `mod-script-pipe` in **Edit -> Preferences -> Modules**, then restart
  Audacity.
- V3 needs **Blue Cat's Triple EQ (stereo)** and **TdrNova** installed in
  Audacity (`BcTripleEq4(stereo)` and `TdrNova`). V4 needs its original
  **SC1 / `Sc1` LADSPA effect** enabled, as well as Audacity's track commands
  and Tremolo. The spectral add-on runs separately in ffmpeg.
- Ensure `%APPDATA%\Audacity\Macros` is writable.

## Tests

Run `python -m unittest -v test_copyr8_macro test_v4_audio test_v4_tempo` from the repository
root. Macro tests lock all 17 original V4 commands and parameters and verify
that the restored file is installed before Audacity runs. Signal tests cover
the **additional spectral layer**: actual spectral cuts, no added whine/sweeps,
no extra channel damage, and preservation of existing macro modulation. The
Windows Audacity/SC1 boundary is mocked; live plugin validation needs that host.
