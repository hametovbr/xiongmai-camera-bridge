# xiongmai-camera-bridge

Turn a cheap Xiongmai / XMeye / iCSee pan-tilt camera into something Home
Assistant can actually drive: PTZ, presets that come back to the same view, OSD
switches, speaker volume and white-light brightness — all over the camera's own
DVRIP protocol (TCP 34567).

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
| `switch.*_osd_title`, `switch.*_osd_time` | on-screen title / clock |
| `number.*_volume` | speaker volume |
| `number.*_white_light` | white light brightness |

Writes are read back from the camera before the state is published, so Home
Assistant never shows a value the camera did not accept.

## Install

```bash
git clone https://github.com/hametovbr/xiongmai-camera-bridge
cd xiongmai-camera-bridge
cp env.example .env        # fill in CAM_HOST, CAM_PASS, MQTT_*
docker compose up -d
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

## Configuration

Everything is environment variables; `env.example` is the annotated list.

| Variable | Default | Meaning |
|---|---|---|
| `CAM_HOST` | `192.168.1.10` | camera address |
| `CAM_PORT` | `34567` | DVRIP port |
| `CAM_USER` | `admin` | login user |
| `CAM_PASS` | *(required)* | login password, hashed with the vendor "sofia" hash |
| `CAM_NAME` | `xiongmai` | MQTT topic segment and entity id prefix |
| `DEVICE_NAME` | `Xiongmai camera` | name shown in Home Assistant |
| `PRESETS` | `{"1": "Preset 1"}` | JSON map `slot -> label` to expose as buttons |
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
