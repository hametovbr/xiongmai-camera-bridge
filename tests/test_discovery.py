"""Discovery payload shape tests: the parts Home Assistant rejects silently."""

import json

import pytest

from xiongmai_bridge.bridge import Bridge, CameraWorker
from xiongmai_bridge.config import CameraConfig


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


@pytest.fixture()
def worker():
    cfg = CameraConfig(name="cam", host="10.0.0.1", password="x",
                       device_name="Cam", area="Hall", presets={1: "Bed"}).validate()
    published = []
    w = CameraWorker(cfg, lambda topic, payload, retain=False: published.append((topic, payload)))
    w.features = {"fvideo": True, "osd_title": True, "osd_time": True,
                  "volume": True, "white_light": True, "slots": []}
    w.publish_discovery()
    return w, published


def _payloads(published):
    return {topic: json.loads(payload) for topic, payload in published if topic.endswith("/config")}


def test_availability_is_a_list_of_mappings(worker):
    """HA rejects `availability: ["topic"]` with 'expected a mapping'; entities are not created."""
    _, published = worker
    configs = _payloads(published)
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


def test_device_block_and_entities(worker):
    _, published = worker
    configs = _payloads(published)
    kinds = sorted(topic.split("/")[1] for topic in configs)
    assert kinds == ["button"] * 6 + ["number"] * 2 + ["switch"] * 2
    for topic, payload in configs.items():
        device = payload["device"]
        assert device["identifiers"] == ["xiongmai_cam"]
        assert device["suggested_area"] == "Hall"
        assert payload["unique_id"].startswith("xiongmai_cam_")


def test_no_entities_when_capabilities_are_missing():
    cfg = CameraConfig(name="plain", host="10.0.0.2", password="x", presets={}).validate()
    published = []
    w = CameraWorker(cfg, lambda topic, payload, retain=False: published.append((topic, payload)))
    w.features = {"fvideo": False, "osd_title": False, "osd_time": False,
                  "volume": False, "white_light": False, "slots": []}
    w.publish_discovery()
    configs = _payloads(published)
    # only the PTZ buttons and the home button, no switches or numbers
    assert {topic.split("/")[1] for topic in configs} == {"button"}
