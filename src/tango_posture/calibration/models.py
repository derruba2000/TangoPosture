"""Versioned approved-range models and supported measurement identities."""

from __future__ import annotations

import math
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain import (
    METRIC_VERSION,
    Calibration,
)
from .baseline import utc_now

RANGE_VERSION: Literal["range-measurements/1.0"] = "range-measurements/1.0"


METRIC_DETAILS = {
    "head_pitch": ("Head", "Head pitch (ear-to-nose)", "degrees"),
    "head_forward": ("Head", "Head-forward offset", "torso_lengths"),
    "head_inclination": ("Head", "Head inclination", "degrees"),
    "shoulder_clearance": ("Shoulders", "Ear-to-shoulder clearance", "normalized_clearance"),
    "hip_offset": ("Hips", "Hip offset from ankle-to-head axis", "axis_lengths"),
    "torso_axis_tilt": ("Torso", "Torso axis tilt (pink line)", "degrees"),
    "trunk_inclination": ("Torso", "Trunk inclination", "degrees"),
    "knee_flexion": ("Knees", "Least-bent reliably visible knee", "degrees"),
}


STEPS = ("Head", "Shoulders", "Hips", "Torso", "Knees")


CYCLIC_METRICS = {"head_inclination", "trunk_inclination"}


class Passage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Passage:
        if self.end <= self.start:
            raise ValueError("A passage must end after it starts")
        return self


class ApprovedRange(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lower: float
    upper: float
    unit: str
    view: Literal["side", "front", "all"]
    passages: list[Passage] = Field(min_length=1)
    median: float
    p10: float
    p90: float
    valid_duration: float = Field(ge=0)
    selected_duration: float = Field(gt=0)
    samples: int = Field(ge=2)
    inside_fraction: float = Field(ge=0, le=1)
    approved: Literal[True] = True
    camera_check_passed: bool = False

    @model_validator(mode="after")
    def ordered(self) -> ApprovedRange:
        if self.lower > self.upper or self.p10 > self.p90:
            raise ValueError("Range limits must be ordered")
        if self.valid_duration > self.selected_duration + 1e-8:
            raise ValueError("Tracking coverage exceeds selected duration")
        return self


class RangeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[2] = 2
    measurement_version: Literal["range-measurements/1.0"] = RANGE_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    created_at_utc: str = Field(default_factory=utc_now)
    context: str = Field(min_length=1)
    model_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    capture_profile: dict[str, Any]
    source: dict[str, Any]
    metrics: dict[str, ApprovedRange] = Field(min_length=1)
    camera_checks: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_metrics(self) -> RangeProfile:
        required = {"side", "facing", "width", "height", "aspect_ratio", "capture_profile", "source"}
        if not required <= self.capture_profile.keys():
            raise ValueError("Calibration requires a complete capture profile")
        capture = self.capture_profile
        if (
            capture["side"] not in ("left", "right") or capture["facing"] not in ("left", "right")
            or any(type(capture[key]) is not int or capture[key] <= 0 for key in ("width", "height"))
            or not isinstance(capture["source"], str)
            or not isinstance(capture["capture_profile"], str)
        ):
            raise ValueError("Invalid calibration capture settings")
        ratio = capture["aspect_ratio"]
        if not isinstance(ratio, (float, int)) or not math.isclose(
            ratio, capture["width"] / capture["height"], rel_tol=1e-8,
        ):
            raise ValueError("Calibration aspect ratio does not match the recorded dimensions")
        for key, target in self.metrics.items():
            if key not in METRIC_DETAILS or target.unit != METRIC_DETAILS[key][2]:
                raise ValueError(f"Unknown metric or incompatible unit: {key}")
        return self

    def calibration(self) -> Calibration:
        return Calibration(
            self.id, self.created_at_utc, self.name, self.capture_profile, {},
            {"range_profile": True}, self.model_hash, METRIC_VERSION,
            {"label": "approved ranges", "range_profile": self.model_dump(mode="json")},
        )


def profile_from_calibration(reference: Calibration) -> RangeProfile | None:
    payload = reference.review.get("range_profile")
    return RangeProfile.model_validate(payload) if payload is not None else None
