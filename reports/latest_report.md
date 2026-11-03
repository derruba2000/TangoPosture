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
| Generated (UTC) | `2026-11-03T08:32:32.154042+00:00` |
| Source | `camera:0` |
| Saved Raw Video | [`videos/practice_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_01.mp4) |
| Saved Annotated Overlay Video | [`videos/overlay_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_01.mp4) |
| Run / Session Status | `stopped` |
| Session ID | `uncalibrated-observation-run` |
| Observed Timeline | `00:00.1 (0.1s)` to `01:30.0 (90.0s)` (`89.97s`, `2092` frames) |
| Selected Side / Facing | `side=left`, `facing=right` |
| Head-Down Alert Config (`.env`) | Angle `< -15.0 deg` for `>= 3.0s` (neutral offset `-12.0 deg`) |
| Shoulder Tension Config (`.env`) | Elevation `>= +16.0%` for `>= 1.5s` |
| Axis Curvature Config (`axis_curve`) | Entry `>+6.0%` for `>= 1.5s`, Clear `<+3.5%` for `>= 0.75s` |
| Closed-Embrace Knee Bend Config (`.env`) | Min knee bend `>= 6.0 deg` (alert after `1.5s`) |
| Embrace Type Distribution | `CLOSED`: `311` frames, `OPEN`: `0` frames |
| Role Hysteresis Config (`.env`) | Assign after `300 ms`, hold for `5000 ms` before returning to `NONE` |
| Musical 8-Beat Phrase Tracking | `4 phrases` (median `114.8 BPM`, 8 beats/phrase) |
| Runtime Throughput & Latency | `23.3 FPS` (median frame latency `38.5 ms`, dropped frames `16`) |
| **Dominant Embrace Role** | **LEADER (left arm raised, right arm horizontal)** |

### Saved Video & Overlay Files (For Consultation & Analysis)

- **Raw Practice Video**: [`videos/practice_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/practice_01.mp4)
- **Annotated Overlay Video**: [`videos/overlay_01.mp4`](file:///Users/joaoramo/src/Personal/TangoPosture/videos/overlay_01.mp4)

### Detected Leader / Follower Embrace Intervals

- **LEADER** (`00:27.3 (27.3s)` -> `00:40.8 (40.8s)`, duration `13.4s`, `311` frames): Left arm raised, right arm horizontal.

---

## 3. Body Alignment Metrics

### 3.1 Head Horizontal Alignment (Ear-to-Nose Pitch)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Nose | `1937 / 2092` | Head horizontal angle coverage |
| Median Head Horizontal Angle | `-13.0 deg` | `0 deg` = nose level with ear; negative = pointing down |
| Minimum (Most Downward) Angle | `-74.0 deg` | Lowest downward pitch observed |
| Maximum (Most Upward) Angle | `+34.0 deg` | Highest pitch observed |
| Total Time Pointing Down (`< -15.0 deg`) | `31.62s` | Cumulative time below downward angle threshold |
| **Confirmed Bad Head Alignment Alerts (`>= 3.0s`)** | **`2`** | Triggered red on-screen `BAD HEAD ALIGNMENT` alert |

### 3.2 Shoulder Tension (Raised / Shrugged Shoulders)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Shoulder | `1940 / 2092` | Shoulder elevation coverage |
| Median Shoulder Elevation | `+19.1%` | `0%` = relaxed neutral; positive = shoulders raised toward ears |
| Peak Shoulder Elevation | `+60.3%` | Highest shoulder shrug / tension observed |
| Total Time Raised (`>= +16.0%`) | `54.14s` | Cumulative time with elevated shoulders |
| **Confirmed Shoulder Tension Alerts (`>= 1.5s`)** | **`3`** | Triggered red on-screen `LOWER YOUR SHOULDERS` alert |

### 3.2b Full-Body Axis Curvature (Ankle–Hip–Back-of-Head Line)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ankle, Hip & Head | `1848 / 2092` | Full-body axis curvature coverage |
| Median Hip / Pelvis Offset | `-1.1%` | `0.0%` = hip on reference ankle–back-of-head line; `+` = hip/bump protruding behind line |
| Peak Posterior Curvature | `+1.0%` | Maximum hip protrusion behind ankle–back-of-head line |
| Total Time Above Entry (`>+6.0%`) | `0.00s` | Cumulative time exceeding entry threshold |
| **Confirmed Axis Curvature Alerts (`>= 1.5s` w/ hysteresis `<+3.5%`)** | **`0`** | Triggered red on-screen `AXIS CURVING TOO MUCH` alert |

### 3.3 Upper-Body Geometry (Ear–Shoulder–Hip Alignment)

| Metric | Unit | Observed Frames | Median | Min | Max | Interpretation |
| --- | --- | --- | --- | --- | --- | --- |
| `head_forward` | `torso_lengths` | `1909 / 2092` | `-0.094` | `-0.409` | `+0.250` | Ear forward displacement relative to shoulder & torso length |
| `head_inclination` | `degrees` | `1909 / 2092` | `-14.831` | `-78.752` | `+47.487` | Ear-shoulder segment angle relative to trunk |
| `trunk_inclination` | `degrees` | `1909 / 2092` | `+4.450` | `-29.270` | `+18.157` | Projected trunk inclination relative to vertical |

---

## 4. What Went Wrong & Where in Time (Issues Timeline)

### 4.1 Confirmed Bad Head Alignment (`< -15.0 deg` for `>= 3.0s` — RED ALERT)

| Start Time (Phrase/Beat) | Red Alert Triggered At | End Time | Duration | Worst Angle | Median Angle | What Went Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| `01:03.2 (63.2s)` | `01:06.2 (66.2s)` | `01:21.3 (81.3s)` | `18.08s` | `-30.1 deg` | `-23.8 deg` | **Bad head alignment**: head pointed downward continuously for `18.1s` |
| `01:21.4 (81.4s) (P1 B5/8)` | `01:24.4 (84.4s)` | `01:26.4 (86.4s)` | `5.02s` | `-29.9 deg` | `-23.4 deg` | **Bad head alignment**: head pointed downward continuously for `5.0s` |

### 4.2 Confirmed Shoulder Tension (`>= +16.0%` for `>= 1.5s` — LOWER YOUR SHOULDERS)

| Start Time (Phrase/Beat) | Alert Triggered At | End Time | Duration | Peak Elevation | Median Elevation | What Went Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| `00:28.2 (28.2s)` | `00:29.7 (29.7s)` | `00:31.6 (31.6s)` | `3.42s` | `+23.7%` | `+19.4%` | **Shoulder tension**: shoulders raised continuously for `3.4s` (lower your shoulders) |
| `00:42.1 (42.1s)` | `00:43.6 (43.6s)` | `00:51.6 (51.6s)` | `9.52s` | `+22.9%` | `+19.0%` | **Shoulder tension**: shoulders raised continuously for `9.5s` (lower your shoulders) |
| `00:51.7 (51.7s)` | `00:53.2 (53.2s)` | `01:27.6 (87.6s)` | `35.95s` | `+37.8%` | `+24.8%` | **Shoulder tension**: shoulders raised continuously for `35.9s` (lower your shoulders) |

### 4.2b Confirmed Unbent Knees in Closed Embrace (`< 6.0 deg` for `>= 1.5s` — BEND YOUR KNEES)

| Start Time (Phrase/Beat) | Alert Triggered At | End Time | Duration | Min Knee Bend | Median Knee Bend | What Went Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| `00:14.8 (14.8s) (P4 B3/8)` | `00:16.3 (16.3s)` | `00:19.8 (19.8s)` | `5.06s` | `3.2 deg` | `4.2 deg` | **Straight knees in closed embrace**: knee bend `< 6.0 deg` for `5.1s` (bend your knees) |

### 4.2 Brief Head-Down Dips (`< -15.0 deg` for `< 3.0s`)

| Start Time | End Time | Duration | Worst Angle | Note |
| --- | --- | --- | --- | --- |
| `00:01.4 (1.4s)` | `00:01.7 (1.7s)` | `0.22s` | `-74.0 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `00:06.8 (6.8s)` | `00:07.1 (7.1s)` | `0.32s` | `-37.8 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `00:07.9 (7.9s)` | `00:09.2 (9.2s)` | `1.27s` | `-61.9 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `00:36.8 (36.8s)` | `00:37.1 (37.1s)` | `0.30s` | `-17.7 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `00:54.6 (54.6s)` | `00:54.9 (54.9s)` | `0.22s` | `-17.6 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `00:56.4 (56.4s)` | `00:56.6 (56.6s)` | `0.22s` | `-19.0 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `00:59.7 (59.7s)` | `01:00.4 (60.4s)` | `0.61s` | `-20.2 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:00.4 (60.4s)` | `01:01.0 (61.0s)` | `0.56s` | `-18.8 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:01.2 (61.2s)` | `01:01.6 (61.6s)` | `0.39s` | `-17.2 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:01.7 (61.7s)` | `01:02.0 (62.0s)` | `0.30s` | `-22.9 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:02.2 (62.2s)` | `01:03.1 (63.1s)` | `0.95s` | `-20.2 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:26.5 (86.5s)` | `01:26.8 (86.8s)` | `0.35s` | `-20.9 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:27.0 (87.0s)` | `01:27.3 (87.3s)` | `0.30s` | `-23.5 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |

### 4.4 Camera Framing & Tracking Abstentions

| Start Time | End Time | Duration | Frames | Primary Reason |
| --- | --- | --- | --- | --- |
| `00:00.1 (0.1s)` | `00:03.6 (3.6s)` | `3.49s` | `79` | One-person mode only |
| `00:04.1 (4.1s)` | `00:04.1 (4.1s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:04.2 (4.2s)` | `00:04.6 (4.6s)` | `0.43s` | `11` | No person detected |
| `00:05.2 (5.2s)` | `00:05.2 (5.2s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:05.3 (5.3s)` | `00:05.4 (5.4s)` | `0.09s` | `3` | Keep head/nose visible in frame |
| `00:05.5 (5.5s)` | `00:05.5 (5.5s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:05.5 (5.5s)` | `00:05.5 (5.5s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:05.8 (5.8s)` | `00:05.9 (5.9s)` | `0.04s` | `2` | Keep head/nose visible in frame |
| `00:06.0 (6.0s)` | `00:06.0 (6.0s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:06.1 (6.1s)` | `00:06.1 (6.1s)` | `0.04s` | `2` | Keep head/nose visible in frame |
| `00:06.2 (6.2s)` | `00:06.2 (6.2s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:06.6 (6.6s)` | `00:06.6 (6.6s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:06.8 (6.8s)` | `00:06.8 (6.8s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:06.9 (6.9s)` | `00:06.9 (6.9s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `00:07.0 (7.0s)` | `00:07.2 (7.2s)` | `0.13s` | `5` | Keep head/nose visible in frame |
| `00:07.3 (7.3s)` | `00:07.6 (7.6s)` | `0.29s` | `9` | Keep head/nose visible in frame |
| `00:07.7 (7.7s)` | `00:08.1 (8.1s)` | `0.47s` | `13` | Keep head/nose visible in frame |
| `00:08.2 (8.2s)` | `00:08.9 (8.9s)` | `0.67s` | `18` | Keep head/nose visible in frame |
| `00:08.9 (8.9s)` | `00:09.0 (9.0s)` | `0.07s` | `3` | Keep shoulders and hips visible in frame |
| `00:09.1 (9.1s)` | `00:09.2 (9.2s)` | `0.07s` | `3` | Keep head/nose visible in frame |
| `00:09.2 (9.2s)` | `00:10.3 (10.3s)` | `1.08s` | `27` | No person detected |
| `00:12.3 (12.3s)` | `00:12.3 (12.3s)` | `0.00s` | `1` | No person detected |
| `00:12.4 (12.4s)` | `00:12.4 (12.4s)` | `0.00s` | `1` | One-person mode only |
| `01:27.8 (87.8s)` | `01:27.8 (87.8s)` | `0.00s` | `1` | Keep head/nose visible in frame |
| `01:27.9 (87.9s)` | `01:29.5 (89.5s)` | `1.57s` | `41` | No person detected |
| `01:29.6 (89.6s)` | `01:30.0 (90.0s)` | `0.47s` | `12` | One-person mode only |
