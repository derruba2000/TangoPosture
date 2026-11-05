# Tango Posture Coach

<p align="center"><img src="logos/Tango_posture_logo.png" alt="TangoPosture logo" width="320"></p>

A local Python desktop coach and movement analysis tool for Argentine Tango dancers. It combines real-time MediaPipe Pose tracking (`pose_landmarker_lite.task`) with live microphone audio analysis to monitor **head horizontal alignment**, **shoulder tension**, **Leader/Follower role & Closed/Open embrace**, **closed-embrace knee flexion**, **central spine axis (front, side steps, and 360° pivots)**, and **8-beat Argentine Tango musical phrases**, while saving synchronized audio+video recordings and post-run Markdown reports.

Practice and pose analysis run locally without an account or API key after installation
and pose-model setup. Optional report interpretation connects to your configured Ollama server.

**Experimental movement observation, not medical assessment or a judgement of artistic tango quality.** For detailed guides, see:
- [Reference & Practice Comparison Manual](docs/REFERENCE_AND_COMPARISON_MANUAL.md) — step-by-step guide to calibrating a reference and comparing practice overlays and reports.
- [Argentine Tango Practice Guide](docs/TANGO_POSTURE_GUIDE.md) — dancer-facing biomechanical and technical guide.

---

## Key Features Added

1. **All-Angle Calibration & 360° Pivot / Side-Step Support (`tp_ALLOW_ALL_ANGLES=true`)**
   - Calibrate and practice **facing the camera**, in **side-on profile**, or while executing **side steps (*pasos al costado*), ochos, and 360° pivots** without losing tracking.
   - Uses your central spine axis (midpoint of shoulders to midpoint of hips) when both sides are visible, and seamlessly falls back to the visible side during pivots.
2. **Guided Audio Calibration & Timestamped Storage (`calibrations/`)**
   - Pressing `C` speaks **"Calibration starting"**, gives you a **3-second countdown** to get into position, plays a **1-second beep** throughout the 12-second capture window, and announces **"Calibration completed"** (or speaks the specific reason if calibration failed).
   - Automatically accepts and saves valid calibrations to `calibrations/calibration_<timestamp>_<id>.json` and `calibrations/latest_calibration.json` for instant reuse across sessions (`--calibration latest`).
3. **Head Horizontal Alignment & Neutral Camera Offset**
   - Draws a cyan horizontal reference line from the ear and a live ear-to-nose pitch vector (`Head horiz: +0.0 deg`).
   - Compensates for natural ear-to-nose landmark geometry and camera tilt via `tp_HEAD_NEUTRAL_OFFSET_DEGREES=-12.0` (or auto-zeroes to your calibrated straight posture) and triggers a red **`BAD HEAD ALIGNMENT`** alert when the head drops below `tp_HEAD_DOWN_ANGLE_DEGREES` (`-15.0 deg`) for longer than `tp_HEAD_DOWN_DURATION_SECONDS` (`3.0s`).
4. **Shoulder Tension Detection (`LOWER YOUR SHOULDERS`)**
   - Measures camera-distance-invariant vertical neck clearance (`shoulder.y - ear.y` normalized by rigid skull/body scale) on both shoulders.
   - Flags **`LOWER SHOULDERS (+XX%)`** with a downward arrow on the shoulder as soon as elevation crosses `tp_SHOULDER_RAISE_THRESHOLD_PCT`, and displays a red **`LOWER YOUR SHOULDERS`** banner after `tp_SHOULDER_TENSION_DURATION_SECONDS` (`1.5s`).
5. **Leader / Follower Role Recognition with Hysteresis**
   - Automatically identifies **`LEADER`** (left arm raised, right arm horizontal) or **`FOLLOWER`** (right arm raised, left arm horizontal).
   - Uses configurable hysteresis (`tp_ROLE_ASSIGN_MS=300` to assign from `NONE`, `tp_ROLE_HOLD_MS=5000` to hold the role across turns/pivots before returning to `NONE`).
6. **Closed vs. Open Embrace & Closed-Embrace Knee Bend Check**
   - Classifies active embraces as **`CLOSED EMBRACE`** or **`OPEN EMBRACE`** (`tp_EMBRACE_CLOSED_THRESHOLD=0.72`) and displays `Embrace: CLOSED EMBRACE` or `Embrace: OPEN EMBRACE` directly below the `Role:` label.
   - When in **`CLOSED EMBRACE`**, monitors knee flexion (`180° - hip–knee–ankle angle`). If your knees are locked/straighter than `tp_CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES` (`6.0 deg`), it highlights the knee joints on the overlay (`BEND KNEES`) and triggers a red **`BEND YOUR KNEES IN CLOSED EMBRACE`** alert after `tp_KNEE_STRAIGHT_DURATION_SECONDS` (`1.5s`).
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

### Option 1: Graphical Practice Studio (Recommended)

Open the native desktop interface with any of these commands:

```bash
./run_practice.sh
uv run tango-posture
uv run tango-posture gui
```

The **Practice** tab selects a camera or video and shows the run log; it also offers
video analysis without a live preview. **References**
lets you name a new calibration, choose saved references (including JSON files and
SQLite records), or define a reference interval in a video. Use **Match reference
camera settings** to restore the recorded side, facing, resolution, and capture
profile. Confirm physical camera placement each time you reuse a reference.

**Files & recording** controls model/data paths, recording switches, and raw/overlay
filenames. It suggests unused names and asks before replacing existing recordings.
**Settings** groups all 58 configuration values into camera/view, quality/calibration,
tracking, head/shoulders, embrace/knees, hip offset, music, and review/Ollama tabs. The Settings menu
also imports/exports `.env` settings; the File menu imports/exports TOML and saves
complete practice presets, including filenames and reference choices.

Click **Open practice coach** for a live preview with Calibrate, Accept, Retry,
Start, Pause/resume, End/save, Beat 1, Skeleton, Torso axis, and Quit/save buttons.
The existing keyboard shortcuts work in this window too. Settings changes apply to
the next run; closing the coach releases capture and saves reports and recordings.

The GUI needs a desktop display and Python's Tk support (check with
`uv run python -m tkinter`). Headless processing continues to use the CLI below.

### Read reports, replay recordings, and review with Ollama

The **Review** tab opens the report from a completed practice automatically. Select
older reports from the list, use **Refresh** after changing the data directory, or
choose **Open report** for a Markdown report elsewhere. Reports have formatted
headings, search, line wrapping, and clickable timestamps.

Under **Video replay**, switch between **Raw video**, **Overlay**, and the input
video when available. Play/pause, step through frames, scrub the timeline, change
speed, or enter a time such as `01:12.5`. Clicking a report timestamp seeks to that
moment. New `.metadata.json` sidecars associate each report with its recordings and
frame timeline, accounting for dropped capture frames when seeking recorded videos.
Older Markdown reports still open through their existing video links; timestamp
seeking for these reports is approximate. Use **Open video** to locate moved files.

Video replay is silent by default. Enable **Sound (ffplay)** for optional audio
when FFmpeg's `ffplay` and an audio output device are available. Audio and Tk video
use separate playback clocks; the **system player** button is available for normal
audio/video playback and synchronization. Pause and seeking stop/restart optional audio.

In **Ollama analysis**, set the server URL and a chat model, optionally use **List
installed models**, enter an analysis focus, then click **Analyze report**. The
report text is sent on this action; video files are not uploaded. Responses stream
into the app without blocking practice controls. Example setup on the machine
running Ollama:

```bash
ollama serve
# In another terminal, install a model once (or use one you already have):
ollama pull llama3.2
```

The default URL is `http://127.0.0.1:11434`, which means the machine running this
application. Configure another reachable server when Ollama runs elsewhere.
Settings and presets retain the server, model, instruction, timeout, temperature,
context size, report character limit, and output-token limit. Environment examples:

```dotenv
tp_OLLAMA_BASE_URL="http://127.0.0.1:11434"
tp_OLLAMA_MODEL="llama3.2"
tp_OLLAMA_TIMEOUT=120
tp_OLLAMA_NUM_CTX=16384
tp_OLLAMA_NUM_PREDICT=2048
```

Completed analyses are stored alongside their report as `.analysis-*.json`, with
the report hash, model, endpoint, instructions, and generation time. Reopen them
from **Saved analyses**, or export Markdown. They never replace the measured report
and are shown only for the matching report content. Cancelled/failed partial
responses are not saved; output that reaches the model's generation limit is
labelled incomplete. No analysis is sent automatically and no models are downloaded
by the app. Interpretation should be checked against report coverage and the video.

### Option 2: Direct CLI Execution

```bash
uv run tango-posture run --camera 0 --side left --facing right
# Explicitly record raw video and annotated overlay (with embedded audio) into videos/
uv run tango-posture run --camera 0 --side left --facing right \
  --record-video videos/practice_01.mp4 --record-overlay videos/overlay_01.mp4
# Auto-number recordings through the original CLI launcher:
./run_practice.sh --cli --calibration latest --confirm-camera-unchanged
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

All application environment variables use the case-sensitive **`tp_` prefix**,
preserving the previous uppercase name: for example,
`tp_HEAD_DOWN_ANGLE_DEGREES`. System variables such as `PATH` are unaffected.
[`.env.example`](.env.example) lists every supported variable, including camera,
file paths, calibration, and metric thresholds. The GUI shows each variable's exact
name alongside its editable value. Precedence is defaults → `.env` → process
environment → TOML → CLI; the GUI's current settings are passed as a TOML snapshot.

To migrate an existing unprefixed `.env`, launch
`uv run tango-posture gui --ignore-env`, choose **Settings → Import tp_ environment
settings**, select the old file, then export the renamed settings to `.env`.
Legacy names are accepted only by this explicit import; normal loading tells you
which names to rename. Practice presets never automatically confirm camera placement.

Common environment settings:

| `.env` Variable | Default | Description |
| --- | --- | --- |
| `tp_HEAD_NEUTRAL_OFFSET_DEGREES` | `-12.0` | Compensates for camera tilt and ear-to-nose landmark offset when standing straight (`0.0 deg` = level) |
| `tp_HEAD_DOWN_ANGLE_DEGREES` | `-15.0` | Downward head pitch threshold (below neutral `0.0 deg`) that flags downward head tilt |
| `tp_HEAD_DOWN_DURATION_SECONDS` | `3.0` | Seconds below `tp_HEAD_DOWN_ANGLE_DEGREES` before triggering red `BAD HEAD ALIGNMENT` alert |
| `tp_SHOULDER_NEUTRAL_CLEARANCE` | `1.71` | Relaxed vertical ear-to-shoulder clearance ratio when running uncalibrated (`0%` tension baseline) |
| `tp_SHOULDER_RAISE_THRESHOLD_PCT` | `16.0` | Shoulder elevation `%` above neutral that flags `LOWER SHOULDERS (+XX%)` |
| `tp_SHOULDER_TENSION_DURATION_SECONDS` | `1.5` | Seconds above `tp_SHOULDER_RAISE_THRESHOLD_PCT` before triggering red `LOWER YOUR SHOULDERS` alert |
| `tp_ROLE_ASSIGN_MS` | `300` | Milliseconds of continuous detection required to transition from `NONE` to `LEADER` or `FOLLOWER` |
| `tp_ROLE_HOLD_MS` | `5000` | Milliseconds to hold `LEADER` or `FOLLOWER` after detection before returning to `NONE` |
| `tp_EMBRACE_CLOSED_THRESHOLD` | `0.72` | Arm compactness threshold separating `CLOSED EMBRACE` (`<= 0.72`) from `OPEN EMBRACE` (`> 0.72`) |
| `tp_CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES` | `6.0` | Minimum knee flexion angle (`180° - hip–knee–ankle`) required when in `CLOSED EMBRACE` |
| `tp_KNEE_STRAIGHT_DURATION_SECONDS` | `1.5` | Seconds with unbent knees in closed embrace before triggering `BEND YOUR KNEES IN CLOSED EMBRACE` |
| `tp_ALLOW_ALL_ANGLES` | `true` | Enables calibration and posture tracking facing the camera, in profile, and across 360° pivots/side steps |
| `tp_BEAT_TRACKING_ENABLED` | `true` | Enables live microphone Argentine Tango 8-beat phrase tracking and audio recording |
| `tp_TANGO_BPM` | `120.0` | Initial tempo prior (BPM) before the 30-second audio analyzer locks onto the song's tempo |
| `tp_BEAT_ANALYSIS_SECONDS` | `30.0` | Duration of active musical audio (`30s`) used to lock onto the exact start (Beat 1) of each 8-beat phrase |

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
