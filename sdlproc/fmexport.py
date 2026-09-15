"""Reproduce the FileMaker "Export Level Data" output byte-for-byte.

This exists to prove the port is faithful, not because the format is good.
The app's own outputs live in :mod:`sdlproc.report`.

The FileMaker export is a tab-separated file with the columns
``Point ID, Backsight, Intermediate, Foresight, Reduced Level Calculation,
Notes``, **carriage-return** record separators (classic Mac, as FileMaker on
macOS writes them) and a trailing tab before each CR because the final Notes
column is usually empty.

The "Reduced Level Calculation" column holds spreadsheet formulas rather than
numbers -- ``=E5+B5-D7`` -- so the file only becomes a level book once it is
opened in Excel and the starting level is typed into E1.  Reproducing that is
the point of this module; producing real numbers instead is the point of the
rest of the app.

Two number-formatting quirks have to be reproduced as well, and they are
FileMaker artefacts rather than intent:

* **Backsight** keeps the instrument's original text (``0.2843``, ``1.7840``),
  because the import writes it straight into the field and nothing recomputes
  it.
* **Intermediate** and **Foresight** are written by ``Set Field``, so they come
  back out as calculated numbers: FileMaker drops the leading zero and any
  trailing zeros (``0.9554`` becomes ``.9554``, ``1.5150`` becomes ``1.515``).
"""

from __future__ import annotations

from decimal import Decimal

from .reduce import LevelBook, Row, last_backsight_row

FIELD_SEP = "\t"
RECORD_SEP = "\r"


def fm_number(text: str | None) -> str:
    """Render a value the way FileMaker renders a calculated Number field."""
    if text in (None, ""):
        return ""
    value = Decimal(text).normalize()
    out = format(value, "f")
    if out.startswith("0."):
        return out[1:]
    if out.startswith("-0."):
        return "-" + out[2:]
    return out


def formulas(rows: list[Row], initial: str) -> list[str]:
    """The ``Reduced Level Calculation`` column.

    Port of ``Level Calcs``.  Row 1 receives the operator-supplied starting
    value; every later row that carries a foresight or an intermediate gets a
    formula referring back to the nearest preceding row with a backsight.  A
    row with only a backsight -- the row that opens a new run -- is left empty
    for the operator to type that run's starting level into.
    """
    out: list[str] = []
    for i, row in enumerate(rows):
        if i == 0:
            out.append(initial)
            continue
        column = "D" if row.foresight not in (None, "") else (
            "C" if row.intermediate not in (None, "") else None
        )
        if column is None:
            out.append("")
            continue
        k = last_backsight_row(rows, i)
        if k is None:
            out.append("")
            continue
        ref = k + 1  # spreadsheet rows are 1-based
        out.append(f"=E{ref}+B{ref}-D{i + 1}" if column == "D"
                   else f"=E{ref}+B{ref}-C{i + 1}")
    return out


def export(book: LevelBook, initial: str) -> str:
    """Return the full text of the FileMaker-compatible export."""
    calcs = formulas(book.rows, initial)
    lines = []
    for row, calc in zip(book.rows, calcs):
        fields = [
            row.point_id,
            row.backsight or "",  # raw text, as imported
            fm_number(row.intermediate),
            fm_number(row.foresight),
            calc,
            row.note,
        ]
        lines.append(FIELD_SEP.join(fields))
    return RECORD_SEP.join(lines) + RECORD_SEP
