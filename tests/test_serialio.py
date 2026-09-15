"""Exercise the capture loop over a pseudo-terminal pair.

There is no instrument here, so the test stands in for one: it writes the
sample download into one end of a pty and lets :func:`sdlproc.serialio.capture`
read it from the other exactly as it would read a USB-serial adaptor.
"""

from __future__ import annotations

import os
import pty
import threading
import time
from pathlib import Path

import pytest

from sdlproc.parser import parse
from sdlproc.serialio import capture, list_ports

pytest.importorskip("serial")

DATA = Path(__file__).parent / "data"


def test_list_ports_does_not_raise():
    assert isinstance(list_ports(), list)


def test_capture_reads_a_download_and_archives_the_raw_bytes(tmp_path):
    payload = DATA.joinpath("AZM020420.csv").read_bytes()
    controller, peripheral = pty.openpty()
    name = os.ttyname(peripheral)

    def instrument():
        # Wait for the capture to open the port, then push the data out in
        # chunks the way the instrument trickles records down the line.
        time.sleep(0.3)
        for start in range(0, len(payload), 256):
            os.write(controller, payload[start:start + 256])
            time.sleep(0.01)

    sender = threading.Thread(target=instrument, daemon=True)
    sender.start()

    result = capture(
        name,
        baudrate=9600,
        raw_dir=tmp_path / "raw",
        job_hint="ptytest",
        start_timeout=10.0,
        idle_timeout=1.0,
    )
    sender.join(timeout=5)
    os.close(controller)
    os.close(peripheral)

    assert result.data == payload
    assert result.raw_path is not None and result.raw_path.exists()
    assert result.raw_path.read_bytes() == payload

    sdl = parse(result.text)
    assert len(sdl.records) == 94
    assert sdl.header.job == "ANG020420"


def test_capture_returns_empty_when_nothing_is_sent(tmp_path):
    controller, peripheral = pty.openpty()
    name = os.ttyname(peripheral)
    result = capture(name, raw_dir=tmp_path, start_timeout=0.5, idle_timeout=0.5)
    os.close(controller)
    os.close(peripheral)
    assert result.data == b""
    assert result.raw_path is None
