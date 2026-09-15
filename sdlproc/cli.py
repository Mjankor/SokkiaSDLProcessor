"""Command line interface: ``python -m sdlproc ...``"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .fmexport import export as fm_export
from .parser import SDLParseError, parse, parse_file
from .reduce import build
from .report import to_csv, to_html


def _starts(values: list[str] | None) -> list[float | None]:
    out: list[float | None] = []
    for value in values or []:
        out.append(None if value.strip().lower() in ("", "-", "carry") else float(value))
    return out


def cmd_ports(args) -> int:
    from .serialio import SerialUnavailable, list_ports

    try:
        ports = list_ports()
    except SerialUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    if not ports:
        print("No serial ports found. Is the USB adaptor plugged in?")
        return 1
    for port in ports:
        print(f"{port.device:24s} {port.description}")
    return 0


def cmd_download(args) -> int:
    from .serialio import SerialUnavailable, capture

    try:
        print(f"Listening on {args.port} at {args.baud} {args.bytesize}{args.parity}{args.stopbits}.")
        print("Start the transfer on the instrument now...")
        result = capture(
            args.port,
            baudrate=args.baud,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=args.stopbits,
            xonxoff=args.xonxoff,
            rtscts=args.rtscts,
            raw_dir=args.raw_dir,
            idle_timeout=args.idle_timeout,
            start_timeout=args.start_timeout,
            on_progress=lambda n: print(f"\r  {n} bytes", end="", flush=True),
        )
    except SerialUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    print()
    if not result.data:
        print("Nothing received.", file=sys.stderr)
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
        print("The raw file above is intact -- send it on for diagnosis.", file=sys.stderr)
        return 1
    print(f"Parsed {len(sdl.records)} observations, job {sdl.header.job}")
    return 0


def cmd_report(args) -> int:
    sdl = parse_file(args.input)
    book = build(sdl, _starts(args.start_rl))
    stem = Path(args.input).stem
    out = Path(args.out) if args.out else Path(f"{stem}-report.html")
    out.write_text(
        to_html(book, surveyor=args.surveyor, job_note=args.note,
                coefficient_mm=args.allowance, source=Path(args.input).name),
        encoding="utf-8",
    )
    print(f"Wrote {out}")
    if args.csv:
        Path(args.csv).write_text(to_csv(book), encoding="utf-8")
        print(f"Wrote {args.csv}")
    for run in book.runs:
        mis = run.misclose
        line = (f"  Run {run.number}: {run.first.point_id}->{run.last.point_id}, "
                f"{len(run.rows)} rows, {run.length_m:.0f} m")
        if mis is not None:
            ok = abs(mis) <= run.allowable_misclose(args.allowance)
            line += (f", misclose {mis * 1000:+.1f} mm of "
                     f"{run.allowable_misclose(args.allowance) * 1000:.1f} mm"
                     f" [{'OK' if ok else 'OUT'}]")
        print(line)
    return 0 if book.misclose_ok else 1


def cmd_fmexport(args) -> int:
    book = build(parse_file(args.input))
    text = fm_export(book, args.initial)
    out = Path(args.out) if args.out else Path(f"{Path(args.input).stem}-reduced.txt")
    with open(out, "w", encoding="ascii", newline="") as fh:
        fh.write(text)
    print(f"Wrote {out} ({len(text)} bytes, FileMaker-compatible)")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdlproc",
        description="Download and reduce Sokkia SDL50 level data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("ports", help="list serial ports").set_defaults(func=cmd_ports)

    dl = subs.add_parser("download", help="capture a download from the instrument")
    dl.add_argument("--port", required=True, help="e.g. COM3 or /dev/cu.usbserial-110")
    dl.add_argument("--baud", type=int, default=9600)
    dl.add_argument("--bytesize", type=int, default=8, choices=[7, 8])
    dl.add_argument("--parity", default="N", choices=["N", "E", "O"])
    dl.add_argument("--stopbits", type=int, default=1, choices=[1, 2])
    dl.add_argument("--xonxoff", action="store_true", help="software flow control")
    dl.add_argument("--rtscts", action="store_true", help="hardware flow control")
    dl.add_argument("--idle-timeout", type=float, default=3.0)
    dl.add_argument("--start-timeout", type=float, default=180.0)
    dl.add_argument("--raw-dir", default="raw", help="where to archive the raw capture")
    dl.add_argument("-o", "--out", help="write the decoded CSV here too")
    dl.set_defaults(func=cmd_download)

    rp = subs.add_parser("report", help="reduce a CSV and write a level report")
    rp.add_argument("input")
    rp.add_argument("-o", "--out", help="output HTML (default <input>-report.html)")
    rp.add_argument("--csv", help="also write a flat CSV of reduced levels")
    rp.add_argument("--start-rl", action="append", metavar="RL",
                    help="starting reduced level for a run; repeat per run, "
                         "or 'carry' to continue from the previous run")
    rp.add_argument("--surveyor", default="")
    rp.add_argument("--note", default="")
    rp.add_argument("--allowance", type=float, default=12.0,
                    help="misclose allowance coefficient in mm (default 12 sqrt K)")
    rp.set_defaults(func=cmd_report)

    fm = subs.add_parser("fmexport",
                         help="write the legacy FileMaker tab-separated file")
    fm.add_argument("input")
    fm.add_argument("-o", "--out")
    fm.add_argument("--initial", default="", help="value for cell E1")
    fm.set_defaults(func=cmd_fmexport)

    subs.add_parser("gui", help="open the window").set_defaults(func=cmd_gui)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SDLParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
