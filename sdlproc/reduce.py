"""Turn raw SDL50 records into a reduced level book.

This is a port of the five FileMaker scripts in ``Levels_and_Projection_Calcs``
(``Level Import`` / ``Level Formatter`` / ``Merge Levels`` / ``Level Calcs`` /
``Level Export``).  The stages are kept separate and in the same order as the
originals so the two can be compared script-by-script.

Stage 1 -- classify (``Level Formatter``)
    The instrument only distinguishes "backsight" (type 1) from "forward
    sighting" (type 2).  A type-2 reading is a **foresight** if the next
    record is a backsight (the instrument moved, so this was the last sight
    from that setup) and an **intermediate** if the next record is another
    type-2 (still shooting from the same setup).  The final record, if type 2,
    is a foresight.

Stage 2 -- merge (``Merge Levels``)
    Each instrument setup produces two records for the same physical point: a
    foresight from the old setup and a backsight from the new one.  They are
    merged into a single level-book row.  The pair is recognised by the
    instrument's own elevation being unchanged across the two records.  When a
    backsight follows a foresight with a *different* elevation, the instrument
    was restarted -- that row begins a new level run.

Stage 3 -- reduce (``Level Calcs``)
    Rise-and-fall via the height of collimation::

        RL(this) = RL(last backsight row) + BS(last backsight row) - FS(this)

    An intermediate uses the same instrument height but does not become one
    itself, so the search for "last backsight row" skips over intermediates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parser import BACKSIGHT, SIGHTING, RawRecord, SDLFile

NEW_RUN_NOTE = "New level run started"


@dataclass
class Row:
    """One row of the reduced level book (one line of the printed report)."""

    point_id: str
    backsight: str | None = None  # raw text as downloaded
    intermediate: str | None = None
    foresight: str | None = None
    note: str = ""

    # Provenance / working values.
    instrument_elevation: float = 0.0
    bs_distance: float | None = None
    fs_distance: float | None = None
    source_index: str = ""
    reduced_level: float | None = None
    rise_fall: float | None = None
    starts_run: bool = False
    run: int = 0

    @property
    def bs(self) -> float | None:
        return None if self.backsight in (None, "") else float(self.backsight)

    @property
    def is_(self) -> float | None:
        return None if self.intermediate in (None, "") else float(self.intermediate)

    @property
    def fs(self) -> float | None:
        return None if self.foresight in (None, "") else float(self.foresight)

    @property
    def sight_distance(self) -> float:
        return (self.bs_distance or 0.0) + (self.fs_distance or 0.0)


@dataclass
class Run:
    """A contiguous level run: everything between two instrument restarts."""

    number: int
    rows: list[Row] = field(default_factory=list)
    start_rl: float | None = None
    closing_rl: float | None = None  # known RL of the closing point, if any

    @property
    def first(self) -> Row:
        return self.rows[0]

    @property
    def last(self) -> Row:
        return self.rows[-1]

    @property
    def sum_backsights(self) -> float:
        return sum(r.bs for r in self.rows if r.bs is not None)

    @property
    def sum_foresights(self) -> float:
        return sum(r.fs for r in self.rows if r.fs is not None)

    @property
    def length_m(self) -> float:
        """Route length: the sum of the backsight and foresight distances.

        Intermediates are side shots off the route and are excluded, which is
        what the misclose allowance is meant to be measured against.
        """
        return sum(r.sight_distance for r in self.rows)

    @property
    def arithmetic_check(self) -> tuple[float, float, float]:
        """``(sum BS - sum FS, last RL - first RL, difference)``.

        The first two must agree; the difference is a check on the reduction
        arithmetic only, not on the observations.
        """
        sums = self.sum_backsights - self.sum_foresights
        levels = (self.last.reduced_level or 0.0) - (self.first.reduced_level or 0.0)
        return sums, levels, sums - levels

    @property
    def misclose(self) -> float | None:
        """Computed closing RL minus the known closing RL, in metres.

        ``None`` when the run has no declared closing value.
        """
        if self.closing_rl is None or self.last.reduced_level is None:
            return None
        return self.last.reduced_level - self.closing_rl

    def allowable_misclose(self, coefficient_mm: float = 12.0) -> float:
        """Allowable misclose in metres: ``coefficient * sqrt(K)`` mm.

        ``K`` is the route length in kilometres.  12 mm is the usual
        general-purpose levelling allowance; the coefficient is a parameter so
        a job run to a tighter spec can be checked against it.
        """
        km = self.length_m / 1000.0
        return coefficient_mm * (km ** 0.5) / 1000.0


@dataclass
class LevelBook:
    header: object
    rows: list[Row]
    runs: list[Run]
    observations: int = 0  # raw records downloaded, before pairing

    @property
    def misclose_ok(self) -> bool:
        return all(
            r.misclose is None or abs(r.misclose) <= r.allowable_misclose() for r in self.runs
        )


# --------------------------------------------------------------------------
# Stage 1 -- classify
# --------------------------------------------------------------------------

FORESIGHT = "FS"
INTERMEDIATE = "IS"


def classify(records: list[RawRecord]) -> list[str]:
    """Label each record ``"BS"``, ``"FS"`` or ``"IS"``.

    Port of ``Level Formatter``.  The original walks the found set with a
    one-record lookahead; this is the same rule expressed directly.
    """
    roles: list[str] = []
    for i, rec in enumerate(records):
        if rec.shot_type == BACKSIGHT:
            roles.append("BS")
        elif i + 1 >= len(records):
            roles.append(FORESIGHT)  # last record: nothing follows it
        elif records[i + 1].shot_type == BACKSIGHT:
            roles.append(FORESIGHT)
        else:
            roles.append(INTERMEDIATE)
    return roles


# --------------------------------------------------------------------------
# Stage 2 -- merge
# --------------------------------------------------------------------------


def merge(records: list[RawRecord], roles: list[str], *, match_point_id: bool = False) -> list[Row]:
    """Collapse each foresight/backsight pair into one level-book row.

    Port of ``Merge Levels``.

    ``match_point_id``
        The FileMaker script's condition also tests
        ``Last Point ID = Point ID``, but ``Last Point ID`` is a *per-record*
        field while ``Last EL`` beside it is *global* -- so after the script
        steps to the next record that test reads the new record's own (empty)
        field and can never be true.  The exported report proves the merge is
        meant to run on the elevation test alone: in the sample job the
        operator mis-keyed a point number (a foresight to 0019 followed by a
        backsight labelled 0020) and the two were still merged.  We therefore
        default to the elevation test only, and keep the stricter behaviour
        available for comparison.
    """
    rows: list[Row] = []
    i = 0
    n = len(records)
    while i < n:
        rec = records[i]
        role = roles[i]
        row = Row(
            point_id=rec.point_id,
            source_index=rec.index,
            instrument_elevation=rec.elevation,
        )
        if role == "BS":
            row.backsight = rec.reading
            row.bs_distance = rec.distance
        elif role == INTERMEDIATE:
            row.intermediate = rec.reading
        else:
            row.foresight = rec.reading
            row.fs_distance = rec.distance
        rows.append(row)

        j = i + 1
        if j < n and records[j].shot_type == BACKSIGHT:
            same_elevation = records[i].elevation == records[j].elevation
            same_point = records[i].point_id == records[j].point_id
            pair = same_elevation and (same_point or not match_point_id)
            if pair:
                # The backsight belongs to the row we just emitted.
                row.backsight = records[j].reading
                row.bs_distance = records[j].distance
                i = j + 1
                continue
            if same_point or not match_point_id:
                # Same point, different elevation: the instrument was
                # restarted, so the backsight row opens a new run.
                rows.append(
                    Row(
                        point_id=records[j].point_id,
                        backsight=records[j].reading,
                        bs_distance=records[j].distance,
                        note=NEW_RUN_NOTE,
                        starts_run=True,
                        source_index=records[j].index,
                        instrument_elevation=records[j].elevation,
                    )
                )
                i = j + 1
                continue
        i = j
    if rows:
        rows[0].starts_run = True
    return rows


# --------------------------------------------------------------------------
# Stage 3 -- reduce
# --------------------------------------------------------------------------


def last_backsight_row(rows: list[Row], before: int) -> int | None:
    """Index of the nearest row at or before ``before - 1`` carrying a backsight."""
    for k in range(before - 1, -1, -1):
        if rows[k].backsight not in (None, ""):
            return k
    return None


def split_runs(rows: list[Row]) -> list[Run]:
    runs: list[Run] = []
    for row in rows:
        if row.starts_run or not runs:
            runs.append(Run(number=len(runs) + 1))
        row.run = runs[-1].number
        runs[-1].rows.append(row)
    return runs


def reduce_levels(rows: list[Run] | list[Row], start_rls: list[float | None]) -> None:
    """Fill in ``reduced_level`` for every row, in place.

    ``start_rls`` supplies the starting reduced level for each run, in order.
    A ``None`` entry means "carry on from the previous run's last computed
    level", which is the right default when a run picks up where the last one
    closed.
    """
    runs = split_runs(rows) if rows and isinstance(rows[0], Row) else rows
    flat = [r for run in runs for r in run.rows]
    by_number = {run.number: run for run in runs}

    for i, row in enumerate(flat):
        if row.starts_run:
            run = by_number[row.run]
            run_no = runs.index(run)
            start = start_rls[run_no] if run_no < len(start_rls) else None
            if start is None:
                # Carry on from where the previous run closed.  This is
                # resolved here rather than up front because the previous
                # run's closing level is only known once it has been reduced.
                prev = runs[run_no - 1].last.reduced_level if run_no else None
                start = prev if prev is not None else 0.0
            run.start_rl = start
            row.reduced_level = start
            row.rise_fall = None
            continue
        k = last_backsight_row(flat, i)
        if k is None or flat[k].reduced_level is None or flat[k].bs is None:
            row.reduced_level = None
            continue
        hi = flat[k].reduced_level + flat[k].bs
        sight = row.fs if row.fs is not None else row.is_
        if sight is None:
            row.reduced_level = None
            continue
        row.reduced_level = hi - sight
        row.rise_fall = row.reduced_level - flat[k].reduced_level


def build(sdl: SDLFile, start_rls: list[float | None] | None = None, *,
          match_point_id: bool = False) -> LevelBook:
    """Run the whole pipeline: raw records in, reduced level book out."""
    roles = classify(sdl.records)
    rows = merge(sdl.records, roles, match_point_id=match_point_id)
    runs = split_runs(rows)
    starts = list(start_rls or [])
    while len(starts) < len(runs):
        starts.append(None)
    reduce_levels(runs, starts)
    for run in runs:
        if run.closing_rl is None:
            # A run that returns to where it started is the common case; the
            # report states the assumption and the operator can override it.
            run.closing_rl = run.start_rl
    return LevelBook(header=sdl.header, rows=rows, runs=runs,
                     observations=len(sdl.records))
