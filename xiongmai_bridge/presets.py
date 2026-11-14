"""Preset slot math and MQTT state payloads (v0.3.0).

Pure helpers: no camera sessions, no MQTT. CameraWorker orchestrates (locks,
DVRIP sessions, publishing); these functions turn the config plus the camera's
occupied slots into the published button set, entity labels, select options
and the retained ``preset_ctl/slots/state`` JSON.

Slot rules are shared with ptz.check_slot and the config loader: usable slots
are 1..254 without the firmware-reserved 100/128/141/250; 255 lies outside the
255-entry table (see docs/ptz-presets.md).
"""

from datetime import datetime, timezone

from .ptz import RESERVED_SLOTS

# Firmware-internal slots as reported in slots/state. 255 is outside the table
# rather than firmware-reserved, so the payload lists only these four.
SERVICE_SLOTS = (100, 128, 141, 250)


def valid_slots(occupied):
    """Occupied slot ids reduced to usable integers (drops reserved/junk)."""
    result = set()
    for slot in occupied or []:
        try:
            number = int(slot)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 254 and number not in RESERVED_SLOTS:
            result.add(number)
    return result


def published_slots(cfg, occupied):
    """Goto buttons to publish: config presets + valid occupied slots, capped.

    Config presets are published even when the camera slot is empty (goto is
    allowed for them); the whitelist only gates save/delete. The cap keeps the
    lowest slot numbers, deterministically.
    """
    slots = set(cfg.presets) | valid_slots(occupied)
    return sorted(slots)[:cfg.max_presets]


def slot_label(cfg, slot):
    """Config label wins; an auto slot is named '<preset_auto_prefix> <slot>'."""
    if slot in cfg.presets:
        return cfg.presets[slot]
    return f"{cfg.label('preset_auto_prefix', 'Preset')} {slot}"


def select_options(cfg, occupied):
    """Select options: managed whitelist plus occupied slots, as strings."""
    return [str(s) for s in sorted(set(cfg.preset_slots) | valid_slots(occupied))]


def utc_ts():
    """ISO-8601 UTC timestamp for last_action/last_error."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slots_state(cfg, occupied, last_action=None, last_error=None):
    """The retained preset_ctl/slots/state payload.

    ``slots`` reflects the camera truth (occupied only); ``free`` is the
    whitelist minus occupied; ``managed`` is the save/delete whitelist.
    """
    occupied = valid_slots(occupied)
    managed = sorted(cfg.preset_slots)
    return {
        "count": len(occupied),
        "slots": {str(s): slot_label(cfg, s) for s in sorted(occupied)},
        "free": [s for s in managed if s not in occupied],
        "managed": managed,
        "reserved": list(SERVICE_SLOTS),
        "last_action": last_action,
        "last_error": last_error,
    }