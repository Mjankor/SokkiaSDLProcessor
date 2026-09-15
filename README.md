# Sokkia SDL50 level processor

Downloads level data from a Sokkia SDL50 digital level over a USB-serial
adaptor, reduces it, and produces a level report — in one step, replacing the
download-then-FileMaker workflow.

Cross-platform (Windows, macOS, Linux). Pure Python plus `pyserial`; the GUI
is Tkinter, which ships with Python, so there is nothing else to install.

## Status

The reduction logic is a **verified** port of the FileMaker database: given
the sample job `AZM020420`, this code reproduces the database's exported file
byte for byte (1677 bytes, `tests/test_reduce.py`). The serial layer is
exercised over a pseudo-terminal pair in `tests/test_serialio.py`, but has
**not yet been run against a real SDL50** — see "Before first real use".

## Install

```sh
pip install -e .          # add [dev] for the tests
```

## Use

```sh
sdlproc gui               # the one-window version: download → report
```

or from the command line:

```sh
sdlproc ports                                     # what's plugged in
sdlproc download --port COM3 -o AZM020420.csv     # capture from the instrument
sdlproc report AZM020420.csv --start-rl 74.614 \
        --surveyor "M. Ankor" --csv levels.csv    # reduce and write the report
```

The report is HTML with print rules: open it and use the browser's
**Print → Save as PDF**. `--csv` additionally writes a flat spreadsheet of
reduced levels.

To reproduce the old FileMaker export instead (tab-separated, spreadsheet
formulas rather than numbers, CR line endings):

```sh
sdlproc fmexport AZM020420.csv --initial 74.614
```

## Serial settings

Defaults are 9600 8-N-1, no flow control. Override with `--baud`,
`--bytesize`, `--parity`, `--stopbits`, `--xonxoff`, `--rtscts`.

The SDL50 pushes data when the operator starts the transfer from the
instrument's menu; the host does not request anything. So the app opens the
port, waits (up to `--start-timeout`, default 3 minutes) for the first byte,
then reads until the line has been quiet for `--idle-timeout` (default 3 s).

**Every capture writes the raw bytes to `raw/` before anything parses them.**
If a download ever fails to parse, that file is the evidence and can be
replayed through the rest of the app offline. It is worth keeping anyway as
the unmodified record of what the instrument sent.

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

On top of that the app adds what the spreadsheet step used to: real reduced
levels rather than formulas, per-run sums, the arithmetic check
(ΣBS − ΣFS against last RL − first RL), route length from the recorded sight
distances, and misclose against a `12√K` mm allowance (the coefficient is
`--allowance`).

## Two things found in the FileMaker database

**A latent bug in `Merge Levels`.** Its condition tests
`Last EL = Reduced Level And Last Point ID = Point ID`, but `Last EL` is a
*global* field while `Last Point ID` beside it is a *per-record* field. After
the script steps to the next record, that second test reads the new record's
own empty field, so it can never be true. The exported report proves the merge
is meant to run on the elevation test alone, and that this is the *right*
behaviour: in the sample job the operator mis-keyed a point number — a
foresight to 0019 followed by a backsight labelled 0020 — and the two were
still correctly merged as one setup. This port uses the elevation test only,
and keeps the stricter variant behind `build(..., match_point_id=True)` for
comparison. **Worth checking the database's own field definitions**: if that
global flag was changed at some point, merging there may now behave
differently from when the sample report was produced.

**Number formatting is inconsistent, and it is an artefact.** In the exported
file the backsight column keeps the instrument's original text (`0.2843`,
`1.7840`) because the import writes it straight into the field, while the
foresight and intermediate columns are written by `Set Field` and so come back
out as calculated numbers with leading and trailing zeros stripped (`.9554`,
`1.515`). `fmexport` reproduces this exactly, for parity. The app's own
outputs use a consistent 4 decimal places throughout.

## Before first real use

The serial path has never met the actual instrument. On the first real
download, please:

1. Run `sdlproc download --port … -o job.csv` and keep whatever lands in
   `raw/`, whether or not it parses.
2. Send that raw file back if anything looks wrong — it is enough to diagnose
   framing, flow control or format problems offline.

Open questions that only a real download can settle: whether the instrument
needs DTR asserted or XON/XOFF handshaking, and whether the file it sends is
always this CSV layout or can be SDR33 depending on an instrument setting.

## Open decisions

- **Misclose is reported, not distributed.** No correction is spread through
  the run, because the FileMaker version did not do so and changing that would
  silently move every level. Say the word and it becomes an option.
- **Closing levels are assumed.** Each run is assumed to close back on its
  own starting level, which is what the sample job does (both runs close to
  +1.7 mm and +0.1 mm). Runs that close on a *different* known benchmark need
  that value entering — currently only the starting level is editable per run.

## Packaging

```sh
pip install pyinstaller
pyinstaller --onefile --windowed --name SDLProcessor -c sdlproc/gui.py
```

produces a standalone `.exe` on Windows and a `.app` on macOS from the same
source. On Windows the adaptor appears as `COM3`; on macOS as
`/dev/cu.usbserial-*`. FTDI and CP210x adaptors need no driver on either;
CH340 clones occasionally need a vendor driver on macOS.

## Tests

```sh
python -m pytest
```
