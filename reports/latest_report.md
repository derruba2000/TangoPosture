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
| Generated (UTC) | `2026-11-05T21:00:33.974850+00:00` |
| Source | `camera:0` |
| Saved Raw Video | [`videos/practice_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_01.mp4) |
| Saved Annotated Overlay Video | [`videos/overlay_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_01.mp4) |
| Run / Session Status | `stopped` |
| Session ID | `uncalibrated-observation-run` |
| Observed Timeline | `00:00.1 (0.1s)` to `00:10.2 (10.2s)` (`10.17s`, `304` frames) |
| Selected Side / Facing | `side=left`, `facing=right` |
| Head-Down Alert Config (`.env`) | Angle `< -15.0 deg` for `>= 3.0s` (neutral offset `-12.0 deg`) |
| Shoulder Tension Config (`.env`) | Elevation `>= +10.0%` for `>= 1.0s` |
| Axis Curvature Config (`axis_curve`) | Entry `>+6.0%` for `>= 1.5s`, Clear `<+3.5%` for `>= 0.75s` |
| Closed-Embrace Knee Bend Config (`.env`) | Min knee bend `>= 8.0 deg` (alert after `1.0s`) |
| Embrace Type Distribution | `CLOSED`: `0` frames, `OPEN`: `0` frames |
| Role Hysteresis Config (`.env`) | Assign after `300 ms`, hold for `5000 ms` before returning to `NONE` |
| Musical 8-Beat Phrase Tracking | `3 phrases` (median `107.1 BPM`, 8 beats/phrase) |
| Runtime Throughput & Latency | `29.9 FPS` (median frame latency `19.3 ms`, dropped frames `1`) |
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
| Frames with Visible Ear & Nose | `75 / 304` | Head horizontal angle coverage |
| Median Head Horizontal Angle | `-3.3 deg` | `0 deg` = nose level with ear; negative = pointing down |
| Minimum (Most Downward) Angle | `-28.6 deg` | Lowest downward pitch observed |
| Maximum (Most Upward) Angle | `+4.0 deg` | Highest pitch observed |
| Total Time Pointing Down (`< -15.0 deg`) | `0.08s` | Cumulative time below downward angle threshold |
| **Confirmed Bad Head Alignment Alerts (`>= 3.0s`)** | **`0`** | Triggered red on-screen `BAD HEAD ALIGNMENT` alert |

### 3.2 Shoulder Tension (Raised / Shrugged Shoulders)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Shoulder | `75 / 304` | Shoulder elevation coverage |
| Median Shoulder Elevation | `-64.1%` | `0%` = relaxed neutral; positive = shoulders raised toward ears |
| Peak Shoulder Elevation | `+18.2%` | Highest shoulder shrug / tension observed |
| Total Time Raised (`>= +10.0%`) | `0.24s` | Cumulative time with elevated shoulders |
| **Confirmed Shoulder Tension Alerts (`>= 1.0s`)** | **`0`** | Triggered red on-screen `LOWER YOUR SHOULDERS` alert |

### 3.2b Full-Body Axis Curvature (Ankle–Hip–Back-of-Head Line)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ankle, Hip & Head | `1 / 304` | Full-body axis curvature coverage |
| Median Hip / Pelvis Offset | `+6.4%` | `0.0%` = hip on reference ankle–back-of-head line; `+` = hip/bump protruding behind line |
| Peak Posterior Curvature | `+6.4%` | Maximum hip protrusion behind ankle–back-of-head line |
| Total Time Above Entry (`>+6.0%`) | `0.00s` | Cumulative time exceeding entry threshold |
| **Confirmed Axis Curvature Alerts (`>= 1.5s` w/ hysteresis `<+3.5%`)** | **`0`** | Triggered red on-screen `HIP OFFSET` alert; separate from the torso axis |

### 3.3 Upper-Body Geometry (Ear–Shoulder–Hip Alignment)

| Metric | Unit | Observed Frames | Median | Min | Max | Interpretation |
| --- | --- | --- | --- | --- | --- | --- |
| `head_forward` | `torso_lengths` | `7 / 304` | `-0.305` | `-0.361` | `-0.258` | Ear forward displacement relative to shoulder & torso length |
| `head_inclination` | `degrees` | `7 / 304` | `-47.461` | `-53.109` | `-42.720` | Ear-shoulder segment angle relative to trunk |
| `trunk_inclination` | `degrees` | `7 / 304` | `+8.851` | `+7.218` | `+17.723` | Projected trunk inclination relative to vertical |

---

## 4. What Went Wrong & Where in Time (Issues Timeline)

### 4.4 Camera Framing & Tracking Abstentions

| Start Time | End Time | Duration | Frames | Primary Reason |
| --- | --- | --- | --- | --- |
| `00:00.1 (0.1s)` | `00:10.2 (10.2s)` | `10.17s` | `304` | One-person mode only |
