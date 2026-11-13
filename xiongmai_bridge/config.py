"""Camera configuration: one entry per camera, several sources.

Three ways to configure the bridge, in order of precedence:

1. ``CAMERAS_FILE`` - path to a YAML/JSON file with a ``cameras:`` list;
2. ``CAMERAS``      - the same list inline as JSON (handy in CI/compose);
3. single-camera    - the ``CAM_HOST`` / ``CAM_PASS`` / ... variables, kept for
                      backward compatibility and for one-container-per-camera
                      setups.

An entry looks like this (YAML, see docs/configuration.md):

    defaults:
      labels: {preset_prefix: "Preset"}
    cameras:
      - name: bedroom
        host: 192.168.1.63
        secret_file: /config/cameras.json   # JSON with per-camera credentials
        secret_key: bedroom
        device_name: Bedroom camera
        area: Bedroom
        presets: {"1": "Bed", "2": "Door"}

``name`` becomes the MQTT topic segment and the Home Assistant ``entity_id``
prefix, so it must be lower-case latin/digits/underscore and unique.
``presets`` maps slot -> label; reserved slots are rejected at load time.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")

# Slots the firmware uses itself, plus 255 which lies outside the 255-entry
# table. See docs/ptz-presets.md.
RESERVED_SLOTS = frozenset({100, 128, 141, 250, 255})


class ConfigError(RuntimeError):
    pass


@dataclass
class CameraConfig:
    name: str
    host: str
    password: str = ""
    user: str = "admin"
    port: int = 34567
    device_name: str = ""
    model: str = "Xiongmai IP camera"
    area: str = ""
    presets: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    ptz_impulse: float = 0.8
    approach: str = "DirectionRightDown"
    preset_settle: float = 9.0
    preset_nudge: float = 0.4
    poll_seconds: int = 30
    # Explicit feature switches; None means "detect on the camera".
    ptz: object = None
    osd: object = None
    volume: object = None
    white_light: object = None

    @property
    def base(self):
        """MQTT topic prefix of this camera."""
        return f"xiongmai/{self.name}"

    def label(self, key, default):
        """Entity name override, e.g. labels: {volume: "Громкость"}."""
        return self.labels.get(key, default)

    def validate(self):
        if not NAME_RE.match(self.name or ""):
            raise ConfigError(f"camera name {self.name!r} must match {NAME_RE.pattern} "
                              f"(it is used in MQTT topics and entity ids)")
        if not self.host:
            raise ConfigError(f"camera {self.name}: host is required")
        if not self.password:
            raise ConfigError(f"camera {self.name}: password is required "
                              f"(set password, password_env or secret_file+secret_key)")
        for slot in self.presets:
            try:
                int(slot)
            except (TypeError, ValueError):
                raise ConfigError(f"camera {self.name}: preset slot {slot!r} is not a number")
            if int(slot) in RESERVED_SLOTS or not 1 <= int(slot) <= 254:
                raise ConfigError(f"camera {self.name}: preset slot {slot} is reserved "
                                  f"or out of range (1..254, not {sorted(RESERVED_SLOTS)})")
        self.presets = {int(k): str(v) for k, v in self.presets.items()}
        return self


def _read_secret(secret_file, secret_key, base_dir):
    path = Path(secret_file)
    if not path.is_absolute():
        path = Path(base_dir) / path
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read secret file {path}: {exc}") from exc
    node = data
    for part in str(secret_key).split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"secret key {secret_key!r} not found in {path}")
        node = node[part]
    if not isinstance(node, dict):
        raise ConfigError(f"secret key {secret_key!r} in {path} is not an object")
    return node


def _credentials(entry, env, base_dir):
    """Return (user, password); a secret object may carry both fields."""
    secret = {}
    if entry.get("secret_file"):
        secret = _read_secret(entry["secret_file"], entry.get("secret_key", ""), base_dir)
    if entry.get("password"):
        password = str(entry["password"])
    elif entry.get("password_env"):
        password = env.get(entry["password_env"])
        if not password:
            raise ConfigError(f"environment variable {entry['password_env']} is empty")
    elif secret.get("password"):
        password = str(secret["password"])
    else:
        raise ConfigError("no password source: use password, password_env or secret_file")
    user = entry.get("user") or secret.get("user") or env.get("CAM_USER", "admin")
    return str(user), password


def _merge(defaults, entry):
    merged = dict(defaults or {})
    for key, value in entry.items():
        if key in ("labels", "presets") and isinstance(value, dict):
            merged[key] = {**merged.get(key, {}), **value}
        else:
            merged[key] = value
    return merged


def from_mapping(entry, defaults=None, env=None, base_dir="."):
    """Build one CameraConfig from a mapping, applying defaults."""
    env = os.environ if env is None else env
    data = _merge(defaults, entry)
    name = str(data.get("name") or "").strip()
    cfg = CameraConfig(
        name=name,
        host=str(data.get("host") or ""),
        password="",
        user="",
        port=int(data.get("port") or env.get("CAM_PORT", 34567)),
        device_name=str(data.get("device_name") or data.get("name") or ""),
        model=str(data.get("model") or env.get("DEVICE_MODEL", "Xiongmai IP camera")),
        area=str(data.get("area") or ""),
        presets=data.get("presets") or {},
        labels=data.get("labels") or {},
        ptz_impulse=float(data.get("ptz_impulse", env.get("PTZ_IMPULSE", 0.8))),
        approach=str(data.get("approach", env.get("PTZ_APPROACH", "DirectionRightDown"))),
        preset_settle=float(data.get("preset_settle", env.get("PRESET_SETTLE", 9))),
        preset_nudge=float(data.get("preset_nudge", env.get("PRESET_NUDGE", 0.4))),
        poll_seconds=int(data.get("poll_seconds", env.get("POLL_SECONDS", 30))),
        ptz=data.get("ptz"),
        osd=data.get("osd"),
        volume=data.get("volume"),
        white_light=data.get("white_light"),
    )
    try:
        cfg.user, cfg.password = _credentials(data, env, base_dir)
    except ConfigError as exc:
        raise ConfigError(f"camera {name or '<unnamed>'}: {exc}") from exc
    return cfg.validate()


def _parse_file(path):
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read cameras file {path}: {exc}") from exc
    suffix = path.suffix.lower()
    if suffix in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("YAML config needs PyYAML: pip install pyyaml") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, list):
        return {"cameras": data}
    if not isinstance(data, dict) or "cameras" not in data:
        raise ConfigError(f"{path}: expected a 'cameras:' list")
    return data


def load(env=None):
    """Return the list of configured cameras."""
    env = os.environ if env is None else env
    base_dir = "."

    if env.get("CAMERAS_FILE"):
        document = _parse_file(env["CAMERAS_FILE"])
        base_dir = str(Path(env["CAMERAS_FILE"]).parent)
    elif env.get("CAMERAS"):
        inline = json.loads(env["CAMERAS"])
        # either a bare list of cameras or a document with defaults/cameras
        document = inline if isinstance(inline, dict) else {"cameras": inline}
    else:
        # single-camera mode: the classic variable set
        if not env.get("CAM_PASS"):
            raise ConfigError("nothing to do: set CAMERAS_FILE, CAMERAS or CAM_PASS")
        defaults = {}
        return [from_mapping({
            "name": env.get("CAM_NAME", "xiongmai"),
            "host": env.get("CAM_HOST", "192.168.1.10"),
            "password": env["CAM_PASS"],
            "user": env.get("CAM_USER", "admin"),
            "port": env.get("CAM_PORT", 34567),
            "device_name": env.get("DEVICE_NAME") or env.get("CAM_NAME", "xiongmai"),
            "model": env.get("DEVICE_MODEL", "Xiongmai IP camera"),
            "presets": json.loads(env.get("PRESETS", '{"1": "Preset 1"}')),
            "labels": json.loads(env.get("LABELS", "{}")),
        }, defaults, env, base_dir)]

    defaults = document.get("defaults") or {}
    entries = document.get("cameras") or []
    if not entries:
        raise ConfigError("cameras list is empty")
    cameras = [from_mapping(entry, defaults, env, base_dir) for entry in entries]
    names = [c.name for c in cameras]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"duplicate camera names: {sorted(duplicates)}")
    return cameras
