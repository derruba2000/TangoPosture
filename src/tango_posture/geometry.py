import math

import numpy as np

from .domain import Landmark


def pixel(p: Landmark, width: int, height: int) -> np.ndarray:
    return np.array([p.x * width, -p.y * height], dtype=float)


def wrapped(angle: float) -> float:
    return (angle + 180) % 360 - 180


def calculate(
    ear: np.ndarray, shoulder: np.ndarray, hip: np.ndarray, facing: str
) -> dict[str, float]:
    length = float(np.linalg.norm(shoulder - hip))
    if not np.isfinite([ear, shoulder, hip]).all() or length <= 0:
        raise ValueError("Nonfinite or degenerate geometry")
    t = (shoulder - hip) / length
    f = 1 if facing == "right" else -1
    n = f * np.array([t[1], -t[0]])
    h = ear - shoulder
    if np.linalg.norm(h) <= 0:
        raise ValueError("Degenerate shoulder-to-ear segment")
    return {
        "head_forward": float(h @ n / length),
        "head_inclination": math.degrees(math.atan2(float(h @ n), float(h @ t))),
        "trunk_inclination": math.degrees(math.atan2(f * t[0], t[1])),
    }


def _usable_point(p: Landmark | None, min_visibility: float = 0.5) -> bool:
    return bool(
        p is not None
        and math.isfinite(p.x)
        and math.isfinite(p.y)
        and (p.visibility is None or p.visibility >= min_visibility)
    )


def head_horizontal_alignment(
    pose: dict[str, Landmark], width: int, height: int
) -> tuple[float, Landmark, Landmark] | None:
    """Return (angle_degrees_from_horizontal, ear_landmark, nose_landmark).

    Negative angles mean the nose is below the horizontal line through the ear
    (head pointing downward).
    """
    nose = pose.get("nose")
    if not _usable_point(nose) or nose is None:
        return None
    ears = [pose[k] for k in ("left_ear", "right_ear") if _usable_point(pose.get(k))]
    if not ears:
        return None
    ear = max(ears, key=lambda e: abs(nose.x - e.x))
    dx = abs(nose.x - ear.x) * width
    dy = (ear.y - nose.y) * height
    if dx < 5.0 and abs(dy) < 5.0:
        return None
    angle = math.degrees(math.atan2(dy, max(dx, 1.0)))
    return (angle, ear, nose)


def detect_role(pose: dict[str, Landmark]) -> str:
    """Detect LEADER or FOLLOWER from arm posture.

    Leader: raises left arm (left wrist above elbow/shoulder) and holds right arm
    horizontal around the follower.
    Follower: raises right arm (right wrist above elbow/shoulder) and holds left arm
    horizontal around the leader's shoulder.
    """
    required = (
        "left_shoulder",
        "left_elbow",
        "left_wrist",
        "right_shoulder",
        "right_elbow",
        "right_wrist",
    )
    if not all(_usable_point(pose.get(k)) for k in required):
        return "NONE"
    ls, le, lw = pose["left_shoulder"], pose["left_elbow"], pose["left_wrist"]
    rs, re, rw = pose["right_shoulder"], pose["right_elbow"], pose["right_wrist"]
    left_up = (le.y - lw.y) > 0.05 and lw.y <= ls.y + 0.08
    right_up = (re.y - rw.y) > 0.05 and rw.y <= rs.y + 0.08
    right_horizontal = abs(rw.y - re.y) <= 0.14 and abs(rw.x - re.x) >= 0.04 and rw.y >= rs.y - 0.03
    left_horizontal = abs(lw.y - le.y) <= 0.14 and abs(lw.x - le.x) >= 0.04 and lw.y >= ls.y - 0.03
    if left_up and right_horizontal and not right_up:
        return "LEADER"
    if right_up and left_horizontal and not left_up:
        return "FOLLOWER"
    return "NONE"
