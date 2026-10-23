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

## `CopyR8 AutoFit V4.txt` -- eleven-step clean stereo spectral-carve chain

V4 now shapes only the source audio. The synthetic high-frequency ribbons,
flanger/phaser sweeps, vibrato, and treble boosts have been removed from both
the base render and final export. The replacement uses static subtractive EQ,
a treble taper, light compression, and soft limiting. Both channels receive the
same EQ, preserving the stereo image without destructive mono phase tricks.
Pitch/tempo changes remain the job of the existing tested shift-search layer,
not an extra hidden adjustment in the macro.

1. Select All
2. Clip Fix (`gain=-4`, `threshold=92`)
3. Notch Filter (`980 Hz`, `Q=5`)
4. Notch Filter (`1720 Hz`, `Q=4.5`)
5. Notch Filter (`2680 Hz`, `Q=4.5`)
6. Notch Filter (`3900 Hz`, `Q=4`)
7. Notch Filter (`5450 Hz`, `Q=4`)
8. Notch Filter (`7350 Hz`, `Q=3.5`)
9. Bass and Treble (`Bass=-1.5`, `Treble=-2.5`)
10. Compressor (`Ratio=1.6:1`, `Threshold=-16 dB`, `Normalize=0`)
11. Limiter (`SoftLimit`, `thresh=-0.8 dB`, `hold=8 ms`)

Before recognition probes and at export, ffmpeg applies Blackman-Harris
zero-phase cuts at `980, 1720, 2680, 3900, 5450, 7350, 9650, 12350 Hz`,
with no positive EQ gain and a tapered ceiling above `14.5 kHz`. There are no
tone/noise generators or time-varying modulation branches. The spectrogram is
still rendered from the actual exported audio, not a decorative overlay.

## Scope and defaults

Only **`/copyr8` -> `Auto-Fit V3 (macro + mixed)`** uses the V3 file, and only
**`/copyr8` -> `Auto-Fit V4 (new macro)`** uses the V4 file. When Audacity is
unavailable on the host, Auto-Fit V4 runs its built-in stereo studio and
zero-phase spectral sculpting chain via `ffmpeg` before searching clean shifts.

No `.env` change is required: an unset or blank `COPYR8_V3_MACRO` /
`COPYR8_V4_MACRO` selects the bundled chain. A non-empty override still wins
with a custom macro name or path.

## Windows host setup

The Audacity integration targets **Audacity 2.3.3 on Windows**:

- Install Audacity; set `AUDACITY_EXE` if it is not in the standard location.
- Enable `mod-script-pipe` in **Edit -> Preferences -> Modules**, then restart
  Audacity.
- V3 needs **Blue Cat's Triple EQ (stereo)** and **TdrNova** installed in
  Audacity (`BcTripleEq4(stereo)` and `TdrNova`). V4 uses built-in Audacity
  2.3.3 effects and bundled Nyquist filters (`ClipFix`, `NotchFilter`,
  `BassAndTreble`, `Compressor`, `Limiter`).
- Ensure `%APPDATA%\Audacity\Macros` is writable.

## Tests

Run `python -m unittest -v test_copyr8_macro test_v4_audio` from the repository
root. The signal tests render real audio and check for added high-frequency
energy, level wobble, channel/mono damage, silence leakage, and spectral cuts.
