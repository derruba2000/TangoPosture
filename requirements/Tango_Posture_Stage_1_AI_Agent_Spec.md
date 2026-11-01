# Tango Posture Coach — Stage 1 Implementation Specification

Version: 1.0 | Date: 1 November 2026 | Deliverable: working local Python application

## 1. Instruction to the implementing AI agent

Implement this specification as a runnable repository, not merely a scaffold or design document. Work through the milestones in order. Use reasonable implementation choices within the requirements and record material deviations. Do not add later-stage features to compensate for incomplete core functionality.

The owner is an experienced data engineer and Argentine tango dancer. The first goal is a practical personal training aid: use a computer webcam to observe one dancer's head and trunk alignment relative to a deliberately recorded reference. The application must explain what it measures and abstain when the image does not support a measurement.

All mandatory runtime components must be open source and run locally. No proprietary service, paid API, account, API key, hosted inference, LLM, or cloud database may be required. After dependency and model installation, the application must run with the network disconnected.

This is a movement observation tool, not a medical assessment or an authority on correct tango technique. Never describe its measurements as spinal curvature, eye gaze, balance, weight distribution, or dance quality.

## 2. Stage 1 boundaries

### Required

- One person, fixed camera, side-on view, standing and simple solo walking while remaining side-on.
- Explicit choice of visible anatomical side and screen-facing direction.
- Webcam preview, skeleton overlay, reference calibration, session controls, and local summary.
- Three measurements: head-forward offset, head segment inclination relative to trunk, and trunk inclination relative to image vertical.
- Persistent-deviation feedback relative to the selected reference.
- Confidence and view-quality gating, temporal smoothing, and missing-data handling.
- Local SQLite storage and JSON/CSV exports.
- Local video-file analysis using the same measurement pipeline, for reproducibility and development without a webcam.
- Tests using synthetic landmarks, plus documented real-video and webcam validation.

### Explicitly excluded

- Couples, embrace assessment, multiple-person tracking, turns, ochos, pivots, and choreography classification.
- Frontal-view scoring, clinical diagnosis, spinal shape estimation, or literal detection of looking at the floor.
- YouTube downloading, reference-video libraries, custom model training, automatic teacher judgements.
- Voice coaching, mobile apps, hosted dashboards, accounts, remote storage, cloud inference.
- A single overall posture score or a claim that a universal ideal posture exists.

Design small extension points for additional metrics and pose backends; do not implement a general agent framework or plugin platform.

## 3. Technology and licence requirements

Use Python 3.11 as the initial compatibility target. Verify current package wheels for the tested OS and architecture before pinning versions. Use a conventional src-layout package, pyproject.toml, uv.lock, and a CLI entry point named tango-posture.

| Purpose | Initial choice |
| --- | --- |
| Webcam/file capture and desktop display | opencv-python |
| Body landmarks | MediaPipe Tasks Pose Landmarker |
| Numerical operations | NumPy |
| Typed configuration | Pydantic, with TOML parsed by the standard library |
| Storage | Standard-library sqlite3 |
| CLI | argparse |
| Tests and quality | pytest, Ruff, mypy |

Avoid adding SciPy, pandas, web frameworks, or deep-learning frameworks unless a demonstrated requirement needs them. Do not use deprecated MediaPipe examples when the Tasks API supplies the required functionality.

Use VIDEO mode for both video files and sequential webcam frames initially, with strictly increasing timestamps. Keep MediaPipe behind a PoseEstimator interface. Configure detection of up to two poses as a guard against another person entering; this is not couple support. Only proceed when exactly one pose is detected. Document that the guard cannot guarantee discovery of every background person.

Licence gate, completed before the model is adopted:

1. Record exact dependency versions, SPDX identifiers where available, and upstream licence links in THIRD_PARTY_NOTICES.md.
2. Record the exact model file, source URL, SHA-256, version, separate weights licence, and model-card link in models/manifest.json.
3. Do not assume the repository licence also covers weights or training data. Record unknown provenance or restrictions explicitly.
4. Select a model with documented terms compatible with local use and the intended distribution. Do not bundle unidentified, noncommercial-only, or otherwise incompatible weights as a claimed open-source solution.
5. If the preferred weights cannot pass this gate, record the blocker and evaluate a compatible alternative behind the same interface. Do not silently use a proprietary fallback.

An explicit models fetch command may use the network during setup. Verify the manifest checksum before installing the model; report mismatch and stop. Normal run commands must never download automatically. Accept an explicit local model path for offline installation. Preserve required notices and use Apache-2.0 for original application code, subject to dependency compatibility.

## 4. User workflow and interface

Provide a native OpenCV window; a web interface is not required.

1. User runs the webcam command and selects visible side (left/right) and facing direction (screen-left/screen-right).
2. Show setup guidance: fixed, approximately level camera; enough distance to see the whole body; adequate light; uncluttered background; fitted clothing where practical; side-on orientation.
3. Preview shows landmarks, image-quality status, and the currently selected reference. No evaluation runs without a usable reference.
4. User presses C to calibrate, after adopting their intended reference position. Give a 3-second countdown followed by a 12-second capture.
5. Show calibration results, quality, and an explicit accept/retry choice. Label the reference “self-selected” unless a user explicitly records that a teacher reviewed it; never infer review.
6. User presses S to start a session, P to pause/resume, E to end and save, and Q to quit cleanly. Quit during an active session saves a partial summary with an explicit stopped status.
7. Show a summary and export paths after ending.

Overlay: elapsed active time, processed FPS, selected side/direction, reference name, per-metric current value and reference delta, and a maximum of one prominent coaching message. Priority: quality problem, then head-forward, head inclination, trunk inclination. All metric events are still logged.

Use text and symbols as well as colour. States: READY, CALIBRATING, RUNNING, PAUSED, FINISHED; measurement states are separate: WITHIN_REFERENCE, PENDING, DEVIATION, UNKNOWN. “Within reference” does not mean medically or technically correct.

Examples: “Head farther forward than reference”; “Head angle differs from reference”; “Trunk angle differs from reference”; “Cannot assess: ear not visible”; “Return to the selected side-on view.” Never display “Bad posture”, “Straighten your spine”, or “You are looking at the floor”.

Mirror only the display if requested. All calculations and saved coordinates use the original unmirrored image. Transform overlay coordinates accordingly and explain facing-direction selection using the unmirrored preview during setup.

## 5. Landmark geometry and metrics

Use selected-side ear E, shoulder S, hip H, plus both shoulders and hips for a side-view quality heuristic. Use MediaPipe's documented landmark mapping in one named constants module; do not scatter numeric indices.

Convert normalised image coordinates to pixel geometry before calculations: p=(x*width, -y*height). The coordinate system is x rightwards, y upwards. Do not compute image-plane angles directly from independently normalised x/y coordinates, which distorts them for non-square frames. Do not use monocular world coordinates as calibrated physical measurements.

Define:

- L = norm(S-H), the projected torso length.
- t = (S-H)/L, the upwards trunk unit vector.
- f = +1 for facing screen-right, -1 for facing screen-left.
- n = f*(t_y, -t_x), the trunk-perpendicular vector toward the selected front.
- h = E-S, the shoulder-to-ear vector.

Require finite coordinates, sufficient segment lengths, and quality checks before computing any metric. All angles use degrees and atan2; handle differences using wrapped angular differences.

| Metric | Formula | Event direction | Interpretation |
| --- | --- | --- | --- |
| Head-forward offset | dot(h,n)/L | Positive increase over reference | Ear farther forward relative to shoulder and trunk |
| Head segment inclination | degrees(atan2(dot(h,n),dot(h,t))) | Absolute change from reference | Ear–shoulder segment orientation relative to trunk |
| Trunk inclination | degrees(atan2(f*t_x,t_y)) | Absolute change from reference | Projected trunk orientation relative to camera vertical |

Head segment inclination is a posture proxy, not an anatomical neck-flexion or facial-pitch measurement. It does not prove the head is tilted down. Offset and inclination are correlated; present them separately, do not sum them into a score.

Initial engineering defaults, not established tango or clinical thresholds:

- Head-forward offset: enter at +0.08 torso lengths; clear below +0.05.
- Head segment inclination: enter at absolute 10 degrees; clear below 6 degrees.
- Trunk inclination: enter at absolute 8 degrees; clear below 5 degrees.

Expose thresholds in config, persist them with each session, and label them experimental. Threshold selection must be refined with a teacher and recorded clips before making performance claims.

## 6. Quality gating

Per frame, require exactly one detected pose, body framing adequate for the exercise, and all metric-required points in bounds and above minimum visibility and presence scores of 0.70 when supplied by the backend. Missing required confidence must be handled explicitly by the adapter, not assumed to equal 1.0.

Minimum torso length: 0.12 * image height. Minimum shoulder-to-ear length: 0.04 * image height. Treat zero, NaN, and degenerate vectors as invalid. Check full-body framing with nose and at least one confidently visible ankle; do not require the occluded far ankle.

Use a conservative side-on heuristic when both shoulder/hip pairs are reliable: max(projected shoulder width, projected hip width)/L <= 0.45. This is an adjustable starting heuristic, not a reliable orientation classifier. When evidence is insufficient to check the view, mark view quality UNKNOWN and abstain. Report resulting side-view coverage during validation; do not bypass the gate to improve apparent accuracy.

Use calibration to retain the expected view-width range and selected-side geometry. Detect obvious view changes; require the user to keep the same facing direction. Do not claim robust automatic detection of a complete turn from a monocular skeleton. Such movement is outside supported exercises.

If more than one pose is detected, show “One-person mode only” and suspend all evaluation. Low-confidence frames never count as good posture. Mark unavailable metrics UNKNOWN individually, or all metrics UNKNOWN when the overall view is invalid.

## 7. Calibration and temporal evaluation

Calibration requires at least 9 seconds of valid observations in the 12-second window and at least 60 valid samples. Use timestamp-based duration, not a presumed frame rate. Reject unstable calibration if median absolute deviation exceeds 0.03 for offset or 4 degrees for either angle. These are configurable engineering defaults.

Save median per-metric values, dispersion, valid coverage, image resolution/aspect ratio, selected side/direction, model checksum, metric version, view-quality baseline, and optional teacher-review metadata. Reference capture must not itself generate alerts.

Invalidate reference compatibility on model or metric-version changes, selected-side/direction changes, or a changed camera/capture profile. Always ask the user to confirm unchanged physical camera placement when reusing a reference: camera movement cannot be reliably detected from metadata. Recalibration is recommended at every new setup. Different resolutions may only be accepted with the same aspect ratio and an explicitly documented compatible capture profile.

Smooth each valid metric with a time-aware exponential filter: alpha=1-exp(-dt/0.25). Initialise from the first valid value. Use source timestamps for video files and monotonic capture timestamps for webcam input.

For each metric, start a deviation only after the entry threshold is exceeded continuously for 1.5 seconds. Clear an active deviation after values stay below the clear threshold for 0.75 seconds. Between thresholds, retain the current stable state. A brief return below entry before activation resets the pending entry timer.

Any invalid observation immediately displays UNKNOWN, resets pending timers, and closes an active event at the last valid observation with reason tracking_lost. Do not interpolate or bridge an event through missing data. Reset smoothing after a gap longer than 0.5 seconds. Pausing closes active events with reason paused and resets temporal state; paused time is excluded from session duration. Ending closes events with reason session_ended.

Use a maximum inter-frame continuity gap of 0.5 seconds. Larger capture gaps behave as tracking loss, even if the next detection is valid. Never let dropped frames produce artificial persistence.

Store event onset as the first threshold-exceeding timestamp in the qualifying run and detection time separately. Avoid repeated pop-ups; the active overlay is sufficient.

## 8. Architecture and execution

Suggested modules:

```text
src/tango_posture/
  cli.py, config.py, domain.py
  capture.py, pose/mediapipe_backend.py
  geometry.py, quality.py, metrics.py
  calibration.py, temporal.py
  storage.py, reports.py, overlay.py, app.py
tests/
  unit/, integration/, fixtures/
configs/default.toml
models/manifest.json
docs/manual_validation.md
README.md, THIRD_PARTY_NOTICES.md, LICENSE
pyproject.toml, uv.lock
```

Keep geometry, metrics, calibration statistics, and temporal evaluation independent of camera APIs and GUI calls. Define typed records for FrameObservation, Landmark, QualityResult, MetricObservation, Calibration, DeviationEvent, and SessionSummary. Use a PoseEstimator protocol so tests can inject recorded landmarks.

Webcam capture may use a producer thread and a queue of size one: discard stale frames instead of building latency. Use one inference owner; do not call the same detector concurrently. Video-file mode processes every decodable frame sequentially. Release capture, detector, window, and database resources on normal exit, exceptions, and Ctrl+C.

For files, use presentation timestamps when available and valid; otherwise use frame_index / declared_fps, recording the fallback. Fail clearly if neither gives reliable timing. Inference timestamps must be strictly increasing; handle duplicate millisecond timestamps in the adapter without corrupting source-time statistics.

Expected commands (exact flags may be refined but preserve capabilities):

```bash
uv sync --frozen
uv run tango-posture models fetch
uv run tango-posture run --camera 0 --side left --facing right
uv run tango-posture run --video ./practice.mp4 --calibration REF_ID
uv run tango-posture run --video ./practice.mp4 --calibration REF_ID --headless
uv run tango-posture sessions list
uv run tango-posture sessions export SESSION_ID --output ./exports
uv run pytest
uv run ruff check .
uv run mypy src
```

Headless file mode requires an existing compatible reference, or an explicit reference interval supplied through CLI; no GUI prompt may block headless execution. Reference intervals are excluded from practice summary statistics.

## 9. Configuration and persistence

Use validated TOML with CLI overrides. Include camera index, requested capture resolution, model path, data directory, selected side/direction, all quality and metric thresholds, smoothing duration, persistence timings, and display mirror setting. Reject inconsistent clear/entry thresholds, negative durations, and invalid confidence values. Record the resolved config, not merely its path.

SQLite schema, with migration version:

- calibrations: UUID, created_at_utc, name, profile JSON, baseline JSON, quality JSON, model hash, metric version, review metadata.
- sessions: UUID, calibration FK, start/end UTC, source type, status, active duration, resolved config JSON, model/runtime versions, summary JSON.
- observations: session FK, source timestamp, metric name, raw/smoothed/delta values (nullable), quality state/reason, evaluation state; composite index on session/time.
- events: UUID, session FK, metric name, onset, detection, end, peak deviation, end reason.

Store processed observations at their actual timestamps using batched transactions. Do not store raw video, audio, images, or full landmark streams by default. No raw-video recording is required in Stage 1. Uploaded local videos remain where the user put them; do not copy them silently. Logs should avoid unnecessary personal names and full source paths.

Outputs: summary.json, observations.csv, events.csv. Export schema/version, units, model hash, configuration, and reference identity. Represent missing values as JSON null or blank CSV cells, never zero. Use UTF-8 and consistent decimal formatting.

Summary per metric: valid duration, unknown duration, valid coverage, time above entry threshold, confirmed-event duration, event count, longest event, median delta, and peak absolute delta (plus peak positive delta for head-forward). Include active time and pause duration separately. Percentages use valid time as denominator and explicitly display that denominator; show N/A when it is zero. Overlapping events across metrics must not be summed as unique session time.

Duration accounting must be timestamp-based, exclude pauses and calibration, and conservatively attribute long inter-frame gaps to unknown time. Report unobserved beginning/end time as unknown. Add tests verifying valid + unknown = active duration within timestamp precision.

## 10. Failure handling and performance

Provide actionable messages for missing model/checksum mismatch, camera permission denial, device unavailable, unreadable file, invalid config, incompatible reference, disk write failure, and absent landmarks. Do not silently switch cameras or models. A camera disconnect ends the session as interrupted; no stale frame may continue generating feedback.

Aim for at least 15 processed FPS and median capture-to-overlay latency below 200 ms at 640x480 using a lightweight model on a recent laptop CPU. These are benchmark targets, not guaranteed capabilities. Report hardware, OS, model, resolution, FPS, median/p95 latency, and dropped-frame count. Do not fabricate benchmark results when no hardware is available. Keep a responsive stop action even under slow inference.

## 11. Tests and acceptance criteria

Automated tests must exercise meaningful behaviours, not simply reproduce implementation formulas in test code.

| ID | Acceptance criterion |
| --- | --- |
| AC-01 | A clean environment can install pinned dependencies and start the CLI from README instructions. |
| AC-02 | After setup, file analysis runs with network access blocked and no attempted model download. |
| AC-03 | Known synthetic geometry produces zero/reference, positive-forward, and backward cases; translation and uniform scaling preserve metrics. Test non-square frames and both facing directions. |
| AC-04 | Display mirroring does not alter metric values or reference deltas. |
| AC-05 | Missing/low-confidence landmarks, degenerate vectors, multiple detected people, and invalid view produce UNKNOWN, never a reassuring state. |
| AC-06 | Stable reference capture succeeds; insufficient duration or excessive movement rejects calibration with an explanation. |
| AC-07 | A 1-second excursion does not activate an event; a continuous 2-second excursion does, with correct onset/detection times. Test multiple frame rates and irregular timestamps. |
| AC-08 | Hysteresis prevents rapid state chatter; tracking loss and pauses close events without counting missing time as valid. |
| AC-09 | Database and exports agree on timestamps, event counts, missing values, units, and denominator calculations. Zero-valid-data sessions show N/A. |
| AC-10 | The same fixture and config reproduce metrics/events within documented floating-point tolerances. |
| AC-11 | Ctrl+C, GUI quit, EOF, and camera disconnect release resources and preserve completed data with the correct session status. |
| AC-12 | Model/dependency licence inventory is complete, and the model checksum is verified before use. |

Use synthetic landmark sequences for deterministic unit/integration tests. Add at least one rights-cleared real video for an actual inference smoke test, or document a reproducible user-supplied fixture command when redistribution is not possible. Mock-only tests do not demonstrate webcam or pose-model performance.

Manual validation, recorded in docs/manual_validation.md:

- Reference stance and side-on solo walking.
- Deliberate forward head movement, changed head-segment angle, and changed trunk angle.
- Transient movements shorter than the persistence threshold.
- Turn toward camera, leave frame, obscure ear, poor lighting, second person entering, and camera movement.
- Repeat on both visible sides and facing directions; include loose clothing if available.
- Ask a tango teacher to annotate reference suitability and observable deviations before calling the feedback useful for technique.

Record coverage, false alerts, missed observable deviations, examples of abstention, hardware performance, and limitations. The prototype may be software-complete while teacher validation remains pending; label these separately. Never invent clinical accuracy, teacher approval, or a successful webcam run.

## 12. Implementation milestones and handover

1. Environment and licences: repository, lockfile, model manifest/fetch, CLI help, compatibility notes.
2. Observation: webcam and file capture, landmarks, quality overlay, graceful shutdown.
3. Measurement: pure geometry, per-metric quality gates, synthetic tests.
4. Reference and feedback: calibration, temporal rules, controls, reference compatibility checks.
5. Persistence: SQLite, session summary, exports, deterministic headless integration tests.
6. Validation: real-model smoke test, webcam/manual checks where available, performance report, README and known limitations.

Deliver a complete repository with installation and usage instructions, default configuration, tests, licence/model records, sample exports from a clearly labelled test run, and a concise implementation report mapping AC-01 through AC-12 to evidence. List any blocked or untested acceptance criteria explicitly. Do not include personal videos in source control.

Definition of done: the user can install, obtain a verified compatible model, calibrate a reference, run a solo side-view session offline, see confidence-aware feedback, stop safely, and inspect locally saved results. All automated acceptance checks pass; hardware-dependent and teacher-dependent checks are reported honestly.

## 13. Upstream starting points

These are documentation entry points, not a substitute for checking the exact versions and model licences selected during implementation.

- MediaPipe source and licence: https://github.com/google-ai-edge/mediapipe
- Pose Landmarker Python guide: https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/python
- OpenCV: https://github.com/opencv/opencv
- NumPy: https://github.com/numpy/numpy
- Pydantic: https://github.com/pydantic/pydantic
- uv: https://github.com/astral-sh/uv
- MMPose, only if an alternative backend is needed: https://github.com/open-mmlab/mmpose

Future stages may add teacher-labelled reference clips, frontal views, and couple analysis. They are not acceptance requirements for this implementation.
