import math

import numpy as np

from .domain import Landmark
from .quality import reliable


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
    # or when the dancer rotates between side profile and frontal view.
    torso_scales: list[float] = []
    for side in ("left", "right"):
        ear = pose.get(f"{side}_ear")
        hip = pose.get(f"{side}_hip")
        if _usable_point(ear) and _usable_point(hip) and ear is not None and hip is not None:
            eh_dy = (hip.y - ear.y) * height
            if eh_dy > 20.0:
                torso_scales.append(0.26 * eh_dy)

    head_scales: list[float] = []
    nose = pose.get("nose")
    if _usable_point(nose) and nose is not None:
        for _, ear, _ in pairs:
            # Use full 2D Euclidean distance (invariant to head pitch) scaled to match
            # vertical ear-to-hip normalization (~0.68x nose-to-ear distance).
            head_scales.append(
                0.68 * math.hypot((nose.x - ear.x) * width, (nose.y - ear.y) * height)
            )
    l_ear, r_ear = pose.get("left_ear"), pose.get("right_ear")
    if _usable_point(l_ear) and _usable_point(r_ear) and l_ear is not None and r_ear is not None:
        head_scales.append(0.65 * abs(l_ear.x - r_ear.x) * width)
    l_sh, r_sh = pose.get("left_shoulder"), pose.get("right_shoulder")
    if _usable_point(l_sh) and _usable_point(r_sh) and l_sh is not None and r_sh is not None:
        head_scales.append(0.28 * abs(l_sh.x - r_sh.x) * width)

    # Prefer vertical ear-to-hip height when visible (invariant to yaw rotation and shrugging);
    # otherwise fall back to head/shoulder scales aligned to the same ratio.
    ref_scale = max(torso_scales) if torso_scales else max(head_scales, default=0.0)
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
    Also supports side-on views where the far embracing arm is occluded behind the torso.
    """
    ls, le, lw = pose.get("left_shoulder"), pose.get("left_elbow"), pose.get("left_wrist")
    rs, re, rw = pose.get("right_shoulder"), pose.get("right_elbow"), pose.get("right_wrist")
    l_ok = all(_usable_point(p, 0.30) for p in (ls, le, lw))
    r_ok = all(_usable_point(p, 0.30) for p in (rs, re, rw))
    if not l_ok and not r_ok:
        return "NONE"

    if l_ok and r_ok and ls and le and lw and rs and re and rw:
        left_up = (
            ((le.y - lw.y) > 0.015 or lw.y <= ls.y + 0.08)
            and lw.y <= ls.y + 0.16
            and (rw.y - lw.y) >= 0.02
        )
        right_up = (
            ((re.y - rw.y) > 0.015 or rw.y <= rs.y + 0.08)
            and rw.y <= rs.y + 0.16
            and (lw.y - rw.y) >= 0.02
        )
        right_horizontal = (
            abs(rw.y - re.y) <= 0.20
            and abs(rw.x - re.x) >= 0.02
            and rs.y - 0.08 <= rw.y <= rs.y + 0.38
        )
        left_horizontal = (
            abs(lw.y - le.y) <= 0.20
            and abs(lw.x - le.x) >= 0.02
            and ls.y - 0.08 <= lw.y <= ls.y + 0.38
        )
        if left_up and right_horizontal and not right_up:
            return "LEADER"
        if right_up and left_horizontal and not left_up:
            return "FOLLOWER"

    # Side-profile fallback when the far arm is occluded behind the torso
    if ls and rs and abs(ls.x - rs.x) < 0.10:
        if l_ok and ls and le and lw and lw.y <= ls.y + 0.10 and le.y <= ls.y + 0.14 and (le.y - lw.y) >= -0.02:
            return "LEADER"
        if r_ok and rs and re and rw and rw.y <= rs.y + 0.10 and re.y <= rs.y + 0.14 and (re.y - rw.y) >= -0.02:
            return "FOLLOWER"
    return "NONE"


def embrace_openness_score(pose: dict[str, Landmark], role: str) -> float | None:
    """Compute a continuous openness score in [0.0, 1.5] for an active `LEADER` or `FOLLOWER` embrace.

    Combines frontal/diagonal and side-profile cues so transitions between `CLOSED` and `OPEN`
    embrace are distinguished reliably from any camera angle:
    1. In frontal/diagonal views (`view_ratio >= 0.18`): checks whether the embracing forearm
       wraps inward across the chest toward the opposite shoulder (`CLOSED`, score ~0.30-0.55)
       versus extending outward away from the torso (`OPEN`, score ~0.85-1.20), combined with
       wrist-to-opposite-shoulder distance and inter-wrist span.
    2. In side-profile views (`view_ratio < 0.30`, including when the far arm is occluded):
       measures how far the frame wrist and elbow advance forward in front of the shoulder line
       along the dancer's facing direction (`CLOSED` when tucked near/behind the shoulder line,
       `OPEN` when extended forward toward the partner).
    """
    if role not in ("LEADER", "FOLLOWER"):
        return None
    if role == "LEADER":
        up_side, emb_side = "left", "right"
    else:
        up_side, emb_side = "right", "left"

    s_up = pose.get(f"{up_side}_shoulder")
    e_up = pose.get(f"{up_side}_elbow")
    w_up = pose.get(f"{up_side}_wrist")
    s_emb = pose.get(f"{emb_side}_shoulder")
    e_emb = pose.get(f"{emb_side}_elbow")
    w_emb = pose.get(f"{emb_side}_wrist")

    up_ok = all(_usable_point(p, 0.30) for p in (s_up, e_up, w_up))
    emb_ok = all(_usable_point(p, 0.30) for p in (s_emb, e_emb, w_emb))
    if not up_ok and not emb_ok:
        return None

    torso_lengths: list[float] = []
    for side in ("left", "right"):
        sh = pose.get(f"{side}_shoulder")
        hp = pose.get(f"{side}_hip")
        if _usable_point(sh, 0.30) and _usable_point(hp, 0.30) and sh is not None and hp is not None:
            torso_lengths.append(math.hypot(sh.x - hp.x, sh.y - hp.y))
    torso_len = max(max(torso_lengths, default=0.25), 0.14)

    sh_span_x = abs(s_up.x - s_emb.x) if (s_up is not None and s_emb is not None) else 0.0
    view_ratio = sh_span_x / torso_len

    nose = pose.get("nose")
    ears = [pose[k] for k in ("left_ear", "right_ear") if _usable_point(pose.get(k), 0.30)]
    if _usable_point(nose, 0.30) and nose is not None and ears:
        ear_x = sum(e.x for e in ears) / len(ears)
        face_sign = 1.0 if nose.x >= ear_x else -1.0
    elif s_up is not None and w_up is not None:
        face_sign = 1.0 if w_up.x >= s_up.x else -1.0
    else:
        face_sign = 1.0

    # Side-profile forward reach (works even when the far arm is occluded behind the torso)
    side_cues: list[float] = []
    if up_ok and s_up is not None and e_up is not None and w_up is not None:
        up_fwd = (w_up.x - s_up.x) * face_sign / torso_len
        up_elb_fwd = (e_up.x - s_up.x) * face_sign / torso_len
        wrist_reach = max(0.0, (up_fwd + 0.04) / 0.22)
        elb_reach = max(0.0, (up_elb_fwd + 0.14) / 0.18)
        side_cues.append(0.65 * min(1.3, wrist_reach) + 0.35 * min(1.3, elb_reach))
    if emb_ok and s_emb is not None and e_emb is not None and w_emb is not None:
        emb_fwd = (w_emb.x - s_emb.x) * face_sign / torso_len
        emb_reach_fwd = max(0.0, emb_fwd / 0.22)
        side_cues.append(min(1.3, emb_reach_fwd))
    side_score = sum(side_cues) / len(side_cues) if side_cues else 0.5

    if not (
        up_ok
        and emb_ok
        and s_up is not None
        and e_up is not None
        and w_up is not None
        and s_emb is not None
        and e_emb is not None
        and w_emb is not None
    ):
        return side_score

    # Frontal / diagonal score when both arms are visible
    emb_u = math.hypot(e_emb.x - s_emb.x, e_emb.y - s_emb.y)
    emb_v = math.hypot(w_emb.x - e_emb.x, w_emb.y - e_emb.y)
    emb_span = math.hypot(w_emb.x - s_emb.x, w_emb.y - s_emb.y)
    emb_reach = emb_span / max(emb_u + emb_v, 1e-4)

    up_u = math.hypot(e_up.x - s_up.x, e_up.y - s_up.y)
    up_v = math.hypot(w_up.x - e_up.x, w_up.y - e_up.y)
    up_span = math.hypot(w_up.x - s_up.x, w_up.y - s_up.y)
    up_reach = up_span / max(up_u + up_v, 1e-4)

    w_emb_to_s_up = math.hypot(w_emb.x - s_up.x, w_emb.y - s_up.y) / torso_len
    ww_dist = math.hypot(w_up.x - w_emb.x, w_up.y - w_emb.y) / torso_len

    dir_to_up = 1.0 if s_up.x >= s_emb.x else -1.0
    forearm_inward_x = (w_emb.x - e_emb.x) * dir_to_up
    wrist_inward_from_sh = (w_emb.x - s_emb.x) * dir_to_up

    frontal_score = (
        0.35 * emb_reach
        + 0.20 * up_reach
        + 0.25 * min(1.3, w_emb_to_s_up / 0.80)
        + 0.20 * min(1.3, ww_dist / 1.00)
    )
    if view_ratio >= 0.18:
        if wrist_inward_from_sh < -0.04 or forearm_inward_x < -0.03:
            frontal_score = max(frontal_score, 0.85 + min(0.35, abs(wrist_inward_from_sh)))
        elif forearm_inward_x > 0.05 and wrist_inward_from_sh > 0.02:
            frontal_score *= 0.65 if w_emb_to_s_up < 0.70 else 0.78

    if view_ratio < 0.18:
        return side_score
    if view_ratio > 0.30:
        return frontal_score
    w_front = (view_ratio - 0.18) / 0.12
    return w_front * frontal_score + (1.0 - w_front) * side_score


def detect_embrace_type(
    pose: dict[str, Landmark],
    role: str,
    closed_threshold: float = 0.72,
) -> str:
    """Classify an active embrace (`LEADER` or `FOLLOWER`) as `CLOSED` or `OPEN`.

    Measures the normalized reach/extension of the embracing arm and raised frame arm
    relative to total upper-arm + forearm length and torso-normalized horizontal reach,
    plus inward forearm wrap across the torso. Compact/wrapped arms <= `closed_threshold`
    return `CLOSED`; extended arms return `OPEN`. Returns `NONE` when `role` is not
    `LEADER` or `FOLLOWER`.
    """
    if role not in ("LEADER", "FOLLOWER"):
        return "NONE"
    openness = embrace_openness_score(pose, role)
    if openness is None:
        return "CLOSED"
    return "CLOSED" if openness <= closed_threshold else "OPEN"


class EmbraceTracker:
    """Temporal smoothing and hysteresis for `CLOSED` <-> `OPEN` embrace transitions.

    - Smooths the continuous arm openness score (`smoothing_sec`, default 0.15s).
    - Uses symmetric hysteresis around `closed_threshold` (`±0.035`) and a short transition
      confirmation time (`switch_ms`, default 220ms) so deliberately opening or closing the
      embrace transitions smoothly and reliably without flickering.
    """

    def __init__(
        self,
        closed_threshold: float = 0.72,
        switch_ms: float = 220.0,
        smoothing_sec: float = 0.15,
    ):
        self.closed_threshold = float(closed_threshold)
        self.switch_sec = max(0.0, float(switch_ms) / 1000.0)
        self.smoothing_sec = max(1e-3, float(smoothing_sec))
        self.state: str = "NONE"
        self.smoothed: float | None = None
        self.last_ts: float | None = None
        self.candidate: str | None = None
        self.candidate_since: float | None = None

    def reset(self) -> None:
        self.state = "NONE"
        self.smoothed = None
        self.last_ts = None
        self.candidate = None
        self.candidate_since = None

    def update(self, pose: dict[str, Landmark] | None, role: str, timestamp: float) -> str:
        if role not in ("LEADER", "FOLLOWER") or pose is None:
            self.reset()
            return "NONE"
        score = embrace_openness_score(pose, role)
        if score is None:
            return self.state if self.state in ("CLOSED", "OPEN") else "CLOSED"

        if self.smoothed is None or self.last_ts is None or (timestamp - self.last_ts) > 0.5:
            self.smoothed = score
        else:
            dt = max(0.0, timestamp - self.last_ts)
            alpha = 1.0 - math.exp(-dt / self.smoothing_sec)
            self.smoothed += alpha * (score - self.smoothed)
        self.last_ts = timestamp

        if self.state not in ("CLOSED", "OPEN"):
            self.state = "CLOSED" if self.smoothed <= self.closed_threshold else "OPEN"
            self.candidate = None
            self.candidate_since = None
            return self.state

        # Hysteresis band: to switch CLOSED -> OPEN, openness must exceed threshold + 0.025;
        # to switch OPEN -> CLOSED, openness must drop below threshold - 0.025.
        target = self.state
        if self.state == "CLOSED" and self.smoothed > self.closed_threshold + 0.025:
            target = "OPEN"
        elif self.state == "OPEN" and self.smoothed <= self.closed_threshold - 0.025:
            target = "CLOSED"

        if target != self.state:
            if self.candidate != target or self.candidate_since is None:
                self.candidate = target
                self.candidate_since = timestamp
            if (timestamp - self.candidate_since) + 1e-9 >= self.switch_sec:
                self.state = target
                self.candidate = None
                self.candidate_since = None
        else:
            self.candidate = None
            self.candidate_since = None
        return self.state


def knee_bend_alignment(
    pose: dict[str, Landmark],
    width: int,
    height: int,
    side: str = "left",
) -> tuple[float, list[tuple[Landmark, Landmark, Landmark]]] | None:
    """Return (knee_bend_degrees, visible_leg_triplets).

    `knee_bend_degrees` is `180.0 - interior_angle(hip, knee, ankle)`:
    - `0.0 deg` = completely straight / locked knee
    - `>= CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES` = knees flexed / bent

    Uses the minimum bend across visible reliable legs (or the camera-facing side when the
    far leg has low confidence) so a locked/straight standing leg is not masked by noise
    or movement on the other leg.
    """
    side_bends: dict[str, tuple[float, float, tuple[Landmark, Landmark, Landmark]]] = {}
    legs: list[tuple[Landmark, Landmark, Landmark]] = []
    ordered_sides = (side, "right" if side == "left" else "left")
    for s in ordered_sides:
        hip = pose.get(f"{s}_hip")
        knee = pose.get(f"{s}_knee")
        ankle = pose.get(f"{s}_ankle")
        if (
            _usable_point(hip, 0.35)
            and _usable_point(knee, 0.35)
            and _usable_point(ankle, 0.35)
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
                conf = min(
                    hip.visibility if hip.visibility is not None else 1.0,
                    knee.visibility if knee.visibility is not None else 1.0,
                    ankle.visibility if ankle.visibility is not None else 1.0,
                )
                triplet = (hip, knee, ankle)
                side_bends[s] = (bend_deg, conf, triplet)
                legs.append(triplet)
    if not side_bends:
        return None
    if len(side_bends) == 2:
        near_bend, near_conf, _ = side_bends[ordered_sides[0]]
        far_bend, far_conf, _ = side_bends[ordered_sides[1]]
        # If the occluded far leg has much lower landmark visibility, use the camera-facing leg;
        # otherwise use min(near_bend, far_bend) so a straight standing leg is always caught.
        if far_conf < 0.60 and near_conf >= far_conf + 0.15:
            chosen_bend = near_bend
        else:
            chosen_bend = min(near_bend, far_bend)
    else:
        chosen_bend = next(iter(side_bends.values()))[0]
    return (chosen_bend, legs)


def torso_axis_alignment(
    pose: dict[str, Landmark],
    width: int,
    height: int,
    confidence: float = 0.5,
) -> tuple[float, Landmark, Landmark] | None:
    """Return (tilt_degrees, shoulder_midpoint, hip_midpoint) for the visible torso.

    Tilt is measured in pixel space from image-up, positive toward camera-right.
    This is a projected torso orientation, not a centre-of-mass or balance estimate.
    All four landmarks must be reliable; do not substitute a limb or the head when
    a shoulder/hip is occluded during a turn.
    """
    names = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
    if width <= 1 or height <= 1 or not all(reliable(pose.get(k), confidence) for k in names):
        return None
    ls, rs, lh, rh = (pose[k] for k in names)
    shoulder = Landmark((ls.x + rs.x) / 2, (ls.y + rs.y) / 2)
    hip = Landmark((lh.x + rh.x) / 2, (lh.y + rh.y) / 2)
    # Match the pixel mapping used by screen_point, including non-square frames.
    dx = (shoulder.x - hip.x) * (width - 1)
    dy = (hip.y - shoulder.y) * (height - 1)
    if math.hypot(dx, dy) < 1.0:
        return None
    return math.degrees(math.atan2(dx, dy)), shoulder, hip


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


class PoseSmoother:
    """Timestamp-driven exponential smoothing and short-dropout hold for pose landmarks.

    Smooths landmark coordinates (`x`, `y`) and confidence scores (`visibility`, `presence`)
    using time constant `smoothing_sec` (seconds), and briefly holds recently valid landmarks
    across 1-2 frame confidence dips (`<= min(0.15s, max_gap * 0.5)`) so overlay skeleton lines
    and segments move smoothly without jittering or blinking on and off.
    """

    def __init__(self, smoothing_sec: float = 0.20, max_gap: float = 0.5):
        self.smoothing_sec = max(0.0, float(smoothing_sec))
        self.max_gap = max(0.0, float(max_gap))
        self.hold_sec = min(0.15, self.max_gap * 0.5) if self.smoothing_sec > 0 else 0.0
        self.state: dict[str, Landmark] = {}
        self.last_seen: dict[str, float] = {}
        self.last_ts: float | None = None

    def reset(self) -> None:
        self.state.clear()
        self.last_seen.clear()
        self.last_ts = None

    def update(
        self, poses: list[dict[str, Landmark]], timestamp: float
    ) -> list[dict[str, Landmark]]:
        if self.smoothing_sec <= 0.0 or not math.isfinite(timestamp):
            return poses
        if len(poses) != 1:
            if self.last_ts is not None and (timestamp - self.last_ts) > self.max_gap:
                self.reset()
            return poses

        if (
            self.last_ts is not None
            and (timestamp < self.last_ts or (timestamp - self.last_ts) > self.max_gap)
        ):
            self.reset()

        pose = poses[0]
        smoothed_pose: dict[str, Landmark] = {}
        for name, lm in pose.items():
            valid_xy = (
                math.isfinite(lm.x)
                and math.isfinite(lm.y)
                and -0.25 <= lm.x <= 1.25
                and -0.25 <= lm.y <= 1.25
            )
            prev = self.state.get(name)
            prev_ts = self.last_seen.get(name)
            if not valid_xy:
                if (
                    prev is not None
                    and prev_ts is not None
                    and 0.0 <= (timestamp - prev_ts) <= self.hold_sec
                ):
                    smoothed_pose[name] = prev
                else:
                    smoothed_pose[name] = lm
                continue

            if prev is None or prev_ts is None or (timestamp - prev_ts) > self.max_gap:
                new_lm = lm
            else:
                dt = max(0.0, timestamp - prev_ts)
                alpha = 1.0 - math.exp(-dt / max(1e-4, self.smoothing_sec))
                sx = prev.x + alpha * (lm.x - prev.x)
                sy = prev.y + alpha * (lm.y - prev.y)
                # Smooth confidence with slightly slower decay than rise to prevent edge blinking
                if lm.visibility is not None and prev.visibility is not None:
                    alpha_v = (
                        alpha
                        if lm.visibility >= prev.visibility
                        else 1.0 - math.exp(-dt / max(1e-4, self.smoothing_sec * 1.25))
                    )
                    sv: float | None = prev.visibility + alpha_v * (
                        lm.visibility - prev.visibility
                    )
                else:
                    sv = lm.visibility
                if lm.presence is not None and prev.presence is not None:
                    alpha_p = (
                        alpha
                        if lm.presence >= prev.presence
                        else 1.0 - math.exp(-dt / max(1e-4, self.smoothing_sec * 1.25))
                    )
                    sp: float | None = prev.presence + alpha_p * (lm.presence - prev.presence)
                else:
                    sp = lm.presence
                new_lm = Landmark(sx, sy, sv, sp)
            self.state[name] = new_lm
            self.last_seen[name] = timestamp
            smoothed_pose[name] = new_lm

        self.last_ts = timestamp
        return [smoothed_pose]


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
