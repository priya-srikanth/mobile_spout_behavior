# mobile_spout_behavior

Latest mobile spout behavior GUI and firmware files for the two supported hardware backends:

- Teensy + SMC02
- Arduino Mega + Zaber

## Repository layout

- `gui/`
  - Current Python GUI used to run the task.
- `firmware/arduino_zaber/`
  - Arduino Mega / Zaber sketch.
- `firmware/teensy_smc02/`
  - Teensy / SMC02 sketch.
- `WIRING_NOTES.md`
  - Current pinout and DAQ-facing signal notes derived from the exported firmware.
- `RELEASE_NOTES.md`
  - Short notes describing the current exported versions.

## Current files

- `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v40.py`
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v35/Behavior_MobileSpouts_Zaber_Arduino_v35.ino`
- `firmware/teensy_smc02/Behavior_MobileSpouts_Teensy_v35.ino`

## Version notes

- GUI `v40`
  - Includes the adaptive-scope verification safeguard so per-position adaptive apply is checked on-device after sending settings.
- Arduino Mega / Zaber firmware `v35`
  - Uses pin `6` for `trial_start TTL` and pin `9` for `trial_stop TTL`, with `trial_stop` pulsed at `dock_start`.
  - Keeps a `trial_stop_ttl` serial event at the same moment to simplify bench debugging.
- Teensy / SMC02 firmware `v35`
  - Keeps cue TTL on pin `6`, keeps trial-start TTL on pin `10`, and repurposes pin `4` to `trial_stop TTL`, pulsed at `dock_start`.
- Firmware `v35` for both backends removes the hidden `dock_wick_ms` addition from ITI timing; ITI is now exactly `iti_min_ms + random(0..iti_jitter_ms)`.

## Suggested next cleanup

- Rename the main files to stable non-versioned names once the current versions are settled.
- Add a small protocol / wiring note for TTL outputs and sync lines.
- Add example config files or mouse profiles if you want the repo to be a fuller handoff package.
