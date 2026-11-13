# Firmware notes: what is inside the stock image

The camera runs a vendor Linux image (`V5.00.R02.000759R5`). It can be downloaded
from the vendor OTA server and unpacked locally; nothing here was written to the
camera. Analysis is static (ARM disassembly of `App`, `libXmDvr.so`,
`XmMotor.ko`, plus the Squirrel scripts) and was confirmed on a live head where
stated.

Addresses below are **ELF virtual addresses** of the specific build analysed
(8 MB NOR image, device id `000759R5`). Under ASLR the loaded addresses differ,
and other firmware versions will differ too. SHA-256 of the analysed binaries:

```
7b77bf5119ab88ee26f06d8fe32ff42eb554ac08b61564dba741eb2773ab5f55  libXmDvr.so
66936d44ee1b9a0cb29ccd0b4469466ee2cf1a9f37d8b5b9f7d05775506b51c8  App.bin
e5c17ddfcace089d331e348c785b5a7250a6e07751655210389be48c3d7f6a17  XmMotor.ko
```

## 1. Layout

| Item | Value |
|---|---|
| Flash | 8 MB SPI NOR |
| Partitions | `boot 0x40000`, `romfs 0x2E0000`, `usr 0x440000`, `web 0x20000`, `custom 0x30000`, `mtd 0x50000` |
| Bootloader | U-Boot 2016.11, console `ttyAMA0 115200`, `ipaddr=192.168.1.10`, `serverip=192.168.1.107` |
| TFTP helpers in env | `da/de/dr/du/dc/dw/up` — flash a partition over TFTP, `up` pulls `update.img` |
| Init | uClibc-based rootfs, `/bin/App` is the main daemon |
| Sensor JSON | encrypted in the image (`XMFV1001…`); sensor type and MAC live in the "crypto" block at the end of `boot` |

## 2. Boot chain relevant to PTZ

```
/bin/App
  -> libXmDvr.so: LibXmDvr_Motor_open(), XmDvr_Motor_createThread()
       -> motor_initpreset()      loads PresetPos table
       -> hand_motor_pointer      background thread (see below)
       -> scanMonitor
  -> /dev/motor (XmMotor.ko)      ioctl interface to the stepper driver
```

`hand_motor_pointer` sleeps 5 s, then polls the driver status up to 45 times
while `state == 19` (the startup self-test/calibration), one second per poll.
When it gives up it logs `The Motor is still autotest`. That message comes from
this thread, **not** from `motor_setpreset`/`motor_gotopreset`, so a custom
client does not have to "wait for autotest" before a preset command — it only
has to wait for the head to physically stop moving. There is no DVRIP call that
reports the driver calibration flag.

The same thread also saves the current position into the internal slot 128 every
30 seconds of idle, which is why 128 must not be used as a user slot.

## 3. `libXmDvr.so`

Stripped, but the dynamic symbol table keeps the function names, which makes the
PTZ path readable. Key exports:

| Symbol | Address | Role |
|---|---|---|
| `PtzWrite` | `0x22544` | entry from `App`; chooses the internal motor branch or UART |
| `motor_write` | `0x1BAAC` | accepts Pelco bytes starting with `0xFF`/`0xA0`, calls `pelco_parse` |
| `pelco_parse` | `0x1B710` | decodes Pelco-D/P; `buffer[3] & 1` selects the preset branch (`3` set, `5` clear, `7` goto), slot from `buffer[5]` |
| `motor_setpreset` | `0x1A614` | store X/Y, persist `PresetPos` |
| `motor_gotopreset` | `0x1AA28` | wait for idle, send goto |
| `motor_clearonepreset` | `0x1B4D4` | clear one slot and persist |
| `motor_clearpreset` | `0x1B300` | clear all (avoid, see docs/ptz-presets.md) |
| `motor_initpreset` | `0x1BC7C` | load the table at startup |
| `LibXmDvr_Motor_gotoPreset` | `0x1C744` | goto by coordinates |
| `LibXmDvr_Motor_moveSteps` | `0x1CA98` | relative move |
| `LibXmDvr_Motor_getPostion` | `0x1C69C` | read the driver counters |
| `PtzGetPresetAbility` | `0x22C50` | returns a product flag from the parsed JSON config |

`m_preset` table: `0x4C5FC`, 255 entries × 16 bytes (details and struct in
`docs/ptz-presets.md`). The Pelco path is what drives the *internal* head — the
library is a Pelco emulation that the camera talks to itself.

Interesting failure strings and where they belong:

| String | Function |
|---|---|
| `Preset not exist` | `motor_gotopreset` when `valid == 0` |
| `The Motor is still autotest` | `hand_motor_pointer` background thread |
| `presetnum = %d, value = %06x init = %d` | `motor_gotopreset` debug print of the `valid` field |
| `The current position is the lookout position` | `LibXmDvr_Motor_isTrackPostion` (auto-tracking) |

Note the misleading `*****************set m_preset 8192` banner: the code right
after it zeroes the table with `memset`, it does not set coordinates to 8192.

## 4. `/dev/motor` (XmMotor.ko)

Misc device; `open()` is a plain counter, `read()` returns 0. All traffic is
ioctl, 24 bytes = `int[6]`:

| ioctl | Number | Buffer |
|---|---|---|
| `_IOW('M',1,24)` | `0x40184D01` | command `{cmd, a1, a2, a3, a4, a5}` |
| `_IOR('M',2,24)` | `0x80184D02` | status `{state, x, y, x_flags, y_flags, 0}` |
| `_IOR('M',3,24)` | `0x80184D03` | `{state, max_steps_x, max_steps_y, …}` |
| `_IOW('M',4,24)` | `0x80184D04` | writes `a5` into `g_motor+0x68`, purpose unclear |

`state == 0xFFFF` means idle; otherwise it is the number of the command being
executed. In `x_flags`, bit `0x04` means "axis moving", bit `0x10` means "startup
calibration finished" — the driver rejects commands until that bit is set.

Commands (`cmd`; direction is a bitmask: 1 left, 2 right, 4 up, 8 down):

| cmd | Meaning | Arguments |
|---|---|---|
| 1,2,4,8,5,6,9,10 | move | `a1`/`a2` speed X/Y (level 0..8), `a3`/`a4` steps X/Y (0 = module default, i.e. until stop) |
| 16 | scan between limits | speed, left X/Y, right X/Y |
| 17 | **goto absolute position** | `a1` = X, `a2` = Y in steps, `a3`/`a4` speed index 1..52 (0 = 52) |
| 18 | stop | — |
| 19 | calibration move | used only at startup |
| 20 | rewrite position counters without moving | `a1` = X, `a2` = Y |

16/17/19 are accepted only while idle. Commands are queued and executed by the
`motor_helper` kernel thread, so an ioctl returning does not mean the head moved.

Steps are half-step counts of an 8-phase coil table. Speed is an index into the
timer period table `tick_x[53]` (32958…4394 ticks) — a **larger index is faster**.
Levels 0..8 map through `speed_reflect_x = {1,1,7,12,17,22,27,32,34}` and
`speed_reflect_y = {1,1,7,10,12,17,22,27,32}`.

The driver keeps exactly **one** preset (`my_preset`); the multi-slot table used
by the app lives in `libXmDvr.so`. The text protocol via `write()` (≤64 bytes)
supports `stop`, `left N`, `right N`, `up N`, `down N`, diagonals, `setpreset`,
`preset`, `lscan`, `rscan`, `scan N`, where `N` is again a **speed**, not steps.
There is no "goto X Y" in the text protocol.

Module parameters worth knowing when reading logs:
`x_steps=3925`, `y_steps=1194` (default travel until stop), `x_stepmax`,
`y_stepmax`, `x_speed_limit`, `y_speed_limit`, `auto_test`, `gpio_pin[]`,
`g_useI2c`, `x_motor_new`, `y_motor_new`, `x_faster`, `y_faster`.

Endstops are polled and logged as `###XL=`, `###XR=`, `###YD=`, `###YU=`.
Because there is no encoder, hitting a stop or being turned by hand makes the
counters (and therefore all presets) drift until the next calibration.

## 5. Squirrel PTZ scripts

`/bin/Squirrel/ptz/` holds `MainPtz` plus protocol modules. `MainPtz` is a
dispatcher: `TranOperationArray` names the operations (`SetPreset`,
`GoToPreset`, `Reset`, `Flip`, `Menu*`, …), and the loaded module — `PELCOD.nut`
or `PELCOP.nut` — encodes them into Pelco-D/P bytes. Those scripts are where the
capital letters `GoToPreset` come from; the DVRIP command table in `App` uses
`GotoPreset`. The comments in those files are OEM-encoded, so read them as
latin-1.

## 6. `App` (the DVRIP daemon)

`App` handles the JSON protocol. Relevant places:

| Address | What |
|---|---|
| `0x167074` | `OPPTZControl` handler |
| `0x5CB124` | command-name table: `DirectionUp`=0 … `SetPreset`=0x11, `ClearPreset`=0x12, `GotoPreset`=0x13 |
| `0x167698` | `GotoPreset` branch → CPtz op 20 |
| `0x16758C` | `SetPreset`/`ClearPreset` branch → CPtz op 18/19 |
| `0x5CB410` | second name table (operation ids used by CPtz), includes `Position` = 0x1F |
| `0xE7B20` | CPtz command builder: switch on the op, calls Squirrel `setPreset`/`setSpeed`/… |
| `0x26138` | `CPtz` start wrapper → `PtzWrite` |

`App` never issues an ioctl to `/dev/motor` itself: all motor access is inside
`libXmDvr.so`. The only direct uses of `LibXmDvr_Motor_*`/`motor_gotopreset` in
`App` are in the auto-tracking threads (`TrackProc`, `TrackProcPed`), which
return the head to slot 100 when a person leaves the frame.

## 7. Auto-tracking and state restore

`Detect.DetectTrack` = `{Enable, ReturnTime, Sensitivity}`; when enabled, the
tracking threads move the head and eventually call `motor_gotopreset(100)`. If
you control the camera yourself, disable tracking — otherwise it will move the
head under you.

`General.ResumePtzState` = `{Enable, PtzState:[{PresetState:{Enable,PresetID}, TourState:{LineID,Running}}]}`
restores a preset after reboot. On the camera analysed both were disabled.
