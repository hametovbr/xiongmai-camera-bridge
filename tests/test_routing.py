"""Routing tests: preset goto hardening, preset_ctl save/delete/select, restore.

No camera and no broker: CameraWorker.call is monkeypatched onto a FakeCam,
mqtt.Client onto a FakeClient, and time.sleep is neutralised (the backlash
recipe would otherwise wait ~12 s per goto).
"""

import logging
import time
import types

import pytest

from xiongmai_bridge import bridge as bridge_mod
from xiongmai_bridge.bridge import Bridge, CameraWorker
from xiongmai_bridge.config import CameraConfig
from xiongmai_bridge.dvrip import DvripError


class FakeCam:
    """Records PTZ commands and maintains a fake Uart.PTZPreset table."""

    def __init__(self, slots=(1, 2)):
        self.calls = []
        self._slots = list(slots)
        self.fail_on = None

    def ptz(self, command, preset=65535, step=1):
        if command == self.fail_on:
            raise DvripError(f"ptz {command}: Ret=101")
        self.calls.append((command, preset))
        if command == "SetPreset" and preset not in self._slots:
            self._slots.append(preset)
        elif command == "ClearPreset" and preset in self._slots:
            self._slots.remove(preset)
        return 100

    def get_config(self, name):
        if name == "Uart":
            return {"PTZPreset": [[{"Id": s} for s in self._slots]]}
        raise DvripError(f"get {name}: Ret=607")

    def close(self):
        pass


class FakeClient:
    def __init__(self):
        self.messages = []
        self.subscriptions = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.messages.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def will_set(self, *a, **k):
        pass

    def username_pw_set(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def _worker(monkeypatch, slots=(1, 2), **cfg_extra):
    cfg = CameraConfig(name="cam", host="h", password="x",
                       presets={1: "Bed"}, **cfg_extra).validate()
    published = []
    w = CameraWorker(cfg, lambda t, p, retain=False: published.append((t, p, retain)))
    fake = FakeCam(slots)
    monkeypatch.setattr(w, "call", lambda fn: fn(fake))
    w.occupied = set(slots)
    w.features = {"slots": list(slots)}
    w.publish_discovery()
    published.clear()                     # keep only what the command under test publishes
    return w, fake, published


@pytest.fixture()
def worker(monkeypatch):
    return _worker(monkeypatch)


def _configs(published):
    return [t for t, p, r in published if t.endswith("/config") and p]


def _state(published, topic):
    for t, p, r in published:
        if t == topic:
            return p, r
    return None


# -- goto hardening ----------------------------------------------------------


def test_goto_routing(worker):
    w, fake, _ = worker
    assert w.handle(["preset", "3"], b"PRESS") is True
    assert ("GotoPreset", 3) in fake.calls


def test_goto_non_numeric_is_ignored_without_crash(worker, caplog):
    w, fake, published = worker
    with caplog.at_level(logging.WARNING, logger="xiongmai-bridge"):
        assert w.handle(["preset", "save"], b"PRESS") is True
    assert fake.calls == []
    assert any("invalid preset slot" in r.getMessage() for r in caplog.records)
    # a bad topic must not mark the camera offline
    assert not any(t.endswith("/availability") and p == "offline"
                   for t, p, r in published)


def test_goto_reserved_is_ignored(worker):
    w, fake, _ = worker
    for slot in ("100", "128", "141", "250", "255", "0"):
        assert w.handle(["preset", slot], b"PRESS") is True
    assert fake.calls == []


# -- select ------------------------------------------------------------------


def test_select_command_publishes_retained(worker):
    w, _, published = worker
    assert w.handle(["preset_ctl", "slot"], b"3") is True
    assert w.selected_slot == 3
    assert ("xiongmai/cam/preset_ctl/slot/state", "3", True) in published


def test_select_invalid_payload_ignored(worker):
    w, _, _ = worker
    w.handle(["preset_ctl", "slot"], b"abc")
    assert w.selected_slot is None


def test_restore_from_retained_does_not_republish(worker):
    w, _, published = worker
    w.restore_selected_slot(b"2")
    assert w.selected_slot == 2
    assert published == []


def test_restore_ignores_empty_and_invalid(worker):
    w, _, _ = worker
    w.restore_selected_slot(b"")
    w.restore_selected_slot(b"100")
    assert w.selected_slot is None


# -- save --------------------------------------------------------------------


def test_save_routing(worker):
    w, fake, published = worker
    w.selected_slot = 3
    assert w.handle(["preset_ctl", "save"], b"PRESS") is True
    assert ("SetPreset", 3) in fake.calls
    assert w.occupied == {1, 2, 3}
    assert w.last_action["kind"] == "save" and w.last_action["result"] == "ok"
    assert w.last_error is None
    # the new button appeared without any restart
    assert "homeassistant/button/cam/preset_3/config" in _configs(published)
    payload, retain = _state(published, "xiongmai/cam/preset_ctl/slots/state")
    assert retain is True and payload.count('"count": 3')


def test_save_without_selection_is_an_error(worker):
    w, fake, published = worker
    w.handle(["preset_ctl", "save"], b"PRESS")
    assert fake.calls == []
    assert w.last_error["kind"] == "save"
    assert w.last_action is None
    assert _state(published, "xiongmai/cam/preset_ctl/slots/state") is not None


def test_save_outside_whitelist_is_an_error(worker):
    w, fake, _ = worker
    w.selected_slot = 9                      # valid number, not in [1,2,3,4]
    w.handle(["preset_ctl", "save"], b"PRESS")
    assert fake.calls == []
    assert "preset_slots" in w.last_error["message"] or "select" in w.last_error["message"]


def test_save_reserved_never_reaches_camera(worker, monkeypatch):
    # third protection level: even a hacked whitelist cannot push a reserved
    # slot past ptz.check_slot inside save_preset
    w, fake, _ = worker
    monkeypatch.setattr(w.cfg, "preset_slots", [100])
    w.selected_slot = 100
    w.handle(["preset_ctl", "save"], b"PRESS")
    assert ("SetPreset", 100) not in fake.calls
    assert w.last_error is not None


def test_save_dvrip_error_reports_last_error(worker):
    w, fake, published = worker
    fake.fail_on = "SetPreset"
    w.selected_slot = 3
    w.handle(["preset_ctl", "save"], b"PRESS")
    assert w.last_error["kind"] == "save"
    assert w.last_action is None
    assert w.occupied == {1, 2}              # entities do not lie about success
    assert "homeassistant/button/cam/preset_3/config" not in _configs(published)


def test_save_same_slot_does_not_republish_discovery(worker):
    w, fake, published = worker
    w.selected_slot = 2                      # already occupied
    w.handle(["preset_ctl", "save"], b"PRESS")
    assert ("SetPreset", 2) in fake.calls
    assert _configs(published) == []         # no discovery churn (risk 11.5)
    assert _state(published, "xiongmai/cam/preset_ctl/slots/state") is not None


# -- delete ------------------------------------------------------------------


def test_delete_routing_removes_button(worker):
    w, fake, published = worker
    w.selected_slot = 2
    assert w.handle(["preset_ctl", "delete"], b"PRESS") is True
    assert ("ClearPreset", 2) in fake.calls
    assert w.occupied == {1}
    assert w.last_action["kind"] == "delete" and w.last_action["result"] == "ok"
    # empty retained payload on the EXACT discovery topic of the removed button
    assert ("homeassistant/button/cam/preset_2/config", "", True) in published
    cleared = [t for t, p, r in published if p == ""]
    assert cleared == ["homeassistant/button/cam/preset_2/config"]


def test_delete_config_preset_keeps_button(worker):
    w, fake, published = worker
    w.selected_slot = 1                      # slot 1 is defined in cameras.yml
    w.handle(["preset_ctl", "delete"], b"PRESS")
    assert ("ClearPreset", 1) in fake.calls
    assert w.occupied == {2}
    assert not any(p == "" for t, p, r in published)   # config button stays
    assert w.last_action["result"] == "ok"


def test_delete_empty_slot_is_idempotent(worker):
    w, fake, _ = worker
    w.selected_slot = 4                      # not occupied
    w.handle(["preset_ctl", "delete"], b"PRESS")
    assert ("ClearPreset", 4) in fake.calls
    assert w.last_action["result"] == "ok" and w.last_error is None


def test_delete_reserved_never_reaches_camera(worker, monkeypatch):
    w, fake, _ = worker
    monkeypatch.setattr(w.cfg, "preset_slots", [250])
    w.selected_slot = 250
    w.handle(["preset_ctl", "delete"], b"PRESS")
    assert ("ClearPreset", 250) not in fake.calls
    assert w.last_error is not None


# -- mode and bridge-level routing -------------------------------------------


def test_preset_ctl_disabled_in_legacy_mode(monkeypatch):
    w, fake, _ = _worker(monkeypatch, auto_presets=False)
    assert w.handle(["preset_ctl", "save"], b"PRESS") is True
    assert fake.calls == []


def test_bridge_subscribes_and_restores_select(monkeypatch):
    fake_client = FakeClient()
    monkeypatch.setattr(bridge_mod.mqtt, "Client", lambda *a, **k: fake_client)
    cfg = CameraConfig(name="cam", host="h", password="x").validate()
    b = Bridge([cfg])
    b.on_connect(fake_client, None, None, 0)
    assert "xiongmai/+/preset_ctl/slot/state" in fake_client.subscriptions
    msg = types.SimpleNamespace(topic="xiongmai/cam/preset_ctl/slot/state", payload=b"3")
    b.on_message(fake_client, None, msg)
    assert b.workers["cam"].selected_slot == 3
    # a normal command still routes through the same callback
    cmd = types.SimpleNamespace(topic="xiongmai/cam/preset_ctl/slot/set", payload=b"2")
    b.on_message(fake_client, None, cmd)
    assert b.workers["cam"].selected_slot == 2