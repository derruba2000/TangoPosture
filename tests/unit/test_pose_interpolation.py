from copy import deepcopy

import numpy as np
import pytest

from tango_posture.config import Config
from tango_posture.domain import FrameObservation, Landmark
from tango_posture.pose.interpolation import interpolate_poses
from tango_posture.references.coach import interpolate_coach_data, render_coach_frame_overlay


def observation(timestamp, x=None, confidence=1):
    pose = {"left_shoulder": Landmark(x=0.4, y=0.25, visibility=1, presence=1)}
    if x is not None:
        pose["left_hip"] = Landmark(x=x, y=0.5, visibility=confidence, presence=confidence)
    return FrameObservation(timestamp, 100, 100, [pose])


@pytest.mark.parametrize(("method", "expected"), [("linear", 0.6), ("quadratic", 0.5)])
def test_missing_joint_follows_selected_curve_without_changing_valid_points(method, expected):
    frames = [
        observation(1000, 0.1), observation(1001, 0.2),
        observation(1002, 0.9, confidence=0.2), observation(1003, 1),
    ]
    before = deepcopy(frames)
    result = interpolate_poses(frames, method=method, max_gap=3, confidence=0.7)
    assert result[2].poses[0]["left_hip"].x == pytest.approx(expected)
    assert result[2].timestamp == 1002
    assert result[0] is frames[0] and result[1] is frames[1] and result[3] is frames[3]
    assert result[2].poses[0]["left_shoulder"] == frames[2].poses[0]["left_shoulder"]
    assert frames == before


@pytest.mark.parametrize(("max_gap", "filled"), [(3, True), (2.99, False), (0, False)])
def test_maximum_gap_and_disabled_interpolation(max_gap, filled):
    frames = [observation(0, 0.2), observation(1), observation(2), observation(3, 0.8)]
    result = interpolate_poses(frames, method="linear", max_gap=max_gap, confidence=0.7)
    assert ("left_hip" in result[1].poses[0]) is filled
    assert ("left_hip" in result[2].poses[0]) is filled
    if filled:
        assert result[1].poses[0]["left_hip"].x == pytest.approx(0.4)
        assert result[2].poses[0]["left_hip"].x == pytest.approx(0.6)


def test_does_not_extrapolate_or_bridge_ambiguous_dancers():
    frames = [
        observation(0), observation(1, 0.1), observation(2), observation(3, 0.3),
        observation(4), observation(5, 0.5), observation(6),
    ]
    frames[3].poses.append(dict(frames[3].poses[0]))
    result = interpolate_poses(frames, method="linear", max_gap=10, confidence=0.7)
    assert all("left_hip" not in result[index].poses[0] for index in (0, 2, 4, 6))


@pytest.mark.parametrize("extra_anchor", [None, 0.9])
def test_quadratic_uses_linear_when_unsupported_or_outside_frame(extra_anchor):
    frames = [observation(0, extra_anchor), observation(1, 0), observation(2), observation(3, 0)]
    result = interpolate_poses(frames, method="quadratic", max_gap=3, confidence=0.7)
    assert result[2].poses[0]["left_hip"].x == pytest.approx(0)


def test_rendered_connections_bridge_missing_joints_without_replacing_raw_evidence():
    cfg = Config()
    data = {"width": 100, "height": 100, "frames": {
        str(index): {
            "timestamp": index,
            "measurement_poses": [{
                "left_shoulder": [0.4, 0.25, 0, 1, 1],
                **({"left_hip": [x, 0.5, 0, 1, 1]} if x is not None else {}),
            }],
            "message": "", "calibration_rows": [{"metric": "hip_offset", "state": "UNKNOWN"}],
        }
        for index, x in enumerate((0.2, None, 0.6))
    }}
    original = deepcopy(data)
    interpolated = interpolate_coach_data(data, cfg)
    image = np.zeros((100, 100, 3), np.uint8)
    raw_image = render_coach_frame_overlay(image, data, 1, cfg, lines_only=True)
    drawn = render_coach_frame_overlay(image, interpolated, 1, cfg, lines_only=True)
    assert raw_image[40, 40].sum() == 0
    assert drawn[40, 40, 1] > drawn[40, 40, 2]
    assert data == original
    assert interpolated["frames"]["1"]["calibration_rows"] == data["frames"]["1"]["calibration_rows"]
    assert "left_hip" not in interpolated["frames"]["1"]["measurement_poses"][0]
