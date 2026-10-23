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

## `CopyR8 AutoFit V4.txt` -- eleven-step stereo studio + spectral comb chain

`CopyR8 AutoFit V4.txt` keeps full stereo imaging and natural tonal balance
(no mono fold-down, no tremolo warble, and no heavy dual-SC1 pumping) while
applying phase movement, room air, narrow high-Q spectral comb notches, gentle
bus compression, and soft limiting before the V4 zero-phase FIR spectral
sculptor and ultrasonic braid run on export:

1. Select All
2. Clip Fix (`gain=-3`, `threshold=95`)
3. Phaser (4-stage, `Freq=0.25`, `Depth=45`, `DryWet=55`, `Feedback=12`)
4. Reverb (short studio air, `RoomSize=16`, `WetGain=-22`, `StereoWidth=32`)
5. Notch Filter (`2550 Hz`, `Q=10`)
6. Notch Filter (`5050 Hz`, `Q=10`)
7. Notch Filter (`8500 Hz`, `Q=10`)
8. Notch Filter (`12950 Hz`, `Q=10`)
9. Bass and Treble (`Treble=+1.5`)
10. Compressor (`Ratio=1.8:1`, `Threshold=-16 dB`, `Normalize=0`)
11. Limiter (`SoftLimit`, `thresh=-0.8 dB`, `hold=8 ms`)

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
  2.3.3 effects and bundled Nyquist filters (`ClipFix`, `Phaser`, `Reverb`,
  `NotchFilter`, `BassAndTreble`, `Compressor`, `Limiter`).
- Ensure `%APPDATA%\Audacity\Macros` is writable.

## Tests

Run `python -m unittest -v test_copyr8_macro` from the repository root.
