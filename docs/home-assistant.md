# Adding the bridge to Home Assistant

The bridge speaks MQTT only: Home Assistant learns about the camera from MQTT
discovery messages, so there is nothing to add through the HA UI. This page is
the order of operations, what you should see, and how to diagnose it when you
see nothing.

## Prerequisites

| What | Why |
|---|---|
| MQTT broker (Mosquitto or any 1883 broker) | the only channel between bridge and HA |
| MQTT integration configured in HA | receives discovery and entity states |
| MQTT discovery enabled (default: on, prefix `homeassistant`) | otherwise nothing appears |
| Broker credentials for the bridge | it publishes with its own user/password |
| Network path container → broker and container → camera (34567/tcp) | bridge polls the camera directly |
| Camera login user and password | `Ret: 205` means the login was rejected |

Check the HA side: **Settings → Devices & Services → MQTT**. Discovery is on when
the integration page shows the option enabled; the prefix is usually
`homeassistant`.

## 1. Give the bridge broker credentials

`.env` (mode 600, never committed):

```text
MQTT_HOST=192.168.1.10
MQTT_PORT=1883
MQTT_USER=myuser
MQTT_PASS=mypassword
CAMERAS_FILE=/config/cameras.yml
```

## 2. Describe the camera(s)

`cameras.yml` — the only required field per camera is `name`, `host` and a
credential source:

```yaml
cameras:
  - name: bedroom              # MQTT topic + entity_id prefix, lower-case latin
    host: 192.168.1.63
    user: admin
    password: camerapassword   # or password_env / secret_file+secret_key
    device_name: Bedroom camera
    area: Bedroom              # suggested area for the HA device
    presets: {"1": "Bed"}
```

Full field reference: [configuration.md](configuration.md).

## 3. Start it and read the log

```bash
docker compose up -d
docker logs -f xiongmai-camera
```

Three lines mean it is healthy:

```text
INFO configured cameras: bedroom
INFO MQTT connected: Success (1 camera(s))
INFO bedroom: features {'fvideo': True, 'osd_title': True, 'osd_time': True, 'volume': True, 'white_light': True}
```

`features` is what the camera actually reports. A `False` there means the entity
group will not be published, not that something is broken — see
"Entities are missing" below.

## 4. Where the device shows up

**Settings → Devices & Services → MQTT → N devices** (or the Devices tab), device
name = `device_name`, manufacturer `Xiongmai`. There is nothing to add by hand:
the bridge publishes retained discovery configs to
`homeassistant/<component>/<name>/<object>/config`, and HA creates the device,
entities and `unique_id`s from them.

Because the configs are retained, the device survives an HA restart and comes
back on its own; if the bridge is stopped, the entities stay but turn
`unavailable`.

## 5. What it gives you

Per camera, one HA device with these entities (published only if the camera has
the capability):

| Entity | What it does | DVRIP side |
|---|---|---|
| `button.<name>_ptz_directionleft/right/up/down` | bounded move (default 0.8 s) and an explicit stop | `Direction*` + `Preset: -1` |
| `button.<name>_ptz_home` | re-centres the head | `Reset` |
| `button.<name>_preset_<slot>` | returns to the slot, with backlash compensation (~12 s) | `GotoPreset` |
| `switch.<name>_osd_title` | on-screen channel title | `fVideo.GUISet.ChannelTitleEnable` |
| `switch.<name>_osd_time` | on-screen clock | `fVideo.GUISet.TimeTitleEnable` |
| `number.<name>_volume` | speaker volume 0..100 | `fVideo.Volume[*].LeftVolume/RightVolume` |
| `number.<name>_white_light` | floodlight brightness 0..100 | `Camera.WhiteLight.Brightness` |

Entity ids are `xiongmai_<name>_<object>` for the `unique_id`, and HA derives the
visible `entity_id` from the device and entity names — with `labels` you control
the names without touching ids:

```yaml
labels: {ptz_home: "PTZ домой", volume: "Громкость", preset_prefix: "Пресет"}
```

Writing a value on a `switch`/`number` reads it back from the camera before the
state is published, so a rejected write leaves HA showing the old value instead
of a false success.

`area` becomes `suggested_area`: HA applies it when the device is created. To
change the area of an existing device use the device page (**Settings → Devices**
→ the device → area).

## 6. Verify it end to end

1. Device page shows the entity count you expect: 7 buttons for a PTZ camera with
   one preset, plus a switch/number per capability.
2. `number.*_volume` and `number.*_white_light` show the camera's current values
   (they are polled every `poll_seconds`, default 30).
3. Change the volume by one step: the value settles to the new one, and the
   camera keeps it (the bridge re-reads it).
4. Press a direction button: the head moves briefly and stops. Log line:
   `cmd xiongmai/<name>/ptz/DirectionLeft/set -> b'PRESS'`.
5. Press a preset button: the head returns to the slot. **Verify by picture, not
   by the button**: `Ret: 100` never proves motion, and a single `GotoPreset`
   misses because of the head's backlash. The bridge already approaches the slot
   from one side; if the view is off, adjust `approach` / `preset_nudge` or
   re-save the slot (see [ptz-presets.md](ptz-presets.md)).
6. Stop the container: within a poll interval the entities go `unavailable`
   (both availability topics, `availability_mode: all`).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No device appears at all | MQTT discovery off in HA; wrong `MQTT_HOST`/credentials; the bridge never logged `MQTT connected`. Check the log and the HA MQTT page |
| Device appeared, all entities `unavailable` | bridge is down (`xiongmai/bridge/availability` = `offline`) or the camera stopped answering (`xiongmai/<name>/availability`) |
| Entities are missing (no switches, no numbers) | the camera reported no such section: `Camera.MotorCtrl`-style sections answer `Ret: 607` on some models. Force a group with `osd: true` / `volume: true` / `white_light: true`, or hide it with `false` |
| A second, duplicate device | `name` was changed: it is the device identifier. Rename back, or delete the old device and clear its retained discovery topics |
| Button press ignored, log says `unknown topic` | the topic shape the client sends does not match the bridge version. Expected: `xiongmai/<name>/ptz/<Command>/set`, `xiongmai/<name>/preset/<slot>/set`, `xiongmai/<name>/<key>/set` |
| `login failed: Ret=205` in the log, no device | wrong user or password; with `secret_file` the login is taken from the same secret object as the password |
| Names in HA are English (`PTZ left`) | add `labels` — names change, `entity_id`s do not |
| Preset returns to a slightly different view | backlash: keep the approach impulse, lower `preset_nudge` for a gentler nudge, or re-save the slot approaching from the same side |
| Device disappeared after upgrading the image | `name` changed between configs, or discovery prefix differs (`DISCOVERY_PREFIX`) |

## Removing the bridge

1. `docker compose down` — the bridge stops and the last will marks it offline.
2. Delete the device in HA (**Settings → Devices → the device → Delete**). The
   retained discovery configs are still in the broker, so it can come back.
3. Clear them explicitly, e.g. with `mosquitto_pub` publishing empty retained
   payloads to the device's config topics:

```bash
for t in button/.../ptz_directionleft button/.../ptz_home switch/.../osd_title \
         number/.../volume; do
  mosquitto_pub -h "$BROKER" -u "$USER" -P "$PASS" -r -n \
    -t "homeassistant/${t%%/*}/${t#*/}/config"
done
```

Removing the bridge from the MQTT topic tree is enough for HA to forget the
device on the next discovery pass; retained `xiongmai/<name>/state`,
`.../availability` and `xiongmai/bridge/availability` can be cleared the same way
(empty retained payload).
