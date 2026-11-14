# xiongmai-camera-bridge

Turn a cheap Xiongmai / XMeye / iCSee pan-tilt camera into something Home
Assistant can actually drive: PTZ, presets that come back to the same view
(and can be saved/deleted from HA, with auto-discovery of the slots made in
the vendor app), OSD switches, speaker volume and white-light brightness —
all over the camera's own DVRIP protocol (TCP 34567).

There is no ONVIF and no HTTP API on these cameras, and the vendor app is the
only supported client. This project is a small, documented replacement for the
parts of the app that matter.

```
DVRIP (TCP 34567)  <--  xiongmai-camera-bridge  -->  MQTT / Home Assistant discovery
```

## What you get in Home Assistant

| Entity | Action |
|---|---|
| `button.*_ptz_directionleft` … | bounded move in one direction, with an explicit stop |
| `button.*_ptz_reset` | `Reset` — firmware re-centres the head |
| `button.*_preset_<n>` | return to preset slot `n` (with backlash compensation) |
| `select.*_preset_slot` + `button.*_preset_save` / `_preset_delete` | save/delete presets from HA (v0.3.0) |
| `sensor.*_preset_slots` | occupied slots, free whitelist slots, last action/error |
| `switch.*_osd_title`, `switch.*_osd_time` | on-screen title / clock |
| `number.*_volume` | speaker volume |
| `number.*_white_light` | white light brightness |

Writes are read back from the camera before the state is published, so Home
Assistant never shows a value the camera did not accept.

Entities exist only for the capabilities the camera reports: no speaker, no
floodlight or no PTZ means no such entities. How to add the device, verify it and
fix a missing one — [docs/home-assistant.md](docs/home-assistant.md).

## Install

Prebuilt multi-arch images (`linux/amd64`, `linux/arm64`) are published to
GitHub Container Registry:

```bash
docker pull ghcr.io/hametovbr/xiongmai-camera-bridge:0.3.0
```

With Compose, drop this next to your `.env` and `cameras.yml`:

```yaml
services:
  xiongmai-camera:
    image: ghcr.io/hametovbr/xiongmai-camera-bridge:0.3.0
    container_name: xiongmai-camera
    restart: unless-stopped
    env_file: [.env]                     # MQTT_HOST/PORT/USER/PASS, CAMERAS_FILE
    volumes:
      - ./cameras.yml:/config/cameras.yml:ro
```

`edge` tracks `main`, `latest` points at the newest release tag.

Requirements on the HA side: an MQTT broker plus the MQTT integration with
discovery enabled. Step-by-step, with what you should see and how to diagnose a
missing device: [docs/home-assistant.md](docs/home-assistant.md).

From source:

```bash
git clone https://github.com/hametovbr/xiongmai-camera-bridge
cd xiongmai-camera-bridge
cp env.example .env        # fill in CAM_HOST, CAM_PASS, MQTT_*
docker compose up -d --build
docker compose logs -f
```

Without Docker:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[measure]'
set -a; . ./.env; set +a
python -m xiongmai_bridge.bridge
```

`devices` then appear in Home Assistant through MQTT discovery (broker discover
must be enabled, as usual for MQTT). The device is named by `DEVICE_NAME`, the
topic and `entity_id` prefix come from `CAM_NAME`.

## Several cameras

One process serves all cameras; each gets its own poll thread, command lock, MQTT
topics and Home Assistant device, so a slow preset move on one camera never
delays another and an offline camera only takes itself offline.

```yaml
# cameras.yml  (mount it and set CAMERAS_FILE=/config/cameras.yml)
cameras:
  - name: bedroom
    host: 192.168.1.63
    secret_file: cameras.json      # JSON with per-camera credentials
    secret_key: bedroom
    device_name: Bedroom camera
    area: Bedroom
    presets: {"1": "Bed"}
  - name: hall
    host: 192.168.1.61
    password_env: HALL_PASS
    device_name: Hall camera
```

`name` is the MQTT topic segment and the `entity_id` prefix. Entities for
features a camera does not have (no speaker, no white light, no PTZ) are not
published at all, so the same image fits different models.
Full reference: [docs/configuration.md](docs/configuration.md).

## Configuration

Everything is environment variables or a cameras file; `env.example` is the
annotated list.

| Variable | Default | Meaning |
|---|---|---|
| `CAM_HOST` | `192.168.1.10` | camera address |
| `CAM_PORT` | `34567` | DVRIP port |
| `CAM_USER` | `admin` | login user |
| `CAM_PASS` | *(required)* | login password, hashed with the vendor "sofia" hash |
| `CAM_NAME` | `xiongmai` | MQTT topic segment and entity id prefix |
| `DEVICE_NAME` | `Xiongmai camera` | name shown in Home Assistant |
| `PRESETS` | `{"1": "Preset 1"}` | JSON map `slot -> label` to expose as buttons |
| `LABELS` | `{}` | optional rename of entities, e.g. `{"volume": "Громкость"}` |
| `PTZ_IMPULSE` | `0.8` | seconds of movement per direction button press |
| `PTZ_APPROACH` | `DirectionRightDown` | direction of the final approach (empty = off) |
| `PRESET_SETTLE` / `PRESET_NUDGE` | `9` / `0.4` | wait after goto, length of the approach impulse |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | broker |
| `MQTT_USER` / `MQTT_PASS` | — | broker credentials |
| `POLL_SECONDS` | `30` | state refresh interval |

## Command line

The `xiongmai-ptz` tool talks to the camera directly, without MQTT:

```bash
export CAM_HOST=... CAM_USER=... CAM_PASS=...

xiongmai-ptz slots                 # preset slots known to the config
xiongmai-ptz save 5 --label Door   # store the current view in slot 5
xiongmai-ptz mark 5                # keep a reference frame of that view
xiongmai-ptz goto 5                # return (backlash compensated)
xiongmai-ptz check 5               # compare live view with the reference, in pixels
xiongmai-ptz clear 5               # free the slot
```

`mark`/`check` need the `measure` extra (PyAV, numpy, Pillow) and the camera's
RTSP port.

## Two things that will bite you

**Command name case.** The DVRIP command is `GotoPreset` with a lower-case `t`.
`GoToPreset` is the internal name in the camera's Squirrel scripts; the network
handler does not recognise it and answers `Ret: 100` while doing nothing. Most
"presets don't work" reports are this.

**`Ret: 100` is not success.** The camera returns 100 for unknown commands too,
and a preset reply arrives long before the head stops moving. Verify by frame,
not by return code — that is what `xiongmai-ptz check` is for.

The bridge compensates for the mechanical backlash of the head (no encoder, so
the step counter drifts with the direction of approach): it always approaches
the slot from the same side, which brings the error from up to ~68 px down to a
few pixels of a 640x360 frame. It costs about 12 seconds per preset press.

## Documentation

* [docs/home-assistant.md](docs/home-assistant.md) — adding the bridge to Home
  Assistant: prerequisites, steps, the entity list, verification and
  troubleshooting.
* [docs/configuration.md](docs/configuration.md) — one or many cameras, credential
  sources, per-camera options, topics, availability, one-process vs one-container.
* [docs/protocol.md](docs/protocol.md) — DVRIP framing, login hash, message
  types, config sections, PTZ command table, RTSP URLs.
* [docs/ptz-presets.md](docs/ptz-presets.md) — how presets are stored inside the
  camera, the `libXmDvr.so` functions involved, why a single `GotoPreset` misses
  and the recipe this bridge uses, how to verify.
* [docs/firmware.md](docs/firmware.md) — flash layout, the `App`/`libXmDvr.so`
  PTZ path with addresses, `/dev/motor` ioctls and motor commands, Squirrel
  scripts, auto-tracking.
* [docs/hardware.md](docs/hardware.md) — the reference camera (IPG-G4-WR-BL,
  GK7202V300, SSV6355), ports, streams, and why alternative firmware is a bad
  trade here.

## Scope and caveats

* Verified against one camera on firmware `V5.00.R02.000759R5`. The protocol
  works on other Xiongmai models, but config section names and PTZ behaviour
  differ between firmware families — check before deploying at scale.
* Slot `255` must not be used (outside the internal 255-entry table); slots
  `100`, `128`, `141`, `250` are used by the firmware itself.
* Accuracy after repeatedly driving the head into a mechanical stop is not
  verified; counters drift in that case and only a recalibration fixes it.
* This project never flashes, telnets into or otherwise modifies the camera.

## License

MIT — see [LICENSE](LICENSE). Use it however you like.
