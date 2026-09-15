"""The golden test: the port must reproduce the FileMaker output exactly.

``AZM020420.csv`` is a real download from the instrument and
``AZM020420-reduced.txt`` is the file the FileMaker database produced from it.
If these two stay in step, the reduction logic is right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sdlproc.fmexport import export, fm_number
from sdlproc.parser import SDLParseError, parse, parse_file
from sdlproc.reduce import NEW_RUN_NOTE, build

DATA = Path(__file__).parent / "data"
RAW = DATA / "AZM020420.csv"
GOLD = DATA / "AZM020420-reduced.txt"

# The value the operator typed into the "Initial Level Value" dialog when the
# sample report was produced; it lands verbatim in cell E1.
INITIAL = "74.614"


@pytest.fixture(scope="module")
def sdl():
    return parse_file(RAW)


@pytest.fixture(scope="module")
def book(sdl):
    return build(sdl)


def test_header(sdl):
    assert sdl.header.model == "SDL50"
    assert sdl.header.serial == "002050"
    assert sdl.header.job == "ANG020420"
    assert len(sdl.records) == 94


def test_matches_filemaker_export_byte_for_byte(book):
    with open(GOLD, "r", encoding="ascii", newline="") as fh:
        expected = fh.read()
    assert export(book, INITIAL) == expected


def test_row_and_run_counts(book):
    assert len(book.rows) == 52
    assert len(book.runs) == 2
    assert [len(r.rows) for r in book.runs] == [17, 35]


def test_new_run_is_marked(book):
    notes = [(i, r.point_id) for i, r in enumerate(book.rows) if r.note == NEW_RUN_NOTE]
    assert notes == [(17, "0017")]
    assert book.rows[17].backsight == "1.1698"
    assert book.rows[17].foresight is None


def test_intermediates_do_not_carry_the_instrument_height(book):
    # Point 0006 is an intermediate; point 0007 is reduced from the setup at
    # 0005, not from 0006.
    row = next(r for r in book.rows if r.point_id == "0006")
    assert row.intermediate == "2.3492"
    assert row.backsight is None and row.foresight is None


def test_mis_keyed_point_is_still_merged(book):
    """A foresight to 0019 followed by a backsight labelled 0020.

    The instrument's elevation is unchanged across the pair, so it is one
    setup and the two records merge -- the point number was simply mis-keyed.
    The merged row keeps the foresight's point ID and 0020 does not appear.
    """
    row = next(r for r in book.rows if r.point_id == "0019")
    assert row.foresight == "1.6511"
    assert row.backsight == "1.6443"
    assert not any(r.point_id == "0020" for r in book.rows)


def test_reduced_levels_follow_the_instrument(sdl):
    """Our levels must differ from the instrument's only by the datum shift."""
    book = build(sdl, [74.614])
    run = book.runs[0]
    offset = 74.614 - 100.0
    for row in run.rows:
        if row.reduced_level is not None:
            assert row.reduced_level == pytest.approx(row.instrument_elevation + offset, abs=5e-5)


def test_arithmetic_check_agrees(sdl):
    book = build(sdl, [74.614])
    for run in book.runs:
        _sums, _levels, diff = run.arithmetic_check
        assert abs(diff) < 1e-9


def test_misclose_within_allowance(sdl):
    book = build(sdl, [74.614])
    assert book.misclose_ok
    assert book.runs[0].misclose == pytest.approx(0.0017, abs=5e-5)
    assert book.runs[1].misclose == pytest.approx(0.0001, abs=5e-5)


def test_second_run_carries_forward(sdl):
    book = build(sdl, [74.614])
    assert book.runs[1].start_rl == pytest.approx(book.runs[0].last.reduced_level)


def test_explicit_start_for_each_run(sdl):
    book = build(sdl, [100.0, 50.0])
    assert book.runs[0].rows[0].reduced_level == pytest.approx(100.0)
    assert book.runs[1].rows[0].reduced_level == pytest.approx(50.0)


@pytest.mark.parametrize(
    "raw,expected",
    [("0.9554", ".9554"), ("1.5150", "1.515"), ("2.3492", "2.3492"),
     ("1.7840", "1.784"), ("100.0000", "100"), ("", ""), (None, "")],
)
def test_filemaker_number_rendering(raw, expected):
    assert fm_number(raw) == expected


def test_backsight_keeps_its_original_text(book):
    """FileMaker never recomputes the backsight, so 0.2843 keeps its zero."""
    text = export(book, INITIAL)
    assert "\t0.2843\t" in text  # backsight column
    assert "\t.9554\t" in text   # foresight column, same magnitude


def test_rejects_a_file_that_is_not_sdl_output():
    with pytest.raises(SDLParseError, match="not an SDL header"):
        parse("date,northing,easting\n1,2,3\n")


def test_rejects_a_truncated_record():
    with pytest.raises(SDLParseError, match="expected at least 8 fields"):
        parse("SDL50,3310,002050,JOB,0,2,,,\r\n0001,0001,1,1\r\n")
