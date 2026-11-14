# Changelog

## 0.3.0 — 2026-11-14

### Added
- Preset auto-discovery: goto buttons are published for the slots the camera
  actually has (`Uart.PTZPreset`), refreshed on every poll — presets created in
  the vendor app appear in Home Assistant without a restart.
- Preset management from Home Assistant: a slot select, **Save preset**
  (`SetPreset`) and **Delete preset** (`ClearPreset`) buttons, and a **Preset
  slots** sensor (`count` state; `slots`, `free`, `managed`, `reserved`,
  `last_action`, `last_error` attributes).
- New optional camera keys: `auto_presets` (default `true`), `preset_slots`
  (default `[1, 2, 3, 4]`, the save/delete whitelist), `max_presets`
  (default `12`); new label keys `preset_auto_prefix`, `preset_slot`,
  `preset_save`, `preset_delete`, `preset_slots`.
- MQTT contract `preset_ctl/*`: `slot/set`, `save/set`, `delete/set` commands;
  retained `slot/state` (restored after a restart) and `slots/state` JSON.

### Changed
- Invalid preset topics (non-numeric or reserved slot) are ignored with a
  warning instead of raising `ValueError` and marking the camera offline.
- Discovery is republished only when the preset button set or select options
  change; a deleted button is removed with an empty retained payload on its
  exact discovery topic.

### Fixed
- `xiongmai-ptz mark` crashed with `AttributeError: 'Namespace' object has no
  attribute 'stream'` — the `--stream` option existed only on `check` while the
  shared frame-grabbing path reads it for both; `mark` now accepts `--stream`.

### Compatibility
- 0.2.2 configs load unchanged; `auto_presets: false` keeps the exact 0.2.2
  entity set; existing `unique_id`s and the `preset/<slot>/set` topic are
  unchanged. Reserved slots `100/128/141/250/255` are never published, saved
  or cleared (config, router and `check_slot` protection).

## 0.2.2
- Fixed the MQTT discovery `availability` shape (list of mappings, not topic
  strings) so Home Assistant accepts new devices.

## 0.2.1
- The login user is taken from the secret file (fixes `Ret=205` with
  `secret_file`).

## 0.2.0
- Several cameras per process: per-camera poll thread, command lock, MQTT
  topics and Home Assistant device.

## 0.1.0
- First release: container image, DVRIP PTZ / OSD / volume / white light,
  localised entity names.