import math
from typing import Any

import cv2

from .domain import FrameObservation, Landmark
from .geometry import (
    detect_embrace_type,
    detect_role,
    head_horizontal_alignment,
    knee_bend_alignment,
    shoulder_tension_alignment,
    torso_axis_alignment,
)
from .pose.constants import CONNECTIONS
from .quality import reliable

MESSAGES = {
    "head_forward": "Head farther forward than reference",
    "head_inclination": "Head angle differs from reference",
    "trunk_inclination": "Trunk angle differs from reference",
}


def screen_point(x: float, y: float, width: int, height: int, mirror: bool) -> tuple[int, int]:
    return (
        width - 1 - round(x * (width - 1)) if mirror else round(x * (width - 1)),
        round(y * (height - 1)),
    )


def _draw_torso_axis(
    image: Any, pose: dict[str, Landmark], mirror: bool, confidence: float
) -> None:
    height, width = image.shape[:2]
    axis = torso_axis_alignment(pose, width, height, confidence)
    if axis is None:
        return
    angle, shoulder, hip = axis
    sx, sy = screen_point(shoulder.x, shoulder.y, width, height, mirror)
    hx, hy = screen_point(hip.x, hip.y, width, height, mirror)
    dx, dy = sx - hx, sy - hy
    length = math.hypot(dx, dy)
    torso_color, vertical_color = (255, 140, 255), (255, 220, 0)

    # Extend the fixed vertical farther so it stays visible behind an upright torso.
    top = max(0, round(hy - 1.35 * length))
    bottom = min(height - 1, round(hy + 0.35 * length))
    for y in range(top, bottom + 1, 20):
        cv2.line(image, (hx, y), (hx, min(y + 11, bottom)), vertical_color, 2, cv2.LINE_AA)
    # Extend the torso direction by 15% beyond each midpoint.
    cv2.line(
        image,
        (round(hx - 0.15 * dx), round(hy - 0.15 * dy)),
        (round(sx + 0.15 * dx), round(sy + 0.15 * dy)),
        torso_color,
        3,
        cv2.LINE_AA,
    )
    for point in ((sx, sy), (hx, hy)):
        cv2.circle(image, point, 5, torso_color, -1, cv2.LINE_AA)
    # Magnitude stays the same when the display is mirrored; it is not a judgement
    # that leaning is wrong, nor an estimate of forward/backward lean in 3D.
    labels = (
        (f"Torso axis: {abs(angle):.1f} deg", torso_color),
        ("Vertical (level camera)", vertical_color),
    )
    label_y = max(24, min(hy + 24, height - 36))
    for i, (text, color) in enumerate(labels):
        (text_width, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.52, 2)
        x = max(8, min(hx + 16, width - text_width - 8))
        cv2.putText(
            image,
            text,
            (x, label_y + 24 * i),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            color,
            2,
            cv2.LINE_AA,
        )


def draw(
    image: Any,
    observation: FrameObservation,
    mirror: bool,
    lines: list[str],
    message: str,
    confidence: float,
    head_down_angle: float = -15.0,
    head_down_duration: float = 3.0,
    head_down_elapsed: float = 0.0,
    head_angle_smoothed: float | None = None,
    role: str | None = None,
    beat_state: dict[str, Any] | None = None,
    head_neutral_offset: float = 0.0,
    shoulder_raise_threshold: float = 10.0,
    shoulder_tension_duration: float = 1.5,
    shoulder_tension_elapsed: float = 0.0,
    shoulder_neutral_clearance: float = 1.71,
    shoulder_elev_smoothed: float | None = None,
    embrace_type: str | None = None,
    embrace_closed_threshold: float = 0.72,
    closed_embrace_min_knee_bend: float = 6.0,
    knee_straight_duration: float = 1.5,
    knee_straight_elapsed: float = 0.0,
    knee_bend_smoothed: float | None = None,
    facing: str = "right",
    side: str = "left",
    axis_neutral_offset: float = 0.0,
    axis_curve_entry: float = 0.06,
    axis_curve_clear: float = 0.035,
    axis_curve_duration: float = 1.5,
    axis_curve_elapsed: float = 0.0,
    axis_curve_smoothed: float | None = None,
    axis_curve_state: str = "WITHIN_LIMIT",
    verbose_skeleton: bool = True,
    show_vertical_axis: bool = True,
    show_footer: bool = True,
) -> Any:
    image = cv2.flip(image, 1) if mirror else image.copy()
    disp_w = max(960, observation.width)
    disp_h = round(observation.height * disp_w / observation.width)
    if (disp_w, disp_h) != (image.shape[1], image.shape[0]):
        image = cv2.resize(image, (disp_w, disp_h), interpolation=cv2.INTER_LINEAR)
    bad_head = head_down_elapsed >= head_down_duration
    bad_shoulders = shoulder_tension_elapsed >= shoulder_tension_duration
    bad_knees = knee_straight_elapsed >= knee_straight_duration
    bad_axis = axis_curve_state == "DEVIATION" or (
        axis_curve_elapsed >= axis_curve_duration
        and (axis_curve_smoothed is None or axis_curve_smoothed >= axis_curve_clear)
    )
    detected_role = role
    detected_embrace = embrace_type
    for pose in observation.poses:
        if detected_role is None:
            detected_role = detect_role(pose)
        if detected_embrace is None and detected_role in ("LEADER", "FOLLOWER"):
            detected_embrace = detect_embrace_type(
                pose, detected_role, closed_threshold=embrace_closed_threshold
            )
        if verbose_skeleton:
            for a, b in CONNECTIONS:
                if reliable(pose.get(a), confidence) and reliable(pose.get(b), confidence):
                    pa, pb = pose[a], pose[b]
                    cv2.line(
                        image,
                        screen_point(pa.x, pa.y, disp_w, disp_h, mirror),
                        screen_point(pb.x, pb.y, disp_w, disp_h, mirror),
                        (80, 220, 80),
                        2,
                    )
        for p in pose.values():
            if math.isfinite(p.x) and math.isfinite(p.y) and 0 <= p.x <= 1 and 0 <= p.y <= 1:
                if verbose_skeleton:
                    cv2.circle(
                        image,
                        screen_point(p.x, p.y, disp_w, disp_h, mirror),
                        3,
                        (80, 220, 80) if reliable(p, confidence) else (0, 180, 255),
                        -1,
                    )
                elif reliable(p, confidence):
                    cv2.circle(
                        image,
                        screen_point(p.x, p.y, disp_w, disp_h, mirror),
                        4,
                        (80, 220, 80),
                        -1,
                    )
        head_info = head_horizontal_alignment(
            pose, observation.width, observation.height, neutral_offset=head_neutral_offset
        )
        if head_info is not None:
            raw_angle, ear, nose = head_info
            angle = head_angle_smoothed if head_angle_smoothed is not None else raw_angle
            ex, ey = screen_point(ear.x, ear.y, disp_w, disp_h, mirror)
            nx, ny = screen_point(nose.x, nose.y, disp_w, disp_h, mirror)
            dir_x = 1 if nx >= ex else -1
            seg_len = max(65.0, math.hypot(nx - ex, ny - ey))
            hx = round(ex + dir_x * seg_len)
            rad = math.radians(angle)
            vx = round(ex + dir_x * seg_len * math.cos(rad))
            vy = round(ey - seg_len * math.sin(rad))
            vec_color = (
                (0, 0, 255)
                if bad_head
                else (0, 165, 255)
                if angle < head_down_angle
                else (80, 220, 80)
            )
            # Horizontal reference line through the ear
            cv2.line(image, (ex, ey), (hx, ey), (255, 220, 0), 2, cv2.LINE_AA)
            # Head alignment vector (ear -> nose)
            cv2.line(image, (ex, ey), (vx, vy), vec_color, 3 if bad_head else 2, cv2.LINE_AA)
            base_deg = 0.0 if dir_x > 0 else 180.0
            end_deg = base_deg - angle if dir_x > 0 else base_deg + angle
            cv2.ellipse(
                image,
                (ex, ey),
                (34, 34),
                0.0,
                min(base_deg, end_deg),
                max(base_deg, end_deg),
                vec_color,
                2,
                cv2.LINE_AA,
            )
            label = f"Head horiz: {angle:+.1f} deg"
            if head_down_elapsed > 0:
                label += f" ({head_down_elapsed:.1f}s)"
            cv2.putText(
                image,
                label,
                (max(8, min(ex, nx) - 10), max(24, min(ey, ny) - 14)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                vec_color,
                2,
                cv2.LINE_AA,
            )
        sh_info = shoulder_tension_alignment(
            pose,
            observation.width,
            observation.height,
            neutral_clearance=shoulder_neutral_clearance,
        )
        if sh_info is not None:
            raw_elev_pct, raw_clear, sh_ear, sh_lm = sh_info
            elev_pct = shoulder_elev_smoothed if shoulder_elev_smoothed is not None else raw_elev_pct
            _sex, sey = screen_point(sh_ear.x, sh_ear.y, disp_w, disp_h, mirror)
            sx, sy = screen_point(sh_lm.x, sh_lm.y, disp_w, disp_h, mirror)
            sh_color = (
                (0, 0, 255)
                if bad_shoulders
                else (0, 165, 255)
                if elev_pct >= shoulder_raise_threshold
                else (80, 220, 80)
            )
            # Draw shoulder level indicator and downward cue when shoulders are raised
            cv2.line(image, (sx - 28, sy), (sx + 28, sy), sh_color, 3 if bad_shoulders else 2, cv2.LINE_AA)
            if raw_clear > 1e-3:
                target_dy = round((sy - sey) * (shoulder_neutral_clearance / raw_clear))
                target_sy = min(disp_h - 8, max(sey + 15, sey + target_dy))
                cv2.line(image, (sx - 22, target_sy), (sx + 22, target_sy), (255, 220, 0), 2, cv2.LINE_AA)
                if elev_pct >= shoulder_raise_threshold and target_sy > sy + 4:
                    cv2.arrowedLine(
                        image,
                        (sx, sy),
                        (sx, target_sy),
                        sh_color,
                        2,
                        cv2.LINE_AA,
                        tipLength=0.35,
                    )
            sh_label = (
                f"LOWER SHOULDERS ({elev_pct:+.0f}%)"
                if elev_pct >= shoulder_raise_threshold
                else f"Shoulders: OK ({elev_pct:+.0f}%)"
            )
            cv2.putText(
                image,
                sh_label,
                (max(8, sx - 60), min(disp_h - 12, sy + 24)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                sh_color,
                2,
                cv2.LINE_AA,
            )
        knee_info = knee_bend_alignment(
            pose, observation.width, observation.height, side=side
        )
        if knee_info is not None:
            raw_knee_bend_deg, vis_legs = knee_info
            knee_bend_deg = (
                knee_bend_smoothed if knee_bend_smoothed is not None else raw_knee_bend_deg
            )
            knees_straight = bad_knees or knee_bend_deg < closed_embrace_min_knee_bend
            knee_color = (
                (0, 0, 255)
                if bad_knees
                else (0, 165, 255)
                if knees_straight
                else (80, 220, 80)
            )
            for _hip_lm, k_lm, _ank_lm in vis_legs:
                kx, ky = screen_point(k_lm.x, k_lm.y, disp_w, disp_h, mirror)
                cv2.circle(
                    image,
                    (kx, ky),
                    12 if knees_straight else 7,
                    knee_color,
                    3 if bad_knees else 2,
                    cv2.LINE_AA,
                )
            if vis_legs:
                first_k = vis_legs[0][1]
                kx, ky = screen_point(first_k.x, first_k.y, disp_w, disp_h, mirror)
                knee_label = (
                    f"BEND KNEES ({knee_bend_deg:.1f} deg < {closed_embrace_min_knee_bend:.1f} deg)"
                    if knees_straight
                    else f"Knees: OK ({knee_bend_deg:.1f} deg)"
                )
                if knee_straight_elapsed > 0:
                    knee_label += f" ({knee_straight_elapsed:.1f}s)"
                cv2.putText(
                    image,
                    knee_label,
                    (max(8, kx + 16), min(disp_h - 12, ky + 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.52,
                    knee_color,
                    2,
                    cv2.LINE_AA,
                )
        if show_vertical_axis:
            _draw_torso_axis(image, pose, mirror, confidence)
    role_text = f"Role: {detected_role or 'NONE'}"
    role_color = (
        (255, 220, 0)
        if detected_role == "LEADER"
        else (255, 140, 255)
        if detected_role == "FOLLOWER"
        else (200, 200, 200)
    )
    cv2.putText(
        image,
        role_text,
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        role_color,
        2,
        cv2.LINE_AA,
    )
    alert_y = 62
    if detected_role in ("LEADER", "FOLLOWER") and detected_embrace in ("CLOSED", "OPEN"):
        embrace_label = f"Embrace: {detected_embrace} EMBRACE"
        embrace_color = (0, 230, 255) if detected_embrace == "CLOSED" else (120, 240, 120)
        cv2.putText(
            image,
            embrace_label,
            (12, 56),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            embrace_color,
            2,
            cv2.LINE_AA,
        )
        alert_y = 88
    if bad_head:
        cv2.putText(
            image,
            f"BAD HEAD ALIGNMENT ({head_down_elapsed:.1f}s > {head_down_duration:.1f}s)",
            (12, alert_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
        alert_y += 32
    if bad_shoulders:
        cv2.putText(
            image,
            f"LOWER YOUR SHOULDERS ({shoulder_tension_elapsed:.1f}s > {shoulder_tension_duration:.1f}s)",
            (12, alert_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
        alert_y += 32
    if bad_knees:
        cv2.putText(
            image,
            f"BEND YOUR KNEES ({knee_straight_elapsed:.1f}s > {knee_straight_duration:.1f}s)",
            (12, alert_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
        alert_y += 32
    if bad_axis:
        cv2.putText(
            image,
            f"HIP OFFSET: PAST ANKLE-HEAD LINE ({axis_curve_elapsed:.1f}s > {axis_curve_duration:.1f}s)",
            (12, alert_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
    if beat_state is not None:
        beat = int(beat_state.get("beat", 0))
        phrase = int(beat_state.get("phrase", 0))
        bpm = float(beat_state.get("bpm", 120.0))
        playing = bool(beat_state.get("playing", True))
        analyzing = bool(beat_state.get("analyzing", False))
        analyzed_sec = float(beat_state.get("analyzed_seconds", 0.0))
        target_sec = float(beat_state.get("analysis_target_seconds", 30.0))
        phrase_locked = bool(beat_state.get("phrase_locked", False))
        source = str(beat_state.get("source", ""))
        box_w, gap = 24, 4
        start_x = max(12, disp_w - 8 * (box_w + gap) - 12)
        if not playing:
            if source == "mic-denied":
                header_text = f"WAITING FOR SONG TO BEGIN... | {bpm:.0f} BPM [MIC DENIED]"
                header_color = (0, 0, 255)
            else:
                header_text = f"WAITING FOR SONG TO BEGIN... | {bpm:.0f} BPM"
                header_color = (200, 200, 200)
        elif analyzing:
            header_text = (
                f"UNDERSTANDING BEATS ({analyzed_sec:.0f}/{target_sec:.0f}s) | "
                f"BEAT {beat}/8 | {bpm:.0f} BPM"
            )
            header_color = (80, 220, 80)
        else:
            lock_tag = " [LOCKED]" if phrase_locked else ""
            header_text = f"Phrase {phrase} | BEAT {beat}/8 | {bpm:.0f} BPM{lock_tag}"
            header_color = (80, 220, 80)
        (tw, _), _ = cv2.getTextSize(header_text, cv2.FONT_HERSHEY_SIMPLEX, 0.52, 2)
        text_x = max(12, disp_w - tw - 12)
        cv2.putText(
            image,
            header_text,
            (text_x, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            header_color,
            2,
            cv2.LINE_AA,
        )
        for idx in range(1, 9):
            bx = start_x + (idx - 1) * (box_w + gap)
            by = 32
            active_beat = playing and (idx == beat)
            fill_color = (80, 220, 80) if active_beat else (45, 45, 45)
            cv2.rectangle(image, (bx, by), (bx + box_w, by + 24), fill_color, -1)
            cv2.rectangle(image, (bx, by), (bx + box_w, by + 24), (200, 200, 200), 1)
            cv2.putText(
                image,
                str(idx),
                (bx + 6, by + 18),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 0, 0) if active_beat else (210, 210, 210),
                2 if active_beat else 1,
                cv2.LINE_AA,
            )
        if analyzing and target_sec > 0:
            total_bar_w = 8 * (box_w + gap) - gap
            prog_w = int(min(1.0, max(0.0, analyzed_sec / target_sec)) * total_bar_w)
            cv2.rectangle(image, (start_x, 60), (start_x + total_bar_w, 65), (50, 50, 50), -1)
            cv2.rectangle(image, (start_x, 60), (start_x + prog_w, 65), (80, 220, 80), -1)
    if not show_footer:
        return image
    # Dedicated bottom panel keeps status readable while image fills 100% horizontally.
    panel = cv2.copyMakeBorder(
        image,
        0,
        max(220, 24 * (len(lines) + 2)),
        0,
        0,
        cv2.BORDER_CONSTANT,
    )
    for i, line in enumerate([message, *lines]):
        is_red = i == 0 and (bad_head or bad_shoulders or bad_knees or bad_axis)
        cv2.putText(
            panel,
            line,
            (12, disp_h + 24 + 24 * i),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55 if is_red else 0.5,
            (0, 0, 255) if is_red else (255, 255, 255),
            2 if is_red else 1,
            cv2.LINE_AA,
        )
    return panel
