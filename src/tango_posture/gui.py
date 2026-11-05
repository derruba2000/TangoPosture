"""Native practice setup, settings editor and live controls (Tk stays on its main thread)."""

import json
import queue
import sqlite3
import threading
import time
import tkinter as tk
import tomllib
import webbrowser
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files as package_files
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import filedialog, messagebox, ttk

from .archive import practice_directory, practice_path, read_manifest
from .calibration import load_calibration_file
from .config import (
    SETTING_GROUPS,
    SETTING_MAP,
    Config,
    export_toml,
    set_setting,
    setting_value,
)
from .models import verify
from .practice import GuiDisplay, PracticeOptions, next_recordings
from .review_ui import MarkdownView, ReviewPanel
from .storage import Storage


class ScrollPage(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(canvas, padding=18)
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.body.columnconfigure(1, weight=1)


class PracticeGUI:
    def __init__(self, root: tk.Tk, cfg: Config):
        self.root = root
        root.title("TangoPosture  /  Practice studio")
        root.geometry("1060x860")
        root.minsize(840, 640)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.worker: threading.Thread | None = None
        self.display: GuiDisplay | None = None
        self.preview: tk.Toplevel | None = None
        self.help_window: tk.Toplevel | None = None
        self.about_window: tk.Toplevel | None = None
        self.splash_window: tk.Toplevel | None = None
        self.splash_timer: str | None = None
        self.help_view: MarkdownView | None = None
        self.help_outline: ttk.Treeview | None = None
        self.help_positions: dict[str, str] = {}
        self.exit_requested = False
        self.running = False
        self.saved_practice: Path | None = None
        self.calibration_run = False
        self.photo: tk.PhotoImage | None = None
        logo_resource = package_files("tango_posture").joinpath(
            "TangoPosture_logo_under_1MB.png"
        )
        try:
            logo_data = logo_resource.read_bytes()
        except FileNotFoundError:
            logo_data = (
                Path(__file__).resolve().parents[2]
                / "logos"
                / "TangoPosture_logo_under_1MB.png"
            ).read_bytes()
        logo = tk.PhotoImage(data=logo_data)
        logo_scale = max(1, (logo.width() + 63) // 64)
        self.brand_logo = logo.subsample(logo_scale, logo_scale)
        splash_scale = max(1, (logo.width() + 255) // 256)
        self.splash_logo = logo.subsample(splash_scale, splash_scale)
        root.iconphoto(True, self.brand_logo)
        self.status = tk.StringVar(value="Set up your practice, then open the live coach.")
        self.settings: dict[str, tk.Variable] = {}
        self.fields = {field: key for key, (field, _) in SETTING_MAP.items()}
        for key, (field, _) in SETTING_MAP.items():
            value = setting_value(cfg, field)
            self.settings[key] = (
                tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=value)
            )
        options = PracticeOptions(
            interval_end=f"{cfg.calibration_duration:g}", record_raw=True, record_overlay=True,
            practice_name=f"Practice_{time.strftime('%Y%m%d_%H%M%S')}",
        )
        options.raw_path, options.overlay_path = next_recordings()
        self.options: dict[str, tk.Variable] = {
            name: tk.BooleanVar(value=value)
            if isinstance(value, bool)
            else tk.StringVar(value=value)
            for name, value in asdict(options).items()
        }
        self.options["calibration"].trace_add(
            "write", lambda *_: self.options["confirm_camera"].set(False)
        )
        self.reference_ids: dict[str, str] = {}
        header = ttk.Frame(root, padding=(20, 16))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, image=self.brand_logo).pack(side="left", padx=(0, 14))
        heading = ttk.Frame(header)
        heading.pack(side="left", fill="x", expand=True)
        ttk.Label(heading, text="TangoPosture", font=("Helvetica", 23, "bold")).pack(anchor="w")
        ttk.Label(
            heading, text="Set up a reference, choose recordings, and start your practice."
        ).pack(anchor="w", pady=(4, 0))
        self.help_button = ttk.Button(header, text="Help", command=self.open_help)
        self.help_button.pack(side="right", padx=(12, 0))
        self.about_button = ttk.Button(header, text="About", command=self.open_about)
        self.about_button.pack(side="right", padx=(12, 0))
        self.tabs = ttk.Notebook(root)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=16)
        self.practice_page = self._page(self.tabs, "Practice")
        self.reference_page = self._page(self.tabs, "References")
        files = self._page(self.tabs, "Files & recording")
        self.setting_tabs = ttk.Notebook(self.tabs)
        self.tabs.add(self.setting_tabs, text="Settings")
        self.group_pages = {}
        for group, fields in SETTING_GROUPS.items():
            page = files if group == "Files" else self._page(self.setting_tabs, group)
            self.group_pages[group] = page
            for row, field in enumerate(fields):
                self._setting(page, row, field)
        self._practice(self.practice_page)
        self._references(self.reference_page)
        self._recording(files)
        self.review = ReviewPanel(self.tabs, self.settings, self.config)
        self.tabs.add(self.review, text="Review")
        self.review.guard(self.review.refresh)
        self._menu()
        footer = ttk.Frame(root, padding=16)
        footer.grid(row=2, column=0, sticky="ew")
        self.start_button = ttk.Button(
            footer, text="Open practice coach", command=lambda: self._guard(self.start)
        )
        self.start_button.pack(side="right", padx=(12, 0))
        ttk.Label(footer, textvariable=self.status, wraplength=650).pack(
            side="left", fill="x", expand=True
        )
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._guard(self.refresh_references)
        self.timer = root.after(40, self.pump)

    def _page(self, notebook, title):
        page = ScrollPage(notebook)
        notebook.add(page, text=title)
        return page.body

    def _entry(self, page, row, label, variable, browse: str | None = None):
        ttk.Label(page, text=label, wraplength=350).grid(
            row=row, column=0, sticky="w", padx=(0, 20), pady=10
        )
        ttk.Entry(page, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=10)
        if browse:
            ttk.Button(page, text="Browse...", command=lambda: self.browse(variable, browse)).grid(
                row=row, column=2, padx=(10, 0)
            )

    def _setting(self, page, row, field):
        key = self.fields[field]
        variable = self.settings[key]
        label = field.replace(".", "  /  ").replace("_", " ").capitalize()
        text = f"{label}\n{key}"
        if isinstance(variable, tk.BooleanVar):
            ttk.Label(page, text=text).grid(row=row, column=0, sticky="w", pady=10, padx=(0, 20))
            ttk.Checkbutton(page, variable=variable, text="Enabled").grid(
                row=row, column=1, sticky="w"
            )
        elif field in ("side", "facing"):
            ttk.Label(page, text=text).grid(row=row, column=0, sticky="w", pady=10)
            ttk.Combobox(
                page, textvariable=variable, values=("left", "right"), state="readonly"
            ).grid(row=row, column=1, sticky="ew")
        else:
            browse = (
                "directory" if field == "data_dir" else "model" if field == "model_path" else None
            )
            self._entry(page, row, text, variable, browse)

    def _practice(self, page):
        ttk.Label(page, text="Choose your practice source", font=("Helvetica", 15, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        ttk.Radiobutton(
            page, text="Live camera", variable=self.options["source"], value="camera"
        ).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(
            page, text="Recorded video", variable=self.options["source"], value="video"
        ).grid(row=1, column=1, sticky="w")
        self._entry(page, 2, "Input video", self.options["video"], "video")
        ttk.Label(
            page,
            text="Select camera, resolution, side, facing and mirroring in Settings > Camera & view.\nUse a fixed, level camera and keep your whole body in frame. Facing refers to the original, unmirrored image.",
            wraplength=800,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=15)
        ttk.Checkbutton(
            page,
            text="Analyze video without a live preview (requires a saved reference or interval)",
            variable=self.options["headless"],
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(page, text="Practice log", font=("Helvetica", 13, "bold")).grid(
            row=5, column=0, sticky="w", pady=(10, 8)
        )
        self.log = tk.Text(page, height=13, wrap="word", state="disabled", font=("TkFixedFont", 10))
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        ttk.Label(
            page,
            text="Experimental movement observation. Feedback is relative to your chosen reference.",
            wraplength=800,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=12)

    def _references(self, page):
        for row, (value, label) in enumerate(
            (
                ("new", "Create a new reference in the calibration camera"),
                ("saved", "Reuse a saved reference"),
                ("interval", "Use a stable interval from the input video"),
            )
        ):
            ttk.Radiobutton(
                page, text=label, variable=self.options["reference_mode"], value=value
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(page, text="Saved reference (.tpc, legacy JSON, latest, or UUID)").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(15, 6)
        )
        self.reference_combo = ttk.Combobox(page, textvariable=self.options["calibration"])
        self.reference_combo.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.reference_combo.bind(
            "<<ComboboxSelected>>", lambda _: self._guard(self.match_reference)
        )
        ttk.Button(
            page,
            text="Browse reference...",
            command=lambda: self.browse(self.options["calibration"], "reference"),
        ).grid(row=4, column=2, padx=10)
        actions = ttk.Frame(page)
        actions.grid(row=5, column=0, columnspan=3, sticky="w", pady=12)
        self.calibration_button = ttk.Button(
            actions, text="Open calibration camera",
            command=lambda: self._guard(partial(self.start, calibration_only=True)),
        )
        self.calibration_button.pack(side="left", padx=(0, 10))
        ttk.Button(
            actions, text="Refresh references", command=lambda: self._guard(self.refresh_references)
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Match reference camera settings",
            command=lambda: self._guard(self.match_reference),
        ).pack(side="left", padx=10)
        ttk.Checkbutton(
            page,
            text="I confirm the physical camera placement is unchanged",
            variable=self.options["confirm_camera"],
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=8)
        self._entry(page, 7, "Video reference start (seconds)", self.options["interval_start"])
        self._entry(page, 8, "Video reference end (seconds)", self.options["interval_end"])
        self._entry(page, 9, "Name for a new reference", self.options["reference_name"])
        self._entry(page, 10, "Teacher review note (optional)", self.options["teacher_note"])
        ttk.Label(
            page,
            text="The interval length must equal Calibration duration in Settings. Recalibrate after moving the camera or changing your capture setup.",
            wraplength=800,
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=12)

    def _recording(self, page):
        ttk.Separator(page).grid(row=2, column=0, columnspan=3, sticky="ew", pady=12)
        self._entry(page, 3, "Practice name", self.options["practice_name"])
        ttk.Button(page, text="New practice name", command=self.suggest_names).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=12
        )
        ttk.Label(
            page,
            text="Practice archives (.tp) are saved in practice/ under the selected data directory. Existing practice names append new recordings.",
            wraplength=800,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=12)

    def _menu(self):
        menu = tk.Menu(self.root)
        files = tk.Menu(menu, tearoff=False)
        for label, action in (
            ("Load practice preset...", self.load_preset),
            ("Save practice preset...", self.save_preset),
            ("Import settings TOML...", self.import_toml),
            ("Export settings TOML...", self.save_toml),
        ):
            files.add_command(label=label, command=partial(self._guard, action))
        files.add_separator()
        files.add_command(label="Quit", command=self.close)
        menu.add_cascade(label="File", menu=files)
        settings = tk.Menu(menu, tearoff=False)
        for group in SETTING_GROUPS:
            settings.add_command(label=group, command=partial(self.select_group, group))
        settings.add_separator()
        settings.add_command(
            label="Import settings TOML...", command=lambda: self._guard(self.import_toml)
        )
        settings.add_command(
            label="Export settings TOML...", command=lambda: self._guard(self.save_toml)
        )
        settings.add_command(
            label="Reset settings to defaults", command=lambda: self.apply_config(Config())
        )
        menu.add_cascade(label="Settings", menu=settings)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Search & browse Tango Posture Manual", command=self.open_help)
        help_menu.add_separator()
        help_menu.add_command(label="About TangoPosture", command=self.open_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menu)

    def show_splash(self):
        if self.splash_window is not None:
            return
        self.root.withdraw()
        window = self.splash_window = tk.Toplevel(self.root)
        window.title("TangoPosture")
        window.overrideredirect(True)
        window.configure(cursor="watch")
        width, height = 440, 380
        left = max(0, (window.winfo_screenwidth() - width) // 2)
        top = max(0, (window.winfo_screenheight() - height) // 2)
        window.geometry(f"{width}x{height}+{left}+{top}")
        content = ttk.Frame(window, padding=24)
        content.pack(fill="both", expand=True)
        ttk.Label(content, image=self.splash_logo).pack(expand=True)
        ttk.Label(content, text="TangoPosture", font=("Helvetica", 24, "bold")).pack(pady=(12, 0))
        self.splash_progress = ttk.Progressbar(content, mode="indeterminate", length=200)
        self.splash_progress.pack(pady=(8, 0))
        self.splash_progress.start(15)
        window.lift()
        self.splash_timer = self.root.after(3000, self.finish_splash)

    def finish_splash(self):
        self.splash_timer = None
        if self.splash_window is not None:
            self.splash_progress.stop()
            self.splash_window.destroy()
            self.splash_window = None
        self.root.deiconify()
        self.root.lift()

    def open_about(self):
        if self.about_window is not None and self.about_window.winfo_exists():
            self.show_about_window(self.about_window)
            return
        try:
            app_version = version("tango-posture")
        except PackageNotFoundError:
            app_version = "development"
        window = self.about_window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("About TangoPosture")
        window.transient(self.root)
        window.resizable(False, False)
        window.iconphoto(True, self.brand_logo)
        content = ttk.Frame(window, padding=24)
        content.pack(fill="both", expand=True)
        ttk.Label(content, image=self.brand_logo).pack(pady=(0, 12))
        ttk.Label(content, text="TangoPosture", font=("Helvetica", 20, "bold")).pack()
        ttk.Label(content, text=f"Version {app_version}").pack(pady=(6, 0))
        ttk.Label(content, text="License: Apache-2.0").pack(pady=(6, 12))
        self.about_repo_link = ttk.Label(
            content, text="GitHub repository", foreground="#146c9c", cursor="hand2", takefocus=True,
        )
        self.about_repo_link.pack(pady=(0, 12))
        for event in ("<Button-1>", "<Return>", "<space>"):
            self.about_repo_link.bind(event, lambda _: webbrowser.open("https://github.com/derruba2000/TangoPosture"))
        ttk.Label(
            content, text="Experimental movement observation, not a medical assessment.",
            wraplength=350, justify="center",
        ).pack()
        ttk.Button(content, text="Close", command=window.withdraw).pack(pady=(18, 0))
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.bind("<Escape>", lambda _: window.withdraw())
        self.show_about_window(window)

    def show_about_window(self, window: tk.Toplevel):
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        left = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        top = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        left = max(0, min(left, window.winfo_screenwidth() - width))
        top = max(0, min(top, window.winfo_screenheight() - height))
        window.geometry(f"{width}x{height}+{left}+{top}")
        window.deiconify()
        window.lift()
        window.focus_set()

    def open_help(self):
        if self.help_window and self.help_window.winfo_exists():
            self.help_window.deiconify()
            self.help_window.lift()
            self.help_window.focus_set()
            return

        manual = package_files("tango_posture").joinpath("TANGO_POSTURE_MANUAL.md")
        try:
            content = manual.read_text(encoding="utf-8")
        except FileNotFoundError:
            path = Path(__file__).resolve().parents[2] / "docs" / "TANGO_POSTURE_GUIDE.md"
            content = path.read_text(encoding="utf-8")

        window = self.help_window = tk.Toplevel(self.root)
        window.title("Tango Posture Manual")
        window.geometry("1180x820")
        window.minsize(840, 600)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        panes = ttk.Panedwindow(window, orient="horizontal")
        panes.grid(row=0, column=0, sticky="nsew")
        navigation = ttk.Frame(panes, padding=(8, 8, 4, 8))
        navigation.rowconfigure(0, weight=1)
        outline = self.help_outline = ttk.Treeview(
            navigation, show="tree", selectmode="browse"
        )
        outline.grid(row=0, column=0, sticky="nsew")
        outline_scroll = ttk.Scrollbar(navigation, orient="vertical", command=outline.yview)
        outline_scroll.grid(row=0, column=1, sticky="ns")
        outline.configure(yscrollcommand=outline_scroll.set)
        reader = self.help_view = MarkdownView(panes, lambda _: None)
        panes.add(navigation, weight=0)
        panes.add(reader, weight=1)
        reader.set(content)

        parents: dict[int, str] = {}
        self.help_positions = {}
        for index, (level, title, position) in enumerate(reader.headings):
            parents = {depth: node for depth, node in parents.items() if depth < level}
            parent = parents.get(level - 1, "")
            node = f"section-{index}"
            outline.insert(parent, "end", iid=node, text=title)
            parents[level] = node
            self.help_positions[node] = position

        def navigate(_event=None):
            selected = outline.selection()
            if selected:
                position = self.help_positions[selected[0]]
                reader.text.see(position)
                reader.text.mark_set("insert", position)

        outline.bind("<<TreeviewSelect>>", navigate)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)

    def _guard(self, action: Callable):
        try:
            action()
        except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, tk.TclError) as error:
            messagebox.showerror("Check practice settings", str(error), parent=self.root)

    def config(self) -> Config:
        values: dict = {}
        for key, variable in self.settings.items():
            set_setting(values, key, str(variable.get()))
        return Config.model_validate(values)

    def practice_options(self) -> PracticeOptions:
        values = {key: variable.get() for key, variable in self.options.items()}
        values["calibration"] = self.reference_ids.get(values["calibration"], values["calibration"])
        return PracticeOptions(**values)

    def apply_config(self, cfg: Config):
        for key, (field, _) in SETTING_MAP.items():
            self.settings[key].set(setting_value(cfg, field))

    def select_group(self, group):
        page = self.group_pages[group]
        if group == "Files":
            self.tabs.select(page.master.master)
        else:
            self.tabs.select(self.setting_tabs)
            self.setting_tabs.select(page.master.master)

    def browse(self, variable, kind):
        if kind == "directory":
            path = filedialog.askdirectory(parent=self.root)
        elif kind == "recording":
            path = filedialog.asksaveasfilename(
                parent=self.root,
                defaultextension=".mp4",
                filetypes=[("MP4 video", "*.mp4"), ("AVI video", "*.avi")],
            )
        else:
            types = {
                "video": [("Video", "*.mp4 *.mov *.avi *.mkv")],
                "model": [("Pose model", "*.task")],
                "reference": [("Calibration archive", "*.tpc"), ("Legacy reference JSON", "*.json")],
            }
            path = filedialog.askopenfilename(
                parent=self.root, filetypes=[*types[kind], ("All files", "*")]
            )
        if path:
            variable.set(path)

    def suggest_names(self):
        raw, overlay = next_recordings()
        self.options["raw_path"].set(raw)
        self.options["overlay_path"].set(overlay)
        self.options["practice_name"].set(f"Practice_{time.strftime('%Y%m%d_%H%M%S')}")

    def refresh_references(self):
        raw_data_dir = str(self.settings[self.fields["data_dir"]].get())
        data_dir = Path(raw_data_dir)
        folder = Path("calibrations") if raw_data_dir == "data" else data_dir / "calibrations"
        refs = {}
        if (data_dir / "tango.sqlite3").is_file():
            storage = Storage(data_dir)
            try:
                for row in storage.list("calibrations"):
                    refs[row["id"]] = (row["name"], row["id"])
            finally:
                storage.close()
        for path in sorted([*folder.glob("calibration_*.json"), *folder.glob("*.tpc")], reverse=True):
            try:
                ref = load_calibration_file(str(path), folder)
                if ref:
                    refs[ref.id] = (ref.name, str(path.resolve()))
            except (ValueError, OSError, TypeError) as error:
                self.append_log(f"Could not read reference {path.name}: {error}\n")
        self.reference_ids = {
            f"{name}  /  {identifier[:8]}": target for identifier, (name, target) in refs.items()
        }
        self.reference_combo.configure(values=["latest", *self.reference_ids])

    def match_reference(self):
        cfg, options = self.config(), self.practice_options()
        folder = (
            Path("calibrations") if cfg.data_dir == "data" else Path(cfg.data_dir) / "calibrations"
        )
        ref = load_calibration_file(options.calibration, folder)
        if ref is None:
            storage = Storage(Path(cfg.data_dir))
            try:
                ref = storage.calibration(options.calibration)
            finally:
                storage.close()
        for field in ("side", "facing", "width", "height", "capture_profile"):
            self.settings[self.fields[field]].set(ref.profile[field])
        source = ref.profile["source"]
        self.options["source"].set("camera" if source.startswith("camera:") else "video")
        if source.startswith("camera:"):
            self.settings[self.fields["camera"]].set(source.split(":", 1)[1])
        self.options["reference_mode"].set("saved")
        self.options["confirm_camera"].set(False)
        self.status.set(
            "Reference settings loaded. Confirm physical camera placement before reuse."
        )

    def save_preset(self):
        payload = {
            "version": 1,
            "config": self.config().model_dump(exclude={"axis_curve"}),
            "practice": asdict(self.practice_options()),
        }
        self._save(json.dumps(payload, indent=2) + "\n", ".json")

    def load_preset(self):
        path = filedialog.askopenfilename(
            parent=self.root, filetypes=[("Practice preset", "*.json")]
        )
        if not path:
            return
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Unsupported practice preset version")
        cfg = Config.model_validate(payload["config"])
        options = PracticeOptions(**payload["practice"])
        # A saved checkbox is never evidence of today's physical camera placement.
        options.confirm_camera = False
        self.apply_config(cfg)
        for key, value in asdict(options).items():
            self.options[key].set(value)
        self.refresh_references()
        self.status.set("Practice preset loaded. Settings are applied when the coach opens.")

    def import_toml(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("TOML settings", "*.toml")])
        if path:
            self.apply_config(Config.model_validate(tomllib.loads(Path(path).read_text())))

    def _save(self, content, extension):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=extension)
        if path:
            Path(path).write_text(content, encoding="utf-8")
            self.status.set(f"Saved {Path(path).name}")

    def save_toml(self):
        self._save(export_toml(self.config()), ".toml")

    def start(self, calibration_only: bool = False):
        if self.running:
            return
        cfg, options = self.config(), self.practice_options()
        if calibration_only:
            options = replace(
                options, source="camera", video="", headless=False, reference_mode="new",
            )
        options.arguments(cfg, Path("settings.toml"))  # Validate before opening a camera.
        verify(Path(cfg.model_path))
        archive = practice_path(practice_directory(cfg.data_dir), options.practice_name)
        if archive.exists():
            read_manifest(archive)
        self.review.player.pause()
        self.display = GuiDisplay()
        self.saved_practice = None
        self.calibration_run = calibration_only
        if not options.headless:
            self._preview(calibration_only)
        self.start_button.configure(state="disabled")
        self.calibration_button.configure(state="disabled")
        if not options.headless and not calibration_only:
            self.live_record_button.configure(state="normal")
        self.status.set(
            "Calibration camera open. Press C to capture the reference."
            if calibration_only else
            "Coach open. Use the live controls; setting edits apply to your next run."
        )
        display = self.display
        self.running = True

        def run():
            from .cli import main

            events = self.events

            class Log:
                def write(self, text):
                    events.put(("log", text))
                    return len(text)

                def flush(self):
                    pass

            result = 2
            try:
                with TemporaryDirectory(prefix="tango-practice-") as directory:
                    path = Path(directory) / "settings.toml"
                    path.write_text(export_toml(cfg))
                    with redirect_stdout(Log()), redirect_stderr(Log()):
                        arguments = options.arguments(cfg, path)
                        if calibration_only:
                            arguments.append("--calibration-only")
                        result = main(arguments, display=display)
            except Exception as error:  # noqa: BLE001 - worker boundary: report and restore controls
                events.put(("log", f"Practice error: {error}\n"))
            finally:
                display.close()
                events.put(("done", result))

        self.worker = threading.Thread(target=run, name="practice-coach")
        self.worker.start()

    def start_recording(self):
        if (
            self.running and self.display and not self.display.stopped.is_set()
            and self.preview is not None
            and not self.calibration_run
            and not self.live_record_button.instate(["disabled"])
        ):
            self.display.request("r")
            self.live_record_button.configure(state="disabled")
            self.status.set("Recording video. Stop / save finishes and saves the recordings.")

    def _preview(self, calibration_only: bool = False):
        self.preview = tk.Toplevel(self.root)
        self.preview.title("TangoPosture  /  Calibration camera" if calibration_only else "TangoPosture  /  Live coach")
        self.preview.iconphoto(True, self.brand_logo)
        self.preview.geometry("1100x850")
        self.preview.minsize(760, 560)
        controls = ttk.Frame(self.preview, padding=8)
        controls.pack(fill="x")
        actions = (
            (("c", "Calibrate / retry"), ("v", "Skeleton"), ("y", "Torso axis"), ("q", "Close / save"))
            if calibration_only else (
                ("c", "Calibrate"),
                ("a", "Accept"),
                ("r", "Record"),
                ("s", "Start scoring"),
                ("p", "Pause / resume"),
                ("e", "Stop recording / save"),
                ("b", "Beat 1"),
                ("v", "Skeleton / confirm view"),
                ("y", "Torso axis"),
                ("q", "Quit / save"),
                ("x", "Abort / exit"),
            )
        )
        for i, (key, label) in enumerate(actions):
            button = ttk.Button(
                controls, text=f"{label} ({key.upper()})", command=partial(self.control, key)
            )
            button.grid(row=i // 5, column=i % 5, padx=3, pady=3, sticky="ew")
            if key == "r":
                self.live_record_button = button
        for i in range(5):
            controls.columnconfigure(i, weight=1)
        if not calibration_only:
            self.auto_song_recording = tk.BooleanVar(value=False)
            self.auto_record_checkbox = ttk.Checkbutton(
                controls, text="Automatically record songs", variable=self.auto_song_recording,
                command=self.toggle_auto_recording,
            )
            self.auto_record_checkbox.grid(row=2, column=1, columnspan=4, padx=3, pady=3, sticky="w")
            if not self.config().beat_tracking or self.options["source"].get() != "camera":
                self.auto_record_checkbox.configure(state="disabled")
            menu = tk.Menu(self.preview)
            actions_menu = tk.Menu(menu, tearoff=False)
            actions_menu.add_command(label="Quit / save", command=lambda: self.control("q"))
            actions_menu.add_command(label="Abort without saving", command=lambda: self.control("x"))
            menu.add_cascade(label="Practice", menu=actions_menu)
            parameters_menu = tk.Menu(menu, tearoff=False)
            parameters_menu.add_command(label="Music recording parameters...", command=self.open_music_parameters)
            menu.add_cascade(label="Parameters", menu=parameters_menu)
            self.preview.configure(menu=menu)
        self.preview_label = tk.Label(
            self.preview,
            background="#151a1d",
            foreground="white",
            text="Opening model and capture...",
        )
        self.preview_label.pack(fill="both", expand=True)
        self.preview.protocol("WM_DELETE_WINDOW", lambda: self.control("q"))
        self.preview.bind(
            "<KeyPress>",
            lambda event: (
                self.control(event.char.lower())
                if event.char and event.char.lower() in ("cvyq" if calibration_only else "carspebvyqx")
                else None
            ),
        )
        self.preview.focus_set()

    def open_music_parameters(self):
        self.select_group("Music")
        self.root.lift()
        self.root.focus_set()

    def toggle_auto_recording(self):
        if self.display is not None:
            if self.auto_song_recording.get():
                self.display.auto_recording.set()
            else:
                self.display.auto_recording.clear()

    def control(self, key):
        if self.display:
            if key == "x" and not messagebox.askyesno(
                "Abort practice?",
                "Discard the current unsaved recording and close the coach? Previously saved segments will remain.",
                parent=self.preview or self.root,
            ):
                return
            if key == "r":
                self.start_recording()
                return
            self.display.request(key)
            if key in ("e", "q", "x"):
                state = "normal" if key == "e" else "disabled"
                if self.preview is not None and not self.calibration_run:
                    self.live_record_button.configure(state=state)
                self.status.set(
                    "Aborting practice without saving..." if key == "x"
                    else "Saving recordings and practice outputs..."
                )

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        if self.display is not None:
            recording_state = None
            while True:
                try:
                    recording_state = self.display.recording_states.get_nowait()
                except queue.Empty:
                    break
            if recording_state is not None and self.preview is not None and not self.calibration_run:
                self.live_record_button.configure(
                    state="normal" if recording_state == "ready" else "disabled"
                )
            latest_practice = None
            while True:
                try:
                    latest_practice = self.display.reports.get_nowait()
                except queue.Empty:
                    break
            if latest_practice:
                if latest_practice.suffix.lower() in (".tp", ".tpc"):
                    self.saved_practice = latest_practice
                self.review.guard(partial(self.review.add_report, latest_practice))
                self.tabs.select(self.review)
        if self.display and self.preview:
            try:
                image = self.display.frames.get_nowait()
                import cv2

                height, width = image.shape[:2]
                scale = min(
                    max(1, self.preview_label.winfo_width()) / width,
                    max(1, self.preview_label.winfo_height()) / height,
                    1.0,
                )
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                rgb = cv2.resize(rgb, (max(1, round(width * scale)), max(1, round(height * scale))))
                h, w = rgb.shape[:2]
                self.photo = tk.PhotoImage(
                    data=f"P6\n{w} {h}\n255\n".encode() + rgb.tobytes(), format="PPM"
                )
                self.preview_label.configure(image=self.photo, text="")
            except queue.Empty:
                pass
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
            elif kind == "done":
                self.running = False
                was_aborted = self.display is not None and self.display.aborted.is_set()
                if self.display:
                    latest_report = None
                    while True:
                        try:
                            latest_report = self.display.reports.get_nowait()
                        except queue.Empty:
                            break
                    if latest_report:
                        if latest_report.suffix.lower() in (".tp", ".tpc"):
                            self.saved_practice = latest_report
                        self.review.guard(partial(self.review.add_report, latest_report))
                        self.tabs.select(self.review)
                if self.preview:
                    self.preview.destroy()
                    self.preview = None
                self.display = None
                self.start_button.configure(state="normal")
                self.calibration_button.configure(state="normal")
                self.status.set(
                    "Practice aborted. The unsaved recording was discarded." if was_aborted else
                    "Practice finished. See the practice log for saved outputs."
                    if value == 0
                    else "Practice could not complete. See the practice log for the error."
                )
                self._guard(self.refresh_references)
                if self.calibration_run and value == 0 and self.saved_practice is not None:
                    self.options["calibration"].set(str(self.saved_practice))
                    self.options["reference_mode"].set("saved")
                    self.options["headless"].set(False)
                    self._guard(self.match_reference)
                if not was_aborted and value == 0 and self.saved_practice is not None and self.saved_practice.is_file():
                    title = "Calibration saved" if self.calibration_run else "Practice saved"
                    messagebox.showinfo(
                        title,
                        f"{title}: {self.saved_practice.name}\n\nSaved to: {self.saved_practice}",
                        parent=self.root,
                    )
                if self.exit_requested:
                    self.review.close()
                    self.root.destroy()
                    return
        self.timer = self.root.after(40, self.pump)

    def close(self):
        if self.running:
            self.exit_requested = True
            self.control("q")
        else:
            self.root.after_cancel(self.timer)
            if self.splash_timer is not None:
                self.root.after_cancel(self.splash_timer)
                self.splash_timer = None
            self.review.close()
            self.root.destroy()


def launch(cfg: Config) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise RuntimeError(
            "The desktop GUI requires a graphical display and Tk. Use the run subcommand for CLI/headless practice."
        ) from error
    root.withdraw()
    gui = PracticeGUI(root, cfg)
    gui.show_splash()
    root.mainloop()
    return 0
