"""In-app reports, local video replay and asynchronous Ollama review."""

import queue
import re
import threading
import tkinter as tk
from collections.abc import Callable
from functools import partial
from http.client import HTTPException
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont
from uuid import uuid4

from .config import Config
from .ollama import OllamaClient
from .replay import ReplayPlayer, parse_time
from .review import ReportRecord, read_report, report_directory, save_analysis, saved_analyses


class MarkdownView(ttk.Frame):
    def __init__(self, parent, on_time: Callable[[float], None]):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.on_time = on_time
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
            ("table", 10, False),
        ):
            font = tkfont.nametofont("TkFixedFont" if name == "table" else "TkDefaultFont").copy()
            font.configure(size=size, weight="bold" if bold else "normal")
            self.fonts[name] = font
        self.text = tk.Text(
            body, wrap="word", state="disabled", padx=12, pady=10, font=self.fonts["body"]
        )
        vertical = ttk.Scrollbar(body, command=self.text.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.text.tag_configure("h1", font=self.fonts["h1"], spacing1=10, spacing3=8)
        self.text.tag_configure("h2", font=self.fonts["h2"], spacing1=9, spacing3=6)
        self.text.tag_configure("h3", font=self.fonts["h3"], spacing1=8, spacing3=4)
        self.text.tag_configure("bold", font=self.fonts["bold"])
        self.text.tag_configure("table", font=self.fonts["table"])
        self.text.tag_configure("found", background="#ffe89c", foreground="black")

    def set(self, markdown: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for tag in self.text.tag_names():
            if tag.startswith("time-"):
                self.text.tag_delete(tag)
        for line in markdown.splitlines():
            heading = re.match(r"^(#{1,3})\s+(.*)", line)
            tag = f"h{len(heading[1])}" if heading else "table" if line.startswith("|") else ""
            line = heading[2] if heading else line
            line = re.sub(r"\[([^\]]+)\]\([^\n]*?\)", r"\1", line).replace("`", "")
            start = self.text.index("end-1c")
            for i, piece in enumerate(re.split(r"\*\*(.*?)\*\*", line)):
                self.text.insert("end", piece, (tag, "bold") if i % 2 else (tag,))
            self.text.insert("end", "\n", (tag,))
            displayed = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
            for match in re.finditer(r"(?<![\w:+-])\d+:\d{2}(?::\d{2})?(?:\.\d+)?\b", displayed):
                key = f"time-{start}-{match.start()}"
                self.text.tag_add(key, f"{start}+{match.start()}c", f"{start}+{match.end()}c")
                self.text.tag_configure(key, foreground="#176caa", underline=True)
                self.text.tag_bind(key, "<Button-1>", partial(self._time, parse_time(match[0])))
        self.text.configure(state="disabled")

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
        self.closed = False
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.job = ""
        self.models_job = ""
        self.cancel = threading.Event()
        self.report_paths: dict[str, Path] = {}
        self.analyses: dict[str, dict] = {}
        self.analysis_data: dict | None = None
        self.selection = tk.StringVar()
        self.video_selection = tk.StringVar()
        self.saved_selection = tk.StringVar()
        self.status = tk.StringVar(value="Choose a saved report to review your practice.")
        self.analysis_status = tk.StringVar(
            value="Analysis runs only when you click Analyze report."
        )
        self.analysis_meta = tk.StringVar()
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 8))
        self.report_combo = ttk.Combobox(top, textvariable=self.selection, state="readonly")
        self.report_combo.pack(side="left", fill="x", expand=True)
        self.report_combo.bind(
            "<<ComboboxSelected>>",
            lambda _: self.guard(lambda: self.load_report(self.report_paths[self.selection.get()])),
        )
        ttk.Button(top, text="Refresh", command=lambda: self.guard(self.refresh)).pack(
            side="left", padx=5
        )
        ttk.Button(top, text="Open report...", command=lambda: self.guard(self.open_report)).pack(
            side="left"
        )
        ttk.Label(self, textvariable=self.status, wraplength=920).pack(anchor="w", pady=(0, 6))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.report_view = MarkdownView(self.tabs, self.seek_report_time)
        self.tabs.add(self.report_view, text="Practice report")
        videos = ttk.Frame(self.tabs)
        self.tabs.add(videos, text="Video replay")
        self.video_page = videos
        chooser = ttk.Frame(videos, padding=6)
        chooser.pack(fill="x")
        self.video_combo = ttk.Combobox(
            chooser, textvariable=self.video_selection, state="readonly", width=20
        )
        self.video_combo.pack(side="left")
        self.video_combo.bind("<<ComboboxSelected>>", lambda _: self.guard(self.switch_video))
        ttk.Button(chooser, text="Open video...", command=lambda: self.guard(self.open_video)).pack(
            side="left", padx=6
        )
        self.player = ReplayPlayer(videos, settings["tp_REPLAY_AUDIO"])
        self.player.pack(fill="both", expand=True)
        self._analysis_page()
        self.timer = self.after(50, self.pump)

    def _analysis_page(self):
        page = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(page, text="Ollama analysis")
        self.analysis_page = page
        form = ttk.Frame(page)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Ollama server").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(form, textvariable=self.settings["tp_OLLAMA_BASE_URL"]).grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Label(form, text="Model").grid(row=1, column=0, sticky="w")
        self.model_combo = ttk.Combobox(form, textvariable=self.settings["tp_OLLAMA_MODEL"])
        self.model_combo.grid(row=1, column=1, sticky="ew", pady=3)
        self.models_button = ttk.Button(
            form, text="List installed models", command=lambda: self.guard(self.list_models)
        )
        self.models_button.grid(row=1, column=2, padx=6)
        ttk.Label(form, text="Analysis focus").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.settings["tp_OLLAMA_INSTRUCTION"]).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=3
        )
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=6)
        self.analyze_button = ttk.Button(
            actions, text="Analyze report", command=lambda: self.guard(self.analyze)
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
        ttk.Label(page, textvariable=self.analysis_status, wraplength=900).pack(anchor="w", pady=5)
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

    def refresh(self):
        folder = report_directory(self.config_provider().data_dir)
        paths = sorted(folder.glob("report_*.md"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        if not paths and (folder / "latest_report.md").exists():
            paths = [folder / "latest_report.md"]
        self.report_paths = {str(path.resolve()): path.resolve() for path in paths}
        self.report_combo.configure(values=list(self.report_paths))
        if self.report is None and paths:
            self.load_report(paths[0])

    def open_report(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("Practice report", "*.md")])
        if path:
            self.load_report(Path(path))

    def add_report(self, path: Path):
        if self.job:
            self.report_paths[str(path.resolve())] = path.resolve()
            self.report_combo.configure(values=list(self.report_paths))
            self.status.set(
                "New practice report ready in the selector. The current report analysis is still running."
            )
        else:
            self.load_report(path)

    def load_report(self, path: Path):
        record = read_report(path)
        self.cancel_analysis()
        self.report = record
        self.selection.set(str(record.path))
        self.report_paths[str(record.path)] = record.path
        self.report_combo.configure(values=list(self.report_paths))
        self.report_view.set(record.markdown)
        self.player.unload()
        self.video_combo.configure(values=list(record.videos))
        self.video_selection.set("")
        self.status.set(str(record.path) + ("\n" + record.warning if record.warning else ""))
        for label in ("Overlay", "Raw video", "Source video"):
            if label in record.videos and record.videos[label].is_file():
                self.video_selection.set(label)
                self.guard(self.switch_video)
                break
        self.analysis_data = None
        self.analysis_meta.set("")
        self.analysis_view.set("")
        self.analysis_status.set("Analyze this report, or select one of its saved analyses.")
        self.guard(self.refresh_analyses)
        self.tabs.select(self.report_view)

    def switch_video(self):
        if not self.report:
            raise ValueError("Open a report first, or use Open video")
        seconds = self.player.position.get()
        self.player.load(self.report.videos[self.video_selection.get()], seconds)

    def open_video(self):
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("Video", "*.mp4 *.mov *.avi *.mkv"), ("All files", "*")]
        )
        if path:
            self.player.load(Path(path))
            self.video_selection.set("Opened video")
            self.tabs.select(self.video_page)

    def seek_report_time(self, seconds):
        def seek():
            if not self.player.reader:
                raise ValueError(
                    "This report has no available video. Open its recording with Open video."
                )
            if self.report and self.video_selection.get() in self.report.videos:
                seconds_in_file = self.report.recording_seconds(seconds, self.player.reader.fps)
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
        self.analysis_view.set("")
        self.analysis_meta.set(f"AI analysis / {cfg.ollama_model} / {client.url}")
        self.analysis_status.set("Waiting for Ollama; model loading can take a moment...")
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

    def cancel_analysis(self):
        self.cancel.set()
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
                self.models_button.configure(state="disabled" if self.job else "normal")
                if kind == "models":
                    self.model_combo.configure(values=value)
                    if not self.job:
                        self.analysis_status.set(
                            f"{len(value)} installed model(s). Choose a model or type its name."
                        )
                elif not self.job:
                    self.analysis_status.set(value)
            elif token == self.job:
                if kind == "chunk":
                    self.analysis_status.set("Receiving analysis...")
                    self.analysis_view.append(value)
                elif kind in ("analysis_done", "analysis_error"):
                    self.job = ""
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

    def close(self):
        if not self.closed:
            self.closed = True
            self.cancel.set()
            self.after_cancel(self.timer)
            self.player.close()
