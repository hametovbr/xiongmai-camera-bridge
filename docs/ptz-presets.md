# PTZ presets: how "save point / return to it" actually works

This is the part that took the longest to get right, because the camera accepts
almost every PTZ command with `Ret: 100` while doing nothing. What follows was
established by disassembling the camera's own libraries and then confirming
every claim on a live head.

## 1. Path inside the camera

```
DVRIP 1400 OPPTZControl
  -> App (CPtz object, numeric ops: SetPreset 18, ClearPreset 19, GotoPreset 20)
  -> Squirrel scripts (MainPtz -> PELCOD.nut)
  -> PtzWrite() -> XmDvr_Motor_write() -> motor_write() -> pelco_parse()
  -> motor_setpreset(N) / motor_clearonepreset(N) / motor_gotopreset(N)
  -> ioctl /dev/motor -> XmMotor.ko
```

The JSON is converted into a **Pelco-D frame** inside the camera and then parsed
back by `libXmDvr.so`; the internal stepper head is driven from that parse
result. There is no external RS485 head involved, even though `Uart` says
`PELCOD`.

In the Pelco-D frame the command byte selects the operation (`0x03` set,
`0x05` clear, `0x07` goto) and the slot number is taken from the byte at
offset 5 — one byte, no offset, no remapping. So a DVRIP `Preset` value of `N`
becomes `motor_*preset(N)` directly.

## 2. Where presets are stored

Not in the config (`Uart.PTZPreset` holds only `Id` and `PresetName`) but in
`libXmDvr.so`, in a global table at ELF address `0x4C5FC`, `0xFF0` = 4080 bytes
= **255 entries of 16 bytes**:

```c
/* reconstructed from ARM code; the meaning of the third field is unknown */
struct Preset {
    int32_t x;        /* +0 step counter X read from the driver */
    int32_t y;        /* +4 step counter Y */
    int32_t unknown;  /* +8 unused by ordinary set/goto */
    int32_t valid;    /* +12 non-zero: the slot exists */
};
/* entry N lives at base + 16*N */
```

X and Y are plain step counts; nothing is packed into one number. The diagnostic
string `preset[0].x=%d ... preset[1].x=%d` in the library is a two-entry local
print from a test helper, not a statement that the table has two slots.

The table is persisted to a file (`PresetPos`) in the writable directory:
`motor_setpreset` writes 4080 bytes and fsyncs, `motor_initpreset` (called from
`XmDvr_Motor_createThread`) loads it at startup. No network call is needed to
initialise the table — a common wrong guess is that `SetPreset` must be preceded
by some "init preset" command. It must not.

**Slot limits:** usable range is `0..254`. The JSON parser and the Squirrel
layer accept `255`, but entry 255 starts past the end of the table and overlaps
the scan data, so do not use it. Four numbers are used internally by the
firmware: `100` (auto-tracking return point), `128` (last position saved by a
background thread every 30 s), `141` and `250` (calibration/scan helpers).
`xiongmai-ptz` refuses all of them.

## 3. The functions

| Function | ELF address | What it does |
|---|---|---|
| `motor_setpreset(N)` | `0x1A614` | ioctl status → store X/Y in slot N, `valid=1`, write `PresetPos` (`w+`, 4080 bytes, fsync) |
| `motor_gotopreset(N)` | `0x1AA28` | if `valid` is 0 return -1 (`Preset not exist`); fill `{cmd=17, x, y}`; poll status until `state == 0xFFFF` (idle); send ioctl `0x40184D01` |
| `motor_clearonepreset(N)` | `0x1B4D4` | check `valid`, zero the entry, rewrite `PresetPos` |
| `motor_clearpreset()` | `0x1B300` | wipe the whole table — suspicious: `memset` of `0x3FC` bytes followed by `fwrite` of `4*0x3FC`; **do not call** |
| `LibXmDvr_Motor_gotoPreset(x,y,sx,sy)` | `0x1C744` | builds `{17,x,y,sx,sy}` and issues the ioctl — takes coordinates, **not** a slot number; easy to confuse with the above |

`motor_gotopreset` waits for idle in a loop with no visible timeout, so a client
that waits for a reply can time out while the camera is still working. It also
does not send an explicit stop first. The speed fields of the command buffer are
not set by the goto path; whatever the previous command left there is reused.

## 4. Why a single `GotoPreset` misses

The head has **no encoder**: its position is a software step counter. When the
direction of travel changes, the first steps only take up the mechanical
backlash, yet the counter counts them. So the same slot is reached with an error
that depends on the direction the head arrived from.

Measured on a 640x360 sub-stream with phase correlation (pixel offsets after
`GotoPreset`):

| Came from | Offset |
|---|---|
| below / right | `(0, 0)` |
| left | `(13, -2)` |
| above | `(0, -64)` … `(0, -68)` |
| big move left+up | `(13, -67)` |
| big move right+down | `(-3, 5)` |

The head does finish moving — the offset stays constant for 37+ seconds after
the command — so this is not "the command was still running". Auto-tracking is
off (`Detect.DetectTrack.Enable = 0`), and a still scene does not drift, so
nothing else is moving the head between measurements.

## 5. The recipe used by this bridge

Always reach the slot from the same side:

```text
GotoPreset N
wait ~9 s                     # the reply arrives long before the head stops
DirectionRightDown (0.4 s)    # load the gears from below-right
stop  (DirectionRightDown, Preset: -1)
wait 1.5 s
GotoPreset N
```

Result: offset `<= 6 px` of 640x360 (about 1-2 % of the frame) regardless of the
previous move, including large two-axis moves and a round trip through MQTT.
Without the extra impulse the error is up to 68 px, which is clearly visible.

The same idea helps on save: move slightly away from the point and back before
`SetPreset`, so the stored counters are taken with the gears loaded from the
same side. `xiongmai-ptz save` does this by default (`--plain` disables it).

Slot types that were saved without this — typically the ones made in the vendor
app — may be a few dozen pixels off; re-saving them fixes the view.

## 6. How to verify a preset

`Ret: 100` is not evidence. Compare frames:

```
xiongmai-ptz save 5 --label "Door"
xiongmai-ptz mark 5        # reference frame of the saved view
# ... move the head somewhere else ...
xiongmai-ptz goto 5
xiongmai-ptz check 5       # prints the offset in pixels and a verdict
```

`check` grabs three frames and reports `(dx, dy, peak)` from phase correlation.
A same-scene pair gives a peak around 0.7; below ~0.1 the views do not overlap
at all. Mean absolute difference over edge maps is *not* a good metric here: on
a dark scene it stays small even for a clearly shifted picture.

## 7. Known blind spots

* Accuracy after many large moves in a row (when the head hits a mechanical
  stop and skips steps) has not been measured; counters drift in that case.
* Behaviour across a camera reboot is not verified on a live head.
* There is no way over DVRIP to read the current coordinates or the calibration
  state (`Camera.MotorCtrl` → `Ret: 607`, `Uart.PTZ` has no position fields).

## 8. Save/delete from Home Assistant (v0.3.0)

The bridge exposes `SetPreset`/`ClearPreset` through the `preset_ctl` MQTT
topics and HA entities (select + save/delete buttons + slots sensor, see
[configuration.md](configuration.md)):

* **save** runs the same gear-loading approach as goto (a tiny move away and
  back before `SetPreset`), so the stored counters match what `goto_preset()`
  reproduces — a point saved from HA comes back within the same ≤ 6 px;
* **delete** sends `ClearPreset` and re-reads `Uart.PTZPreset`; clearing an
  empty slot is a success (idempotent). When the slot is neither occupied nor
  defined in `presets`, its HA button is removed with an empty retained
  discovery payload;
* the occupied set is re-read on every poll, so presets created in the vendor
  app appear automatically. Slots saved by the app without the approach may
  still be a few dozen pixels off — re-save them from HA (or
  `xiongmai-ptz save`) to fix the view;
* reserved slots `100/128/141/250` and out-of-table `255` are rejected at three
  levels: config load, the MQTT router and `check_slot()` — `SetPreset` /
  `ClearPreset` are never sent for them;
* slot names are never written to the camera (`SetConfig Uart` corrupts them);
  labels live in `cameras.yml`, auto slots are named `<preset_auto_prefix> N`.
