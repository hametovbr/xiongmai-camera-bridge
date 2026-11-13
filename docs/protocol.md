# DVRIP protocol notes (Xiongmai / Sofia)

Everything here was verified against a Xiongmai camera running
`V5.00.R02.000759R5` unless a line says otherwise. DVRIP is also known as the
"Sofia" protocol; the vendor apps (XMeye, iCSee) speak it over TCP 34567.

## 1. Framing

Each message is a 20-byte little-endian header plus a JSON payload:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | head | always `0xFF` |
| 1 | 1 | version | `0x01` |
| 2 | 2 | reserved | `0x0000` |
| 4 | 4 | session | `0` before login, then the id from the login reply |
| 8 | 4 | sequence | any increasing number for requests |
| 12 | 1 | total | fragment count, `0` = single packet |
| 13 | 1 | current | fragment index |
| 14 | 2 | msg_id | message type |
| 16 | 4 | length | payload length |
| 20 | n | payload | JSON, in practice terminated with `\n\0` |

The reply carries `msg_id + 1`. Unsolicited messages (alarms, keep-alives) can
arrive at any time and must be skipped by `msg_id`.

| Request | Reply | Meaning |
|---|---|---|
| 1000 | 1001 | login |
| 1020 | 1021 | keep-alive |
| 1040 | 1041 | set config section |
| 1042 | 1043 | get config section |
| 1400 | 1401 | `OPPTZControl` (pan/tilt/zoom, presets, reset) |
| 1450 | 1451 | `OPMachine` (reboot, shutdown) |

## 2. Login

The password never travels in clear text. It is hashed into an 8-character
string ("sofia" hash): take the MD5 of the password, then for each `i` in
`0..7` sum bytes `digest[2i]` and `digest[2i+1]` and index a 62-character
alphabet `0-9A-Za-z` with `sum % 62`.

```json
{"UserName": "admin", "PassWord": "<xmmd5 hash>", "EncryptType": "MD5", "LoginType": "DVRIP-Web"}
```

A successful reply has `Ret: 100` and `SessionID` as a hex string; that value
goes both into the binary header and into every JSON body as `SessionID`.

## 3. Return codes

`Ret: 100` means "accepted", **not** "done". The camera answers 100 to PTZ
command names it does not know, so a code is never proof that the head moved.
Acceptance must be checked visually (see `docs/ptz-presets.md`).

| Ret | Meaning |
|---|---|
| 100 | ok / accepted |
| 603 | config imported, applied after reboot |
| 607 | no such config section on this firmware |

## 4. Config sections

Read with `{"Name": "<section>", "SessionID": "..."}`; write the whole section
back with type 1040. Section naming is inconsistent across firmware versions:
some are nested (`Simplify.Encode`), some are top level (`Camera`, `Uart`).

Sections seen on this firmware:

| Section | Contents |
|---|---|
| `Simplify.Encode` | two video profiles: `Compression` (H.264/H.265), `Resolution`, `FPS`, `BitRate`, `GOP`, `AudioEnable` |
| `fVideo` | `GUISet` (OSD switches), `OSDInfo`, `OsdLogo`, `Volume`, `VolumeIn` |
| `Camera` | `WhiteLight` (brightness, mode, schedule), `Param` (day/night, `IRCUTMode`) |
| `Uart` | PTZ protocol settings and `PTZPreset` — only `Id` and `PresetName`, **no coordinates** |
| `Detect` | `HumanDetection`, `MotionDetect`, `DetectTrack` (auto-tracking) |
| `Record`, `Storage` | recording mode, pre-record, stream selection |
| `NetWork` | HTTP/HTTPS/RTSP/DVRIP ports, Wi-Fi client, UPnP |
| `General` | `ResumePtzState` (restore PTZ after reboot) |

Not available on this firmware: `SystemInfo`, `StorageInfo`, `OSD`,
`Simplify.OSD`, `VideoColor`, `Camera.MotorCtrl` (all `Ret: 607`).

Two observations worth knowing:

* The OSD text and time are in `fVideo.GUISet` (`ChannelTitleEnable`,
  `TimeTitleEnable`), not in an `OSD` section.
* `Uart.PTZ.ProtocolName` is `PELCOD` even though the camera has an internal
  stepper head. The Pelco-D frames are generated *inside* the camera and parsed
  by `libXmDvr.so`; they never reach an RS485 port. See `docs/firmware.md`.

## 5. PTZ commands

All go through type 1400:

```json
{"Name": "OPPTZControl", "SessionID": "<hex>",
 "OPPTZControl": {"Command": "<name>", "Parameter": {
   "Channel": 0, "Preset": 0, "Step": 5,
   "Tour": 0, "Pattern": 0, "MenuOpt": 0, "Aux": 0}}}
```

Command names are **case-sensitive** and taken from a fixed table in the
firmware binary: `DirectionUp`, `DirectionDown`, `DirectionLeft`,
`DirectionRight`, `DirectionLeftUp`, `DirectionLeftDown`, `DirectionRightUp`,
`DirectionRightDown`, `ZoomWide`, `ZoomTile`, `FocusNear`, `FocusFar`,
`IrisLarge`, `IrisSmall`, `LightOn`, `LightOff`, `SetPreset`, `ClearPreset`,
`GotoPreset`, `AutoPanOn/Off`, `SetLimitLeft/Right`, `AutoScanOn/Off`,
`AddTour`, `DeleteTour`, `StartTour`, `StopTour`, `ClearTour`, `Position`,
`Aux`, `Menu`, `Flip`, `Reset`, `Pattern`.

The important ones:

| Command | Parameter | Effect |
|---|---|---|
| `Direction*` | `Preset: 0` or `65535`, `Step: 0..8` | start moving; continues until stopped |
| `Direction*` | `Preset: -1` | **stop** — there is no separate stop command |
| `SetPreset` | `Preset: N` | store the current step counters in slot `N` |
| `GotoPreset` | `Preset: N` | move back to slot `N` |
| `ClearPreset` | `Preset: N` | free slot `N` |
| `Reset` | — | firmware re-centres the head |

`Step` is a speed level, not a number of steps. With `Preset` set to anything
other than `0`, `65535` or `-1`, `Direction*` is accepted and silently ignored.

**`GoToPreset` (capital `T`) is not a DVRIP command.** It is the name of the
operation inside the camera's Squirrel scripts; the network handler does not
know it and returns `Ret: 100` without doing anything. This single-letter
difference is the most common reason a preset "does not work" from a custom
client.

`Position` and `Azimuth` build a Pelco packet that starts with `0x3A`; the
internal head path returns before any motor action, so absolute coordinates are
not reachable over DVRIP.

## 6. RTSP

Xiongmai puts credentials in the path instead of using HTTP auth:

```
rtsp://<host>:554/user=<user>_password=<pass>_channel=1_stream=0.sdp?real_stream
rtsp://<host>:554/user=<user>_password=<pass>_channel=1_stream=1.sdp?real_stream
```

`stream=0` is the main stream, `stream=1` the sub stream. Both `_` and `&` work
as separators; `_` is safer inside URLs. The SDP is authoritative for codec and
resolution — the config may claim `D1` while the SDP says 640x360.

## 7. Reboot

```json
{"Name": "OPMachine", "SessionID": "<hex>", "OPMachine": {"Action": "Reboot"}}
```

Config writes that need a restart answer `Ret: 603` and only take effect after
the camera reboots (a codec switch is the typical case). The camera comes back
in about 1.5 minutes over Wi-Fi.
