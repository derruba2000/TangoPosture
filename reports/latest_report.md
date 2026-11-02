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
| Generated (UTC) | `2026-11-02T23:11:41.148490+00:00` |
| Source | `camera:0` |
| Saved Raw Video | [`videos/practice_05.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_05.mp4) |
| Saved Annotated Overlay Video | [`videos/overlay_05.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_05.mp4) |
| Run / Session Status | `stopped` |
| Session ID | `uncalibrated-observation-run` |
| Observed Timeline | `00:00.1 (0.1s)` to `01:54.0 (114.0s)` (`113.94s`, `2899` frames) |
| Selected Side / Facing | `side=left`, `facing=right` |
| Head-Down Alert Config (`.env`) | Angle `< -15.0 deg` for `>= 3.0s` (neutral offset `-12.0 deg`) |
| Shoulder Tension Config (`.env`) | Elevation `>= +16.0%` for `>= 1.5s` |
| Role Hysteresis Config (`.env`) | Assign after `300 ms`, hold for `5000 ms` before returning to `NONE` |
| Musical 8-Beat Phrase Tracking | `17 phrases` (median `132.6 BPM`, 8 beats/phrase) |
| Runtime Throughput & Latency | `25.4 FPS` (median frame latency `49.7 ms`, dropped frames `299`) |
| **Dominant Embrace Role** | **LEADER (left arm raised, right arm horizontal)** |

### Saved Video & Overlay Files (For Consultation & Analysis)

- **Raw Practice Video**: [`videos/practice_05.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_05.mp4)
- **Annotated Overlay Video**: [`videos/overlay_05.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_05.mp4)

### Detected Leader / Follower Embrace Intervals

- **LEADER** (`00:06.9 (6.9s)` -> `00:31.5 (31.5s)`, duration `24.5s`, `636` frames): Left arm raised, right arm horizontal.

---

## 3. Body Alignment Metrics

### 3.1 Head Horizontal Alignment (Ear-to-Nose Pitch)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Nose | `2848 / 2899` | Head horizontal angle coverage |
| Median Head Horizontal Angle | `-5.4 deg` | `0 deg` = nose level with ear; negative = pointing down |
| Minimum (Most Downward) Angle | `-44.7 deg` | Lowest downward pitch observed |
| Maximum (Most Upward) Angle | `+56.2 deg` | Highest pitch observed |
| Total Time Pointing Down (`< -15.0 deg`) | `0.90s` | Cumulative time below downward angle threshold |
| **Confirmed Bad Head Alignment Alerts (`>= 3.0s`)** | **`0`** | Triggered red on-screen `BAD HEAD ALIGNMENT` alert |

### 3.2 Shoulder Tension (Raised / Shrugged Shoulders)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Shoulder | `2848 / 2899` | Shoulder elevation coverage |
| Median Shoulder Elevation | `-9.5%` | `0%` = relaxed neutral; positive = shoulders raised toward ears |
| Peak Shoulder Elevation | `+178.4%` | Highest shoulder shrug / tension observed |
| Total Time Raised (`>= +16.0%`) | `8.33s` | Cumulative time with elevated shoulders |
| **Confirmed Shoulder Tension Alerts (`>= 1.5s`)** | **`1`** | Triggered red on-screen `LOWER YOUR SHOULDERS` alert |

### 3.3 Upper-Body Geometry (Ear–Shoulder–Hip Alignment)

| Metric | Unit | Observed Frames | Median | Min | Max | Interpretation |
| --- | --- | --- | --- | --- | --- | --- |
| `head_forward` | `torso_lengths` | `614 / 2899` | `-0.269` | `-0.514` | `+0.022` | Ear forward displacement relative to shoulder & torso length |
| `head_inclination` | `degrees` | `614 / 2899` | `-32.573` | `-84.671` | `+2.755` | Ear-shoulder segment angle relative to trunk |
| `trunk_inclination` | `degrees` | `614 / 2899` | `+3.619` | `-178.068` | `+179.079` | Projected trunk inclination relative to vertical |

---

## 4. What Went Wrong & Where in Time (Issues Timeline)

### 4.2 Confirmed Shoulder Tension (`>= +16.0%` for `>= 1.5s` — LOWER YOUR SHOULDERS)

| Start Time (Phrase/Beat) | Alert Triggered At | End Time | Duration | Peak Elevation | Median Elevation | What Went Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| `00:21.8 (21.8s)` | `00:23.3 (23.3s)` | `00:23.8 (23.8s)` | `1.93s` | `+178.4%` | `+29.1%` | **Shoulder tension**: shoulders raised continuously for `1.9s` (lower your shoulders) |

### 4.2 Brief Head-Down Dips (`< -15.0 deg` for `< 3.0s`)

| Start Time | End Time | Duration | Worst Angle | Note |
| --- | --- | --- | --- | --- |
| `00:29.9 (29.9s)` | `00:30.4 (30.4s)` | `0.56s` | `-44.7 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |

### 4.4 Camera Framing & Tracking Abstentions

| Start Time | End Time | Duration | Frames | Primary Reason |
| --- | --- | --- | --- | --- |
| `00:00.1 (0.1s)` | `01:54.0 (114.0s)` | `113.94s` | `2899` | Keep nose and at least one ankle visible in frame |
