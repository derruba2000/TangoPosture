import json
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ENV_SUFFIXES = {
    "head_neutral_offset": "HEAD_NEUTRAL_OFFSET_DEGREES",
    "head_down_duration": "HEAD_DOWN_DURATION_SECONDS",
    "head_down_angle": "HEAD_DOWN_ANGLE_DEGREES",
    "beat_tracking": "BEAT_TRACKING_ENABLED",
    "tango_bpm": "TANGO_BPM",
    "beat_analysis_seconds": "BEAT_ANALYSIS_SECONDS",
    "role_assign_ms": "ROLE_ASSIGN_MS",
    "role_hold_ms": "ROLE_HOLD_MS",
    "shoulder_tension_duration": "SHOULDER_TENSION_DURATION_SECONDS",
    "shoulder_raise_threshold": "SHOULDER_RAISE_THRESHOLD_PCT",
    "shoulder_neutral_clearance": "SHOULDER_NEUTRAL_CLEARANCE",
    "allow_all_angles": "ALLOW_ALL_ANGLES",
    "embrace_closed_threshold": "EMBRACE_CLOSED_THRESHOLD",
    "closed_embrace_min_knee_bend": "CLOSED_EMBRACE_MIN_KNEE_BEND_DEGREES",
    "knee_straight_duration": "KNEE_STRAIGHT_DURATION_SECONDS",
    "axis_curve_entry": "AXIS_CURVE_ENTRY",
    "axis_curve_clear": "AXIS_CURVE_CLEAR",
    "axis_curve_duration": "AXIS_CURVE_DURATION_SECONDS",
    "axis_curve_clear_duration": "AXIS_CURVE_CLEAR_DURATION_SECONDS",
    "axis_neutral_offset": "AXIS_NEUTRAL_OFFSET",
}


def load_dotenv(
    env_path: Path = Path(".env"), *, include_process: bool = True, allow_legacy: bool = False
) -> dict:
    values: dict = {}
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = (part.strip() for part in line.split("=", 1))
            key = key.removeprefix("export ")
            if key in _ENV_SUFFIXES.values():
                if not allow_legacy:
                    raise ValueError(
                        f"Rename {key} to tp_{key} in {env_path}; or use gui --ignore-env to import and migrate this file"
                    )
                key = "tp_" + key
            if key in ENV_MAP:
                if val.startswith('"'):
                    val = json.loads(val)
                elif val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                set_setting(values, key, val)
    if include_process:
        for env_key in ENV_MAP:
            if env_key in os.environ:
                set_setting(values, env_key, os.environ[env_key])
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
    head_neutral_offset: float = Field(default=-12.0)
    head_down_duration: float = Field(default=3.0, gt=0)
    head_down_angle: float = Field(default=-15.0, lt=0)
    beat_tracking: bool = True
    tango_bpm: float = Field(default=120.0, gt=20, le=240)
    beat_analysis_seconds: float = Field(default=30.0, gt=0)
    auto_record_start_delay: float = Field(default=1.0, ge=0, le=60)
    auto_record_stop_delay: float = Field(default=3.0, ge=0, le=120)
    max_recordings: int = Field(default=10, ge=1, le=1000)
    role_assign_ms: float = Field(default=300.0, ge=0)
    role_hold_ms: float = Field(default=5000.0, ge=0)
    shoulder_tension_duration: float = Field(default=1.0, gt=0)
    shoulder_raise_threshold: float = Field(default=10.0, gt=0)
    shoulder_neutral_clearance: float = Field(default=1.05, gt=0)
    allow_all_angles: bool = False
    embrace_closed_threshold: float = Field(default=0.72, gt=0, le=1.0)
    closed_embrace_min_knee_bend: float = Field(default=8.0, ge=0)
    knee_straight_duration: float = Field(default=1.0, gt=0)
    axis_curve_entry: float = Field(default=0.06, gt=0)
    axis_curve_clear: float = Field(default=0.035, ge=0)
    axis_curve_duration: float = Field(default=1.5, gt=0)
    axis_curve_clear_duration: float = Field(default=0.75, gt=0)
    axis_neutral_offset: float = Field(default=0.0)
    replay_audio: bool = False
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = Field(default="llama3.2", min_length=1)
    ollama_timeout: float = Field(default=120.0, gt=0, le=1800)
    ollama_temperature: float = Field(default=0.2, ge=0, le=2)
    ollama_num_ctx: int = Field(default=16384, ge=1024, le=131072)
    ollama_num_predict: int = Field(default=2048, ge=128, le=16384)
    ollama_max_report_chars: int = Field(default=40000, ge=1000)
    ollama_instruction: str = (
        "Explain the supported observations and suggest three focused practice exercises."
    )
    head_forward: Threshold = Threshold(entry=0.08, clear=0.05)
    head_inclination: Threshold = Threshold(entry=10, clear=6)
    trunk_inclination: Threshold = Threshold(entry=8, clear=5)
    axis_curve: Threshold | None = None

    @model_validator(mode="after")
    def durations(self) -> "Config":
        if self.calibration_valid_duration > self.calibration_duration:
            raise ValueError("valid calibration duration exceeds capture window")
        if self.axis_curve is not None:
            if "axis_curve_entry" not in self.model_fields_set:
                self.axis_curve_entry = self.axis_curve.entry
            if "axis_curve_clear" not in self.model_fields_set:
                self.axis_curve_clear = self.axis_curve.clear
        if self.axis_curve_clear >= self.axis_curve_entry:
            raise ValueError("axis_curve clear threshold must be below entry threshold")
        if self.axis_curve is None:
            self.axis_curve = Threshold(entry=self.axis_curve_entry, clear=self.axis_curve_clear)
        return self


def parse_bool(value: str) -> bool:
    if value.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        raise ValueError("expected true or false")
    return value.lower() in ("true", "1", "yes", "on")


# Each editable field belongs to exactly one group. The redundant axis_curve TOML
# alias is represented by axis_curve_entry/clear in the editor and environment.
SETTING_GROUPS = {
    "Camera & view": (
        "camera",
        "width",
        "height",
        "side",
        "facing",
        "capture_profile",
        "mirror",
        "allow_all_angles",
    ),
    "Files": ("model_path", "data_dir"),
    "Quality & calibration": (
        "confidence",
        "min_torso",
        "min_head",
        "max_view_width",
        "view_change_tolerance",
        "calibration_duration",
        "calibration_valid_duration",
        "calibration_samples",
        "offset_mad",
        "angle_mad",
    ),
    "Tracking": (
        "smoothing",
        "entry_duration",
        "clear_duration",
        "max_gap",
        "head_forward.entry",
        "head_forward.clear",
        "head_inclination.entry",
        "head_inclination.clear",
        "trunk_inclination.entry",
        "trunk_inclination.clear",
    ),
    "Head & shoulders": (
        "head_neutral_offset",
        "head_down_duration",
        "head_down_angle",
        "shoulder_tension_duration",
        "shoulder_raise_threshold",
        "shoulder_neutral_clearance",
    ),
    "Embrace & knees": (
        "role_assign_ms",
        "role_hold_ms",
        "embrace_closed_threshold",
        "closed_embrace_min_knee_bend",
        "knee_straight_duration",
    ),
    "Torso & hip offset": (
        "axis_curve_entry",
        "axis_curve_clear",
        "axis_curve_duration",
        "axis_curve_clear_duration",
        "axis_neutral_offset",
    ),
    "Music": (
        "beat_tracking", "tango_bpm", "beat_analysis_seconds",
        "auto_record_start_delay", "auto_record_stop_delay", "max_recordings",
    ),
    "Review & Ollama": (
        "replay_audio",
        "ollama_base_url",
        "ollama_model",
        "ollama_timeout",
        "ollama_temperature",
        "ollama_num_ctx",
        "ollama_num_predict",
        "ollama_max_report_chars",
        "ollama_instruction",
    ),
}

ENV_MAP: dict[str, tuple[str, Callable]] = {}


def _setting_parser(field: str) -> Callable:
    kind = float if "." in field else Config.model_fields[field].annotation
    if kind is bool:
        return parse_bool
    if kind is int:
        return int
    if kind is float:
        return float
    return str


for _fields in SETTING_GROUPS.values():
    for _field in _fields:
        _name = "tp_" + _ENV_SUFFIXES.get(_field, _field.replace(".", "_").upper())
        ENV_MAP[_name] = (_field, _setting_parser(_field))


def set_setting(data: dict, env_key: str, value: str) -> None:
    field, parse = ENV_MAP[env_key]
    try:
        parsed = parse(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{env_key}: {error}") from error
    target = data
    parts = field.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = parsed


def setting_value(cfg: Config, field: str):
    value = cfg.model_dump()
    for part in field.split("."):
        value = value[part]
    return value


def export_dotenv(cfg: Config) -> str:
    lines = ["# TangoPosture settings. Process environment overrides this file."]
    for group, fields in SETTING_GROUPS.items():
        lines.extend(("", f"# {group}"))
        for key, (field, _) in ENV_MAP.items():
            if field in fields:
                lines.append(f"{key}={json.dumps(setting_value(cfg, field))}")
    return "\n".join(lines) + "\n"


def export_toml(cfg: Config) -> str:
    data = cfg.model_dump(exclude={"axis_curve"})
    lines = [
        f"{key} = {json.dumps(value)}" for key, value in data.items() if not isinstance(value, dict)
    ]
    for key, value in data.items():
        if isinstance(value, dict):
            lines.extend(("", f"[{key}]"))
            lines.extend(f"{name} = {json.dumps(number)}" for name, number in value.items())
    return "\n".join(lines) + "\n"


def load(
    path: Path | None, overrides: dict, env_path: Path = Path(".env"), *, use_env: bool = True
) -> Config:
    data: dict = load_dotenv(env_path) if use_env else {}
    if path:
        data.update(tomllib.loads(path.read_text()))
    data.update({k: v for k, v in overrides.items() if v is not None})
    return Config.model_validate(data)
