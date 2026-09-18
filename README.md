# Sokkia SDL50 level processor

Downloads level data from a Sokkia SDL50 digital level over a USB-serial
adaptor, reduces it, and produces a level report — in one step, replacing the
download-then-FileMaker workflow.

**Prototype: one script, no install.** `sdl_levels.py` is the whole program.
Packaging comes later.

```sh
python sdl_levels.py                            # opens the window: download → report
python sdl_levels.py data/AZM020420.csv         # opens the window onto a file
python sdl_levels.py selftest                   # prove the reduction is faithful
```

Run with no arguments it opens the window, so it works as a double-click or as
the file association for a download. `gui` does the same thing explicitly.

Runs on Windows, macOS and Linux. Only the `download` command needs
`pip install pyserial`; everything else runs on a stock Python 3.9+. The GUI
uses Tkinter, which is bundled with the official Python on Windows and macOS
(on Linux it is usually a separate package, e.g. `sudo apt install python3-tk`).

## Command line

```sh
python sdl_levels.py ports                              # what's plugged in
python sdl_levels.py download --port COM3 -o job.csv    # capture
python sdl_levels.py report job.csv --start-rl 74.614 \
       --surveyor "M. Ankor" --csv levels.csv           # reduce and report
python sdl_levels.py fmexport job.csv --initial 74.614  # legacy format
```

The report is HTML with print rules: open it and use the browser's
**Print → Save as PDF**.

## Status

The reduction logic is a **verified** port of the FileMaker database. Given
the sample job `AZM020420`, it reproduces the database's exported file byte
for byte — 1677 bytes. `selftest` checks that, and it is the contract: if the
output ever moves, the change is wrong.

The serial path has **not yet been run against a real SDL50.** On the first
real download, keep whatever lands in `raw/` whether or not it parses, and
send it back if anything looks off — it is enough to diagnose framing or
flow-control problems offline.

## Serial settings

Defaults are 9600 8-N-1, no flow control. Override with `--baud`,
`--bytesize`, `--parity`, `--stopbits`, `--xonxoff`, `--rtscts`.

The SDL50 pushes data when the operator starts the transfer from the
instrument's menu; the host does not request anything. So the script opens the
port, waits (up to `--start-timeout`, default 3 minutes) for the first byte,
then reads until the line has been quiet for `--idle-timeout` (default 3 s).

**Every capture writes the raw bytes to `raw/` before anything parses them.**
That file is the evidence if a download ever fails to parse, and it can be
replayed through the rest of the script offline. Worth keeping anyway as the
unmodified record of what the instrument sent.

Open questions only a real download can settle: whether the instrument needs
DTR asserted or XON/XOFF handshaking, and whether it always sends this CSV
layout or can send SDR33 depending on a setting.

## The data

The instrument writes one CSV line per observation:

```
SDL50,3310,002050,ANG020420,0,95,,,
0001,0001,1,1,1,23.60,1.5111,100.0000,
```

`index, point_id, ?, ?, shot_type, distance, reading, elevation`, where
`shot_type` is 1 for a backsight and 2 for a forward sighting. The instrument
reduces as it goes, restarting each run from an assumed 100.0000.

Reduction happens in three stages, mirroring the FileMaker scripts:

1. **Classify.** A type-2 reading is a *foresight* if the next record is a
   backsight (the instrument moved) and an *intermediate* if it is another
   type-2 (still shooting from the same setup).
2. **Merge.** Each setup writes two records for the same physical point — a
   foresight from the old setup, a backsight from the new one. They become one
   level-book row. The pair is recognised by the instrument's elevation being
   unchanged across the two. When it *has* changed, the instrument was
   restarted and that row opens a new level run.
3. **Reduce.** Height of collimation:
   `RL = (RL + backsight at the last setup) − this sight`. Intermediates use
   the instrument height but do not carry it forward.

On top of that the script adds what the spreadsheet step used to: real reduced
levels rather than formulas, per-run sums, the arithmetic check
(ΣBS − ΣFS against last RL − first RL), route length from the recorded sight
distances, and misclose against a `12√K` mm allowance (`--allowance`).

Where no starting level is given, the first run falls back to the instrument's
own assumed datum of 100.0000, so an un-benchmarked job still reduces to the
numbers the instrument shows.

## Two things found in the FileMaker database

**A latent bug in `Merge Levels`.** Its condition tests
`Last EL = Reduced Level And Last Point ID = Point ID`, but `Last EL` is a
*global* field while `Last Point ID` beside it is a *per-record* field. After
the script steps to the next record, that second test reads the new record's
own empty field, so it can never be true.

The exported report proves the elevation test alone is the correct behaviour:
in the sample job the operator mis-keyed a point number — a foresight to 0019
followed by a backsight labelled 0020 — and the two were still correctly
merged as one setup, with 0020 vanishing. This port uses the elevation test
only, and keeps the stricter variant behind `build(..., match_point_id=True)`.
**Worth checking the database's field definitions**: if that global flag was
changed at some point, merging there may now behave differently from when the
sample report was produced.

**Number formatting is inconsistent, and it is an artefact.** In the exported
file the backsight column keeps the instrument's original text (`0.2843`,
`1.7840`) because the import writes it straight into the field, while the
foresight and intermediate columns are written by `Set Field` and so come back
out as calculated numbers with leading and trailing zeros stripped (`.9554`,
`1.515`). `fmexport` reproduces this exactly, for parity. The script's own
outputs use a consistent 4 decimal places throughout.

## Open decisions

- **Misclose is reported, not distributed.** No correction is spread through
  the run, because the FileMaker version did not do so and changing that would
  silently move every level. Say the word and it becomes an option.
- **Closing levels are assumed.** Each run is assumed to close back on its own
  starting level, which is what the sample job does (+1.7 mm and +0.1 mm).
  Runs closing on a *different* known benchmark need that value entering;
  currently only the starting level is editable per run.
