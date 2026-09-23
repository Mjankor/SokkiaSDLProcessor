# SokkiaSDLProcessor — project rules

## Prototype shape

One script, `sdl_levels.py`, with no install step and no dependency but
`pyserial` (and that only for the `download` command). Keep it that way until
we deliberately decide to package it — no new files, no `pip install -e`.

Section order in the file: parsing → reduction → FileMaker export → report →
serial → GUI → selftest → CLI. Nothing above the serial section may import
`pyserial`, and nothing above the GUI section may import `tkinter`; both are
imported lazily so the whole pipeline stays runnable, and testable, with no
instrument attached and no display.

## The selftest is the contract

`data/AZM020420.csv` is a real SDL50 download and `AZM020420-reduced.txt` is
what the FileMaker database produced from it. `python sdl_levels.py selftest`
must keep passing: the byte-for-byte check is the only proof that the
reduction is a faithful port. Do not edit the expected file to make a change
pass — if the output moves, the change is wrong, or the reason it is right has
to be written down.

Add a new golden pair (raw download + its finished report) whenever a job
turns up a case the current fixture does not cover.

## The GUI must not reimplement anything

`monitor` and `scan` take an `emit` callback and a `should_stop` predicate so
the command line and the window run the *same* routine — the command line
passes `print`, the window passes a queue feed and a Stop button. Any new
diagnostic follows that shape rather than growing a second copy inside the GUI.

Tk variables may only be read from the main thread. Anything a worker thread
needs is snapshotted first — see `_link_kwargs` — because reading
`self.baud.get()` inside a lambda that runs on the worker raises "main thread
is not in main loop" only at runtime, and only when a real port is attached.

## The workbook stays formula-driven

Levels in the Excel output are formulas, not values. That is the property the
FileMaker export had and it is worth keeping: one benchmark drives the job and
the arithmetic is visible. A run that carries on from the previous one
references its closing cell; only a start the operator actually supplied is
written as a literal. `selftest` retypes the datum and requires every run to
follow, which is the only check that catches a literal creeping back in.

`openpyxl` is imported lazily like `pyserial` and `tkinter` — the rest of the
script, `selftest` included, must keep working without it.

## Conventions

- Serial capture always writes the raw bytes to disk *before* parsing them.
  A download that fails to parse must still leave evidence behind.
- The FileMaker export section reproduces FileMaker's quirks deliberately (raw
  backsight text, stripped zeros on calculated fields, CR record separators).
  Do not "tidy" them — byte-level parity is that section's whole job. New,
  better-behaved output goes in the report section.
- Tolerances, allowances and datums are named constants or parameters with
  documented defaults, not literals buried in code (see `ASSUMED_DATUM` and
  `Run.allowable_misclose`).
- Branch per change, cut from `main`, merged back by PR.
