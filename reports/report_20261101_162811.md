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
| Generated (UTC) | `2026-11-01T16:28:11.673763+00:00` |
| Source | `camera:0` |
| Run / Session Status | `stopped` |
| Session ID | `uncalibrated-observation-run` |
| Observed Timeline | `00:00.1 (0.1s)` to `01:59.9 (119.9s)` (`119.81s`, `3331` frames) |
| Selected Side / Facing | `side=left`, `facing=right` |
| Head-Down Alert Config (`.env`) | Angle `< -15.0 deg` for `>= 3.0s` |
| Musical 8-Beat Phrase Tracking | `16 phrases` (median `121.4 BPM`, 8 beats/phrase) |
| Runtime Throughput & Latency | `27.8 FPS` (median frame latency `52.0 ms`, dropped frames `261`) |
| **Dominant Embrace Role** | **LEADER (left arm raised, right arm horizontal)** |

### Detected Leader / Follower Embrace Intervals

- **LEADER** (`00:21.6 (21.6s)` -> `00:21.6 (21.6s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`00:27.4 (27.4s)` -> `00:27.4 (27.4s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`00:36.3 (36.3s)` -> `00:36.3 (36.3s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`00:45.4 (45.4s)` -> `00:45.4 (45.4s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:11.5 (71.5s)` -> `01:11.5 (71.5s)`, duration `0.0s`, `2` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:21.7 (81.7s)` -> `01:21.7 (81.7s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:27.0 (87.0s)` -> `01:27.0 (87.0s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:38.1 (98.1s)` -> `01:38.1 (98.1s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:38.2 (98.2s)` -> `01:38.2 (98.2s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:38.3 (98.3s)` -> `01:38.3 (98.3s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:46.7 (106.7s)` -> `01:46.7 (106.7s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:48.3 (108.3s)` -> `01:48.3 (108.3s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:48.3 (108.3s)` -> `01:48.3 (108.3s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:48.6 (108.6s)` -> `01:48.6 (108.6s)`, duration `0.0s`, `1` frames): Left arm raised, right arm horizontal.
- **LEADER** (`01:56.1 (116.1s)` -> `01:56.1 (116.1s)`, duration `0.1s`, `2` frames): Left arm raised, right arm horizontal.

---

## 3. Body Alignment Metrics

### 3.1 Head Horizontal Alignment (Ear-to-Nose Pitch)

| Metric | Value | Interpretation |
| --- | --- | --- |
| Frames with Visible Ear & Nose | `3321 / 3331` | Head horizontal angle coverage |
| Median Head Horizontal Angle | `-22.4 deg` | `0 deg` = nose level with ear; negative = pointing down |
| Minimum (Most Downward) Angle | `-38.3 deg` | Lowest downward pitch observed |
| Maximum (Most Upward) Angle | `+4.8 deg` | Highest pitch observed |
| Total Time Pointing Down (`< -15.0 deg`) | `105.68s` | Cumulative time below downward angle threshold |
| **Confirmed Bad Head Alignment Alerts (`>= 3.0s`)** | **`5`** | Triggered red on-screen `BAD HEAD ALIGNMENT` alert |

### 3.2 Upper-Body Geometry (Ear–Shoulder–Hip Alignment)

| Metric | Unit | Observed Frames | Median | Min | Max | Interpretation |
| --- | --- | --- | --- | --- | --- | --- |
| `head_forward` | `torso_lengths` | `0 / 3331` | `N/A` | `N/A` | `N/A` | Requires visible left ear, shoulder, and hip in frame |
| `head_inclination` | `degrees` | `0 / 3331` | `N/A` | `N/A` | `N/A` | Requires visible left ear, shoulder, and hip in frame |
| `trunk_inclination` | `degrees` | `0 / 3331` | `N/A` | `N/A` | `N/A` | Requires visible left ear, shoulder, and hip in frame |

---

## 4. What Went Wrong & Where in Time (Issues Timeline)

### 4.1 Confirmed Bad Head Alignment (`< -15.0 deg` for `>= 3.0s` — RED ALERT)

| Start Time (Phrase/Beat) | Red Alert Triggered At | End Time | Duration | Worst Angle | Median Angle | What Went Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| `00:02.3 (2.3s) (P1 B1/8)` | `00:05.3 (5.3s)` | `00:06.4 (6.4s)` | `4.07s` | `-33.7 deg` | `-29.5 deg` | **Bad head alignment**: head pointed downward continuously for `4.1s` |
| `00:07.1 (7.1s) (P1 B1/8)` | `00:10.1 (10.1s)` | `00:45.7 (45.7s)` | `38.59s` | `-38.3 deg` | `-26.0 deg` | **Bad head alignment**: head pointed downward continuously for `38.6s` |
| `00:45.8 (45.8s) (P7 B3/8)` | `00:48.8 (48.8s)` | `01:32.5 (92.5s)` | `46.76s` | `-32.8 deg` | `-22.8 deg` | **Bad head alignment**: head pointed downward continuously for `46.8s` |
| `01:32.8 (92.8s) (P13 B4/8)` | `01:35.8 (95.8s)` | `01:38.2 (98.2s)` | `5.47s` | `-26.2 deg` | `-19.6 deg` | **Bad head alignment**: head pointed downward continuously for `5.5s` |
| `01:49.2 (109.2s) (P15 B3/8)` | `01:52.2 (112.2s)` | `01:52.9 (112.9s)` | `3.64s` | `-22.6 deg` | `-18.7 deg` | **Bad head alignment**: head pointed downward continuously for `3.6s` |

### 4.2 Brief Head-Down Dips (`< -15.0 deg` for `< 3.0s`)

| Start Time | End Time | Duration | Worst Angle | Note |
| --- | --- | --- | --- | --- |
| `00:00.4 (0.4s)` | `00:02.1 (2.1s)` | `1.69s` | `-29.3 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:47.0 (107.0s)` | `01:47.6 (107.6s)` | `0.60s` | `-27.9 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:47.8 (107.8s)` | `01:48.4 (108.4s)` | `0.57s` | `-21.2 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:52.9 (112.9s)` | `01:54.1 (114.1s)` | `1.17s` | `-23.0 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:54.2 (114.2s)` | `01:54.9 (114.9s)` | `0.73s` | `-20.0 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:55.0 (115.0s)` | `01:55.2 (115.2s)` | `0.20s` | `-18.1 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:55.3 (115.3s)` | `01:55.8 (115.8s)` | `0.47s` | `-21.6 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |
| `01:58.5 (118.5s)` | `01:59.1 (119.1s)` | `0.57s` | `-24.5 deg` | Transient downward head tilt (recovered before `3.0s` threshold) |

### 4.4 Camera Framing & Tracking Abstentions

| Start Time | End Time | Duration | Frames | Primary Reason |
| --- | --- | --- | --- | --- |
| `00:00.1 (0.1s)` | `01:59.9 (119.9s)` | `119.81s` | `3331` | Keep nose and at least one ankle visible in frame |
