"""Configuration tests: single-camera compat, multi-camera list, credentials."""

import json

import pytest

from xiongmai_bridge.config import ConfigError, load


def test_single_camera_mode_from_env():
    env = {"CAM_HOST": "10.0.0.5", "CAM_PASS": "secret", "CAM_NAME": "cam1",
           "PRESETS": '{"1": "Bed"}', "LABELS": '{"volume": "Громкость"}'}
    (cam,) = load(env)
    assert cam.name == "cam1" and cam.host == "10.0.0.5"
    assert cam.presets == {1: "Bed"}
    assert cam.label("volume", "Speaker volume") == "Громкость"
    assert cam.base == "xiongmai/cam1"


def test_nothing_configured_is_an_error():
    with pytest.raises(ConfigError):
        load({})


def test_multi_camera_inline_json_with_defaults():
    env = {"CAMERAS": json.dumps({
        "defaults": {"poll_seconds": 15, "labels": {"preset_prefix": "Пресет"}},
        "cameras": [
            {"name": "bedroom", "host": "10.0.0.5", "password": "a", "presets": {"1": "Кровать"}},
            {"name": "hall", "host": "10.0.0.6", "password": "b", "poll_seconds": 60},
        ]})}
    bedroom, hall = load(env)
    assert bedroom.poll_seconds == 15 and hall.poll_seconds == 60
    assert bedroom.presets == {1: "Кровать"}
    assert bedroom.label("preset_prefix", "Preset") == "Пресет"
    assert bedroom.base != hall.base


def test_yaml_file_with_secret_file(tmp_path):
    secrets = tmp_path / "cameras.json"
    secrets.write_text(json.dumps({"bedroom": {"user": "urfn", "password": "pw"}}))
    cfg = tmp_path / "cameras.yml"
    cfg.write_text(
        "cameras:\n"
        "  - name: bedroom\n"
        "    host: 10.0.0.5\n"
        "    secret_file: cameras.json\n"
        "    secret_key: bedroom\n"
        "    presets: {1: Bed}\n")
    (cam,) = load({"CAMERAS_FILE": str(cfg)})
    assert cam.password == "pw" and cam.host == "10.0.0.5"
    # the login user comes from the same secret object unless set explicitly
    assert cam.user == "urfn"
    assert cam.presets == {1: "Bed"}


def test_explicit_user_wins_over_secret(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"cam": {"user": "secretuser", "password": "pw"}}))
    cfg = tmp_path / "cameras.yml"
    cfg.write_text("cameras:\n"
                   "  - {name: cam, host: h, user: explicit, secret_file: s.json, secret_key: cam}\n")
    (cam,) = load({"CAMERAS_FILE": str(cfg)})
    assert cam.user == "explicit" and cam.password == "pw"


def test_password_from_env_variable():
    env = {"CAMERAS": json.dumps([{"name": "cam", "host": "h",
                                   "password_env": "CAM_PW"}]),
           "CAM_PW": "from-env"}
    (cam,) = load(env)
    assert cam.password == "from-env"


def test_missing_password_is_reported_per_camera():
    env = {"CAMERAS": json.dumps([{"name": "cam", "host": "h"}])}
    with pytest.raises(ConfigError, match="cam"):
        load(env)


@pytest.mark.parametrize("entry", [
    {"name": "cam", "host": "h", "password": "p", "presets": {"255": "x"}},
    {"name": "cam", "host": "h", "password": "p", "presets": {"128": "x"}},
    {"name": "cam", "host": "h", "password": "p", "presets": {"0": "x"}},
    {"name": "Bad Name", "host": "h", "password": "p"},
    {"name": "cam", "host": "", "password": "p"},
])
def test_invalid_entries_are_rejected(entry):
    with pytest.raises(ConfigError):
        load({"CAMERAS": json.dumps([entry])})


def test_duplicate_camera_names_are_rejected():
    env = {"CAMERAS": json.dumps([
        {"name": "cam", "host": "h1", "password": "p"},
        {"name": "cam", "host": "h2", "password": "p"}])}
    with pytest.raises(ConfigError, match="duplicate"):
        load(env)
