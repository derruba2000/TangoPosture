# Tango Posture Coach

<p align="center"><img src="logos/Tango_posture_logo.png" alt="TangoPosture logo" width="320"></p>

A local Python desktop coach and movement analysis tool for Argentine Tango dancers. It combines real-time MediaPipe Pose tracking (`pose_landmarker_lite.task`) with live microphone audio analysis to monitor **head horizontal alignment**, **shoulder tension**, **Leader/Follower role & Closed/Open embrace**, **closed-embrace knee flexion**, **central spine axis (front, side steps, and 360° pivots)**, and **8-beat Argentine Tango musical phrases**, while saving synchronized audio+video recordings and post-run Markdown reports.

Practice and pose analysis run locally without an account or API key after installation
and pose-model setup. Optional report interpretation connects to your configured Ollama server.

Visit the [TangoPosture community website](https://tangoposture-community.correia-ramos.chatgpt.site/).

The desktop application uses **Qt / PySide6** exclusively.

For developers, see the [architecture and workflow guide](docs/ARCHITECTURE.md)
for the source layout, module responsibilities, use cases, UML and sequence diagrams.

**Experimental movement observation, not medical assessment or a judgement of artistic tango quality.** See the [Tango Posture Coach Manual](docs/TANGO_POSTURE_GUIDE.md) for one guide to practice, references, reports, configuration, examples, and limitations. Open **Help & Guide** in the desktop navigation rail for the same guide with search and section navigation.

---

## Key Features Added

1. **All-Angle Calibration & 360° Pivot / Side-Step Support (`allow_all_angles=true`)**
   - Calibrate and practice **facing the camera**, in **side-on profile**, or while executing **side steps (*pasos al costado*), ochos, and 360° pivots** without losing tracking.
   - Uses your central spine axis (midpoint of shoulders to midpoint of hips) when both sides are visible, and seamlessly falls back to the visible side during pivots.
2. **Reference Recordings & Calibration Profiles**
   - `.tpr` files contain replayable reference video and cached measurements; `.tpc` profiles contain approved metric ranges independently of the source video.
   - **Library > Guided camera calibration** and **References > New Calibration** guide neutral/change/return/repeat holds and open the same approval editor used by a reference video's **Create calibration** action. New profiles require a distinct name; originals are not replaced.
3. **Head Horizontal Alignment & Neutral Camera Offset**
   - Draws a cyan horizontal reference line from the ear and a live ear-to-nose pitch vector (`Head horiz: +0.0 deg`).
   - Compensates for natural ear-to-nose landmark geometry and camera tilt via `head_neutral_offset=-12.0` (or auto-zeroes to your calibrated straight posture) and triggers a red **`BAD HEAD ALIGNMENT`** alert when the head drops below `head_down_angle` (`-15.0 deg`) for longer than `head_down_duration` (`3.0s`).
4. **Shoulder Tension Detection (`LOWER YOUR SHOULDERS`)**
   - Measures camera-distance-invariant vertical neck clearance (`shoulder.y - ear.y` normalized by rigid skull/body scale) on both shoulders.
   - Flags **`LOWER SHOULDERS (+XX%)`** with a downward arrow on the shoulder as soon as elevation crosses `shoulder_raise_threshold`, and displays a red **`LOWER YOUR SHOULDERS`** banner after `shoulder_tension_duration` (`1.5s`).
5. **Leader / Follower Role Recognition with Hysteresis**
   - Automatically identifies **`LEADER`** (left arm raised, right arm horizontal) or **`FOLLOWER`** (right arm raised, left arm horizontal).
   - Uses configurable hysteresis (`role_assign_ms=300` to assign from `NONE`, `role_hold_ms=5000` to hold the role across turns/pivots before returning to `NONE`).
6. **Closed vs. Open Embrace & Closed-Embrace Knee Bend Check**
   - Classifies active embraces as **`CLOSED EMBRACE`** or **`OPEN EMBRACE`** (`embrace_closed_threshold=0.72`) and displays `Embrace: CLOSED EMBRACE` or `Embrace: OPEN EMBRACE` directly below the `Role:` label.
   - When in **`CLOSED EMBRACE`**, monitors knee flexion (`180° - hip–knee–ankle angle`). If your knees are locked/straighter than `closed_embrace_min_knee_bend` (`6.0 deg`), it highlights the knee joints on the overlay (`BEND KNEES`) and triggers a red **`BEND YOUR KNEES IN CLOSED EMBRACE`** alert after `knee_straight_duration` (`1.5s`).
7. **Argentine Tango 8-Beat Musical Phrase Tracker**
   - **Silence / No Music Gate**: Displays **`WAITING FOR SONG TO BEGIN...`** with all `1..8` beat boxes unlit (`beat = 0`, `phrase = 0`) when no music is playing.
   - **30-Second Phrase-Start Analysis**: As soon as a song is detected on the microphone, starts a 30-second analysis window (`UNDERSTANDING BEATS (0/30s -> 30/30s)`) using scale-invariant multi-band spectral flux and 8-beat harmonic comb autocorrelation while progressing through beats `1 -> 2 -> ... -> 8` in green.
   - **Phrase Lock**: Locks onto **Beat 1** of each 8-beat tango phrase (`Phrase N | BEAT 1..8 | YYY BPM [LOCKED]`) and pauses immediately when the music stops.
8. **Portable Practice Archives (`practice/`) & Numbered Recordings**
   - Saves each named practice as a ZIP archive with a `.tp` extension. Each recording includes raw video, annotated overlay, report, and metadata, numbered from `00`.
   - Embeds recorded microphone audio into video via native macOS `AVFoundation` muxing when supported (no separate `.wav` files left behind).
   - Full-width video display (`>= 960 px` wide) with no black side margins.
9. **Per-Recording Markdown Reports**
   - Each saved recording has a report inside its `.tp` archive, with embrace role/type intervals, alignment metrics, and an issues timeline linked to the matching raw and overlay videos.

---

## Install

### Desktop installers (experimental)

When available, download the macOS Apple Silicon `.dmg` or Windows x64 setup
executable from the project's [GitHub Releases](https://github.com/derruba2000/TangoPosture/releases).
The installers include Python and the verified pose model; a separate model
download is not needed. On macOS, drag the app into Applications; on Windows,
run the installer and open TangoPosture from the Start menu.

These builds are **unsigned** and the macOS build is **not notarized**. Your
operating system may warn or block installation. Only install artifacts you
trust, and compare them with the release's `SHA256SUMS.txt`. macOS Intel,
Windows ARM, and Linux installers are not provided. Camera, microphone, and
recording functionality still require testing on the target machine; Windows
support is experimental, and some media features depend on platform-specific
tools or capabilities.

If macOS says **"Apple could not verify TangoPosture is free of malware"**,
this is the expected Gatekeeper warning for these unnotarized builds, not a
successful malware scan. Only if you trust the release and have verified its
checksum: copy the app into Applications, try opening it, then go to
**System Settings > Privacy & Security > Open Anyway** and confirm **Open**.
This creates an exception for this app; do not disable Gatekeeper globally.
See [Apple's instructions](https://support.apple.com/en-us/102445).
Removing this warning for distributed builds requires an Apple Developer ID
signature and Apple notarization; changing the logo does not remove it.

Installed-app data and recordings are stored under
`~/Library/Application Support/TangoPosture` on macOS and
`%LOCALAPPDATA%\TangoPosture` on Windows. Uninstalling the app does not remove
those files. Optional tools such as `ffmpeg`, `yt-dlp`, and Ollama are not
included in the installers.

### Source installation

Tested on macOS (Apple Silicon arm64), Python 3.11. Install [uv](https://github.com/astral-sh/uv), then from this repository:

```bash
uv python install 3.11
uv sync --frozen
uv run tango-posture --help
uv run tango-posture models fetch
uv run tango-posture qt
```

`models fetch` is an explicit setup operation. It downloads only the versioned model in `models/manifest.json`, verifies SHA-256, and atomically installs it. Runs never download models. For offline installation, transfer that exact verified file yourself and pass `--model-path /path/to/pose_landmarker_lite.task`. Every run verifies its checksum.

`uv run tango-posture`, `uv run tango-posture gui`, and
`uv run tango-posture qt` all open the same PySide6 desktop interface.
The packaged application also uses Qt. The former Tk interface has been removed;
existing `.tp`/`.tpc` archives, data directories, and TOML settings are retained.

The pinned MediaPipe 0.10.31 release fixes Windows Tasks initialization (`function 'free' not found` in 0.10.30) without pulling in a second OpenCV package. See [wheel evidence](docs/wheel_compatibility.json), [third-party notices](THIRD_PARTY_NOTICES.md), and the [model manifest](models/manifest.json). Original application code is Apache-2.0; dependency and model terms remain separate.

### Building release assets

The [desktop workflow](.github/workflows/desktop-release.yml) runs checks on
macOS and Windows. Run it manually through Actions to build installers as
temporary workflow artifacts without publishing. To publish assets, set the
package version in `pyproject.toml`, create a matching tag such as `v0.1.0`,
and publish a GitHub Release for that tag. The workflow builds and smoke-tests
both installers and, only if both builds and all checks pass, attaches them and
`SHA256SUMS.txt` to the release. The release is visible before the builds
finish; a failed check leaves it without new assets. Existing assets are not
overwritten. A release must have been published after this workflow exists on
the default branch, and repository Actions settings must permit the workflow's
release write permission.

Desktop builds convert `logos/TangoPosture_logo_under_1MB.png` into multi-size
macOS `.icns` and Windows `.ico` icons using the packaging dependency group.
These icons are embedded in the app bundle, Windows executable, and Windows
setup installer. For local PyInstaller builds, run
`uv run --frozen --group packaging python scripts/prepare_desktop_icons.py`
before building.

The smoke tests exercise bundled resources, native dependencies, and a pose
inference; they do not certify real camera, microphone, GUI interaction, or
recording behavior. Validate those manually on clean target machines before
recommending a release.

macOS disk-image cleanup retries transient busy-volume errors before forcing
detach of the CI smoke-test image. Smoke-test and final detach failures still
fail the build.

Windows compilation, installation, and smoke testing appear as separate workflow
steps. Installation is limited to 180 seconds and smoke testing to 120 seconds.
The `windows-verification-logs` workflow artifact includes the installer log and
the packaged application's Python traceback if verification fails. A hung or
failed verification prevents installer publication.

---

## Webcam Practice Workflow

### Option 1: Graphical Practice Studio

Open the native Qt desktop interface with:

```bash
uv run tango-posture qt
```

Qt startup shows a branded splash screen for three seconds. The left navigation
rail opens **Practice**, **References**, **Library**, **Review**, **Analytics**,
**AI Coach**, **Settings**, and **Help & Guide**. **About** shows the version,
license, and repository link.

In **Practice**, name the practice, select a camera or video source, then click
**Start Preview**. Use **New Calibration** for guided camera capture and explicit
range approval. **References > New Calibration** also starts this workflow;
saved `.tpc` profiles can be inspected and matched with **Match Camera Settings**.
Confirm unchanged physical camera placement when reusing a reference.

**Record (R)** starts video recording. **Start Scoring (S)** starts
reference-relative evaluation independently of recording. **Pause Scoring (P)**
pauses scoring, and **Stop & Save (E)** saves the current recording.
**Abort Recording (X)** discards the current unsaved recording after confirmation.
**Close Preview** releases the capture. Previously saved recordings remain intact.
Practices are saved as `practice/<name>.tp`; reusing the name appends indexed
recordings rather than replacing earlier segments.

**Settings** groups camera/view, quality/calibration, tracking, head/shoulders,
embrace/knees, hip offset, music, and replay/Ollama values. Apply changes for the
current session; use TOML import/export to retain settings across launches.

Tick **Automatically record songs** in the coach to record when the live microphone
detects music and save when it detects a sustained stop. It repeats for subsequent
songs while checked, appending numbered recordings to the same `.tp` file. Unchecking
it saves an active automatic recording and stops automation. This option requires
camera mode, enabled beat tracking, and a usable microphone; it does not use clock
or tap-tempo fallback as evidence of a song.

Set the following under **Settings > Music**. Changes apply to the next run:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `auto_record_start_delay` | 1 second | Continuous detected music before starting |
| `auto_record_stop_delay` | 3 seconds | Continuous detector silence before stopping/saving |
| `max_recordings` | 10 | Maximum manual or automatic recordings started in one coach run |

Short silent gaps below the stop delay do not split a recording. Song detection is
heuristic; the configured delays apply after the detector reports music or silence.

The GUI needs a desktop display and PySide6, installed by `uv sync --frozen`.
Tk/Tcl is not required. Headless processing continues to use the CLI below.

### Read reports, replay recordings, and review with Ollama

**Library** lists `.tp` practices, `.tpr` reference recordings and `.tpc`
calibration profiles. Local, YouTube and Facebook imports default to **Import as
reference (.tpr)**; uncheck it to create a practice archive. Imports retain raw,
overlay, floor-lines and indexed analytics without changing the original media.
**Promote to Reference** copies a selected practice recording or excerpt into a
separate `.tpr`. Rename persists the display name;
confirmed Delete moves an archive to Trash. **Migrate Legacy Data** copies
compatible loose reports/database evidence and copies legacy `.tpc` recordings
into `.tpr` references while retaining every original neutral baseline.

On a `.tpr`, choose **Create calibration**. Select the numbered dancer in the
video; explicitly restart a track after ambiguity or occlusion and confirm linked
passages show the same dancer. Save that selection in the reference if desired.
Work through **Head → Shoulders → Hips → Torso → Knees**, review the histogram,
timeline, median, middle 80%, reliable duration and extreme examples, and edit
example passages separately from acceptable limits. Approve only supported
metrics and save distinct profiles for different contexts.

Video-derived profiles support comparison immediately, but do not enable scoring.
Use **Check camera** on a profile for guided repeatability checks, retry or skip
failed steps, and explicitly approve live use metric by metric. Walking contexts
include a walking drill. Repeatability is an engineering check, not technique or
cross-camera accuracy validation; confirm the view is comparable. Approved live
profiles require matching model and capture settings and unchanged physical placement.

In **Review**, use **Open Library Archive…**, select the recording and stream,
then play, scrub, step frames, or change speed. Report timestamps seek to archived
frames, including timing gaps. **Analyze posture & movement** saves results in
the selected `.tp` or `.tpr`; layer controls display skeletons, metrics, alerts, pivots,
steps, and lines on a neutral floor. **Open Comparison…** selects another Library
archive for synchronized side-by-side video. Archive contents are extracted
temporarily and cleaned up when closed. Qt replay is currently video-only;
export a stream for playback with audio in an external player.

**Analytics** loads indexed recording analytics from `.tp` archives, independently
of the original database. Unknown observations remain gaps, not good scores.

In **AI Coach**, configure the server in Settings, select a report, choose a model
with **Check Ollama**, enter an analysis focus, then click **Analyze Selected Report**. The
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
TOML settings retain the server, model, instruction, timeout, temperature,
context size, report character limit, and output-token limit. TOML examples:

```toml
ollama_base_url="http://127.0.0.1:11434"
ollama_model="llama3.2"
ollama_timeout=120
ollama_num_ctx=16384
ollama_num_predict=2048
```

Completed analyses are stored inside the practice archive alongside their report as `.analysis-*.json`, with
the report hash, model, endpoint, instructions, and generation time. Reopen them
from **Saved analyses**, or export Markdown. They never replace the measured report
and are shown only for the matching report content. Cancelled/failed partial
responses are not saved; output that reaches the model's generation limit is
labelled incomplete. No analysis is sent automatically and no models are downloaded
by the app. Interpretation should be checked against report coverage and the video.

### Option 2: Direct CLI Execution

```bash
uv run tango-posture run --camera 0 --side left --facing right
# Open a named practice; R records, E saves a segment, Q saves and quits
uv run tango-posture run --camera 0 --side left --facing right \
   --practice-name 'Evening practice'
# Use the CLI launcher:
./run_practice.sh --cli --calibration latest --confirm-camera-unchanged
```

On macOS, grant Camera and Microphone access to the terminal/application that launches Python in **System Settings → Privacy & Security**.

### Interactive Controls

| Key | Action |
| --- | --- |
| `C` | Start guided calibration (spoken `"Calibration starting"`, 3s positioning wait, 12s capture with 1s beeps, auto-saved to `calibrations/`) |
| `A` | Accept a displayed calibration (when reviewing a video reference interval) |
| `R` | Record raw video and overlay; after E, record the next segment in the same practice |
| `S` | Start an evaluated practice session against the active reference |
| `P` | Pause/resume session; pause time is excluded |
| `B` | Manually sync Argentine Tango **Beat 1** / tap tempo (`1..8` phrase counter) |
| `E` | Stop recording/scoring and save the segment to the `.tp` archive; keep the preview open |
| `Q` / window close / `Ctrl+C` | Save the current segment and quit |
| `X` | Confirm Abort, discard the current unsaved recording/scoring session, and exit the coach |
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

## App Settings & TOML

All application parameters are editable in **Settings**, using the same names as
[configs/default.toml](configs/default.toml). The default TOML includes every editable
parameter. Use **Settings > Import settings TOML** or **Export settings TOML** to
load/save them, or save a complete practice preset.

Configuration precedence is app defaults, then an explicitly selected TOML file,
then CLI options. Coach runs use a snapshot of the app's current settings. `.env`
files and process environment variables are ignored; `--ignore-env` has been removed.
Start normally with `uv run tango-posture gui`, or choose a file with
`uv run tango-posture --config configs/default.toml gui`. Practice presets never
automatically confirm camera placement.

Common settings:

| Setting | Default | Description |
| --- | --- | --- |
| `head_neutral_offset` | `-12.0` | Compensates for camera tilt and ear-to-nose landmark offset when standing straight (`0.0 deg` = level) |
| `head_down_angle` | `-15.0` | Downward head pitch threshold (below neutral `0.0 deg`) that flags downward head tilt |
| `head_down_duration` | `3.0` | Seconds below `head_down_angle` before triggering red `BAD HEAD ALIGNMENT` alert |
| `shoulder_neutral_clearance` | `1.05` | Relaxed vertical ear-to-shoulder clearance ratio when running uncalibrated (`0%` tension baseline) |
| `shoulder_raise_threshold` | `10.0` | Shoulder elevation `%` above neutral that flags `LOWER SHOULDERS (+XX%)` |
| `shoulder_tension_duration` | `1.0` | Seconds above `shoulder_raise_threshold` before triggering red `LOWER YOUR SHOULDERS` alert |
| `role_assign_ms` | `300` | Milliseconds of continuous detection required to transition from `NONE` to `LEADER` or `FOLLOWER` |
| `role_hold_ms` | `5000` | Milliseconds to hold `LEADER` or `FOLLOWER` after detection before returning to `NONE` |
| `embrace_closed_threshold` | `0.72` | Arm compactness threshold separating `CLOSED EMBRACE` (`<= 0.72`) from `OPEN EMBRACE` (`> 0.72`) |
| `closed_embrace_min_knee_bend` | `8.0` | Minimum knee flexion angle (`180° - hip–knee–ankle`) required when in `CLOSED EMBRACE` |
| `knee_straight_duration` | `1.0` | Seconds with unbent knees in closed embrace before triggering `BEND YOUR KNEES IN CLOSED EMBRACE` |
| `allow_all_angles` | `false` | Enables calibration and posture tracking facing the camera, in profile, and across 360° pivots/side steps |
| `beat_tracking` | `true` | Enables live microphone Argentine Tango 8-beat phrase tracking and audio recording |
| `tango_bpm` | `120.0` | Initial tempo prior (BPM) before the 30-second audio analyzer locks onto the song's tempo |
| `beat_analysis_seconds` | `30.0` | Duration of active musical audio used to lock onto the exact start (Beat 1) of each 8-beat phrase |

---

## Saved Artifacts & Output Directories

- **`practice/<name>.tp`**: Portable ZIP archive containing `video_00.mp4`, `overlay_00.mp4`, `report_00.md`, `report_00.metadata.json`, and a manifest. Further recordings increment to `01`, `02`, and beyond, preserving earlier recordings. Observation-only runs have a report but no recorded videos.
- **`references/<name>.tpr`**: Reference recording ZIP with raw/overlay/lines video where available, indexed measurements, report and source/selection metadata.
- **`calibrations/<name>.tpc`**: Version-2 calibration ZIP with `profile.json` and a manifest: explicitly approved ranges, units, views, context, evidence coverage, provenance, model and measurement versions. It needs no source video to score. Version-1 `.tpc` files remain readable as neutral baselines, without invented distributions; migration creates separate `.tpr` copies. Legacy JSON baselines also remain readable.
- **Legacy `reports/` and `videos/`**: Existing loose files remain untouched and readable; new practice reports and recordings are archived instead.
- **`data/tango.sqlite3` & `data/exports/SESSION_ID/`**: SQLite session storage (`summary.json`, `observations.csv`, `events.csv`).

---

## Video and Reference Reuse

```bash
# List saved calibrations (from both calibrations/ and SQLite storage)
uv run tango-posture calibrations list

# Reuse the most recent .tpc calibration (with legacy JSON fallback)
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

Tests cover synthetic and all-angle geometry, confidence gates, calibration storage/reuse, head pitch offset, shoulder tension, Leader/Follower hysteresis, Closed/Open embrace classification, closed-embrace knee flexion, 30-second musical phrase-start locking, silence gating, CLI safeguards, and real-model inference. See the [Tango Posture Coach Manual](docs/TANGO_POSTURE_GUIDE.md), [Manual Validation](docs/manual_validation.md), and [Implementation Report](docs/implementation_report.md).

The in-app tutorial renders the bundled manual as styled HTML, with formatted tables, code blocks, section navigation, and search. Clicking a setting label opens and highlights its explanation. Tutorial content is available offline.
