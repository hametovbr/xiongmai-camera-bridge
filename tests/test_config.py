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


# -- preset control keys (v0.3.0) -------------------------------------------


def _cam(**extra):
    entry = {"name": "cam", "host": "h", "password": "p"}
    entry.update(extra)
    return {"CAMERAS": json.dumps([entry])}


def test_preset_control_defaults():
    (cam,) = load(_cam())
    assert cam.auto_presets is True
    assert cam.preset_slots == [1, 2, 3, 4]
    assert cam.max_presets == 12


def test_legacy_config_without_new_keys_is_valid():
    # a 0.2.2 entry keeps loading; auto_presets defaults on, opt-out is explicit
    (cam,) = load(_cam(presets={"1": "Bed", "2": "Door"}))
    assert cam.presets == {1: "Bed", 2: "Door"}
    (legacy,) = load(_cam(auto_presets=False))
    assert legacy.auto_presets is False


@pytest.mark.parametrize("slots", [
    [100], [128], [141], [250], [255],   # reserved / outside the table
    [0], [256], [-1],                    # out of range (same rule as presets)
    ["x"], [1, "y"],                     # not numbers
    [1, 128],                            # one bad entry poisons the list
])
def test_invalid_preset_slots_are_rejected(slots):
    with pytest.raises(ConfigError):
        load(_cam(preset_slots=slots))


def test_empty_preset_slots_is_rejected():
    with pytest.raises(ConfigError, match="empty"):
        load(_cam(preset_slots=[]))


def test_preset_slots_duplicates_are_dropped():
    (cam,) = load(_cam(preset_slots=[3, 1, 3, 2, 1]))
    assert cam.preset_slots == [1, 2, 3]


def test_preset_slots_not_a_list_is_rejected():
    with pytest.raises(ConfigError):
        load(_cam(preset_slots="1,2"))


@pytest.mark.parametrize("value", [0, -3])
def test_max_presets_must_be_positive(value):
    with pytest.raises(ConfigError, match="max_presets"):
        load(_cam(max_presets=value))


def test_presets_outside_whitelist_are_allowed():
    # goto stays allowed for config presets; only save/delete use the whitelist
    (cam,) = load(_cam(presets={"5": "Window"}, preset_slots=[1, 2]))
    assert cam.presets == {5: "Window"}
    assert cam.preset_slots == [1, 2]


def test_preset_control_keys_merge_from_defaults():
    env = {"CAMERAS": json.dumps({
        "defaults": {"preset_slots": [1, 2], "max_presets": 6,
                     "labels": {"preset_auto_prefix": "Пресет"}},
        "cameras": [{"name": "a", "host": "h", "password": "p"},
                    {"name": "b", "host": "h", "password": "p",
                     "preset_slots": [7, 8]}]})}
    a, b = load(env)
    assert a.preset_slots == [1, 2] and a.max_presets == 6
    assert b.preset_slots == [7, 8] and b.max_presets == 6
    assert a.label("preset_auto_prefix", "Preset") == "Пресет"
