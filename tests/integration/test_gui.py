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
    video, overlay, report = (tmp_path / name for name in ("raw.mp4", "overlay.mp4", "report.md"))
    import cv2

    for path in (video, overlay):
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
    for index in range(3):
        gui.review.segment_selection.set(f"Recording {index:02d}")
        gui.review.segment_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert gui.review.report.markdown == f"# Recording {index}"
        assert gui.review.player.reader.path.name == f"overlay_{index:02d}.mp4"
        gui.review.video_selection.set("Raw video")
        gui.review.switch_video()
        assert gui.review.player.reader.path.name == f"video_{index:02d}.mp4"
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
    assert gui.review.player.reader.path.name == "overlay_00.mp4"
    gui.review.video_selection.set("Raw video")
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
    assert gui.live_record_button.instate(["!disabled"])
    assert gui.live_record_button.cget("text") == "Record (R)"
    labels = [child.cget("text") for child in gui.live_record_button.master.winfo_children()]
    assert "Retry (R)" not in labels
    assert "Start scoring (S)" in labels
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
    assert panel.video_selection.get() == "Overlay"
    panel.seek_report_time(1.2)
    root.geometry("840x640")
    root.update()
    for button in (panel.player.play_button, gui.start_button):
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
