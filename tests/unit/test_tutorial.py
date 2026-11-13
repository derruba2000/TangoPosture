from pathlib import Path
from xml.etree import ElementTree

from tango_posture.config import SETTING_MAP
from tango_posture.tutorial_rendering import render_tutorial


def test_manual_has_semantic_html_and_unique_setting_targets():
    manual = Path(__file__).resolve().parents[2] / "docs" / "TANGO_POSTURE_GUIDE.md"
    page, headings, settings = render_tutorial(manual.read_text(encoding="utf-8"))
    document = ElementTree.fromstring(page.removeprefix("<!DOCTYPE html>"))
    assert document.findall(".//table/thead/tr/th")
    assert document.findall(".//pre/code")
    assert document.findall(".//a")
    assert set(SETTING_MAP) <= set(settings)
    ids = [node.attrib["id"] for node in document.iter() if "id" in node.attrib]
    assert len(ids) == len(set(ids))
    assert all(anchor in ids for _, _, anchor in headings)
    for field in SETTING_MAP:
        row = document.find(f".//tr[@id='{settings[field]}']")
        assert row is not None
        assert row.find("td/code").text == field
        assert len(list(row)) in (2, 3)
