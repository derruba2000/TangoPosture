# Tango Posture Coach — Stage 1

A local Python desktop tool for observing one dancer, side-on, relative to an explicitly recorded reference. It measures projected ear–shoulder–hip geometry, abstains when the image is inadequate, and saves local session summaries. No account, API key, hosted inference, or network connection is needed after installation and model setup.

**Experimental movement observation, not medical assessment or a judgement of correct tango technique.** There is no overall score. “Within reference” only describes similarity to your reference. Head segment inclination does not establish neck flexion, facial pitch, or whether someone is looking down. This app does not measure spinal shape, eye gaze, balance, weight distribution, or dance quality.

For a detailed dancer-facing and technical explanation, read the [extensive Argentine tango practice guide](docs/TANGO_POSTURE_GUIDE.md).

## Install

Tested on macOS 26.6.2 / Apple M2 Pro arm64, Python 3.11.15. Other operating systems are not validated. Install [uv](https://github.com/astral-sh/uv), then from this repository:

```bash
uv python install 3.11
uv sync --frozen
uv run tango-posture --help
uv run tango-posture models fetch
```

`models fetch` is an explicit setup operation. It downloads only the versioned model in `models/manifest.json`, verifies SHA-256, and atomically installs it. Runs never download models. For offline installation, transfer that exact verified file yourself and pass `--model-path /path/to/pose_landmarker_lite.task`. Every run verifies its checksum. Copy the Python environment/dependency wheels beforehand if installing on an offline machine.

The pinned MediaPipe 0.10.30 release supports the Tasks API without pulling in a second OpenCV package. Newer releases inspected during implementation depended on `opencv-contrib-python`, which conflicts with the requested `opencv-python` namespace. See [wheel evidence](docs/wheel_compatibility.json), [third-party notices](THIRD_PARTY_NOTICES.md), and the [model manifest](models/manifest.json). Original application code is Apache-2.0; dependency and model terms remain separate.

## Webcam workflow

```bash
uv run tango-posture run --camera 0 --side left --facing right
# Optional: explicitly record raw video and annotated overlay into videos/
uv run tango-posture run --camera 0 --side left --facing right \
  --record-video videos/practice.mp4 --record-overlay videos/overlay.mp4
```

Choose the anatomical side visible to the camera, and the direction the dancer faces **in the unmirrored image**. The setup preview is always unmirrored. `--mirror` changes only the display after V, C, or S confirms setup; saved measurements always use original image geometry.

Use a fixed, approximately level camera, with the whole body visible, adequate lighting, an uncluttered background, and fitted clothing where practical. Stand and walk side-on without turning or changing facing direction. On macOS, grant camera access to the terminal/application that launches Python in System Settings → Privacy & Security → Camera.

| Key | Action |
| --- | --- |
| C | Adopt your reference; 3-second countdown, then 12-second capture |
| A | Accept the displayed successful calibration |
| R | Retry calibration |
| S | Start a session with the accepted reference |
| P | Pause/resume; pause time is excluded |
| B | Sync Argentine Tango Beat 1 / tap tempo (8-beat phrase counter `1..8`) |
| E | End, save, and display a summary; S can start another session |
| Q / window close / Ctrl+C | Quit; save active session as `stopped` |
| V | Confirm unmirrored facing direction and enable requested display mirroring |

Calibration requires at least 9 valid seconds and 60 valid samples. Rejected captures explain missing coverage or excessive median absolute deviation. A reference is labelled **self-selected** unless you explicitly supply `--teacher-reviewed 'review note'`. This flag records your statement; it is not inferred approval. Use `--reference-name 'Practice stance'` to name it.

The overlay prioritises one quality message, then head-forward, head inclination, and trunk inclination. Every metric event is logged independently. Missing ear observations can invalidate head metrics while leaving the trunk metric available. Inadequate framing, unreliable view evidence, or more than one detected pose invalidates all metrics. Detecting up to two poses is a guard, not a guarantee that every background person will be found.

## Video and reference reuse

Headless analysis uses every decodable frame sequentially. Supply a local video with a stable 12-second reference interval, followed by supported solo movement:

```bash
uv run tango-posture run --video videos/practice.mp4 --side left --facing right \
  --reference-interval 0 12 --reference-name 'Recorded stance' --headless
uv run tango-posture calibrations list
uv run tango-posture run --video videos/practice.mp4 --calibration REF_ID \
  --confirm-camera-unchanged --headless
```

Supplying `--reference-interval` in headless mode explicitly accepts the interval **if** calibration checks pass. The interval and all preceding preview time are excluded from practice statistics. In GUI video mode, use A to accept the interval, then S to start. A GUI run without an interval also supports C/R. Video GUI playback runs at processing speed, not real time.

When reusing a reference, always confirm unchanged physical camera placement. Interactive runs ask in the terminal unless `--confirm-camera-unchanged` is supplied; headless/noninteractive runs require the flag. Camera motion cannot be detected reliably from metadata. Recalibrate for every new setup. Exact resolution, capture profile, source kind/device, anatomical side, facing direction, model hash, and metric version must match. Stage 1 conservatively rejects different resolutions, even at the same aspect ratio. A webcam reference is not automatically portable to a file capture.

## Configuration and output

```bash
uv run tango-posture --config configs/default.toml --data-dir ./data run --camera 0
uv run tango-posture sessions list
uv run tango-posture sessions export SESSION_ID --output ./exports
```

Global `--config` and `--data-dir` flags precede the subcommand. TOML must explicitly specify side and facing when used instead of those CLI flags. CLI values override TOML. Copy [configs/default.toml](configs/default.toml) to adjust confidence, minimum segment sizes, side-width heuristic, smoothing, calibration stability, and event timings/thresholds. Invalid or unknown settings are rejected. Resolved values are stored with each session.

Default thresholds are engineering starting points: offset enter +0.08 / clear +0.05 torso lengths; head inclination 10° / 6°; trunk inclination 8° / 5°. Entry requires 1.5 seconds; clearing requires 0.75 seconds. They need teacher-labelled clip validation before performance claims.

Data lives in `data/tango.sqlite3` (schema version 1); automatic exports are in `data/exports/SESSION_ID/`:

- `summary.json`: reference, model/runtime versions, units, resolved config, timing, coverage, per-metric statistics and runtime measurements.
- `observations.csv`: actual source timestamps, raw/smoothed/delta values, confidence/view status, reason, and evaluation state.
- `events.csv`: event onset, detection, end, peak deviation, and closure reason.

Missing measurements are JSON null / empty CSV fields. A zero-valid-time percentage is null and displayed as N/A. Percentages explicitly use **valid seconds per metric** as denominator. Metrics overlap; their event durations must not be added to obtain unique session time. Source timestamps are seconds from the video timeline or monotonic webcam capture origin; UTC start/end fields describe the analysis run.

Raw images, audio, video, and full landmark streams are not stored by default (use `--record-video` or `--record-overlay` to explicitly save video locally). Input videos are not copied. Names and teacher notes you supply are stored locally. Completed observations are committed in batches of 180 rows; normal exit, Ctrl+C, and exceptions flush pending rows and save a final summary. A forced process kill or power loss can leave a `running` session and lose the last batch; automatic crash recovery is not implemented. Disk failures are reported, and no successful save is claimed after a failure.

## Measurement and timing

Landmarks are converted to `(x * width, -y * height)` pixels before calculating angles. Head-forward is shoulder-to-ear displacement along the front-facing trunk perpendicular, divided by torso length. Head inclination is the ear–shoulder segment angle relative to the trunk; trunk inclination is relative to image vertical. Angle differences and smoothing wrap across ±180°. No monocular world coordinates are treated as physical measurements.

A time-aware exponential filter uses a 0.25-second time constant. Any invalid frame resets pending persistence and closes an active event at the last valid timestamp (`tracking_lost`). Gaps above 0.5 seconds also reset smoothing and break event continuity. Pausing resets all temporal state. Event onset records the start of the qualifying run; detection records when persistence was satisfied.

Durations are conservative: an interval counts as valid only if **both endpoints** are valid and its length is at most 0.5 seconds. All other active time, including unseen beginning/end portions, is unknown. Above-threshold duration uses the left endpoint of a valid interval; confirmed event durations intersect those intervals. Median/peak delta are observation-sampled, not time-weighted. File presentation timestamps are used when valid; a recorded FPS fallback is locked in after timestamps fail, with a continuity offset if required. Decoder metadata that provides neither usable timing source is rejected. Duplicate millisecond inference timestamps are advanced only inside the adapter; statistics retain source timestamps.

The webcam producer holds one latest frame and drops stale frames. The GUI polls quit while a single inference worker runs. Native inference already in progress must finish before its detector is released. Benchmark targets of 15 FPS / median latency below 200 ms have **not** been established on a real dancer/webcam. Runtime reports are measurements of the current run, not accuracy claims.

## Verify and validate

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Tests cover synthetic geometry, confidence gates, calibration, persistence/hysteresis, duration accounting, storage/exports, CLI safeguards, resource cleanup, and real-model inference on generated blank video when the model is installed. Blank video is not human pose validation. [Sample exports](samples/synthetic/) are explicitly synthetic.

For real-video inference without retaining any frames:

```bash
uv run python scripts/smoke_video.py videos/practice.mp4 --side left --facing right
```

Supply only a video you have rights to use. See [manual validation](docs/manual_validation.md) for the protocol and pending hardware/teacher checks, and [implementation report](docs/implementation_report.md) for AC-01–AC-12 evidence and limitations. No personal video belongs in source control.
