"""Offline HTML tutorial with searchable content and stable setting anchors."""

import html
import re
import textwrap
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import ttk
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import markdown

STYLE = """
body { background: #f2f5f9; color: #26364a; font-family: Helvetica, Arial, sans-serif;
       font-size: 15px; line-height: 1.6; margin: 0; }
.article { background: white; margin: 20px; padding: 30px; }
.eyebrow { color: #56718d; font-size: 11px; font-weight: bold; letter-spacing: 2px; }
h1 { color: #152d45; font-size: 30px; line-height: 1.2; margin: 12px 0 24px; }
h2 { color: #173e60; font-size: 24px; margin: 40px 0 18px;
     padding-top: 18px; border-top: 1px solid #dce5ed; }
h3 { color: #234d6a; font-size: 19px; margin: 28px 0 12px; }
p { margin: 12px 0; }
a { color: #116aa0; text-decoration: underline; }
ul, ol { padding-left: 26px; margin: 14px 0; }
li { margin: 8px 0; }
table { width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }
th { background: #e7eff6; color: #173e60; text-align: left; font-weight: bold; }
th, td { padding: 12px; border: 1px solid #dbe4ed; vertical-align: top; }
tr:nth-child(even) { background: #f7f9fc; }
tr.setting-focus td { background: #fff1c9; border-color: #d4a545; }
code { font-family: Courier, monospace; font-size: 12px; background: #eef3f7; color: #244c69; }
pre { background: #eef3f7; border-left: 3px solid #6c92b3; padding: 16px;
      white-space: pre-wrap; font-size: 12px; }
blockquote { background: #edf5fa; border-left: 4px solid #3e83ad;
             margin: 20px 0; padding: 12px 18px; }
hr { border: 0; border-top: 1px solid #dbe4ed; margin: 28px 0; }
"""


def render_tutorial(source):
    converter = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"])
    content = converter.convert(source)
    headings = []

    def collect(tokens):
        for token in tokens:
            headings.append((token["level"], html.unescape(token["name"]), token["id"]))
            collect(token["children"])

    collect(converter.toc_tokens)
    settings = {}

    def anchor(match):
        field = html.unescape(match[1])
        target = "setting-" + field.replace(".", "-").replace("_", "-")
        settings[field] = target
        return f'<tr id="{target}"><td><code>{match[1]}</code>'

    # Earlier reference tables can repeat setting keys. Anchor only the detailed guide.
    start = content.find('<h2 id="settings-explained">')
    if start >= 0:
        end = content.find("<h2 ", start + 1)
        end = len(content) if end < 0 else end
        section = re.sub(r"<tr>\s*<td><code>([a-z][a-z0-9_.]*)</code>",
                         anchor, content[start:end])
        content = content[:start] + section + content[end:]
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8" />'
            f'<title>Tango Posture Manual</title><style>{STYLE}</style></head><body>'
            '<div class="article"><div class="eyebrow">TANGO POSTURE / LEARN &amp; PRACTISE</div>'
            f'{content}</div></body></html>')
    return page, headings, settings


class _Element:
    def __init__(self, node: ElementTree.Element, view: "TutorialView", anchor: str):
        self.node = node
        self.view = view
        self.anchor = anchor

    def getAttribute(self, name: str) -> str:
        return self.node.get(name, "")

    def setAttribute(self, name: str, value: str) -> None:
        self.node.set(name, value)
        if name == "class" and self.anchor in self.view.anchors:
            start, end = self.view.anchors[self.anchor]
            if "setting-focus" in value.split():
                self.view.text.tag_add("setting-focus", start, end)
            else:
                self.view.text.tag_remove("setting-focus", start, end)

    def scrollIntoView(self) -> None:
        if self.anchor in self.view.anchors:
            start, _ = self.view.anchors[self.anchor]
            self.view.text.mark_set("insert", start)
            self.view.text.yview(start)
            self.view.text.see(start)


class _Document:
    def __init__(self, view: "TutorialView"):
        self.view = view

    def getElementById(self, anchor: str) -> _Element | None:
        return self.view.elements.get(anchor)


class _Browser:
    def __init__(self, view: "TutorialView"):
        self.document = _Document(view)


class TutorialView(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="Search tutorial").pack(side="left", padx=(0, 8))
        self.query = tk.StringVar()
        entry = ttk.Entry(toolbar, textvariable=self.query, width=24)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _: self.search())
        ttk.Button(toolbar, text="Find", command=self.search).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Next", command=self.next_match).pack(side="left")
        self.search_status = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.search_status).pack(side="left", padx=8)

        canvas_bg = tk.Frame(self, background="#f2f5f9", padx=16, pady=12)
        canvas_bg.grid(row=1, column=0, sticky="nsew")
        canvas_bg.columnconfigure(0, weight=1)
        canvas_bg.rowconfigure(0, weight=1)

        self.fonts: dict[str, tkfont.Font] = {}
        for name, family, size, bold, italic in (
            ("eyebrow", "TkDefaultFont", 10, True, False),
            ("h1", "TkDefaultFont", 22, True, False),
            ("h2", "TkDefaultFont", 17, True, False),
            ("h3", "TkDefaultFont", 14, True, False),
            ("body", "TkDefaultFont", 12, False, False),
            ("bold", "TkDefaultFont", 12, True, False),
            ("italic", "TkDefaultFont", 12, False, True),
            ("code", "TkFixedFont", 11, False, False),
            ("code_bold", "TkFixedFont", 11, True, False),
            ("table", "TkFixedFont", 11, False, False),
            ("table_bold", "TkFixedFont", 11, True, False),
        ):
            font = tkfont.nametofont(family).copy()
            font.configure(
                size=size,
                weight="bold" if bold else "normal",
                slant="italic" if italic else "roman",
            )
            self.fonts[name] = font

        self.text = tk.Text(
            canvas_bg,
            wrap="word",
            state="disabled",
            padx=28,
            pady=24,
            font=self.fonts["body"],
            foreground="#26364a",
            background="#ffffff",
            insertbackground="#26364a",
            selectbackground="#cfe8ff",
            selectforeground="#152d45",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#dbe4ed",
            highlightcolor="#dbe4ed",
        )
        scrollbar = ttk.Scrollbar(canvas_bg, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.text.tag_configure(
            "eyebrow", font=self.fonts["eyebrow"], foreground="#56718d", spacing3=6
        )
        self.text.tag_configure(
            "h1", font=self.fonts["h1"], foreground="#152d45", spacing1=6, spacing3=16
        )
        self.text.tag_configure(
            "h2_rule", font=self.fonts["table"], foreground="#dce5ed", spacing1=18, spacing3=2
        )
        self.text.tag_configure(
            "h2", font=self.fonts["h2"], foreground="#173e60", spacing1=4, spacing3=10
        )
        self.text.tag_configure(
            "h3", font=self.fonts["h3"], foreground="#234d6a", spacing1=14, spacing3=6
        )
        self.text.tag_configure(
            "p", font=self.fonts["body"], foreground="#26364a", spacing1=4, spacing2=3, spacing3=6
        )
        self.text.tag_configure("bold", font=self.fonts["bold"], foreground="#152d45")
        self.text.tag_configure("italic", font=self.fonts["italic"], foreground="#26364a")
        self.text.tag_configure(
            "code", font=self.fonts["code"], foreground="#244c69", background="#eef3f7"
        )
        self.text.tag_configure(
            "pre",
            font=self.fonts["code"],
            foreground="#244c69",
            background="#eef3f7",
            lmargin1=16,
            lmargin2=16,
            rmargin=16,
            spacing1=2,
            spacing2=2,
            spacing3=2,
        )
        self.text.tag_configure(
            "blockquote",
            font=self.fonts["body"],
            foreground="#26364a",
            background="#edf5fa",
            lmargin1=18,
            lmargin2=18,
            rmargin=18,
            spacing1=6,
            spacing2=2,
            spacing3=6,
        )
        self.text.tag_configure(
            "li",
            font=self.fonts["body"],
            foreground="#26364a",
            lmargin1=18,
            lmargin2=36,
            spacing1=3,
            spacing2=2,
            spacing3=3,
        )
        self.text.tag_configure("link", foreground="#116aa0", underline=True)
        self.text.tag_configure(
            "hr", font=self.fonts["table"], foreground="#dbe4ed", spacing1=12, spacing3=12
        )
        self.text.tag_configure(
            "th",
            font=self.fonts["table_bold"],
            foreground="#173e60",
            background="#e7eff6",
            spacing1=4,
            spacing3=4,
        )
        self.text.tag_configure(
            "table_rule",
            font=self.fonts["table"],
            foreground="#c5d3e0",
            background="#e7eff6",
        )
        self.text.tag_configure(
            "tr_odd",
            font=self.fonts["table"],
            foreground="#26364a",
            background="#ffffff",
            spacing1=1,
            spacing3=1,
        )
        self.text.tag_configure(
            "tr_even",
            font=self.fonts["table"],
            foreground="#26364a",
            background="#f7f9fc",
            spacing1=1,
            spacing3=1,
        )
        self.text.tag_configure(
            "tr_rule",
            font=self.fonts["table"],
            foreground="#e3ebf2",
            background="#ffffff",
        )
        self.text.tag_configure(
            "table_code",
            font=self.fonts["table_bold"],
            foreground="#244c69",
        )
        self.text.tag_configure(
            "setting-focus",
            background="#fff1c9",
            foreground="#152d45",
        )
        self.text.tag_configure(
            "search-match",
            background="#ffe89c",
            foreground="#152d45",
        )
        self.text.tag_configure(
            "search-current",
            background="#f5b841",
            foreground="#152d45",
        )
        self.text.tag_raise("setting-focus")
        self.text.tag_raise("table_code")
        self.text.tag_raise("search-match")
        self.text.tag_raise("search-current")

        self.browser = _Browser(self)
        self.page = ""
        self.headings: list[tuple[int, str, str]] = []
        self.setting_anchors: dict[str, str] = {}
        self.anchors: dict[str, tuple[str, str]] = {}
        self.elements: dict[str, _Element] = {}
        self.matches: list[tuple[str, str]] = []
        self.current_anchor: str | None = None
        self.active_setting: str | None = None
        self.match_count = 0
        self.match_index = 1
        self.last_query = ""
        self._link_seq = 0

    def set(self, source):
        self.page, self.headings, self.setting_anchors = render_tutorial(source)
        document = ElementTree.fromstring(self.page.removeprefix("<!DOCTYPE html>"))
        article = document.find(".//div[@class='article']")
        if article is None:
            article = document

        self.anchors.clear()
        self.elements.clear()
        self.matches.clear()
        self.current_anchor = None
        self.active_setting = None
        self.match_count = 0
        self.match_index = 1
        self.last_query = ""
        self.search_status.set("")

        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for tag in list(self.text.tag_names()):
            if tag.startswith("link-"):
                self.text.tag_delete(tag)
        self._link_seq = 0

        for child in article:
            self._render_block(child)

        self.text.configure(state="disabled")

    def _register_anchor(self, node: ElementTree.Element, start: str, end: str) -> None:
        anchor = node.get("id")
        if anchor:
            self.anchors[anchor] = (start, end)
            self.elements[anchor] = _Element(node, self, anchor)

    def _render_block(self, node: ElementTree.Element) -> None:
        tag = node.tag
        if tag == "div" and node.get("class") == "eyebrow":
            start = self.text.index("end-1c")
            self.text.insert("end", "".join(node.itertext()).strip() + "\n", ("eyebrow",))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag in ("h1", "h2", "h3"):
            if tag == "h2":
                self.text.insert("end", "─" * 92 + "\n", ("h2_rule",))
            start = self.text.index("end-1c")
            self._insert_inline(node, (tag,))
            self.text.insert("end", "\n", (tag,))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag == "p":
            start = self.text.index("end-1c")
            self._insert_inline(node, ("p",))
            self.text.insert("end", "\n", ("p",))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag in ("ul", "ol"):
            start = self.text.index("end-1c")
            for idx, item in enumerate(node.findall("li"), start=1):
                item_start = self.text.index("end-1c")
                prefix = f"{idx}. " if tag == "ol" else "• "
                self.text.insert("end", prefix, ("li", "bold" if tag == "ol" else "li"))
                self._insert_inline(item, ("li",))
                self.text.insert("end", "\n", ("li",))
                self._register_anchor(item, item_start, self.text.index("end-1c"))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag == "pre":
            start = self.text.index("end-1c")
            raw = "".join(node.itertext()).rstrip("\n")
            for line in raw.splitlines() or [""]:
                self.text.insert("end", f"  {line}\n", ("pre",))
            self.text.insert("end", "\n", ("p",))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag == "blockquote":
            start = self.text.index("end-1c")
            children = list(node)
            if children:
                for child in children:
                    self._insert_inline(child, ("blockquote",))
                    self.text.insert("end", "\n", ("blockquote",))
            else:
                self._insert_inline(node, ("blockquote",))
                self.text.insert("end", "\n", ("blockquote",))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag == "hr":
            start = self.text.index("end-1c")
            self.text.insert("end", "─" * 92 + "\n", ("hr",))
            self._register_anchor(node, start, self.text.index("end-1c"))
        elif tag == "table":
            self._render_table(node)

    def _insert_inline(self, node: ElementTree.Element, base_tags: tuple[str, ...]) -> None:
        if node.text:
            text = re.sub(r"\s+", " ", node.text)
            if text:
                self.text.insert("end", text, base_tags)
        for child in node:
            child_tags = list(base_tags)
            if child.tag in ("strong", "b"):
                child_tags.append("bold")
            elif child.tag in ("em", "i"):
                child_tags.append("italic")
            elif child.tag == "code":
                child_tags.append("code")
            elif child.tag == "a":
                href = child.get("href", "")
                self._link_seq += 1
                link_tag = f"link-{self._link_seq}"

                def on_click(_event: object, url: str = href) -> None:
                    self.open_link(url)

                self.text.tag_bind(link_tag, "<Button-1>", on_click)
                self.text.tag_bind(
                    link_tag, "<Enter>", lambda _e: self.text.configure(cursor="hand2")
                )
                self.text.tag_bind(
                    link_tag, "<Leave>", lambda _e: self.text.configure(cursor="")
                )
                child_tags.extend(["link", link_tag])
            self._insert_inline(child, tuple(child_tags))
            if child.tail:
                tail = re.sub(r"\s+", " ", child.tail)
                if tail:
                    self.text.insert("end", tail, base_tags)

    def _render_table(self, table: ElementTree.Element) -> None:
        start = self.text.index("end-1c")
        header_cells = [
            re.sub(r"\s+", " ", "".join(th.itertext())).strip()
            for th in table.findall(".//thead/tr/th")
        ]
        body_rows = table.findall(".//tbody/tr") or table.findall("tr")
        parsed_rows: list[tuple[ElementTree.Element, list[str], list[bool]]] = []
        for tr in body_rows:
            tds = tr.findall("td")
            if not tds:
                continue
            texts = [re.sub(r"\s+", " ", "".join(td.itertext())).strip() for td in tds]
            has_code = [td.find("code") is not None and (td.text or "").strip() == "" for td in tds]
            parsed_rows.append((tr, texts, has_code))

        col_count = max(
            [len(header_cells)] + [len(texts) for _, texts, _ in parsed_rows],
            default=0,
        )
        if col_count == 0:
            return

        if len(header_cells) < col_count:
            header_cells.extend([""] * (col_count - len(header_cells)))

        natural = [len(header_cells[c]) for c in range(col_count)]
        for _, texts, _ in parsed_rows:
            for c, cell in enumerate(texts):
                natural[c] = max(natural[c], len(cell))

        target_total = 90
        widths = [0] * col_count
        flexible: list[int] = []
        remaining = target_total - 3 * (col_count - 1)
        for c in range(col_count):
            if c < col_count - 1 and natural[c] <= 28:
                widths[c] = max(natural[c], len(header_cells[c]), 6)
                remaining -= widths[c]
            else:
                flexible.append(c)

        if not flexible:
            flexible = [col_count - 1]
            remaining += widths[col_count - 1]

        per_flex = max(18, remaining // len(flexible))
        for c in flexible:
            widths[c] = max(16, min(natural[c], per_flex))

        def wrap_row(cells: list[str]) -> list[list[str]]:
            wrapped = [
                textwrap.wrap(
                    cells[c] if c < len(cells) else "",
                    width=widths[c],
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
                for c in range(col_count)
            ]
            height = max(len(col_lines) for col_lines in wrapped)
            for col_lines in wrapped:
                col_lines.extend([""] * (height - len(col_lines)))
            return wrapped

        if any(header_cells):
            wrapped_header = wrap_row(header_cells)
            for line_idx in range(len(wrapped_header[0])):
                line = " " + " │ ".join(
                    wrapped_header[c][line_idx].ljust(widths[c]) for c in range(col_count)
                ) + " \n"
                self.text.insert("end", line, ("th",))
            rule = "─" + "─┼─".join("─" * widths[c] for c in range(col_count)) + "─\n"
            self.text.insert("end", rule, ("table_rule",))

        for row_idx, (tr, texts, has_code) in enumerate(parsed_rows):
            row_tag = "tr_even" if row_idx % 2 == 1 else "tr_odd"
            row_start = self.text.index("end-1c")
            wrapped_cells = wrap_row(texts)
            line_count = len(wrapped_cells[0])
            for line_idx in range(line_count):
                self.text.insert("end", " ", (row_tag,))
                for c in range(col_count):
                    cell_part = wrapped_cells[c][line_idx].ljust(widths[c])
                    cell_tags = (row_tag, "table_code") if has_code[c] else (row_tag,)
                    self.text.insert("end", cell_part, cell_tags)
                    if c < col_count - 1:
                        self.text.insert("end", " │ ", (row_tag,))
                self.text.insert("end", " \n", (row_tag,))
            row_end = self.text.index("end-1c")
            self._register_anchor(tr, row_start, row_end)

        self.text.insert("end", "\n", ("p",))
        self._register_anchor(table, start, self.text.index("end-1c"))

    def navigate(self, anchor: str) -> None:
        element = self.browser.document.getElementById(anchor)
        if element is not None:
            element.scrollIntoView()
            self.current_anchor = anchor

    def show_setting(self, field: str) -> None:
        if self.active_setting:
            old = self.browser.document.getElementById(self.setting_anchors[self.active_setting])
            if old is not None:
                old.setAttribute("class", "")
        self.active_setting = field
        target = self.setting_anchors[field]
        element = self.browser.document.getElementById(target)
        if element is not None:
            element.setAttribute("class", "setting-focus")
        self.navigate(target)

    def search(self) -> None:
        self.last_query = self.query.get().strip()
        self.text.tag_remove("search-match", "1.0", "end")
        self.text.tag_remove("search-current", "1.0", "end")
        self.matches.clear()
        self.match_index = 1
        if not self.last_query:
            self.match_count = 0
            self.search_status.set("")
            return

        pos = "1.0"
        count_var = tk.IntVar()
        while True:
            found = self.text.search(
                self.last_query, pos, stopindex="end", nocase=True, count=count_var
            )
            if not found or count_var.get() <= 0:
                break
            end = f"{found}+{count_var.get()}c"
            self.matches.append((found, end))
            self.text.tag_add("search-match", found, end)
            pos = end

        self.match_count = len(self.matches)
        self.search_status.set(f"{self.match_count} matches")
        if self.matches:
            first_start, first_end = self.matches[0]
            self.text.tag_add("search-current", first_start, first_end)
            self.text.see(first_start)

    def next_match(self) -> None:
        if self.query.get().strip() != self.last_query:
            self.search()
        elif self.match_count:
            self.match_index = self.match_index % self.match_count + 1
            self.text.tag_remove("search-current", "1.0", "end")
            start, end = self.matches[self.match_index - 1]
            self.text.tag_add("search-current", start, end)
            self.text.see(start)
            self.search_status.set(f"{self.match_index} of {self.match_count}")

    def open_link(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.fragment and parsed.scheme in ("", "file"):
            self.navigate(unquote(parsed.fragment))
        elif parsed.scheme in ("https", "http"):
            webbrowser.open(url)

