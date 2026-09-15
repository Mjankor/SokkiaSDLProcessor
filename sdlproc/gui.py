"""A single window: download, reduce, report.

Deliberately plain Tkinter -- it ships with Python on Windows and macOS, so
the packaged app has no GUI dependency to install or break.  Everything it
does is a thin call into the core modules; no reduction logic lives here.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .fmexport import export as fm_export
from .parser import SDLParseError, parse, parse_file
from .reduce import build
from .report import to_csv, to_html
from .serialio import DEFAULT_BAUD, SerialUnavailable, capture, likely_adaptors


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self.sdl = None
        self.book = None
        self.source_name = ""
        self.raw_bytes = b""
        self.start_vars: list[tk.StringVar] = []
        self.events: queue.Queue = queue.Queue()

        self._build_connection()
        self._build_job()
        self._build_table()
        self._build_actions()
        self._set_status("Ready. Connect the instrument, or open a downloaded file.")
        self.after(100, self._drain)

    # -- layout ----------------------------------------------------------
    def _build_connection(self) -> None:
        box = ttk.LabelFrame(self, text="1. Download from instrument", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)  # only the port combo grows

        ttk.Label(box, text="Port").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.port = ttk.Combobox(box, state="readonly", width=34, values=[])
        self.port.grid(row=0, column=1, sticky="ew")
        ttk.Button(box, text="Refresh", command=self.refresh_ports, width=8).grid(
            row=0, column=2, padx=6)

        self.baud = tk.StringVar(value=str(DEFAULT_BAUD))
        self.parity = tk.StringVar(value="N")
        self.databits = tk.StringVar(value="8")
        self.stopbits = tk.StringVar(value="1")
        self.flow = tk.StringVar(value="none")

        def combo(label, var, values, width, col):
            ttk.Label(box, text=label).grid(row=0, column=col, sticky="e", padx=(10, 4))
            widget = ttk.Combobox(box, textvariable=var, values=values,
                                  width=width, state="readonly")
            widget.grid(row=0, column=col + 1, sticky="w")

        combo("Baud", self.baud, ["1200", "2400", "4800", "9600", "19200", "38400"], 7, 3)
        combo("Data", self.databits, ["7", "8"], 3, 5)
        combo("Parity", self.parity, ["N", "E", "O"], 3, 7)
        combo("Stop", self.stopbits, ["1", "2"], 3, 9)
        combo("Flow", self.flow, ["none", "xon/xoff", "rts/cts"], 9, 11)

        self.download_button = ttk.Button(box, text="Download", command=self.start_download)
        self.download_button.grid(row=0, column=13, padx=(12, 0))
        ttk.Button(box, text="Open file…", command=self.open_file).grid(
            row=0, column=14, padx=(6, 0))

    def _build_job(self) -> None:
        box = ttk.LabelFrame(self, text="2. Job details", padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(5, weight=1)

        self.surveyor = tk.StringVar()
        self.note = tk.StringVar()
        self.allowance = tk.StringVar(value="12")

        ttk.Label(box, text="Surveyor").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(box, textvariable=self.surveyor).grid(row=0, column=1, sticky="ew")
        ttk.Label(box, text="Note").grid(row=0, column=2, sticky="w", padx=(12, 6))
        ttk.Entry(box, textvariable=self.note).grid(row=0, column=3, sticky="ew")
        ttk.Label(box, text="Allowance (mm√K)").grid(row=0, column=4, sticky="e", padx=(12, 6))
        ttk.Entry(box, textvariable=self.allowance, width=6).grid(row=0, column=5, sticky="w")

        self.runs_frame = ttk.Frame(box)
        self.runs_frame.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))

    def _build_table(self) -> None:
        box = ttk.LabelFrame(self, text="3. Reduced levels", padding=8)
        box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        columns = ("run", "point", "bs", "is", "fs", "rl", "dist", "note")
        headings = ("Run", "Point", "Backsight", "Inter.", "Foresight",
                    "Reduced level", "Dist (m)", "Notes")
        widths = (44, 70, 90, 90, 90, 110, 80, 190)
        self.tree = ttk.Treeview(box, columns=columns, show="headings", height=16)
        for col, head, width in zip(columns, headings, widths):
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

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bar.columnconfigure(0, weight=1)
        self.status = ttk.Label(bar, text="", anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")
        self.save_report = ttk.Button(bar, text="Save report…",
                                      command=self.do_save_report, state="disabled")
        self.save_report.grid(row=0, column=1, padx=4)
        self.save_csv = ttk.Button(bar, text="Save CSV…",
                                   command=self.do_save_csv, state="disabled")
        self.save_csv.grid(row=0, column=2, padx=4)
        self.save_legacy = ttk.Button(bar, text="Save FileMaker format…",
                                      command=self.do_save_legacy, state="disabled")
        self.save_legacy.grid(row=0, column=3, padx=4)

    # -- helpers ---------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def refresh_ports(self) -> None:
        try:
            ports = likely_adaptors()
        except SerialUnavailable as exc:
            messagebox.showerror("Serial support missing", str(exc))
            return
        self.port.configure(values=[str(p) for p in ports])
        self._ports = ports
        if ports and not self.port.get():
            self.port.current(0)
        self._set_status(f"{len(ports)} serial port(s) found." if ports
                         else "No serial ports found -- check the adaptor is plugged in.")

    def _selected_port(self) -> str | None:
        choice = self.port.get()
        for port in getattr(self, "_ports", []):
            if str(port) == choice:
                return port.device
        return choice.split()[0] if choice else None

    # -- download --------------------------------------------------------
    def start_download(self) -> None:
        device = self._selected_port()
        if not device:
            messagebox.showwarning("No port", "Choose the serial port first.")
            return
        self.download_button.configure(state="disabled")
        self._set_status("Waiting -- start the transfer on the instrument…")
        flow = self.flow.get()
        kwargs = dict(
            baudrate=int(self.baud.get()),
            bytesize=int(self.databits.get()),
            parity=self.parity.get(),
            stopbits=int(self.stopbits.get()),
            xonxoff=flow == "xon/xoff",
            rtscts=flow == "rts/cts",
            raw_dir=Path.cwd() / "raw",
        )

        def work():
            try:
                result = capture(
                    device,
                    on_progress=lambda n: self.events.put(("progress", n)),
                    **kwargs,
                )
                self.events.put(("done", result))
            except Exception as exc:  # surfaced in the UI thread
                self.events.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._set_status(f"Receiving… {payload} bytes")
                elif kind == "error":
                    self.download_button.configure(state="normal")
                    messagebox.showerror("Download failed", str(payload))
                    self._set_status("Download failed.")
                elif kind == "done":
                    self.download_button.configure(state="normal")
                    self._finish_download(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _finish_download(self, result) -> None:
        if not result.data:
            self._set_status("Nothing received. Check the port, baud rate and cable.")
            messagebox.showwarning(
                "Nothing received",
                "No data arrived.\n\nCheck the port and baud rate, that the cable is "
                "in the instrument's RS-232 socket, and that the transfer was started "
                "on the instrument.")
            return
        self.raw_bytes = result.data
        where = f"\nRaw capture saved to {result.raw_path}" if result.raw_path else ""
        try:
            self.sdl = parse(result.text)
        except SDLParseError as exc:
            messagebox.showerror(
                "Could not read the download",
                f"{len(result.data)} bytes arrived but could not be parsed:\n\n{exc}{where}")
            self._set_status("Download captured but not understood -- raw file kept.")
            return
        self.source_name = f"{self.sdl.header.job} (serial)"
        self._set_status(f"Received {len(result.data)} bytes, "
                         f"{len(self.sdl.records)} observations.{where}")
        self.rebuild()

    # -- file ------------------------------------------------------------
    def open_file(self) -> None:
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
        self._set_status(f"Loaded {self.source_name}: {len(self.sdl.records)} observations.")
        self.rebuild()

    # -- reduce and display ----------------------------------------------
    def rebuild(self, *_args) -> None:
        if self.sdl is None:
            return
        starts: list[float | None] = []
        for var in self.start_vars:
            text = var.get().strip()
            try:
                starts.append(float(text) if text else None)
            except ValueError:
                starts.append(None)
        self.book = build(self.sdl, starts)
        self._sync_run_inputs()
        self._fill_table()
        self._fill_summary()
        for button in (self.save_report, self.save_csv, self.save_legacy):
            button.configure(state="normal")

    def _sync_run_inputs(self) -> None:
        if len(self.start_vars) == len(self.book.runs):
            return
        for child in self.runs_frame.winfo_children():
            child.destroy()
        self.start_vars = []
        ttk.Label(self.runs_frame, text="Starting reduced level for each run:").grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        for i, run in enumerate(self.book.runs):
            var = tk.StringVar(value=f"{run.start_rl:.4f}" if i == 0 and run.start_rl else "")
            self.start_vars.append(var)
            ttk.Label(self.runs_frame,
                      text=f"Run {run.number} ({run.first.point_id})").grid(
                row=0, column=1 + i * 2, sticky="e", padx=(10, 4))
            entry = ttk.Entry(self.runs_frame, textvariable=var, width=11)
            entry.grid(row=0, column=2 + i * 2, sticky="w")
            entry.bind("<Return>", self.rebuild)
            entry.bind("<FocusOut>", self.rebuild)
        ttk.Label(self.runs_frame,
                  text="(blank = carry on from the previous run)").grid(
            row=0, column=1 + len(self.book.runs) * 2, sticky="w", padx=(12, 0))

    def _fill_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for run in self.book.runs:
            for row in run.rows:
                tags = ()
                if row.starts_run and run.number > 1:
                    tags = ("newrun",)
                elif row.intermediate:
                    tags = ("inter",)
                self.tree.insert("", "end", tags=tags, values=(
                    run.number, row.point_id,
                    f"{row.bs:.4f}" if row.bs is not None else "",
                    f"{row.is_:.4f}" if row.is_ is not None else "",
                    f"{row.fs:.4f}" if row.fs is not None else "",
                    f"{row.reduced_level:.4f}" if row.reduced_level is not None else "",
                    f"{row.sight_distance:.2f}" if row.sight_distance else "",
                    row.note,
                ))

    def _fill_summary(self) -> None:
        try:
            coefficient = float(self.allowance.get())
        except ValueError:
            coefficient = 12.0
        lines = []
        for run in self.book.runs:
            _s, _l, diff = run.arithmetic_check
            misclose = run.misclose
            allowed = run.allowable_misclose(coefficient)
            part = (f"Run {run.number}: {run.first.point_id}→{run.last.point_id}, "
                    f"{len(run.rows)} rows, {run.length_m:.0f} m.  "
                    f"Arithmetic {'agrees' if abs(diff) < 5e-5 else f'OUT by {diff:+.4f} m'}.")
            if misclose is not None:
                ok = abs(misclose) <= allowed
                part += (f"  Misclose {misclose * 1000:+.1f} mm of "
                         f"±{allowed * 1000:.1f} mm — "
                         f"{'within tolerance' if ok else 'EXCEEDS TOLERANCE'}.")
            lines.append(part)
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state="disabled")

    # -- output ----------------------------------------------------------
    def _default_stem(self) -> str:
        job = getattr(self.book.header, "job", "") or "levels"
        return job

    def do_save_report(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".html", initialfile=f"{self._default_stem()}-report.html",
            filetypes=[("HTML report", "*.html")])
        if not path:
            return
        try:
            coefficient = float(self.allowance.get())
        except ValueError:
            coefficient = 12.0
        Path(path).write_text(
            to_html(self.book, surveyor=self.surveyor.get(), job_note=self.note.get(),
                    coefficient_mm=coefficient, source=self.source_name),
            encoding="utf-8")
        self._set_status(f"Report written to {path}")
        if messagebox.askyesno("Report saved",
                               "Open it now?\n\nUse the browser's Print → Save as PDF "
                               "to produce a PDF."):
            webbrowser.open(Path(path).resolve().as_uri())

    def do_save_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=f"{self._default_stem()}-levels.csv",
            filetypes=[("CSV", "*.csv")])
        if path:
            Path(path).write_text(to_csv(self.book), encoding="utf-8")
            self._set_status(f"CSV written to {path}")

    def do_save_legacy(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile=f"{self._default_stem()}-reduced.txt",
            filetypes=[("Tab separated", "*.txt")])
        if not path:
            return
        initial = self.start_vars[0].get().strip() if self.start_vars else ""
        with open(path, "w", encoding="ascii", newline="") as fh:
            fh.write(fm_export(self.book, initial))
        self._set_status(f"FileMaker-format file written to {path}")


def main() -> int:
    root = tk.Tk()
    root.title("Sokkia SDL50 — level processor")
    root.geometry("1180x780")
    app = App(root)
    try:
        app.refresh_ports()
    except Exception:
        pass
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
