import math
from dataclasses import replace
from itertools import pairwise

import cv2
import numpy as np
import pytest

from tango_posture.domain import FrameObservation, Landmark
from tango_posture.geometry import torso_axis_alignment
from tango_posture.overlay import draw, screen_point


def torso_pose():
    return {
        "left_shoulder": Landmark(0.45, 0.25, 0.9, 0.9),
        "right_shoulder": Landmark(0.65, 0.25, 0.9, 0.9),
        "left_hip": Landmark(0.35, 0.65, 0.9, 0.9),
        "right_hip": Landmark(0.55, 0.65, 0.9, 0.9),
    }


@pytest.mark.parametrize("width,height", [(641, 481), (1281, 721), (481, 641)])
def test_torso_midpoints_and_pixel_tilt(width, height):
    result = torso_axis_alignment(torso_pose(), width, height)
    assert result is not None
    angle, shoulder, hip = result
    assert (shoulder.x, shoulder.y) == pytest.approx((0.55, 0.25))
    assert (hip.x, hip.y) == pytest.approx((0.45, 0.65))
    # Normalized-coordinate angles would incorrectly be identical for these frames.
    assert angle == pytest.approx(math.degrees(math.atan2(0.1 * (width - 1), 0.4 * (height - 1))))


def test_torso_does_not_depend_on_head_or_feet():
    pose = torso_pose()
    expected = torso_axis_alignment(pose, 640, 480)
    for x in (0.0, 0.5, 1.0):
        for name in ("nose", "left_ear", "right_ear", "left_ankle", "right_ankle"):
            pose[name] = Landmark(x, 0.9, 0.99, 0.99)
        assert torso_axis_alignment(pose, 640, 480) == expected


def test_upright_and_leftward_torso():
    pose = torso_pose()
    for name in ("left_shoulder", "right_shoulder"):
        pose[name] = replace(pose[name], x=pose[name].x - 0.1)
    assert torso_axis_alignment(pose, 640, 480)[0] == pytest.approx(0)
    for name in ("left_shoulder", "right_shoulder"):
        pose[name] = replace(pose[name], x=pose[name].x - 0.1)
    assert torso_axis_alignment(pose, 640, 480)[0] < 0


@pytest.mark.parametrize("name", list(torso_pose()))
def test_each_midpoint_landmark_is_required(name):
    pose = torso_pose()
    pose[name] = replace(pose[name], visibility=0.4)
    assert torso_axis_alignment(pose, 640, 480, confidence=0.5) is None
    assert torso_axis_alignment(pose, 640, 480, confidence=0.3) is not None
    del pose[name]
    assert torso_axis_alignment(pose, 640, 480) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"visibility": None},
        {"visibility": float("nan")},
        {"presence": None},
        {"presence": 0.2},
        {"x": float("nan")},
        {"y": float("inf")},
        {"x": 1.1},
        {"y": -0.1},
    ],
)
def test_unreliable_torso_hides_axes(changes):
    pose = torso_pose()
    pose["left_shoulder"] = replace(pose["left_shoulder"], **changes)
    assert torso_axis_alignment(pose, 640, 480) is None
    image = np.zeros((480, 640, 3), np.uint8)
    observation = FrameObservation(0, 640, 480, [pose])
    visible = draw(image, observation, False, [], "", 0.5)
    hidden = draw(image, observation, False, [], "", 0.5, show_vertical_axis=False)
    np.testing.assert_array_equal(visible, hidden)


def test_degenerate_torso_is_hidden():
    pose = {name: Landmark(0.5, 0.5, 0.9, 0.9) for name in torso_pose()}
    assert torso_axis_alignment(pose, 640, 480) is None
    assert torso_axis_alignment(torso_pose(), 0, 480) is None


def test_upright_vertical_stays_visible_beyond_torso():
    pose = torso_pose()
    for name in ("left_shoulder", "right_shoulder"):
        pose[name] = replace(pose[name], x=pose[name].x - 0.1)
    observation = FrameObservation(0, 640, 480, [pose])
    shown = draw(np.zeros((480, 640, 3), np.uint8), observation, False, [], "", 0.5)
    sx, sy = screen_point(0.45, 0.25, 960, 720, False)
    # Cyan dashes must remain visible beyond the magenta line when both coincide.
    assert np.any(np.all(shown[: sy - 50, sx] == (255, 220, 0), axis=1))
    assert tuple(shown[sy, sx]) == (255, 140, 255)


@pytest.mark.parametrize("mirror", [False, True])
def test_overlay_renders_extended_torso_and_dashed_hip_vertical(monkeypatch, mirror):
    pose = torso_pose()
    observation = FrameObservation(0, 640, 480, [pose])
    image = np.zeros((480, 640, 3), np.uint8)
    line_calls, labels = [], []
    real_line, real_text = cv2.line, cv2.putText

    def line(img, start, end, color, *args, **kwargs):
        line_calls.append((start, end, color))
        return real_line(img, start, end, color, *args, **kwargs)

    def text(img, label, *args, **kwargs):
        labels.append(label)
        return real_text(img, label, *args, **kwargs)

    monkeypatch.setattr(cv2, "line", line)
    monkeypatch.setattr(cv2, "putText", text)
    shown = draw(image, observation, mirror, [], "", 0.5, verbose_skeleton=False)
    assert shown.shape == (940, 960, 3)
    np.testing.assert_array_equal(image, np.zeros_like(image))
    sx, sy = screen_point(0.55, 0.25, 960, 720, mirror)
    hx, hy = screen_point(0.45, 0.65, 960, 720, mirror)
    torso = [(a, b) for a, b, c in line_calls if c == (255, 140, 255)]
    assert len(torso) == 1
    start, end = torso[0]
    # Line follows the torso midpoints and extends above/below them.
    assert start[1] > hy > sy > end[1]
    vx, vy = end[0] - start[0], end[1] - start[1]
    for x, y in ((sx, sy), (hx, hy)):
        distance = abs(vx * (y - start[1]) - vy * (x - start[0])) / math.hypot(vx, vy)
        assert distance < 1.0  # Allow integer rounding of the extended endpoints.
    assert (end[0] < start[0]) == mirror
    vertical = [(a, b) for a, b, c in line_calls if c == (255, 220, 0)]
    assert len(vertical) > 1
    assert all(a[0] == b[0] == hx for a, b in vertical)
    assert all(a[1] < b[1] for a, b in vertical)
    assert all(b[1] < next_a[1] for (_, b), (next_a, _) in pairwise(vertical))
    assert "Torso axis: 18.4 deg" in labels
    assert "Vertical (level camera)" in labels
    assert not any("Axis line:" in label or "AXIS CURVE:" in label for label in labels)
    line_calls.clear()
    labels.clear()
    hidden = draw(
        image,
        observation,
        mirror,
        [],
        "",
        0.5,
        verbose_skeleton=False,
        show_vertical_axis=False,
    )
    assert not line_calls
    assert "Vertical (level camera)" not in labels
    assert not np.array_equal(shown, hidden)
