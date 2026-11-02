# Manual: Configuring a Good Reference & Comparing Your Practice Overlays, Reports, and Results

This manual explains step-by-step how to set up and capture a high-quality **Reference Posture**, how each body alignment metric works (**head horizontal alignment**, **vertical alignment**, and **shoulder/embrace positions**), and how to compare your practice **overlays (`videos/`)**, **Markdown reports (`reports/`)**, and **session exports (`data/exports/`)** against your reference.

---

## 1. What You Are Measuring

Tango Posture Coach tracks four complementary layers of geometry from your camera feed:

| Alignment Area | Metric / Indicator | What It Measures Geometrically | Where You See It |
| --- | --- | --- | --- |
| **1. Head Horizontal Alignment** | `Head horiz` (`deg`) & `BAD HEAD ALIGNMENT` alert | Angle of the **ear-to-nose vector** relative to a true horizontal line (`0.0°` = nose level with ear; negative = head tilted/looking down). | Drawn directly on the head in the video overlay + Section 3.1 & Section 4 of the Markdown report. |
| **2. Vertical Alignment (Torso / Axis)** | `trunk_inclination` (`deg`) | Angle of the **hip-to-shoulder line** relative to image vertical (`0.0°` = vertical axis; `+` = leaning forward in facing direction; `-` = leaning backward). | Overlay status panel (`smoothed` & `delta` vs. reference), Section 3.2 & Section 3.3 of the report, `observations.csv`. |
| **3. Head-to-Shoulder Vertical & Forward Alignment** | `head_inclination` (`deg`) & `head_forward` (`torso_lengths`) | How far your ear tilts (`deg`) or shifts forward (`torso_lengths`) **relative to your shoulder and trunk axis**. | Overlay status panel (`smoothed` & `delta` vs. reference), Section 3.2 & Section 3.3 of the report, `observations.csv`. |
| **4. Shoulder & Arm Embrace Positions** | `Role: LEADER / FOLLOWER / NONE` + `view_width` gate | **1)** Shoulder-to-hip/ear hinge; **2)** Projected shoulder width (`<= 0.45` torso lengths to verify your shoulders stay side-on without twisting open); **3)** Left/right shoulder–elbow–wrist geometry to detect **Leader** vs. **Follower** embrace. | Top-left `Role:` badge on the overlay, Section 2 of the report (`Detected Leader / Follower Embrace Intervals`). |

---

## 2. Physical Setup for a Reliable Reference

Because all measurements compare your practice geometry to your recorded reference, the physical camera setup is the foundation:

1. **Camera Height & Level**:
   - Place the laptop or webcam on a stable surface at roughly **chest-to-waist height** (`0.9m – 1.3m` off the floor).
   - Keep the camera **level (no tilt/roll)** so image vertical matches true gravity vertical for your `trunk_inclination` measurement.
2. **Full-Body Framing (Head to Ankles)**:
   - Step back far enough (`~2.5m – 3.5m`) that your **entire body from the top of your head (`nose`/`ear`) down to your `ankles`** stays inside the frame along your walking lane.
   - *Note*: If your ankles go below the bottom edge of the frame, full-body reference calibration will reject those frames (`Framing inadequate: keep nose and ankles in frame`), although upper-body and head horizontal metrics will still be logged in the Markdown report.
3. **Side-On Orientation (`--side` and `--facing`)**:
   - Stand **side-on (90° profile)** to the camera so one anatomical side faces the lens.
   - `--side left` or `--side right`: Which side of your body is closest to the camera.
   - `--facing left` or `--facing right`: Which side of the **unmirrored** screen you are looking toward.
   - Example: If your **left** shoulder/hip is closest to the camera, you are facing toward the **right** edge of the unmirrored camera image (`--side left --facing right`).

---

## 3. Configuring Thresholds (`.env` and `configs/default.toml`)

### 3.1 `.env` Settings (Head Alignment, Role Hysteresis & 8-Beat Phrase Tracker)

Edit [`.env`](../.env) in the project root to tune real-time visual alerts and role stability:

```dotenv
# Head horizontal alignment configuration
# Triggers red "BAD HEAD ALIGNMENT" alert if ear-to-nose angle stays below -15.0 deg for >= 3.0s
HEAD_DOWN_DURATION_SECONDS=3.0
HEAD_DOWN_ANGLE_DEGREES=-15.0

# Tango 8-beat musical phrase tracker configuration
BEAT_TRACKING_ENABLED=true
TANGO_BPM=120.0
BEAT_ANALYSIS_SECONDS=30.0

# Leader / Follower role recognition hysteresis (milliseconds)
# 300ms continuous detection to switch from NONE -> LEADER/FOLLOWER
# 5000ms hold time before returning from LEADER/FOLLOWER -> NONE
ROLE_ASSIGN_MS=300
ROLE_HOLD_MS=5000
```

### 3.2 `configs/default.toml` Settings (Reference Stability & Deviation Thresholds)

Copy [`configs/default.toml`](../configs/default.toml) (e.g., to `configs/my_practice.toml`) if you want to customize how strictly your practice is compared to your reference:

```toml
# Minimum landmark confidence (0.0 to 1.0)
confidence = 0.70

# Side-on shoulder/hip width gate (checks if shoulders rotate open toward camera)
max_view_width = 0.45          # Projected shoulder/hip width must be <= 45% of torso length
view_change_tolerance = 0.15   # Max allowed change in shoulder/hip width ratio from your reference

# Persistence & hysteresis timing
smoothing = 0.25               # Exponential smoothing time constant (seconds)
entry_duration = 1.5           # Deviation must persist continuously for 1.5s to confirm an event
clear_duration = 0.75          # Must stay below clear threshold for 0.75s to close the event

# Calibration quality requirements (during the 12-second capture window)
calibration_duration = 12.0
calibration_valid_duration = 9.0
calibration_samples = 60
offset_mad = 0.03              # Max allowed jitter (MAD) in head_forward during calibration
angle_mad = 4.0                # Max allowed jitter (MAD in degrees) in head/trunk angles

# Reference comparison thresholds (Delta from your Reference Baseline)
[head_forward]
entry = 0.08                   # +8% of torso length farther forward than reference -> DEVIATION
clear = 0.05                   # Drops back below +5% -> WITHIN_REFERENCE

[head_inclination]
entry = 10.0                   # >= 10 deg change from reference ear-shoulder angle -> DEVIATION
clear = 6.0

[trunk_inclination]
entry = 8.0                    # >= 8 deg change from reference vertical trunk axis -> DEVIATION
clear = 5.0
```

---

## 4. How to Record & Validate a Good Reference

You can create a reference either **live on the webcam** or **from a 12-second interval of a recorded video**.

### Option A: Live Webcam Reference Capture (Recommended)

1. Start the application:
   ```bash
   uv run tango-posture run --camera 0 --side left --facing right \
     --reference-name "Tango Axis & Embrace Reference" \
     --record-video videos/practice.mp4 \
     --record-overlay videos/overlay.mp4
   ```
2. **Verify Framing in `READY` Mode**:
   - Look at the bottom status line of the window. Make sure it says:
     `Image quality: VALID (side-on heuristic)`
   - Check that your `Head horiz` angle is near `0°` (yellow horizontal line and green ear-to-nose line are nearly parallel) and that `Role: LEADER` (or `FOLLOWER`) appears in the top-left once you raise your embrace arms for `300 ms`.
3. **Press `C` to Calibrate**:
   - You get a **3-second countdown** (`Adopt reference: countdown ...`) to settle into your best vertical axis, shoulder position, and level head gaze.
   - Hold your reference posture steadily for the **12-second capture** (`Capturing reference: ... remaining`).
4. **Check the Calibration Quality Output**:
   - When the 12 seconds finish, the app checks that you had at least **9.0 valid seconds**, **60 valid frames**, and low postural sway (**MAD** $\le 0.03$ torso lengths and $\le 4^\circ$).
   - If successful, it displays:
     `Reference: 11.4s valid, MAD {...}. A: accept / R: retry`
   - **Press `A`** to accept and save the reference to your local SQLite database (`data/tango.sqlite3`), or **`R`** to retry if you moved during capture.
5. **Press `S` to Start Practicing**:
   - Begin your solo walk / phrasing practice to music. Press **`E`** when done (or **`Q`** to stop).

### Option B: Listing & Reusing Your Saved Reference Across Sessions

Once you have accepted a good reference, you do **not** need to recalibrate every run as long as your camera stays in the same spot:

1. List your saved references to get the Reference `id` (`REF_ID`):
   ```bash
   uv run tango-posture calibrations list
   ```
2. Run future practice sessions against that exact reference:
   ```bash
   uv run tango-posture run --camera 0 --calibration REF_ID \
     --confirm-camera-unchanged \
     --record-video videos/practice_run2.mp4 \
     --record-overlay videos/overlay_run2.mp4
   ```

---

## 5. How to Compare Your Practice Overlays, Reports, and Results Against Your Reference

Every practice run produces three outputs that work together:

1. **Annotated Overlay Video (`videos/overlay.mp4`)** — visual + audio frame-by-frame inspection.
2. **Markdown Report (`reports/latest_report.md` & `reports/report_<timestamp>.md`)** — human-readable diagnostic summary with exact timestamps of every issue.
3. **Session Exports (`data/exports/<SESSION_ID>/`)** — `summary.json`, `observations.csv`, and `events.csv` for quantitative comparison across sessions.

---

### 5.1 Reading the Practice Overlay (`videos/overlay.mp4`)

Open `videos/overlay.mp4` (linked directly at the top of your Markdown report). It contains your practice video with embedded microphone audio and four visual overlays:

1. **Head Horizontal Alignment Protractor (On Your Head)**:
   - **Yellow horizontal line**: True horizontal (`0.0°`) passing through your visible ear.
   - **Colored ear-to-nose vector & arc**:
     - **Green**: Head is level (`angle >= -15.0°`).
     - **Orange**: Head is tilted down (`angle < -15.0°`), timer counting up (`(1.2s)`).
     - **Red + `BAD HEAD ALIGNMENT` banner**: Head has pointed down below `-15.0°` continuously for more than `3.0s`.
2. **Shoulder & Embrace Role Badge (Top-Left)**:
   - Displays `Role: LEADER`, `Role: FOLLOWER`, or `Role: NONE`.
   - Watch this during walking and turns/changes of weight: if your embrace arms drop so far that the role resets to `NONE` (after the 5000 ms hold), your elbow/wrist height drifted out of the embrace zone.
3. **8-Beat Tango Phrase Bar (Top-Right)**:
   - Displays `Phrase N | BEAT X/8 | YYY BPM` and highlights the current beat `1..8` in green so you can see **which musical beat** you were on when a posture deviation happened (e.g., dropping the head or leaning the chest forward on Beat 1 or Beat 5).
4. **Reference Comparison Rows (Bottom Panel)**:
   During an active session (`RUNNING`), the bottom panel shows three live comparison rows:
   ```text
   head_forward:      0.112 | delta +0.018 | WITHIN_REFERENCE
   head_inclination: 14.200 | delta +2.100 | WITHIN_REFERENCE
   trunk_inclination: 2.400 | delta +9.300 | DEVIATION
   ```
   - **`smoothed`**: Your current smoothed measurement.
   - **`delta`**: **Current measurement minus your Reference baseline** (`smoothed - reference_baseline`).
     - `delta = 0.000` means you are matching your reference posture.
     - For `trunk_inclination` (vertical alignment): a positive `delta` (e.g., `+9.3°`) means your torso/shoulders are leaning `9.3°` **farther forward** than your reference axis; a negative `delta` means you are leaning backward relative to your reference.
     - For `head_forward` (head vs. shoulder position): a positive `delta` (e.g., `+0.095`) means your head has crept forward ahead of your shoulder by `9.5%` of your torso length compared to your reference.
   - **`state`**:
     - `WITHIN_REFERENCE`: Within your configured thresholds.
     - `PENDING`: You crossed the `entry` threshold, but for less than `1.5s` so far.
     - `DEVIATION`: You stayed beyond the threshold for `>= 1.5s` (confirmed posture drift).

---

### 5.2 Reading the Markdown Report (`reports/latest_report.md`)

Open `reports/latest_report.md` after any run:

1. **Section 2 (`Run, Runtime Performance & Role Summary`)**:
   - Contains clickable links to your **Saved Raw Video** and **Saved Annotated Overlay Video** so you can jump straight to the video.
   - Shows your **Dominant Embrace Role** and the exact time spans (`MM:SS.s -> MM:SS.s`) where you maintained the `LEADER` or `FOLLOWER` shoulder/arm embrace.
2. **Section 3 (`Body Alignment Metrics`)**:
   - **Section 3.1 (`Head Horizontal Alignment`)**: Shows your **Median**, **Minimum (most downward)**, and **Maximum** head horizontal angle (`deg`) across the entire run, plus total seconds spent with the head pointing down.
   - **Section 3.2 (`Upper-Body Geometry: Ear–Shoulder–Hip`)**: Shows your median, min, and max values for:
     - `head_forward` (ear position relative to shoulder),
     - `head_inclination` (neck/head vertical inclination relative to trunk),
     - `trunk_inclination` (vertical alignment of the hip-to-shoulder axis).
   - **Section 3.3 (`Reference-Relative Session Summary`)**: Shows a side-by-side table of your **Reference Baseline** vs. your **Median Delta**, **Peak Delta**, **% of Valid Time in Deviation**, and **Confirmed Event Count** for each metric.
3. **Section 4 (`What Went Wrong & Where in Time`)**:
   - Lists every specific issue with **exact start and end timestamps (`MM:SS.s`)**:
     - **Confirmed Bad Head Alignment (`>= 3.0s`)**: Exact timestamps and worst downward angle so you can scrub `videos/overlay.mp4` to that second.
     - **Confirmed Reference Posture Deviations**: Exact onset, detection, end time, duration, and peak deviation from your reference for `head_forward`, `head_inclination`, and `trunk_inclination`.
     - **Camera Framing / Quality Abstentions**: Exact timestamps if your ankles stepped out of frame or if your shoulders rotated too far toward the camera (`Side-on view unreliable`).

---

### 5.3 Comparing Multiple Practice Sessions Over Time

To track your progress across multiple practice runs using the same reference:

1. Compare the **Section 3.1 & Section 3.3 tables** across reports in `reports/report_*.md` (or `data/exports/<SESSION_ID>/summary.json`):
   - **Head Horizontal Alignment**: Is your **Median Head Horizontal Angle** staying closer to `0°`, and did **Confirmed Bad Head Alignment Episodes (`>= 3.0s`)** drop to `0`?
   - **Vertical Alignment (`trunk_inclination`)**: Is your **Median Delta** closer to `0.0°` and **Peak Absolute Delta** below `8.0°` during walking phrases?
   - **Head & Shoulder Relationship (`head_forward` & `head_inclination`)**: Is your **Confirmed Deviation % of Valid Time** decreasing from session to session while keeping your `LEADER` / `FOLLOWER` embrace active?

