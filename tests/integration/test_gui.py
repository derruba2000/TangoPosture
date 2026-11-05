import json
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np
import pytest

from tango_posture.calibration import save_calibration_file
from tango_posture.config import ENV_MAP, Config
from tango_posture.gui import PracticeGUI
from tango_posture.review import write_metadata


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("A graphical display is required for Tk integration tests")
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    gui = PracticeGUI(root, Config())
    root.update()
    yield root, gui
    gui.close()
    assert not errors


def test_all_tabs_and_setting_edits(studio):
    root, gui = studio
    for group in gui.group_pages:
        gui.select_group(group)
        root.update()
    assert set(gui.settings) == set(ENV_MAP)
    gui.settings["tp_CAMERA"].set("2")
    gui.settings["tp_HEAD_DOWN_ANGLE_DEGREES"].set("-22")
    gui.settings["tp_MIRROR"].set(True)
    cfg = gui.config()
    assert cfg.camera == 2 and cfg.head_down_angle == -22 and cfg.mirror
    gui.settings["tp_AXIS_CURVE_CLEAR"].set("0.9")
    with pytest.raises(ValueError):
        gui.config()


def test_markdown_text_has_explicit_contrast_colors(studio):
    _, gui = studio
    text = gui.review.report_view.text
    assert text.cget("foreground") == "#202124"
    assert text.cget("background") == "#ffffff"
    assert text.tag_cget("code", "foreground") == "#202124"


def test_recording_button_replaces_custom_minimize_controls(studio):
    root, gui = studio
    assert not hasattr(gui, "minimize_button")
    assert gui.record_button.cget("text") == "Start recording video"
    assert gui.record_button.instate(["disabled"])
    menu = root.nametowidget(root.cget("menu"))
    files = root.nametowidget(menu.entrycget(0, "menu"))
    labels = [
        files.entrycget(index, "label")
        for index in range(files.index("end") + 1)
        if files.type(index) == "command"
    ]
    assert "Minimize app" not in labels


def test_help_menu_opens_searchable_manual_with_section_navigation(studio):
    root, gui = studio
    assert gui.help_window is None
    assert gui.help_button.winfo_ismapped()
    menu = root.nametowidget(root.cget("menu"))
    assert "Help" in [menu.entrycget(index, "label") for index in range(menu.index("end") + 1)]
    gui.help_button.invoke()
    root.update()
    assert gui.help_window is not None
    assert gui.help_view is not None and gui.help_outline is not None
    assert "Settings explained" in [title for _, title, _ in gui.help_view.headings]
    assert gui.help_outline.get_children()

    gui.help_view.query.set("confidence")
    gui.help_view.search()
    assert gui.help_view.text.tag_ranges("found")

    target = next(
        node for node, position in gui.help_positions.items()
        if gui.help_view.text.get(position, f"{position} lineend").strip() == "Settings explained"
    )
    gui.help_outline.selection_set(target)
    gui.help_outline.event_generate("<<TreeviewSelect>>")
    root.update()
    assert gui.help_view.text.index("insert") == gui.help_positions[target]


def test_preset_keeps_filenames_and_reference_but_resets_confirmation(
    studio, tmp_path, monkeypatch
):
    _, gui = studio
    preset = tmp_path / "preset.json"
    monkeypatch.setattr("tango_posture.gui.filedialog.asksaveasfilename", lambda **_: str(preset))
    monkeypatch.setattr("tango_posture.gui.filedialog.askopenfilename", lambda **_: str(preset))
    gui.options["reference_mode"].set("saved")
    gui.options["calibration"].set("reference-uuid")
    gui.options["confirm_camera"].set(True)
    gui.options["raw_path"].set("videos/my dance.mp4")
    gui.settings["tp_TANGO_BPM"].set("128")
    gui.save_preset()
    assert json.loads(preset.read_text())["practice"]["raw_path"] == "videos/my dance.mp4"
    gui.options["raw_path"].set("other.mp4")
    gui.load_preset()
    assert gui.options["raw_path"].get() == "videos/my dance.mp4"
    assert gui.options["calibration"].get() == "reference-uuid"
    assert gui.options["confirm_camera"].get() is False
    assert gui.config().tango_bpm == 128


def test_environment_import_migrates_old_names_without_reading_process(
    studio, tmp_path, monkeypatch
):
    _, gui = studio
    env = tmp_path / "old.env"
    env.write_text("TANGO_BPM=133\nHEAD_DOWN_ANGLE_DEGREES=-20\n")
    monkeypatch.setenv("tp_TANGO_BPM", "144")
    monkeypatch.setattr("tango_posture.gui.filedialog.askopenfilename", lambda **_: str(env))
    monkeypatch.setattr("tango_posture.gui.filedialog.asksaveasfilename", lambda **_: str(env))
    gui.import_env()
    assert gui.config().tango_bpm == 133
    gui.save_env()
    assert "tp_TANGO_BPM=133" in env.read_text()
    assert "\nTANGO_BPM=" not in env.read_text()


def test_reference_picker_restores_capture_profile_and_resets_confirmation(studio, reference):
    _, gui = studio
    save_calibration_file(reference)
    gui.refresh_references()
    label = next(label for label in gui.reference_ids if reference.name in label)
    gui.options["calibration"].set(label)
    gui.match_reference()
    assert gui.options["reference_mode"].get() == "saved"
    assert gui.options["source"].get() == "video"
    assert gui.config().side == reference.profile["side"]
    assert gui.config().width == reference.profile["width"]
    gui.options["confirm_camera"].set(True)
    gui.options["calibration"].set("latest")
    assert gui.options["confirm_camera"].get() is False


def test_native_preview_worker_and_stop_controls(studio, monkeypatch):
    root, gui = studio
    monkeypatch.setattr("tango_posture.gui.verify", lambda _: "verified")
    received = []
    stopped = threading.Event()

    def run(args, *, display):
        received.extend(args)
        display.show(np.zeros((240, 320, 3), np.uint8))
        # Keep the worker alive until the Tk test clicks quit; no camera is opened.
        if not display.stopped.wait(3):
            raise RuntimeError("Quit control was not delivered")
        stopped.set()
        report = Path("report_saved.md")
        report.write_text("# Saved practice\nThe original measurements remain available.\n")
        display.report_ready(report)
        print("Saved report: test-report.md")
        return 0

    monkeypatch.setattr("tango_posture.cli.main", run)
    gui.start()
    deadline = time.monotonic() + 3
    while gui.photo is None and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert gui.photo is not None
    assert gui.start_button.instate(["disabled"])
    assert not gui.display.recording.is_set()
    assert gui.record_button.instate(["!disabled"])
    gui.record_button.invoke()
    assert gui.display.recording.is_set()
    assert gui.record_button.instate(["disabled"])
    assert Path(received[received.index("--config") + 1]).is_file()
    gui.control("p")
    assert gui.display.poll_key() == ord("p")
    gui.control("q")
    while gui.running and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert stopped.is_set() and not gui.running
    assert gui.preview is None
    assert gui.start_button.instate(["!disabled"])
    assert gui.record_button.instate(["disabled"])
    assert "Saved report" in gui.log.get("1.0", "end")
    assert gui.review.report.path == (Path.cwd() / "report_saved.md")
    assert gui.tabs.select() == str(gui.review)


def review_fixture(tmp_path):
    import cv2

    raw, overlay = tmp_path / "raw.avi", tmp_path / "overlay.avi"
    for path, base in ((raw, 10), (overlay, 80)):
        width, height = (320, 700) if path == overlay else (160, 120)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (width, height))
        assert writer.isOpened()
        for i in range(12):
            writer.write(np.full((height, width, 3), base + i * 5, np.uint8))
        writer.release()
    report = tmp_path / "report_review.md"
    report.write_text(
        "# Practice report\nGenerated: 2026-11-05T07:21:40+00:00\n\n## Observations\nAt **00:01.2**, tracking was UNKNOWN.\n"
    )
    write_metadata(report, "camera:0", raw, overlay, [1.0 + i * 0.1 for i in range(12)])
    return report


def test_report_and_raw_overlay_replay_are_readable_in_the_app(studio, tmp_path):
    root, gui = studio
    panel = gui.review
    panel.load_report(review_fixture(tmp_path))
    gui.tabs.select(panel)
    root.update()
    assert "tracking was UNKNOWN" in panel.report_view.text.get("1.0", "end")
    assert len([tag for tag in panel.report_view.text.tag_names() if tag.startswith("time-")]) == 1
    assert panel.video_selection.get() == "Overlay"
    panel.seek_report_time(1.2)
    root.geometry("840x640")
    root.update()
    for button in (panel.player.play_button, gui.start_button, gui.stop_button):
        assert button.winfo_ismapped()
        assert (
            button.winfo_rooty() + button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
        )
    assert panel.player.reader.index == 2
    overlay_mean = panel.player.last_image.mean()
    panel.video_selection.set("Raw video")
    panel.switch_video()
    assert panel.player.reader.index == 2
    assert panel.player.last_image.mean() < overlay_mean
    panel.player.step(1)
    assert panel.player.reader.index == 3
    panel.player.toggle()
    deadline = time.monotonic() + 0.18
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    panel.player.pause()
    assert panel.player.reader.index > 3
    assert not panel.player.playing


def test_markdown_view_formats_report_blocks_and_inline_styles(studio):
    _, gui = studio
    view = gui.review.report_view
    view.set(
        "# Posture report\n\n"
        "**Observation:** *shoulders relaxed* with `clearance`.\n\n"
        "- First item\n1. Second item\n\n"
        "> Coverage note\n\n"
        "| Metric | Value |\n| --- | --- |\n| Head | UNKNOWN |\n\n"
        "```text\n**literal code**\n```\n"
    )
    rendered = view.text.get("1.0", "end")
    assert "# Posture" not in rendered
    assert "**Observation:**" not in rendered and "`" not in rendered
    assert "First item" in rendered and "Second item" in rendered
    assert "Coverage note" in rendered and "UNKNOWN" in rendered
    assert "**literal code**" in rendered
    assert view.text.tag_ranges("h1")
    assert view.text.tag_ranges("bold")
    assert view.text.tag_ranges("italic")
    assert view.text.tag_ranges("code")
    assert view.text.tag_ranges("quote")
    assert view.text.tag_ranges("list")
    assert view.text.tag_ranges("table")


def test_analyze_button_shows_preflight_errors_in_ollama_pane(studio, tmp_path):
    _, gui = studio
    panel = gui.review
    panel.analyze_button.invoke()
    assert "Open a practice report first" in panel.analysis_status.get()
    report = tmp_path / "report_invalid_ollama.md"
    report.write_text("# Report\n")
    panel.load_report(report)
    gui.settings["tp_OLLAMA_BASE_URL"].set("not-a-server-url")
    panel.analyze_button.invoke()
    assert "Ollama URL must be an http(s) server URL" in panel.analysis_status.get()


def test_oversized_report_error_is_visible_and_includes_limit(studio, tmp_path):
    root, gui = studio
    panel = gui.review
    report = tmp_path / "report_over_limit.md"
    report.write_text("x" * 1001)
    panel.load_report(report)
    gui.tabs.select(panel)
    gui.settings["tp_OLLAMA_MAX_REPORT_CHARS"].set("1000")
    panel.analyze_button.invoke()

    deadline = time.monotonic() + 2
    while panel.job and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)

    assert not panel.job
    assert "1,001 characters" in panel.analysis_status.get()
    assert "limit is 1,000" in panel.analysis_status.get()
    assert "The report was not sent" in panel.analysis_status.get()


def test_ollama_analysis_streams_saves_and_reopens_for_its_report(studio, tmp_path, ollama_server):
    root, gui = studio
    url, server = ollama_server
    panel = gui.review
    path = review_fixture(tmp_path)
    panel.load_report(path)
    gui.settings["tp_OLLAMA_BASE_URL"].set(url)
    gui.settings["tp_OLLAMA_MODEL"].set("test-model:latest")
    panel.list_models()
    deadline = time.monotonic() + 4
    while panel.models_job and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert tuple(panel.model_combo["values"]) == ("test-model:latest",)
    panel.analyze()
    while panel.job and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not panel.job
    assert "UNKNOWN" in panel.analysis_view.text.get("1.0", "end")
    assert "test-model:latest" in panel.analysis_meta.get()
    assert len(list(tmp_path.glob("*.analysis-*.json"))) == 1
    assert server.requests[0]["model"] == "test-model:latest"
    original = panel.report.markdown
    panel.load_report(path)
    assert panel.report.markdown == original
    assert panel.analysis_data["model"] == "test-model:latest"
    assert "Discuss camera framing" in panel.analysis_view.text.get("1.0", "end")
    other = tmp_path / "report_other.md"
    other.write_text("# Different practice\n")
    panel.load_report(other)
    assert panel.analysis_data is None
    assert not panel.analysis_view.text.get("1.0", "end").strip()


def test_ollama_stream_is_rendered_before_completion(studio, tmp_path, monkeypatch):
    root, gui = studio
    panel = gui.review
    report = tmp_path / "report_stream.md"
    report.write_text("# Report\n\nObservation at 00:01.0.\n")
    panel.load_report(report)
    gui.tabs.select(panel)
    emitted = threading.Event()
    release = threading.Event()

    def analyze(_client, _report, on_chunk, _cancel):
        on_chunk("## Streaming\n\n**Visible** `now`")
        emitted.set()
        assert release.wait(2)
        return "## Streaming\n\n**Visible** `now`", True

    monkeypatch.setattr("tango_posture.review_ui.OllamaClient.analyze", analyze)
    panel.request_analysis()
    assert emitted.wait(2)
    root.update()
    assert panel.analysis_status_label.winfo_ismapped()
    assert panel.analysis_status.get() == "Receiving analysis..."
    deadline = time.monotonic() + 2
    while not panel.analysis_stream and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    rendered = panel.analysis_view.text.get("1.0", "end")
    assert panel.analysis_status.get() == "Receiving analysis..."
    assert "Visible now" in rendered
    assert "##" not in rendered and "**" not in rendered and "`" not in rendered
    assert panel.analysis_view.text.tag_ranges("h2")
    assert panel.analysis_view.text.tag_ranges("bold")
    assert panel.analysis_view.text.tag_ranges("code")
    release.set()
    deadline = time.monotonic() + 2
    while panel.job and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not panel.job


def test_cancelled_analysis_does_not_save_or_show_stale_results(studio, tmp_path, monkeypatch):
    root, gui = studio
    panel = gui.review
    panel.load_report(review_fixture(tmp_path))
    finished = threading.Event()

    def analyze(_client, _report, on_chunk, cancel):
        on_chunk("Partial")
        cancel.wait(2)
        finished.set()
        return "Cancelled output", True

    monkeypatch.setattr("tango_posture.review_ui.OllamaClient.analyze", analyze)
    panel.analyze()
    panel.cancel_analysis()
    assert finished.wait(2)
    deadline = time.monotonic() + 0.1
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert panel.analysis_data is None
    assert not list(tmp_path.glob("*.analysis-*.json"))
    assert not panel.job
