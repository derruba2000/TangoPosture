import tomllib
from pathlib import Path

import pytest

from tango_posture.config import (
    SETTING_GROUPS,
    SETTING_MAP,
    Config,
    export_toml,
    load,
)


def test_every_setting_has_a_toml_name_and_editor_group():
    expected = set(Config.model_fields) - {
        "head_forward",
        "head_inclination",
        "trunk_inclination",
        "axis_curve",
    }
    expected |= {
        f"{metric}.{threshold}"
        for metric in ("head_forward", "head_inclination", "trunk_inclination")
        for threshold in ("entry", "clear")
    }
    fields = [field for group in SETTING_GROUPS.values() for field in group]
    assert len(fields) == len(set(fields))
    assert set(fields) == expected == set(SETTING_MAP)
    assert SETTING_MAP["head_down_angle"][0] == "head_down_angle"


def test_default_toml_has_every_editable_setting():
    path = Path(__file__).resolve().parents[2] / "configs" / "default.toml"
    data = tomllib.loads(path.read_text())
    fields = {
        f"{key}.{name}" if isinstance(value, dict) else key
        for key, value in data.items()
        for name in (value if isinstance(value, dict) else [None])
    }
    assert fields == set(SETTING_MAP)
    Config.model_validate(data)


def test_full_toml_round_trip(tmp_path):
    cfg = Config(
        camera=2,
        mirror=True,
        model_path='models/with spaces "quoted".task',
        data_dir="my sessions",
        head_forward={"entry": 0.2, "clear": 0.1},
        axis_curve_entry=0.1,
        auto_record_start_delay=2.0,
        auto_record_stop_delay=5.0,
        max_recordings=3,
    )
    assert Config.model_validate(tomllib.loads(export_toml(cfg))) == cfg


def test_environment_is_ignored_and_toml_cli_precedence_is_preserved(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.chdir(tmp_path)
    env.write_text("HEAD_DOWN_ANGLE_DEGREES=-19\ntp_HEAD_DOWN_ANGLE_DEGREES=-21\n")
    monkeypatch.setenv("HEAD_DOWN_ANGLE_DEGREES", "-99")
    monkeypatch.setenv("tp_HEAD_DOWN_ANGLE_DEGREES", "-21")
    assert load(None, {}).head_down_angle == -15
    settings = tmp_path / "settings.toml"
    settings.write_text("head_down_angle=-23\n")
    assert load(settings, {}).head_down_angle == -23
    assert load(settings, {"head_down_angle": -25}).head_down_angle == -25


@pytest.mark.parametrize("line", ['mirror="maybe"', 'camera="foo"', 'tango_bpm=nan', 'axis_curve_clear=0.8'])
def test_invalid_settings_are_rejected(tmp_path, line):
    settings = tmp_path / "settings.toml"
    settings.write_text(line)
    with pytest.raises(ValueError):
        load(settings, {})
