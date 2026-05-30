# Release Notes

## Current export

- GUI: `BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v40.py`
- Arduino Mega / Zaber: `Behavior_MobileSpouts_Zaber_Arduino_v32.ino`
- Teensy / SMC02: `Behavior_MobileSpouts_Teensy_v32.ino`

## GUI highlights

- Added verification after adaptive apply to confirm `adapt.use_per_position` actually stuck on the device.
- Includes recent geometry, visualization, coordinate-load, and adaptive-difficulty updates.

## Firmware highlights

- Manual reward can bypass auto-hold for a single pulse while leaving auto-hold active afterward.
- Current position events, sync pulses, cue events, and task events remain logged through the existing serial/event pipeline.

## Packaging notes

- This repo is intended as a lightweight source snapshot of the latest GUI and firmware files.
- Versioned filenames are preserved from the active lab workflow.
