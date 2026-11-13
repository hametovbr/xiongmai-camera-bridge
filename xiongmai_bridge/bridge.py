"""DVRIP -> MQTT bridge with Home Assistant discovery.

Xiongmai cameras have no ONVIF and no HTTP API, so Home Assistant cannot reach
their PTZ, on-screen display, speaker volume or white light. This bridge talks
DVRIP (TCP 34567) and exposes what the camera really supports as MQTT entities.

Exposed entities (all under one HA device):

    button  PTZ left / right / up / down     bounded move, then explicit stop
    button  PTZ home                         Reset (firmware re-centres the head)
    button  preset N                         goto preset with backlash compensation
    switch  OSD channel title                fVideo.GUISet.ChannelTitleEnable
    switch  OSD time                         fVideo.GUISet.TimeTitleEnable
    number  speaker volume                   fVideo.Volume[*].LeftVolume/RightVolume
    number  white light brightness           Camera.WhiteLight.Brightness

Every write is read back before the state is published: if the camera did not
confirm the value, Home Assistant sees the old value instead of a false success.

Environment (see env.example):

    CAM_HOST, CAM_PORT, CAM_USER, CAM_PASS   camera connection (password required)
    CAM_NAME, DEVICE_NAME                    topic/entity id and HA device name
    PRESETS                                  JSON map slot -> label
    PTZ_IMPULSE                              seconds per direction button press
    PTZ_APPROACH                             direction used to load the gears
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS
    POLL_SECONDS                             state refresh interval
"""

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

from .dvrip import Dvrip
from .ptz import goto_preset, move

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xiongmai-bridge")

CAM_HOST = os.environ.get("CAM_HOST", "192.168.1.10")
CAM_PORT = int(os.environ.get("CAM_PORT", "34567"))
CAM_USER = os.environ.get("CAM_USER", "admin")
CAM_PASS = os.environ["CAM_PASS"]

CAM_NAME = os.environ.get("CAM_NAME", "xiongmai")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Xiongmai camera")
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "Xiongmai IP camera")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None

DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
BASE = f"xiongmai/{CAM_NAME}"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))

PRESETS = json.loads(os.environ.get("PRESETS", '{"1": "Preset 1"}'))
# Optional per-entity name overrides, e.g. {"ptz_home": "PTZ домой", "volume": "Громкость"}.
# Keys: ptz_home, osd_title, osd_time, volume, white_light, ptz_directionleft,
# ptz_directionright, ptz_directionup, ptz_directiondown.
LABELS = json.loads(os.environ.get("LABELS", "{}"))


def label(key, default):
    return LABELS.get(key, default)

PTZ_IMPULSE = float(os.environ.get("PTZ_IMPULSE", "0.8"))
# Empty string disables backlash compensation (single GotoPreset, less accurate).
PTZ_APPROACH = os.environ.get("PTZ_APPROACH", "DirectionRightDown")
PRESET_SETTLE = float(os.environ.get("PRESET_SETTLE", "9"))
PRESET_NUDGE = float(os.environ.get("PRESET_NUDGE", "0.4"))

DIRECTION_BUTTONS = (
    ("DirectionLeft", "PTZ left", "mdi:arrow-left-bold"),
    ("DirectionRight", "PTZ right", "mdi:arrow-right-bold"),
    ("DirectionUp", "PTZ up", "mdi:arrow-up-bold"),
    ("DirectionDown", "PTZ down", "mdi:arrow-down-bold"),
)


def with_cam(fn):
    """Run one operation on a fresh session: the camera drops idle sessions."""
    cam = Dvrip(CAM_HOST, CAM_USER, CAM_PASS, port=CAM_PORT)
    try:
        return fn(cam)
    finally:
        cam.close()


# ---------------------------------------------------------------- state

def read_state(cam):
    fvideo = cam.get_config("fVideo")
    camera = cam.get_config("Camera")
    gui = fvideo.get("GUISet") or {}
    volume = (fvideo.get("Volume") or [{}])[0].get("LeftVolume", 0)
    light = camera.get("WhiteLight") or {}
    return {
        "osd_title": bool(gui.get("ChannelTitleEnable")),
        "osd_time": bool(gui.get("TimeTitleEnable")),
        "volume": int(volume),
        "white_light": int(light.get("Brightness", 0)),
        "white_light_mode": light.get("WorkMode", "?"),
    }


def set_osd(cam, field, value):
    fvideo = cam.get_config("fVideo")
    fvideo.setdefault("GUISet", {})[field] = bool(value)
    cam.set_config("fVideo", fvideo)
    return read_state(cam)          # read back: publish only confirmed values


def set_volume(cam, value):
    fvideo = cam.get_config("fVideo")
    for key in ("Volume", "VolumeIn"):
        for item in fvideo.get(key) or []:
            item["LeftVolume"] = item["RightVolume"] = int(value)
    cam.set_config("fVideo", fvideo)
    return read_state(cam)


def set_white_light(cam, value):
    camera = cam.get_config("Camera")
    camera.setdefault("WhiteLight", {})["Brightness"] = int(value)
    cam.set_config("Camera", camera)
    return read_state(cam)


# ---------------------------------------------------------------- bridge

class Bridge:
    def __init__(self):
        self.state = {"osd_title": True, "osd_time": True,
                      "volume": 0, "white_light": 0, "white_light_mode": "?"}
        self.lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"xiongmai-{CAM_NAME}")
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.will_set(f"{BASE}/availability", "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def device(self):
        return {"identifiers": [f"xiongmai_{CAM_NAME}"], "name": DEVICE_NAME,
                "manufacturer": "Xiongmai", "model": DEVICE_MODEL,
                "configuration_url": f"http://{CAM_HOST}"}

    def publish_discovery(self):
        device = self.device()
        avail = {"availability_topic": f"{BASE}/availability",
                 "payload_available": "online", "payload_not_available": "offline"}

        def disco(component, object_id, payload):
            topic = f"{DISCOVERY_PREFIX}/{component}/{CAM_NAME}/{object_id}/config"
            self.client.publish(topic, json.dumps({**payload, "device": device, **avail}),
                                retain=True)

        for command, name, icon in DIRECTION_BUTTONS:
            disco("button", f"ptz_{command.lower()}", {
                "name": label(f"ptz_{command.lower()}", name),
                "unique_id": f"xiongmai_{CAM_NAME}_ptz_{command.lower()}",
                "command_topic": f"{BASE}/ptz/{command}/set", "payload_press": "PRESS",
                "icon": icon})

        for slot, label_preset in PRESETS.items():
            disco("button", f"preset_{slot}", {
                "name": f"{label('preset_prefix', 'Preset')} {label_preset}",
                "icon": "mdi:bookmark",
                "unique_id": f"xiongmai_{CAM_NAME}_preset_{slot}",
                "command_topic": f"{BASE}/preset/{slot}/set", "payload_press": "PRESS"})

        disco("button", "ptz_home", {
            "name": label("ptz_home", "PTZ home"),
            "unique_id": f"xiongmai_{CAM_NAME}_ptz_home",
            "command_topic": f"{BASE}/ptz/Reset/set", "payload_press": "PRESS",
            "icon": "mdi:home-import-outline"})

        for key, name, icon in (("osd_title", "OSD channel title", "mdi:format-text"),
                                ("osd_time", "OSD time", "mdi:clock-outline")):
            disco("switch", key, {
                "name": label(key, name), "unique_id": f"xiongmai_{CAM_NAME}_{key}",
                "command_topic": f"{BASE}/{key}/set", "state_topic": f"{BASE}/{key}/state",
                "payload_on": "ON", "payload_off": "OFF", "icon": icon})

        disco("number", "volume", {
            "name": label("volume", "Speaker volume"),
            "unique_id": f"xiongmai_{CAM_NAME}_volume",
            "command_topic": f"{BASE}/volume/set", "state_topic": f"{BASE}/volume/state",
            "min": 0, "max": 100, "step": 1, "mode": "slider",
            "icon": "mdi:volume-high", "unit_of_measurement": "%"})

        disco("number", "white_light", {
            "name": label("white_light", "White light brightness"),
            "unique_id": f"xiongmai_{CAM_NAME}_white_light",
            "command_topic": f"{BASE}/white_light/set",
            "state_topic": f"{BASE}/white_light/state",
            "min": 0, "max": 100, "step": 1, "mode": "slider",
            "icon": "mdi:light-flood-down", "unit_of_measurement": "%"})

        self.client.publish(f"{BASE}/state", json.dumps(self.state), retain=True)

    def publish_state(self, state):
        self.client.publish(f"{BASE}/osd_title/state",
                            "ON" if state["osd_title"] else "OFF", retain=True)
        self.client.publish(f"{BASE}/osd_time/state",
                            "ON" if state["osd_time"] else "OFF", retain=True)
        self.client.publish(f"{BASE}/volume/state", str(state["volume"]), retain=True)
        self.client.publish(f"{BASE}/white_light/state", str(state["white_light"]),
                            retain=True)
        self.client.publish(f"{BASE}/state", json.dumps(state), retain=True)
        self.client.publish(f"{BASE}/availability", "online", retain=True)

    # -- handlers

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected: %s", reason_code)
        client.subscribe(f"{BASE}/+/+/set")
        client.subscribe(f"{BASE}/+/set")
        self.publish_discovery()

    def on_message(self, client, userdata, msg):
        rest = msg.topic[len(BASE) + 1:].split("/")
        if not rest or rest[-1] != "set":
            return
        parts = rest[:-1]
        log.info("cmd %s -> %s", msg.topic, msg.payload)
        try:
            with self.lock:
                if parts[0] == "ptz" and len(parts) == 2:
                    command = parts[1]
                    if command in ("Reset",):
                        with_cam(lambda c: c.ptz(command))
                    else:
                        # bounded move: the camera has no separate stop command
                        with_cam(lambda c: move(c, command, PTZ_IMPULSE))
                elif parts[0] == "preset" and len(parts) == 2:
                    with_cam(lambda c: goto_preset(c, int(parts[1]),
                                                   approach=PTZ_APPROACH or None,
                                                   settle=PRESET_SETTLE,
                                                   nudge=PRESET_NUDGE))
                elif parts[0] == "osd_title":
                    state = with_cam(lambda c: set_osd(
                        c, "ChannelTitleEnable", msg.payload.decode() == "ON"))
                    self.publish_state(state)
                elif parts[0] == "osd_time":
                    state = with_cam(lambda c: set_osd(
                        c, "TimeTitleEnable", msg.payload.decode() == "ON"))
                    self.publish_state(state)
                elif parts[0] == "volume":
                    state = with_cam(lambda c: set_volume(c, float(msg.payload)))
                    self.publish_state(state)
                elif parts[0] == "white_light":
                    state = with_cam(lambda c: set_white_light(c, float(msg.payload)))
                    self.publish_state(state)
                else:
                    log.warning("unknown topic %s", msg.topic)
        except Exception as exc:                      # noqa: BLE001
            log.error("command failed: %s", exc)
            self.client.publish(f"{BASE}/availability", "offline", retain=True)

    # -- loop

    def poll_forever(self):
        while True:
            try:
                state = with_cam(read_state)
                with self.lock:
                    self.state = state
                    self.publish_state(state)
            except Exception as exc:                  # noqa: BLE001
                log.warning("poll failed: %s", exc)
                self.client.publish(f"{BASE}/availability", "offline", retain=True)
            time.sleep(POLL_SECONDS)

    def run(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()
        threading.Thread(target=self.poll_forever, daemon=True).start()
        while True:
            time.sleep(3600)


def main():
    Bridge().run()


if __name__ == "__main__":
    main()
