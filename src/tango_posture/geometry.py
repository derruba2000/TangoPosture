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
    pose: dict[str, Landmark],
    width: int,
    height: int,
    neutral_offset: float = 0.0,
) -> tuple[float, Landmark, Landmark] | None:
    """Return (angle_degrees_from_horizontal, ear_landmark, nose_landmark).

    `neutral_offset` (e.g. -12.0 deg) is subtracted from the raw ear-to-nose angle so
    that a straight, level head reads 0.0 deg despite camera angle and ear/nose anatomy.
    Negative angles mean the nose is below the neutral horizontal line (head pointing down).
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
    if len(ears) == 2:
        l_ear, r_ear = pose["left_ear"], pose["right_ear"]
        inter_ear_dx = abs(l_ear.x - r_ear.x) * width
        # When facing the camera or pivoting through front view, use inter-ear head scale
        # so head pitch stays stable across 360-degree rotations.
        if inter_ear_dx > dx * 1.15:
            dx = max(dx, 0.75 * inter_ear_dx)
            mid_ear_y = 0.5 * (l_ear.y + r_ear.y)
            dy = (mid_ear_y - nose.y) * height
    if dx < 5.0 and abs(dy) < 5.0:
        return None
    raw_angle = math.degrees(math.atan2(dy, max(dx, 1.0)))
    angle = raw_angle - neutral_offset
    return (angle, ear, nose)


def shoulder_tension_alignment(
    pose: dict[str, Landmark],
    width: int,
    height: int,
    neutral_clearance: float = 1.71,
) -> tuple[float, float, Landmark, Landmark] | None:
    """Return (elevation_pct, raw_clearance_ratio, ear_landmark, shoulder_landmark).

    Measures vertical neck clearance (`shoulder.y - ear.y`) normalized by a rigid,
    shrug-invariant anatomical reference scale (~12.5 cm equivalent across skull/hip/shoulder
    width). When shoulders go up in tension, `raw_clearance_ratio` shrinks and `elevation_pct`
    increases above 0%.
    """
    pairs: list[tuple[float, Landmark, Landmark]] = []
    for side in ("left", "right"):
        ear = pose.get(f"{side}_ear")
        sh = pose.get(f"{side}_shoulder")
        if _usable_point(ear) and _usable_point(sh) and ear is not None and sh is not None:
            dy = (sh.y - ear.y) * height
            pairs.append((dy, ear, sh))
    if not pairs:
        return None

    # Build a camera-distance-invariant reference scale that does NOT change when shoulders shrug
    scales: list[float] = []
    nose = pose.get("nose")
    if _usable_point(nose) and nose is not None:
        for _, ear, _ in pairs:
            scales.append(math.hypot((nose.x - ear.x) * width, (nose.y - ear.y) * height))
    l_ear, r_ear = pose.get("left_ear"), pose.get("right_ear")
    if _usable_point(l_ear) and _usable_point(r_ear) and l_ear is not None and r_ear is not None:
        scales.append(0.85 * abs(l_ear.x - r_ear.x) * width)
    l_sh, r_sh = pose.get("left_shoulder"), pose.get("right_shoulder")
    if _usable_point(l_sh) and _usable_point(r_sh) and l_sh is not None and r_sh is not None:
        scales.append(0.34 * abs(l_sh.x - r_sh.x) * width)
    for side in ("left", "right"):
        ear = pose.get(f"{side}_ear")
        hip = pose.get(f"{side}_hip")
        if _usable_point(ear) and _usable_point(hip) and ear is not None and hip is not None:
            eh_dy = (hip.y - ear.y) * height
            if eh_dy > 20.0:
                scales.append(0.20 * eh_dy)

    ref_scale = max(scales, default=0.0)
    if ref_scale < 8.0:
        return None

    # Select the most elevated (smallest neck clearance) visible shoulder
    min_dy, best_ear, best_sh = min(pairs, key=lambda item: item[0])
    raw_clearance = min_dy / ref_scale
    base = max(0.20, float(neutral_clearance))
    elevation_pct = ((base - raw_clearance) / base) * 100.0
    return (elevation_pct, raw_clearance, best_ear, best_sh)


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


def detect_embrace_type(
    pose: dict[str, Landmark],
    role: str,
    closed_threshold: float = 0.72,
) -> str:
    """Classify an active embrace (`LEADER` or `FOLLOWER`) as `CLOSED` or `OPEN`.

    Measures the normalized reach/extension of the embracing arm and raised frame arm
    relative to total upper-arm + forearm length, plus inward forearm wrap across the
    torso. Compact/wrapped arms <= `closed_threshold` return `CLOSED`; extended arms
    return `OPEN`. Returns `NONE` when `role` is not `LEADER` or `FOLLOWER`.
    """
    if role not in ("LEADER", "FOLLOWER"):
        return "NONE"
    required = (
        "left_shoulder",
        "left_elbow",
        "left_wrist",
        "right_shoulder",
        "right_elbow",
        "right_wrist",
    )
    if not all(_usable_point(pose.get(k)) for k in required):
        return "CLOSED"
    ls, le, lw = pose["left_shoulder"], pose["left_elbow"], pose["left_wrist"]
    rs, re, rw = pose["right_shoulder"], pose["right_elbow"], pose["right_wrist"]
    if role == "LEADER":
        s_up, e_up, w_up = ls, le, lw
        s_emb, e_emb, w_emb = rs, re, rw
        s_opp = ls
    else:
        s_up, e_up, w_up = rs, re, rw
        s_emb, e_emb, w_emb = ls, le, lw
        s_opp = rs

    emb_chain = math.hypot(e_emb.x - s_emb.x, e_emb.y - s_emb.y) + math.hypot(
        w_emb.x - e_emb.x, w_emb.y - e_emb.y
    )
    emb_span = math.hypot(w_emb.x - s_emb.x, w_emb.y - s_emb.y)
    emb_reach = emb_span / max(emb_chain, 1e-4)

    # If the embracing forearm folds back inward toward the opposite shoulder, it is a close wrap
    if abs(s_emb.x - s_opp.x) > 0.05 and abs(w_emb.x - s_opp.x) < abs(e_emb.x - s_opp.x):
        emb_reach *= 0.80

    up_chain = math.hypot(e_up.x - s_up.x, e_up.y - s_up.y) + math.hypot(
        w_up.x - e_up.x, w_up.y - e_up.y
    )
    up_span = math.hypot(w_up.x - s_up.x, w_up.y - s_up.y)
    up_reach = up_span / max(up_chain, 1e-4)

    openness = 0.65 * emb_reach + 0.35 * up_reach
    return "CLOSED" if openness <= closed_threshold else "OPEN"


def knee_bend_alignment(
    pose: dict[str, Landmark],
    width: int,
    height: int,
) -> tuple[float, list[tuple[Landmark, Landmark, Landmark]]] | None:
    """Return (knee_bend_degrees, visible_leg_triplets).

    `knee_bend_degrees` is `180.0 - interior_angle(hip, knee, ankle)`:
    - `0.0 deg` = completely straight / locked knee
    - `>= CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES` = knees flexed / bent
    """
    bends: list[float] = []
    legs: list[tuple[Landmark, Landmark, Landmark]] = []
    for side in ("left", "right"):
        hip = pose.get(f"{side}_hip")
        knee = pose.get(f"{side}_knee")
        ankle = pose.get(f"{side}_ankle")
        if (
            _usable_point(hip, 0.45)
            and _usable_point(knee, 0.45)
            and _usable_point(ankle, 0.45)
            and hip is not None
            and knee is not None
            and ankle is not None
        ):
            ux = (hip.x - knee.x) * width
            uy = (hip.y - knee.y) * height
            vx = (ankle.x - knee.x) * width
            vy = (ankle.y - knee.y) * height
            u_norm = math.hypot(ux, uy)
            v_norm = math.hypot(vx, vy)
            if u_norm >= 10.0 and v_norm >= 10.0:
                cos_ang = max(-1.0, min(1.0, (ux * vx + uy * vy) / (u_norm * v_norm)))
                interior_deg = math.degrees(math.acos(cos_ang))
                bend_deg = max(0.0, 180.0 - interior_deg)
                bends.append(bend_deg)
                legs.append((hip, knee, ankle))
    if not bends:
        return None
    return (max(bends), legs)


def axis_curve_alignment(
    pose: dict[str, Landmark],
    width: int,
    height: int,
    facing: str = "right",
    side: str = "left",
    neutral_offset: float = 0.0,
) -> tuple[float, float, Landmark, Landmark, Landmark, Landmark] | None:
    """Return (deviation_ratio, raw_offset_ratio, head_back_landmark, hip_landmark, ankle_landmark, proj_landmark).

    Measures how far the hip / pelvis ('bump') passes behind the straight axis line connecting
    the ankle to the back of the head (estimated from the ear and nose vector, or ear directly),
    normalized by the ankle-to-head axis length.
    - Positive `raw_offset_ratio` means the hip protrudes toward the dancer's posterior ('back')
      side of the ankle-to-back-of-head line (anterior pelvic tilt / curving too much).
    - `deviation_ratio` is `raw_offset_ratio - neutral_offset`.
    """
    sides = (side, "right" if side == "left" else "left")
    chosen_side = None
    for s in sides:
        if (
            _usable_point(pose.get(f"{s}_ear"), 0.45)
            and _usable_point(pose.get(f"{s}_hip"), 0.45)
            and _usable_point(pose.get(f"{s}_ankle"), 0.45)
        ):
            chosen_side = s
            break
    if chosen_side is None:
        return None

    ear = pose[f"{chosen_side}_ear"]
    hip = pose[f"{chosen_side}_hip"]
    ankle = pose[f"{chosen_side}_ankle"]
    nose = pose.get("nose")

    # Estimate the back of the head in normalized image coordinates by extending from nose through ear
    f_sign = 1.0 if facing == "right" else -1.0
    if _usable_point(nose, 0.45) and nose is not None:
        head_span_x = abs(nose.x - ear.x)
        back_dx = max(0.35 * head_span_x, 0.015)
        back_x = max(0.0, min(1.0, ear.x - f_sign * back_dx))
        back_y = ear.y
    else:
        back_x = ear.x
        back_y = ear.y
    head_back = Landmark(back_x, back_y, ear.visibility, ear.presence)

    # Compute vectors in pixel space (x right, y up)
    a_pt = pixel(ankle, width, height)
    h_pt = pixel(hip, width, height)
    b_pt = pixel(head_back, width, height)

    axis_vec = b_pt - a_pt
    axis_len = float(np.linalg.norm(axis_vec))
    if axis_len < 0.20 * height or not math.isfinite(axis_len):
        return None

    u = axis_vec / axis_len
    # Unit normal pointing toward the dancer's posterior (back / 'bump') side of the ankle->head axis:
    # When facing right (+1), up-axis u=(0,1) has posterior normal (-1,0) = (-f_sign * u[1], f_sign * u[0])
    n_posterior = np.array([-f_sign * u[1], f_sign * u[0]], dtype=float)

    rel_hip = h_pt - a_pt
    signed_posterior_px = float(np.dot(rel_hip, n_posterior))
    raw_offset_ratio = signed_posterior_px / axis_len
    deviation_ratio = raw_offset_ratio - float(neutral_offset)

    # Orthogonal projection of the hip onto the ankle -> head_back line for visualization
    along_px = float(np.dot(rel_hip, u))
    proj_pt = a_pt + along_px * u
    proj_lm = Landmark(
        float(proj_pt[0] / width),
        float(-proj_pt[1] / height),
        hip.visibility,
        hip.presence,
    )
    return (deviation_ratio, raw_offset_ratio, head_back, hip, ankle, proj_lm)


class AxisCurveTracker:
    """Timestamp-driven exponential smoothing, entry/clear thresholds, and persistence hysteresis
    for the ankle–hip–back-of-head axis curvature metric.

    - Smoothing: time-constant exponential filter (`smoothing` seconds).
    - Entry: smoothed deviation must exceed `entry` continuously for `entry_duration` seconds
      to transition `WITHIN_LIMIT -> PENDING -> DEVIATION`.
    - Hysteresis Clear: once in `DEVIATION`, smoothed deviation must drop below `clear`
      (`clear < entry`) continuously for `clear_duration` seconds before returning to `WITHIN_LIMIT`.
    """

    def __init__(
        self,
        entry: float = 0.06,
        clear: float = 0.035,
        entry_duration: float = 1.5,
        clear_duration: float = 0.75,
        smoothing: float = 0.25,
        max_gap: float = 0.5,
    ):
        if clear >= entry:
            raise ValueError("clear threshold must be below entry threshold")
        self.entry = float(entry)
        self.clear = float(clear)
        self.entry_duration = max(0.0, float(entry_duration))
        self.clear_duration = max(0.0, float(clear_duration))
        self.smoothing = max(1e-4, float(smoothing))
        self.max_gap = max(0.0, float(max_gap))
        self.smoothed: float | None = None
        self.last_valid: float | None = None
        self.pending_since: float | None = None
        self.clearing_since: float | None = None
        self.in_deviation: bool = False

    def reset(self) -> None:
        self.smoothed = None
        self.last_valid = None
        self.pending_since = None
        self.clearing_since = None
        self.in_deviation = False

    def update(self, raw_deviation: float | None, timestamp: float) -> dict[str, float | str | bool | None]:
        if self.last_valid is not None and (timestamp - self.last_valid) > self.max_gap:
            self.reset()
        if raw_deviation is None or not math.isfinite(raw_deviation):
            self.pending_since = None
            self.clearing_since = None
            self.in_deviation = False
            return {
                "raw": None,
                "smoothed": None,
                "state": "UNKNOWN",
                "elapsed": 0.0,
                "alert": False,
            }

        if self.smoothed is None or self.last_valid is None:
            self.smoothed = float(raw_deviation)
        else:
            dt = max(0.0, timestamp - self.last_valid)
            alpha = 1.0 - math.exp(-dt / self.smoothing)
            self.smoothed += alpha * (float(raw_deviation) - self.smoothed)
        self.last_valid = timestamp

        val = self.smoothed
        if self.in_deviation:
            if val < self.clear:
                if self.clearing_since is None:
                    self.clearing_since = timestamp
                if (timestamp - self.clearing_since) + 1e-9 >= self.clear_duration:
                    self.in_deviation = False
                    self.clearing_since = None
                    self.pending_since = None
            else:
                self.clearing_since = None
        else:
            if val > self.entry:
                if self.pending_since is None:
                    self.pending_since = timestamp
                if (timestamp - self.pending_since) + 1e-9 >= self.entry_duration:
                    self.in_deviation = True
                    self.clearing_since = None
            else:
                self.pending_since = None

        if self.in_deviation:
            state = "DEVIATION"
            elapsed = (
                (timestamp - self.pending_since) if self.pending_since is not None else self.entry_duration
            )
        elif self.pending_since is not None:
            state = "PENDING"
            elapsed = timestamp - self.pending_since
        else:
            state = "WITHIN_LIMIT"
            elapsed = 0.0

        return {
            "raw": float(raw_deviation),
            "smoothed": float(val),
            "state": state,
            "elapsed": float(elapsed),
            "alert": bool(self.in_deviation),
        }


class RoleTracker:
    """Temporal hysteresis for LEADER / FOLLOWER embrace role recognition.

    - When in NONE: requires continuous detection of a role for `assign_ms` (default 300ms)
      before transitioning from NONE to LEADER or FOLLOWER.
    - Once in LEADER or FOLLOWER: holds that role tag for `hold_ms` (default 5000ms)
      after the last frame that detected it before returning to NONE.
    """

    def __init__(self, assign_ms: float = 300.0, hold_ms: float = 5000.0):
        self.assign_sec = max(0.0, float(assign_ms) / 1000.0)
        self.hold_sec = max(0.0, float(hold_ms) / 1000.0)
        self.role: str = "NONE"
        self.last_confirmed_at: float | None = None
        self.candidate: str | None = None
        self.candidate_since: float | None = None

    def update(self, raw_role: str, timestamp: float) -> str:
        if self.role in ("LEADER", "FOLLOWER"):
            if raw_role == self.role:
                self.last_confirmed_at = timestamp
                self.candidate = None
                self.candidate_since = None
                return self.role
            if (
                self.last_confirmed_at is not None
                and (timestamp - self.last_confirmed_at) + 1e-9 < self.hold_sec
            ):
                return self.role
            self.role = "NONE"
            self.last_confirmed_at = None
            self.candidate = None
            self.candidate_since = None

        if raw_role in ("LEADER", "FOLLOWER"):
            if self.candidate != raw_role or self.candidate_since is None:
                self.candidate = raw_role
                self.candidate_since = timestamp
            if (timestamp - self.candidate_since) + 1e-9 >= self.assign_sec:
                self.role = raw_role
                self.last_confirmed_at = timestamp
                self.candidate = None
                self.candidate_since = None
        else:
            self.candidate = None
            self.candidate_since = None
        return self.role
