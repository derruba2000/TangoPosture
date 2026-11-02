from dataclasses import replace

import numpy as np
import pytest
from conftest import frame

from tango_posture.beat import BeatTracker
from tango_posture.config import load
from tango_posture.domain import METRICS, Landmark
from tango_posture.geometry import (
    RoleTracker,
    calculate,
    detect_role,
    head_horizontal_alignment,
    shoulder_tension_alignment,
    wrapped,
)
from tango_posture.metrics import measure
from tango_posture.overlay import screen_point


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("facing", ["left", "right"])
@pytest.mark.parametrize("size", [(640, 480), (1280, 720)])
def test_geometry(cfg, side, facing, size):
    cfg.side, cfg.facing = side, facing
    base = measure(frame(width=size[0], height=size[1], side=side), cfg)
    assert base.values == pytest.approx(dict.fromkeys(METRICS, 0))
    positive = measure(
        frame(forward=0.2, width=size[0], height=size[1], side=side, facing=facing), cfg
    )
    assert positive.values["head_forward"] == pytest.approx(0.2)
    assert positive.values["head_inclination"] > 0
    assert measure(frame(forward=-0.2, side=side, facing=facing), cfg).values["head_forward"] < 0


def test_translation_scale_and_rotation():
    ear, shoulder, hip = np.array([30.0, 80.0]), np.array([0.0, 0.0]), np.array([0.0, -200.0])
    expected = calculate(ear, shoulder, hip, "right")
    for scale in (0.5, 2, 8):
        transformed = [p * scale + np.array([500, -200]) for p in (ear, shoulder, hip)]
        assert calculate(*transformed, "right") == pytest.approx(expected)
    angle = np.deg2rad(-20)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    rotated = calculate(*(rotation @ p for p in (ear, shoulder, hip)), "right")
    assert rotated["head_inclination"] == pytest.approx(expected["head_inclination"])
    assert rotated["head_forward"] == pytest.approx(expected["head_forward"])
    assert rotated["trunk_inclination"] == pytest.approx(20)
    assert wrapped(359) == -1


def test_mirror_only_display(cfg):
    f = frame(forward=0.1)
    before = measure(f, cfg)
    cfg.mirror = True
    assert measure(f, cfg) == before
    assert screen_point(0.25, 0.5, 641, 481, True) == (480, 240)
    assert screen_point(0.25, 0.5, 641, 481, False) == (160, 240)


@pytest.mark.parametrize("fault", ["missing", "low", "presence", "nan", "out", "short"])
def test_head_unknown_independently(cfg, fault):
    f = frame()
    p = f.poses[0]["left_ear"]
    if fault == "missing":
        del f.poses[0]["left_ear"]
    else:
        replacements = {
            "low": replace(p, visibility=0.2),
            "presence": replace(p, presence=None),
            "nan": replace(p, x=float("nan")),
            "out": replace(p, x=1.2),
            "short": f.poses[0]["left_shoulder"],
        }
        f.poses[0]["left_ear"] = replacements[fault]
    result = measure(f, cfg)
    assert result.values["head_forward"] is None
    assert result.values["head_inclination"] is None
    assert result.quality["trunk_inclination"].valid


@pytest.mark.parametrize(
    "fault", ["none", "two", "frontal", "far", "degenerate", "ankles", "nose", "confidence"]
)
def test_overall_unknown(cfg, fault):
    f = frame()
    p = f.poses[0]
    if fault == "none":
        f.poses.clear()
    elif fault == "two":
        f.poses.append(p.copy())
    elif fault == "frontal":
        p["right_shoulder"] = replace(p["right_shoulder"], x=0.9)
    elif fault in ("far", "degenerate"):
        p["left_shoulder"] = replace(
            p["left_hip"], y=p["left_hip"].y - (0.01 if fault == "far" else 0)
        )
    elif fault == "ankles":
        del p["left_ankle"], p["right_ankle"]
    elif fault == "nose":
        del p["nose"]
    else:
        p["right_hip"] = Landmark(0.5, 0.5)
    result = measure(f, cfg)
    assert all(v is None for v in result.values.values())
    assert not any(q.valid for q in result.quality.values())


def test_view_baseline_and_far_ankle(cfg):
    f = frame()
    del f.poses[0]["right_ankle"]
    assert all(q.valid for q in measure(f, cfg).quality.values())
    assert not any(q.valid for q in measure(f, cfg, (0.3, 0.4)).quality.values())


def test_head_horizontal_alignment_and_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HEAD_DOWN_DURATION_SECONDS=4.5\nHEAD_DOWN_ANGLE_DEGREES=-18.0\n")
    loaded = load(None, {}, env_path=env_file)
    assert loaded.head_down_duration == pytest.approx(4.5)
    assert loaded.head_down_angle == pytest.approx(-18.0)

    level_pose = {
        "left_ear": Landmark(0.4, 0.2, 0.99, 0.99),
        "nose": Landmark(0.5, 0.2, 0.99, 0.99),
    }
    res_level = head_horizontal_alignment(level_pose, 640, 480)
    assert res_level is not None and res_level[0] == pytest.approx(0.0)

    down_pose = {
        "left_ear": Landmark(0.4, 0.2, 0.99, 0.99),
        "nose": Landmark(0.5, 0.2 + 64 / 480, 0.99, 0.99),
    }
    res_down = head_horizontal_alignment(down_pose, 640, 480)
    assert res_down is not None and res_down[0] == pytest.approx(-45.0)


def test_leader_and_follower_role_detection():
    leader_pose = {
        "left_shoulder": Landmark(0.80, 0.36, 0.99, 0.99),
        "left_elbow": Landmark(1.00, 0.42, 0.99, 0.99),
        "left_wrist": Landmark(1.01, 0.19, 0.99, 0.99),
        "right_shoulder": Landmark(0.54, 0.37, 0.99, 0.99),
        "right_elbow": Landmark(0.47, 0.64, 0.99, 0.99),
        "right_wrist": Landmark(0.67, 0.59, 0.99, 0.99),
    }
    assert detect_role(leader_pose) == "LEADER"

    follower_pose = {
        "left_shoulder": Landmark(0.84, 0.36, 0.99, 0.99),
        "left_elbow": Landmark(1.00, 0.54, 0.99, 0.99),
        "left_wrist": Landmark(0.81, 0.51, 0.99, 0.99),
        "right_shoulder": Landmark(0.56, 0.36, 0.99, 0.99),
        "right_elbow": Landmark(0.38, 0.52, 0.99, 0.99),
        "right_wrist": Landmark(0.40, 0.26, 0.99, 0.99),
    }
    assert detect_role(follower_pose) == "FOLLOWER"

    neutral_pose = {
        "left_shoulder": Landmark(0.6, 0.35, 0.99, 0.99),
        "left_elbow": Landmark(0.6, 0.55, 0.99, 0.99),
        "left_wrist": Landmark(0.6, 0.75, 0.99, 0.99),
        "right_shoulder": Landmark(0.4, 0.35, 0.99, 0.99),
        "right_elbow": Landmark(0.4, 0.55, 0.99, 0.99),
        "right_wrist": Landmark(0.4, 0.75, 0.99, 0.99),
    }
    assert detect_role(neutral_pose) == "NONE"

    # Hysteresis: 300ms to assign from NONE, 5000ms hold before returning to NONE
    rt = RoleTracker(assign_ms=300.0, hold_ms=5000.0)
    assert rt.update("LEADER", 1.00) == "NONE"
    assert rt.update("LEADER", 1.20) == "NONE"  # Only 200ms elapsed (< 300ms)
    assert rt.update("LEADER", 1.30) == "LEADER"  # 300ms reached -> assigns LEADER
    assert rt.update("NONE", 3.00) == "LEADER"  # Held at +1.7s (< 5000ms)
    assert rt.update("NONE", 6.29) == "LEADER"  # Held at +4.99s (< 5000ms)
    assert rt.update("NONE", 6.31) == "NONE"  # 5000ms expired -> returns to NONE


def test_shoulder_tension_detection():
    # Relaxed shoulders: ear at y=0.20, shoulder at y=0.33, hip at y=0.80 -> clearance ~ 1.0
    relaxed_pose = {
        "nose": Landmark(0.58, 0.22, 0.99, 0.99),
        "left_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "left_shoulder": Landmark(0.50, 0.32, 0.99, 0.99),
        "left_hip": Landmark(0.50, 0.80, 0.99, 0.99),
    }
    relaxed_res = shoulder_tension_alignment(relaxed_pose, 640, 480, neutral_clearance=0.95)
    assert relaxed_res is not None
    assert relaxed_res[0] < 10.0  # Below 10% tension threshold

    # Shrugged / tense shoulders: shoulder rises up to y=0.28 toward the ear (> 10% elevation)
    tense_pose = {
        "nose": Landmark(0.58, 0.22, 0.99, 0.99),
        "left_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "left_shoulder": Landmark(0.50, 0.28, 0.99, 0.99),
        "left_hip": Landmark(0.50, 0.80, 0.99, 0.99),
    }
    tense_res = shoulder_tension_alignment(tense_pose, 640, 480, neutral_clearance=0.95)
    assert tense_res is not None
    assert tense_res[0] >= 10.0  # Above 10% is no longer OK -> flags LOWER YOUR SHOULDERS


def test_tango_8_beat_phrase_tracker():
    tracker = BeatTracker(bpm=120.0, enable_audio=False)
    # Initialize at t=0.0; at 120 BPM, each beat is 0.5 seconds
    tracker.tap(0.0)
    expected_beats = [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3]
    for i, expected in enumerate(expected_beats):
        st = tracker.state_at(i * 0.5 + 0.05)
        assert st["beat"] == expected
        assert st["phrase"] == (1 if i < 8 else 2)

    # Tapping Beat 1 / tempo at 10.0s and 10.6s (100 BPM) resets beat to 1 and updates BPM
    tracker.tap(10.0)
    assert tracker.state_at(10.05)["beat"] == 1
    tracker.tap(10.6)
    st_after = tracker.state_at(10.61)
    assert st_after["beat"] == 1
    assert 95.0 <= st_after["bpm"] <= 120.0

    # Audio spectral flux onset shifts phase smoothly and marks source as mic-pll
    for k in range(25):
        tracker.process_flux(1.0 if k % 5 == 0 else 0.05, 11.0 + k * 0.1)
    st_audio = tracker.state_at(13.5)
    assert st_audio["source"] == "mic-pll"

    # In microphone mode (require_audio=True), beats MUST NOT be counted before a song begins
    # or when music stops
    mic_tracker = BeatTracker(
        bpm=120.0, enable_audio=False, require_audio=True, analysis_seconds=0.0
    )
    st_waiting_0 = mic_tracker.state_at(0.0)
    st_waiting_2 = mic_tracker.state_at(2.0)
    assert st_waiting_0["playing"] is False
    assert st_waiting_2["playing"] is False
    assert st_waiting_0["beat"] == 0
    assert st_waiting_0["phrase"] == 0
    assert st_waiting_2["beat"] == 0  # Still 0 (not counting) after 2s of silence
    assert st_waiting_2["source"] == "waiting-for-song"

    # Feed active music with 120 BPM onsets (every 0.5s) from t=2.0s to t=4.5s
    t = 2.0
    while t <= 4.5:
        is_beat = abs((t - 2.0) % 0.5) < 1e-6 or abs(((t - 2.0) % 0.5) - 0.5) < 1e-6
        mic_tracker.process_flux(2.0 if is_beat else 0.1, round(t, 2), rms=0.05)
        mic_tracker.state_at(round(t, 2))
        t += 0.05
    playing_state = mic_tracker.state_at(4.5)
    assert playing_state["playing"] is True
    assert playing_state["beat"] >= 1
    assert playing_state["phrase"] >= 1

    # Music is paused at t=4.55s (RMS drops to silence) -> must stop counting beats/phrases
    while t <= 8.0:
        mic_tracker.process_flux(0.0, round(t, 2), rms=0.0)
        t += 0.05
    paused_at_5s = mic_tracker.state_at(5.0)
    paused_at_8s = mic_tracker.state_at(8.0)
    assert paused_at_5s["playing"] is False
    assert paused_at_8s["playing"] is False
    assert paused_at_5s["beat"] == 0
    assert paused_at_8s["beat"] == 0
    assert paused_at_8s["phrase"] == 0
    assert paused_at_8s["source"] == "mic-paused"


def test_tango_30s_phrase_start_analysis_and_audio_recording(tmp_path):
    sr = 16000
    beat_sec = 0.5  # 120 BPM (8-beat phrase = 4.0s)
    total_beats = 64  # 32.0 seconds (8 full 8-beat phrases)
    # Start the recording mid-phrase so the true Beat 1 falls at beat index k = 3, 11, 19, ...
    true_phrase_offset = 3
    total_samples = int(total_beats * beat_sec * sr)
    audio = np.zeros(total_samples, dtype=np.float32)
    t_arr = np.arange(int(beat_sec * sr), dtype=np.float32) / sr

    for k in range(total_beats):
        phrase_beat = ((k - true_phrase_offset) % 8) + 1  # 1..8
        phrase_num = (k - true_phrase_offset) // 8
        start = int(k * beat_sec * sr)
        end = start + len(t_arr)
        # Decay envelope for marcato attack
        env = np.exp(-t_arr / (0.28 if phrase_beat == 1 else 0.14 if phrase_beat == 5 else 0.09))
        if phrase_beat == 8:
            # Phrase cadence / breath before Beat 1
            env *= 0.35
        # Contrabajo + piano bass marcato (90 Hz + 180 Hz)
        amp = 0.45 if phrase_beat == 1 else 0.28 if phrase_beat in (3, 5, 7) else 0.14
        sig = amp * env * (np.sin(2 * np.pi * 95.0 * t_arr) + 0.6 * np.sin(2 * np.pi * 190.0 * t_arr))
        # Orchestration / harmonic shift at the start of every 8-beat phrase (Beat 1)
        chord_freq = 520.0 if (phrase_num % 2 == 0) else 1180.0
        chord_amp = 0.35 if phrase_beat == 1 else 0.18 if phrase_beat <= 4 else 0.10
        sig += chord_amp * env * np.sin(2 * np.pi * chord_freq * t_arr)
        audio[start:end] = sig.astype(np.float32)

    wav_out = tmp_path / "videos" / "cara_sucia_test.wav"
    tracker = BeatTracker(
        bpm=120.0,
        enable_audio=False,
        require_audio=True,
        analysis_seconds=30.0,
        audio_save_path=wav_out,
    )
    block = 1024
    # First feed 4 seconds of ambient room noise (RMS ~0.003) before any song begins
    rng = np.random.default_rng(42)
    ambient = rng.normal(0.0, 0.003, size=4 * sr).astype(np.float32)
    for idx in range(0, len(ambient), block):
        chunk = ambient[idx : idx + block]
        ts = (idx + len(chunk)) / sr
        tracker.feed_samples(chunk, ts)

    st_before_song = tracker.state_at(4.0)
    assert tracker.song_detected is False
    assert st_before_song["playing"] is False
    assert st_before_song["beat"] == 0
    assert st_before_song["phrase"] == 0
    assert st_before_song["analyzed_seconds"] == 0.0
    assert st_before_song["source"] == "waiting-for-song"

    # Now start the song at t = 4.0s and track all beats observed across the phrases
    observed_beats: set[int] = set()
    for idx in range(0, len(audio), block):
        chunk = audio[idx : idx + block]
        ts = 4.0 + (idx + len(chunk)) / sr
        tracker.feed_samples(chunk, ts)
        st_cur = tracker.state_at(ts)
        if st_cur["playing"] and st_cur["beat"] > 0:
            observed_beats.add(int(st_cur["beat"]))

    # Must progress through all 8 beats (1, 2, 3, 4, 5, 6, 7, 8) of the phrase
    assert observed_beats == {1, 2, 3, 4, 5, 6, 7, 8}
    assert tracker.song_detected is True
    assert tracker.phrase_locked is True
    anchor_in_beats = round(tracker.phrase_anchor_frame / tracker.tau_frames) % 8
    assert anchor_in_beats == true_phrase_offset
    assert 118.0 <= tracker.bpm <= 122.0

    # At beat k = 64 (after 4.0s silence + 32.0s song = 36.0s):
    # After 64 beats starting at offset 3, 64 - 3 = 61 beats after first Beat 1 -> 61 % 8 = 5 (Beat 6)
    st_end = tracker.state_at(36.01)
    assert st_end["beat"] in (5, 6)
    assert st_end["phrase_locked"] is True

    saved = tracker.save_audio(wav_out)
    assert wav_out.is_file()
    assert len(saved) >= 1
