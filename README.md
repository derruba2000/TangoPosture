# Tango Posture Coach

A local Python desktop coach and movement analysis tool for Argentine Tango dancers. It combines real-time MediaPipe Pose tracking (`pose_landmarker_lite.task`) with live microphone audio analysis to monitor **head horizontal alignment**, **shoulder tension**, **Leader/Follower role & Closed/Open embrace**, **closed-embrace knee flexion**, **central spine axis (front, side steps, and 360° pivots)**, and **8-beat Argentine Tango musical phrases**, while saving synchronized audio+video recordings and post-run Markdown reports.

No account, API key, hosted inference, or network connection is needed after installation and model setup.

**Experimental movement observation, not medical assessment or a judgement of artistic tango quality.** For detailed guides, see:
- [Reference & Practice Comparison Manual](docs/REFERENCE_AND_COMPARISON_MANUAL.md) — step-by-step guide to calibrating a reference and comparing practice overlays and reports.
- [Argentine Tango Practice Guide](docs/TANGO_POSTURE_GUIDE.md) — dancer-facing biomechanical and technical guide.

---

## Key Features Added

1. **All-Angle Calibration & 360° Pivot / Side-Step Support (`ALLOW_ALL_ANGLES=true`)**
   - Calibrate and practice **facing the camera**, in **side-on profile**, or while executing **side steps (*pasos al costado*), ochos, and 360° pivots** without losing tracking.
   - Uses your central spine axis (midpoint of shoulders to midpoint of hips) when both sides are visible, and seamlessly falls back to the visible side during pivots.
2. **Guided Audio Calibration & Timestamped Storage (`calibrations/`)**
   - Pressing `C` speaks **"Calibration starting"**, gives you a **3-second countdown** to get into position, plays a **1-second beep** throughout the 12-second capture window, and announces **"Calibration completed"** (or speaks the specific reason if calibration failed).
   - Automatically accepts and saves valid calibrations to `calibrations/calibration_<timestamp>_<id>.json` and `calibrations/latest_calibration.json` for instant reuse across sessions (`--calibration latest`).
3. **Head Horizontal Alignment & Neutral Camera Offset**
   - Draws a cyan horizontal reference line from the ear and a live ear-to-nose pitch vector (`Head horiz: +0.0 deg`).
   - Compensates for natural ear-to-nose landmark geometry and camera tilt via `HEAD_NEUTRAL_OFFSET_DEGREES=-12.0` (or auto-zeroes to your calibrated straight posture) and triggers a red **`BAD HEAD ALIGNMENT`** alert when the head drops below `HEAD_DOWN_ANGLE_DEGREES` (`-15.0 deg`) for longer than `HEAD_DOWN_DURATION_SECONDS` (`3.0s`).
4. **Shoulder Tension Detection (`LOWER YOUR SHOULDERS`)**
   - Measures camera-distance-invariant vertical neck clearance (`shoulder.y - ear.y` normalized by rigid skull/body scale) on both shoulders.
   - Flags **`LOWER SHOULDERS (+XX%)`** with a downward arrow on the shoulder as soon as elevation crosses `SHOULDER_RAISE_THRESHOLD_PCT`, and displays a red **`LOWER YOUR SHOULDERS`** banner after `SHOULDER_TENSION_DURATION_SECONDS` (`1.5s`).
5. **Leader / Follower Role Recognition with Hysteresis**
   - Automatically identifies **`LEADER`** (left arm raised, right arm horizontal) or **`FOLLOWER`** (right arm raised, left arm horizontal).
   - Uses configurable hysteresis (`ROLE_ASSIGN_MS=300` to assign from `NONE`, `ROLE_HOLD_MS=5000` to hold the role across turns/pivots before returning to `NONE`).
6. **Closed vs. Open Embrace & Closed-Embrace Knee Bend Check**
   - Classifies active embraces as **`CLOSED EMBRACE`** or **`OPEN EMBRACE`** (`EMBRACE_CLOSED_THRESHOLD=0.72`) and displays `Embrace: CLOSED EMBRACE` or `Embrace: OPEN EMBRACE` directly below the `Role:` label.
   - When in **`CLOSED EMBRACE`**, monitors knee flexion (`180° - hip–knee–ankle angle`). If your knees are locked/straighter than `CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES` (`6.0 deg`), it highlights the knee joints on the overlay (`BEND KNEES`) and triggers a red **`BEND YOUR KNEES IN CLOSED EMBRACE`** alert after `KNEE_STRAIGHT_DURATION_SECONDS` (`1.5s`).
7. **Argentine Tango 8-Beat Musical Phrase Tracker**
   - **Silence / No Music Gate**: Displays **`WAITING FOR SONG TO BEGIN...`** with all `1..8` beat boxes unlit (`beat = 0`, `phrase = 0`) when no music is playing.
   - **30-Second Phrase-Start Analysis**: As soon as a song is detected on the microphone, starts a 30-second analysis window (`UNDERSTANDING BEATS (0/30s -> 30/30s)`) using scale-invariant multi-band spectral flux and 8-beat harmonic comb autocorrelation while progressing through beats `1 -> 2 -> ... -> 8` in green.
   - **Phrase Lock**: Locks onto **Beat 1** of each 8-beat tango phrase (`Phrase N | BEAT 1..8 | YYY BPM [LOCKED]`) and pauses immediately when the music stops.
8. **Synchronized Audio+Video Recording (`videos/`) & Auto-Incrementing Launcher (`./run_practice.sh`)**
   - Embeds recorded microphone audio directly into `videos/practice_XX.mp4` and `videos/overlay_XX.mp4` via native macOS `AVFoundation` muxing (no separate `.wav` files left behind).
   - Full-width video display (`>= 960 px` wide) with no black side margins.
9. **Automatic Post-Run Markdown Reports (`reports/`)**
   - Every run generates `reports/report_<timestamp>.md` and `reports/latest_report.md` with clickable links to the saved raw and overlay videos, embrace role & type intervals, head horizontal pitch, shoulder tension, closed-embrace knee bend, calibrated reference deltas, and an issues timeline mapped to exact timestamps and musical Phrase/Beat.

---

## Install

Tested on macOS (Apple Silicon arm64), Python 3.11. Install [uv](https://github.com/astral-sh/uv), then from this repository:

```bash
uv python install 3.11
uv sync --frozen
uv run tango-posture --help
uv run tango-posture models fetch
```

`models fetch` is an explicit setup operation. It downloads only the versioned model in `models/manifest.json`, verifies SHA-256, and atomically installs it. Runs never download models. For offline installation, transfer that exact verified file yourself and pass `--model-path /path/to/pose_landmarker_lite.task`. Every run verifies its checksum.

The pinned MediaPipe 0.10.30 release supports the Tasks API without pulling in a second OpenCV package. See [wheel evidence](docs/wheel_compatibility.json), [third-party notices](THIRD_PARTY_NOTICES.md), and the [model manifest](models/manifest.json). Original application code is Apache-2.0; dependency and model terms remain separate.

---

## Webcam Practice Workflow

### Option 1: Auto-Incrementing Practice Script (Recommended)

Run [`./run_practice.sh`](run_practice.sh) to automatically number your recorded raw and overlay videos (`videos/practice_01.mp4` + `videos/overlay_01.mp4`, then `02`, `03`, etc.) with embedded microphone audio:

```bash
./run_practice.sh
# Reuse your latest saved calibration from calibrations/:
./run_practice.sh --calibration latest --confirm-camera-unchanged
```

### Option 2: Direct CLI Execution

```bash
uv run tango-posture run --camera 0 --side left --facing right
# Explicitly record raw video and annotated overlay (with embedded audio) into videos/
uv run tango-posture run --camera 0 --side left --facing right \
  --record-video videos/practice_01.mp4 --record-overlay videos/overlay_01.mp4
```

On macOS, grant Camera and Microphone access to the terminal/application that launches Python in **System Settings → Privacy & Security**.

### Interactive Controls

| Key | Action |
| --- | --- |
| `C` | Start guided calibration (spoken `"Calibration starting"`, 3s positioning wait, 12s capture with 1s beeps, auto-saved to `calibrations/`) |
| `A` | Accept a displayed calibration (when reviewing a video reference interval) |
| `R` | Retry calibration |
| `S` | Start an evaluated practice session against the active reference |
| `P` | Pause/resume session; pause time is excluded |
| `B` | Manually sync Argentine Tango **Beat 1** / tap tempo (`1..8` phrase counter) |
| `E` | End and save the current session |
| `Q` / window close / `Ctrl+C` | Quit, mux recorded audio into `.mp4` videos, and write the Markdown report to `reports/` |
| `V` | Confirm unmirrored facing direction and enable requested display mirroring |
| `Y` | Show/hide the torso axis and vertical reference |

### Torso axis overlay

The solid **magenta torso axis** joins the midpoint of your hips to the midpoint of
your shoulders and extends slightly beyond both. The **dashed cyan vertical** runs
through your hip midpoint. `Torso axis: X deg` shows the magnitude of the torso's
image-plane tilt from vertical, corrected for the video's aspect ratio. These cues
work in both live and recorded overlays, including mirrored displays. Head movement
and stepping feet do not define this axis. Both lines hide when any of the four
shoulder/hip landmarks is missing or below the configured confidence threshold.

Keep the camera level: the vertical is an image reference, not a measured gravity
direction. This overlay shows torso orientation, not your centre of mass or which
foot carries your weight. The existing ankle-to-head hip-offset calculation remains
separate, labelled **Hip offset** in the overlay status; its calibration, thresholds,
and report fields are unchanged.

---

## `.env` Configuration Reference

All real-time posture, role, embrace, shoulder, knee, and musical beat thresholds can be tuned in [`.env`](.env):

| `.env` Variable | Default | Description |
| --- | --- | --- |
| `HEAD_NEUTRAL_OFFSET_DEGREES` | `-12.0` | Compensates for camera tilt and ear-to-nose landmark offset when standing straight (`0.0 deg` = level) |
| `HEAD_DOWN_ANGLE_DEGREES` | `-15.0` | Downward head pitch threshold (below neutral `0.0 deg`) that flags downward head tilt |
| `HEAD_DOWN_DURATION_SECONDS` | `3.0` | Seconds below `HEAD_DOWN_ANGLE_DEGREES` before triggering red `BAD HEAD ALIGNMENT` alert |
| `SHOULDER_NEUTRAL_CLEARANCE` | `1.71` | Relaxed vertical ear-to-shoulder clearance ratio when running uncalibrated (`0%` tension baseline) |
| `SHOULDER_RAISE_THRESHOLD_PCT` | `16.0` | Shoulder elevation `%` above neutral that flags `LOWER SHOULDERS (+XX%)` |
| `SHOULDER_TENSION_DURATION_SECONDS` | `1.5` | Seconds above `SHOULDER_RAISE_THRESHOLD_PCT` before triggering red `LOWER YOUR SHOULDERS` alert |
| `ROLE_ASSIGN_MS` | `300` | Milliseconds of continuous detection required to transition from `NONE` to `LEADER` or `FOLLOWER` |
| `ROLE_HOLD_MS` | `5000` | Milliseconds to hold `LEADER` or `FOLLOWER` after detection before returning to `NONE` |
| `EMBRACE_CLOSED_THRESHOLD` | `0.72` | Arm compactness threshold separating `CLOSED EMBRACE` (`<= 0.72`) from `OPEN EMBRACE` (`> 0.72`) |
| `EMBRACE_HYSTERESIS_SECONDS` | `1.0` | Seconds of continuous openness/compactness required to switch between `CLOSED EMBRACE` and `OPEN EMBRACE` |
| `CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES` | `6.0` | Minimum knee flexion angle (`180° - hip–knee–ankle`) required when in `CLOSED EMBRACE` |
| `KNEE_STRAIGHT_DURATION_SECONDS` | `1.5` | Seconds with unbent knees in closed embrace before triggering `BEND YOUR KNEES IN CLOSED EMBRACE` |
| `SLOW_TANGO_MAX_BPM` | `115.0` | Tempo ceiling (BPM) for slow lyrical tangos (e.g. Carlos Di Sarli) where feet must glide close to the floor |
| `ANKLE_HIGH_LIFT_RATIO` | `0.06` | Maximum vertical ankle lift (as a fraction of leg length, `0.06` = `6%`) before a step is counted as raised too high |
| `MAX_CONSECUTIVE_HIGH_ANKLE_STEPS` | `2` | Number of consecutive high-ankle steps in slow tango (`<= SLOW_TANGO_MAX_BPM`) that triggers `KEEP FEET CLOSE TO FLOOR` |
| `OVERLAY_SMOOTHING_SECONDS` | `0.20` | Exponential smoothing time constant (seconds) for overlay skeleton lines, segments, and visual vectors (`0.0` disables landmark smoothing; higher values like `0.35`–`0.50` make lines much smoother) |
| `SMOOTHING_SECONDS` | `0.25` | Exponential smoothing time constant (seconds) for core posture and axis-curve measurements |
| `ALLOW_ALL_ANGLES` | `true` | Enables calibration and posture tracking facing the camera, in profile, and across 360° pivots/side steps |
| `BEAT_TRACKING_ENABLED` | `true` | Enables live microphone Argentine Tango 8-beat phrase tracking and audio recording |
| `TANGO_BPM` | `120.0` | Initial tempo prior (BPM) before the 30-second audio analyzer locks onto the song's tempo |
| `BEAT_ANALYSIS_SECONDS` | `30.0` | Duration of active musical audio (`30s`) used to lock onto the exact start (Beat 1) of each 8-beat phrase |

---

## Saved Artifacts & Output Directories

- **`videos/`**: Raw practice videos (`practice_XX.mp4`) and full-width annotated overlay videos (`overlay_XX.mp4`) with embedded 16 kHz PCM/AAC audio.
- **`calibrations/`**: Timestamped calibration JSON files (`calibration_<YYYYMMDD_HHMMSS>_<id>.json`) and `latest_calibration.json`. Reuse any saved calibration via `--calibration latest`, a filename in `calibrations/`, or a calibration UUID.
- **`reports/`**: Automated post-run Markdown reports (`report_<YYYYMMDD_HHMMSS>.md` and `latest_report.md`) linking directly to your saved raw and overlay videos and detailing every posture, shoulder, knee, and head-alignment issue by timestamp and musical Phrase/Beat.
- **`data/tango.sqlite3` & `data/exports/SESSION_ID/`**: SQLite session storage (`summary.json`, `observations.csv`, `events.csv`).

---

## Video and Reference Reuse

```bash
# List saved calibrations (from both calibrations/ and SQLite storage)
uv run tango-posture calibrations list

# Reuse the most recent calibration in calibrations/latest_calibration.json
uv run tango-posture run --camera 0 --side left --facing right \
  --calibration latest --confirm-camera-unchanged

# Headless video analysis with a 12-second reference interval
uv run tango-posture run --video videos/practice_01.mp4 --side left --facing right \
  --reference-interval 0 12 --reference-name 'Recorded stance' --headless
```

---

## Verify and Validate

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Tests cover synthetic and all-angle geometry, confidence gates, calibration storage/reuse, head pitch offset, shoulder tension, Leader/Follower hysteresis, Closed/Open embrace classification, closed-embrace knee flexion, 30-second musical phrase-start locking, silence gating, CLI safeguards, and real-model inference. See [Reference & Comparison Manual](docs/REFERENCE_AND_COMPARISON_MANUAL.md), [Manual Validation](docs/manual_validation.md), and [Implementation Report](docs/implementation_report.md).
