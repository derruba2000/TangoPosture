# TangoPosture Baseline Inventory & Regression Audit

**Date:** 12 November 2026
**Commit Baseline:** `df39fbbf10a399d3c6f8ab756632e9780d706e96`
**Target Systems:** macOS (arm64 Apple Silicon) & Windows x64

---

## 1. Existing Module Inventory & Test Baseline

| Module | Purpose | Lines | Unit / Integration Test Coverage |
|---|---|---:|---|
| `config.py` | Pydantic v2 configuration, threshold modeling, TOML import/export | 310 | `tests/unit/test_settings.py` (8 passed) |
| `domain.py` | Landmarks, FrameObservation, Calibration domain models | 102 | `tests/unit/test_geometry_quality.py` |
| `geometry.py`, `quality.py` | Vector geometry, angle/mad metrics, coverage checks | ~300 | `tests/unit/test_geometry_quality.py` (35 passed) |
| `metrics.py`, `temporal.py` | Posture measurements, hysteresis, alert durations | ~400 | `tests/unit/test_temporal_calibration.py`, `test_torso_axis.py` |
| `movement.py` | Pivots, step detection, floor guide rendering | ~500 | `tests/unit/test_movement.py` (14 passed) |
| `beat.py` | Audio capture, tempo detection, beat tracking | ~300 | `tests/unit/test_capture.py` (part) |
| `calibration.py` | Reference capture, stability calculation, profile match | ~250 | `tests/unit/test_temporal_calibration.py` (13 passed) |
| `archive.py` | `.tp` and `.tpc` zip archives, file locking, sha256 checksums | 412 | `tests/unit/test_archive.py` (18 passed) |
| `storage.py` | SQLite local catalog for sessions and calibrations | 165 | `tests/integration/test_storage_session.py` (3 passed) |
| `capture.py`, `display.py`, `overlay.py` | Video capture, Tk GUI display bridge, overlay rendering | ~600 | `tests/unit/test_capture.py` (3 passed) |
| `reference_import.py` | Video import, probe, YouTube / Facebook reel links, clip trim | ~1000 | `tests/unit/test_reference_import.py` (15 passed) |
| `replay.py`, `review.py` | Video replay reader, metadata json, report parsing | ~600 | `tests/unit/test_review.py` (17 passed) |
| `ollama.py` | Local Ollama streaming client and markdown guidance | 150 | `tests/unit/test_ollama.py` (10 passed) |
| `tutorial_ui.py` | Markdown parser, text browser, tutorial viewer | 603 | `tests/unit/test_tutorial.py` (1 passed) |
| `cli.py` | Command line parser (`gui`, `run`, `models`, `sessions`, `calibrations`) | 205 | `tests/unit/test_cli.py` (9 passed) |
| `app.py` | Practice session controller, scoring loop, auto-song recording | 370 | `tests/integration/test_app.py` (27 passed) |
| `gui.py`, `review_ui.py`, `notes_ui.py` | Legacy Tkinter desktop interface | ~4700 | `tests/integration/test_gui.py` |

**Baseline test execution:**
- `pytest tests/unit/`: 177 passed in 5.93s
- `pytest tests/integration/test_storage_session.py tests/integration/test_app.py`: 30 passed in 3.55s
- Total baseline automated assertions: 207 passed.

---

## 2. Invariants & Preservation Rules

1. **Numerical Invariants:** Units (degrees, pixels, ratios, seconds, BPM) and mathematical algorithms in `geometry.py`, `metrics.py`, `movement.py`, `beat.py`, and `temporal.py` must remain completely untouched.
2. **Archive Invariants:** Manifest version 1, `.tp` and `.tpc` ZIP structures with `manifest.json`, `video_*.mp4`, `overlay_*.mp4`, `report_*.md`, `report_*.metadata.json`, and `movement_*.v1.json` are preserved bit-for-bit in compatibility.
3. **No Network by Default:** The application remains offline-first. Ollama and model fetching are explicit user-requested actions.
4. **Desktop Only:** No HTTP server, REST API, or web client.
