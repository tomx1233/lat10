# CopyR8 Auto-Fit macros (V3 and V4)

## `CopyR8 AutoFit V3.txt` -- supplied seven-step chain

`CopyR8 AutoFit V3.txt` contains the supplied seven-step Audacity chain, with
its original command names, parameter values, and order preserved:

1. Clip Fix (`gain=-10.5`, `threshold=85`)
2. Change Tempo (`+2.5%`)
3. Change Pitch (`+1.4%`, **not semitones**)
4. Bass and Treble (`Bass=-17`)
5. Blue Cat's Triple EQ (stereo)
6. TDR Nova
7. Bass and Treble (`Treble=-17`)

## `CopyR8 AutoFit V4.txt` -- supplied seventeen-step mono wobble chain

`CopyR8 AutoFit V4.txt` contains the supplied chain, with its original
command names, parameter values, and order preserved:

1. Select All
2. Stereo to Mono
3. Duplicate
4. Select Track 1 (0-based), Pan Right
5. Select Track 0, Pan Left
6. Select Track 1, Tremolo (Sine, 4 Hz, start phase 60, wet 50%)
7. Select Track 1, Tremolo (Sine, 4 Hz, start phase 0, wet 40%)
8. Select All, Mix and Render (the panned pair becomes one stereo track)
9. Select All, SC1 compressor x2 (same settings both times)

## Scope and defaults

Only **`/copyr8` → `Auto-Fit V3 (macro + mixed)`** uses the V3 file, and only
**`/copyr8` → `Auto-Fit V4 (new macro)`** uses the V4 file. Both run before
their shared mixed search. Auto-Fit, Auto-Fit V2, the four fixed presets, and
all other commands are unchanged.

No `.env` change is required: an unset or blank `COPYR8_V3_MACRO` /
`COPYR8_V4_MACRO` selects the bundled chain. A non-empty override still wins
with a custom macro name/path. Remove an old override if you want a supplied
chain.

## Windows host setup

The existing integration targets **Audacity 2.3.3 on Windows**. On the account
running the bot:

- Install Audacity; set `AUDACITY_EXE` if it is not in the standard location.
- Enable `mod-script-pipe` in **Edit → Preferences → Modules**, then restart
  Audacity.
- V3 additionally needs **Blue Cat's Triple EQ (stereo)** and **TDR Nova**
  installed in Audacity (the macro references `BcTripleEq4(stereo)` and
  `TdrNova`); these plugins are not included with the bot. V4 needs nothing
  extra: SC1 ships with Audacity 2.3.3 as a LADSPA effect (`sc1_1425.dll`).
- Ensure the account's `APPDATA` points to its Windows application-data folder
  and that `%APPDATA%\Audacity\Macros` is writable.
- Pull the bot changes and restart the bot, then select Auto-Fit V3 or V4 in
  `/copyr8`.

On the first run of a fit, the bot installs the bundled file into
`%APPDATA%\Audacity\Macros\` (e.g. `CopyR8 AutoFit V4.txt`) **before launching
hidden Audacity**. Later runs synchronize that bot-managed file only if its
bytes change, restarting the cached Audacity session when needed. Other macro
files are left alone. To customize a chain without automatic synchronization,
save it under a different name and point `COPYR8_V3_MACRO` /
`COPYR8_V4_MACRO` at that name.

The TDR Nova parameter line in V3 exceeds the scripting pipe's per-command
limit, so chains run through Audacity's native **Tools → Apply Macro** runner
rather than sending each line through the pipe. Do not split or truncate
parameter lines.

Both fits retain the same fallback behavior: if Audacity or an effect is
missing, or macro execution fails, they report the reason and search shifts on
the original audio instead. Check the **Macro** field in the reply to confirm
the chain actually ran (7 steps for V3, 17 for V4); a returned file alone does
not confirm that.

## Tests

Run `python -m unittest -v test_copyr8_macro` from the repository root.
These offline tests cover parsing, installation/update behavior, failure
messages, Audacity handoff, configuration, and V3/V4 auto-fit routing.
They mock Audacity; a real end-to-end audio/plugin check must run on the
configured Windows host.
