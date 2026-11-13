# Hardware notes: the camera this was reverse-engineered on

The reference device is a consumer Wi-Fi pan/tilt camera sold as **JFTech
RF-4D** (4 MP), built on the Xiongmai module **IPG-G4-WR-BL**. The same module
appears in many rebadged cameras, so most of the protocol work applies broadly;
the addresses and pin-level details are specific to this board.

## 1. Module and SoC

| Item | Value |
|---|---|
| Module | Xiongmai **IPG-G4-WR-BL** |
| Camera model | JFTech **RF-4D**, 4 MP |
| SoC | Goke **GK7202V300** (ARM Cortex-A7, vendor media stack `gk7205v200_*`) |
| Device id | `000759R5` (matches the firmware version suffix) |
| Firmware analysed | `V5.00.R02.000759R5.10000.140d23.00000000`, built 2024-03-04 |
| Flash | 8 MB SPI NOR |
| RAM | enough for a 4 MP H.265 encoder plus the app; no swap |
| Sensor | 4 MP, type stored encrypted in the `boot` partition |
| PT | two stepper motors, driven by `XmMotor.ko` through GPIO, **no encoder** |
| IR | IR-cut filter and IR LEDs on GPIO; optional white light |
| Ethernet | **none** — the module only has Wi-Fi |
| Wi-Fi | **SSV6355** (iComm Semiconductor), `ssv6355.ko`, 2.4 GHz only |
| Sound | one-way over G.711A (`gk7205v200_acodec/aenc`), speaker for talk-back |

The Wi-Fi chip explains the MAC OUI (`84:b4:d2`, iComm Semiconductor) — it is the
radio vendor, not the camera brand.

## 2. Network services

| Port | Proto | Purpose |
|---|---|---|
| 80 | HTTP | vendor "Web Viewer" page, Windows ActiveX plugin — useless outside IE |
| 8443 | HTTPS | same web layer over TLS |
| 554 | RTSP | video, credentials in the URL path |
| 34567 | TCP | DVRIP: config, PTZ, recording, archive search |
| 34568 | UDP | DVRIP over UDP (alarms) |

**There is no ONVIF.** `/onvif/device_service` returns 404, ports 8000/8899/2020
are closed, and there is no ONVIF section in the network config. Home Assistant
`onvif` integration will not find it; use `generic` (RTSP) plus this bridge.

**Telnet is not available** on stock firmware (23 and 2300 closed). Enabling it
requires editing `u-boot.env.img` inside a vendor image and re-flashing — see
section 5 for why that is a risk, not a recommendation.

## 3. Video

| Stream | Codec | Resolution | FPS | Bitrate |
|---|---|---|---|---|
| main (`stream=0`) | H.265 by default, switchable to H.264 | 2560x1440 | 25 | ~1024 kbit/s VBR |
| sub (`stream=1`) | same | 640x360 | 25 | ~552 kbit/s VBR |

Audio is PCMA (G.711A) at 8 kHz on both streams.

Practical notes gathered while integrating with Home Assistant:

* H.265 is what breaks `camera.snapshot` in HA: the `generic` platform forces an
  `ffmpeg:` source, and that combination fails (`exit status 183`) while live
  video still works. Switching the encoder to H.264 via `Simplify.Encode` fixes
  snapshots; the setting needs a reboot (`Ret: 603`).
* The config may report `D1` for the sub stream while the SDP says 640x360 —
  trust the SDP.
* The lens is mounted rotated 90° in this housing: the picture arrives sideways,
  while the OSD text stays upright (so the rotation is mechanical, not
  software). Don't try to see the camera body as a symptom of a firmware bug.
* `PictureFlip` and `PictureMirror` exist in the config; a 90° rotation does not.

## 4. Interesting strings in the image

The image is a fairly standard OEM build: `libXmDvr.so`, `libXmComm.so`,
`libXmJson.so`, `XmDvrBox`, `App`, the Squirrel PTZ scripts, `CustomConfig/*.custom`
(`Ability.custom`, `trackset.custom`) and `jsons/`. Sensor descriptions are
encrypted (`XMFV1001…`), and the calibration data (sensor type, MAC) sits in the
"crypto" block at the end of `boot` — which is why flashing a bootloader is
dangerous on these cameras.

Cloud dependencies are compiled in and enabled: NAT-P2P to `secu100.net:8765`,
push to `push.umeye.cn:80`, updates from `upgrade.secu100.net:8083` with
auto-update on, and an account binding flag. None of that is needed for local
control, but it does mean the camera phones home by default.

## 5. Alternative firmware

OpenIPC knows both the SoC (`gk7202v300`) and this module
([IPG-G4-WR-BL](https://openipc.org/cameras/boards/xiongmai-ipg-g4-wr-bl)), and
has `openipc.gk7202v300-nor-lite` builds. For this board, however, the registry
says *stay on stock*: there is no coupler image for device id `000759R5` and no
ready transition path.

What switching would cost:

| Loss | Why |
|---|---|
| Network | the only interface is the SSV6355 Wi-Fi; if the driver doesn't come up, only UART remains |
| PTZ | the head is driven by `XmMotor.ko`; OpenIPC has no support for it |
| IR-cut and light | tied to GPIO and a photoresistor, would need manual configuration |
| Audio | G.711A through the vendor media stack |
| MAC and sensor calibration | stored in the crypto block at the end of `boot`; overwriting it yields a default MAC and a camera that no longer encodes properly |
| Fast rollback | only UART + TFTP with `update.img`, i.e. opening the housing |

Conclusion from this work: keep the stock firmware. The useful result — full P
and T control including presets — is reachable over DVRIP without touching the
image.

## 6. What this means for a custom client

* Everything the app does with PTZ is available over TCP 34567; no shell needed.
* There is no way to read head coordinates over the network, so verification of
  a preset return has to be visual (`xiongmai-ptz check`).
* The head has no encoder: position is a step counter, and it drifts when steps
  are lost (hitting a stop, being turned by hand). After such an event only a
  recalibration (reboot) restores the reference.
