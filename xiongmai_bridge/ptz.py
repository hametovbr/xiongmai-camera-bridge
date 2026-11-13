"""Pan/tilt helpers for the internal stepper head (no encoder).

Findings this module relies on (see docs/ptz-presets.md):

* The DVRIP command name is case-sensitive. The camera answers Ret 100 to any
  name, but only "GotoPreset" (lower-case "t") moves the head. "GoToPreset" is
  the internal Squirrel name and is silently ignored.
* Direction* commands move continuously until stopped with the same command
  and Preset = -1. "Step" is the speed (0..8 on the motor), not a step count.
* SetPreset N stores the current X/Y step counters in slot N (direct index).
* The head has mechanical backlash: GotoPreset lands with an error that depends
  on the direction of approach (up to ~66 px of the 640x360 sub-stream when the
  head arrives from above, ~0 when it arrives from below/right). Approaching the
  final point always from the same side brings the error down to <= 6 px.
"""

import time

DIRECTIONS = ("DirectionUp", "DirectionDown", "DirectionLeft", "DirectionRight",
              "DirectionLeftUp", "DirectionLeftDown", "DirectionRightUp",
              "DirectionRightDown")

# Slot 255 is accepted by the parser but lies outside the 255-entry table
# (0..254) and overlaps other data. 100, 128, 141 and 250 are used internally
# by the firmware (tracking return point, last position, calibration, scan).
RESERVED_SLOTS = frozenset({100, 128, 141, 250, 255})


def check_slot(slot: int) -> int:
    slot = int(slot)
    if not 1 <= slot <= 254 or slot in RESERVED_SLOTS:
        raise ValueError(f"preset slot {slot} is reserved or out of range (use 1..254, "
                         f"not {sorted(RESERVED_SLOTS)})")
    return slot


def move(cam, direction: str, seconds: float, speed: int = 5, settle: float = 1.5):
    """Move for a bounded time, then stop explicitly."""
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction {direction}")
    cam.ptz(direction, preset=0, step=speed)
    try:
        time.sleep(seconds)
    finally:
        cam.ptz(direction, preset=-1, step=speed)
    time.sleep(settle)


def save_preset(cam, slot: int, approach: str = "DirectionRightDown",
                nudge: float = 0.4):
    """Store the current position in a slot.

    With an approach direction the head first makes a tiny move back and forth
    so that the gears are loaded from the same side that goto_preset() uses.
    Pass approach=None to store the position exactly as it is.
    """
    slot = check_slot(slot)
    if approach:
        opposite = {"DirectionRightDown": "DirectionLeftUp",
                    "DirectionLeftUp": "DirectionRightDown",
                    "DirectionRightUp": "DirectionLeftDown",
                    "DirectionLeftDown": "DirectionRightUp"}.get(approach)
        if opposite:
            move(cam, opposite, nudge)
            move(cam, approach, nudge)
    return cam.ptz("SetPreset", preset=slot, step=0)


def goto_preset(cam, slot: int, approach: str = "DirectionRightDown",
                settle: float = 9.0, nudge: float = 0.4):
    """Return to a slot with backlash compensation.

    GotoPreset -> wait for the move -> short move in the approach direction ->
    GotoPreset again, so the final approach always comes from the same side.
    approach=None sends a single GotoPreset.
    """
    slot = check_slot(slot)
    cam.ptz("GotoPreset", preset=slot, step=0)
    if not approach:
        return True
    time.sleep(settle)
    move(cam, approach, nudge)
    return cam.ptz("GotoPreset", preset=slot, step=0)


def clear_preset(cam, slot: int):
    return cam.ptz("ClearPreset", preset=check_slot(slot), step=0)


def list_slots(cam):
    """Slot ids known to the config (names only; coordinates are not exposed)."""
    uart = cam.get_config("Uart")
    groups = uart.get("PTZPreset") or [[]]
    return [p.get("Id") for p in groups[0]]
