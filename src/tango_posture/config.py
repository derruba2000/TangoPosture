import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_MAP: dict[str, tuple[str, Callable[[str], float | bool]]] = {
    "HEAD_DOWN_DURATION_SECONDS": ("head_down_duration", float),
    "HEAD_DOWN_ANGLE_DEGREES": ("head_down_angle", float),
    "BEAT_TRACKING_ENABLED": (
        "beat_tracking",
        lambda s: s.lower() not in ("0", "false", "no", "off"),
    ),
    "TANGO_BPM": ("tango_bpm", float),
    "BEAT_ANALYSIS_SECONDS": ("beat_analysis_seconds", float),
}


def load_dotenv(env_path: Path = Path(".env")) -> dict[str, float | bool]:
    values: dict[str, float | bool] = {}
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = (part.strip() for part in line.split("=", 1))
            if key in ENV_MAP:
                field_name, parser_fn = ENV_MAP[key]
                values[field_name] = parser_fn(val.strip("'\""))
    for env_key, (field_name, parser_fn) in ENV_MAP.items():
        if env_key in os.environ:
            values[field_name] = parser_fn(os.environ[env_key].strip("'\""))
    return values


class Threshold(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    entry: float = Field(gt=0)
    clear: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> "Threshold":
        if self.clear >= self.entry:
            raise ValueError("clear threshold must be below entry threshold")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    camera: int = Field(default=0, ge=0)
    width: int = Field(default=640, gt=0)
    height: int = Field(default=480, gt=0)
    model_path: str = "models/pose_landmarker_lite.task"
    data_dir: str = "data"
    side: Literal["left", "right"] = "left"
    facing: Literal["left", "right"] = "right"
    capture_profile: str = "default"
    mirror: bool = False
    confidence: float = Field(default=0.70, ge=0, le=1)
    min_torso: float = Field(default=0.12, gt=0)
    min_head: float = Field(default=0.04, gt=0)
    max_view_width: float = Field(default=0.45, gt=0)
    view_change_tolerance: float = Field(default=0.15, gt=0)
    smoothing: float = Field(default=0.25, gt=0)
    entry_duration: float = Field(default=1.5, gt=0)
    clear_duration: float = Field(default=0.75, gt=0)
    max_gap: float = Field(default=0.5, gt=0)
    calibration_duration: float = Field(default=12, gt=0)
    calibration_valid_duration: float = Field(default=9, gt=0)
    calibration_samples: int = Field(default=60, ge=2)
    offset_mad: float = Field(default=0.03, gt=0)
    angle_mad: float = Field(default=4, gt=0)
    head_down_duration: float = Field(default=3.0, gt=0)
    head_down_angle: float = Field(default=-15.0, lt=0)
    beat_tracking: bool = True
    tango_bpm: float = Field(default=120.0, gt=20, le=240)
    beat_analysis_seconds: float = Field(default=30.0, gt=0)
    head_forward: Threshold = Threshold(entry=0.08, clear=0.05)
    head_inclination: Threshold = Threshold(entry=10, clear=6)
    trunk_inclination: Threshold = Threshold(entry=8, clear=5)

    @model_validator(mode="after")
    def durations(self) -> "Config":
        if self.calibration_valid_duration > self.calibration_duration:
            raise ValueError("valid calibration duration exceeds capture window")
        return self


def load(path: Path | None, overrides: dict, env_path: Path = Path(".env")) -> Config:
    data: dict = load_dotenv(env_path)
    if path:
        data.update(tomllib.loads(path.read_text()))
    data.update({k: v for k, v in overrides.items() if v is not None})
    return Config.model_validate(data)
