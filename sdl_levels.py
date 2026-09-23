#!/usr/bin/env python3
"""Sokkia SDL50 level processor -- download, reduce, report. Prototype.

One file, no install step:

    python sdl_levels.py gui                        # download -> report
    python sdl_levels.py ports
    python sdl_levels.py download --port COM3 -o job.csv
    python sdl_levels.py report job.csv --start-rl 74.614
    python sdl_levels.py fmexport job.csv --initial 74.614
    python sdl_levels.py selftest                   # prove the port is faithful

Only `pip install pyserial` is needed, and only for the download command --
everything else runs on a stock Python.

The reduction is a port of the five FileMaker scripts in
`Levels_and_Projection_Calcs` (Level Import / Level Formatter / Merge Levels /
Level Calcs / Level Export).  `selftest` checks it still reproduces the
database's own export of job AZM020420 byte for byte; that is the contract.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

VERSION = "0.2.0-prototype"
HERE = Path(__file__).resolve().parent

BACKSIGHT = 1  # instrument set up, sighting back at a known point
SIGHTING = 2   # a forward sight: foresight or intermediate, decided by context
NEW_RUN_NOTE = "New level run started"

# The datum the SDL50 assumes when it reduces internally; each run restarts
# from it. Used as the fallback starting level so an un-benchmarked job still
# reduces to the same numbers the instrument shows.
ASSUMED_DATUM = 100.0


class SDLParseError(ValueError):
    """The file is not recognisable SDL50 output."""


# ===========================================================================
# Parsing
# ===========================================================================
#
# The instrument writes CRLF lines with a trailing comma:
#
#     SDL50,3310,002050,ANG020420,0,95,,,
#     0001,0001,1,1,1,23.60,1.5111,100.0000,
#
# Header: model, code, serial, job, ?, line count.
# Record: index, point_id, ?, ?, shot_type, distance, reading, elevation.
#
# `reading` is kept as raw text as well as a float -- the FileMaker-compatible
# export reproduces the original text verbatim and a float round-trip would
# lose trailing zeros.
#
# `elevation` is the level the *instrument* computed, starting each run from an
# assumed 100.0000.  We do not reduce from it, but it is what separates one run
# from the next: the instrument resets it when a new run is started.


@dataclass(frozen=True)
class Header:
    model: str
    code: str
    serial: str
    job: str


@dataclass
class RawRecord:
    index: str
    point_id: str
    shot_type: int
    distance: float
    reading: str
    elevation: float


@dataclass
class SDLFile:
    header: Header
    records: list


def parse(text: str) -> SDLFile:
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        raise SDLParseError("file is empty")

    head = lines[0].split(",")
    if not head[0].strip().upper().startswith("SDL"):
        raise SDLParseError(
            f"first line is not an SDL header (got {lines[0][:40]!r}); "
            "is this a raw instrument download?")
    header = Header(
        model=head[0].strip(),
        code=head[1].strip() if len(head) > 1 else "",
        serial=head[2].strip() if len(head) > 2 else "",
        job=head[3].strip() if len(head) > 3 else "",
    )

    records = []
    for lineno, line in enumerate(lines[1:], start=2):
        parts = line.split(",")
        if len(parts) < 8:
            raise SDLParseError(f"line {lineno}: expected at least 8 fields, got {len(parts)}")
        try:
            shot_type = int(parts[4])
            distance = float(parts[5])
            elevation = float(parts[7])
        except ValueError as exc:
            raise SDLParseError(f"line {lineno}: {exc}") from exc
        if shot_type not in (BACKSIGHT, SIGHTING):
            raise SDLParseError(f"line {lineno}: unknown shot type {shot_type!r}")
        records.append(RawRecord(parts[0].strip(), parts[1].strip(), shot_type,
                                 distance, parts[6].strip(), elevation))
    if not records:
        raise SDLParseError("file contains a header but no observations")
    return SDLFile(header, records)


def parse_file(path) -> SDLFile:
    with open(path, "r", encoding="ascii", errors="replace", newline="") as fh:
        return parse(fh.read())


# ===========================================================================
# Reduction
# ===========================================================================


@dataclass
class Row:
    """One row of the reduced level book (one line of the printed report)."""

    point_id: str
    backsight: str = ""       # raw text as downloaded
    intermediate: str = ""
    foresight: str = ""
    note: str = ""
    instrument_elevation: float = 0.0
    bs_distance: float = 0.0
    fs_distance: float = 0.0
    reduced_level: float = None
    rise_fall: float = None
    starts_run: bool = False
    run: int = 0

    @property
    def bs(self):
        return float(self.backsight) if self.backsight else None

    @property
    def is_(self):
        return float(self.intermediate) if self.intermediate else None

    @property
    def fs(self):
        return float(self.foresight) if self.foresight else None

    @property
    def sight_distance(self) -> float:
        return self.bs_distance + self.fs_distance


@dataclass
class Run:
    """A contiguous level run: everything between two instrument restarts."""

    number: int
    rows: list = field(default_factory=list)
    start_rl: float = None
    closing_rl: float = None  # known RL of the closing point, if any
    start_explicit: bool = False  # True when the operator supplied the start

    @property
    def first(self):
        return self.rows[0]

    @property
    def last(self):
        return self.rows[-1]

    @property
    def sum_backsights(self) -> float:
        return sum(r.bs for r in self.rows if r.bs is not None)

    @property
    def sum_foresights(self) -> float:
        return sum(r.fs for r in self.rows if r.fs is not None)

    @property
    def length_m(self) -> float:
        """Route length: backsight + foresight distances.

        Intermediates are side shots off the route, so they are excluded --
        the misclose allowance is meant to be measured against the route.
        """
        return sum(r.sight_distance for r in self.rows)

    @property
    def arithmetic_check(self):
        """(sum BS - sum FS, last RL - first RL, difference).

        The first two must agree.  This checks the reduction arithmetic only,
        not the observations.
        """
        sums = self.sum_backsights - self.sum_foresights
        levels = (self.last.reduced_level or 0.0) - (self.first.reduced_level or 0.0)
        return sums, levels, sums - levels

    @property
    def misclose(self):
        """Computed closing RL minus known closing RL, in metres."""
        if self.closing_rl is None or self.last.reduced_level is None:
            return None
        return self.last.reduced_level - self.closing_rl

    def allowable_misclose(self, coefficient_mm: float = 12.0) -> float:
        """Allowable misclose in metres: coefficient * sqrt(K) mm, K in km.

        12 mm is the usual general-purpose levelling allowance; it is a
        parameter so a job run to a tighter spec can be checked against it.
        """
        return coefficient_mm * ((self.length_m / 1000.0) ** 0.5) / 1000.0


@dataclass
class LevelBook:
    header: Header
    rows: list
    runs: list
    observations: int = 0  # raw records downloaded, before pairing
    records: list = field(default_factory=list)  # the download, for the audit sheet
    roles: list = field(default_factory=list)    # how each record was classified

    @property
    def misclose_ok(self) -> bool:
        return all(r.misclose is None or abs(r.misclose) <= r.allowable_misclose()
                   for r in self.runs)


def classify(records) -> list:
    """Label each record "BS", "FS" or "IS" -- port of `Level Formatter`.

    The instrument only distinguishes backsight from forward sighting.  A
    type-2 reading is a foresight if the next record is a backsight (the
    instrument moved, so that was the last sight from the setup) and an
    intermediate if the next record is another type-2 (still the same setup).
    The final record, if type 2, is a foresight.
    """
    roles = []
    for i, rec in enumerate(records):
        if rec.shot_type == BACKSIGHT:
            roles.append("BS")
        elif i + 1 >= len(records) or records[i + 1].shot_type == BACKSIGHT:
            roles.append("FS")
        else:
            roles.append("IS")
    return roles


def merge(records, roles, match_point_id: bool = False) -> list:
    """Collapse each foresight/backsight pair into one row -- port of `Merge Levels`.

    Each setup writes two records for the same physical point: a foresight from
    the old setup and a backsight from the new one.  The pair is recognised by
    the instrument's elevation being unchanged across the two.  When it *has*
    changed, the instrument was restarted and that backsight opens a new run.

    `match_point_id`
        The FileMaker script's condition also tests `Last Point ID = Point ID`,
        but `Last Point ID` is a *per-record* field while `Last EL` beside it is
        *global* -- so once the script steps to the next record that test reads
        the new record's own empty field and can never be true.  The exported
        report shows the elevation test alone is what is wanted: in the sample
        job the operator mis-keyed a point number (foresight to 0019, backsight
        labelled 0020) and the two were still correctly merged.  Default to the
        elevation test; the stricter variant is kept for comparison.
    """
    rows = []
    i, n = 0, len(records)
    while i < n:
        rec, role = records[i], roles[i]
        row = Row(point_id=rec.point_id, instrument_elevation=rec.elevation)
        if role == "BS":
            row.backsight, row.bs_distance = rec.reading, rec.distance
        elif role == "IS":
            row.intermediate = rec.reading
        else:
            row.foresight, row.fs_distance = rec.reading, rec.distance
        rows.append(row)

        j = i + 1
        if j < n and records[j].shot_type == BACKSIGHT:
            same_elevation = records[i].elevation == records[j].elevation
            same_point = records[i].point_id == records[j].point_id
            if same_elevation and (same_point or not match_point_id):
                row.backsight = records[j].reading
                row.bs_distance = records[j].distance
                i = j + 1
                continue
            if same_point or not match_point_id:
                rows.append(Row(point_id=records[j].point_id,
                                backsight=records[j].reading,
                                bs_distance=records[j].distance,
                                note=NEW_RUN_NOTE, starts_run=True,
                                instrument_elevation=records[j].elevation))
                i = j + 1
                continue
        i = j
    if rows:
        rows[0].starts_run = True
    return rows


def last_backsight_row(rows, before: int):
    """Index of the nearest row at or before `before - 1` carrying a backsight."""
    for k in range(before - 1, -1, -1):
        if rows[k].backsight:
            return k
    return None


def split_runs(rows) -> list:
    runs = []
    for row in rows:
        if row.starts_run or not runs:
            runs.append(Run(number=len(runs) + 1))
        row.run = runs[-1].number
        runs[-1].rows.append(row)
    return runs


def reduce_levels(runs, start_rls) -> None:
    """Fill in `reduced_level` for every row, in place.

    Height of collimation: RL = (RL + backsight at the last setup) - this
    sight.  Intermediates use the instrument height but do not carry it
    forward, which is why the search for the last backsight row skips them.

    `start_rls` gives the starting level for each run in order.  None means
    carry on from where the previous run closed; for the first run, where
    there is nothing to carry on from, it means the instrument's own assumed
    datum of 100.0000, so the levels shown match what the instrument computed
    until a real benchmark value is supplied.
    """
    flat = [r for run in runs for r in run.rows]
    by_number = {run.number: run for run in runs}

    for i, row in enumerate(flat):
        if row.starts_run:
            run = by_number[row.run]
            run_no = runs.index(run)
            start = start_rls[run_no] if run_no < len(start_rls) else None
            run.start_explicit = start is not None
            if start is None:
                # Resolved here, not up front: the previous run's closing level
                # is only known once that run has been reduced.
                prev = runs[run_no - 1].last.reduced_level if run_no else None
                start = prev if prev is not None else ASSUMED_DATUM
            run.start_rl = start
            row.reduced_level = start
            continue
        k = last_backsight_row(flat, i)
        if k is None or flat[k].reduced_level is None or flat[k].bs is None:
            continue
        sight = row.fs if row.fs is not None else row.is_
        if sight is None:
            continue
        row.reduced_level = flat[k].reduced_level + flat[k].bs - sight
        row.rise_fall = row.reduced_level - flat[k].reduced_level


def build(sdl: SDLFile, start_rls=None, match_point_id: bool = False) -> LevelBook:
    """Raw records in, reduced level book out."""
    roles = classify(sdl.records)
    rows = merge(sdl.records, roles, match_point_id)
    runs = split_runs(rows)
    starts = list(start_rls or [])
    starts += [None] * (len(runs) - len(starts))
    reduce_levels(runs, starts)
    for run in runs:
        if run.closing_rl is None:
            # A run returning to where it started is the common case; the
            # report states the assumption so it can be overridden.
            run.closing_rl = run.start_rl
    return LevelBook(sdl.header, rows, runs, len(sdl.records),
                     records=sdl.records, roles=roles)


# ===========================================================================
# FileMaker-compatible export
# ===========================================================================
#
# This exists to prove the port is faithful, not because the format is good.
#
# Tab-separated, columns Point ID / Backsight / Intermediate / Foresight /
# Reduced Level Calculation / Notes, CR record separators (classic Mac, as
# FileMaker on macOS writes them).  The level column holds spreadsheet formulas
# (=E5+B5-D7), so the file only becomes a level book once it is opened in Excel
# and the starting level is typed into E1.
#
# Two number-formatting quirks have to be reproduced, and both are FileMaker
# artefacts rather than intent:
#
#   Backsight keeps the instrument's original text (0.2843, 1.7840) -- the
#   import writes it straight into the field and nothing recomputes it.
#
#   Intermediate and Foresight are written by Set Field, so they come back out
#   as calculated numbers with leading and trailing zeros stripped: 0.9554
#   becomes .9554, 1.5150 becomes 1.515.


def fm_number(text: str) -> str:
    """Render a value the way FileMaker renders a calculated Number field."""
    if not text:
        return ""
    out = format(Decimal(text).normalize(), "f")
    if out.startswith("0."):
        return out[1:]
    if out.startswith("-0."):
        return "-" + out[2:]
    return out


def fm_formulas(rows, initial: str) -> list:
    """The `Reduced Level Calculation` column -- port of `Level Calcs`.

    Row 1 gets the operator-supplied starting value.  Every later row carrying
    a foresight or intermediate gets a formula referring back to the nearest
    preceding row with a backsight.  A row with only a backsight -- the row
    that opens a new run -- is left empty for the operator to fill in.
    """
    out = []
    for i, row in enumerate(rows):
        if i == 0:
            out.append(initial)
            continue
        column = "D" if row.foresight else ("C" if row.intermediate else None)
        k = last_backsight_row(rows, i) if column else None
        if column is None or k is None:
            out.append("")
        else:
            out.append(f"=E{k + 1}+B{k + 1}-{column}{i + 1}")
    return out


def fm_export(book: LevelBook, initial: str) -> str:
    calcs = fm_formulas(book.rows, initial)
    lines = ["\t".join([row.point_id,
                        row.backsight,               # raw text, as imported
                        fm_number(row.intermediate),
                        fm_number(row.foresight),
                        calc,
                        row.note])
             for row, calc in zip(book.rows, calcs)]
    return "\r".join(lines) + "\r"


# ===========================================================================
# Report
# ===========================================================================
#
# HTML with print rules rather than a drawn PDF: the layout is far easier to
# adjust, and "Print -> Save as PDF" from any browser produces the PDF.
# Nothing is fetched from the network, so the file stands alone and can be
# filed next to the raw capture.

CSS = """
@page { size: A4 portrait; margin: 14mm 12mm 16mm; }
* { box-sizing: border-box; }
body { font: 11px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #14181d; background: #fff; margin: 0; padding: 18px; }
h1 { font-size: 17px; margin: 0 0 2px; letter-spacing: -0.01em; }
h2 { font-size: 13px; margin: 26px 0 8px; padding-bottom: 4px;
     border-bottom: 1.5px solid #14181d; }
.sub { color: #5b6672; margin: 0 0 18px; }
.meta, .summary { display: flex; flex-wrap: wrap; gap: 8px 28px; padding: 10px 12px;
                  background: #f4f6f8; border-radius: 4px; }
.meta { margin: 0 0 20px; }
.summary { margin-top: 10px; }
.meta div, .summary div { font-size: 11px; }
.meta b, .summary b { display: block; font-weight: 600; color: #5b6672; font-size: 9.5px;
                      text-transform: uppercase; letter-spacing: 0.04em; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 3px 6px; border-bottom: 1px solid #e2e6ea; text-align: right; white-space: nowrap; }
th { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.04em;
     color: #5b6672; border-bottom: 1.5px solid #14181d; }
th.l, td.l { text-align: left; }
tbody tr.newrun td { background: #fff6e5; font-weight: 600; }
tbody tr.inter td { color: #5b6672; }
tfoot td { border-top: 1.5px solid #14181d; border-bottom: none; font-weight: 600; padding-top: 6px; }
.pass { color: #14663a; } .fail { color: #96231f; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: 600; }
.badge.pass { background: #dff0e5; } .badge.fail { background: #fbe3e1; }
.note { color: #5b6672; font-size: 10px; margin-top: 16px; }
tr { break-inside: avoid; }
@media print { body { padding: 0; } }
"""


def _f(value, places=4):
    return "" if value is None else f"{value:.{places}f}"


def _run_table(run: Run) -> str:
    body = []
    for row in run.rows:
        cls = "newrun" if row.starts_run and run.number > 1 else ("inter" if row.intermediate else "")
        body.append(
            f"<tr class='{cls}'><td class='l'>{html.escape(row.point_id)}</td>"
            f"<td>{_f(row.bs)}</td><td>{_f(row.is_)}</td><td>{_f(row.fs)}</td>"
            f"<td>{_f(row.reduced_level)}</td>"
            f"<td>{_f(row.sight_distance, 2) if row.sight_distance else ''}</td>"
            f"<td class='l'>{html.escape(row.note)}</td></tr>")
    return (
        "<table><thead><tr><th class='l'>Point</th><th>Backsight</th><th>Inter.</th>"
        "<th>Foresight</th><th>Reduced level</th><th>Dist (m)</th>"
        "<th class='l'>Notes</th></tr></thead><tbody>" + "".join(body) + "</tbody>"
        f"<tfoot><tr><td class='l'>Totals</td><td>{run.sum_backsights:.4f}</td><td></td>"
        f"<td>{run.sum_foresights:.4f}</td><td></td><td>{run.length_m:.2f}</td>"
        "<td></td></tr></tfoot></table>")


def _run_summary(run: Run, coefficient_mm: float) -> str:
    _sums, levels, diff = run.arithmetic_check
    agrees = abs(diff) < 5e-5
    cells = [
        ("Sum backsights", f"{run.sum_backsights:.4f} m"),
        ("Sum foresights", f"{run.sum_foresights:.4f} m"),
        ("&Sigma;BS &minus; &Sigma;FS", f"{run.sum_backsights - run.sum_foresights:+.4f} m"),
        ("Last RL &minus; first RL", f"{levels:+.4f} m"),
        ("Arithmetic check", f"<span class='{'pass' if agrees else 'fail'}'>"
                             f"{'agrees' if agrees else f'OUT BY {diff:+.4f} m'}</span>"),
        ("Route length", f"{run.length_m:.1f} m"),
    ]
    misclose = run.misclose
    if misclose is not None:
        allowed = run.allowable_misclose(coefficient_mm)
        ok = abs(misclose) <= allowed
        cells += [
            ("Closing level (assumed)", f"{run.closing_rl:.4f} m"),
            ("Misclose", f"<span class='{'pass' if ok else 'fail'}'>{misclose * 1000:+.1f} mm</span>"),
            (f"Allowable ({coefficient_mm:g}&radic;K)", f"&plusmn;{allowed * 1000:.1f} mm"),
            ("Result", f"<span class='badge {'pass' if ok else 'fail'}'>"
                       f"{'WITHIN TOLERANCE' if ok else 'EXCEEDS TOLERANCE'}</span>"),
        ]
    return "<div class='summary'>" + "".join(f"<div><b>{k}</b>{v}</div>" for k, v in cells) + "</div>"


def to_html(book: LevelBook, title="Level Report", surveyor="", job_note="",
            coefficient_mm=12.0, source="") -> str:
    meta = [("Job", book.header.job),
            ("Instrument", f"{book.header.model} s/n {book.header.serial}"),
            ("Observations", str(book.observations)),
            ("Level book rows", str(len(book.rows))),
            ("Level runs", str(len(book.runs))),
            ("Produced", datetime.now().strftime("%d %b %Y %H:%M"))]
    if surveyor:
        meta.append(("Surveyor", surveyor))
    if source:
        meta.append(("Source file", source))

    parts = [f"<h1>{html.escape(title)}</h1>"]
    if job_note:
        parts.append(f"<p class='sub'>{html.escape(job_note)}</p>")
    parts.append("<div class='meta'>" + "".join(
        f"<div><b>{html.escape(k)}</b>{html.escape(str(v))}</div>" for k, v in meta) + "</div>")
    for run in book.runs:
        parts.append(f"<h2>Level run {run.number} &mdash; {html.escape(run.first.point_id)} "
                     f"to {html.escape(run.last.point_id)}</h2>")
        parts.append(_run_table(run))
        parts.append(_run_summary(run, coefficient_mm))
    parts.append("<p class='note'>Reduced by height of collimation: RL = (RL + backsight at "
                 "the last instrument setup) &minus; this sight. Intermediate sights do not "
                 "carry the instrument height forward. Levels are as observed; no misclose "
                 "adjustment has been distributed.</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
            + "".join(parts) + "</body></html>")


def to_csv(book: LevelBook) -> str:
    """A flat, spreadsheet-ready CSV with real reduced levels, not formulas."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["Run", "Point", "Backsight", "Intermediate", "Foresight",
                     "Reduced Level", "Rise/Fall", "Distance", "Notes"])
    for run in book.runs:
        for row in run.rows:
            writer.writerow([run.number, row.point_id, _f(row.bs), _f(row.is_), _f(row.fs),
                             _f(row.reduced_level), _f(row.rise_fall),
                             _f(row.sight_distance, 2) if row.sight_distance else "", row.note])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------------
#
# Three sheets: the level book, a summary, and the raw download.
#
# The levels are written as live formulas rather than numbers, the way the
# FileMaker export did it.  That is worth keeping: typing a benchmark into the
# one yellow cell recalculates the whole job, and the arithmetic stays on the
# page where it can be checked instead of being a number to take on trust.
#
# The subtle part is a run that simply carries on from where the last one
# closed.  Its starting cell must REFERENCE the previous run's closing level,
# not repeat it as a number -- otherwise retyping the benchmark moves the first
# run and leaves the rest anchored to the old datum, and the book quietly
# disagrees with itself.  A run whose start the operator actually supplied gets
# a literal, because that is a real benchmark rather than a continuation.
#
# openpyxl is imported lazily, like pyserial and tkinter, so everything else
# keeps working where it is not installed.

XLSX_HEADER_FILL = "2F3B47"
XLSX_INPUT_FILL = "FFF3CD"   # cells the operator is meant to type into
XLSX_RUN_FILL = "FFF6E5"
XLSX_NUM = "0.0000"


class ExcelUnavailable(RuntimeError):
    """openpyxl is not installed."""


def _require_openpyxl():
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:
        raise ExcelUnavailable(
            "Excel output needs openpyxl. Run: pip install openpyxl") from exc
    return openpyxl


def to_xlsx(book: LevelBook, path, surveyor="", job_note="", coefficient_mm=12.0,
            source="") -> None:
    """Write the level book, a summary and the raw download to one workbook."""
    _require_openpyxl()
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.formatting.rule import CellIsRule  # noqa: PLC0415
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: PLC0415
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    head_font = Font(bold=True, color="FFFFFF", size=10)
    head_fill = PatternFill("solid", fgColor=XLSX_HEADER_FILL)
    input_fill = PatternFill("solid", fgColor=XLSX_INPUT_FILL)
    run_fill = PatternFill("solid", fgColor=XLSX_RUN_FILL)
    thin = Side(style="thin", color="B9C2CC")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    def header_row(sheet, row, labels, widths=None):
        for col, label in enumerate(labels, start=1):
            cell = sheet.cell(row=row, column=col, value=label)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
        if widths:
            for col, width in enumerate(widths, start=1):
                sheet.column_dimensions[get_column_letter(col)].width = width

    wb = Workbook()

    # -- Level Book ------------------------------------------------------
    ws = wb.active
    ws.title = "Level Book"
    header_row(ws, 1, ["Point", "Backsight", "Intermediate", "Foresight",
                       "Reduced Level", "Distance", "Notes"],
               [10, 12, 13, 12, 14, 10, 26])

    def sheet_row(index):
        return index + 2  # row 1 is the header

    runs_by_number = {run.number: run for run in book.runs}
    for i, row in enumerate(book.rows):
        r = sheet_row(i)
        ws.cell(row=r, column=1, value=row.point_id)
        ws.cell(row=r, column=2, value=row.bs)
        ws.cell(row=r, column=3, value=row.is_)
        ws.cell(row=r, column=4, value=row.fs)

        level = ws.cell(row=r, column=5)
        if row.starts_run:
            run = runs_by_number[row.run]
            carried = i > 0 and not run.start_explicit
            level.value = f"=E{r - 1}" if carried else row.reduced_level
            level.fill = input_fill
            level.border = box
        else:
            k = last_backsight_row(book.rows, i)
            if k is None:
                level.value = None
            else:
                kr = sheet_row(k)
                column = "D" if row.foresight else "C"
                level.value = f"=E{kr}+B{kr}-{column}{r}"

        ws.cell(row=r, column=6, value=row.sight_distance or None)
        ws.cell(row=r, column=7, value=row.note or None)
        for col in (2, 3, 4, 5, 6):
            ws.cell(row=r, column=col).number_format = XLSX_NUM
        if row.starts_run and row.run > 1:
            for col in range(1, 8):
                ws.cell(row=r, column=col).fill = run_fill
    ws.freeze_panes = "A2"

    # -- Summary ---------------------------------------------------------
    sm = wb.create_sheet("Summary")
    meta = [("Job", book.header.job),
            ("Instrument", f"{book.header.model} s/n {book.header.serial}"),
            ("Surveyor", surveyor),
            ("Note", job_note),
            ("Source file", source),
            ("Observations", book.observations),
            ("Level book rows", len(book.rows)),
            ("Produced", datetime.now().strftime("%d %b %Y %H:%M"))]
    sm.cell(row=1, column=1, value="Level Report").font = Font(bold=True, size=14)
    for i, (label, value) in enumerate(meta, start=2):
        if value in ("", None):
            continue
        sm.cell(row=i, column=1, value=label).font = Font(bold=True, size=10)
        sm.cell(row=i, column=2, value=value)

    top = len(meta) + 3
    header_row(sm, top,
               ["Run", "From", "To", "Rows", "Sum BS", "Sum FS", "ΣBS − ΣFS",
                "Last − first RL", "Check", "Route (m)", "Start RL", "Closing RL",
                "Misclose (mm)", f"Allowable ({coefficient_mm:g}√K)", "Result"],
               [6, 9, 9, 7, 11, 11, 12, 13, 10, 11, 11, 12, 13, 14, 19])

    index = 0
    for n, run in enumerate(book.runs):
        first, last = sheet_row(index), sheet_row(index + len(run.rows) - 1)
        index += len(run.rows)
        r = top + 1 + n
        L = "'Level Book'!"
        sm.cell(row=r, column=1, value=run.number)
        sm.cell(row=r, column=2, value=run.first.point_id)
        sm.cell(row=r, column=3, value=run.last.point_id)
        sm.cell(row=r, column=4, value=len(run.rows))
        sm.cell(row=r, column=5, value=f"=SUM({L}B{first}:B{last})")
        sm.cell(row=r, column=6, value=f"=SUM({L}D{first}:D{last})")
        sm.cell(row=r, column=7, value=f"=E{r}-F{r}")
        sm.cell(row=r, column=8, value=f"={L}E{last}-{L}E{first}")
        sm.cell(row=r, column=9,
                value=f'=IF(ABS(G{r}-H{r})<0.00005,"agrees","OUT BY "&TEXT(G{r}-H{r},"0.0000"))')
        sm.cell(row=r, column=10, value=f"=SUM({L}F{first}:F{last})")
        sm.cell(row=r, column=11, value=f"={L}E{first}")
        # The closing level is an assumption -- that the run came back to where
        # it started -- so it is an input cell like the benchmark.
        sm.cell(row=r, column=12, value=f"=K{r}" if run.closing_rl == run.start_rl
                else run.closing_rl)
        sm.cell(row=r, column=12).fill = input_fill
        sm.cell(row=r, column=12).border = box
        sm.cell(row=r, column=13, value=f"=({L}E{last}-L{r})*1000")
        sm.cell(row=r, column=14, value=f"={coefficient_mm:g}*SQRT(J{r}/1000)")
        sm.cell(row=r, column=15,
                value=f'=IF(ABS(M{r})<=N{r},"WITHIN TOLERANCE","EXCEEDS TOLERANCE")')
        for col in (5, 6, 7, 8, 11, 12):
            sm.cell(row=r, column=col).number_format = XLSX_NUM
        for col in (10, 13, 14):
            sm.cell(row=r, column=col).number_format = "0.0"

    if book.runs:
        span = f"O{top + 1}:O{top + len(book.runs)}"
        sm.conditional_formatting.add(span, CellIsRule(
            operator="equal", formula=['"WITHIN TOLERANCE"'],
            fill=PatternFill("solid", fgColor="DFF0E5"), font=Font(color="14663A")))
        sm.conditional_formatting.add(span, CellIsRule(
            operator="equal", formula=['"EXCEEDS TOLERANCE"'],
            fill=PatternFill("solid", fgColor="FBE3E1"), font=Font(color="96231F")))

    # -- Raw download ----------------------------------------------------
    #
    # The instrument's own records, unaltered, with the role each was given by
    # the reduction beside it -- so the finished levels can be traced back to
    # what actually came off the instrument without opening another file.
    rw = wb.create_sheet("Raw Download")
    rw.cell(row=1, column=1,
            value=f"Unaltered download from {book.header.model} "
                  f"s/n {book.header.serial}, job {book.header.job}").font = Font(
        bold=True, size=11)
    rw.cell(row=2, column=1,
            value="“Role” is how the reduction classified each record; "
                  "“Instrument RL” is the level the instrument itself "
                  "computed from its assumed datum.").font = Font(
        italic=True, size=9, color="5B6672")
    header_row(rw, 4, ["Index", "Point", "Type", "Role", "Distance", "Reading",
                       "Instrument RL"], [9, 10, 8, 8, 11, 12, 14])
    roles = book.roles or [""] * len(book.records)
    for i, (record, role) in enumerate(zip(book.records, roles)):
        r = i + 5
        rw.cell(row=r, column=1, value=record.index)
        rw.cell(row=r, column=2, value=record.point_id)
        rw.cell(row=r, column=3, value="BS" if record.shot_type == BACKSIGHT else "sight")
        rw.cell(row=r, column=4, value=role)
        rw.cell(row=r, column=5, value=record.distance).number_format = "0.00"
        rw.cell(row=r, column=6, value=float(record.reading)).number_format = XLSX_NUM
        rw.cell(row=r, column=7, value=record.elevation).number_format = XLSX_NUM
    rw.freeze_panes = "A5"

    wb.save(str(path))


# ===========================================================================
# Serial capture
# ===========================================================================
#
# The SDL50 pushes stored data out when the operator starts the transfer from
# the instrument's own menu; the host does not request anything.  So the
# capture is deliberately dumb: open the port, wait for the first byte, then
# read until the line has been quiet long enough that the transfer must be
# over.
#
# Every capture writes the bytes to a .raw file BEFORE anything interprets
# them.  If a download ever fails to parse, that file is the evidence, and it
# can be replayed through the rest of the script offline.  It is worth keeping
# anyway as the unmodified record of what the instrument sent.
#
# pyserial is imported lazily so parsing, reduction and reporting all work
# where it is not installed.


class SerialUnavailable(RuntimeError):
    """pyserial is not installed."""


def _require_serial():
    try:
        import serial  # noqa: PLC0415
        import serial.tools.list_ports  # noqa: PLC0415
    except ImportError as exc:
        raise SerialUnavailable("pyserial is not installed. Run: pip install pyserial") from exc
    return serial


def list_ports() -> list:
    """(device, description) for every serial port the OS can see.

    Names differ by platform -- COM3 on Windows, /dev/cu.usbserial-* on macOS,
    /dev/ttyUSB* on Linux -- but pyserial enumerates all three the same way.
    """
    _require_serial()
    from serial.tools import list_ports as lp  # noqa: PLC0415
    return [(p.device, p.description or "") for p in lp.comports()]


@dataclass
class Capture:
    data: bytes
    raw_path: Path
    seconds: float

    @property
    def text(self) -> str:
        return self.data.decode("ascii", errors="replace")


def capture(port, baudrate=9600, bytesize=8, parity="N", stopbits=1,
            xonxoff=False, rtscts=False, dtr=None, rts=None,
            start_timeout=180.0, idle_timeout=3.0,
            raw_dir=None, job_hint="download", on_progress=None) -> Capture:
    """Capture one download.

    `start_timeout` is how long to wait for the operator to start the transfer
    on the instrument.  `idle_timeout` is how long the line must stay quiet
    before the transfer is considered finished -- 3 s is comfortably longer
    than any gap between records at 9600 baud, where a 40-character record
    takes about 40 ms.
    """
    serial = _require_serial()
    port, warning = normalise_port(port)
    if warning:
        print(f"note: {warning}", file=sys.stderr)
    chunks, started = [], time.monotonic()

    with serial.Serial(port=port, baudrate=baudrate, bytesize=bytesize, parity=parity,
                       stopbits=stopbits, xonxoff=xonxoff, rtscts=rtscts, timeout=0.25) as link:
        if dtr is not None:
            link.dtr = dtr
        if rts is not None:
            link.rts = rts
        link.reset_input_buffer()
        deadline = time.monotonic() + start_timeout
        seen_any, last_byte_at = False, None
        while True:
            data = link.read(link.in_waiting or 1)
            now = time.monotonic()
            if data:
                chunks.append(data)
                seen_any, last_byte_at = True, now
                if on_progress:
                    on_progress(sum(len(c) for c in chunks))
            elif seen_any and now - last_byte_at >= idle_timeout:
                break
            elif not seen_any and now >= deadline:
                break

    payload = b"".join(chunks)
    raw_path = None
    if raw_dir and payload:
        raw_dir = Path(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{job_hint}-{datetime.now():%Y%m%d-%H%M%S}.raw"
        raw_path.write_bytes(payload)
    return Capture(payload, raw_path, time.monotonic() - started)


# ===========================================================================
# Diagnostics
# ===========================================================================
#
# For when nothing arrives.  The usual causes, in the order they bite:
#
#   1. The wrong device node.  On macOS a USB-serial adaptor appears twice, as
#      /dev/tty.usbserial-XXX and /dev/cu.usbserial-XXX.  Opening the tty.*
#      node BLOCKS until the other end asserts carrier detect, which a survey
#      instrument never does -- so the terminal just sits there forever looking
#      like a dead link.  Always use cu.*.  `normalise_port` rewrites it.
#   2. Hardware flow control left on.  If the terminal waits for CTS and the
#      instrument never raises it, nothing is ever read.  Use flow control
#      "none" unless the instrument is known to need otherwise.
#   3. Wrong baud or framing.  This gives *garbage*, not silence -- so if some
#      bytes arrive but look like noise, `scan` will find the right settings.
#   4. Cable or converter.  If `monitor` shows no bytes at all and no control
#      lines move, suspect the cable or a counterfeit adaptor chip before
#      anything in software.
#
# Settings tried by `scan`, most likely first.  The SDL50 family is normally
# 9600 8-N-1, but older Sokkia gear and some instrument menus use 1200 or
# 2400, and 7-E-1 is common on survey instruments of that vintage.
SCAN_SETTINGS = [
    (9600, 8, "N", 1),
    (9600, 7, "E", 1),
    (1200, 8, "N", 1),
    (1200, 7, "E", 1),
    (2400, 8, "N", 1),
    (2400, 7, "E", 1),
    (4800, 8, "N", 1),
    (4800, 7, "E", 1),
    (19200, 8, "N", 1),
    (38400, 8, "N", 1),
    (9600, 8, "E", 1),
    (9600, 8, "O", 1),
]


def normalise_port(port: str):
    """Return (port, warning).  Rewrites macOS tty.* nodes to cu.*.

    Opening /dev/tty.usbserial-* on macOS blocks waiting for carrier detect,
    which an instrument will never assert -- the single most common reason a
    serial terminal appears to hang on a Mac.
    """
    if port and "/tty." in port:
        fixed = port.replace("/tty.", "/cu.", 1)
        return fixed, (f"{port} would block waiting for carrier detect; "
                       f"using {fixed} instead.")
    return port, None


def port_details() -> list:
    """Everything the OS knows about each serial port.

    The chipset matters on macOS: FTDI and CP210x work with drivers Apple
    ships, while CH340 clones usually need a vendor driver and counterfeit
    PL2303 chips are refused by Prolific's driver outright -- they enumerate,
    so the port appears, but no data ever flows.
    """
    _require_serial()
    from serial.tools import list_ports as lp  # noqa: PLC0415

    out = []
    for p in lp.comports():
        vid_pid = (f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None and p.pid is not None
                   else "")
        out.append({
            "device": p.device,
            "description": p.description or "",
            "manufacturer": p.manufacturer or "",
            "product": p.product or "",
            "serial_number": p.serial_number or "",
            "vid_pid": vid_pid,
            "chipset": _chipset(vid_pid, f"{p.description} {p.manufacturer} {p.product}"),
        })
    return out


def _chipset(vid_pid: str, blob: str) -> str:
    """Best guess at the adaptor chip, and whether macOS needs a driver for it."""
    vid = vid_pid.split(":")[0].upper() if vid_pid else ""
    known = {
        "0403": "FTDI (driver built into macOS)",
        "10C4": "Silicon Labs CP210x (driver built into macOS)",
        "067B": "Prolific PL2303 (counterfeit chips are refused by the driver)",
        "1A86": "WCH CH340/CH341 (usually needs a vendor driver on macOS)",
        "2341": "Arduino CDC",
        "239A": "Adafruit CDC",
    }
    if vid in known:
        return known[vid]
    lowered = blob.lower()
    for needle, name in (("ftdi", "FTDI"), ("cp210", "Silicon Labs CP210x"),
                         ("pl2303", "Prolific PL2303"), ("ch340", "WCH CH340"),
                         ("prolific", "Prolific")):
        if needle in lowered:
            return name
    return ""


def _control_lines(link) -> str:
    """CTS/DSR/CD/RI as a readable string, or why they could not be read.

    These say whether anything is electrically alive at the far end.  A cable
    with nothing on it reads all-low and never changes.
    """
    try:
        return (f"CTS={int(link.cts)} DSR={int(link.dsr)} "
                f"CD={int(link.cd)} RI={int(link.ri)}")
    except Exception as exc:
        return f"(unavailable: {exc})"


def _hexdump(data: bytes, offset: int = 0) -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexed = " ".join(f"{b:02X}" for b in chunk).ljust(47)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {offset + i:06X}  {hexed}  |{text}|")
    return "\n".join(lines)


def printable_ratio(data: bytes) -> float:
    """Fraction of bytes that are plausible text -- the score `scan` ranks on."""
    if not data:
        return 0.0
    good = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return good / len(data)


def monitor(port, baudrate=9600, bytesize=8, parity="N", stopbits=1,
            xonxoff=False, rtscts=False, dtr=None, rts=None,
            seconds=60.0, show_hex=True, emit=print, should_stop=None) -> int:
    """Watch a port and report whatever arrives, as hex and text.

    This is the "is anything happening at all" tool.  It never tries to parse:
    if a single byte arrives it is shown, however mangled, which is what
    separates a dead cable from wrong baud settings.

    `emit` receives each line, so the same routine drives the command line
    (where it prints) and the GUI (where it appends to a log pane).
    `should_stop` is polled so a Stop button can end it early.
    """
    serial = _require_serial()
    port, warning = normalise_port(port)
    if warning:
        emit(f"note: {warning}")

    emit(f"Opening {port} at {baudrate} {bytesize}{parity}{stopbits}, "
          f"flow={'xon/xoff' if xonxoff else 'rts/cts' if rtscts else 'none'}")
    with serial.Serial(port=port, baudrate=baudrate, bytesize=bytesize, parity=parity,
                       stopbits=stopbits, xonxoff=xonxoff, rtscts=rtscts,
                       timeout=0.2) as link:
        if dtr is not None:
            link.dtr = dtr
        if rts is not None:
            link.rts = rts
        emit(f"  DTR={int(bool(link.dtr))} RTS={int(bool(link.rts))}  "
              f"{_control_lines(link)}")
        emit(f"Listening for {seconds:.0f}s. Start the transfer on the "
              f"instrument now. Ctrl-C to stop.\n")
        link.reset_input_buffer()

        total, lines_before = 0, _control_lines(link)
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                if should_stop is not None and should_stop():
                    emit("stopped.")
                    break
                data = link.read(link.in_waiting or 1)
                if data:
                    if show_hex:
                        emit(_hexdump(data, total))
                    else:
                        emit(data.decode("ascii", errors="replace").rstrip("\n"))
                    total += len(data)
                now_lines = _control_lines(link)
                if now_lines != lines_before:
                    emit(f"  [control lines changed: {now_lines}]")
                    lines_before = now_lines
        except KeyboardInterrupt:
            emit("stopped.")

    emit(f"{total} bytes received.")
    if total == 0:
        emit("Nothing arrived at all. In order of likelihood:")
        emit("  1. Wrong port. Run `ports` and check the device name.")
        emit("  2. Transfer not actually started on the instrument.")
        emit("  3. Flow control: try --rtscts, or --dtr on, in case the")
        emit("     instrument waits for a line to be raised.")
        emit("  4. Cable: a straight-through DB9 where a crossover is needed,")
        emit("     or the wrong socket on the instrument.")
        emit("  5. The adaptor itself -- check the chipset line in `ports`.")
        emit("If the control lines above are all 0 and never changed, suspect")
        emit("the cable or the adaptor before anything in software.")
    else:
        emit("Bytes arrived, so the link is alive. If they look like noise")
        emit("rather than text, the baud rate or framing is wrong -- run `scan`.")
    return 0 if total else 1


def scan(port, seconds_each=1.5, rounds=3, xonxoff=False, rtscts=False,
         dtr=None, rts=None, emit=print, should_stop=None):
    """Cycle through common serial settings and report which yields text.

    The instrument sends its data once, so this has to sample while the
    transfer is running: start the download on the instrument, then let this
    cycle.  Each setting gets a short listen and is scored on how much of what
    arrived looks like text.

    Returns the best (baud, bytesize, parity, stopbits) found, or None if
    nothing arrived at any setting, so a caller can offer to apply it.
    """
    serial = _require_serial()
    port, warning = normalise_port(port)
    if warning:
        emit(f"note: {warning}")

    emit(f"Scanning {port}: {len(SCAN_SETTINGS)} settings x {rounds} round(s) "
          f"at {seconds_each:.1f}s each.")
    emit("Start the transfer on the instrument NOW and let it run.\n")

    results = {}
    for round_no in range(rounds):
        for baud, bits, par, stop in SCAN_SETTINGS:
            if should_stop is not None and should_stop():
                emit("stopped.")
                rounds = round_no  # fall through to the summary of what we got
                break
            key = (baud, bits, par, stop)
            try:
                with serial.Serial(port=port, baudrate=baud, bytesize=bits, parity=par,
                                   stopbits=stop, xonxoff=xonxoff, rtscts=rtscts,
                                   timeout=0.2) as link:
                    if dtr is not None:
                        link.dtr = dtr
                    if rts is not None:
                        link.rts = rts
                    link.reset_input_buffer()
                    chunks, deadline = [], time.monotonic() + seconds_each
                    while time.monotonic() < deadline:
                        data = link.read(link.in_waiting or 1)
                        if data:
                            chunks.append(data)
                    payload = b"".join(chunks)
            except Exception as exc:
                results.setdefault(key, {"bytes": 0, "sample": b"", "error": str(exc)})
                continue

            entry = results.setdefault(key, {"bytes": 0, "sample": b"", "error": ""})
            entry["bytes"] += len(payload)
            if payload and not entry["sample"]:
                entry["sample"] = payload[:48]

        if should_stop is not None and should_stop():
            break
        done = (round_no + 1) * len(SCAN_SETTINGS)
        emit(f"  round {round_no + 1}/{rounds} done ({done} probes)")

    emit(f"{'setting':>16}  {'bytes':>6}  {'text':>5}  sample")
    emit("  " + "-" * 74)
    ranked = []
    for key, entry in results.items():
        # Rank on text quality first, then volume; ties break towards the
        # setting listed earliest in SCAN_SETTINGS, which is the most likely
        # one. The ratio is rounded so a near-tie resolves the same way rather
        # than on a fraction of a percent of noise.
        ratio = printable_ratio(entry["sample"])
        likelihood = -SCAN_SETTINGS.index(key)
        ranked.append((entry["bytes"] > 0, round(ratio, 2), entry["bytes"],
                       likelihood, key, entry))
    ranked.sort(reverse=True)

    for _any_bytes, ratio, count, _likelihood, (baud, bits, par, stop), entry in ranked:
        label = f"{baud} {bits}{par}{stop}"
        if entry["error"]:
            emit(f"{label:>16}  {'-':>6}  {'-':>5}  error: {entry['error'][:40]}")
            continue
        sample = "".join(chr(b) if 32 <= b < 127 else "." for b in entry["sample"])
        emit(f"{label:>16}  {count:>6}  {ratio * 100:>4.0f}%  {sample}")

    best = next((r for r in ranked if r[2] > 0), None)
    emit("")
    if best is None:
        emit("No bytes at any setting. This is not a baud rate problem --")
        emit("run the monitor and work through the checklist it prints.")
        return None
    _a, ratio, count, _l, (baud, bits, par, stop), _e = best
    emit(f"Best: {baud} {bits}{par}{stop} ({count} bytes, "
          f"{ratio * 100:.0f}% text).")
    if ratio < 0.85:
        emit("That is still not clean text -- treat it as a hint, not an answer,")
        emit("and try the next few rows as well.")
    else:
        emit(f"Download with:  sdl_levels.py download --port {port} "
             f"--baud {baud} --bytesize {bits} --parity {par} --stopbits {stop}")
    return (baud, bits, par, stop)


# ===========================================================================
# GUI
# ===========================================================================
#
# Plain Tkinter -- it ships with Python on Windows and macOS, so there is no
# GUI dependency to install or break.  Imported inside the function so the
# command line still works where tkinter is absent.


def run_gui(preload=None) -> int:
    import queue  # noqa: PLC0415
    import threading  # noqa: PLC0415
    import webbrowser  # noqa: PLC0415

    try:
        import tkinter as tk  # noqa: PLC0415
        from tkinter import filedialog, messagebox, ttk  # noqa: PLC0415
    except ImportError:
        # Bundled with the official Python on Windows and macOS, but commonly a
        # separate package on Linux.
        print("The window needs tkinter, which this Python does not have.\n"
              "  Debian/Ubuntu:  sudo apt install python3-tk\n"
              "  Fedora:         sudo dnf install python3-tkinter\n"
              "Or use the command line instead: sdl_levels.py report <file>",
              file=sys.stderr)
        return 2

    class App(ttk.Frame):
        def __init__(self, master):
            super().__init__(master, padding=10)
            self.grid(sticky="nsew")
            master.columnconfigure(0, weight=1)
            master.rowconfigure(0, weight=1)
            self.columnconfigure(0, weight=1)
            self.rowconfigure(2, weight=1)

            self.sdl = self.book = None
            self.source_name = ""
            self.start_vars = []
            self.ports = []
            self.events = queue.Queue()
            self.diag = None
            # "auto" leaves the handshake lines as pyserial sets them; some
            # instruments will not talk until one is forced.
            self.dtr_mode = tk.StringVar(value="auto")
            self.rts_mode = tk.StringVar(value="auto")

            self._connection()
            self._job()
            self._table()
            self._actions()
            self._status("Ready. Connect the instrument, or open a downloaded file.")
            self.after(100, self._drain)

        # -- layout --
        def _connection(self):
            box = ttk.LabelFrame(self, text="1. Download from instrument", padding=8)
            box.grid(row=0, column=0, sticky="ew")
            box.columnconfigure(1, weight=1)  # only the port combo grows

            ttk.Label(box, text="Port").grid(row=0, column=0, sticky="w", padx=(0, 6))
            self.port = ttk.Combobox(box, state="readonly", width=34)
            self.port.grid(row=0, column=1, sticky="ew")
            ttk.Button(box, text="Refresh", command=self.refresh_ports, width=8).grid(
                row=0, column=2, padx=6)

            self.baud = tk.StringVar(value="9600")
            self.databits = tk.StringVar(value="8")
            self.parity = tk.StringVar(value="N")
            self.stopbits = tk.StringVar(value="1")
            self.flow = tk.StringVar(value="none")

            def combo(label, var, values, width, col):
                ttk.Label(box, text=label).grid(row=0, column=col, sticky="e", padx=(10, 4))
                ttk.Combobox(box, textvariable=var, values=values, width=width,
                             state="readonly").grid(row=0, column=col + 1, sticky="w")

            combo("Baud", self.baud, ["1200", "2400", "4800", "9600", "19200", "38400"], 7, 3)
            combo("Data", self.databits, ["7", "8"], 3, 5)
            combo("Parity", self.parity, ["N", "E", "O"], 3, 7)
            combo("Stop", self.stopbits, ["1", "2"], 3, 9)
            combo("Flow", self.flow, ["none", "xon/xoff", "rts/cts"], 9, 11)

            self.download_button = ttk.Button(box, text="Download", command=self.start_download)
            self.download_button.grid(row=0, column=13, padx=(12, 0))
            ttk.Button(box, text="Open file…", command=self.open_file).grid(
                row=0, column=14, padx=(6, 0))

            ttk.Button(box, text="Connection test…",
                       command=self.open_diagnostics).grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk.Label(box, foreground="#5b6672",
                      text="Nothing arriving? Start here — it shows whether any "
                           "data reaches the port, and finds the right baud rate.").grid(
                row=1, column=3, columnspan=12, sticky="w", pady=(8, 0), padx=(8, 0))

        def _job(self):
            box = ttk.LabelFrame(self, text="2. Job details", padding=8)
            box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            box.columnconfigure(1, weight=1)
            box.columnconfigure(3, weight=1)

            self.surveyor = tk.StringVar()
            self.note = tk.StringVar()
            self.allowance = tk.StringVar(value="12")

            ttk.Label(box, text="Surveyor").grid(row=0, column=0, sticky="w", padx=(0, 6))
            ttk.Entry(box, textvariable=self.surveyor).grid(row=0, column=1, sticky="ew")
            ttk.Label(box, text="Note").grid(row=0, column=2, sticky="w", padx=(12, 6))
            ttk.Entry(box, textvariable=self.note).grid(row=0, column=3, sticky="ew")
            ttk.Label(box, text="Allowance (mm√K)").grid(row=0, column=4, sticky="e", padx=(12, 6))
            entry = ttk.Entry(box, textvariable=self.allowance, width=6)
            entry.grid(row=0, column=5, sticky="w")
            entry.bind("<FocusOut>", self.rebuild)
            entry.bind("<Return>", self.rebuild)

            self.runs_frame = ttk.Frame(box)
            self.runs_frame.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))

        def _table(self):
            box = ttk.LabelFrame(self, text="3. Reduced levels", padding=8)
            box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
            box.columnconfigure(0, weight=1)
            box.rowconfigure(0, weight=1)

            cols = ("run", "point", "bs", "is", "fs", "rl", "dist", "note")
            heads = ("Run", "Point", "Backsight", "Inter.", "Foresight",
                     "Reduced level", "Dist (m)", "Notes")
            widths = (44, 70, 90, 90, 90, 110, 80, 190)
            self.tree = ttk.Treeview(box, columns=cols, show="headings", height=16)
            for col, head, width in zip(cols, heads, widths):
                self.tree.heading(col, text=head)
                self.tree.column(col, width=width,
                                 anchor="w" if col in ("point", "note", "run") else "e")
            self.tree.grid(row=0, column=0, sticky="nsew")
            bar = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
            bar.grid(row=0, column=1, sticky="ns")
            self.tree.configure(yscrollcommand=bar.set)
            self.tree.tag_configure("newrun", background="#fff6e5")
            self.tree.tag_configure("inter", foreground="#5b6672")

            self.summary = tk.Text(box, height=5, wrap="word", relief="flat",
                                   background="#f4f6f8", padx=8, pady=6)
            self.summary.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            self.summary.configure(state="disabled")

        def _actions(self):
            bar = ttk.Frame(self)
            bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            bar.columnconfigure(0, weight=1)
            self.status_label = ttk.Label(bar, text="", anchor="w")
            self.status_label.grid(row=0, column=0, sticky="ew")
            self.buttons = []
            for i, (text, cmd) in enumerate([("Save report…", self.save_report),
                                             ("Save CSV…", self.save_csv),
                                             ("Save Excel…", self.save_xlsx),
                                             ("Save FileMaker format…", self.save_legacy)]):
                button = ttk.Button(bar, text=text, command=cmd, state="disabled")
                button.grid(row=0, column=1 + i, padx=4)
                self.buttons.append(button)

        # -- helpers --
        def _status(self, text):
            self.status_label.configure(text=text)

        def refresh_ports(self):
            try:
                self.ports = list_ports()
            except SerialUnavailable as exc:
                messagebox.showerror("Serial support missing", str(exc))
                return
            self.port.configure(values=[f"{d}  ({desc})" for d, desc in self.ports])
            if self.ports and not self.port.get():
                self.port.current(0)
            self._status(f"{len(self.ports)} serial port(s) found." if self.ports
                         else "No serial ports found -- check the adaptor is plugged in.")

        def _selected_port(self):
            choice = self.port.get()
            for device, desc in self.ports:
                if choice.startswith(device):
                    return device
            return choice.split()[0] if choice else None

        # -- connection test ------------------------------------------------
        def open_diagnostics(self):
            """A window for working out why nothing is arriving.

            Runs the same `monitor` and `scan` routines the command line uses,
            with their output piped into a log pane, so a user who will never
            open a terminal can still do the diagnosis and send the log on.
            """
            device = self._selected_port()
            if not device:
                messagebox.showwarning("No port", "Choose the serial port first.")
                return
            if getattr(self, "diag", None) is not None and self.diag.winfo_exists():
                self.diag.lift()
                return

            win = tk.Toplevel(self)
            self.diag = win
            win.title("Connection test")
            win.geometry("860x620")
            win.transient(self.winfo_toplevel())  # keep it above the main window
            win.columnconfigure(0, weight=1)
            win.rowconfigure(2, weight=1)

            self.diag_queue = queue.Queue()
            self.diag_stop = threading.Event()
            self.diag_busy = False

            # What the OS knows about this adaptor. The chipset decides whether
            # macOS needs a driver for it, which is the first thing to rule out.
            info = ttk.LabelFrame(win, text="Adaptor", padding=8)
            info.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 0))
            info.columnconfigure(1, weight=1)
            details = {}
            try:
                details = next((d for d in port_details() if d["device"] == device), {})
            except SerialUnavailable:
                pass
            rows = [("Port", device),
                    ("Chipset", details.get("chipset") or "unrecognised"),
                    ("USB VID:PID", details.get("vid_pid") or "-"),
                    ("Description", details.get("description") or "-")]
            for i, (label, value) in enumerate(rows):
                ttk.Label(info, text=label, width=13).grid(row=i, column=0, sticky="w")
                ttk.Label(info, text=value).grid(row=i, column=1, sticky="w")
            if "/tty." in device:
                ttk.Label(info, foreground="#96231f", wraplength=760,
                          text="This is the tty node: opening it blocks until the "
                               "instrument asserts carrier detect, which it never "
                               "does. The cu node will be used instead.").grid(
                    row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 0))

            bar = ttk.LabelFrame(win, text="Tests", padding=8)
            bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(8, 0))
            self.diag_watch = ttk.Button(bar, text="1. Watch the port",
                                         command=self._diag_monitor)
            self.diag_watch.grid(row=0, column=0)
            self.diag_scan = ttk.Button(bar, text="2. Find the settings",
                                        command=self._diag_scan)
            self.diag_scan.grid(row=0, column=1, padx=6)
            self.diag_stop_button = ttk.Button(bar, text="Stop", state="disabled",
                                               command=lambda: self.diag_stop.set())
            self.diag_stop_button.grid(row=0, column=2, padx=(0, 16))

            ttk.Label(bar, text="DTR").grid(row=0, column=3, sticky="e", padx=(6, 4))
            ttk.Combobox(bar, textvariable=self.dtr_mode, width=6, state="readonly",
                         values=["auto", "on", "off"]).grid(row=0, column=4)
            ttk.Label(bar, text="RTS").grid(row=0, column=5, sticky="e", padx=(10, 4))
            ttk.Combobox(bar, textvariable=self.rts_mode, width=6, state="readonly",
                         values=["auto", "on", "off"]).grid(row=0, column=6)
            ttk.Button(bar, text="Copy log", command=self._diag_copy).grid(
                row=0, column=7, padx=(16, 0))
            ttk.Label(bar, wraplength=820, foreground="#5b6672",
                      text="Watch the port, then start the transfer on the "
                           "instrument. Nothing at all means a port, cable or flow "
                           "control problem; unreadable characters mean the baud "
                           "rate or framing is wrong, which test 2 will find.").grid(
                row=1, column=0, columnspan=8, sticky="w", pady=(8, 0))

            wrap = ttk.Frame(win)
            wrap.grid(row=2, column=0, sticky="nsew", padx=10, pady=(8, 10))
            wrap.columnconfigure(0, weight=1)
            wrap.rowconfigure(0, weight=1)
            self.diag_log = tk.Text(wrap, wrap="none", font=("Menlo", 10),
                                    background="#11151a", foreground="#dfe6ee",
                                    insertbackground="#dfe6ee", padx=8, pady=6)
            self.diag_log.grid(row=0, column=0, sticky="nsew")
            vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.diag_log.yview)
            vbar.grid(row=0, column=1, sticky="ns")
            hbar = ttk.Scrollbar(wrap, orient="horizontal", command=self.diag_log.xview)
            hbar.grid(row=1, column=0, sticky="ew")
            self.diag_log.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set,
                                    state="disabled")

            self._diag_write(f"Ready. Port {device}.")
            self._diag_write("Press “Watch the port”, then start the "
                             "transfer on the instrument.\n")
            win.protocol("WM_DELETE_WINDOW", self._diag_close)
            self._diag_drain()

        def _diag_close(self):
            self.diag_stop.set()
            if self.diag is not None:
                self.diag.destroy()
                self.diag = None

        def _diag_write(self, line):
            self.diag_log.configure(state="normal")
            self.diag_log.insert("end", line + "\n")
            self.diag_log.see("end")
            self.diag_log.configure(state="disabled")

        def _diag_copy(self):
            self.clipboard_clear()
            self.clipboard_append(self.diag_log.get("1.0", "end"))
            self._status("Connection test log copied to the clipboard.")

        def _line_state(self, var):
            return None if var.get() == "auto" else var.get() == "on"

        def _diag_run(self, work):
            """Run a diagnostic on a worker thread, logging as it goes."""
            if self.diag_busy:
                return
            self.diag_busy = True
            self.diag_stop.clear()
            for button in (self.diag_watch, self.diag_scan):
                button.configure(state="disabled")
            self.diag_stop_button.configure(state="normal")

            def runner():
                try:
                    result = work()
                except Exception as exc:
                    self.diag_queue.put(("line", f"error: {exc}"))
                    result = None
                self.diag_queue.put(("finished", result))

            threading.Thread(target=runner, daemon=True).start()

        def _link_kwargs(self, with_format=True):
            """Snapshot the link settings on the UI thread.

            Tk variables may only be read from the main thread, so everything a
            worker needs is turned into plain values here, before the thread
            starts.
            """
            flow = self.flow.get()
            kwargs = dict(xonxoff=flow == "xon/xoff", rtscts=flow == "rts/cts",
                          dtr=self._line_state(self.dtr_mode),
                          rts=self._line_state(self.rts_mode))
            if with_format:
                kwargs.update(baudrate=int(self.baud.get()),
                              bytesize=int(self.databits.get()),
                              parity=self.parity.get(),
                              stopbits=int(self.stopbits.get()))
            return kwargs

        def _diag_monitor(self):
            device = self._selected_port()
            kwargs = self._link_kwargs()
            emit = lambda line: self.diag_queue.put(("line", line))  # noqa: E731
            self._diag_write("\n" + "=" * 70)
            self._diag_run(lambda: monitor(device, seconds=120.0, emit=emit,
                                           should_stop=self.diag_stop.is_set,
                                           **kwargs))

        def _diag_scan(self):
            device = self._selected_port()
            kwargs = self._link_kwargs(with_format=False)
            emit = lambda line: self.diag_queue.put(("line", line))  # noqa: E731
            self._diag_write("\n" + "=" * 70)
            self._diag_run(lambda: ("settings",
                                    scan(device, emit=emit,
                                         should_stop=self.diag_stop.is_set, **kwargs)))

        def _diag_drain(self):
            if self.diag is None or not self.diag.winfo_exists():
                return
            try:
                while True:
                    kind, payload = self.diag_queue.get_nowait()
                    if kind == "line":
                        self._diag_write(payload)
                    else:
                        self.diag_busy = False
                        for button in (self.diag_watch, self.diag_scan):
                            button.configure(state="normal")
                        self.diag_stop_button.configure(state="disabled")
                        self._diag_finished(payload)
            except queue.Empty:
                pass
            self.diag.after(120, self._diag_drain)

        def _diag_finished(self, result):
            """Offer to adopt the settings a scan found."""
            if not (isinstance(result, tuple) and len(result) == 2
                    and result[0] == "settings" and result[1]):
                return
            baud, bits, par, stop = result[1]
            if messagebox.askyesno(
                    "Settings found",
                    f"{baud} {bits}{par}{stop} produced readable text.\n\n"
                    "Use these settings for the download?", parent=self.diag):
                self.baud.set(str(baud))
                self.databits.set(str(bits))
                self.parity.set(par)
                self.stopbits.set(str(stop))
                self._status(f"Serial settings set to {baud} {bits}{par}{stop}.")

        # -- download --
        def start_download(self):
            device = self._selected_port()
            if not device:
                messagebox.showwarning("No port", "Choose the serial port first.")
                return
            self.download_button.configure(state="disabled")
            self._status("Waiting -- start the transfer on the instrument…")
            kwargs = self._link_kwargs()
            kwargs["raw_dir"] = Path.cwd() / "raw"

            def work():
                try:
                    self.events.put(("done", capture(
                        device, on_progress=lambda n: self.events.put(("progress", n)), **kwargs)))
                except Exception as exc:  # surfaced on the UI thread
                    self.events.put(("error", exc))

            threading.Thread(target=work, daemon=True).start()

        def _drain(self):
            try:
                while True:
                    kind, payload = self.events.get_nowait()
                    if kind == "progress":
                        self._status(f"Receiving… {payload} bytes")
                    elif kind == "error":
                        self.download_button.configure(state="normal")
                        messagebox.showerror("Download failed", str(payload))
                        self._status("Download failed.")
                    else:
                        self.download_button.configure(state="normal")
                        self._finish(payload)
            except queue.Empty:
                pass
            self.after(100, self._drain)

        def _finish(self, result):
            if not result.data:
                self._status("Nothing received. Check the port, baud rate and cable.")
                messagebox.showwarning(
                    "Nothing received",
                    "No data arrived.\n\nCheck the port and baud rate, that the cable is in "
                    "the instrument's RS-232 socket, and that the transfer was started on "
                    "the instrument.")
                return
            where = f"\nRaw capture saved to {result.raw_path}" if result.raw_path else ""
            try:
                self.sdl = parse(result.text)
            except SDLParseError as exc:
                messagebox.showerror("Could not read the download",
                                     f"{len(result.data)} bytes arrived but could not be "
                                     f"parsed:\n\n{exc}{where}")
                self._status("Download captured but not understood -- raw file kept.")
                return
            self.source_name = f"{self.sdl.header.job} (serial)"
            self._status(f"Received {len(result.data)} bytes, "
                         f"{len(self.sdl.records)} observations.{where}")
            self.start_vars = []
            self.rebuild()

        def open_file(self):
            path = filedialog.askopenfilename(
                title="Open an SDL50 download",
                filetypes=[("SDL downloads", "*.csv *.txt *.raw"), ("All files", "*.*")])
            if not path:
                return
            try:
                self.sdl = parse_file(path)
            except (SDLParseError, OSError) as exc:
                messagebox.showerror("Could not read the file", str(exc))
                return
            self.source_name = Path(path).name
            self._status(f"Loaded {self.source_name}: {len(self.sdl.records)} observations.")
            self.start_vars = []
            self.rebuild()

        # -- reduce and display --
        def rebuild(self, *_event):
            if self.sdl is None:
                return
            starts = []
            for var in self.start_vars:
                text = var.get().strip()
                try:
                    starts.append(float(text) if text else None)
                except ValueError:
                    starts.append(None)
            self.book = build(self.sdl, starts)
            self._sync_runs()
            self._fill_table()
            self._fill_summary()
            for button in self.buttons:
                button.configure(state="normal")

        def _sync_runs(self):
            if len(self.start_vars) == len(self.book.runs):
                return
            for child in self.runs_frame.winfo_children():
                child.destroy()
            self.start_vars = []
            ttk.Label(self.runs_frame, text="Starting reduced level for each run:").grid(
                row=0, column=0, sticky="w", padx=(0, 10))
            for i, run in enumerate(self.book.runs):
                var = tk.StringVar(value=f"{ASSUMED_DATUM:.4f}" if i == 0 else "")
                self.start_vars.append(var)
                ttk.Label(self.runs_frame, text=f"Run {run.number} ({run.first.point_id})").grid(
                    row=0, column=1 + i * 2, sticky="e", padx=(10, 4))
                entry = ttk.Entry(self.runs_frame, textvariable=var, width=11)
                entry.grid(row=0, column=2 + i * 2, sticky="w")
                entry.bind("<Return>", self.rebuild)
                entry.bind("<FocusOut>", self.rebuild)
            ttk.Label(self.runs_frame, text="(blank = carry on from the previous run)").grid(
                row=0, column=1 + len(self.book.runs) * 2, sticky="w", padx=(12, 0))

        def _fill_table(self):
            self.tree.delete(*self.tree.get_children())
            for run in self.book.runs:
                for row in run.rows:
                    tags = ("newrun",) if row.starts_run and run.number > 1 else (
                        ("inter",) if row.intermediate else ())
                    self.tree.insert("", "end", tags=tags, values=(
                        run.number, row.point_id, _f(row.bs), _f(row.is_), _f(row.fs),
                        _f(row.reduced_level),
                        _f(row.sight_distance, 2) if row.sight_distance else "", row.note))

        def _coefficient(self):
            try:
                return float(self.allowance.get())
            except ValueError:
                return 12.0

        def _fill_summary(self):
            coefficient = self._coefficient()
            lines = []
            for run in self.book.runs:
                _s, _l, diff = run.arithmetic_check
                line = (f"Run {run.number}: {run.first.point_id}→{run.last.point_id}, "
                        f"{len(run.rows)} rows, {run.length_m:.0f} m.  Arithmetic "
                        f"{'agrees' if abs(diff) < 5e-5 else f'OUT by {diff:+.4f} m'}.")
                if run.misclose is not None:
                    allowed = run.allowable_misclose(coefficient)
                    ok = abs(run.misclose) <= allowed
                    line += (f"  Misclose {run.misclose * 1000:+.1f} mm of "
                             f"±{allowed * 1000:.1f} mm — "
                             f"{'within tolerance' if ok else 'EXCEEDS TOLERANCE'}.")
                lines.append(line)
            self.summary.configure(state="normal")
            self.summary.delete("1.0", "end")
            self.summary.insert("1.0", "\n".join(lines))
            self.summary.configure(state="disabled")

        # -- output --
        def _stem(self):
            return self.book.header.job or "levels"

        def save_report(self):
            path = filedialog.asksaveasfilename(
                defaultextension=".html", initialfile=f"{self._stem()}-report.html",
                filetypes=[("HTML report", "*.html")])
            if not path:
                return
            Path(path).write_text(
                to_html(self.book, surveyor=self.surveyor.get(), job_note=self.note.get(),
                        coefficient_mm=self._coefficient(), source=self.source_name),
                encoding="utf-8")
            self._status(f"Report written to {path}")
            if messagebox.askyesno("Report saved", "Open it now?\n\nUse the browser's "
                                                   "Print → Save as PDF for a PDF."):
                webbrowser.open(Path(path).resolve().as_uri())

        def save_csv(self):
            path = filedialog.asksaveasfilename(
                defaultextension=".csv", initialfile=f"{self._stem()}-levels.csv",
                filetypes=[("CSV", "*.csv")])
            if path:
                Path(path).write_text(to_csv(self.book), encoding="utf-8")
                self._status(f"CSV written to {path}")

        def save_xlsx(self):
            path = filedialog.asksaveasfilename(
                defaultextension=".xlsx", initialfile=f"{self._stem()}.xlsx",
                filetypes=[("Excel workbook", "*.xlsx")])
            if not path:
                return
            try:
                to_xlsx(self.book, path, surveyor=self.surveyor.get(),
                        job_note=self.note.get(), coefficient_mm=self._coefficient(),
                        source=self.source_name)
            except ExcelUnavailable as exc:
                messagebox.showerror("Excel output unavailable", str(exc))
                return
            self._status(f"Excel workbook written to {path}")

        def save_legacy(self):
            path = filedialog.asksaveasfilename(
                defaultextension=".txt", initialfile=f"{self._stem()}-reduced.txt",
                filetypes=[("Tab separated", "*.txt")])
            if not path:
                return
            initial = self.start_vars[0].get().strip() if self.start_vars else ""
            with open(path, "w", encoding="ascii", newline="") as fh:
                fh.write(fm_export(self.book, initial))
            self._status(f"FileMaker-format file written to {path}")

    root = tk.Tk()
    root.title("Sokkia SDL50 — level processor")
    root.geometry("1180x780")
    app = App(root)
    try:
        app.refresh_ports()
    except Exception:
        pass
    if preload:
        try:
            app.sdl = parse_file(preload)
            app.source_name = Path(preload).name
            app._status(f"Loaded {app.source_name}: {len(app.sdl.records)} observations.")
            app.rebuild()
        except (SDLParseError, OSError) as exc:
            app._status(f"Could not open {preload}: {exc}")
    root.mainloop()
    return 0


# ===========================================================================
# Self test
# ===========================================================================


def selftest() -> int:
    """Check the reduction still reproduces the FileMaker export exactly.

    data/AZM020420.csv is a real download; AZM020420-reduced.txt is what the
    FileMaker database produced from it.  While those two stay in step the
    reduction logic is right.  If the output moves, the change is wrong -- do
    not edit the expected file to make it pass.
    """
    raw, gold = HERE / "data" / "AZM020420.csv", HERE / "data" / "AZM020420-reduced.txt"
    if not raw.exists():
        # The packaged app deliberately does not ship the sample job, so this
        # is a source-checkout command rather than something a user can run.
        print(f"The sample job is not here ({raw}).\n"
              "selftest runs against data/AZM020420.csv from the repository; "
              "the packaged app does not bundle it.", file=sys.stderr)
        return 2

    failures = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failures.append(f"{name}: got {got!r}, expected {want!r}")

    sdl = parse_file(raw)
    print("Parsing")
    check("header", (sdl.header.model, sdl.header.serial, sdl.header.job),
          ("SDL50", "002050", "ANG020420"))
    check("record count", len(sdl.records), 94)

    book = build(sdl)
    print("Reduction")
    check("level book rows", len(book.rows), 52)
    check("runs", [len(r.rows) for r in book.runs], [17, 35])
    check("new run marked at row 18", [(i, r.point_id) for i, r in enumerate(book.rows)
                                       if r.note == NEW_RUN_NOTE], [(17, "0017")])
    # Point 0006 is an intermediate: 0007 reduces from the setup at 0005.
    check("intermediate 0006", next(r for r in book.rows if r.point_id == "0006").intermediate,
          "2.3492")
    # A foresight to 0019 followed by a backsight mis-keyed as 0020: the
    # elevation is unchanged, so it is one setup and the two merge.
    merged = next(r for r in book.rows if r.point_id == "0019")
    check("mis-keyed 0020 merged into 0019", (merged.foresight, merged.backsight),
          ("1.6511", "1.6443"))
    check("point 0020 absent", any(r.point_id == "0020" for r in book.rows), False)

    with open(gold, "r", encoding="ascii", newline="") as fh:
        expected = fh.read()
    print("FileMaker parity")
    check(f"export byte-for-byte ({len(expected)} bytes)", fm_export(book, "74.614"), expected)
    check("number rendering", [fm_number(x) for x in ("0.9554", "1.5150", "2.3492", "")],
          [".9554", "1.515", "2.3492", ""])

    checked = build(sdl, [74.614])
    print("Checks")
    offset = 74.614 - 100.0
    drift = max(abs(r.reduced_level - (r.instrument_elevation + offset))
                for r in checked.runs[0].rows if r.reduced_level is not None)
    check("run 1 levels track the instrument", drift < 5e-5, True)
    check("arithmetic agrees", [abs(r.arithmetic_check[2]) < 1e-9 for r in checked.runs],
          [True, True])
    check("misclose within allowance", checked.misclose_ok, True)
    check("run 2 carries forward",
          round(checked.runs[1].start_rl, 4) == round(checked.runs[0].last.reduced_level, 4), True)

    # -- Excel ----------------------------------------------------------
    # The workbook's levels are formulas, so the only thing that can really be
    # wrong is a row reference. These checks resolve the formula graph the way
    # a spreadsheet would and compare it with the reduction, then retype the
    # benchmark and require every run to follow -- a run whose start repeats a
    # number instead of referencing the previous run's close passes the first
    # check and fails the second.
    print("Excel")
    try:
        _require_openpyxl()
    except ExcelUnavailable as exc:
        print(f"  SKIP  {exc}")
    else:
        import re  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import openpyxl  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "check.xlsx"
            to_xlsx(build(sdl, [74.614]), out, surveyor="selftest", source=raw.name)
            wb = openpyxl.load_workbook(out)
            check("three sheets", wb.sheetnames,
                  ["Level Book", "Summary", "Raw Download"])
            lb = wb["Level Book"]
            check("raw sheet holds the whole download",
                  wb["Raw Download"].max_row - 4, len(sdl.records))

            reference = re.compile(r"([A-G])(\d+)")

            def evaluate():
                """Resolve the sheet's formulas as a spreadsheet would."""
                cache = {}

                def value(col, row):
                    if (col, row) in cache:
                        return cache[(col, row)]
                    cell = lb[f"{col}{row}"].value
                    if isinstance(cell, str) and cell.startswith("="):
                        cell = eval(reference.sub(  # noqa: S307 - our own arithmetic
                            lambda mo: repr(value(mo.group(1), int(mo.group(2)))),
                            cell[1:]))
                    cell = 0.0 if cell is None else cell
                    cache[(col, row)] = cell
                    return cell

                return value

            worst = 0.0
            for datum in (74.614, 100.0, 12.345):
                lb["E2"] = datum
                value = evaluate()
                expected = build(sdl, [datum])
                for i, row in enumerate(expected.rows):
                    worst = max(worst, abs(value("E", i + 2) - row.reduced_level))
            check("formulas match the reduction at three datums", worst < 5e-9, True)

    print()
    if failures:
        print(f"{len(failures)} FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("All checks passed.")
    return 0


# ===========================================================================
# Command line
# ===========================================================================


def _starts(values):
    out = []
    for value in values or []:
        out.append(None if value.strip().lower() in ("", "-", "carry") else float(value))
    return out


COMMANDS = ("gui", "ports", "selftest", "download", "report", "fmexport",
            "xlsx", "monitor", "scan")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Running the script with no arguments -- a double-click, or just
    # `python sdl_levels.py` -- should open the window, not print usage.
    # A bare filename should open the window onto that file, so the script can
    # be set as the file association for a download.
    if not argv:
        argv = ["gui"]
    elif (argv[0] not in COMMANDS and not argv[0].startswith("-")
          and Path(argv[0]).exists()):
        argv = ["gui", argv[0]]

    parser = argparse.ArgumentParser(
        prog="sdl_levels.py", description="Download and reduce Sokkia SDL50 level data.")
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command", required=True)

    gui = subs.add_parser("gui", help="open the window")
    gui.add_argument("input", nargs="?", help="optionally open this download at startup")
    subs.add_parser("ports", help="list serial ports and what chip they use")

    def add_link_options(sub, with_format=True):
        sub.add_argument("--port", required=True,
                         help="e.g. /dev/cu.usbserial-210 or COM3")
        if with_format:
            sub.add_argument("--baud", type=int, default=9600)
            sub.add_argument("--bytesize", type=int, default=8, choices=[7, 8])
            sub.add_argument("--parity", default="N", choices=["N", "E", "O"])
            sub.add_argument("--stopbits", type=int, default=1, choices=[1, 2])
        sub.add_argument("--xonxoff", action="store_true", help="software flow control")
        sub.add_argument("--rtscts", action="store_true", help="hardware flow control")
        sub.add_argument("--dtr", choices=["on", "off"],
                         help="force the DTR line; some instruments wait on it")
        sub.add_argument("--rts", choices=["on", "off"], help="force the RTS line")

    mon = subs.add_parser("monitor",
                          help="watch a port and show whatever arrives (start here "
                               "when nothing works)")
    add_link_options(mon)
    mon.add_argument("--seconds", type=float, default=60.0)
    mon.add_argument("--text", action="store_true",
                     help="print as text instead of a hex dump")

    sc = subs.add_parser("scan", help="try common baud rates and framings, "
                                      "report which yields readable text")
    add_link_options(sc, with_format=False)
    sc.add_argument("--seconds-each", type=float, default=1.5)
    sc.add_argument("--rounds", type=int, default=3)
    subs.add_parser("selftest", help="verify the reduction against the FileMaker output")

    dl = subs.add_parser("download", help="capture a download from the instrument")
    dl.add_argument("--port", required=True, help="e.g. COM3 or /dev/cu.usbserial-110")
    dl.add_argument("--baud", type=int, default=9600)
    dl.add_argument("--bytesize", type=int, default=8, choices=[7, 8])
    dl.add_argument("--parity", default="N", choices=["N", "E", "O"])
    dl.add_argument("--stopbits", type=int, default=1, choices=[1, 2])
    dl.add_argument("--xonxoff", action="store_true", help="software flow control")
    dl.add_argument("--rtscts", action="store_true", help="hardware flow control")
    dl.add_argument("--dtr", choices=["on", "off"],
                    help="force the DTR line; some instruments wait on it")
    dl.add_argument("--rts", choices=["on", "off"], help="force the RTS line")
    dl.add_argument("--idle-timeout", type=float, default=3.0)
    dl.add_argument("--start-timeout", type=float, default=180.0)
    dl.add_argument("--raw-dir", default="raw", help="where to archive the raw capture")
    dl.add_argument("-o", "--out", help="write the decoded CSV here too")

    rp = subs.add_parser("report", help="reduce a CSV and write a level report")
    rp.add_argument("input")
    rp.add_argument("-o", "--out", help="output HTML (default <input>-report.html)")
    rp.add_argument("--csv", help="also write a flat CSV of reduced levels")
    rp.add_argument("--xlsx", help="also write an Excel workbook (needs openpyxl)")
    rp.add_argument("--start-rl", action="append", metavar="RL",
                    help="starting level for a run; repeat per run, or 'carry'")
    rp.add_argument("--surveyor", default="")
    rp.add_argument("--note", default="")
    rp.add_argument("--allowance", type=float, default=12.0,
                    help="misclose allowance coefficient in mm (default 12 sqrt K)")

    xl = subs.add_parser("xlsx", help="write an Excel workbook of the reduced levels")
    xl.add_argument("input")
    xl.add_argument("-o", "--out", help="output .xlsx (default <input>.xlsx)")
    xl.add_argument("--start-rl", action="append", metavar="RL",
                    help="starting level for a run; repeat per run, or 'carry'")
    xl.add_argument("--surveyor", default="")
    xl.add_argument("--note", default="")
    xl.add_argument("--allowance", type=float, default=12.0)

    fm = subs.add_parser("fmexport", help="write the legacy FileMaker tab-separated file")
    fm.add_argument("input")
    fm.add_argument("-o", "--out")
    fm.add_argument("--initial", default="", help="value for cell E1")

    args = parser.parse_args(argv)

    try:
        if args.command == "gui":
            return run_gui(args.input)

        if args.command == "selftest":
            return selftest()

        if args.command == "ports":
            ports = port_details()
            if not ports:
                print("No serial ports found. Is the USB adaptor plugged in?")
                print("On macOS the adaptor should appear as /dev/cu.usbserial-*;")
                print("if it does not, the converter or its driver is the problem,")
                print("not anything downstream of it.")
                return 1
            for info in ports:
                print(info["device"])
                for label, key in (("description", "description"),
                                   ("manufacturer", "manufacturer"),
                                   ("product", "product"),
                                   ("serial number", "serial_number"),
                                   ("USB VID:PID", "vid_pid"),
                                   ("chipset", "chipset")):
                    if info[key]:
                        print(f"    {label:<14} {info[key]}")
                if info["device"].startswith("/dev/tty."):
                    print("    NOTE           opening this node blocks waiting for "
                          "carrier detect;")
                    print("                   use the matching /dev/cu.* node instead")
            return 0

        if args.command == "monitor":
            return monitor(args.port, baudrate=args.baud, bytesize=args.bytesize,
                           parity=args.parity, stopbits=args.stopbits,
                           xonxoff=args.xonxoff, rtscts=args.rtscts,
                           dtr=(None if args.dtr is None else args.dtr == "on"), rts=(None if args.rts is None else args.rts == "on"),
                           seconds=args.seconds, show_hex=not args.text)

        if args.command == "scan":
            return 0 if scan(args.port, seconds_each=args.seconds_each, rounds=args.rounds,
                        xonxoff=args.xonxoff, rtscts=args.rtscts,
                        dtr=(None if args.dtr is None else args.dtr == "on"),
                        rts=(None if args.rts is None else args.rts == "on")) else 1

        if args.command == "download":
            print(f"Listening on {args.port} at {args.baud} "
                  f"{args.bytesize}{args.parity}{args.stopbits}.")
            print("Start the transfer on the instrument now...")
            result = capture(args.port, baudrate=args.baud, bytesize=args.bytesize,
                             parity=args.parity, stopbits=args.stopbits, xonxoff=args.xonxoff,
                             rtscts=args.rtscts, raw_dir=args.raw_dir,
                             dtr=(None if args.dtr is None else args.dtr == "on"), rts=(None if args.rts is None else args.rts == "on"),
                             idle_timeout=args.idle_timeout, start_timeout=args.start_timeout,
                             on_progress=lambda n: print(f"\r  {n} bytes", end="", flush=True))
            print()
            if not result.data:
                print("Nothing received.", file=sys.stderr)
                print(f"Run:  sdl_levels.py monitor --port {args.port}\n"
                      "to see whether anything reaches the port at all.",
                      file=sys.stderr)
                return 1
            print(f"Captured {len(result.data)} bytes in {result.seconds:.1f}s")
            if result.raw_path:
                print(f"Raw capture: {result.raw_path}")
            if args.out:
                Path(args.out).write_text(result.text, encoding="ascii", errors="replace")
                print(f"Wrote {args.out}")
            try:
                sdl = parse(result.text)
            except SDLParseError as exc:
                print(f"Captured, but could not parse it: {exc}", file=sys.stderr)
                print("The raw file above is intact -- keep it for diagnosis.", file=sys.stderr)
                return 1
            print(f"Parsed {len(sdl.records)} observations, job {sdl.header.job}")
            return 0

        if args.command == "report":
            book = build(parse_file(args.input), _starts(args.start_rl))
            out = Path(args.out or f"{Path(args.input).stem}-report.html")
            out.write_text(to_html(book, surveyor=args.surveyor, job_note=args.note,
                                   coefficient_mm=args.allowance,
                                   source=Path(args.input).name), encoding="utf-8")
            print(f"Wrote {out}")
            if args.csv:
                Path(args.csv).write_text(to_csv(book), encoding="utf-8")
                print(f"Wrote {args.csv}")
            if args.xlsx:
                to_xlsx(book, args.xlsx, surveyor=args.surveyor, job_note=args.note,
                        coefficient_mm=args.allowance, source=Path(args.input).name)
                print(f"Wrote {args.xlsx}")
            for run in book.runs:
                line = (f"  Run {run.number}: {run.first.point_id}->{run.last.point_id}, "
                        f"{len(run.rows)} rows, {run.length_m:.0f} m")
                if run.misclose is not None:
                    allowed = run.allowable_misclose(args.allowance)
                    line += (f", misclose {run.misclose * 1000:+.1f} mm of "
                             f"{allowed * 1000:.1f} mm "
                             f"[{'OK' if abs(run.misclose) <= allowed else 'OUT'}]")
                print(line)
            return 0 if book.misclose_ok else 1

        if args.command == "xlsx":
            book = build(parse_file(args.input), _starts(args.start_rl))
            out = Path(args.out or f"{Path(args.input).stem}.xlsx")
            to_xlsx(book, out, surveyor=args.surveyor, job_note=args.note,
                    coefficient_mm=args.allowance, source=Path(args.input).name)
            print(f"Wrote {out} ({len(book.rows)} rows, {len(book.runs)} run(s), "
                  f"{book.observations} raw records)")
            return 0

        if args.command == "fmexport":
            book = build(parse_file(args.input))
            text = fm_export(book, args.initial)
            out = Path(args.out or f"{Path(args.input).stem}-reduced.txt")
            with open(out, "w", encoding="ascii", newline="") as fh:
                fh.write(text)
            print(f"Wrote {out} ({len(text)} bytes, FileMaker-compatible)")
            return 0

    except (SerialUnavailable, ExcelUnavailable) as exc:
        print(exc, file=sys.stderr)
        return 2
    except SDLParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
