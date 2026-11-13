# Stage 1 Implementation Report

Date: 1 November 2026 | Version: 0.1.0 | Target environment: Python 3.11 on macOS arm64

## Qt desktop implementation update

The PySide6 entry point (`uv run tango-posture qt`) now connects Live Practice
to the existing application worker, supports explicit calibration acceptance,
and provides real archive/video Review, synchronized comparison, timestamp
seeking, frame stepping, speed control, on-demand background posture analysis
with synchronized skeleton, metric and alert insights, report-to-Coach handoff,
local video export, archive-contained Analytics charts/events, and an
explicitly invoked Ollama Coach. Settings expose the shared configuration groups,
the navigation rail has a custom vector icon for every destination, and
startup/About/Help provide the desktop experience. Coach responses stream on a worker thread, are cancellable, and
are saved against a SHA-256 report digest; a changed source report is discarded
rather than saved.

Qt is now the only desktop implementation: no-argument startup, `gui`, `qt`,
and the packaged entry point all launch PySide6. Tk-only UI modules, the Tk
replay widget and their integration tests were removed; shared video helpers,
manual rendering, analysis services, saved archives, settings and CLI processing
remain. Packaged smoke checks initialize Qt, and builds exclude Tk modules.

Latest automated verification:

- `QT_QPA_PLATFORM=offscreen uv run --frozen pytest -q`: **330 passed** at the
  first integrated range-profile checkpoint. Tk-only integration tests were
  removed with their UI; Qt-entry-point, no-Tk startup and packaged-smoke
  regressions were added, while shared-engine tests remain.
- `uv run --frozen ruff check .`: passed.
- `uv run --frozen mypy src`: passed across 71 source files.
- `git diff --check`: passed.

Remaining desktop scope and acceptance limits:

- Library catalogues `.tp` practices, `.tpr` reference recordings and `.tpc`
  calibration profiles. Display-name changes
  persist in manifests; confirmed deletion moves archives to Trash. Local and
  YouTube/Facebook Reel imports are Library-only and package matching indexed
  raw, overlay, floor-lines, report and analytics members without changing the
  original media. Legacy migration copies compatible reports/database evidence,
  retains originals, reports failures, and is idempotent for migrated reports.
  Soundtrack analysis is not exposed by the Qt Library.
- Reference imports default to `.tpr`, and practice recordings/excerpts can be
  promoted without changing the original. Legacy baseline migration creates
  `.tpr` copies and retains original `.tpc` files; no user archives are migrated
  automatically. Version-2 `.tpc` files contain only approved ranges/provenance
  and remain usable without source video.
- Video and guided camera calibration share an evidence/range-approval editor:
  numbered dancer selection, explicit track restart anchors, cached measurements,
  video/timeline/histogram, separate examples and acceptable bounds, quantile
  suggestions, coverage, extrema seeking and per-metric approval. Guided holds
  use repeatability heuristics with retry/skip and an optional walking drill.
  Video profiles allow comparison immediately; scoring requires explicit
  per-metric camera approval and compatible capture/model identity.
- Range sessions preserve legacy baseline behavior and use two-sided bounds,
  persistence, unknown/gap accounting, matching units and portable indexed
  analytics. Synthetic tests exercise a complete recorded range session,
  export, Analytics reload and promotion to cached reference evidence.
- Review archive/comparison pickers are restricted to Library entries. The
  comparison-stream selector is removed. Posture/movement reanalysis persists
  to `analytics_XX.json` after verifying the archived raw-video hash. Pivot rings,
  step footprints and lines-only floor rendering retain the shared algorithms.
- Analytics reads each selected `.tp` recording's stored evidence without the
  database. Stored frame measurements remain separate from baseline/range
  evaluation and do not fabricate calibrated coverage.
  Temporal alignment remains unavailable.
- Reference comparison synchronizes by elapsed playback time; it does not
  calculate or claim a semantic/biomechanical alignment.
- Physical camera/microphone validation, >1-hour soak, native accessibility and
  scaling checks, macOS/Windows clean-install smoke, teacher review, and user
  acceptance remain pending. See `docs/manual_validation.md`.

## 1. Milestone Completion Summary

| Milestone | Status | Key Deliverables |
| --- | --- | --- |
| 1. Environment and licences | Completed | `pyproject.toml`, `uv.lock`, `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, `docs/licenses/`, `docs/wheel_compatibility.json`, `models/manifest.json`, `src/tango_posture/models.py`, `src/tango_posture/cli.py` |
| 2. Observation | Completed | `src/tango_posture/capture.py`, `src/tango_posture/pose/constants.py`, `src/tango_posture/pose/mediapipe_backend.py`, `src/tango_posture/overlay.py`, `src/tango_posture/app.py` |
| 3. Measurement | Completed | `src/tango_posture/domain.py`, `src/tango_posture/geometry.py`, `src/tango_posture/quality.py`, `src/tango_posture/metrics.py`, `tests/unit/test_geometry_quality.py` |
| 4. Reference and feedback | Completed | `src/tango_posture/calibration.py`, `src/tango_posture/temporal.py`, `tests/unit/test_temporal_calibration.py` |
| 5. Persistence | Completed | `src/tango_posture/storage.py`, `src/tango_posture/reports.py`, `src/tango_posture/session.py`, `tests/integration/test_storage_session.py`, `tests/integration/test_app.py`, `samples/synthetic/` |
| 6. Validation | Software-complete; hardware & teacher checks pending | `scripts/smoke_video.py`, `docs/runtime_smoke.json`, `docs/manual_validation.md`, `README.md`, `docs/implementation_report.md` |

## 2. Acceptance Criteria Mapping (AC-01 to AC-12)

| ID | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| AC-01 | Clean environment installs pinned dependencies and starts CLI from README instructions. | Passed | Pinned versions in `pyproject.toml` and `uv.lock`; wheel compatibility recorded in `docs/wheel_compatibility.json`; CLI entry point `tango-posture` verified in `tests/unit/test_cli.py` and `dist/` build artifacts. |
| AC-02 | After setup, file analysis runs with network access blocked and no attempted model download. | Passed | `tests/integration/test_storage_session.py::test_roundtrip_accounting_determinism` and `tests/integration/test_app.py::test_real_backend_offline_blank_video` monkeypatch `socket.socket.connect` and `urllib.request.urlopen` to raise on any network call while running the full session and real MediaPipe Pose Landmarker inference on a local video. |
| AC-03 | Known synthetic geometry produces zero/reference, positive-forward, and backward cases; translation and uniform scaling preserve metrics; non-square frames and both facing directions tested. | Passed | `tests/unit/test_geometry_quality.py::test_geometry` (tests `left`/`right` side, `left`/`right` facing, `640x480` and `1280x720` non-square frames) and `tests/unit/test_geometry_quality.py::test_translation_scale_and_rotation`. |
| AC-04 | Display mirroring does not alter metric values or reference deltas. | Passed | `tests/unit/test_geometry_quality.py::test_mirror_only_display` verifies identical `MetricObservation` values with `cfg.mirror = False` and `True`, while `overlay.screen_point` transforms only rendered pixel coordinates. |
| AC-05 | Missing/low-confidence landmarks, degenerate vectors, multiple detected people, and invalid view produce UNKNOWN, never a reassuring state. | Passed | `tests/unit/test_geometry_quality.py::test_head_unknown_independently` (missing ear, low visibility, `None` presence, `NaN`, out-of-bounds, degenerate ear–shoulder segment) and `test_overall_unknown` (zero poses, two poses, frontal width ratio, short/degenerate torso, missing ankles, missing nose, missing hip confidence). |
| AC-06 | Stable reference capture succeeds; insufficient duration or excessive movement rejects calibration with an explanation. | Passed | `tests/unit/test_temporal_calibration.py::test_calibration` verifies 12 s valid capture acceptance and rejection with actionable `ValueError` messages on short duration, high MAD instability, and insufficient timestamp coverage. |
| AC-07 | A 1-second excursion does not activate an event; a continuous 2-second excursion does, with correct onset/detection times across multiple frame rates and irregular timestamps. | Passed | `tests/unit/test_temporal_calibration.py::test_persistence_and_onset` (10, 15, 30, 60 FPS) and `test_irregular_gap_unknown_pause` (irregular timestamps). |
| AC-08 | Hysteresis prevents rapid state chatter; tracking loss and pauses close events without counting missing time as valid. | Passed | `tests/unit/test_temporal_calibration.py::test_transient_hysteresis_and_clear`, `test_irregular_gap_unknown_pause`, and `tests/integration/test_storage_session.py::test_roundtrip_accounting_determinism`. |
| AC-09 | Database and exports agree on timestamps, event counts, missing values, units, and denominator calculations. Zero-valid-data sessions show N/A. | Passed | `tests/integration/test_storage_session.py::test_roundtrip_accounting_determinism` and `test_zero_valid_and_endpoints`; sample output in `samples/synthetic/`. |
| AC-10 | The same fixture and config reproduce metrics/events within documented floating-point tolerances. | Passed | `tests/integration/test_storage_session.py::test_roundtrip_accounting_determinism` asserts exact equality across repeated runs on the synthetic fixture; `docs/manual_validation.md` documents `1e-9` pure-pipeline and `1e-6` same-host inference tolerances. |
| AC-11 | Ctrl+C, GUI quit, EOF, and camera disconnect release resources and preserve completed data with the correct session status. | Passed | `tests/integration/test_app.py::test_shutdown` (`completed` on EOF, `stopped` on `KeyboardInterrupt`, `interrupted` on `OSError`), `test_gui_controls`, and `test_camera_disconnect_status` verify `capture.closed` and `estimator.closed` and persisted session statuses. |
| AC-12 | Model/dependency licence inventory is complete, and the model checksum is verified before use. | Passed | `THIRD_PARTY_NOTICES.md`, `docs/licenses/`, `models/manifest.json`, `tests/unit/test_cli.py::test_manifest_copy`, and `tests/integration/test_app.py::test_model_gate`. |

## 3. Implementation Choices and Material Deviations

1. **MediaPipe version selection (`0.10.31`)**: This release fixes Windows Tasks initialization (`function 'free' not found` in 0.10.30; [upstream issue](https://github.com/google-ai-edge/mediapipe/issues/6187)). Its wheel metadata does not require `opencv-contrib-python`, allowing `opencv-python==5.0.0.93` without overlapping OpenCV packages.
2. **Conservative resolution compatibility**: Section 7 allows different resolutions only with the same aspect ratio and an explicitly documented compatible capture profile. Stage 1 conservatively requires exact resolution (`width` and `height`), `aspect_ratio`, `capture_profile`, `side`, `facing`, and `source` equality in `calibration.compatible()` so a webcam calibration or different frame size is never silently reused without recalibration.
3. **Additional module separation (`models.py`, `session.py`, `pose/constants.py`)**: To keep modules focused and prevent circular imports between CLI, persistence, and the application loop, model verification/fetching lives in `src/tango_posture/models.py`, session lifecycle coordination lives in `src/tango_posture/session.py`, and landmark indices/connections live in `src/tango_posture/pose/constants.py`.
4. **Conservative interval duration accounting**: An inter-frame interval `[t_a, t_b]` counts toward `valid_duration` only when both endpoints are valid and `0 < t_b - t_a <= max_gap` (0.5 s). Any interval touching an `UNKNOWN` observation, exceeding `max_gap`, or lying between session start/end and the first/last valid sample is attributed to `unknown_duration`, ensuring `valid_duration + unknown_duration == active_duration`.

## 4. Untested / Pending Hardware and Teacher Validation

The initial Stage 1 automated unit and integration tests (58 passed), static type checks (`mypy src`), and lint checks (`ruff check .`) pass locally. The following empirical items require a live human subject, physical webcam session, and tango teacher review, and are explicitly recorded as **pending** rather than fabricated:

- **Real dancer video and live webcam benchmark**: `docs/runtime_smoke.json` records real MediaPipe Pose Landmarker inference on a locally encoded 640×480 blank synthetic video (104.6 FPS, 9.09 ms median decode-to-measurement latency on Apple M2 Pro arm64). It is an execution smoke test, not a human pose benchmark or webcam capture-to-overlay latency measurement.
- **Side-view occlusion and clothing coverage**: Empirical coverage of the far-side shoulder/hip confidence gate (`>= 0.70`) during real side-on tango walking and in loose clothing remains to be measured via `scripts/smoke_video.py` and live sessions as documented in `docs/manual_validation.md`.
- **Teacher validation of reference postures and thresholds**: Default thresholds (`+0.08` / `+0.05` torso lengths for head-forward offset, `10°` / `6°` for head inclination, `8°` / `5°` for trunk inclination) are experimental engineering defaults awaiting teacher-annotated clip refinement.


## 5. Verification scope and handover additions

A separate clean Python 3.11 environment was created with `uv sync --frozen`, and its installed `tango-posture --help` command succeeded. The model was fetched explicitly and its SHA-256 verified. The wheel and source distribution built successfully.

AC-02 evidence blocks Python socket and URL calls; it is not an operating-system firewall audit of native libraries. AC-10 repeated-inference tolerance is a documented validation target; the executed deterministic comparison uses synthetic landmarks. AC-11 GUI/device controls were simulated in tests, while the file codec and blank-video inference path used real resources. Physical webcam permission, disconnect, and GUI interaction still require the manual protocol. These limits qualify the automated “Passed” statuses above.

The unified user and architecture manual is [TANGO_POSTURE_GUIDE.md](TANGO_POSTURE_GUIDE.md). It includes the practice and comparison workflow, all 58 GUI settings with defaults and beginner examples, technical boundaries, storage, privacy, and validation limits. The desktop app bundles this document in its searchable **Help** menu.

The final documentation-time check of the then-current workspace reported **63 tests passed**, Ruff clean, mypy clean across 20 source files, and successful wheel/source builds. Additional beat/recording/head/arm/report features appeared during the work; they are documented separately in the manual and remain outside the original three-metric validation claims. Passing software tests does not establish dancer accuracy or teacher validation for those additions.
