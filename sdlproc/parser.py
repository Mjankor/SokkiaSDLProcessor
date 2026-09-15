"""Parser for the CSV format written by the Sokkia SDL50 digital level.

File shape (observed, CRLF line endings, trailing comma on every line)::

    SDL50,3310,002050,ANG020420,0,95,,,
    0001,0001,1,1,1,23.60,1.5111,100.0000,
    0002,0002,1,1,2,9.70,1.2094,100.3017,

Header fields are ``model, code, serial, job, ?, line_count``.

Data fields are ``index, point_id, ?, ?, shot_type, distance, reading,
elevation``:

``shot_type``
    ``1`` = backsight (the instrument has just been set up and is sighting
    back at a known point), ``2`` = a forward sighting.  Whether a type-2
    reading is a *foresight* or an *intermediate* is not recorded by the
    instrument -- it is inferred from what follows (see :mod:`sdlproc.reduce`).

``reading``
    Staff reading in metres.  Kept as the raw text as well as a float: the
    FileMaker-compatible export reproduces the original text verbatim, and
    round-tripping through a float would lose trailing zeros.

``elevation``
    The reduced level the *instrument* computed, starting each run from an
    assumed 100.0000.  We do not use it for our own reduction, but it is the
    signal that separates one level run from the next -- the instrument resets
    it to 100.0000 when a new run is started.
"""

from __future__ import annotations

from dataclasses import dataclass

BACKSIGHT = 1
SIGHTING = 2


class SDLParseError(ValueError):
    """Raised when a file is not recognisable as SDL50 output."""


@dataclass(frozen=True)
class Header:
    model: str
    code: str
    serial: str
    job: str
    raw: list[str]

    @property
    def description(self) -> str:
        return f"{self.model} s/n {self.serial}, job {self.job}"


@dataclass
class RawRecord:
    """One line of the instrument's CSV, before any interpretation."""

    index: str
    point_id: str
    shot_type: int
    distance: float
    reading: str  # raw text, e.g. "0.2843" -- leading/trailing zeros preserved
    elevation: float
    extra: list[str]

    @property
    def value(self) -> float:
        return float(self.reading)


@dataclass
class SDLFile:
    header: Header
    records: list[RawRecord]


def parse(text: str) -> SDLFile:
    """Parse the text of an SDL50 CSV download."""
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        raise SDLParseError("file is empty")

    head = lines[0].split(",")
    if not head[0].strip().upper().startswith("SDL"):
        raise SDLParseError(
            f"first line is not an SDL header (got {lines[0][:40]!r}); "
            "is this a raw instrument download?"
        )
    header = Header(
        model=head[0].strip(),
        code=head[1].strip() if len(head) > 1 else "",
        serial=head[2].strip() if len(head) > 2 else "",
        job=head[3].strip() if len(head) > 3 else "",
        raw=head,
    )

    records: list[RawRecord] = []
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
        records.append(
            RawRecord(
                index=parts[0].strip(),
                point_id=parts[1].strip(),
                shot_type=shot_type,
                distance=distance,
                reading=parts[6].strip(),
                elevation=elevation,
                extra=parts[2:4],
            )
        )
    if not records:
        raise SDLParseError("file contains a header but no observations")
    return SDLFile(header=header, records=records)


def parse_file(path) -> SDLFile:
    with open(path, "r", encoding="ascii", errors="replace", newline="") as fh:
        return parse(fh.read())
