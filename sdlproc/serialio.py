"""Capture an SDL50 download from a USB-serial adaptor.

The SDL50 pushes stored data out of its RS-232 port when the operator starts
the transfer from the instrument's own menu; the host does not request
anything.  So the capture model here is deliberately dumb: open the port, wait
for the first byte, then read until the line has been quiet long enough that
the transfer must be over.

Every capture writes the bytes to a ``.raw`` file **before** anything tries to
interpret them.  If a download ever fails to parse, that file is the evidence,
and it can be replayed through the rest of the app offline.  It is also worth
keeping for its own sake: it is the unmodified record of what the instrument
sent, which is the thing an audit would want to see.

``pyserial`` is imported lazily so that parsing, reduction and reporting all
work in an environment where it is not installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_BAUD = 9600
DEFAULT_BYTESIZE = 8
DEFAULT_PARITY = "N"
DEFAULT_STOPBITS = 1


class SerialUnavailable(RuntimeError):
    """pyserial is not installed."""


def _serial():
    try:
        import serial  # noqa: PLC0415
        import serial.tools.list_ports  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SerialUnavailable(
            "pyserial is not installed. Install it with: pip install pyserial"
        ) from exc
    return serial


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str

    def __str__(self) -> str:
        return f"{self.device}  ({self.description})"


def list_ports() -> list[PortInfo]:
    """Every serial port the OS can see.

    Names differ by platform -- ``COM3`` on Windows, ``/dev/cu.usbserial-*`` on
    macOS, ``/dev/ttyUSB*`` on Linux -- but pyserial enumerates all three the
    same way.
    """
    serial = _serial()
    from serial.tools import list_ports as lp  # noqa: PLC0415

    return [PortInfo(p.device, p.description or "", p.hwid or "") for p in lp.comports()]


def likely_adaptors() -> list[PortInfo]:
    """Ports that look like a USB-serial adaptor rather than a built-in port."""
    keep = ("usbserial", "usbmodem", "ttyusb", "ttyacm", "ftdi", "ch340",
            "cp210", "pl2303", "prolific", "silicon labs", "com")
    out = []
    for port in list_ports():
        blob = f"{port.device} {port.description} {port.hwid}".lower()
        if any(k in blob for k in keep):
            out.append(port)
    return out or list_ports()


@dataclass
class CaptureResult:
    data: bytes
    raw_path: Path | None
    seconds: float
    port: str

    @property
    def text(self) -> str:
        return self.data.decode("ascii", errors="replace")


def capture(
    port: str,
    *,
    baudrate: int = DEFAULT_BAUD,
    bytesize: int = DEFAULT_BYTESIZE,
    parity: str = DEFAULT_PARITY,
    stopbits: int = DEFAULT_STOPBITS,
    xonxoff: bool = False,
    rtscts: bool = False,
    dsrdtr: bool = False,
    start_timeout: float = 180.0,
    idle_timeout: float = 3.0,
    raw_dir: Path | str | None = None,
    job_hint: str = "download",
    on_progress=None,
) -> CaptureResult:
    """Capture one download.

    ``start_timeout``
        How long to wait for the operator to start the transfer on the
        instrument before giving up.

    ``idle_timeout``
        How long the line must stay quiet before the transfer is considered
        finished.  Three seconds is comfortably longer than any gap the
        instrument leaves between records at 9600 baud, where a 40-character
        record takes about 40 ms.

    ``on_progress``
        Optional callable receiving the running byte count, for a UI.
    """
    serial = _serial()
    chunks: list[bytes] = []
    started = time.monotonic()

    with serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
        xonxoff=xonxoff,
        rtscts=rtscts,
        dsrdtr=dsrdtr,
        timeout=0.25,
    ) as link:
        link.reset_input_buffer()
        deadline = time.monotonic() + start_timeout
        seen_any = False
        last_byte_at = None

        while True:
            waiting = link.in_waiting
            data = link.read(waiting or 1)
            now = time.monotonic()
            if data:
                chunks.append(data)
                seen_any = True
                last_byte_at = now
                if on_progress:
                    on_progress(sum(len(c) for c in chunks))
            elif seen_any and last_byte_at and now - last_byte_at >= idle_timeout:
                break
            elif not seen_any and now >= deadline:
                break

    payload = b"".join(chunks)
    raw_path = None
    if raw_dir is not None and payload:
        raw_dir = Path(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_path = raw_dir / f"{job_hint}-{stamp}.raw"
        raw_path.write_bytes(payload)

    return CaptureResult(
        data=payload,
        raw_path=raw_path,
        seconds=time.monotonic() - started,
        port=port,
    )
