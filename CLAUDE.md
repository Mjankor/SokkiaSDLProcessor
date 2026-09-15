# SokkiaSDLProcessor — project rules

## The golden test is the contract

`tests/data/AZM020420.csv` is a real SDL50 download and
`AZM020420-reduced.txt` is what the FileMaker database produced from it.
`test_matches_filemaker_export_byte_for_byte` must keep passing: it is the
only proof that the reduction logic is a faithful port. Do not "fix" the
expected file to make a change pass — if the output moves, the change is
wrong, or the reason it is right has to be written down.

Add a new golden pair (raw download + its finished report) whenever a job
turns up a case the current fixture does not cover.

## Layering

`parser` → `reduce` → (`report` | `fmexport`); `serialio` feeds `parser` and
depends on nothing else. `cli` and `gui` are thin shells. No reduction logic
in the UI layer, and nothing below `serialio` may import `pyserial` — the core
must stay importable and testable without it, which is what lets the whole
pipeline be developed with no instrument attached.

## Conventions

- Serial capture always writes the raw bytes to disk *before* parsing them.
  A download that fails to parse must still leave evidence behind.
- `fmexport` reproduces FileMaker's quirks deliberately (raw backsight text,
  stripped leading/trailing zeros on calculated fields, CR record separators).
  Do not "tidy" them — that module's whole job is byte-level parity. New,
  better-behaved output goes in `report`.
- Tolerances and allowances are parameters with documented defaults, not
  literals buried in code (see `Run.allowable_misclose`).
- Branch per change, cut from `main`, merged back by PR.
