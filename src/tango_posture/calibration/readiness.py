"""Compatibility and clip-interval readiness for baseline analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..domain import (
    Calibration,
)
from ..models import analysis_model_path, model_manifest, verify
from ..references.video import VideoProbeInfo, validate_clip_range
from .baseline import compatible, profile


@dataclass(frozen=True)
class CalibrationReadiness:
    """Pre-check or outcome for reference-relative posture review."""

    ready: bool
    mode: str  # "movement" | "existing_reference" | "clip_interval"
    summary: str
    reason: str = ""
    reference_id: str | None = None
    reference_name: str | None = None
    interval_source: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": bool(self.ready),
            "mode": self.mode,
            "summary": self.summary,
            "reason": self.reason,
            "reference_id": self.reference_id,
            "reference_name": self.reference_name,
            "interval_source": list(self.interval_source) if self.interval_source else None,
        }


def check_calibration_readiness(
    *,
    analysis_mode: str,
    cfg: Config,
    probe: VideoProbeInfo,
    selected_in: float,
    selected_out: float,
    reference: Calibration | None = None,
    calibration_interval: tuple[float, float] | None = None,
    model_hash: str | None = None,
) -> CalibrationReadiness:
    """Check whether reference-relative posture review can run with the chosen inputs."""
    clip_in, clip_out = validate_clip_range(probe, selected_in, selected_out)
    if analysis_mode == "movement":
        return CalibrationReadiness(
            ready=True,
            mode="movement",
            summary="Movement review (no calibration required)",
        )
    if analysis_mode != "posture":
        raise ValueError(f"Unsupported analysis_mode: {analysis_mode!r}")

    if reference is not None:
        model_path = analysis_model_path(cfg)
        resolved_hash = model_hash or (
            verify(model_path, cfg.analysis_model) if model_path.is_file() else
            model_manifest(cfg.analysis_model)["sha256"]
        )
        cap_profile = profile(cfg, probe.width, probe.height, "video")
        try:
            compatible(reference, cap_profile, resolved_hash)
        except ValueError as exc:
            return CalibrationReadiness(
                ready=False,
                mode="existing_reference",
                summary="Existing reference is incompatible with this clip",
                reason=str(exc),
                reference_id=reference.id,
                reference_name=reference.name,
            )
        return CalibrationReadiness(
            ready=True,
            mode="existing_reference",
            summary=f"Ready with reference '{reference.name}' ({reference.id[:8]})",
            reference_id=reference.id,
            reference_name=reference.name,
        )

    if calibration_interval is not None:
        cal_in, cal_out = float(calibration_interval[0]), float(calibration_interval[1])
        if cal_in < clip_in - 1e-6 or cal_out > clip_out + 1e-6 or cal_out <= cal_in:
            return CalibrationReadiness(
                ready=False,
                mode="clip_interval",
                summary="Calibration interval must lie inside the selected clip range",
                reason=f"Calibration interval [{cal_in:.1f}s, {cal_out:.1f}s) is outside [{clip_in:.1f}s, {clip_out:.1f}s)",
                interval_source=(cal_in, cal_out),
            )
        cal_len = cal_out - cal_in
        if cal_len + 1e-6 < cfg.calibration_valid_duration:
            return CalibrationReadiness(
                ready=False,
                mode="clip_interval",
                summary="Calibration interval is too short",
                reason=(
                    f"Selected calibration interval is {cal_len:.1f}s; "
                    f"need at least {cfg.calibration_valid_duration:.1f}s of stable posture"
                ),
                interval_source=(cal_in, cal_out),
            )
        if (clip_out - clip_in) - cal_len < 0.5:
            return CalibrationReadiness(
                ready=False,
                mode="clip_interval",
                summary="Calibration interval leaves no practice frames to score",
                reason="Select a clip range longer than the calibration interval so practice frames remain",
                interval_source=(cal_in, cal_out),
            )
        return CalibrationReadiness(
            ready=True,
            mode="clip_interval",
            summary=f"Ready to calibrate from clip interval [{cal_in:.1f}s, {cal_out:.1f}s)",
            interval_source=(cal_in, cal_out),
        )

    return CalibrationReadiness(
        ready=False,
        mode="existing_reference",
        summary="Reference-relative posture review requires a reference or calibration interval",
        reason="Select a compatible saved reference, mark a stable calibration interval in the clip, or switch to Movement review",
    )
