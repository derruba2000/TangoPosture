"""Consistent, scrollable observation notes for live and recorded video."""

import re
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


class NotesSidebar(ttk.Frame):
    def __init__(self, parent, title="Frame insights", subtitle="Observations for the current frame"):
        super().__init__(parent, width=310, padding=(16, 16, 12, 12))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.fonts = []
        for size, weight in ((15, "bold"), (10, "normal"), (10, "bold"), (12, "normal")):
            font = tkfont.nametofont("TkDefaultFont").copy()
            font.configure(size=size, weight=weight)
            self.fonts.append(font)
        ttk.Label(self, text=title, font=self.fonts[0]).grid(row=0, column=0, sticky="w")
        self.subtitle = ttk.Label(self, text=subtitle, font=self.fonts[1], wraplength=270)
        self.subtitle.grid(row=1, column=0, sticky="ew", pady=(4, 16))
        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="nsew")
        self.text = tk.Text(
            body, width=28, height=10, wrap="word", state="disabled",
            relief="flat", borderwidth=0, highlightthickness=0,
            background="#f5f7fa", foreground="#243447", padx=14, pady=14,
            font=self.fonts[3], cursor="arrow", selectbackground="#cfe3f5",
            selectforeground="#172b40", spacing1=2, spacing3=8,
        )
        bar = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("section", font=self.fonts[2], foreground="#47617b",
                                spacing1=18, spacing3=9)
        self.text.tag_configure("label", font=self.fonts[2], foreground="#64748b", spacing3=2)
        self.text.tag_configure("value", spacing3=10)
        self.text.tag_configure("summary", font=self.fonts[3], foreground="#173f5f", spacing3=14)
        self.text.tag_configure("detail", font=self.fonts[1], foreground="#526477", spacing3=8)
        ttk.Label(self, text="Reference-based observations", font=self.fonts[1]).grid(
            row=3, column=0, sticky="w", pady=(12, 0)
        )
        self.previous = None
        self.set_notes([])

    @staticmethod
    def friendly(value):
        names = {
            "head_forward": "Head forward", "head_inclination": "Head inclination",
            "trunk_inclination": "Trunk inclination", "WITHIN_LIMIT": "Within reference",
            "DEVIATION": "Outside reference", "PENDING": "Pending",
            "UNKNOWN": "Unavailable", "N/A": "Not available",
        }
        for original, replacement in names.items():
            value = value.replace(original, replacement)
        return value

    def set_notes(self, notes, subtitle=None):
        if subtitle is not None:
            self.subtitle.configure(text=subtitle)
        if isinstance(notes, str):
            notes = notes.split("\n\n")
        notes = tuple(str(note).strip() for note in notes if str(note).strip())
        if notes == self.previous:
            return
        self.previous = notes
        position = self.text.yview()[0]
        groups = {"Alignment": [], "Session": [], "Reference": [], "Measurements": [],
                  "Guidance": []}
        for line in notes[1:]:
            if re.match(r"[A-Z] \w.* / retry|[A-Z] calibrate", line):
                continue
            elif line.startswith("Reference:"):
                groups["Reference"].append(line.removeprefix("Reference:").strip())
            elif " | delta " in line:
                groups["Measurements"].append(line)
            elif line.startswith("Role:"):
                groups["Alignment"].extend(line.split(" | "))
            elif " | active " in line:
                groups["Session"].extend(line.split(" | "))
            else:
                groups["Guidance"].append(line)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "CURRENT OBSERVATION\n", "label")
        self.text.insert("end", self.friendly(notes[0]) + "\n" if notes else
                         "Notes will appear when a video frame is available.\n", "summary")
        for heading, entries in groups.items():
            if not entries:
                continue
            self.text.insert("end", heading.upper() + "\n", "section")
            for entry in entries:
                entry = self.friendly(entry)
                label, separator, value = entry.partition(":")
                if separator and heading in ("Alignment", "Measurements"):
                    self.text.insert("end", label + "\n", "label")
                    self.text.insert("end", value.strip().replace(" | ", "\n") + "\n", "value")
                else:
                    self.text.insert("end", entry + "\n", "value")
        self.text.configure(state="disabled")
        self.text.yview_moveto(position)
