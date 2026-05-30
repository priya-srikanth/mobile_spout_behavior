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
- `RELEASE_NOTES.md`
  - Short notes describing the current exported versions.

## Current files

- `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v40.py`
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v32.ino`
- `firmware/teensy_smc02/Behavior_MobileSpouts_Teensy_v32.ino`

## Version notes

- GUI `v40`
  - Includes the adaptive-scope verification safeguard so per-position adaptive apply is checked on-device after sending settings.
- Arduino Mega / Zaber firmware `v32`
  - Includes the manual-reward bypass fix so manual reward can transiently bypass auto-hold for one pulse.
- Teensy / SMC02 firmware `v32`
  - Matches the same manual-reward bypass behavior as the Mega / Zaber firmware.

## Suggested next cleanup

- Rename the main files to stable non-versioned names once the current versions are settled.
- Add a small protocol / wiring note for TTL outputs and sync lines.
- Add example config files or mouse profiles if you want the repo to be a fuller handoff package.
