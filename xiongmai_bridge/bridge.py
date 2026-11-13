"""DVRIP -> MQTT bridge with Home Assistant discovery, for one or many cameras.

Xiongmai cameras have no ONVIF and no HTTP API, so Home Assistant cannot reach
their PTZ, on-screen display, speaker volume or white light. This bridge talks
DVRIP (TCP 34567) and exposes what each camera really supports as MQTT entities.

Everything below one device is per camera; several cameras run inside one
process, each with its own poll thread, its own command lock and its own
availability topic. A slow preset move on one camera therefore does not block
commands to the others, and a camera that is offline only takes itself offline.

Exposed entities (per camera):

    button  PTZ left / right / up / down     bounded move, then explicit stop
    button  PTZ home                         Reset (firmware re-centres the head)
    button  preset N                         goto preset with backlash compensation
    switch  OSD channel title                fVideo.GUISet.ChannelTitleEnable
    switch  OSD time                         fVideo.GUISet.TimeTitleEnable
    number  speaker volume                   fVideo.Volume[*].LeftVolume/RightVolume
    number  white light brightness           Camera.WhiteLight.Brightness

Entities are published only when the camera reports the section: a model without
a speaker or white light simply does not get those entities.

Every write is read back before the state is published: if the camera did not
confirm the value, Home Assistant keeps the old one instead of a false success.

Configuration is described in docs/configuration.md; the short version is
CAMERAS_FILE (YAML/JSON list), CAMERAS (JSON list) or the single-camera
CAM_HOST/CAM_PASS variables.
"""

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

from .config import load as load_cameras
from .dvrip import Dvrip, DvripError
from .ptz import DIRECTIONS, goto_preset, move

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xiongmai-bridge")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")

# One availability topic for the whole process: lets Home Assistant show every
# camera as unavailable when the bridge itself dies (last will, retained).
BRIDGE_AVAILABILITY = os.environ.get("BRIDGE_AVAILABILITY",
                                     "xiongmai/bridge/availability")

DIRECTION_BUTTONS = (
    ("DirectionLeft", "PTZ left", "mdi:arrow-left-bold"),
    ("DirectionRight", "PTZ right", "mdi:arrow-right-bold"),
    ("DirectionUp", "PTZ up", "mdi:arrow-up-bold"),
    ("DirectionDown", "PTZ down", "mdi:arrow-down-bold"),
)


class CameraWorker:
    """One camera: its own poll thread, command lock, state and discovery."""

    def __init__(self, cfg, publisher):
        self.cfg = cfg
        self.publish = publisher              # callable(topic, payload, retain)
        self.lock = threading.Lock()          # serialises commands to this camera
        self.state = {}
        self.features = None                  # filled by probe()
        self.discovered = False

    # -- connection helpers

    def session(self):
        return Dvrip(self.cfg.host, self.cfg.user, self.cfg.password, port=self.cfg.port)

    def call(self, fn):
        """Run fn(cam) on a fresh session; the camera drops idle sessions."""
        cam = self.session()
        try:
            return fn(cam)
        finally:
            cam.close()

    # -- feature detection

    def probe(self):
        """Ask the camera which sections exist; missing sections drop entities."""
        def work(cam):
            feats = {"fvideo": False, "osd_title": False, "osd_time": False,
                     "volume": False, "white_light": False, "slots": []}
            try:
                fvideo = cam.get_config("fVideo") or {}
                feats["fvideo"] = True
                gui = fvideo.get("GUISet") or {}
                feats["osd_title"] = "ChannelTitleEnable" in gui
                feats["osd_time"] = "TimeTitleEnable" in gui
                feats["volume"] = bool(fvideo.get("Volume") or fvideo.get("VolumeIn"))
            except DvripError as exc:
                log.info("%s: no fVideo section (%s)", self.cfg.name, exc)
            try:
                camera = cam.get_config("Camera") or {}
                feats["white_light"] = bool(camera.get("WhiteLight"))
            except DvripError as exc:
                log.info("%s: no Camera section (%s)", self.cfg.name, exc)
            try:
                uart = cam.get_config("Uart") or {}
                groups = uart.get("PTZPreset") or [[]]
                feats["slots"] = [p.get("Id") for p in groups[0]]
            except DvripError:
                pass
            return feats

        feats = self.call(work)
        # explicit switches in the config win over detection
        for key in ("osd", "volume", "white_light"):
            if self.cfg.__dict__.get(key) is not None:
                enabled = bool(self.cfg.__dict__[key])
                if key == "osd":
                    feats["osd_title"] = feats["osd_time"] = enabled
                else:
                    feats[key] = enabled
        self.features = feats
        log.info("%s: features %s", self.cfg.name,
                 {k: v for k, v in feats.items() if k != "slots"})
        return feats

    @property
    def has_ptz(self):
        return self.cfg.ptz is not False

    # -- state

    def read_state(self):
        feats = self.features or {}

        def work(cam):
            state = {}
            if feats.get("fvideo"):
                fvideo = cam.get_config("fVideo") or {}
                gui = fvideo.get("GUISet") or {}
                if feats.get("osd_title"):
                    state["osd_title"] = bool(gui.get("ChannelTitleEnable"))
                if feats.get("osd_time"):
                    state["osd_time"] = bool(gui.get("TimeTitleEnable"))
                if feats.get("volume"):
                    items = fvideo.get("Volume") or fvideo.get("VolumeIn") or [{}]
                    state["volume"] = int(items[0].get("LeftVolume", 0))
            if feats.get("white_light"):
                camera = cam.get_config("Camera") or {}
                light = camera.get("WhiteLight") or {}
                state["white_light"] = int(light.get("Brightness", 0))
                state["white_light_mode"] = light.get("WorkMode", "?")
            return state

        return self.call(work)

    def publish_state(self, state):
        for key in ("osd_title", "osd_time"):
            if key in state:
                self.publish(f"{self.cfg.base}/{key}/state",
                             "ON" if state[key] else "OFF", True)
        for key in ("volume", "white_light"):
            if key in state:
                self.publish(f"{self.cfg.base}/{key}/state", str(state[key]), True)
        self.publish(f"{self.cfg.base}/state", json.dumps(state), True)
        self.publish(f"{self.cfg.base}/availability", "online", True)

    # -- discovery

    def publish_discovery(self):
        cfg = self.cfg
        device = {
            "identifiers": [f"xiongmai_{cfg.name}"],
            "name": cfg.device_name or cfg.name,
            "manufacturer": "Xiongmai",
            "model": cfg.model,
            "configuration_url": f"http://{cfg.host}",
        }
        if cfg.area:
            device["suggested_area"] = cfg.area
        availability = {
            "availability": [f"{cfg.base}/availability", BRIDGE_AVAILABILITY],
            "availability_mode": "all",
            "payload_available": "online",
            "payload_not_available": "offline",
        }

        def disco(component, object_id, payload):
            topic = f"{DISCOVERY_PREFIX}/{component}/{cfg.name}/{object_id}/config"
            self.publish(topic, json.dumps({**payload, "device": device, **availability}), True)

        if self.has_ptz:
            for command, name, icon in DIRECTION_BUTTONS:
                disco("button", f"ptz_{command.lower()}", {
                    "name": cfg.label(f"ptz_{command.lower()}", name),
                    "unique_id": f"xiongmai_{cfg.name}_ptz_{command.lower()}",
                    "command_topic": f"{cfg.base}/ptz/{command}/set",
                    "payload_press": "PRESS", "icon": icon})
            disco("button", "ptz_home", {
                "name": cfg.label("ptz_home", "PTZ home"),
                "unique_id": f"xiongmai_{cfg.name}_ptz_home",
                "command_topic": f"{cfg.base}/ptz/Reset/set",
                "payload_press": "PRESS", "icon": "mdi:home-import-outline"})
            for slot, preset_label in sorted(cfg.presets.items()):
                disco("button", f"preset_{slot}", {
                    "name": f"{cfg.label('preset_prefix', 'Preset')} {preset_label}",
                    "unique_id": f"xiongmai_{cfg.name}_preset_{slot}",
                    "command_topic": f"{cfg.base}/preset/{slot}/set",
                    "payload_press": "PRESS", "icon": "mdi:bookmark"})

        feats = self.features or {}
        for key, name, icon in (("osd_title", "OSD channel title", "mdi:format-text"),
                                ("osd_time", "OSD time", "mdi:clock-outline")):
            if feats.get(key):
                disco("switch", key, {
                    "name": cfg.label(key, name),
                    "unique_id": f"xiongmai_{cfg.name}_{key}",
                    "command_topic": f"{cfg.base}/{key}/set",
                    "state_topic": f"{cfg.base}/{key}/state",
                    "payload_on": "ON", "payload_off": "OFF", "icon": icon})

        if feats.get("volume"):
            disco("number", "volume", {
                "name": cfg.label("volume", "Speaker volume"),
                "unique_id": f"xiongmai_{cfg.name}_volume",
                "command_topic": f"{cfg.base}/volume/set",
                "state_topic": f"{cfg.base}/volume/state",
                "min": 0, "max": 100, "step": 1, "mode": "slider",
                "icon": "mdi:volume-high", "unit_of_measurement": "%"})

        if feats.get("white_light"):
            disco("number", "white_light", {
                "name": cfg.label("white_light", "White light brightness"),
                "unique_id": f"xiongmai_{cfg.name}_white_light",
                "command_topic": f"{cfg.base}/white_light/set",
                "state_topic": f"{cfg.base}/white_light/state",
                "min": 0, "max": 100, "step": 1, "mode": "slider",
                "icon": "mdi:light-flood-down", "unit_of_measurement": "%"})

        self.publish(f"{cfg.base}/state", json.dumps(self.state), True)
        self.discovered = True

    # -- writes

    def _set_osd(self, cam, field, value):
        fvideo = cam.get_config("fVideo") or {}
        fvideo.setdefault("GUISet", {})[field] = bool(value)
        cam.set_config("fVideo", fvideo)
        return self.read_state()      # read back: publish only confirmed values

    def _set_volume(self, cam, value):
        fvideo = cam.get_config("fVideo") or {}
        for key in ("Volume", "VolumeIn"):
            for item in fvideo.get(key) or []:
                item["LeftVolume"] = item["RightVolume"] = int(value)
        cam.set_config("fVideo", fvideo)
        return self.read_state()

    def _set_white_light(self, cam, value):
        camera = cam.get_config("Camera") or {}
        camera.setdefault("WhiteLight", {})["Brightness"] = int(value)
        cam.set_config("Camera", camera)
        return self.read_state()

    # -- commands

    def handle(self, parts, payload):
        """Handle one <camera>/... command; returns True when it was understood."""
        if not parts:
            return False
        with self.lock:
            if parts[0] == "ptz" and len(parts) == 2:
                command = parts[1]
                if not self.has_ptz:
                    log.warning("%s: PTZ disabled by config", self.cfg.name)
                    return True
                if command == "Reset":
                    self.call(lambda cam: cam.ptz("Reset"))
                elif command in DIRECTIONS:
                    # bounded impulse: the camera has no separate stop command
                    self.call(lambda cam: move(cam, command, self.cfg.ptz_impulse))
                else:
                    log.warning("%s: unknown direction %s", self.cfg.name, command)
                return True
            if parts[0] == "preset" and len(parts) == 2:
                self.call(lambda cam: goto_preset(cam, int(parts[1]),
                                                  approach=self.cfg.approach or None,
                                                  settle=self.cfg.preset_settle,
                                                  nudge=self.cfg.preset_nudge))
                return True
            if parts[0] == "osd_title" and len(parts) == 1:
                self.publish_state(self.call(lambda cam: self._set_osd(
                    cam, "ChannelTitleEnable", payload.decode() == "ON")))
                return True
            if parts[0] == "osd_time" and len(parts) == 1:
                self.publish_state(self.call(lambda cam: self._set_osd(
                    cam, "TimeTitleEnable", payload.decode() == "ON")))
                return True
            if parts[0] == "volume" and len(parts) == 1:
                self.publish_state(self.call(lambda cam: self._set_volume(
                    cam, float(payload))))
                return True
            if parts[0] == "white_light" and len(parts) == 1:
                self.publish_state(self.call(lambda cam: self._set_white_light(
                    cam, float(payload))))
                return True
        return False

    # -- loop

    def run(self):
        while True:
            try:
                if not self.discovered:
                    self.probe()
                    self.publish_discovery()
                state = self.read_state()
                self.state = state
                self.publish_state(state)
            except Exception as exc:                      # noqa: BLE001
                log.warning("%s: poll failed: %s", self.cfg.name, exc)
                self.publish(f"{self.cfg.base}/availability", "offline", True)
            time.sleep(self.cfg.poll_seconds)


class Bridge:
    def __init__(self, cameras):
        self.cameras = cameras
        self.workers = {cfg.name: CameraWorker(cfg, self.publish) for cfg in cameras}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="xiongmai-bridge")
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.will_set(BRIDGE_AVAILABILITY, "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def publish(self, topic, payload, retain=False):
        self.client.publish(topic, payload, retain=retain)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected: %s (%d camera(s))", reason_code, len(self.workers))
        self.publish(BRIDGE_AVAILABILITY, "online", True)
        # <camera>/<key>/set and <camera>/<group>/<value>/set
        client.subscribe("xiongmai/+/+/set")
        client.subscribe("xiongmai/+/+/+/set")
        for worker in self.workers.values():
            if worker.discovered:
                worker.publish_discovery()

    def on_message(self, client, userdata, msg):
        rest = msg.topic.split("/")[1:]        # drop the leading "xiongmai"
        if len(rest) < 3 or rest[-1] != "set":
            return
        name, parts = rest[0], rest[1:-1]
        worker = self.workers.get(name)
        if worker is None:
            log.warning("unknown camera in topic %s", msg.topic)
            return
        log.info("%s: cmd %s -> %s", name, msg.topic, msg.payload)
        try:
            if not worker.handle(parts, msg.payload):
                log.warning("%s: unknown topic %s", name, msg.topic)
        except Exception as exc:                          # noqa: BLE001
            log.error("%s: command failed: %s", name, exc)
            self.publish(f"{worker.cfg.base}/availability", "offline", True)

    def run(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()
        for worker in self.workers.values():
            threading.Thread(target=worker.run, daemon=True,
                             name=f"cam-{worker.cfg.name}").start()
        while True:
            time.sleep(3600)


def main():
    cameras = load_cameras()
    log.info("configured cameras: %s", ", ".join(c.name for c in cameras))
    Bridge(cameras).run()


if __name__ == "__main__":
    main()
