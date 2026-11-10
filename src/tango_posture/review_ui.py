"""In-app reports, local video replay and asynchronous Ollama review."""

import json
import math
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from functools import partial
from http.client import HTTPException
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import filedialog, ttk
from tkinter import font as tkfont
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import cv2
import numpy as np

from .archive import (
    PracticeArchive,
    calibration_directory,
    file_sha256,
    movement_member_name,
    practice_directory,
    read_movement_analysis,
)
from .calibration import load_calibration_file
from .config import Config
from .movement import (
    MovementAnalysisCoordinator,
    RecordingTarget,
    render_floor_guide,
    render_movement_overlay,
    validate_movement_analysis,
)
from .notes_ui import NotesSidebar
from .ollama import OllamaClient
from .pose.mediapipe_backend import MediaPipeEstimator
from .reference_import import (
    MAX_IMPORT_RANGE_SECONDS,
    ClipComparisonResult,
    ImportedClipResult,
    ReferenceImportWorker,
    VideoProbeInfo,
    YouTubeClockAlignment,
    YouTubeSourceLink,
    build_youtube_open_url,
    check_calibration_readiness,
    coach_notes_for_frame,
    compare_clips,
    download_youtube_video,
    imports_directory,
    load_report_coach_data,
    load_report_movement_data,
    parse_youtube_url,
    probe_import_video,
    relink_report_video,
    render_coach_frame_overlay,
    validate_clip_range,
)
from .replay import (
    ReplayPlayer,
    cut_video_segment,
    parse_time,
    save_overlay_video,
    time_label,
)
from .review import (
    ReportRecord,
    read_report,
    report_directory,
    save_analysis,
    saved_analyses,
    update_report_metadata,
)


def _render_replay_overlay_frame(
    image: np.ndarray,
    frame_index: int,
    fps: float,
    *,
    stream: str,
    movement_data: dict[str, Any] | None,
    coach_data: dict[str, Any] | None,
    cfg: Config,
    show_coach: bool,
    show_pivots: bool,
    show_steps: bool,
    floor_only: bool,
    step_history_seconds: float,
    pane_label: str | None = None,
) -> np.ndarray:
    dynamic_overlay = stream in ("Dynamic overlay", "Dynamic lines-only")
    if stream not in ("Raw video", "Source video", "Opened video") and not dynamic_overlay:
        return image
    if dynamic_overlay:
        show_coach = bool(show_coach or coach_data is not None)
        show_pivots = bool(show_pivots or (movement_data and movement_data.get("pivots")))
        show_steps = bool(show_steps or (movement_data and movement_data.get("steps")))
        floor_only = floor_only or stream == "Dynamic lines-only"
    result = render_floor_guide(image) if floor_only else image
    if (
        (show_pivots or show_steps)
        and movement_data is not None
        and result.shape[1] == movement_data.get("width")
        and result.shape[0] == movement_data.get("height")
    ):
        result = render_movement_overlay(
            result,
            movement_data,
            frame_index,
            fps,
            show_pivots=show_pivots,
            show_steps=show_steps,
            step_history_seconds=step_history_seconds,
            mirror=False,
        )
    if show_coach and coach_data is not None:
        result = render_coach_frame_overlay(result, coach_data, frame_index, cfg)
    if pane_label:
        height = result.shape[0]
        origin = (16, height - 18)
        cv2.putText(result, pane_label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.65, (20, 24, 28), 4, cv2.LINE_AA)
        cv2.putText(result, pane_label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.65, (245, 245, 245), 1, cv2.LINE_AA)
    return result


def _analytics_points(
    coach_data: dict[str, Any] | None,
    metric: str,
    *,
    scale: float = 1.0,
    alert_key: str,
) -> list[tuple[float, float, bool]]:
    frames = coach_data.get("frames") if isinstance(coach_data, dict) else None
    if not isinstance(frames, dict):
        return []
    points: list[tuple[float, float, bool]] = []
    for entry in frames.values():
        if not isinstance(entry, dict):
            continue
        value = entry.get(metric)
        timestamp = entry.get("clip_timestamp", entry.get("timestamp"))
        if not (
            isinstance(value, int | float)
            and math.isfinite(value)
            and isinstance(timestamp, int | float)
            and math.isfinite(timestamp)
        ):
            continue
        alerts = entry.get("alerts")
        is_bad = bool(alerts.get(alert_key, False)) if isinstance(alerts, dict) else False
        points.append((float(timestamp), float(value) * scale, is_bad))
    return sorted(points, key=lambda point: point[0])


def _draw_analytics_chart(
    canvas: tk.Canvas,
    *,
    title: str,
    unit: str,
    practice: list[tuple[float, float, bool]],
    reference: list[tuple[float, float, bool]],
    threshold: float,
    zoom: float = 1.0,
    pan: float = 0.0,
) -> None:
    canvas.delete("all")
    width = max(1, canvas.winfo_width())
    height = max(1, canvas.winfo_height())
    canvas.configure(background="#ffffff", highlightthickness=1, highlightbackground="#d0d5d8")
    canvas.create_text(12, 12, anchor="nw", text=title, font=("TkDefaultFont", 11, "bold"), fill="#263238")
    if not practice and not reference:
        canvas.create_text(
            width / 2,
            height / 2,
            text="Coach analysis is not available for either video",
            fill="#59636a",
            width=max(100, width - 40),
        )
        return

    plot_left, plot_top = 54, 38
    plot_right, plot_bottom = max(plot_left + 1, width - 16), max(plot_top + 1, height - 34)
    data_end = max((point[0] for point in (*practice, *reference)), default=1.0)
    data_end = max(data_end, 1e-3)
    view_span = data_end / max(1.0, zoom)
    view_start = max(0.0, data_end - view_span) * min(1.0, max(0.0, pan))
    view_end = min(data_end, view_start + view_span)
    practice = [point for point in practice if view_start <= point[0] <= view_end]
    reference = [point for point in reference if view_start <= point[0] <= view_end]
    if not practice and not reference:
        canvas.create_text(
            width / 2,
            height / 2,
            text="No samples in this time window",
            fill="#59636a",
        )
        return

    values = [point[1] for point in (*practice, *reference)]
    if math.isfinite(threshold):
        values.append(threshold)
    low, high = min(values), max(values)
    spread = high - low
    padding = max(spread * 0.10, 1.0 if spread == 0 else spread * 0.03)
    low -= padding
    high += padding
    visible_duration = max(view_end - view_start, 1e-3)

    def project(timestamp: float, value: float) -> tuple[float, float]:
        x = plot_left + ((timestamp - view_start) / visible_duration) * (plot_right - plot_left)
        y = plot_bottom - ((value - low) / (high - low)) * (plot_bottom - plot_top)
        return x, y

    for index in range(5):
        value = low + (high - low) * index / 4.0
        y = plot_bottom - (plot_bottom - plot_top) * index / 4.0
        canvas.create_line(plot_left, y, plot_right, y, fill="#e6eaed")
        canvas.create_text(plot_left - 6, y, anchor="e", text=f"{value:.1f}", fill="#59636a")
    if math.isfinite(threshold):
        _, threshold_y = project(0.0, threshold)
        canvas.create_line(
            plot_left, threshold_y, plot_right, threshold_y, fill="#d97706", dash=(5, 3)
        )

    for previous, current in pairwise(practice):
        if previous[2] or current[2]:
            x0, _ = project(previous[0], previous[1])
            x1, _ = project(current[0], current[1])
            canvas.create_rectangle(
                x0, plot_top, max(x0 + 1, x1), plot_bottom, fill="#fde2e2", outline=""
            )

    def draw_series(points: list[tuple[float, float, bool]], base_color: str) -> None:
        for previous, current in pairwise(points):
            x0, y0 = project(previous[0], previous[1])
            x1, y1 = project(current[0], current[1])
            color = "#c62828" if base_color == "#18804b" and (previous[2] or current[2]) else base_color
            canvas.create_line(x0, y0, x1, y1, fill=color, width=2)
        for timestamp, value, is_bad in points:
            x, y = project(timestamp, value)
            color = "#c62828" if base_color == "#18804b" and is_bad else base_color
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color)

    draw_series(reference, "#2563eb")
    draw_series(practice, "#18804b")
    canvas.create_text(
        plot_left,
        height - 17,
        anchor="w",
        text=f"{int(view_start // 60)}:{view_start % 60:04.1f}",
        fill="#59636a",
    )
    canvas.create_text(
        plot_right,
        height - 17,
        anchor="e",
        text=f"{int(view_end // 60)}:{view_end % 60:04.1f}",
        fill="#59636a",
    )
    legend_y = 14
    canvas.create_line(max(plot_left + 8, width - 190), legend_y + 3, max(plot_left + 24, width - 174), legend_y + 3, fill="#18804b", width=2)
    canvas.create_text(max(plot_left + 28, width - 168), legend_y + 3, anchor="w", text="Practice", fill="#263238")
    canvas.create_line(max(plot_left + 95, width - 105), legend_y + 3, max(plot_left + 111, width - 89), legend_y + 3, fill="#2563eb", width=2)
    canvas.create_text(max(plot_left + 115, width - 83), legend_y + 3, anchor="w", text="Reference", fill="#263238")
    canvas.create_text(plot_left - 8, plot_top - 8, anchor="e", text=unit, fill="#59636a")


class MarkdownView(ttk.Frame):
    def __init__(self, parent, on_time: Callable[[float], None]):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.on_time = on_time
        self.headings: list[tuple[int, str, str]] = []
        self.query = tk.StringVar()
        toolbar = ttk.Frame(self, padding=4)
        toolbar.grid(row=0, column=0, sticky="ew")
        entry = ttk.Entry(toolbar, textvariable=self.query, width=28)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _: self.search())
        ttk.Button(toolbar, text="Find", command=self.search).pack(side="left", padx=4)
        wrap = tk.BooleanVar(value=True)
        self.wrap = wrap
        ttk.Checkbutton(
            toolbar,
            text="Wrap lines",
            variable=wrap,
            command=lambda: self.text.configure(wrap="word" if wrap.get() else "none"),
        ).pack(side="right")
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew")
        self.fonts = {}
        for name, size, bold in (
            ("body", 11, False),
            ("h1", 19, True),
            ("h2", 14, True),
            ("h3", 12, True),
            ("bold", 11, True),
            ("italic", 11, False),
            ("table", 10, False),
            ("code", 10, False),
        ):
            font = tkfont.nametofont("TkFixedFont" if name == "table" else "TkDefaultFont").copy()
            font.configure(
                size=size,
                weight="bold" if bold else "normal",
                slant="italic" if name == "italic" else "roman",
            )
            self.fonts[name] = font
        self.fonts["code"] = tkfont.nametofont("TkFixedFont").copy()
        self.fonts["code"].configure(size=10)
        self.text = tk.Text(
            body,
            wrap="word",
            state="disabled",
            padx=12,
            pady=10,
            font=self.fonts["body"],
            foreground="#202124",
            background="#ffffff",
            insertbackground="#202124",
            selectbackground="#cfe8ff",
            selectforeground="#111111",
        )
        vertical = ttk.Scrollbar(body, command=self.text.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.text.tag_configure(
            "h1", font=self.fonts["h1"], foreground="#17212b", spacing1=10, spacing3=8
        )
        self.text.tag_configure(
            "h2", font=self.fonts["h2"], foreground="#233746", spacing1=9, spacing3=6
        )
        self.text.tag_configure(
            "h3", font=self.fonts["h3"], foreground="#34495a", spacing1=8, spacing3=4
        )
        self.text.tag_configure("bold", font=self.fonts["bold"], foreground="#202124")
        self.text.tag_configure("italic", font=self.fonts["italic"], foreground="#202124")
        self.text.tag_configure("table", font=self.fonts["table"], foreground="#202124")
        self.text.tag_configure(
            "code", font=self.fonts["code"], foreground="#202124", background="#f0f0f0"
        )
        self.text.tag_configure("strike", overstrike=True)
        self.text.tag_configure("quote", foreground="#46535e", lmargin1=18, lmargin2=18)
        self.text.tag_configure("list", lmargin1=18, lmargin2=34)
        self.text.tag_configure("found", background="#ffe89c", foreground="black")

    def set(self, markdown: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.headings = []
        for tag in self.text.tag_names():
            if tag.startswith(("time-", "link-")):
                self.text.tag_delete(tag)
        lines = markdown.splitlines()
        index = 0
        fenced = False
        while index < len(lines):
            line = lines[index]
            if line.lstrip().startswith("```"):
                fenced = not fenced
                index += 1
                continue
            if fenced:
                self._insert_line(line, "code", literal=True)
            elif line.lstrip().startswith("|"):
                table = []
                while index < len(lines) and lines[index].lstrip().startswith("|"):
                    cells = self._table_cells(lines[index])
                    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                        table.append(cells)
                    index += 1
                self._insert_table(table)
                continue
            else:
                heading = re.match(r"^\s*(#{1,3})\s+(.*)", line)
                if heading:
                    self.headings.append(
                        (len(heading[1]), heading[2], self.text.index("end-1c"))
                    )
                    self._insert_line(heading[2], f"h{len(heading[1])}")
                elif re.match(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$", line):
                    self._insert_line("--------------------------------", "table")
                elif quote := re.match(r"^\s*>\s?(.*)", line):
                    self._insert_line(quote[1], "quote")
                elif bullet := re.match(r"^(\s*)[-*+]\s+(.*)", line):
                    self._insert_line(f"{bullet[1]}- {bullet[2]}", "list")
                elif number := re.match(r"^(\s*\d+\.)\s+(.*)", line):
                    self._insert_line(f"{number[1]} {number[2]}", "list")
                else:
                    self._insert_line(line)
            index += 1
        self.text.configure(state="disabled")

    @staticmethod
    def _table_cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    def _insert_table(self, rows: list[list[str]]):
        if not rows:
            return
        columns = max(map(len, rows))
        widths = [
            max((len(row[column]) if column < len(row) else 0) for row in rows)
            for column in range(columns)
        ]
        for row_index, row in enumerate(rows):
            cells = [
                (row[column] if column < len(row) else "").ljust(widths[column])
                for column in range(columns)
            ]
            self._insert_line(" | ".join(cells).rstrip(), "table")
            if row_index == 0 and len(rows) > 1:
                rule = "-+-".join("-" * width for width in widths)
                self._insert_line(rule, "table")

    def _insert_line(self, line: str, block_tag: str = "", *, literal: bool = False):
        if literal:
            self.text.insert("end", line + "\n", (block_tag,) if block_tag else ())
            return
        start = self.text.index("end-1c")
        displayed = []
        pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)|\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`|\*(.+?)\*|_(.+?)_|~~(.+?)~~")
        cursor = 0

        def insert(value: str, *tags: str):
            active = ((block_tag,) if block_tag else ()) + tags
            self.text.insert("end", value, active)
            displayed.append(value)

        for match in pattern.finditer(line):
            insert(line[cursor : match.start()])
            if match[1] is not None:
                label, target = match[1], match[2]
                link_tag = f"link-{uuid4().hex}"
                self.text.tag_configure(link_tag, foreground="#176caa", underline=True)
                scheme = urlsplit(target).scheme.lower()
                if scheme in ("http", "https", "file"):
                    self.text.tag_bind(link_tag, "<Button-1>", partial(self._open_link, target))
                insert(label, link_tag)
            elif match[3] is not None or match[4] is not None:
                insert(match[3] or match[4], "bold")
            elif match[5] is not None:
                insert(match[5], "code")
            elif match[6] is not None or match[7] is not None:
                insert(match[6] or match[7], "italic")
            else:
                insert(match[8], "strike")
            cursor = match.end()
        insert(line[cursor:])
        self.text.insert("end", "\n", (block_tag,) if block_tag else ())
        plain = "".join(displayed)
        for match in re.finditer(r"(?<![\w:+-])\d+:\d{2}(?::\d{2})?(?:\.\d+)?\b", plain):
            key = f"time-{start}-{match.start()}"
            self.text.tag_add(key, f"{start}+{match.start()}c", f"{start}+{match.end()}c")
            self.text.tag_configure(key, foreground="#176caa", underline=True)
            self.text.tag_bind(key, "<Button-1>", partial(self._time, parse_time(match[0])))

    @staticmethod
    def _open_link(target: str, _event=None):
        webbrowser.open(target)

    def _time(self, seconds, _event=None):
        self.on_time(seconds)

    def append(self, text: str):
        self.text.configure(state="normal")
        self.text.insert("end", text)
        self.text.see("end")
        self.text.configure(state="disabled")

    def search(self):
        self.text.tag_remove("found", "1.0", "end")
        query = self.query.get()
        if not query:
            return
        at = "1.0"
        first = None
        while found := self.text.search(query, at, stopindex="end", nocase=True):
            first = first or found
            at = f"{found}+{len(query)}c"
            self.text.tag_add("found", found, at)
        if first:
            self.text.see(first)


class ReviewPanel(ttk.Frame):
    def __init__(self, parent, settings: dict[str, tk.Variable], config: Callable[[], Config]):
        super().__init__(parent, padding=8)
        self.config_provider, self.settings = config, settings
        self.report: ReportRecord | None = None
        self.archive: PracticeArchive | None = None
        self.segment_paths: dict[str, Path] = {}
        self.segment_selection = tk.StringVar()
        self.current_segment_index: int | None = None
        self.opened_video_path: Path | None = None
        self.movement_data: dict[str, Any] | None = None
        self.session_movement_results: dict[tuple[str, str, int], tuple[dict[str, Any], bool]] = {}
        self.movement_progress_by_key: dict[tuple[str, str, int], int] = {}
        self.movement_state_by_key: dict[tuple[str, str, int], str] = {}
        self.show_pivots_var = tk.BooleanVar(value=False)
        self.show_steps_var = tk.BooleanVar(value=False)
        self.floor_only_var = tk.BooleanVar(value=False)
        self.show_coach_var = tk.BooleanVar(value=False)
        self.coach_data: dict[str, Any] | None = None
        self.practice_analytics_data: dict[str, Any] | None = None
        self.movement_status = tk.StringVar(value="Not analysed")
        self.closed = False
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.movement_coordinator = MovementAnalysisCoordinator(
            lambda payload: self.events.put((payload[1], payload[0], payload[2:]))
        )
        self.import_worker = ReferenceImportWorker(
            lambda payload: self.events.put((payload[1], payload[0], payload[2:]))
        )
        self.cut_job = ""
        self.cut_dialog: tk.Toplevel | None = None
        self.cut_status_var: tk.StringVar | None = None
        self.cut_button: ttk.Button | None = None
        self.overlay_export_job = ""
        self.overlay_export_button: ttk.Button | None = None
        self.import_estimator_factory: Callable[[], Any] | None = None
        self.import_youtube_downloader: Callable[[str, Path], Path] | None = None
        self._downloaded_youtube_file: tuple[Path, str] | None = None
        self.import_probe: VideoProbeInfo | None = None
        self.import_video_var = tk.StringVar()
        self.import_title_var = tk.StringVar()
        self.import_youtube_var = tk.StringVar()
        self.import_in_var = tk.StringVar(value="0.0")
        self.import_out_var = tk.StringVar(value="")
        self.import_mode_var = tk.StringVar(value="movement")
        self.import_reference_var = tk.StringVar(value="latest")
        self.import_cal_in_var = tk.StringVar(value="")
        self.import_cal_out_var = tk.StringVar(value="")
        self.import_soundtrack_var = tk.BooleanVar(value=True)
        self.import_confirm_same_edit_var = tk.BooleanVar(value=False)
        self.import_align_local_var = tk.StringVar(value="0.0")
        self.import_align_youtube_var = tk.StringVar(value="0.0")
        self.import_file_info = tk.StringVar(value="No local file selected")
        self.import_range_status = tk.StringVar(value="Max clip range: 120s")
        self.import_readiness_status = tk.StringVar(
            value="Movement review (no calibration required)"
        )
        self.import_status = tk.StringVar(value="Choose a local video file to analyse")
        self.import_panel_visible = False
        self.last_opened_youtube_url: str = ""
        self.compare_target_var = tk.StringVar()
        self.show_reference_var = tk.BooleanVar(value=False)
        self.reference_paths: dict[str, Path] = {}
        self.reference_record: ReportRecord | None = None
        self.reference_archive: PracticeArchive | None = None
        self.reference_movement_data: dict[str, Any] | None = None
        self.reference_coach_data: dict[str, Any] | None = None
        self.analytics_window: tk.Toplevel | None = None
        self.analytics_canvases: dict[str, tk.Canvas] = {}
        self.analytics_status = tk.StringVar(value="")
        self.analytics_job = ""
        self.analytics_note = ""
        self.analytics_analyse_button: ttk.Button | None = None
        self.analytics_metric_var = tk.StringVar(value="Head alignment")
        self.analytics_zoom_var = tk.DoubleVar(value=1.0)
        self.analytics_pan_var = tk.DoubleVar(value=0.0)
        self.replay_controls_visible = False
        self.anchor_left_var = tk.StringVar(value="0.0")
        self.anchor_right_var = tk.StringVar(value="0.0")
        self.comparison_summary_var = tk.StringVar(value="")
        self.last_comparison_result: ClipComparisonResult | None = None
        self.job = ""
        self.models_job = ""
        self.cancel = threading.Event()
        self.report_paths: dict[str, Path] = {}
        self.analyses: dict[str, dict] = {}
        self.analysis_data: dict | None = None
        self.analysis_stream = ""
        self.selection = tk.StringVar()
        self.video_selection = tk.StringVar()
        self.video_paths: dict[str, Path] = {}
        self.video_stream_keys: dict[str, str] = {}
        self.saved_selection = tk.StringVar()
        self.status = tk.StringVar(value="Choose a saved report to review your practice.")
        self.analysis_status = tk.StringVar(
            value="Analysis runs only when you click Analyze report."
        )
        self.analysis_meta = tk.StringVar()
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.report_page = ttk.Frame(self.tabs)
        self.tabs.add(self.report_page, text="Practice report")
        report_page = self.report_page
        top = ttk.Frame(report_page, padding=(6, 6, 6, 2))
        top.pack(fill="x")
        self.report_combo = ttk.Combobox(top, textvariable=self.selection, state="readonly")
        self.report_combo.pack(side="left", fill="x", expand=True)
        self.report_combo.bind(
            "<<ComboboxSelected>>",
            lambda _: self.guard(lambda: self.load_report(self.report_paths[self.selection.get()])),
        )
        ttk.Button(top, text="Refresh", command=lambda: self.guard(self.refresh)).pack(
            side="left", padx=5
        )
        ttk.Button(top, text="Open practice...", command=lambda: self.guard(self.open_report)).pack(
            side="left"
        )
        segments = ttk.Frame(report_page, padding=(6, 2))
        segments.pack(fill="x")
        ttk.Label(segments, text="Recording").pack(side="left", padx=(0, 8))
        self.segment_combo = ttk.Combobox(
            segments, textvariable=self.segment_selection, state="disabled",
        )
        self.segment_combo.pack(side="left", fill="x", expand=True)
        self.segment_combo.bind(
            "<<ComboboxSelected>>",
            lambda _: self.guard(lambda: self.load_report(
                self.segment_paths[self.segment_selection.get()], archived=True,
            )),
        )
        ttk.Label(report_page, textvariable=self.status, wraplength=920).pack(
            anchor="w", padx=6, pady=(2, 6)
        )
        self.report_view = MarkdownView(report_page, self.seek_report_time)
        self.report_view.pack(fill="both", expand=True)
        videos = ttk.Frame(self.tabs)
        self.tabs.add(videos, text="Video replay")
        self.video_page = videos
        chooser = ttk.Frame(videos, padding=6)
        chooser.pack(fill="x")
        self.video_combo = ttk.Combobox(
            chooser, textvariable=self.video_selection, state="readonly", width=20
        )
        ttk.Label(chooser, text="Practice video:").pack(side="left", padx=(0, 4))
        self.video_combo.pack(side="left")
        self.video_combo.bind("<<ComboboxSelected>>", lambda _: self.guard(self.switch_video))
        ttk.Button(chooser, text="Open video...", command=lambda: self.guard(self.open_video)).pack(
            side="left", padx=6
        )
        self.replay_controls_button = ttk.Button(
            chooser,
            text="Show controls",
            command=lambda: self.toggle_replay_controls(),
        )
        self.replay_controls_button.pack(side="left", padx=4)
        ttk.Button(
            chooser,
            text="Analytics",
            command=lambda: self.guard(self.open_posture_analytics),
        ).pack(side="left", padx=4)
        ttk.Label(videos, textvariable=self.status, wraplength=1100).pack(
            anchor="w", padx=6, pady=(0, 2)
        )
        self.replay_options = ttk.Frame(videos)
        replay_actions = ttk.Frame(self.replay_options, padding=(6, 0))
        replay_actions.pack(fill="x")
        ttk.Button(
            replay_actions,
            text="Cut video…",
            command=lambda: self.guard(self.cut_video_dialog),
        ).pack(side="left", padx=4)
        self.overlay_export_button = ttk.Button(
            replay_actions,
            text="Save overlay…",
            command=lambda: self.guard(self.save_overlay_video_dialog),
        )
        self.overlay_export_button.pack(side="left", padx=4)
        self.import_reference_button = ttk.Button(
            replay_actions,
            text="Import reference video…",
            command=lambda: self.guard(self.toggle_import_panel),
        )
        self.import_reference_button.pack(side="left", padx=(0, 4))
        self.relink_video_button = ttk.Button(
            replay_actions,
            text="Relink video…",
            state="disabled",
            command=lambda: self.guard(self.relink_current_video_dialog),
        )
        self.relink_video_button.pack(side="left", padx=4)
        self.open_youtube_button = ttk.Button(
            replay_actions,
            text="Open on YouTube",
            state="disabled",
            command=lambda: self.guard(self.open_current_youtube),
        )
        self.open_youtube_button.pack(side="left", padx=4)
        self.open_moment_youtube_button = ttk.Button(
            replay_actions,
            text="Open this moment on YouTube",
            state="disabled",
            command=lambda: self.guard(self.open_current_moment_on_youtube),
        )
        self.open_moment_youtube_button.pack(side="left", padx=4)
        self.reference_bar = ttk.Frame(self.replay_options, padding=(6, 2))
        self.reference_bar.pack(fill="x")
        ttk.Label(self.reference_bar, text="Reference video:").pack(side="left", padx=(0, 4))
        self.reference_combo = ttk.Combobox(
            self.reference_bar,
            textvariable=self.compare_target_var,
            state="readonly",
            width=34,
        )
        self.reference_combo.pack(side="left", padx=(0, 8))
        self.reference_combo.bind(
            "<<ComboboxSelected>>", lambda _: self.guard(self.on_reference_selection)
        )
        self.show_reference_check = ttk.Checkbutton(
            self.reference_bar,
            text="Show reference",
            variable=self.show_reference_var,
            command=lambda: self.guard(self.on_reference_toggle),
        )
        self.show_reference_check.pack(side="left")
        self._build_import_panel(self.replay_options)
        movement_bar = ttk.Frame(self.replay_options, padding=(6, 2))
        movement_bar.pack(fill="x")
        ttk.Label(movement_bar, text="Movement analysis:").pack(side="left", padx=(0, 6))
        self.movement_button = ttk.Button(
            movement_bar,
            text="Analyse movement",
            state="disabled",
            command=lambda: self.guard(self.request_movement_analysis),
        )
        self.movement_button.pack(side="left")
        self.movement_cancel_button = ttk.Button(
            movement_bar,
            text="Cancel",
            state="disabled",
            command=lambda: self.guard(self.cancel_movement_analysis),
        )
        self.movement_cancel_button.pack(side="left", padx=(4, 8))
        self.movement_status_label = ttk.Label(movement_bar, textvariable=self.movement_status)
        self.movement_status_label.pack(side="left", fill="x", expand=True)
        overlay_controls = ttk.Frame(self.replay_options, padding=(6, 0))
        overlay_controls.pack(fill="x", pady=(0, 3))
        self.floor_only_check = ttk.Checkbutton(
            overlay_controls,
            text="Hide video (gray floor)",
            variable=self.floor_only_var,
            command=lambda: self.guard(self.on_floor_only_toggle),
        )
        self.floor_only_check.pack(side="left", padx=(0, 10))
        self.show_coach_check = ttk.Checkbutton(
            overlay_controls,
            text="Coach lines",
            variable=self.show_coach_var,
            command=lambda: self.guard(self.on_coach_toggle),
        )
        self.show_coach_check.pack(side="left", padx=(0, 6))
        self.show_pivots_check = ttk.Checkbutton(
            overlay_controls,
            text="Show pivots",
            variable=self.show_pivots_var,
            command=lambda: self.guard(self.on_movement_toggle),
        )
        self.show_pivots_check.pack(side="left", padx=(0, 6))
        self.show_steps_check = ttk.Checkbutton(
            overlay_controls,
            text="Show recent steps",
            variable=self.show_steps_var,
            command=lambda: self.guard(self.on_movement_toggle),
        )
        self.show_steps_check.pack(side="left", padx=(0, 10))
        body = ttk.Panedwindow(videos, orient="horizontal")
        body.pack(fill="both", expand=True)
        self.replay_body = body
        self.replay_options.pack_forget()
        self.player = ReplayPlayer(body, settings["replay_audio"])
        self.player.frame_transform = self._transform_replay_frame
        body.add(self.player, weight=5)
        self.notes_sidebar = NotesSidebar(body)
        body.add(self.notes_sidebar, weight=0)
        self.video_notes = self.notes_sidebar.text
        self.frame_notes: list[Any] = []
        self.player.on_frame = self.show_video_notes
        if "replay_step_history_seconds" in self.settings:
            self.settings["replay_step_history_seconds"].trace_add(
                "write", lambda *_: self._on_step_history_changed()
            )
        self._analysis_page()
        self.timer = self.after(50, self.pump)

    def _analysis_page(self):
        page = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(page, text="Ollama analysis")
        self.analysis_page = page
        activity = ttk.Frame(page, padding=(8, 6))
        activity.pack(fill="x", pady=(0, 6))
        activity.columnconfigure(0, weight=1)
        self.analysis_status_label = ttk.Label(
            activity, textvariable=self.analysis_status, font=("Helvetica", 11, "bold")
        )
        self.analysis_status_label.grid(row=0, column=0, sticky="w")
        self.analysis_progress = ttk.Progressbar(
            activity, mode="indeterminate", length=150, maximum=100
        )
        self.analysis_progress.grid(row=0, column=1, sticky="e", padx=(12, 0))
        form = ttk.Frame(page)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Ollama server").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(form, textvariable=self.settings["ollama_base_url"]).grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Label(form, text="Model").grid(row=1, column=0, sticky="w")
        self.model_combo = ttk.Combobox(form, textvariable=self.settings["ollama_model"])
        self.model_combo.grid(row=1, column=1, sticky="ew", pady=3)
        self.models_button = ttk.Button(
            form, text="List installed models", command=lambda: self.guard(self.list_models)
        )
        self.models_button.grid(row=1, column=2, padx=6)
        ttk.Label(form, text="Analysis focus").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.settings["ollama_instruction"]).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=3
        )
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=6)
        self.analyze_button = ttk.Button(
            actions, text="Analyze report", command=self.request_analysis
        )
        self.analyze_button.pack(side="left")
        self.cancel_button = ttk.Button(
            actions, text="Cancel", state="disabled", command=self.cancel_analysis
        )
        self.cancel_button.pack(side="left", padx=5)
        ttk.Button(
            actions, text="Export analysis...", command=lambda: self.guard(self.export_analysis)
        ).pack(side="right")
        ttk.Label(
            page,
            text="The selected report text is sent to the configured server. AI interpretation is separate from measured results.",
            wraplength=900,
        ).pack(anchor="w")
        saved = ttk.Frame(page)
        saved.pack(fill="x")
        ttk.Label(saved, text="Saved analyses").pack(side="left", padx=(0, 8))
        self.saved_combo = ttk.Combobox(saved, textvariable=self.saved_selection, state="readonly")
        self.saved_combo.pack(side="left", fill="x", expand=True)
        self.saved_combo.bind("<<ComboboxSelected>>", lambda _: self.guard(self.show_saved))
        ttk.Label(page, textvariable=self.analysis_meta, wraplength=900).pack(anchor="w", pady=5)
        self.analysis_view = MarkdownView(page, self.seek_report_time)
        self.analysis_view.pack(fill="both", expand=True)

    def guard(self, action):
        try:
            action()
        except (ValueError, OSError, RuntimeError, TypeError, KeyError) as error:
            self.status.set(str(error))

    def toggle_replay_controls(self) -> None:
        self.replay_controls_visible = not self.replay_controls_visible
        if self.replay_controls_visible:
            self.replay_options.pack(fill="x", before=self.replay_body)
            self.replay_controls_button.configure(text="Hide controls")
        else:
            self.replay_options.pack_forget()
            self.replay_controls_button.configure(text="Show controls")

    def refresh(self):
        folder = report_directory(self.config_provider().data_dir)
        practice_folder = practice_directory(self.config_provider().data_dir)
        calibration_folder = calibration_directory(self.config_provider().data_dir)
        paths = sorted(
            [*practice_folder.glob("*.tp"), *calibration_folder.glob("*.tpc"), *folder.glob("report_*.md")],
            key=lambda p: p.stat().st_mtime_ns, reverse=True,
        )
        if not paths and (folder / "latest_report.md").exists():
            paths = [folder / "latest_report.md"]
        self.report_paths = {str(path.resolve()): path.resolve() for path in paths}
        self.report_combo.configure(values=list(self.report_paths))
        self._refresh_reference_choices()
        if self.report is None and paths:
            self.load_report(paths[0])

    def open_report(self):
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("Tango practice / calibration", "*.tp *.tpc"), ("Legacy report", "*.md")],
        )
        if path:
            self.load_report(Path(path))

    def add_report(self, path: Path):
        if self.job:
            self.report_paths[str(path.resolve())] = path.resolve()
            self.report_combo.configure(values=list(self.report_paths))
            self._refresh_reference_choices()
            self.status.set(
                "New practice report ready in the selector. The current report analysis is still running."
            )
        else:
            self.load_report(path)

    def _refresh_reference_choices(self) -> None:
        current_path = None
        if self.report is not None:
            current_path = self.report.archive_path or self.report.path
            current_path = current_path.expanduser().resolve()

        choices: dict[str, Path] = {}
        for path in self.report_paths.values():
            resolved = path.expanduser().resolve()
            if not resolved.is_file():
                continue
            if current_path is not None and resolved == current_path:
                continue
            base = path.stem.replace("_", " ").strip() or path.name
            label = base
            suffix = 2
            while label in choices:
                label = f"{base} ({suffix})"
                suffix += 1
            choices[label] = resolved

        self.reference_paths = choices
        self.reference_combo.configure(values=list(choices))
        if self.compare_target_var.get() not in choices:
            self.compare_target_var.set("")
            self.show_reference_var.set(False)
            self.player.compare([], refresh=False)
            self.reference_record = None
            self.reference_movement_data = None
            self.reference_coach_data = None
            if self.reference_archive is not None:
                self.reference_archive.close()
                self.reference_archive = None
        self.show_reference_check.configure(state="normal" if choices else "disabled")

    def load_report(self, path: Path, archived: bool = False):
        if path.suffix.lower() in (".tp", ".tpc"):
            archive = PracticeArchive(path)
            if not archive.manifest["segments"]:
                archive.close()
                raise ValueError("This practice has no saved recordings")
            self.cancel_analysis()
            self.player.unload()
            if self.archive is not None:
                self.archive.close()
            self.archive = archive
            self.segment_paths = {
                ("Calibration" if archive.manifest.get("kind") == "calibration"
                 else f"Recording {segment['index']:02d}"): archive.report(segment["index"])
                for segment in archive.manifest["segments"]
            }
            self.segment_combo.configure(values=list(self.segment_paths), state="readonly")
            self.segment_selection.set(list(self.segment_paths)[-1])
            self.load_report(self.segment_paths[self.segment_selection.get()], archived=True)
            return
        record = read_report(path)
        self.cancel_analysis()
        self.player.unload()
        self._clear_standalone_movement()
        if not archived and self.archive is not None:
            self.archive.close()
            self.archive = None
            self.segment_paths = {}
            self.segment_selection.set("")
            self.segment_combo.configure(values=[], state="disabled")
        if archived and self.archive is not None:
            record.archive_path = self.archive.path
            selected_segment = self.archive.manifest["segments"][
                list(self.segment_paths).index(self.segment_selection.get())
            ]
            self.current_segment_index = int(selected_segment["index"])
            record.videos = {
                label: self.archive.folder / selected_segment[key]
                for label, key in (("Raw video", "video"), ("Overlay", "overlay"))
                if selected_segment[key] is not None
            }
        else:
            self.current_segment_index = None
        overlay = record.videos.get("Overlay")
        self.frame_notes = []
        self.practice_analytics_data = None
        if overlay:
            skeleton = overlay.with_name(overlay.stem + "_lines.mp4")
            if skeleton.is_file():
                record.videos["Full lines + messages"] = skeleton
            notes_path = overlay.with_suffix(".notes.jsonl")
            if notes_path.is_file():
                try:
                    self.frame_notes = [json.loads(line) for line in notes_path.read_text().splitlines()]
                except (ValueError, OSError):
                    self.frame_notes = []
        practice_frames: dict[str, dict[str, Any]] = {}
        cfg = self.config_provider()
        for index, note in enumerate(self.frame_notes):
            if not isinstance(note, dict):
                continue
            row = dict(note)
            row.setdefault("clip_timestamp", row.get("timestamp", float(index)))
            alerts = row.get("alerts")
            if not isinstance(alerts, dict):
                hip_offset = row.get("axis_curve_ratio")
                row["alerts"] = {
                    "head": row.get("head_angle") is not None
                    and float(row["head_angle"]) < cfg.head_down_angle,
                    "shoulders": row.get("shoulder_elev_pct") is not None
                    and float(row["shoulder_elev_pct"]) >= cfg.shoulder_raise_threshold,
                    "knees": row.get("knee_bend_deg") is not None
                    and float(row["knee_bend_deg"]) < cfg.closed_embrace_min_knee_bend,
                    "axis": hip_offset is not None
                    and float(hip_offset) > cfg.axis_curve_entry,
                }
            practice_frames[str(index)] = row
        if practice_frames:
            self.practice_analytics_data = {"version": 1, "frames": practice_frames}
        self.video_paths = {}
        self.video_stream_keys = {}
        original_key = next(
            (
                key
                for key in ("Raw video", "Source video")
                if key in record.videos and record.videos[key].is_file()
            ),
            None,
        )
        if original_key is not None:
            self.video_paths["Practice video"] = record.videos[original_key]
            self.video_stream_keys["Practice video"] = original_key
        practice_path = self.video_paths.get("Practice video")
        for key, label, dynamic_key in (
            ("Overlay", "Overlay video", "Dynamic overlay"),
            ("Full lines + messages", "Overlay with lines", "Dynamic lines-only"),
        ):
            overlay_path = record.videos.get(key)
            if overlay_path is not None and overlay_path.is_file():
                self.video_paths[label] = overlay_path
                self.video_stream_keys[label] = key
            elif practice_path is not None:
                self.video_paths[label] = practice_path
                self.video_stream_keys[label] = dynamic_key
        self.report = record
        self.coach_data = load_report_coach_data(record)
        self._refresh_posture_analytics()
        if record.is_imported_reference:
            has_coach = self.coach_data is not None
            self.show_coach_var.set(has_coach)
            self.show_pivots_var.set(True)
            self.show_steps_var.set(True)
        selected_path = record.archive_path or record.path
        self.selection.set(str(selected_path))
        self.report_paths[str(selected_path)] = selected_path
        self.report_combo.configure(values=list(self.report_paths))
        self._refresh_reference_choices()
        self.report_view.set(record.markdown)
        self.player.unload()
        self.video_combo.configure(values=list(self.video_paths))
        self.video_selection.set("")
        self.status.set(str(selected_path) + ("\n" + record.warning if record.warning else ""))
        self._sync_import_and_youtube_controls()
        for label in ("Practice video",):
            if label in self.video_paths and self.video_paths[label].is_file():
                self.video_selection.set(label)
                self.guard(self.switch_video)
                break
        else:
            if record.is_imported_reference and record.videos:
                missing_name = next(iter(record.videos.values())).name
                self.status.set(
                    f"Source video '{missing_name}' was not found at its saved path. Click 'Relink video…' to locate it."
                )
            self._refresh_movement_state()
        self.analysis_data = None
        self.analysis_meta.set("")
        self.analysis_view.set("")
        self.analysis_status.set("Analyze this report, or select one of its saved analyses.")
        self.guard(self.refresh_analyses)
        self.tabs.select(self.report_page)

    def switch_video(self):
        if not self.report:
            raise ValueError("Open a report first, or use Open video")
        if self.opened_video_path is not None and self.video_selection.get() != "Opened video":
            self._clear_standalone_movement()
        seconds = self.player.position.get()
        clip_range = self.report.clip_range if self.report.is_imported_reference else None
        selected_video = self.video_selection.get()
        video_path = self.video_paths.get(selected_video)
        if video_path is None:
            video_path = self.report.videos[selected_video]
        self.player.load(
            video_path,
            seconds,
            clip_range=clip_range,
        )
        self.update_comparison()
        self._refresh_movement_state(redraw=True)
        self._sync_import_and_youtube_controls()

    def update_comparison(self):
        if self.show_reference_var.get():
            self.show_reference_video(show_pane=True)
        else:
            self.player.compare([], refresh=False)
            if self.reference_archive is not None:
                self.reference_archive.close()
                self.reference_archive = None
        self._refresh_posture_analytics()

    def on_reference_selection(self) -> None:
        self.show_reference_video(show_pane=self.show_reference_var.get())
        self._refresh_posture_analytics()

    def on_reference_toggle(self) -> None:
        self.update_comparison()

    def open_posture_analytics(self) -> None:
        if self.analytics_window is not None and self.analytics_window.winfo_exists():
            self.analytics_window.lift()
            self._refresh_posture_analytics()
            return

        window = tk.Toplevel(self)
        window.title("Practice and reference analytics")
        window.geometry("1100x720")
        window.minsize(760, 520)
        window.transient(self.winfo_toplevel())
        window.columnconfigure(0, weight=1)
        window.rowconfigure(2, weight=1)
        self.analytics_window = window
        ttk.Label(
            window,
            textvariable=self.analytics_status,
            wraplength=1040,
            padding=(10, 8),
        ).grid(row=0, column=0, sticky="ew")
        controls = ttk.Frame(window, padding=(10, 0, 10, 6))
        controls.grid(row=1, column=0, sticky="ew")
        self.analytics_analyse_button = ttk.Button(
            controls,
            text="Analyze Practice",
            command=lambda: self.guard(self.analyse_practice_for_analytics),
        )
        self.analytics_analyse_button.pack(side="left", padx=(0, 8))
        ttk.Label(controls, text="Copy:").pack(side="left", padx=(0, 4))
        metric_combo = ttk.Combobox(
            controls,
            textvariable=self.analytics_metric_var,
            values=("Head alignment", "Shoulder height", "Knee bend", "Hip alignment / offset"),
            state="readonly",
            width=24,
        )
        metric_combo.pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Copy chart image", command=self.copy_analytics_chart).pack(
            side="left", padx=(0, 14)
        )
        ttk.Label(controls, text="Zoom").pack(side="left", padx=(0, 4))
        ttk.Scale(
            controls,
            from_=1.0,
            to=8.0,
            variable=self.analytics_zoom_var,
            command=lambda _value: self._refresh_posture_analytics(),
            length=140,
        ).pack(side="left", padx=(0, 14))
        ttk.Label(controls, text="Pan").pack(side="left", padx=(0, 4))
        ttk.Scale(
            controls,
            from_=0.0,
            to=1.0,
            variable=self.analytics_pan_var,
            command=lambda _value: self._refresh_posture_analytics(),
            length=160,
        ).pack(side="left")
        grid = ttk.Frame(window, padding=6)
        grid.grid(row=2, column=0, sticky="nsew")
        for column in range(2):
            grid.columnconfigure(column, weight=1, uniform="metric")
        for row in range(2):
            grid.rowconfigure(row, weight=1, uniform="metric")

        for index, key in enumerate(("head", "shoulders", "knees", "hips")):
            canvas = tk.Canvas(grid, height=280, highlightthickness=0)
            canvas.grid(row=index // 2, column=index % 2, sticky="nsew", padx=4, pady=4)

            def redraw_chart(_event: tk.Event, metric_key: str = key) -> None:
                self._draw_analytics_metric(metric_key)

            canvas.bind("<Configure>", redraw_chart)
            self.analytics_canvases[key] = canvas
        window.protocol("WM_DELETE_WINDOW", self.close_posture_analytics)
        window.update_idletasks()
        self._refresh_posture_analytics()

    def close_posture_analytics(self) -> None:
        if self.analytics_window is not None:
            self.analytics_window.destroy()
        self.analytics_window = None
        self.analytics_canvases = {}

    def copy_analytics_chart(self) -> None:
        labels = {
            "Head alignment": "head",
            "Shoulder height": "shoulders",
            "Knee bend": "knees",
            "Hip alignment / offset": "hips",
        }
        key = labels.get(self.analytics_metric_var.get())
        canvas = self.analytics_canvases.get(key or "")
        if canvas is None or not canvas.winfo_ismapped():
            raise ValueError("Open Analytics and select a chart to copy")
        executable = shutil.which("screencapture") or "/usr/sbin/screencapture"
        if not Path(executable).is_file():
            raise ValueError("Chart image copying is available on macOS only")
        canvas.update_idletasks()
        region = (
            canvas.winfo_rootx(),
            canvas.winfo_rooty(),
            canvas.winfo_width(),
            canvas.winfo_height(),
        )
        result = subprocess.run(
            [executable, "-c", "-R", ",".join(str(value) for value in region)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "Could not copy chart image to the clipboard")
        self.analytics_status.set(f"Copied {self.analytics_metric_var.get()} chart image to clipboard")

    def _refresh_posture_analytics(self) -> None:
        if self.analytics_window is None or not self.analytics_window.winfo_exists():
            return
        practice_data = self.practice_analytics_data or self.coach_data
        practice_count = len((practice_data or {}).get("frames", {}))
        reference_count = len((self.reference_coach_data or {}).get("frames", {}))
        status = (
            f"Practice: {practice_count} analyzed frames. Reference: {reference_count} analyzed frames. "
            "Red bands mark Practice alert intervals."
        )
        if not practice_count or not reference_count:
            missing = []
            if not practice_count:
                missing.append("Practice coach data")
            if not reference_count:
                missing.append("Reference coach data")
            status += f" Missing {', '.join(missing)}; analyze both videos as clips to compare them."
        self.analytics_status.set(status)
        for key in self.analytics_canvases:
            self._draw_analytics_metric(key)

    def _draw_analytics_metric(self, key: str) -> None:
        canvas = self.analytics_canvases.get(key)
        if canvas is None:
            return
        cfg = self.config_provider()
        charts = {
            "head": ("Head alignment", "head_angle", "degrees", cfg.head_down_angle, "head", 1.0),
            "shoulders": (
                "Shoulder height",
                "shoulder_elev_pct",
                "% raised",
                cfg.shoulder_raise_threshold,
                "shoulders",
                1.0,
            ),
            "knees": (
                "Knee bend",
                "knee_bend_deg",
                "degrees",
                cfg.closed_embrace_min_knee_bend,
                "knees",
                1.0,
            ),
            "hips": (
                "Hip alignment / offset",
                "axis_curve_smoothed",
                "% offset",
                cfg.axis_curve_entry * 100.0,
                "axis",
                100.0,
            ),
        }
        title, metric, unit, threshold, alert_key, scale = charts[key]
        _draw_analytics_chart(
            canvas,
            title=title,
            unit=unit,
            practice=_analytics_points(
                self.practice_analytics_data or self.coach_data,
                metric,
                scale=scale,
                alert_key=alert_key,
            ),
            reference=_analytics_points(
                self.reference_coach_data, metric, scale=scale, alert_key=alert_key
            ),
            threshold=threshold,
            zoom=float(self.analytics_zoom_var.get()),
            pan=float(self.analytics_pan_var.get()),
        )

    def analyse_practice_for_analytics(self) -> None:
        if self.analytics_job:
            return
        reader = self.player.reader
        if reader is None:
            raise ValueError("Open a Practice video first")
        source = self.video_paths.get("Practice video", reader.path)
        if self.player.clip_range is not None:
            selected_in, selected_out = self.player.clip_range
        else:
            selected_in = 0.0
            selected_out = min(reader.duration, MAX_IMPORT_RANGE_SECONDS)
        if selected_out - selected_in > MAX_IMPORT_RANGE_SECONDS:
            selected_out = selected_in + MAX_IMPORT_RANGE_SECONDS
        if selected_out <= selected_in:
            raise ValueError("The Practice video has no analyzable duration")

        from .reference_import import analyse_imported_clip
        from .storage import Storage

        if self.analytics_analyse_button is not None:
            self.analytics_analyse_button.configure(state="disabled", text="Analyzing Practice…")
        job_id = uuid4().hex
        self.analytics_job = job_id
        self.analytics_note = ""
        self.analytics_status.set(
            f"Analyzing Practice from {time_label(selected_in)} to {time_label(selected_out)}…"
        )
        cfg = self.config_provider()

        def work() -> None:
            try:
                with TemporaryDirectory(prefix="tango-practice-analytics-") as temp_dir:
                    temp_path = Path(temp_dir)
                    storage = Storage(temp_path / "analytics.sqlite3")
                    try:
                        result = analyse_imported_clip(
                            video_path=source,
                            cfg=cfg,
                            report_dir=temp_path / "reports",
                            storage=storage,
                            selected_in=selected_in,
                            selected_out=selected_out,
                            analysis_mode="movement",
                            analyse_soundtrack=False,
                            estimator_factory=self.import_estimator_factory,
                            progress_callback=lambda current, total, message: self.events.put(
                                (job_id, "analytics_progress", (current, total, message))
                            ),
                        )
                    finally:
                        storage.close()
                    self.events.put(
                        (
                            job_id,
                            "analytics_done",
                            (result.coach_data, result.movement_data, selected_in, selected_out),
                        )
                    )
            except Exception as error:  # noqa: BLE001 - report background analytics errors to UI
                self.events.put((job_id, "analytics_error", (str(error),)))

        threading.Thread(target=work, name="practice-analytics", daemon=True).start()

    def show_reference_video(self, *, show_pane: bool = True) -> None:
        if show_pane and self.player.reader is None:
            raise ValueError("Open a Practice video first")
        target = self.reference_paths.get(self.compare_target_var.get())
        if target is None:
            raise ValueError("Choose a Reference video first")
        if not target.is_file():
            self._refresh_reference_choices()
            raise ValueError("That Reference file is missing. Refresh reports and choose another Reference.")

        new_archive: PracticeArchive | None = None
        if target.suffix.lower() in (".tp", ".tpc"):
            new_archive = PracticeArchive(target)
            if not new_archive.manifest["segments"]:
                new_archive.close()
                raise ValueError("The selected Reference has no video")
            segment = new_archive.manifest["segments"][-1]
            reference = read_report(new_archive.report(int(segment["index"])))
            reference.archive_path = target
            reference.videos = {
                label: new_archive.folder / segment[key]
                for label, key in (("Raw video", "video"), ("Overlay", "overlay"))
                if segment[key] is not None
            }
        else:
            reference = read_report(target)

        reference_video = reference.recorded_video
        if reference_video is None or not reference_video.is_file():
            if new_archive is not None:
                new_archive.close()
            raise ValueError("The selected Reference video is unavailable")
        reference_stream = next(
            (key for key in ("Raw video", "Source video", "Overlay") if key in reference.videos),
            "Raw video",
        )
        movement_data = load_report_movement_data(reference)
        coach_data = load_report_coach_data(reference)
        cfg = self.config_provider()

        def transform(image: np.ndarray, frame_index: int, fps: float) -> np.ndarray:
            return _render_replay_overlay_frame(
                image,
                frame_index,
                fps,
                stream=reference_stream,
                movement_data=movement_data,
                coach_data=coach_data,
                cfg=cfg,
                show_coach=bool(self.show_coach_var.get()),
                show_pivots=bool(self.show_pivots_var.get()),
                show_steps=bool(self.show_steps_var.get()),
                floor_only=bool(self.floor_only_var.get()),
                step_history_seconds=self._step_history_seconds(),
                pane_label="REFERENCE",
            )

        if show_pane:
            assert self.player.reader is not None
            primary_anchor = (
                self.player.clip_range[0]
                if self.player.clip_range is not None
                else self.player.start_frame / self.player.reader.fps
            )
            reference_anchor = float(reference.clip_in or 0.0)
            self.player.compare(
                [reference_video],
                refresh=True,
                specs=[
                    {
                        "clip_range": reference.clip_range,
                        "primary_anchor_source_seconds": primary_anchor,
                        "anchor_source_seconds": reference_anchor,
                        "frame_transform": transform,
                    }
                ],
            )
        else:
            self.player.compare([], refresh=False)
        previous_archive = self.reference_archive
        self.reference_archive = new_archive
        self.reference_record = reference
        self.reference_movement_data = movement_data
        self.reference_coach_data = coach_data
        if previous_archive is not None:
            previous_archive.close()
        self._refresh_posture_analytics()

    def show_video_notes(self, index):
        coach_notes = (
            coach_notes_for_frame(self.coach_data, index)
            if self.coach_data is not None and self.video_selection.get() != "Opened video"
            else None
        )
        if coach_notes is not None:
            self.notes_sidebar.set_notes(
                coach_notes, subtitle=f"Frame {index + 1} · Open practice coach · Synced"
            )
            return
        note_entry = self.frame_notes[index] if index < len(self.frame_notes) else None
        if isinstance(note_entry, dict):
            stored_notes = note_entry.get("notes")
            if isinstance(stored_notes, list):
                notes = [str(line) for line in stored_notes]
            else:
                notes = [
                    f"Role: {note_entry.get('role', 'NONE')} | Embrace: {note_entry.get('embrace_type', 'N/A')}",
                    f"Head: {note_entry.get('head_angle', 'N/A')} | Shoulders: {note_entry.get('shoulder_elev_pct', 'N/A')}% | Knees: {note_entry.get('knee_bend_deg', 'N/A')} deg",
                    f"Hip offset: {note_entry.get('axis_curve_ratio', 'N/A')} | Feet: {note_entry.get('ankle_lift_ratio', 'N/A')}",
                ]
        elif isinstance(note_entry, list):
            notes = [str(line) for line in note_entry]
        else:
            notes = ["No separate notes for this frame. Older recordings retain their original footer."]
        self.notes_sidebar.set_notes(notes, subtitle=f"Frame {index + 1} · Synced with playback")

    def open_video(self):
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("Video", "*.mp4 *.mov *.avi *.mkv"), ("All files", "*")]
        )
        if path:
            self.player.unload()
            self.frame_notes = []
            self.coach_data = None
            self._clear_standalone_movement()
            resolved = Path(path).resolve()
            self.set_import_video_file(resolved)
            self.opened_video_path = resolved
            self.show_pivots_var.set(True)
            self.show_steps_var.set(True)
            if not self.import_panel_visible:
                self.toggle_import_panel()
            self.import_status.set(
                "Choose a clip range, then click Analyse clip to generate coach and movement overlays."
            )
            self.video_selection.set("Opened video")
            self._refresh_movement_state(redraw=True)
            if self.player.reader is not None and self.player.reader.duration <= 30.0:
                self.start_reference_import()
            self.tabs.select(self.video_page)

    def cut_video_dialog(self) -> None:
        if self.player.reader is None:
            raise ValueError("Open a video in Video Replay first")
        if self.cut_dialog is not None and self.cut_dialog.winfo_exists():
            self.cut_dialog.lift()
            return

        source = self.player.reader.path
        bounds = self.player.clip_range or (0.0, self.player.reader.duration)
        dialog = tk.Toplevel(self)
        dialog.title("Cut video segment")
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(True, False)
        dialog.columnconfigure(1, weight=1)
        self.cut_dialog = dialog
        start_var = tk.StringVar(dialog, value=time_label(bounds[0]))
        end_var = tk.StringVar(dialog, value=time_label(bounds[1]))
        output_var = tk.StringVar(
            dialog, value=str(source.with_name(f"{source.stem}_clip.mp4"))
        )
        self.cut_status_var = tk.StringVar(dialog, value="")

        ttk.Label(dialog, text="Start time").grid(row=0, column=0, sticky="w", padx=10, pady=6)
        ttk.Entry(dialog, textvariable=start_var, width=18).grid(
            row=0, column=1, sticky="ew", padx=10, pady=6
        )
        ttk.Label(dialog, text="End time").grid(row=1, column=0, sticky="w", padx=10, pady=6)
        ttk.Entry(dialog, textvariable=end_var, width=18).grid(
            row=1, column=1, sticky="ew", padx=10, pady=6
        )
        ttk.Label(dialog, text="Output file").grid(row=2, column=0, sticky="w", padx=10, pady=6)
        ttk.Entry(dialog, textvariable=output_var).grid(
            row=2, column=1, sticky="ew", padx=(10, 4), pady=6
        )

        def browse_output() -> None:
            selected = filedialog.asksaveasfilename(
                parent=dialog,
                title="Save sliced video",
                initialdir=str(source.parent),
                initialfile=Path(output_var.get()).name,
                defaultextension=".mp4",
                filetypes=(("MP4 video", "*.mp4"), ("AVI video", "*.avi")),
            )
            if selected:
                output_var.set(selected)

        ttk.Button(dialog, text="Browse…", command=lambda: self.guard(browse_output)).grid(
            row=2, column=2, padx=(4, 10), pady=6
        )
        ttk.Label(dialog, textvariable=self.cut_status_var, wraplength=560).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=10, pady=6
        )

        def start_cut() -> None:
            if self.cut_job:
                return
            status_var = self.cut_status_var
            cut_button = self.cut_button
            if status_var is None or cut_button is None:
                return
            try:
                start = parse_time(start_var.get())
                end = parse_time(end_var.get())
                if not output_var.get().strip():
                    raise ValueError("Choose an output file path")
                output = Path(output_var.get()).expanduser()
                if start < 0.0 or end <= start:
                    raise ValueError("End time must be greater than a nonnegative start time")
            except (ValueError, OSError) as error:
                status_var.set(str(error))
                return

            self.cut_job = uuid4().hex
            job_id = self.cut_job
            status_var.set("Cutting video…")
            cut_button.configure(state="disabled")

            def work() -> None:
                try:
                    result = cut_video_segment(source, output, start, end)
                except Exception as error:  # noqa: BLE001 - report worker errors to the UI
                    self.events.put((job_id, "video_cut_error", (str(error),)))
                else:
                    self.events.put((job_id, "video_cut_done", (result,)))

            threading.Thread(target=work, name="video-cut-worker", daemon=True).start()

        self.cut_button = ttk.Button(
            dialog, text="Cut video", command=lambda: self.guard(start_cut)
        )
        self.cut_button.grid(row=4, column=1, sticky="e", padx=10, pady=(4, 10))
        ttk.Button(dialog, text="Close", command=dialog.destroy).grid(
            row=4, column=2, sticky="e", padx=(4, 10), pady=(4, 10)
        )

    def save_overlay_video_dialog(self) -> None:
        reader = self.player.reader
        if reader is None:
            raise ValueError("Open a video in Video Replay first")
        if self.overlay_export_job:
            raise ValueError("An overlay video export is already running")
        export_button = self.overlay_export_button
        if export_button is None:
            raise RuntimeError("Overlay export control is unavailable")
        stream = self.video_stream_keys.get(
            self.video_selection.get(), self.video_selection.get()
        )
        dynamic_overlay = stream in ("Dynamic overlay", "Dynamic lines-only")
        show_coach = bool(
            (self.show_coach_var.get() or dynamic_overlay) and self.coach_data is not None
        )
        show_pivots = bool(
            (self.show_pivots_var.get() or dynamic_overlay)
            and self.movement_data is not None
        )
        show_steps = bool(
            (self.show_steps_var.get() or dynamic_overlay)
            and self.movement_data is not None
        )
        if not (show_coach or show_pivots or show_steps):
            raise ValueError("Enable an available coach, pivot, or step overlay first")

        source = reader.path
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="Save overlay video",
            initialdir=str(source.parent),
            initialfile=f"{source.stem}_overlay.mp4",
            defaultextension=".mp4",
            filetypes=(("MP4 video", "*.mp4"),),
        )
        if not selected:
            return
        output = Path(selected).expanduser()
        start_frame, end_frame = self.player.start_frame, self.player.end_frame
        movement_data = self.movement_data
        coach_data = self.coach_data
        cfg = self.config_provider()
        step_history_seconds = self._step_history_seconds()
        floor_only = bool(self.floor_only_var.get())
        job_id = uuid4().hex
        self.overlay_export_job = job_id
        export_button.configure(state="disabled")
        self.movement_status.set("Saving overlay video…")

        def transform(image: Any, frame_index: int, fps: float) -> np.ndarray:
            return _render_replay_overlay_frame(
                image,
                frame_index,
                fps,
                stream=stream,
                movement_data=movement_data,
                coach_data=coach_data,
                cfg=cfg,
                show_coach=show_coach,
                show_pivots=show_pivots,
                show_steps=show_steps,
                floor_only=floor_only,
                step_history_seconds=step_history_seconds,
                pane_label="PRACTICE",
            )

        def work() -> None:
            try:
                path, audio_muxed = save_overlay_video(
                    source,
                    output,
                    start_frame,
                    end_frame,
                    frame_transform=transform,
                )
            except Exception as error:  # noqa: BLE001 - report worker errors to the UI
                self.events.put((job_id, "overlay_export_error", (str(error),)))
            else:
                self.events.put((job_id, "overlay_export_done", (path, audio_muxed)))

        threading.Thread(target=work, name="overlay-video-export", daemon=True).start()

    def _clear_standalone_movement(self):
        if self.opened_video_path is not None:
            old_key = RecordingTarget("standalone", self.opened_video_path, 0).key
            self.movement_coordinator.cancel(old_key)
        self.opened_video_path = None
        self.session_movement_results = {
            k: v for k, v in self.session_movement_results.items() if k[0] != "standalone"
        }
        self.movement_progress_by_key = {
            k: v for k, v in self.movement_progress_by_key.items() if k[0] != "standalone"
        }
        self.movement_state_by_key = {
            k: v for k, v in self.movement_state_by_key.items() if k[0] != "standalone"
        }

    def _step_history_seconds(self) -> float:
        if "replay_step_history_seconds" in self.settings:
            try:
                val = float(str(self.settings["replay_step_history_seconds"].get()))
                if 0.5 <= val <= 30.0:
                    return val
            except (ValueError, TypeError, tk.TclError):
                pass
        try:
            return float(self.config_provider().replay_step_history_seconds)
        except (ValueError, TypeError, tk.TclError):
            return 5.0

    def _on_step_history_changed(self):
        if not self.closed and self.player.reader is not None:
            self.player.refresh_frame()

    def _expected_model_hash(self) -> str | None:
        if self.movement_coordinator.estimator_factory is not MediaPipeEstimator:
            return getattr(self.movement_coordinator.estimator_factory, "model_hash", None)
        model_str = (
            str(self.settings["model_path"].get())
            if "model_path" in self.settings
            else str(self.config_provider().model_path)
        )
        model_path = Path(model_str)
        if model_path.is_file():
            try:
                return file_sha256(model_path)
            except OSError:
                return None
        return None

    def _resolve_movement_target(self) -> tuple[RecordingTarget | None, Path | None, str]:
        if self.opened_video_path is not None and self.video_selection.get() == "Opened video":
            if self.opened_video_path.is_file():
                return (
                    RecordingTarget("standalone", self.opened_video_path, 0),
                    self.opened_video_path,
                    "",
                )
            return None, None, "Unavailable: video file not found"

        if self.archive is not None and self.current_segment_index is not None:
            if self.archive.manifest.get("kind") == "calibration":
                return None, None, "Unavailable (calibration reference)"
            raw_path = self.report.videos.get("Raw video") if self.report else None
            if raw_path is None or not raw_path.is_file():
                if not self.archive.path.is_file():
                    return None, None, "Practice archive is missing. Reopen or relink the Practice video."
                return None, None, "Raw video required"
            if not self.archive.path.is_file():
                path = raw_path.resolve()
                return RecordingTarget("standalone", path, 0), path, ""
            return (
                RecordingTarget("archive", self.archive.path.resolve(), self.current_segment_index),
                raw_path,
                "",
            )

        if self.report is not None:
            raw_path = self.report.videos.get("Raw video") or self.report.videos.get("Source video")
            if raw_path is not None and raw_path.is_file():
                return (
                    RecordingTarget("standalone", raw_path.resolve(), 0),
                    raw_path.resolve(),
                    "",
                )
            if "Overlay" in self.report.videos:
                return None, None, "Raw video required"
            return None, None, "Unavailable (no raw video)"

        return None, None, "Not analysed"

    def _refresh_movement_state(self, *, redraw: bool = False):
        target, raw_path, _ = self._resolve_movement_target()
        self.movement_data = None
        if target is not None and raw_path is not None and raw_path.is_file():
            key = target.key
            exp_seg = target.segment_index if target.kind == "archive" else -1
            try:
                raw_sha = file_sha256(raw_path)
            except OSError:
                raw_sha = None
            exp_model = self._expected_model_hash()

            candidate: dict[str, Any] | None = None
            if key in self.session_movement_results:
                candidate = self.session_movement_results[key][0]
            elif target.kind == "archive":
                if self.archive is not None and self.archive.path.resolve() == target.path:
                    member_file = self.archive.folder / movement_member_name(target.segment_index)
                    if member_file.is_file():
                        try:
                            candidate = json.loads(member_file.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            candidate = None
                if candidate is None:
                    try:
                        candidate = read_movement_analysis(target.path, target.segment_index)
                    except (OSError, ValueError):
                        candidate = None
            elif self.report is not None:
                candidate = load_report_movement_data(self.report)

            if candidate is not None and raw_sha is not None:
                try:
                    self.movement_data = validate_movement_analysis(
                        candidate,
                        expected_segment_index=exp_seg,
                        expected_source_sha256=raw_sha,
                        expected_model_hash=exp_model,
                    )
                except (ValueError, TypeError, KeyError):
                    self.movement_data = None

        self._sync_movement_ui()
        if redraw and self.player.reader is not None:
            self.player.refresh_frame()

    def _sync_movement_ui(self):
        target, _, reason = self._resolve_movement_target()
        if target is None:
            self.movement_button.configure(text="Analyse movement", state="disabled")
            self.movement_cancel_button.configure(state="disabled")
            self.movement_status.set(reason)
            return

        key = target.key
        has_valid = self.movement_data is not None
        button_label = "Re-run movement analysis" if has_valid else "Analyse movement"

        if self.movement_coordinator.is_active(key) or key in self.movement_progress_by_key:
            pct = self.movement_progress_by_key.get(key, 0)
            self.movement_button.configure(text=button_label, state="disabled")
            self.movement_cancel_button.configure(state="normal")
            self.movement_status.set(f"Analysing {pct}%")
            return

        if self.movement_coordinator.is_queued(key):
            self.movement_button.configure(text=button_label, state="disabled")
            self.movement_cancel_button.configure(state="normal")
            self.movement_status.set("Queued")
            return

        self.movement_button.configure(text=button_label, state="normal")
        self.movement_cancel_button.configure(state="disabled")

        selected_stream = self.video_stream_keys.get(
            self.video_selection.get(), self.video_selection.get()
        )
        if (
            self.movement_data is not None
            and self.player.reader is not None
            and selected_stream in (
                "Raw video",
                "Source video",
                "Opened video",
                "Dynamic overlay",
                "Dynamic lines-only",
            )
            and (
                self.player.reader.width != self.movement_data.get("width")
                or self.player.reader.height != self.movement_data.get("height")
            )
        ):
            self.movement_status.set("Unavailable: video dimensions do not match analysis")
            return

        if key in self.movement_state_by_key:
            state_msg = self.movement_state_by_key[key]
            if has_valid and state_msg == "Cancelled":
                self.movement_status.set("Cancelled (previous result kept)")
            elif has_valid and state_msg.startswith("Unavailable"):
                self.movement_status.set(f"{state_msg} (previous result kept)")
            else:
                self.movement_status.set(state_msg)
            return

        if has_valid and self.movement_data is not None:
            has_events = bool(
                self.movement_data.get("pivots") or self.movement_data.get("steps")
            )
            base = "Ready" if has_events else "No reliable events"
            if key in self.session_movement_results and self.session_movement_results[key][1]:
                base += " (session only; archive is read-only)"
            elif (
                target.kind == "standalone"
                and key in self.session_movement_results
                and not (self.report and self.report.movement_analysis_file)
            ):
                base += " (session only)"
            self.movement_status.set(base)
        else:
            self.movement_status.set("Not analysed")

    def on_movement_toggle(self):
        enabled = bool(self.show_pivots_var.get() or self.show_steps_var.get())
        if enabled and self.report is not None:
            current_stream = self.video_stream_keys.get(
                self.video_selection.get(), self.video_selection.get()
            )
            if current_stream in (
                "Overlay",
                "Full lines + messages",
                "Dynamic overlay",
                "Dynamic lines-only",
            ):
                raw_label = "Practice video" if "Practice video" in self.video_paths else None
                if raw_label is not None:
                    current_time = self.player.position.get()
                    self.video_selection.set(raw_label)
                    self.switch_video()
                    self.status.set(
                        f"Switched to Practice video at {current_time:.1f}s to show the movement analysis overlay."
                    )
                    return
                self.movement_status.set("Raw video required")
                self.status.set("Raw video required to show the movement analysis overlay.")
                return
        self._sync_movement_ui()
        if self.player.reader is not None:
            self.player.refresh_frame()

    def _transform_replay_frame(
        self, image: np.ndarray, frame_index: int, fps: float
    ) -> np.ndarray:
        return _render_replay_overlay_frame(
            image,
            frame_index,
            fps,
            stream=self.video_stream_keys.get(
                self.video_selection.get(), self.video_selection.get()
            ),
            movement_data=self.movement_data,
            coach_data=self.coach_data,
            cfg=self.config_provider(),
            show_coach=bool(self.show_coach_var.get()),
            show_pivots=bool(self.show_pivots_var.get()),
            show_steps=bool(self.show_steps_var.get()),
            floor_only=bool(self.floor_only_var.get()),
            step_history_seconds=self._step_history_seconds(),
            pane_label="PRACTICE",
        )

    def on_floor_only_toggle(self):
        if self.player.reader is not None:
            self.player.refresh_frame()

    def on_coach_toggle(self):
        if self.player.reader is not None:
            self.player.refresh_frame()

    def request_movement_analysis(self):
        if not self.replay_controls_visible:
            self.toggle_replay_controls()
        target, _, reason = self._resolve_movement_target()
        if target is None:
            self.movement_status.set(reason)
            return
        model_str = (
            str(self.settings["model_path"].get())
            if "model_path" in self.settings
            else str(self.config_provider().model_path)
        )
        self.movement_state_by_key.pop(target.key, None)
        self.movement_coordinator.enqueue(target, Path(model_str))
        self._sync_movement_ui()

    def cancel_movement_analysis(self):
        target, _, _ = self._resolve_movement_target()
        if target is not None:
            self.movement_coordinator.cancel(target.key)
            self._sync_movement_ui()

    def on_segment_saved(self, archive_path: Path, segment_index: int):
        resolved = archive_path.resolve()
        if resolved.suffix.lower() != ".tp" or not resolved.is_file():
            return
        auto_enabled = (
            bool(self.settings["replay_auto_analyse_movement"].get())
            if "replay_auto_analyse_movement" in self.settings
            else bool(self.config_provider().replay_auto_analyse_movement)
        )
        if not auto_enabled:
            return
        model_str = (
            str(self.settings["model_path"].get())
            if "model_path" in self.settings
            else str(self.config_provider().model_path)
        )
        target = RecordingTarget("archive", resolved, int(segment_index))
        self.movement_state_by_key.pop(target.key, None)
        self.movement_coordinator.enqueue(target, Path(model_str))
        self._sync_movement_ui()

    def seek_report_time(self, seconds):
        def seek():
            if not self.player.reader:
                raise ValueError(
                    "This report has no available video. Open its recording with Open video."
                )
            selected_stream = self.video_stream_keys.get(
                self.video_selection.get(), self.video_selection.get()
            )
            if self.report and selected_stream in self.report.videos:
                seconds_in_file = self.report.recording_seconds(
                    seconds, self.player.reader.fps, selected_stream
                )
            else:
                seconds_in_file = seconds
            self.player.seek(seconds_in_file)
            self.tabs.select(self.video_page)

        self.guard(seek)

    def refresh_analyses(self):
        self.analyses = {}
        self.saved_selection.set("")
        self.saved_combo.configure(values=[])
        if self.report:
            for path, data in saved_analyses(self.report):
                label = f"{data['created_at_utc']} / {data['model']} / {path.stem[-8:]}"
                self.analyses[label] = data
        self.saved_combo.configure(values=list(self.analyses))
        if self.analyses:
            self.saved_selection.set(next(iter(self.analyses)))
            self.show_saved()

    def show_saved(self):
        data = self.analyses[self.saved_selection.get()]
        self.analysis_data = data
        self.analysis_view.set(data["analysis"])
        self.analysis_meta.set(
            f"AI analysis / {data['model']} / {data['created_at_utc']} / {data['endpoint']}"
            + (" / Incomplete output" if not data.get("complete", True) else "")
        )

    def list_models(self):
        client = OllamaClient(self.config_provider())
        token = self.models_job = uuid4().hex
        self.models_button.configure(state="disabled")
        self.analysis_status.set("Reading installed models...")
        self.analysis_progress.start(12)
        events = self.events

        def work():
            try:
                events.put((token, "models", client.models()))
            except (ValueError, OSError, RuntimeError, TypeError, HTTPException) as error:
                events.put((token, "models_error", str(error)))

        threading.Thread(target=work, name="ollama-models", daemon=True).start()

    def analyze(self):
        if not self.report:
            raise ValueError("Open a practice report first")
        cfg = self.config_provider()
        client = OllamaClient(cfg)
        self.cancel_analysis()
        report = self.report
        token = self.job = uuid4().hex
        cancel = self.cancel = threading.Event()
        self.analysis_data = None
        self.analysis_stream = ""
        self.analysis_view.set("")
        self.analysis_meta.set(f"AI analysis / {cfg.ollama_model} / {client.url}")
        self.analysis_status.set("Waiting for Ollama; model loading can take a moment...")
        self.analysis_progress.start(12)
        self.analyze_button.configure(state="disabled")
        self.models_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.saved_combo.configure(state="disabled")
        self.tabs.select(self.analysis_page)
        events = self.events

        def work():
            try:
                text, complete = client.analyze(
                    report.markdown, lambda chunk: events.put((token, "chunk", chunk)), cancel
                )
                if not cancel.is_set():
                    path = save_analysis(
                        report, cfg.ollama_model, client.url, cfg.ollama_instruction, text, complete
                    )
                    events.put((token, "analysis_done", (text, complete, path)))
            except (ValueError, OSError, RuntimeError, TypeError, HTTPException) as error:
                events.put((token, "analysis_error", str(error)))

        threading.Thread(target=work, name="ollama-analysis", daemon=True).start()

    def request_analysis(self):
        try:
            self.analyze()
        except (ValueError, OSError, RuntimeError, TypeError, KeyError) as error:
            self.analysis_status.set(str(error))
            self.analysis_meta.set("")
            self.analysis_progress.stop()

    def cancel_analysis(self):
        self.cancel.set()
        self.analysis_progress.stop()
        if self.job:
            self.job = ""
            self.analysis_status.set("Analysis cancelled. Partial text has not been saved.")
        self.analyze_button.configure(state="normal")
        self.models_button.configure(state="normal" if not self.models_job else "disabled")
        self.cancel_button.configure(state="disabled")
        self.saved_combo.configure(state="readonly")

    def export_analysis(self):
        if not self.analysis_data:
            raise ValueError("Complete or select a saved analysis first")
        data = self.analysis_data
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".md")
        if path:
            Path(path).write_text(
                f"# AI practice analysis\n\nModel: {data['model']}\nGenerated: {data['created_at_utc']}\nReport: {self.report.path.name if self.report else ''}\n\n{data['analysis']}\n",
                encoding="utf-8",
            )

    def pump(self):
        if self.closed:
            return
        while True:
            try:
                token, kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ("models", "models_error") and token == self.models_job:
                self.models_job = ""
                self.analysis_progress.stop()
                self.models_button.configure(state="disabled" if self.job else "normal")
                if kind == "models":
                    self.model_combo.configure(values=value)
                    if not self.job:
                        self.analysis_status.set(
                            f"{len(value)} installed model(s). Choose a model or type its name."
                        )
                elif not self.job:
                    self.analysis_status.set(value)
            elif kind == "movement_queued":
                (key,) = value
                self.movement_state_by_key.pop(key, None)
                self._sync_movement_ui()
            elif kind == "movement_progress":
                key, pct = value
                self.movement_progress_by_key[key] = int(pct)
                self._sync_movement_ui()
            elif kind == "movement_done":
                key, result, readonly_warning = value
                self.movement_progress_by_key.pop(key, None)
                self.movement_state_by_key.pop(key, None)
                if readonly_warning or key[0] == "standalone":
                    self.session_movement_results[key] = (result, bool(readonly_warning))
                else:
                    self.session_movement_results.pop(key, None)
                if (
                    key[0] == "archive"
                    and not readonly_warning
                    and self.archive is not None
                    and self.archive.path.resolve() == Path(key[1])
                ):
                    try:
                        (self.archive.folder / movement_member_name(int(key[2]))).write_text(
                            json.dumps(result, indent=2) + "\n", encoding="utf-8"
                        )
                    except OSError:
                        pass
                current_target, _, _ = self._resolve_movement_target()
                if current_target is not None and current_target.key == key:
                    self._refresh_movement_state(redraw=True)
                else:
                    self._sync_movement_ui()
            elif kind == "movement_cancelled":
                (key,) = value
                self.movement_progress_by_key.pop(key, None)
                self.movement_state_by_key[key] = "Cancelled"
                self._sync_movement_ui()
            elif kind == "movement_error":
                key, error_msg = value
                self.movement_progress_by_key.pop(key, None)
                self.movement_state_by_key[key] = f"Unavailable: {error_msg}"
                self._sync_movement_ui()
            elif kind == "import_progress":
                cur, tot, msg = value
                self.import_status.set(f"{msg} ({cur}/{tot}%)")
                self.import_start_button.configure(state="disabled")
                self.import_cancel_button.configure(state="normal")
            elif kind == "import_done":
                (import_res,) = value
                assert isinstance(import_res, ImportedClipResult)
                self.import_cancel_button.configure(state="disabled")
                self._sync_import_form_state()
                self.import_status.set(
                    f"Completed: '{import_res.title}' ({import_res.selected_out - import_res.selected_in:.1f}s)"
                )
                self.load_report(import_res.report_path)
                self.tabs.select(self.video_page)
            elif kind == "youtube_downloaded":
                path, link = value
                self.import_cancel_button.configure(state="disabled")
                self.apply_downloaded_youtube_video(path, link)
            elif kind == "import_cancelled":
                self.import_cancel_button.configure(state="disabled")
                self._sync_import_form_state()
                self.import_status.set("Reference video import cancelled.")
            elif kind == "import_error":
                (err_msg,) = value
                self.import_cancel_button.configure(state="disabled")
                self._sync_import_form_state()
                self.import_status.set(f"Import failed: {err_msg}")
                self.status.set(f"Reference import failed: {err_msg}")
            elif kind in ("video_cut_done", "video_cut_error") and token == self.cut_job:
                self.cut_job = ""
                successful = kind == "video_cut_done"
                message = (
                    f"Saved sliced video: {value[0]}"
                    if successful
                    else f"Video cut failed: {value[0]}"
                )
                self.status.set(message)
                if self.cut_dialog is not None and self.cut_dialog.winfo_exists():
                    if self.cut_status_var is not None:
                        self.cut_status_var.set(message)
                    if self.cut_button is not None:
                        self.cut_button.configure(state="normal")
            elif kind in ("overlay_export_done", "overlay_export_error") and token == self.overlay_export_job:
                self.overlay_export_job = ""
                if kind == "overlay_export_done":
                    path, audio_muxed = value
                    audio_note = "" if audio_muxed else " (audio not copied; FFmpeg unavailable)"
                    self.movement_status.set(f"Saved overlay video: {path}{audio_note}")
                else:
                    self.movement_status.set(f"Overlay export failed: {value[0]}")
                if self.overlay_export_button is not None:
                    self.overlay_export_button.configure(state="normal")
            elif kind == "analytics_progress" and token == self.analytics_job:
                current, total, message = value
                self.analytics_status.set(f"Practice analysis {current}/{total}% · {message}")
            elif kind == "analytics_done" and token == self.analytics_job:
                coach_data, movement_data, selected_in, selected_out = value
                self.analytics_job = ""
                self.practice_analytics_data = coach_data
                self.coach_data = coach_data
                self.movement_data = movement_data
                self.show_coach_var.set(True)
                if self.analytics_analyse_button is not None:
                    self.analytics_analyse_button.configure(
                        state="normal", text="Re-analyze Practice"
                    )
                self._refresh_posture_analytics()
                self.analytics_status.set(
                    self.analytics_status.get()
                    + f" Practice analyzed: {time_label(selected_in)}–{time_label(selected_out)}."
                )
                if self.player.reader is not None:
                    self.player.refresh_frame()
            elif kind == "analytics_error" and token == self.analytics_job:
                self.analytics_job = ""
                if self.analytics_analyse_button is not None:
                    self.analytics_analyse_button.configure(state="normal")
                self.analytics_status.set(f"Practice analysis failed: {value[0]}")
            elif token == self.job:
                if kind == "chunk":
                    self.analysis_status.set("Receiving analysis...")
                    self.analysis_stream += value
                    self.analysis_view.set(self.analysis_stream)
                    self.analysis_view.text.see("end")
                elif kind in ("analysis_done", "analysis_error"):
                    self.job = ""
                    self.analysis_progress.stop()
                    self.analyze_button.configure(state="normal")
                    self.models_button.configure(
                        state="normal" if not self.models_job else "disabled"
                    )
                    self.cancel_button.configure(state="disabled")
                    self.saved_combo.configure(state="readonly")
                    if kind == "analysis_done":
                        text, complete, path = value
                        self.analysis_view.set(text)
                        self.analysis_status.set(
                            f"Saved {path.name}"
                            if complete
                            else "Saved incomplete output; the model reached its generation limit."
                        )
                        self.guard(self.refresh_analyses)
                    else:
                        self.analysis_status.set(value + " / Partial text has not been saved.")
        self.timer = self.after(50, self.pump)

    def _build_import_panel(self, parent: ttk.Frame) -> None:
        self.import_panel = ttk.LabelFrame(
            parent, text="Import Reference Video (Teacher / Study Clip)", padding=6
        )
        row0 = ttk.Frame(self.import_panel)
        row0.pack(fill="x", pady=2)
        ttk.Label(row0, text="Local video:").pack(side="left")
        ttk.Entry(row0, textvariable=self.import_video_var, width=36).pack(
            side="left", padx=4, fill="x", expand=True
        )
        ttk.Button(
            row0,
            text="Choose file…",
            command=lambda: self.guard(self.choose_import_video_file),
        ).pack(side="left", padx=3)
        ttk.Label(row0, textvariable=self.import_file_info).pack(side="left", padx=6)

        row1 = ttk.Frame(self.import_panel)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Clip In (s):").pack(side="left")
        in_entry = ttk.Entry(row1, textvariable=self.import_in_var, width=8)
        in_entry.pack(side="left", padx=3)
        in_entry.bind("<FocusOut>", lambda _: self.guard(self._sync_import_form_state))
        ttk.Button(
            row1,
            text="Set In from player",
            command=lambda: self.guard(self.set_import_in_from_player),
        ).pack(side="left", padx=2)
        ttk.Label(row1, text="Out (s):").pack(side="left", padx=(8, 0))
        out_entry = ttk.Entry(row1, textvariable=self.import_out_var, width=8)
        out_entry.pack(side="left", padx=3)
        out_entry.bind("<FocusOut>", lambda _: self.guard(self._sync_import_form_state))
        ttk.Button(
            row1,
            text="Set Out from player",
            command=lambda: self.guard(self.set_import_out_from_player),
        ).pack(side="left", padx=2)
        ttk.Label(row1, textvariable=self.import_range_status).pack(side="left", padx=8)

        row2 = ttk.Frame(self.import_panel)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Title:").pack(side="left")
        ttk.Entry(row2, textvariable=self.import_title_var, width=22).pack(side="left", padx=4)
        ttk.Label(row2, text="YouTube link:").pack(side="left", padx=(8, 0))
        yt_entry = ttk.Entry(row2, textvariable=self.import_youtube_var, width=32)
        yt_entry.pack(side="left", padx=4, fill="x", expand=True)
        yt_entry.bind("<FocusOut>", lambda _: self.guard(self._sync_import_form_state))
        self.import_open_yt_button = ttk.Button(
            row2,
            text="Open on YouTube",
            command=lambda: self.guard(self.open_import_youtube_link),
        )
        self.import_open_yt_button.pack(side="left", padx=3)
        self.import_download_button = ttk.Button(
            row2,
            text="Download from YouTube",
            state="disabled",
            command=lambda: self.guard(self.download_youtube_to_local),
        )
        self.import_download_button.pack(side="left", padx=3)

        row3 = ttk.Frame(self.import_panel)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Mode:").pack(side="left")
        ttk.Radiobutton(
            row3,
            text="Movement review",
            value="movement",
            variable=self.import_mode_var,
            command=lambda: self.guard(self._sync_import_form_state),
        ).pack(side="left", padx=4)
        ttk.Radiobutton(
            row3,
            text="Reference-relative posture review",
            value="posture",
            variable=self.import_mode_var,
            command=lambda: self.guard(self._sync_import_form_state),
        ).pack(side="left", padx=4)
        ttk.Label(row3, text="Reference:").pack(side="left", padx=(8, 2))
        self.import_ref_combo = ttk.Combobox(
            row3,
            textvariable=self.import_reference_var,
            values=("latest", "clip_interval"),
            width=16,
        )
        self.import_ref_combo.pack(side="left", padx=2)
        self.import_ref_combo.bind(
            "<<ComboboxSelected>>", lambda _: self.guard(self._sync_import_form_state)
        )
        ttk.Label(row3, text="Cal [in, out]:").pack(side="left", padx=(6, 2))
        ttk.Entry(row3, textvariable=self.import_cal_in_var, width=6).pack(side="left", padx=1)
        ttk.Entry(row3, textvariable=self.import_cal_out_var, width=6).pack(side="left", padx=1)
        ttk.Checkbutton(
            row3,
            text="Analyse video soundtrack",
            variable=self.import_soundtrack_var,
        ).pack(side="left", padx=8)

        row4 = ttk.Frame(self.import_panel)
        row4.pack(fill="x", pady=2)
        ttk.Checkbutton(
            row4,
            text="Confirm local file & YouTube are same edit",
            variable=self.import_confirm_same_edit_var,
            command=lambda: self.guard(self._sync_import_form_state),
        ).pack(side="left")
        ttk.Label(row4, text="Local time (s):").pack(side="left", padx=(8, 2))
        ttk.Entry(row4, textvariable=self.import_align_local_var, width=7).pack(side="left")
        ttk.Label(row4, text="YouTube time (s):").pack(side="left", padx=(6, 2))
        ttk.Entry(row4, textvariable=self.import_align_youtube_var, width=7).pack(side="left")
        ttk.Button(
            row4,
            text="Save YouTube alignment",
            command=lambda: self.guard(self.save_current_youtube_alignment),
        ).pack(side="left", padx=6)
        ttk.Label(row4, textvariable=self.import_readiness_status).pack(side="left", padx=6)

        row5 = ttk.Frame(self.import_panel)
        row5.pack(fill="x", pady=(4, 0))
        self.import_start_button = ttk.Button(
            row5,
            text="Analyse clip",
            state="disabled",
            command=lambda: self.guard(self.start_reference_import),
        )
        self.import_start_button.pack(side="left")
        self.import_cancel_button = ttk.Button(
            row5,
            text="Cancel import",
            state="disabled",
            command=lambda: self.guard(self.cancel_reference_import),
        )
        self.import_cancel_button.pack(side="left", padx=6)
        ttk.Label(row5, textvariable=self.import_status, wraplength=700).pack(
            side="left", padx=8, fill="x", expand=True
        )

    def toggle_import_panel(self) -> None:
        if not self.replay_controls_visible:
            self.toggle_replay_controls()
        if self.import_panel_visible:
            self.import_panel.pack_forget()
            self.import_panel_visible = False
        else:
            self.import_panel.pack(fill="x", padx=6, pady=4, before=self.reference_bar)
            self.import_panel_visible = True
            self._sync_import_form_state()
        self.tabs.select(self.video_page)

    def choose_import_video_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            filetypes=[("Video files", "*.mp4 *.mov *.m4v *.avi *.mkv"), ("All files", "*")],
        )
        if path:
            self.set_import_video_file(Path(path))

    def set_import_video_file(self, path: Path | str) -> VideoProbeInfo:
        probe = probe_import_video(Path(path))
        self._downloaded_youtube_file = None
        self.import_probe = probe
        self.import_video_var.set(str(probe.path))
        if not self.import_title_var.get().strip():
            self.import_title_var.set(probe.path.stem)
        self.import_in_var.set("0.0")
        default_out = min(probe.duration_seconds, MAX_IMPORT_RANGE_SECONDS)
        self.import_out_var.set(f"{default_out:.2f}")
        self.import_file_info.set(
            f"{probe.width}x{probe.height} · {probe.fps:.1f} FPS · {probe.duration_seconds:.1f}s"
        )
        # Also load into player for previewing & setting in/out points
        self.player.load(probe.path)
        self.opened_video_path = probe.path
        self.video_selection.set("Opened video")
        self._sync_import_form_state()
        return probe

    def set_import_in_from_player(self) -> None:
        self.import_in_var.set(f"{self.player.source_time_seconds:.2f}")
        self._sync_import_form_state()

    def set_import_out_from_player(self) -> None:
        self.import_out_var.set(f"{self.player.source_time_seconds:.2f}")
        self._sync_import_form_state()

    def _resolve_selected_import_reference(self, cfg: Config):
        choice = self.import_reference_var.get().strip()
        if not choice or choice == "clip_interval":
            return None
        cal_dir = calibration_directory(cfg.data_dir)
        return load_calibration_file(choice, cal_dir)

    def _resolve_import_calibration_interval(self) -> tuple[float, float] | None:
        raw_in = self.import_cal_in_var.get().strip()
        raw_out = self.import_cal_out_var.get().strip()
        if not raw_in or not raw_out:
            return None
        return (parse_time(raw_in), parse_time(raw_out))

    def _sync_import_form_state(self) -> None:
        cfg = self.config_provider()
        raw_video = self.import_video_var.get().strip()
        raw_yt = self.import_youtube_var.get().strip()
        yt_link: YouTubeSourceLink | None = None
        if raw_yt:
            try:
                yt_link = parse_youtube_url(raw_yt)
            except ValueError as exc:
                self.import_status.set(str(exc))
                self.import_start_button.configure(state="disabled")
                self.import_download_button.configure(state="disabled")
                return
        busy = self.import_worker.is_running()
        self.import_download_button.configure(
            state="normal" if yt_link is not None and not busy else "disabled"
        )

        if not raw_video:
            self.import_probe = None
            self.import_file_info.set("No local file selected")
            if yt_link is not None:
                try:
                    in_sec, out_sec = self._youtube_clip_range(yt_link)
                    self.import_range_status.set(
                        f"Range [{time_label(in_sec)} - {time_label(out_sec)}] "
                        f"({out_sec - in_sec:.1f}s / {MAX_IMPORT_RANGE_SECONDS:.0f}s max)"
                    )
                except ValueError as exc:
                    self.import_start_button.configure(state="disabled")
                    self.import_status.set(str(exc))
                    return
                self.import_start_button.configure(state="disabled" if busy else "normal")
                self.import_status.set(
                    "Ready: Analyse clip will download the video from YouTube, then analyse it."
                )
            else:
                self.import_start_button.configure(state="disabled")
                self.import_status.set("Choose a local video file or enter a YouTube link")
            return

        try:
            probe = (
                self.import_probe
                if self.import_probe is not None and str(self.import_probe.path) == raw_video
                else probe_import_video(Path(raw_video))
            )
            self.import_probe = probe
            in_sec = parse_time(self.import_in_var.get() or "0.0")
            out_str = self.import_out_var.get().strip()
            out_sec = parse_time(out_str) if out_str else min(probe.duration_seconds, MAX_IMPORT_RANGE_SECONDS)
            clip_in, clip_out = validate_clip_range(probe, in_sec, out_sec)
            self.import_range_status.set(
                f"Range [{time_label(clip_in)} - {time_label(clip_out)}] ({clip_out - clip_in:.1f}s / {MAX_IMPORT_RANGE_SECONDS:.0f}s max)"
            )
            mode = self.import_mode_var.get()
            cal_interval = self._resolve_import_calibration_interval()
            ref_obj = (
                None
                if cal_interval is not None
                else self._resolve_selected_import_reference(cfg)
            )
            readiness = check_calibration_readiness(
                analysis_mode=mode,
                cfg=cfg,
                probe=probe,
                selected_in=clip_in,
                selected_out=clip_out,
                reference=ref_obj,
                calibration_interval=cal_interval,
                model_hash=self._expected_model_hash(),
            )
            self.import_readiness_status.set(
                readiness.summary if readiness.ready else f"{readiness.summary}: {readiness.reason}"
            )
            if readiness.ready and not self.import_worker.is_running():
                self.import_start_button.configure(state="normal")
                self.import_status.set("Ready to analyse clip.")
            else:
                self.import_start_button.configure(state="disabled")
                if not readiness.ready:
                    self.import_status.set(readiness.reason or readiness.summary)
        except ValueError as exc:
            self.import_start_button.configure(state="disabled")
            self.import_status.set(str(exc))

    def _youtube_clip_range(self, link: YouTubeSourceLink) -> tuple[float, float]:
        """Requested `[in, out)` for a not-yet-downloaded YouTube video (duration unknown)."""
        raw_in = self.import_in_var.get().strip()
        in_sec = parse_time(raw_in) if raw_in else 0.0
        if in_sec <= 0.0 and link.anchor_seconds is not None:
            in_sec = float(link.anchor_seconds)
        raw_out = self.import_out_var.get().strip()
        out_sec = parse_time(raw_out) if raw_out else in_sec + MAX_IMPORT_RANGE_SECONDS
        if out_sec <= in_sec:
            raise ValueError("Clip Out must be greater than Clip In")
        if out_sec - in_sec > MAX_IMPORT_RANGE_SECONDS + 1e-6:
            raise ValueError(f"Selected range exceeds the {MAX_IMPORT_RANGE_SECONDS:.0f}s limit")
        return in_sec, out_sec

    def apply_downloaded_youtube_video(
        self, path: Path | str, link: YouTubeSourceLink
    ) -> VideoProbeInfo:
        """Populate Local video and `[in, out)` after a YouTube download."""
        probe = self.set_import_video_file(path)
        self._downloaded_youtube_file = (probe.path.resolve(), link.video_id)
        in_sec = 0.0
        if link.anchor_seconds is not None and link.anchor_seconds < probe.duration_seconds:
            in_sec = float(link.anchor_seconds)
        out_sec = min(probe.duration_seconds, in_sec + MAX_IMPORT_RANGE_SECONDS)
        self.import_in_var.set(f"{in_sec:.2f}")
        self.import_out_var.set(f"{out_sec:.2f}")
        self.import_confirm_same_edit_var.set(True)
        self.import_align_local_var.set(f"{in_sec:.2f}")
        self.import_align_youtube_var.set(f"{in_sec:.2f}")
        self._sync_import_form_state()
        self.import_status.set(
            f"Downloaded {probe.path.name}. Adjust the range if needed, then click Analyse clip."
        )
        return probe

    def download_youtube_to_local(self, *, synchronous: bool = False) -> Path | str:
        raw_yt = self.import_youtube_var.get().strip()
        link = parse_youtube_url(raw_yt)
        if link is None:
            raise ValueError("Enter a valid HTTPS YouTube URL first")
        target_dir = imports_directory(self.config_provider().data_dir)
        if synchronous:
            path, _ = download_youtube_video(
                link.canonical_url, target_dir, downloader=self.import_youtube_downloader
            )
            self.apply_downloaded_youtube_video(path, link)
            return path
        self.import_start_button.configure(state="disabled")
        self.import_download_button.configure(state="disabled")
        self.import_cancel_button.configure(state="normal")
        self.import_status.set("Starting YouTube download…")
        return self.import_worker.start_download(
            link.canonical_url, target_dir, downloader=self.import_youtube_downloader
        )

    def open_import_youtube_link(self) -> str:
        raw_yt = self.import_youtube_var.get().strip()
        link = parse_youtube_url(raw_yt)
        if link is None:
            raise ValueError("Enter a valid HTTPS YouTube URL first")
        url = build_youtube_open_url(link)
        self.last_opened_youtube_url = url
        webbrowser.open(url)
        return url

    def _build_pending_youtube_alignment(
        self,
        *,
        source_sha256: str,
        youtube_video_id: str,
    ) -> YouTubeClockAlignment | None:
        if not self.import_confirm_same_edit_var.get():
            return None
        try:
            local_sec = parse_time(self.import_align_local_var.get() or "0.0")
            yt_sec = parse_time(self.import_align_youtube_var.get() or "0.0")
        except ValueError:
            return None
        return YouTubeClockAlignment(
            local_source_seconds=local_sec,
            youtube_seconds=yt_sec,
            confirmed_same_edit=True,
            source_sha256=source_sha256,
            youtube_video_id=youtube_video_id,
        )

    def start_reference_import(self, *, synchronous: bool = False) -> ImportedClipResult | str:
        self._sync_import_form_state()
        raw_video = self.import_video_var.get().strip()
        raw_yt = self.import_youtube_var.get().strip() or None
        yt_link = parse_youtube_url(raw_yt) if raw_yt else None
        if not raw_video and yt_link is None:
            raise ValueError("Choose a local video file or enter a YouTube link to analyse")
        cfg = self.config_provider()
        mode = self.import_mode_var.get()
        cal_interval = self._resolve_import_calibration_interval()
        ref_obj = None if cal_interval is not None else self._resolve_selected_import_reference(cfg)
        alignment: YouTubeClockAlignment | None = None
        video_arg: Path | str
        if raw_video:
            probe = self.import_probe or probe_import_video(Path(raw_video))
            in_sec = parse_time(self.import_in_var.get() or "0.0")
            out_str = self.import_out_var.get().strip()
            out_sec = (
                parse_time(out_str)
                if out_str
                else min(probe.duration_seconds, MAX_IMPORT_RANGE_SECONDS)
            )
            clip_in, clip_out = validate_clip_range(probe, in_sec, out_sec)
            selected_out_arg: float | None = clip_out
            video_arg = probe.path
            if yt_link is not None:
                if self._downloaded_youtube_file == (probe.path.resolve(), yt_link.video_id):
                    alignment = YouTubeClockAlignment(
                        local_source_seconds=clip_in,
                        youtube_seconds=clip_in,
                        confirmed_same_edit=True,
                        source_sha256=file_sha256(probe.path),
                        youtube_video_id=yt_link.video_id,
                    )
                else:
                    alignment = self._build_pending_youtube_alignment(
                        source_sha256=file_sha256(probe.path),
                        youtube_video_id=yt_link.video_id,
                    )
        else:
            assert yt_link is not None
            # Downloaded inside the worker; analyse_imported_clip fetches then analyses.
            clip_in, clip_out = self._youtube_clip_range(yt_link)
            selected_out_arg = clip_out if self.import_out_var.get().strip() else None
            video_arg = ""
        folder = report_directory(cfg.data_dir)
        kwargs: dict[str, Any] = {
            "video_path": video_arg,
            "cfg": cfg,
            "report_dir": folder,
            "selected_in": clip_in,
            "selected_out": selected_out_arg,
            "youtube_downloader": self.import_youtube_downloader,
            "title": self.import_title_var.get().strip(),
            "youtube_url": raw_yt,
            "youtube_alignment": alignment,
            "analysis_mode": mode,
            "reference": ref_obj,
            "calibration_interval": cal_interval,
            "analyse_soundtrack": bool(self.import_soundtrack_var.get()),
            "estimator_factory": self.import_estimator_factory,
        }
        if synchronous:
            from .reference_import import analyse_imported_clip

            result = analyse_imported_clip(**kwargs)
            self.import_status.set(
                f"Completed: '{result.title}' ({result.selected_out - result.selected_in:.1f}s)"
            )
            self.load_report(result.report_path)
            self.tabs.select(self.video_page)
            return result

        self.import_start_button.configure(state="disabled")
        self.import_cancel_button.configure(state="normal")
        self.import_status.set("Starting reference video analysis…")
        return self.import_worker.start(**kwargs)

    def cancel_reference_import(self) -> None:
        self.import_worker.cancel()

    def _sync_import_and_youtube_controls(self) -> None:
        if self.report is None:
            self.relink_video_button.configure(state="disabled")
            self.open_youtube_button.configure(state="disabled")
            self.open_moment_youtube_button.configure(state="disabled")
            return

        self.relink_video_button.configure(
            state="normal" if (self.report.is_imported_reference or self.report.videos) else "disabled"
        )
        yt_link = YouTubeSourceLink.from_dict(self.report.youtube_link)
        if yt_link is None:
            self.open_youtube_button.configure(state="disabled")
            self.open_moment_youtube_button.configure(state="disabled")
            return

        self.open_youtube_button.configure(state="normal")
        alignment = YouTubeClockAlignment.from_dict(self.report.youtube_alignment)
        clip_in = float(self.report.clip_in or 0.0)
        clip_out = float(
            self.report.clip_out
            if self.report.clip_out is not None and self.report.clip_out > clip_in
            else max(clip_in + 1.0, self.report.duration_seconds)
        )
        cur_src = self.player.source_time_seconds if self.player.reader is not None else clip_in
        if (
            alignment is not None
            and not self.report.relinked_content_changed
            and alignment.is_valid_for(
                source_sha256=self.report.source_sha256,
                youtube_video_id=yt_link.video_id,
                clip_in=clip_in,
                clip_out=clip_out,
                local_source_seconds=cur_src,
            )
        ):
            self.open_moment_youtube_button.configure(state="normal")
        else:
            self.open_moment_youtube_button.configure(state="disabled")

    def save_current_youtube_alignment(self) -> YouTubeClockAlignment:
        if self.report is None:
            raise ValueError("Load an imported reference report first")
        raw_yt = self.import_youtube_var.get().strip()
        if raw_yt:
            yt_link = parse_youtube_url(raw_yt)
            if yt_link is not None:
                self.report.youtube_link = yt_link.to_dict()
        else:
            yt_link = YouTubeSourceLink.from_dict(self.report.youtube_link)
        if yt_link is None:
            raise ValueError("This clip has no YouTube source link")
        if not self.import_confirm_same_edit_var.get():
            update_report_metadata(self.report, {"youtube_alignment": None})
            self.report.youtube_alignment = None
            self._sync_import_and_youtube_controls()
            raise ValueError("Confirm that the local file and YouTube video are the same edit")
        local_sec = parse_time(self.import_align_local_var.get() or "0.0")
        yt_sec = parse_time(self.import_align_youtube_var.get() or "0.0")
        clip_in = float(self.report.clip_in or 0.0)
        clip_out = float(
            self.report.clip_out
            if self.report.clip_out is not None and self.report.clip_out > clip_in
            else max(clip_in + 1.0, self.report.duration_seconds)
        )
        alignment = YouTubeClockAlignment(
            local_source_seconds=local_sec,
            youtube_seconds=yt_sec,
            confirmed_same_edit=True,
            source_sha256=self.report.source_sha256,
            youtube_video_id=yt_link.video_id,
        )
        if not alignment.is_valid_for(
            source_sha256=self.report.source_sha256,
            youtube_video_id=yt_link.video_id,
            clip_in=clip_in,
            clip_out=clip_out,
        ):
            raise ValueError(
                f"Local alignment point ({local_sec:.1f}s) must lie within the analysed clip range [{clip_in:.1f}s, {clip_out:.1f}s)"
            )
        update_report_metadata(
            self.report,
            {
                "youtube_link": yt_link.to_dict(),
                "youtube_alignment": alignment.to_dict(),
            },
        )
        self.report.youtube_alignment = alignment.to_dict()
        self._sync_import_and_youtube_controls()
        self.status.set(
            f"Saved YouTube clock alignment: local {local_sec:.1f}s -> YouTube {yt_sec:.1f}s."
        )
        return alignment

    def open_current_youtube(self) -> str:
        if self.report is None:
            raise ValueError("Open a report with a YouTube source link first")
        yt_link = YouTubeSourceLink.from_dict(self.report.youtube_link)
        if yt_link is None:
            raise ValueError("This report has no saved YouTube source link")
        url = build_youtube_open_url(yt_link)
        self.last_opened_youtube_url = url
        webbrowser.open(url)
        return url

    def open_current_moment_on_youtube(self) -> str:
        if self.report is None:
            raise ValueError("Open a report with a YouTube source link first")
        yt_link = YouTubeSourceLink.from_dict(self.report.youtube_link)
        alignment = YouTubeClockAlignment.from_dict(self.report.youtube_alignment)
        if yt_link is None or alignment is None:
            raise ValueError("Set and confirm a YouTube clock alignment first")
        clip_in = float(self.report.clip_in or 0.0)
        clip_out = float(
            self.report.clip_out
            if self.report.clip_out is not None and self.report.clip_out > clip_in
            else max(clip_in + 1.0, self.report.duration_seconds)
        )
        cur_src = self.player.source_time_seconds if self.player.reader is not None else clip_in
        mapped = alignment.youtube_timestamp_at(
            cur_src,
            source_sha256=self.report.source_sha256,
            youtube_video_id=yt_link.video_id,
            clip_in=clip_in,
            clip_out=clip_out,
        )
        if mapped is None:
            raise ValueError(
                "Current playback position has no valid YouTube timestamp alignment"
            )
        url = build_youtube_open_url(yt_link, timestamp_seconds=mapped)
        self.last_opened_youtube_url = url
        webbrowser.open(url)
        return url

    def relink_current_video_dialog(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            filetypes=[("Video files", "*.mp4 *.mov *.m4v *.avi *.mkv"), ("All files", "*")],
        )
        if path:
            self.relink_current_video(Path(path))

    def relink_current_video(self, new_path: Path | str):
        if self.report is None:
            raise ValueError("Open a report first")
        outcome = relink_report_video(self.report, new_path)
        self.load_report(self.report.path)
        self.status.set(outcome.message)
        return outcome

    def set_left_anchor_from_player(self) -> None:
        self.anchor_left_var.set(f"{self.player.clip_time_seconds:.2f}")

    def run_clip_comparison(self, other_report_path: Path | str | None = None) -> ClipComparisonResult:
        if self.report is None:
            raise ValueError("Load a primary report or reference clip first")
        target_str = str(other_report_path or self.compare_target_var.get()).strip()
        if not target_str:
            raise ValueError("Choose a second clip/report to compare with")
        resolved_target = (
            self.reference_paths.get(target_str)
            or self.report_paths.get(target_str)
            or Path(target_str).expanduser().resolve()
        )
        if resolved_target.suffix.lower() in (".tp", ".tpc"):
            temp_archive = PracticeArchive(resolved_target)
            try:
                if not temp_archive.manifest["segments"]:
                    raise ValueError("Selected archive has no recordings")
                last_seg = temp_archive.manifest["segments"][-1]
                other_record = read_report(temp_archive.report(int(last_seg["index"])))
                other_record.videos = {
                    label: temp_archive.folder / last_seg[key]
                    for label, key in (("Raw video", "video"), ("Overlay", "overlay"))
                    if last_seg[key] is not None
                }
            finally:
                # Keep temp_archive open only if needed; for standalone report_paths we read directly
                pass
        else:
            other_record = read_report(resolved_target)

        left_anchor = parse_time(self.anchor_left_var.get() or "0.0")
        right_anchor = parse_time(self.anchor_right_var.get() or "0.0")
        other_mov = load_report_movement_data(other_record)
        other_coach = (
            load_report_coach_data(other_record) if other_record.is_imported_reference else None
        )
        cmp_result = compare_clips(
            self.report,
            other_record,
            anchor_left_clip_time=left_anchor,
            anchor_right_clip_time=right_anchor,
            left_movement=self.movement_data,
            right_movement=other_mov,
        )
        self.last_comparison_result = cmp_result

        other_video = other_record.recorded_video
        if other_video is not None and other_video.is_file():
            left_in = float(self.report.clip_in or 0.0)
            right_in = float(other_record.clip_in or 0.0)

            def _other_transform(img: np.ndarray, f_idx: int, f_fps: float) -> np.ndarray:
                show_p = bool(self.show_pivots_var.get())
                show_s = bool(self.show_steps_var.get())
                out_img = img
                if (
                    (show_p or show_s)
                    and other_mov is not None
                    and img.shape[1] == other_mov.get("width")
                    and img.shape[0] == other_mov.get("height")
                ):
                    out_img = render_movement_overlay(
                        img,
                        other_mov,
                        f_idx,
                        f_fps,
                        show_pivots=show_p,
                        show_steps=show_s,
                        step_history_seconds=self._step_history_seconds(),
                        mirror=False,
                    )
                if self.show_coach_var.get() and other_coach is not None:
                    out_img = render_coach_frame_overlay(
                        out_img, other_coach, f_idx, self.config_provider()
                    )
                return out_img

            self.player.compare(
                [other_video],
                refresh=True,
                specs=[
                    {
                        "clip_range": other_record.clip_range,
                        "primary_anchor_source_seconds": left_in + cmp_result.anchor_left_clip_time,
                        "anchor_source_seconds": right_in + cmp_result.anchor_right_clip_time,
                        "frame_transform": _other_transform,
                    }
                ],
            )
            self.player.seek_clip_relative(cmp_result.anchor_left_clip_time)

        parts = list(cmp_result.summary_lines)
        if cmp_result.comparability_warnings:
            parts.append("Note: " + " ".join(cmp_result.comparability_warnings))
        summary_txt = " | ".join(parts) if parts else "Clips aligned at manual anchors."
        self.comparison_summary_var.set(summary_txt)
        self.status.set(summary_txt)
        return cmp_result

    def clear_clip_comparison(self) -> None:
        self.last_comparison_result = None
        self.comparison_summary_var.set("")
        self.update_comparison()

    def close(self):
        if not self.closed:
            self.closed = True
            self.cancel.set()
            self.after_cancel(self.timer)
            self.import_worker.close()
            self.movement_coordinator.close()
            self.player.close()
            self.close_posture_analytics()
            if self.archive is not None:
                self.archive.close()
