# Tango Posture & Body Alignment Report

## 1. What This Report Is & Its Purpose

- **What this report is**: An automated post-run body alignment, embrace-role, musical 8-beat phrase, and movement performance report generated locally by **Tango Posture Coach** from pose-landmark observations.
- **Purpose of this report**:
  1. **Body Alignment Analysis**: Quantify head horizontal pitch (ear-to-nose angle relative to horizontal), head-forward offset (ear relative to shoulder/torso), head segment inclination, and trunk vertical inclination.
  2. **Leader / Follower Embrace Identification**: Detect whether the dancer adopts the **Leader** embrace (left arm raised, right arm horizontal around the follower) or **Follower** embrace (right arm raised, left arm horizontal around the leader's shoulder) and when each role occurs.
  3. **Tango 8-Beat Phrase Tracking**: Track the 8-beat Argentine tango musical phrase (`Beat 1..8 -> 1..8`) and map alignment events to both time and musical phrase/beat.
  4. **Body Performance & Issue Pinpointing (Where in Time)**: Identify every time interval where alignment deviated beyond configured thresholds (such as the head pointing down for longer than the `.env` threshold) or where camera framing obstructed full-body tracking, with exact timestamps so the dancer and teacher can inspect those moments.
  5. **Scope & Disclaimer**: This is a geometric movement observation aid, not a medical assessment or an authority on artistic tango quality.

---

## 2. Run, Runtime Performance & Role Summary

| Field | Value |
| --- | --- |
| Generated (UTC) | `2026-11-03T07:53:24.630164+00:00` |
| Source | `camera:0` |
| Saved Raw Video | [`videos/practice_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_01.mp4) |
| Saved Annotated Overlay Video | [`videos/overlay_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_01.mp4) |
| Run / Session Status | `stopped` |
| Session ID | `uncalibrated-observation-run` |
| Observed Timeline | `00:00.1 (0.1s)` to `00:02.8 (2.8s)` (`2.73s`, `67` frames) |
| Selected Side / Facing | `side=left`, `facing=right` |
| Head-Down Alert Config (`.env`) | Angle `< -15.0 deg` for `>= 3.0s` (neutral offset `-12.0 deg`) |
| Shoulder Tension Config (`.env`) | Elevation `>= +16.0%` for `>= 1.5s` |
| Axis Curvature Config (`axis_curve`) | Entry `>+6.0%` for `>= 1.5s`, Clear `<+3.5%` for `>= 0.75s` |
| Closed-Embrace Knee Bend Config (`.env`) | Min knee bend `>= 6.0 deg` (alert after `1.5s`) |
| Embrace Type Distribution | `CLOSED`: `0` frames, `OPEN`: `0` frames |
| Role Hysteresis Config (`.env`) | Assign after `300 ms`, hold for `5000 ms` before returning to `NONE` |
| Musical 8-Beat Phrase Tracking | `1 phrases` (median `120.0 BPM`, 8 beats/phrase) |
| Runtime Throughput & Latency | `24.3 FPS` (median frame latency `56.4 ms`, dropped frames `15`) |
| **Dominant Embrace Role** | **NONE DETECTED (arms neither in Leader nor Follower embrace)** |

### Saved Video & Overlay Files (For Consultation & Analysis)

- **Raw Practice Video**: [`videos/practice_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_01.mp4)
- **Annotated Overlay Video**: [`videos/overlay_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_01.mp4)

### Detected Leader / Follower Embrace Intervals

- No continuous `LEADER` or `FOLLOWER` arm embrace intervals were detected during this run.

---

## 3. Body Alignment Metrics

### 3.1 Head Horizontal Alignment (Ear-to-Nose Pitch)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Nose | `66 / 67` | Head horizontal angle coverage |
| Median Head Horizontal Angle | `-4.5 deg` | `0 deg` = nose level with ear; negative = pointing down |
| Minimum (Most Downward) Angle | `-7.5 deg` | Lowest downward pitch observed |
| Maximum (Most Upward) Angle | `+0.3 deg` | Highest pitch observed |
| Total Time Pointing Down (`< -15.0 deg`) | `0.00s` | Cumulative time below downward angle threshold |
| **Confirmed Bad Head Alignment Alerts (`>= 3.0s`)** | **`0`** | Triggered red on-screen `BAD HEAD ALIGNMENT` alert |

### 3.2 Shoulder Tension (Raised / Shrugged Shoulders)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Shoulder | `66 / 67` | Shoulder elevation coverage |
| Median Shoulder Elevation | `+15.8%` | `0%` = relaxed neutral; positive = shoulders raised toward ears |
| Peak Shoulder Elevation | `+20.2%` | Highest shoulder shrug / tension observed |
| Total Time Raised (`>= +16.0%`) | `0.63s` | Cumulative time with elevated shoulders |
| **Confirmed Shoulder Tension Alerts (`>= 1.5s`)** | **`0`** | Triggered red on-screen `LOWER YOUR SHOULDERS` alert |

### 3.2b Full-Body Axis Curvature (Ankle–Hip–Back-of-Head Line)

- Axis curvature could not be computed (ankle, hip, or ear not visible).

### 3.3 Upper-Body Geometry (Ear–Shoulder–Hip Alignment)

| Metric | Unit | Observed Frames | Median | Min | Max | Interpretation |
| --- | --- | --- | --- | --- | --- | --- |
| `head_forward` | `torso_lengths` | `0 / 67` | `N/A` | `N/A` | `N/A` | Requires visible left ear, shoulder, and hip in frame |
| `head_inclination` | `degrees` | `0 / 67` | `N/A` | `N/A` | `N/A` | Requires visible left ear, shoulder, and hip in frame |
| `trunk_inclination` | `degrees` | `0 / 67` | `N/A` | `N/A` | `N/A` | Requires visible left ear, shoulder, and hip in frame |

---

## 4. What Went Wrong & Where in Time (Issues Timeline)

### 4.4 Camera Framing & Tracking Abstentions

| Start Time | End Time | Duration | Frames | Primary Reason |
| --- | --- | --- | --- | --- |
| `00:00.1 (0.1s)` | `00:02.8 (2.8s)` | `2.73s` | `67` | Keep shoulders and hips visible in frame |
