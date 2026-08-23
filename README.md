# mobile_spout_behavior

Mobile spout behavior GUI and firmware for the three rigs currently running the task.

Widefield note as of August 7, 2026: the widefield rig is currently running the Arduino Mega / Zaber `v36` firmware, not `v37`.

| Rig | Location / imaging | MCU | Motion backend |
|---|---|---|---|
| **2pRAM** | two-photon room, **Teensy shared with a co-tenant 2-spout task** | Teensy 4.1 | SMC02, button emulation |
| **GB219** | behavior room / photometry | Teensy 4.1 (dedicated) | SMC02, button emulation |
| **Widefield** | widefield microscope | Arduino Mega | Zaber X-AS01 shield, serial |

All three run the same task and speak the same serial protocol, so one GUI drives all of them.

## Repository layout

- `gui/` — Python GUI used to run the task.
- `firmware/teensy_smc02/` — Teensy / SMC02 sketches. From `v37` there are two builds from one source, selected by a `#define` at the top of the file.
- `firmware/arduino_zaber/` — Arduino Mega / Zaber sketch.
- `tools/` — bench utilities that are not part of a task build.
- `docs/` — printable bench references.
- `WIRING_NOTES.md` — per-rig pinouts and DAQ-facing signal meanings, derived from the firmware source.
- `RELEASE_NOTES.md` — what changed in each export.

## Current files

- `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v50.py` — stable `v47`-line GUI with one-row-per-trial logging, lick-based auto-hold resume, and coordinate-only profile label refresh
- `firmware/teensy_smc02/Behavior_MobileSpouts_2pRAM_Teensy_v37/`
- `firmware/teensy_smc02/Behavior_MobileSpouts_GB219_Teensy_v37/`
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v36/` — current widefield rig build
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v39/` — `v38`-based Zaber build with any-lick auto-hold release
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v37/`
- `tools/LickScan_Teensy/` — read-only pin scanner for identifying lick lines safely
- `docs/Wiring_2pRAM_Teensy_v37.html` — printable bench wiring sheet for the shared-board rig

## The two Teensy builds

`Behavior_MobileSpouts_2pRAM_Teensy_v37` and `Behavior_MobileSpouts_GB219_Teensy_v37` are **byte-identical apart from one line**:

```cpp
#define RIG_2PRAM 1     // shared Teensy on the breakout board, 2-photon room
#define RIG_GB219 0     // dedicated Teensy, behavior room / photometry rig
```

They are committed as two ready-to-flash sketches so neither rig can be flashed with the wrong pin map by accident. If you edit one, regenerate the other by flipping those two lines rather than hand-editing both, or they will drift.

## Shared-board constraint (2pRAM only)

The 2pRAM Teensy is shared with a colleague's 2-spout task; each of you flashes your own firmware onto the same physical board. Two rules make that safe, and both are documented in `WIRING_NOTES.md`:

1. **SMC02 motor lines must live on pins 25–39.** Those pins are never declared in the co-tenant sketch, so they stay high-Z (= "button released") when his firmware runs. Any pin in 1–24 is actively driven by his firmware, and a driven-LOW line reads as a pressed button, which would command the stages during his sessions.
2. **Pins 1, 4, 9, 13, 15, 21, 22, 40, 41 are never configured** by this firmware. They belong to the co-tenant task (right solenoid, right lick detector, opto, second speaker, background light, external sync in).

## Suggested next cleanup

- Rename to stable non-versioned filenames once v37 is settled on all three rigs.
- Add a document mapping MCU pins onto DAQ channel names, camera GPIO, and detector/solenoid terminals.
- Add example config files or mouse profiles for a fuller handoff package.
