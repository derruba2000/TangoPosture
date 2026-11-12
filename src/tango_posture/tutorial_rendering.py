"""Shared Markdown-to-HTML renderer for the offline manual."""

from __future__ import annotations

import html
import re
from typing import Any

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


def render_tutorial(
    source: str,
) -> tuple[str, list[tuple[int, str, str]], dict[str, str]]:
    converter = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"])
    content = converter.convert(source)
    headings: list[tuple[int, str, str]] = []

    def collect(tokens: list[dict[str, Any]]) -> None:
        for token in tokens:
            headings.append(
                (token["level"], html.unescape(token["name"]), token["id"])
            )
            collect(token["children"])

    collect(converter.toc_tokens)
    settings: dict[str, str] = {}

    def anchor(match: re.Match[str]) -> str:
        field = html.unescape(match[1])
        target = "setting-" + field.replace(".", "-").replace("_", "-")
        settings[field] = target
        return f'<tr id="{target}"><td><code>{match[1]}</code>'

    start = content.find('<h2 id="settings-explained">')
    if start >= 0:
        end = content.find("<h2 ", start + 1)
        end = len(content) if end < 0 else end
        section = re.sub(r"<tr>\s*<td><code>([a-z][a-z0-9_.]*)</code>", anchor, content[start:end])
        content = content[:start] + section + content[end:]
    page = (
        '<!DOCTYPE html><html><head><meta charset="utf-8" />'
        f'<title>Tango Posture Manual</title><style>{STYLE}</style></head><body>'
        '<div class="article"><div class="eyebrow">TANGO POSTURE / LEARN &amp; PRACTISE</div>'
        f"{content}</div></body></html>"
    )
    return page, headings, settings
