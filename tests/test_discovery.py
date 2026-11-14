"""Discovery payload shape tests: the parts Home Assistant rejects silently."""

import json

import pytest

from xiongmai_bridge.bridge import Bridge, CameraWorker
from xiongmai_bridge.config import CameraConfig
from xiongmai_bridge.presets import valid_slots


class FakeClient:
    """Records publishes instead of talking to a broker."""

    def __init__(self):
        self.messages = []
        self.subscriptions = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.messages.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def will_set(self, *args, **kwargs):
        pass

    def username_pw_set(self, *args, **kwargs):
        pass

    def on_connect(self, *args, **kwargs):  # set by Bridge.__init__
        pass

    def on_message(self, *args, **kwargs):  # set by Bridge.__init__
        pass


def _worker(auto_presets=True, presets=None, slots=(), labels=None, ptz=None):
    cfg = CameraConfig(name="cam", host="10.0.0.1", password="x",
                       device_name="Cam", area="Hall",
                       presets={1: "Bed"} if presets is None else presets,
                       labels=labels or {}, auto_presets=auto_presets,
                       ptz=ptz).validate()
    published = []
    w = CameraWorker(cfg, lambda topic, payload, retain=False:
                     published.append((topic, payload, retain)))
    w.features = {"fvideo": True, "osd_title": True, "osd_time": True,
                  "volume": True, "white_light": True, "slots": list(slots)}
    w.occupied = valid_slots(slots)
    w.publish_discovery()
    return w, published


@pytest.fixture()
def worker():
    # legacy mode (auto_presets=False): the exact 0.2.2 entity set
    return _worker(auto_presets=False)


@pytest.fixture()
def auto_worker():
    return _worker(slots=[1, 2, 3, 100],
                   labels={"preset_prefix": "Пресет", "preset_auto_prefix": "Пресет"})


def _payloads(published):
    return {topic: json.loads(payload) for topic, payload, _ in published
            if topic.endswith("/config") and payload}


def _assert_availability(configs):
    assert configs, "no discovery payloads published"
    for topic, payload in configs.items():
        availability = payload["availability"]
        assert isinstance(availability, list) and availability, topic
        for entry in availability:
            assert isinstance(entry, dict), f"{topic}: availability entry must be a mapping, got {entry!r}"
            assert entry["topic"], f"{topic}: availability entry needs a topic"
            assert entry["payload_available"] == "online"
            assert entry["payload_not_available"] == "offline"
        assert payload["availability_mode"] == "all"
        # the legacy single-topic form must not be used together with the list
        assert "availability_topic" not in payload


def test_availability_is_a_list_of_mappings(worker):
    """HA rejects `availability: ["topic"]` with 'expected a mapping'; entities are not created."""
    _, published = worker
    _assert_availability(_payloads(published))


def test_availability_on_new_entities(auto_worker):
    """The 0.2.2 regression applies to select/save/delete/sensor as well."""
    _, published = auto_worker
    configs = _payloads(published)
    assert "homeassistant/select/cam/preset_slot/config" in configs
    assert "homeassistant/sensor/cam/preset_slots/config" in configs
    _assert_availability(configs)


def test_device_block_and_entities(worker):
    """Legacy mode keeps the exact 0.2.2 set: 6 buttons, 2 numbers, 2 switches."""
    _, published = worker
    configs = _payloads(published)
    kinds = sorted(topic.split("/")[1] for topic in configs)
    assert kinds == ["button"] * 6 + ["number"] * 2 + ["switch"] * 2
    for topic, payload in configs.items():
        device = payload["device"]
        assert device["identifiers"] == ["xiongmai_cam"]
        assert device["suggested_area"] == "Hall"
        assert payload["unique_id"].startswith("xiongmai_cam_")


def test_legacy_mode_has_no_preset_ctl(worker):
    w, published = worker
    assert not any("preset_ctl" in topic for topic, _, _ in published)
    configs = _payloads(published)
    assert not any(t.startswith(("homeassistant/select/", "homeassistant/sensor/"))
                   for t in configs)
    assert w.published_slots == {1}


def test_default_mode_entity_set(auto_worker):
    _, published = auto_worker
    configs = _payloads(published)
    kinds = sorted(topic.split("/")[1] for topic in configs)
    # 4 directions + home + 3 preset buttons (1,2,3; 100 filtered) + save + delete
    assert kinds == (["button"] * 10 + ["number"] * 2 + ["select"]
                     + ["sensor"] + ["switch"] * 2)


def test_preset_button_names_and_ids(auto_worker):
    _, published = auto_worker
    configs = _payloads(published)
    bed = configs["homeassistant/button/cam/preset_1/config"]
    assert bed["name"] == "Пресет Bed"                       # config label keeps 0.2.2 form
    assert bed["unique_id"] == "xiongmai_cam_preset_1"
    assert bed["command_topic"] == "xiongmai/cam/preset/1/set"
    auto = configs["homeassistant/button/cam/preset_3/config"]
    assert auto["name"] == "Пресет 3"                        # auto slot, no double prefix
    assert auto["unique_id"] == "xiongmai_cam_preset_3"
    assert auto["command_topic"] == "xiongmai/cam/preset/3/set"
    assert "homeassistant/button/cam/preset_100/config" not in configs  # reserved


def test_preset_save_payload(auto_worker):
    _, published = auto_worker
    p = _payloads(published)["homeassistant/button/cam/preset_save/config"]
    assert p["name"] == "Save preset"
    assert p["unique_id"] == "xiongmai_cam_preset_save"
    assert p["command_topic"] == "xiongmai/cam/preset_ctl/save/set"
    assert p["payload_press"] == "PRESS"
    assert p["entity_category"] == "config"
    assert p["icon"] == "mdi:content-save"


def test_preset_delete_payload(auto_worker):
    _, published = auto_worker
    p = _payloads(published)["homeassistant/button/cam/preset_delete/config"]
    assert p["name"] == "Delete preset"
    assert p["unique_id"] == "xiongmai_cam_preset_delete"
    assert p["command_topic"] == "xiongmai/cam/preset_ctl/delete/set"
    assert p["payload_press"] == "PRESS"
    assert p["entity_category"] == "config"
    assert p["icon"] == "mdi:bookmark-remove"


def test_select_payload(auto_worker):
    _, published = auto_worker
    p = _payloads(published)["homeassistant/select/cam/preset_slot/config"]
    assert p["name"] == "Preset slot"
    assert p["unique_id"] == "xiongmai_cam_preset_slot"
    assert p["command_topic"] == "xiongmai/cam/preset_ctl/slot/set"
    assert p["state_topic"] == "xiongmai/cam/preset_ctl/slot/state"
    assert p["options"] == ["1", "2", "3", "4"]              # managed ∪ occupied, strings
    assert p["entity_category"] == "config"
    assert p["icon"] == "mdi:bookmark-box-multiple"


def test_sensor_payload(auto_worker):
    _, published = auto_worker
    p = _payloads(published)["homeassistant/sensor/cam/preset_slots/config"]
    assert p["name"] == "Preset slots"
    assert p["unique_id"] == "xiongmai_cam_preset_slots"
    assert p["state_topic"] == "xiongmai/cam/preset_ctl/slots/state"
    assert p["value_template"] == "{{ value_json.count }}"
    assert p["json_attributes_topic"] == "xiongmai/cam/preset_ctl/slots/state"
    assert p["json_attributes"] == ["slots", "free", "managed", "reserved",
                                    "last_action", "last_error"]
    assert p["icon"] == "mdi:bookmark-multiple"


def test_slots_state_published_on_discovery(auto_worker):
    _, published = auto_worker
    states = {topic: (payload, retain) for topic, payload, retain in published
              if topic == "xiongmai/cam/preset_ctl/slots/state"}
    assert states, "slots/state not published"
    payload, retain = states["xiongmai/cam/preset_ctl/slots/state"]
    assert retain is True
    state = json.loads(payload)
    assert state["count"] == 3 and state["free"] == [4]


def test_no_entities_when_capabilities_are_missing():
    cfg = CameraConfig(name="plain", host="10.0.0.2", password="x", presets={},
                       auto_presets=False).validate()
    published = []
    w = CameraWorker(cfg, lambda topic, payload, retain=False:
                     published.append((topic, payload, retain)))
    w.features = {"fvideo": False, "osd_title": False, "osd_time": False,
                  "volume": False, "white_light": False, "slots": []}
    w.publish_discovery()
    configs = _payloads(published)
    # only the PTZ buttons and the home button, no switches or numbers
    assert {topic.split("/")[1] for topic in configs} == {"button"}


def test_no_ptz_means_no_preset_entities():
    _, published = _worker(ptz=False, slots=[1, 2])
    configs = _payloads(published)
    assert {topic.split("/")[1] for topic in configs} == {"number", "switch"}
    assert not any("preset" in topic for topic in configs)