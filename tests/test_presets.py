"""Pure preset helpers: slot math, labels and the slots/state payload."""

import re

from xiongmai_bridge.config import CameraConfig
from xiongmai_bridge.presets import (published_slots, select_options, slot_label,
                                     slots_state, utc_ts, valid_slots)


def _cfg(**extra):
    base = dict(name="cam", host="10.0.0.1", password="x")
    base.update(extra)
    return CameraConfig(**base).validate()


def test_valid_slots_filters_reserved_and_junk():
    assert valid_slots([1, 100, 128, 141, 250, 255, None, "3", 0, 254, "x"]) == {1, 3, 254}


def test_valid_slots_koridor_case():
    # the corridor camera reports only the firmware slot 100 -> nothing usable
    assert valid_slots([100]) == set()
    assert valid_slots([]) == set()
    assert valid_slots(None) == set()


def test_published_slots_union_and_cap():
    cfg = _cfg(presets={1: "Bed", 2: "Door"})
    assert published_slots(cfg, [2, 3, 100]) == [1, 2, 3]
    cfg.max_presets = 2
    assert published_slots(cfg, [2, 3, 4]) == [1, 2]


def test_published_slots_config_preset_without_camera_slot():
    cfg = _cfg(presets={5: "Window"})
    assert published_slots(cfg, []) == [5]


def test_slot_label_config_wins_over_auto():
    cfg = _cfg(presets={1: "Кровать"}, labels={"preset_auto_prefix": "Пресет"})
    assert slot_label(cfg, 1) == "Кровать"
    assert slot_label(cfg, 3) == "Пресет 3"


def test_slot_label_default_auto_prefix():
    assert slot_label(_cfg(), 7) == "Preset 7"


def test_select_options_managed_plus_occupied_strings():
    cfg = _cfg(preset_slots=[1, 2, 3, 4])
    assert select_options(cfg, [2, 9, 100]) == ["1", "2", "3", "4", "9"]


def test_slots_state_schema():
    cfg = _cfg(presets={1: "Кровать", 2: "Вход"}, preset_slots=[1, 2, 3, 4])
    action = {"kind": "save", "slot": 3, "result": "ok", "ts": utc_ts()}
    state = slots_state(cfg, [1, 2, 100], last_action=action)
    assert set(state) == {"count", "slots", "free", "managed", "reserved",
                          "last_action", "last_error"}
    assert state["count"] == 2
    assert state["slots"] == {"1": "Кровать", "2": "Вход"}
    assert state["free"] == [3, 4]
    assert state["managed"] == [1, 2, 3, 4]
    assert state["reserved"] == [100, 128, 141, 250]
    assert state["last_action"] == action
    assert state["last_error"] is None
    # every key is JSON-safe and slot keys are strings
    assert all(isinstance(k, str) for k in state["slots"])


def test_slots_state_auto_label_and_error():
    cfg = _cfg(labels={"preset_auto_prefix": "Пресет"})
    err = {"kind": "save", "slot": 9, "message": "not in preset_slots", "ts": utc_ts()}
    state = slots_state(cfg, [3], last_error=err)
    assert state["slots"] == {"3": "Пресет 3"}
    assert state["free"] == [1, 2, 4]
    assert state["last_error"] == err


def test_utc_ts_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_ts())