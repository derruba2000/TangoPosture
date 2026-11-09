import json
import threading
import time
import tkinter as tk
import tomllib
from pathlib import Path

import numpy as np
import pytest

from tango_posture.archive import append_segment, save_calibration_archive
from tango_posture.calibration import save_calibration_file
from tango_posture.config import SETTING_MAP, Config
from tango_posture.gui import PracticeGUI
from tango_posture.practice import GuiDisplay
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


def test_splash_hides_studio_until_three_second_timer_finishes(studio, monkeypatch):
    root, gui = studio
    scheduled = []
    after = root.after

    def schedule(delay, callback, *args):
        if delay == 3000:
            scheduled.append((delay, callback))
            return after(60000, callback)
        return after(delay, callback, *args)

    monkeypatch.setattr(root, "after", schedule)
    gui.show_splash()
    root.update()
    assert root.state() == "withdrawn"
    assert gui.splash_window.winfo_ismapped()
    assert gui.splash_window.cget("cursor") == "watch"
    assert str(gui.splash_progress.cget("mode")) == "indeterminate"
    assert scheduled[0][0] == 3000
    splash = gui.splash_window
    gui.show_splash()
    assert gui.splash_window is splash and len(scheduled) == 1
    root.after_cancel(gui.splash_timer)
    scheduled[0][1]()
    root.update()
    assert gui.splash_window is None and gui.splash_timer is None
    assert root.state() == "normal"


def test_help_about_menu_opens_single_branded_dialog(studio, monkeypatch):
    root, gui = studio
    monkeypatch.setattr("tango_posture.gui.version", lambda _: "0.1.0")
    opened = []
    monkeypatch.setattr("tango_posture.gui.webbrowser.open", lambda url: opened.append(url))
    menu = root.nametowidget(root.cget("menu"))
    help_index = next(
        index for index in range(menu.index("end") + 1)
        if menu.entrycget(index, "label") == "Help"
    )
    help_menu = root.nametowidget(menu.entrycget(help_index, "menu"))
    about_index = next(
        index for index in range(help_menu.index("end") + 1)
        if help_menu.type(index) == "command"
        and help_menu.entrycget(index, "label") == "About TangoPosture"
    )
    help_menu.invoke(about_index)
    root.update()
    window = gui.about_window
    assert window.winfo_ismapped()
    assert gui.about_button.winfo_ismapped()
    assert gui.about_button.cget("text") == "About"
    assert window.winfo_rootx() >= 0 and window.winfo_rooty() >= 0
    assert window.winfo_rootx() + window.winfo_width() <= window.winfo_screenwidth()
    assert window.winfo_rooty() + window.winfo_height() <= window.winfo_screenheight()
    labels = [
        child.cget("text") for child in window.winfo_children()[0].winfo_children()
        if child.winfo_class() == "TLabel"
    ]
    assert "TangoPosture" in labels
    assert "Version 0.1.0" in labels
    assert "License: Apache-2.0" in labels
    assert gui.about_repo_link.winfo_ismapped()
    gui.about_repo_link.event_generate("<Button-1>")
    assert opened == ["https://github.com/derruba2000/TangoPosture"]
    gui.open_about()
    assert gui.about_window is window
    window.withdraw()
    gui.about_button.invoke()
    root.update()
    assert gui.about_window is window and window.winfo_ismapped()


def test_review_can_select_each_recording_in_a_practice_archive(studio, tmp_path):
    root, gui = studio
    archive = tmp_path / "practice" / "Evening.tp"
    video, overlay, lines, report = (
        tmp_path / name
        for name in ("raw.mp4", "overlay.mp4", "overlay_lines.mp4", "report.md")
    )
    import cv2

    for path in (video, overlay, lines):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
        assert writer.isOpened()
        writer.write(np.zeros((64, 64, 3), np.uint8))
        writer.release()
    for index in range(3):
        report.write_text(f"# Recording {index}")
        append_segment(archive, report, video, overlay, [index + 0.2])
    gui.review.load_report(archive)
    root.update()
    assert list(gui.review.segment_paths) == ["Recording 00", "Recording 01", "Recording 02"]
    assert gui.review.report.markdown == "# Recording 2"
    assert tuple(gui.review.video_combo["values"]) == (
        "Practice video",
        "Overlay video",
        "Overlay with lines",
    )
    for index in range(3):
        gui.review.segment_selection.set(f"Recording {index:02d}")
        gui.review.segment_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert gui.review.report.markdown == f"# Recording {index}"
        assert gui.review.player.reader.path.name == f"video_{index:02d}.mp4"
        gui.review.video_selection.set("Practice video")
        gui.review.switch_video()
        assert gui.review.player.reader.path.name == f"video_{index:02d}.mp4"
    gui.review.archive.path.unlink()
    target, raw_path, reason = gui.review._resolve_movement_target()
    assert target is not None and target.kind == "standalone"
    assert raw_path is not None and raw_path.is_file()
    assert reason == ""
    extracted = gui.review.archive.folder
    gui.review.close()
    assert not extracted.exists()


def test_review_opens_calibration_reference_and_its_videos(studio, tmp_path, reference):
    root, gui = studio
    import cv2

    raw, overlay = tmp_path / "raw.mp4", tmp_path / "overlay.mp4"
    for path in (raw, overlay):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
        writer.write(np.zeros((64, 64, 3), np.uint8))
        writer.release()
    archive = tmp_path / "calibrations" / "My reference.tpc"
    save_calibration_archive(archive, reference, raw, overlay, [0.1])
    gui.review.refresh()
    assert str(archive.resolve()) in gui.review.report_paths
    gui.review.load_report(archive)
    root.update()
    assert list(gui.review.segment_paths) == ["Calibration"]
    assert reference.id in gui.review.report.markdown
    assert gui.review.player.reader.path.name == "video_00.mp4"
    gui.review.video_selection.set("Practice video")
    gui.review.switch_video()
    assert gui.review.player.reader.path.name == "video_00.mp4"
    gui.refresh_references()
    assert str(archive.resolve()) in gui.reference_ids.values()
    gui.options["calibration"].set(str(archive))
    gui.match_reference()
    assert gui.options["reference_mode"].get() == "saved"


def test_calibration_camera_has_separate_controls_and_uses_camera(studio, tmp_path, reference, monkeypatch):
    root, gui = studio
    monkeypatch.setattr("tango_posture.gui.verify", lambda _: "verified")
    monkeypatch.setattr("tango_posture.gui.messagebox.showinfo", lambda *args, **kwargs: None)
    monkeypatch.setattr(gui.review, "add_report", lambda _: None)
    received = []
    reference.profile["source"] = "camera:0"
    archive = tmp_path / "calibrations" / "Reference.tpc"

    def run(args, *, display):
        received.extend(args)
        display.show(np.zeros((240, 320, 3), np.uint8))
        display.stopped.wait(3)
        raw, overlay = tmp_path / "raw.mp4", tmp_path / "overlay.mp4"
        raw.write_bytes(b"raw")
        overlay.write_bytes(b"overlay")
        save_calibration_archive(archive, reference, raw, overlay, [0.0])
        display.report_ready(archive)
        return 0

    monkeypatch.setattr("tango_posture.cli.main", run)
    gui.options["source"].set("video")
    gui.options["headless"].set(True)
    gui.calibration_button.invoke()
    deadline = time.monotonic() + 3
    while gui.photo is None and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert "Calibration camera" in gui.preview.title()
    assert "--calibration-only" in received
    assert "--video" not in received and "--headless" not in received
    controls = gui.preview.winfo_children()[0]
    labels = [child.cget("text") for child in controls.winfo_children()]
    assert "Calibrate / retry (C)" in labels
    assert not any("Record" in label or "scoring" in label for label in labels)
    gui.control("q")
    while gui.running and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not gui.running
    assert gui.options["calibration"].get() == str(archive.resolve())
    assert gui.options["reference_mode"].get() == "saved"
    assert gui.options["source"].get() == "camera"
    assert gui.options["headless"].get() is False
    assert gui.options["confirm_camera"].get() is False


def test_coach_abort_and_auto_record_controls(studio, tmp_path, monkeypatch):
    root, gui = studio
    monkeypatch.setattr("tango_posture.gui.verify", lambda _: "verified")
    monkeypatch.setattr("tango_posture.gui.messagebox.askyesno", lambda *args, **kwargs: True)
    notices = []
    monkeypatch.setattr("tango_posture.gui.messagebox.showinfo", lambda *args, **kwargs: notices.append(args))

    def run(args, *, display):
        display.show(np.zeros((240, 320, 3), np.uint8))
        display.stopped.wait(3)
        assert display.aborted.is_set()
        return 0

    monkeypatch.setattr("tango_posture.cli.main", run)
    gui.start()
    deadline = time.monotonic() + 3
    while gui.photo is None and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    gui.auto_record_checkbox.invoke()
    assert gui.display.auto_recording.is_set()
    gui.auto_record_checkbox.invoke()
    assert not gui.display.auto_recording.is_set()
    gui.display.recording_states.put("recording")
    gui.pump()
    assert gui.live_record_button.instate(["disabled"])
    gui.display.recording_states.put("ready")
    gui.pump()
    assert gui.live_record_button.instate(["!disabled"])
    gui.display.recording_states.put("limit")
    gui.pump()
    assert gui.live_record_button.instate(["disabled"])
    assert {"auto_record_start_delay", "auto_record_stop_delay", "max_recordings"} <= set(gui.settings)
    archive = tmp_path / "Earlier.tp"
    archive.write_bytes(b"already saved")
    gui.saved_practice = archive
    gui.control("x")
    while gui.running and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not gui.running and gui.preview is None
    assert "aborted" in gui.status.get()
    assert archive.read_bytes() == b"already saved" and not notices


def test_all_tabs_and_setting_edits(studio):
    root, gui = studio
    for group in gui.group_pages:
        gui.select_group(group)
        root.update()
    assert set(gui.settings) == set(SETTING_MAP)
    gui.settings["camera"].set("2")
    gui.settings["head_down_angle"].set("-22")
    gui.settings["mirror"].set(True)
    cfg = gui.config()
    assert cfg.camera == 2 and cfg.head_down_angle == -22 and cfg.mirror
    gui.settings["axis_curve_clear"].set("0.9")
    with pytest.raises(ValueError):
        gui.config()


def test_markdown_text_has_explicit_contrast_colors(studio):
    _, gui = studio
    text = gui.review.report_view.text
    assert text.cget("foreground") == "#202124"
    assert text.cget("background") == "#ffffff"
    assert text.tag_cget("code", "foreground") == "#202124"


def test_main_window_has_no_duplicate_coach_or_minimize_controls(studio):
    root, gui = studio
    assert not hasattr(gui, "minimize_button")
    assert not hasattr(gui, "record_button")
    assert not hasattr(gui, "stop_button")
    footer_labels = [
        child.cget("text") for child in gui.start_button.master.winfo_children()
        if child.winfo_class() == "TButton"
    ]
    assert footer_labels == ["Open practice coach"]
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
    assert "Tango Posture Coach Manual" in gui.help_view.text.get("1.0", "end-1c")
    assert gui.help_view.text.dlineinfo("1.0") is not None

    gui.help_view.query.set("confidence")
    gui.help_view.search()
    assert gui.help_view.match_count > 0
    assert gui.help_view.text.tag_ranges("search-match")
    assert gui.help_view.text.tag_ranges("search-current")

    target = next(
        node for node, position in gui.help_positions.items()
        if gui.help_outline.item(node, "text") == "Settings explained"
    )
    gui.help_outline.selection_set(target)
    gui.help_outline.event_generate("<<TreeviewSelect>>")
    root.update()
    assert gui.help_view.current_anchor == gui.help_positions[target]
    anchor_start, _ = gui.help_view.anchors[gui.help_positions[target]]
    assert gui.help_view.text.dlineinfo(anchor_start) is not None


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
    gui.settings["tango_bpm"].set("128")
    gui.save_preset()
    assert json.loads(preset.read_text())["practice"]["raw_path"] == "videos/my dance.mp4"
    gui.options["raw_path"].set("other.mp4")
    gui.load_preset()
    assert gui.options["raw_path"].get() == "videos/my dance.mp4"
    assert gui.options["calibration"].get() == "reference-uuid"
    assert gui.options["confirm_camera"].get() is False
    assert gui.config().tango_bpm == 128


def test_toml_import_and_export_ignore_process_environment(
    studio, tmp_path, monkeypatch
):
    _, gui = studio
    settings = tmp_path / "settings.toml"
    settings.write_text("tango_bpm=133\nhead_down_angle=-20\n")
    monkeypatch.setenv("tp_TANGO_BPM", "144")
    monkeypatch.setattr("tango_posture.gui.filedialog.askopenfilename", lambda **_: str(settings))
    monkeypatch.setattr("tango_posture.gui.filedialog.asksaveasfilename", lambda **_: str(settings))
    gui.import_toml()
    assert gui.config().tango_bpm == 133
    gui.save_toml()
    assert tomllib.loads(settings.read_text())["tango_bpm"] == 133


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


@pytest.mark.parametrize("result,save_file", [(0, True), (2, True), (0, False)])
def test_quit_popup_names_the_successfully_saved_practice(studio, tmp_path, monkeypatch, result, save_file):
    root, gui = studio
    notices = []
    monkeypatch.setattr(
        "tango_posture.gui.messagebox.showinfo",
        lambda title, message, **kwargs: notices.append((title, message, kwargs)),
    )
    monkeypatch.setattr(gui.review, "add_report", lambda _: None)
    practice = tmp_path / "Evening practice.tp"
    gui.display = GuiDisplay()
    gui.running = True
    if save_file:
        practice.write_bytes(b"saved practice")
        gui.display.report_ready(practice)
        gui.pump()
        assert gui.saved_practice == practice
        assert not notices
    gui.events.put(("done", result))
    gui.pump()
    if result == 0 and save_file:
        assert len(notices) == 1
        title, message, kwargs = notices[0]
        assert title == "Practice saved"
        assert practice.name in message and str(practice) in message
        assert kwargs["parent"] is root
    else:
        assert not notices


def test_native_preview_worker_and_stop_controls(studio, monkeypatch):
    root, gui = studio
    monkeypatch.setattr("tango_posture.gui.verify", lambda _: "verified")
    received = []
    stopped = threading.Event()

    def run(args, *, display):
        received.extend(args)
        display.notes = "CURRENT OBSERVATION\n\nR record | S score | E stop/save"
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
    assert "R record" in gui.live_notes_sidebar.text.get("1.0", "end-1c")
    assert gui.start_button.instate(["disabled"])
    assert not gui.display.recording.is_set()
    assert gui.live_record_button.instate(["!disabled"])
    assert gui.live_record_button.cget("text") == "Record (R)"
    labels = [child.cget("text") for child in gui.live_record_button.master.winfo_children()]
    assert "Retry (R)" not in labels
    assert "Start scoring (S)" in labels
    assert not any("Calibrate" in label for label in labels)
    gui.live_record_button.invoke()
    assert gui.display.poll_key() == ord("r")
    assert gui.live_record_button.instate(["disabled"])
    assert Path(received[received.index("--config") + 1]).is_file()
    gui.control("p")
    assert gui.display.poll_key() == ord("p")
    gui.control("e")
    assert gui.display.poll_key() == ord("e")
    assert gui.live_record_button.instate(["!disabled"])
    gui.control("r")
    assert gui.display.poll_key() == ord("r")
    gui.control("q")
    while gui.running and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert stopped.is_set() and not gui.running
    assert gui.preview is None
    assert gui.start_button.instate(["!disabled"])
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
    assert panel.video_selection.get() == "Practice video"
    panel.seek_report_time(1.2)
    root.geometry("840x640")
    root.update()
    for button in (panel.player.play_button, gui.start_button):
        assert button.winfo_ismapped()
        assert (
            button.winfo_rooty() + button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
        )
    assert panel.player.reader.index == 2
    assert panel.player.reader.index == 2
    assert panel.player.reader.path == (tmp_path / "raw.avi").resolve()
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
    gui.settings["ollama_base_url"].set("not-a-server-url")
    panel.analyze_button.invoke()
    assert "Ollama URL must be an http(s) server URL" in panel.analysis_status.get()


def test_oversized_report_error_is_visible_and_includes_limit(studio, tmp_path):
    root, gui = studio
    panel = gui.review
    report = tmp_path / "report_over_limit.md"
    report.write_text("x" * 1001)
    panel.load_report(report)
    gui.tabs.select(panel)
    gui.settings["ollama_max_report_chars"].set("1000")
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
    gui.settings["ollama_base_url"].set(url)
    gui.settings["ollama_model"].set("test-model:latest")
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


def test_review_compares_three_views_and_seeks_sidebar_notes(studio, tmp_path):
    import cv2

    root, gui = studio
    raw, overlay, lines = [tmp_path / name for name in
                           ("raw.mp4", "overlay.mp4", "overlay_lines.mp4")]
    for path in (raw, overlay, lines):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
        assert writer.isOpened()
        for index in range(3):
            writer.write(np.full((64, 64, 3), index * 40, np.uint8))
        writer.release()
    overlay.with_suffix(".notes.jsonl").write_text(
        '\n'.join(json.dumps([f"Frame {index} notes"]) for index in range(3)) + '\n'
    )
    report = tmp_path / "report.md"
    report.write_text("# Practice")
    archive = tmp_path / "Compare.tp"
    append_segment(archive, report, raw, overlay, [1, 2, 3])
    gui.review.load_report(archive)
    gui.review.tabs.select(gui.review.video_page)
    root.update()
    assert "Full lines + messages" in gui.review.report.videos
    gui.review.reference_paths["Reference practice"] = archive
    gui.review.reference_combo.configure(values=("Reference practice",))
    gui.review.compare_target_var.set("Reference practice")
    gui.review.show_reference_var.set(True)
    gui.review.on_reference_toggle()
    gui.review.player.seek(0.2)
    assert [reader.index for reader, _ in gui.review.player.comparisons] == [2]
    assert "Frame 2 notes" in gui.review.video_notes.get("1.0", "end")
    gui.review.player.step(-1)
    assert [reader.index for reader, _ in gui.review.player.comparisons] == [1]
    assert "Frame 1 notes" in gui.review.video_notes.get("1.0", "end")
    gui.review.show_reference_var.set(False)
    gui.review.on_reference_toggle()
    assert not gui.review.player.comparisons


def test_setting_labels_open_their_specific_manual_explanation(studio):
    root, gui = studio
    from tango_posture.config import SETTING_GROUPS

    for group, fields in SETTING_GROUPS.items():
        page = gui.group_pages[group]
        for field in fields:
            row = fields.index(field)
            labels = [widget for widget in page.grid_slaves(row=row, column=0)
                      if widget.winfo_class() == "TLabel"]
            assert len(labels) == 1
            label = labels[0]
            assert "\n" not in label.cget("text")
            assert "_" not in label.cget("text")
            label.event_generate("<Button-1>")
            root.update()
            assert gui.help_window.winfo_exists()
            selected = gui.help_outline.selection()
            expected = "Tracking and reference thresholds" if group == "Tracking" else group
            assert gui.help_outline.item(selected[0], "text") == expected
            assert gui.help_view.active_setting == field
            anchor = gui.help_view.setting_anchors[field]
            assert gui.help_view.current_anchor == anchor
            element = gui.help_view.browser.document.getElementById(anchor)
            assert element.getAttribute("class") == "setting-focus"
            highlighted = gui.help_view.text.tag_ranges("setting-focus")
            assert highlighted
            assert field in gui.help_view.text.get(*highlighted)
            assert gui.help_view.text.dlineinfo(highlighted[0]) is not None


def _make_synthetic_movement_estimator(direction_sign: float = 1.0):
    from tango_posture.domain import FrameObservation, Landmark

    class SyntheticEstimator:
        model_hash = "synthetic-gui-test"

        def __init__(self, _model_path):
            self.idx = 0

        def estimate(self, frame_bgr, timestamp: float) -> FrameObservation:
            import math

            h, w = frame_bgr.shape[:2]
            i = self.idx
            self.idx += 1
            yaw = direction_sign * (5.0 * (i - 3) if 3 <= i <= 15 else (0.0 if i < 3 else 60.0))
            rad = math.radians(yaw)
            # Step left foot from x=0.36 to x=0.52 between frames 4..9 and settle 10..24
            if i <= 3:
                lx = 0.36
            elif i <= 9:
                lx = 0.36 + (0.52 - 0.36) * ((i - 3) / 6.0)
            else:
                lx = 0.52

            def lm(x, y, z=0.0):
                return Landmark(x=x, y=y, z=z, visibility=0.95, presence=0.95)

            pose = {
                "nose": lm(0.5, 0.18),
                "left_shoulder": lm(0.43, 0.28),
                "right_shoulder": lm(0.57, 0.28),
                "left_hip": lm(0.44, 0.50),
                "right_hip": lm(0.56, 0.50),
                "left_ankle": lm(lx, 0.84),
                "right_ankle": lm(0.56, 0.84),
                "left_heel": lm(lx, 0.85),
                "right_heel": lm(0.56, 0.85),
                "left_foot_index": lm(lx, 0.89),
                "right_foot_index": lm(0.56, 0.89),
            }
            world = {
                "left_hip": lm(-0.07 * math.cos(rad), 0.0, -0.07 * math.sin(rad)),
                "right_hip": lm(0.07 * math.cos(rad), 0.0, 0.07 * math.sin(rad)),
            }
            return FrameObservation(timestamp=timestamp, width=w, height=h, poses=[pose], world_poses=[world])

        def close(self):
            pass

    return SyntheticEstimator


def test_movement_analysis_two_segments_save_reopen_toggle_switch_and_fade(studio, tmp_path):
    import cv2

    from tango_posture.archive import read_movement_analysis

    root, gui = studio
    panel = gui.review
    panel.movement_coordinator.estimator_factory = _make_synthetic_movement_estimator(1.0)

    archive = tmp_path / "practice" / "DoubleTake.tp"
    raw0, raw1, overlay, report = (
        tmp_path / name for name in ("raw0.mp4", "raw1.mp4", "overlay.mp4", "report.md")
    )
    for path, fill_val in ((raw0, 30), (raw1, 45), (overlay, 120)):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (160, 120))
        assert writer.isOpened()
        for _ in range(25):
            writer.write(np.full((120, 160, 3), fill_val, np.uint8))
        writer.release()

    report.write_text("# Segment 0")
    idx0 = append_segment(archive, report, raw0, overlay, [i / 15.0 for i in range(25)])
    report.write_text("# Segment 1")
    idx1 = append_segment(archive, report, raw1, overlay, [i / 15.0 for i in range(25)])
    assert (idx0, idx1) == (0, 1)

    # Simulate two segments saved in a single coach run; both saved_segments events must be processed
    gui.display = GuiDisplay()
    gui.display.segment_saved(archive, 0)
    gui.display.segment_saved(archive, 1)
    gui.display.report_ready(archive)
    # Calibration archive should be ignored by movement analysis
    cal_archive = tmp_path / "cal.tpc"
    cal_archive.write_bytes(b"cal")
    gui.display.segment_saved(cal_archive, 0)
    gui.pump()

    deadline = time.monotonic() + 5.0
    while (
        read_movement_analysis(archive, 0) is None
        or read_movement_analysis(archive, 1) is None
        or panel.movement_coordinator.is_busy(("archive", str(archive.resolve()), 0))
        or panel.movement_coordinator.is_busy(("archive", str(archive.resolve()), 1))
    ) and time.monotonic() < deadline:
        panel.pump()
        root.update()
        time.sleep(0.02)

    panel.pump()
    root.update()
    assert read_movement_analysis(archive, 0) is not None
    assert read_movement_analysis(archive, 1) is not None

    # Reopen the archive from scratch and verify both segments restore their movement analysis
    panel.load_report(archive)
    root.update()
    assert panel.segment_selection.get() == "Recording 01"
    assert panel.video_selection.get() == "Practice video"
    assert panel.movement_status.get() == "Ready"
    assert panel.movement_button.cget("text") == "Re-run movement analysis"

    # Seek to frame 12 (0.8s, inside pivot & step history) on the Practice video
    panel.player.seek(0.8)
    assert panel.player.reader.index == 12
    # Movement overlays render directly over the Practice video.
    panel.show_pivots_var.set(True)
    panel.show_steps_var.set(True)
    panel.on_movement_toggle()
    root.update()
    assert panel.video_selection.get() == "Practice video"
    assert panel.player.reader.index == 12
    assert not np.array_equal(panel.player.last_image, panel.player.raw_frame)

    # Seek to 1.4s (frame 21, ~0.8s after step at frame 9): with default 5.0s window step is visible;
    # changing replay_step_history_seconds to 0.5s immediately fades out the step without re-analysis
    panel.show_pivots_var.set(False)
    panel.on_movement_toggle()
    panel.player.seek(1.4)
    img_5s = panel.player.last_image.copy()
    assert not np.array_equal(img_5s, panel.player.raw_frame)
    gui.settings["replay_step_history_seconds"].set("0.5")
    root.update()
    img_05s = panel.player.last_image.copy()
    panel.show_steps_var.set(False)
    panel.player.refresh_frame()
    without_steps = panel.player.last_image.copy()
    panel.show_steps_var.set(True)
    panel.player.refresh_frame()
    assert np.array_equal(img_05s, without_steps)


def test_movement_analysis_cancel_preserves_previous_result_and_handles_overlay_only_and_standalone(
    studio, tmp_path, monkeypatch
):
    import cv2

    root, gui = studio
    panel = gui.review
    panel.movement_coordinator.estimator_factory = _make_synthetic_movement_estimator(1.0)

    # 1. Archive with only Overlay (no Raw video) reports "Raw video required"
    overlay_only_archive = tmp_path / "OverlayOnly.tp"
    overlay, report = tmp_path / "overlay.mp4", tmp_path / "report.md"
    writer = cv2.VideoWriter(str(overlay), cv2.VideoWriter_fourcc(*"mp4v"), 15, (160, 120))
    for _ in range(10):
        writer.write(np.zeros((120, 160, 3), np.uint8))
    writer.release()
    report.write_text("# Overlay only")
    append_segment(overlay_only_archive, report, None, overlay, [0.1 * i for i in range(10)])
    panel.load_report(overlay_only_archive)
    root.update()
    assert panel.movement_status.get() == "Raw video required"
    assert panel.movement_button.instate(["disabled"])

    # 2. Standalone clip via Open video supports session-only manual analysis and clears on source change
    standalone = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(standalone), cv2.VideoWriter_fourcc(*"mp4v"), 15, (160, 120))
    for _ in range(25):
        writer.write(np.full((120, 160, 3), 20, np.uint8))
    writer.release()
    monkeypatch.setattr("tango_posture.review_ui.filedialog.askopenfilename", lambda **_: str(standalone))
    panel.open_video()
    root.update()
    assert panel.movement_status.get() == "Not analysed"
    assert panel.movement_button.instate(["!disabled"])
    panel.request_movement_analysis()
    deadline = time.monotonic() + 4.0
    while panel.movement_coordinator.is_busy(("standalone", str(standalone.resolve()), 0)) and time.monotonic() < deadline:
        panel.pump()
        root.update()
        time.sleep(0.02)
    panel.pump()
    root.update()
    assert panel.movement_status.get() == "Ready (session only)"
    assert panel.movement_data is not None

    # Re-run and cancel while slow estimator is running -> previous valid result is preserved
    started = threading.Event()
    release = threading.Event()

    class SlowEstimator:
        model_hash = "synthetic-gui-test"

        def __init__(self, _model_path):
            pass

        def estimate(self, frame_bgr, timestamp: float):
            started.set()
            release.wait(2.0)
            from tango_posture.domain import FrameObservation

            return FrameObservation(timestamp=timestamp, width=160, height=120, poses=[], world_poses=[])

        def close(self):
            pass

    panel.movement_coordinator.estimator_factory = SlowEstimator
    panel.request_movement_analysis()
    assert started.wait(2.0)
    panel.cancel_movement_analysis()
    release.set()
    deadline = time.monotonic() + 2.0
    while panel.movement_coordinator.is_busy(("standalone", str(standalone.resolve()), 0)) and time.monotonic() < deadline:
        panel.pump()
        root.update()
        time.sleep(0.02)
    panel.pump()
    root.update()
    assert "Cancelled" in panel.movement_status.get()
    assert panel.movement_data is not None


def test_reference_video_import_youtube_relink_and_comparison_workflow(
    studio, tmp_path, monkeypatch
):
    import shutil

    import cv2

    root, gui = studio
    panel = gui.review
    est_cls = _make_synthetic_movement_estimator(1.0)
    panel.import_estimator_factory = lambda: est_cls(Path("models/pose_landmarker_lite.task"))

    opened_urls: list[str] = []
    monkeypatch.setattr("tango_posture.review_ui.webbrowser.open", lambda u: opened_urls.append(u))

    # 1. Open Import Reference Video panel; verify URL-only enables Open on YouTube,
    #    Download from YouTube, and Analyse clip
    assert panel.replay_controls_visible is False
    assert panel.replay_options.winfo_manager() == ""
    panel.toggle_import_panel()
    root.update()
    assert panel.replay_controls_visible is True
    assert panel.import_panel_visible is True
    assert panel.import_soundtrack_var.get() is True
    panel.import_youtube_var.set("https://youtu.be/dQw4w9WgXcQ?t=15")
    panel._sync_import_form_state()
    assert panel.import_download_button.instate(["!disabled"])
    assert panel.import_start_button.instate(["!disabled"])
    assert "YouTube" in panel.import_status.get()
    panel.open_import_youtube_link()
    assert opened_urls[-1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=15s"

    # 1b. Test direct YouTube import with blank Local video using injectable downloader
    def _mock_yt_downloader(canonical_url: str, target_path: Path) -> Path:
        w = cv2.VideoWriter(str(target_path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (160, 120))
        for idx in range(36):
            w.write(np.full((120, 160, 3), 30 + idx * 2, np.uint8))
        w.release()
        return target_path

    panel.import_youtube_downloader = _mock_yt_downloader
    panel.import_title_var.set("Direct YouTube Clip")
    yt_direct_res = panel.start_reference_import(synchronous=True)
    root.update()
    assert yt_direct_res.video_path.name == "youtube_dQw4w9WgXcQ.mp4"
    assert panel.report is not None and panel.report.title == "Direct YouTube Clip"
    assert tuple(panel.video_combo["values"]) == (
        "Practice video",
        "Overlay video",
        "Overlay with lines",
    )
    missing_reference = tmp_path / "missing_reference.tp"
    panel.reference_paths["Missing reference"] = missing_reference
    panel.reference_combo.configure(values=("Missing reference",))
    panel.compare_target_var.set("Missing reference")
    panel.guard(panel.show_reference_video)
    assert "Reference file is missing" in panel.status.get()
    assert panel.open_youtube_button.instate(["!disabled"])
    assert panel.open_moment_youtube_button.instate(["!disabled"])
    assert panel.show_coach_var.get() is True
    assert panel.show_pivots_var.get() is True
    assert panel.show_steps_var.get() is True
    assert panel.floor_only_check.cget("text") == "Hide video (gray floor)"
    gui.tabs.select(panel)
    panel.tabs.select(panel.video_page)
    root.update()
    assert panel.floor_only_check.winfo_ismapped()
    assert panel.coach_data is not None
    sidebar_text = panel.video_notes.get("1.0", "end")
    assert "No separate notes for this frame" not in sidebar_text
    assert "Torso axis" in sidebar_text and "Clip Movement Totals" in sidebar_text
    assert panel.player.reader is not None and panel.player.raw_frame is not None
    panel.floor_only_var.set(True)
    panel.video_selection.set("Overlay with lines")
    panel.switch_video()
    assert panel.player.reader.path == yt_direct_res.video_path
    floor_frame = panel._transform_replay_frame(
        panel.player.raw_frame.copy(), panel.player.reader.index, panel.player.reader.fps
    )
    assert not np.array_equal(floor_frame, panel.player.raw_frame)
    assert floor_frame[2, 2].tolist() == [63, 67, 70]
    panel.video_selection.set("Practice video")
    panel.switch_video()
    overlay_path = tmp_path / "direct_youtube_overlay.mp4"
    monkeypatch.setattr(
        "tango_posture.review_ui.filedialog.asksaveasfilename",
        lambda **_: str(overlay_path),
    )
    panel.save_overlay_video_dialog()
    deadline = time.monotonic() + 5.0
    while panel.overlay_export_job and time.monotonic() < deadline:
        panel.pump()
        root.update()
        time.sleep(0.02)
    panel.pump()
    root.update()
    assert overlay_path.is_file(), panel.movement_status.get()
    assert "Saved overlay video" in panel.movement_status.get()
    panel.floor_only_var.set(False)

    # 2. Select local teacher video file (not downloaded from YouTube) and set sub-range [0.2, 2.0)
    teacher_vid = tmp_path / "teacher_demo.mp4"
    writer = cv2.VideoWriter(str(teacher_vid), cv2.VideoWriter_fourcc(*"mp4v"), 15, (160, 120))
    for i in range(36):
        writer.write(np.full((120, 160, 3), 25 + i * 2, np.uint8))
    writer.release()

    panel.import_confirm_same_edit_var.set(False)
    monkeypatch.setattr(
        "tango_posture.review_ui.filedialog.askopenfilename",
        lambda **_: str(teacher_vid),
    )
    panel.open_video()
    root.update()
    assert panel.import_panel_visible is True
    assert Path(panel.import_video_var.get()) == teacher_vid.resolve()
    deadline = time.monotonic() + 5.0
    while panel.import_worker.is_running() and time.monotonic() < deadline:
        panel.pump()
        root.update()
        time.sleep(0.02)
    panel.pump()
    root.update()
    assert panel.coach_data is not None
    assert panel._resolve_movement_target()[0] is not None
    assert panel.show_pivots_var.get() is True
    assert panel.show_steps_var.get() is True
    panel.cut_video_dialog()
    assert panel.cut_dialog is not None
    assert panel.cut_dialog.title() == "Cut video segment"
    assert sum(widget.winfo_class() == "TEntry" for widget in panel.cut_dialog.winfo_children()) == 3
    assert panel.cut_button is not None and panel.cut_button.cget("text") == "Cut video"
    panel.cut_dialog.destroy()
    panel.import_title_var.set("Teacher Giro Clip")
    panel.import_in_var.set("0.2")
    panel.import_out_var.set("2.0")
    panel._sync_import_form_state()
    assert panel.import_start_button.instate(["!disabled"])

    # Run import via background worker and pump until complete
    panel.start_reference_import()
    deadline = time.monotonic() + 5.0
    while panel.import_worker.is_running() and time.monotonic() < deadline:
        panel.pump()
        root.update()
        time.sleep(0.02)
    panel.pump()
    root.update()

    assert panel.report is not None
    assert panel.report.is_imported_reference is True
    assert panel.report.title == "Teacher Giro Clip"
    assert panel.report.clip_range == pytest.approx((0.2, 2.0))
    assert panel.player.clip_range == pytest.approx((0.2, 2.0))
    assert panel.movement_data is not None
    assert panel.open_youtube_button.instate(["!disabled"])
    # Without confirmed alignment, Open this moment on YouTube is disabled
    assert panel.open_moment_youtube_button.instate(["disabled"])

    # 3. Confirm same edit and save YouTube alignment at local 0.5s -> YouTube 30.0s
    panel.import_confirm_same_edit_var.set(True)
    panel.import_align_local_var.set("0.5")
    panel.import_align_youtube_var.set("30.0")
    panel.save_current_youtube_alignment()
    panel.player.seek(1.5)
    panel._sync_import_and_youtube_controls()
    assert panel.open_moment_youtube_button.instate(["!disabled"])
    moment_url = panel.open_current_moment_on_youtube()
    assert moment_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=31s"

    # 4. Move local file, reopen report, and relink with SHA-256 verification
    teacher_report_path = panel.report.path
    moved_vid = tmp_path / "relocated_teacher.mp4"
    shutil.move(str(teacher_vid), str(moved_vid))
    panel.load_report(teacher_report_path)
    root.update()
    assert "Relink video" in panel.status.get()
    outcome = panel.relink_current_video(moved_vid)
    assert outcome.exact_match is True
    assert panel.movement_data is not None

    # 5. Import a second clip (Practice) and run anchored Teacher-vs-Practice comparison
    practice_vid = tmp_path / "student_practice.mp4"
    writer2 = cv2.VideoWriter(str(practice_vid), cv2.VideoWriter_fourcc(*"mp4v"), 15, (160, 120))
    for i in range(36):
        writer2.write(np.full((120, 160, 3), 40 + i * 2, np.uint8))
    writer2.release()
    panel.set_import_video_file(practice_vid)
    panel.import_title_var.set("Student Practice Clip")
    panel.import_in_var.set("0.0")
    panel.import_out_var.set("2.0")
    practice_res = panel.start_reference_import(synchronous=True)

    # Load teacher report and compare against practice report at manual anchors
    panel.load_report(teacher_report_path)
    panel.anchor_left_var.set("0.5")
    panel.anchor_right_var.set("0.6")
    cmp_res = panel.run_clip_comparison(practice_res.report_path)
    root.update()
    assert len(panel.player.comparisons) == 1
    assert cmp_res.left_title == "Teacher Giro Clip"
    assert cmp_res.right_title == "Student Practice Clip"
    assert panel.comparison_summary_var.get() != ""


