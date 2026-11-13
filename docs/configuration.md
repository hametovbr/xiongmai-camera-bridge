# Configuration: one camera or many

The bridge runs any number of cameras in one process. Each camera gets its own
poll thread, its own command lock, its own MQTT topics and its own Home
Assistant device — a 12-second preset move on one camera does not delay a
command to another, and a camera that goes offline takes only itself offline.

## Three sources, in order of precedence

| Source | Use it when |
|---|---|
| `CAMERAS_FILE=/path/cameras.yml` | the normal case: several cameras, readable file with comments |
| `CAMERAS='[{"name": ...}]'` | container orchestrators and CI, where a file is awkward |
| `CAM_HOST` / `CAM_PASS` / `CAM_NAME` … | one camera per container, or upgrading from 0.1.x |

The first two accept defaults that apply to every camera; anything set on a
camera overrides them.

## A file with three cameras

```yaml
defaults:
  labels: {preset_prefix: "Preset"}

cameras:
  - name: bedroom                 # MQTT topic segment + entity_id prefix
    host: 192.168.1.63
    secret_file: cameras.json     # JSON with per-camera credentials
    secret_key: bedroom           # -> cameras.json["bedroom"]["password"]
    device_name: Bedroom camera
    area: Bedroom                 # suggested area for the HA device
    model: IPG-G4-WR-BL (GK7202V300)
    presets: {"1": "Bed", "2": "Door"}

  - name: hall
    host: 192.168.1.61
    secret_file: cameras.json
    secret_key: corridor
    device_name: Hall camera
    area: Hallway
    ptz_impulse: 0.6              # gentler nudge than the default 0.8 s

  - name: garden
    host: 192.168.1.57
    password_env: GARDEN_PASS     # credential from the environment
    device_name: Garden camera
    white_light: false            # this model has no white light
```

`name` is used in MQTT topics (`xiongmai/<name>/...`), in the HA `entity_id`
(`xiongmai_<name>_...`) and in the device identifier, so it must be lower-case
latin/digits/underscore and unique. Changing it later makes Home Assistant
create a new device.

## Camera fields

| Field | Default | Meaning |
|---|---|---|
| `name` | — | required, topic/entity prefix |
| `host` | — | required, camera address |
| `port` | `34567` | DVRIP port |
| `user` | `admin` | login user |
| `password` / `password_env` / `secret_file`+`secret_key` | — | one credential source is required |
| `device_name` | `name` | name of the device in Home Assistant |
| `model` | `Xiongmai IP camera` | shown on the HA device page |
| `area` | empty | `suggested_area` for the HA device (applied on creation) |
| `presets` | `{}` | slot → label, exposed as buttons; slots must be 1..254 |
| `labels` | `{}` | per-entity renames (see below) |
| `ptz_impulse` | `0.8` | seconds of movement per direction press |
| `approach` | `DirectionRightDown` | final approach used for backlash compensation (`""` = off) |
| `preset_settle` / `preset_nudge` | `9` / `0.4` | wait after goto, length of the approach impulse |
| `poll_seconds` | `30` | state refresh interval |
| `ptz`, `osd`, `volume`, `white_light` | auto | force an entity group on (`true`) or off (`false`) |

Credential sources: `password` inline (only for throwaway setups), `password_env`
(name of an environment variable), or `secret_file` + `secret_key` pointing into
a JSON file such as `.secrets/cameras.json`. The secret file path is resolved
relative to the directory of the cameras file, so mounting both into the
container keeps them side by side.

Reserved preset slots are refused at load time: `100`, `128`, `141`, `250` are
used by the firmware itself and `255` lies outside the 255-entry table.

## What each camera gets in Home Assistant

One device per camera, with these entities (only for the capabilities the camera
reports):

| Entity | Key | Source |
|---|---|---|
| `button.*_ptz_directionleft/right/up/down` | `ptz_direction*` | bounded move + explicit stop |
| `button.*_ptz_home` | `ptz_home` | `Reset` |
| `button.*_preset_<slot>` | `preset_prefix` + preset label | `GotoPreset` with backlash compensation |
| `switch.*_osd_title`, `switch.*_osd_time` | `osd_title`, `osd_time` | `fVideo.GUISet` |
| `number.*_volume` | `volume` | `fVideo.Volume` |
| `number.*_white_light` | `white_light` | `Camera.WhiteLight` |

Capabilities are detected once per camera by reading the config sections: if
`fVideo` or `Camera.WhiteLight` is missing (`Ret: 607`), the corresponding
entities are simply not published, which is why the same image works on models
without a speaker or a floodlight. A camera that is unreachable at startup gets
no device until it answers — then its entities appear.

`labels` renames entities without forking the bridge, e.g. to keep Home
Assistant in another language:

```yaml
labels: {ptz_home: "PTZ домой", volume: "Громкость", preset_prefix: "Пресет"}
```

## Topics and availability

| Topic | Direction | Content |
|---|---|---|
| `xiongmai/<name>/ptz/<Command>/set` | in | `PRESS`, e.g. `DirectionLeft`, `Reset` |
| `xiongmai/<name>/preset/<slot>/set` | in | `PRESS` |
| `xiongmai/<name>/osd_title/set`, `…/osd_time/set` | in | `ON` / `OFF` |
| `xiongmai/<name>/volume/set`, `…/white_light/set` | in | `0`..`100` |
| `xiongmai/<name>/state` | out | JSON with all current values |
| `xiongmai/<name>/availability` | out | `online` / `offline`, retained |
| `xiongmai/bridge/availability` | out | `online` while the process lives, `offline` via last will |
| `homeassistant/<component>/<name>/<object>/config` | out | discovery |

A device depends on **both** availability topics with `availability_mode: all`:
if the bridge process dies, every camera shows as unavailable immediately, and a
camera that stops answering goes offline on its own without touching the others.

`BRIDGE_AVAILABILITY` renames the process-wide topic — useful when running a
second instance (test, staging) next to the production one.

## One process or one container per camera?

Both are supported; the image is the same.

**Recommended: one process, several cameras** (`CAMERAS_FILE`).

* one place to add a camera, one restart, one log;
* one MQTT connection instead of N;
* isolation is provided in-process: separate threads, locks, availability topics;
* the failure of one camera is logged and does not affect the rest.

**One container per camera** (`CAM_HOST` variables, different `CAM_NAME`) is
better when: you need different image versions per camera (e.g. testing a change
on one device), or you want hard process isolation, or cameras live in separate
Docker networks. Cost: N env files and N services to keep in sync.

Rough resource numbers: the process idles at a few tens of MB and one DVRIP
session per camera per poll interval.

## Migration from 0.1.x

0.1.x was single-camera and env-only. The variables still work, so nothing breaks
on upgrade. To move to a file:

1. create `cameras.yml` with one entry per camera (`name` = your old `CAM_NAME`);
2. keep `MQTT_*` in the env file, drop `CAM_HOST`/`CAM_PASS`/`PRESETS`/`LABELS`
   (they are per-camera now) or keep them as global defaults;
3. mount the cameras file and the secrets file read-only, set `CAMERAS_FILE`;
4. `docker compose up -d` — Home Assistant keeps the same entities as long as
   `name` is unchanged.

## Validate before deploying

The loader fails fast with a message naming the camera:

```bash
docker compose run --rm xiongmai-camera python -c \
  "from xiongmai_bridge.config import load; print([c.name for c in load()])"
```

It rejects a missing password source, duplicate names, bad names (anything that
is not lower-case latin/underscore) and reserved preset slots.
