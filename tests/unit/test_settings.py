import tomllib

import pytest

from tango_posture.config import (
    ENV_MAP,
    SETTING_GROUPS,
    Config,
    export_dotenv,
    export_toml,
    load,
    load_dotenv,
)


def test_every_setting_has_a_prefixed_variable_and_editor_group():
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
    assert set(fields) == expected == {field for field, _ in ENV_MAP.values()}
    assert all(key.startswith("tp_") for key in ENV_MAP)
    assert ENV_MAP["tp_HEAD_DOWN_ANGLE_DEGREES"][0] == "head_down_angle"


def test_full_environment_and_toml_round_trip(tmp_path):
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
    env = tmp_path / ".env"
    env.write_text(export_dotenv(cfg))
    assert Config.model_validate(load_dotenv(env, include_process=False)) == cfg
    assert Config.model_validate(tomllib.loads(export_toml(cfg))) == cfg


def test_environment_precedence_and_legacy_migration(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HEAD_DOWN_ANGLE_DEGREES=-19\n")
    with pytest.raises(ValueError, match="tp_HEAD_DOWN_ANGLE_DEGREES"):
        load_dotenv(env)
    assert load_dotenv(env, include_process=False, allow_legacy=True)["head_down_angle"] == -19
    env.write_text("tp_HEAD_DOWN_ANGLE_DEGREES=-19\n")
    monkeypatch.setenv("HEAD_DOWN_ANGLE_DEGREES", "-99")
    monkeypatch.setenv("tp_HEAD_DOWN_ANGLE_DEGREES", "-21")
    assert load(None, {}, env).head_down_angle == -21
    assert load(None, {}, env, use_env=False).head_down_angle == -15
    settings = tmp_path / "settings.toml"
    settings.write_text("head_down_angle=-23\n")
    assert load(settings, {}, env).head_down_angle == -23
    assert load(settings, {"head_down_angle": -25}, env).head_down_angle == -25


@pytest.mark.parametrize(
    "line", ["tp_MIRROR=maybe", "tp_CAMERA=foo", "tp_TANGO_BPM=nan", "tp_AXIS_CURVE_CLEAR=0.8"]
)
def test_invalid_settings_are_rejected(tmp_path, line):
    env = tmp_path / ".env"
    env.write_text(line)
    with pytest.raises(ValueError):
        load(None, {}, env)
