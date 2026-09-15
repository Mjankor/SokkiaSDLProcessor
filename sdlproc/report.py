"""Render a reduced level book as a printable report, or as CSV.

The report is HTML with print rules rather than a drawn PDF: the layout is far
easier to adjust, and "Print -> Save as PDF" from any browser produces the
PDF.  Nothing is fetched from the network, so the file stands alone and can be
filed alongside the raw capture.
"""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime

from .reduce import LevelBook, Run

CSS = """
@page { size: A4 portrait; margin: 14mm 12mm 16mm; }
* { box-sizing: border-box; }
body { font: 11px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #14181d; background: #fff; margin: 0; padding: 18px; }
h1 { font-size: 17px; margin: 0 0 2px; letter-spacing: -0.01em; }
h2 { font-size: 13px; margin: 26px 0 8px; padding-bottom: 4px;
     border-bottom: 1.5px solid #14181d; }
.sub { color: #5b6672; margin: 0 0 18px; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 30px; margin: 0 0 20px;
        padding: 10px 12px; background: #f4f6f8; border-radius: 4px; }
.meta div { font-size: 11px; }
.meta b { display: block; font-weight: 600; color: #5b6672; font-size: 9.5px;
          text-transform: uppercase; letter-spacing: 0.04em; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 3px 6px; border-bottom: 1px solid #e2e6ea; text-align: right;
         white-space: nowrap; }
th { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.04em;
     color: #5b6672; border-bottom: 1.5px solid #14181d; text-align: right; }
th.l, td.l { text-align: left; }
tbody tr.newrun td { background: #fff6e5; font-weight: 600; }
tbody tr.inter td { color: #5b6672; }
tfoot td { border-top: 1.5px solid #14181d; border-bottom: none;
           font-weight: 600; padding-top: 6px; }
.summary { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px 26px;
           padding: 10px 12px; border-radius: 4px; background: #f4f6f8; }
.summary div { font-size: 11px; }
.summary b { display: block; font-weight: 600; color: #5b6672; font-size: 9.5px;
             text-transform: uppercase; letter-spacing: 0.04em; }
.pass { color: #14663a; } .fail { color: #96231f; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px;
         font-size: 10px; font-weight: 600; }
.badge.pass { background: #dff0e5; } .badge.fail { background: #fbe3e1; }
.note { color: #5b6672; font-size: 10px; margin-top: 16px; }
.run { break-inside: auto; }
tr { break-inside: avoid; }
@media print { body { padding: 0; } .noprint { display: none; } }
"""


def _fmt(value, places=4):
    return "" if value is None else f"{value:.{places}f}"


def _run_table(run: Run) -> str:
    rows = []
    for row in run.rows:
        cls = "newrun" if row.starts_run and run.number > 1 else (
            "inter" if row.intermediate else "")
        rows.append(
            "<tr class='%s'>"
            "<td class='l'>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td class='l'>%s</td></tr>"
            % (
                cls,
                html.escape(row.point_id),
                _fmt(row.bs), _fmt(row.is_), _fmt(row.fs),
                _fmt(row.reduced_level, 4),
                _fmt(row.sight_distance, 2) if row.sight_distance else "",
                html.escape(row.note),
            )
        )
    sums, levels, diff = run.arithmetic_check
    foot = (
        "<tfoot><tr><td class='l'>Totals</td>"
        f"<td>{run.sum_backsights:.4f}</td><td></td>"
        f"<td>{run.sum_foresights:.4f}</td><td></td>"
        f"<td>{run.length_m:.2f}</td><td></td></tr></tfoot>"
    )
    return (
        "<table><thead><tr>"
        "<th class='l'>Point</th><th>Backsight</th><th>Inter.</th><th>Foresight</th>"
        "<th>Reduced level</th><th>Dist (m)</th><th class='l'>Notes</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody>" + foot + "</table>"
    )


def _run_summary(run: Run, coefficient_mm: float) -> str:
    sums, levels, diff = run.arithmetic_check
    misclose = run.misclose
    allowed = run.allowable_misclose(coefficient_mm)
    cells = [
        ("Sum backsights", f"{run.sum_backsights:.4f} m"),
        ("Sum foresights", f"{run.sum_foresights:.4f} m"),
        ("&Sigma;BS &minus; &Sigma;FS", f"{sums:+.4f} m"),
        ("Last RL &minus; first RL", f"{levels:+.4f} m"),
        ("Arithmetic check",
         f"<span class='{'pass' if abs(diff) < 5e-5 else 'fail'}'>"
         f"{'agrees' if abs(diff) < 5e-5 else f'OUT BY {diff:+.4f} m'}</span>"),
        ("Route length", f"{run.length_m:.1f} m"),
    ]
    if misclose is not None:
        ok = abs(misclose) <= allowed
        cells += [
            ("Closing level (assumed)", f"{run.closing_rl:.4f} m"),
            ("Misclose", f"<span class='{'pass' if ok else 'fail'}'>{misclose * 1000:+.1f} mm</span>"),
            (f"Allowable ({coefficient_mm:g}&radic;K)", f"&plusmn;{allowed * 1000:.1f} mm"),
            ("Result", f"<span class='badge {'pass' if ok else 'fail'}'>"
                       f"{'WITHIN TOLERANCE' if ok else 'EXCEEDS TOLERANCE'}</span>"),
        ]
    return "<div class='summary'>" + "".join(
        f"<div><b>{k}</b>{v}</div>" for k, v in cells
    ) + "</div>"


def to_html(book: LevelBook, *, title: str = "Level Report",
            surveyor: str = "", job_note: str = "",
            coefficient_mm: float = 12.0,
            source: str = "") -> str:
    header = book.header
    meta = [
        ("Job", getattr(header, "job", "")),
        ("Instrument", f"{getattr(header, 'model', '')} s/n {getattr(header, 'serial', '')}"),
        ("Observations", str(getattr(book, "observations", 0) or "")),
        ("Level book rows", str(sum(len(r.rows) for r in book.runs))),
        ("Level runs", str(len(book.runs))),
        ("Produced", datetime.now().strftime("%d %b %Y %H:%M")),
    ]
    if surveyor:
        meta.append(("Surveyor", surveyor))
    if source:
        meta.append(("Source file", source))

    body = [
        f"<h1>{html.escape(title)}</h1>",
        f"<p class='sub'>{html.escape(job_note)}</p>" if job_note else "",
        "<div class='meta'>" + "".join(
            f"<div><b>{html.escape(k)}</b>{html.escape(str(v))}</div>" for k, v in meta
        ) + "</div>",
    ]
    for run in book.runs:
        body.append(
            f"<div class='run'><h2>Level run {run.number} &mdash; "
            f"{html.escape(run.first.point_id)} to {html.escape(run.last.point_id)}</h2>"
        )
        body.append(_run_table(run))
        body.append(_run_summary(run, coefficient_mm))
        body.append("</div>")
    body.append(
        "<p class='note'>Reduced by height of collimation: "
        "RL = (RL + backsight at the last instrument setup) &minus; this sight. "
        "Intermediate sights do not carry the instrument height forward. "
        "Levels are as observed; no misclose adjustment has been distributed.</p>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        "<body>" + "".join(body) + "</body></html>"
    )


def to_csv(book: LevelBook) -> str:
    """A flat, spreadsheet-ready CSV with real reduced levels, not formulas."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["Run", "Point", "Backsight", "Intermediate", "Foresight",
                     "Reduced Level", "Rise/Fall", "Distance", "Notes"])
    for run in book.runs:
        for row in run.rows:
            writer.writerow([
                run.number, row.point_id,
                _fmt(row.bs), _fmt(row.is_), _fmt(row.fs),
                _fmt(row.reduced_level), _fmt(row.rise_fall),
                _fmt(row.sight_distance, 2) if row.sight_distance else "",
                row.note,
            ])
    return buf.getvalue()
