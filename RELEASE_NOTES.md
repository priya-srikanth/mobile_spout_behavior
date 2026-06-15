# Release Notes

## Current export

- GUI: `BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v40.py`
- Arduino Mega / Zaber: `Behavior_MobileSpouts_Zaber_Arduino_v35/Behavior_MobileSpouts_Zaber_Arduino_v35.ino`
- Teensy / SMC02: `Behavior_MobileSpouts_Teensy_v35.ino`

## GUI highlights

- Added verification after adaptive apply to confirm `adapt.use_per_position` actually stuck on the device.
- Includes recent geometry, visualization, coordinate-load, and adaptive-difficulty updates.

## Firmware highlights

- Manual reward can bypass auto-hold for a single pulse while leaving auto-hold active afterward.
- Current position events, sync pulses, cue events, and task events remain logged through the existing serial/event pipeline.
- Arduino pin `6` remains `trial_start TTL`, and pin `9` is now `trial_stop TTL` at `dock_start`.
- Arduino `v35` keeps a matching `trial_stop_ttl` serial event for scope/console correlation.
- Teensy pin `6` remains `cue TTL`, pin `10` remains `trial_start TTL`, and pin `4` is now `trial_stop TTL` at `dock_start`.
- Firmware `v35` removes the hidden `dock_wick_ms` addition from ITI timing, so ITI duration now matches the GUI fields: `iti_min_ms + random(0..iti_jitter_ms)`.

## Packaging notes

- This repo is intended as a lightweight source snapshot of the latest GUI and firmware files.
- Versioned filenames are preserved from the active lab workflow.
