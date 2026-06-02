# Wiring Notes

These notes are based on the current source files in this repo:

- `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v40.py`
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v34/Behavior_MobileSpouts_Zaber_Arduino_v34.ino`
- `firmware/teensy_smc02/Behavior_MobileSpouts_Teensy_v34.ino`

Older pinout notes in `BehaviorRig` were useful as cross-checks, but this document is intended to match the current Arduino `v34` and Teensy `v34` firmware files.

## Signal meanings

Across both backends, the DAQ-facing digital signals are intended to mean:

- `sync`: irregular sync pulse train for alignment across systems
- `cue TTL`: cue onset marker
- `reward TTL`: reward delivery marker
- `position bits + position strobe`: current target position code after the spout reaches target
- `trial_stop TTL`: trial ended / spout begins moving away from the target toward dock
- `trial_start` event: trial initiated / move-to-target begins

Important note:

- `trial_start` is not the same as "spout arrived at target"
- `position strobe` is the arrival marker for the coded target position
- on the current Teensy backend, pin `6` is `cue TTL`, pin `10` is `trial_start TTL`, and pin `4` is `trial_stop TTL`
- `trial_stop TTL` pulses at `dock_start`, not after docking completes

## Arduino Mega / Zaber (`Behavior_MobileSpouts_Zaber_Arduino_v34/Behavior_MobileSpouts_Zaber_Arduino_v34.ino`)

### Task / DAQ / cue I/O

| Arduino pin | Signal | Direction | Notes |
|---|---|---|---|
| `D2` | `PIN_LICK_IN` | input | Digital lick input from detector board |
| `D3` | `PIN_TTL_REWARD` | output | Reward TTL to DAQ |
| `D4` | `PIN_SYNC` | output | Sync TTL to DAQ / cameras |
| `D5` | `PIN_CUE_TTL` | output | Cue onset TTL |
| `D6` | `PIN_TRIAL_START` | output | Trial-start TTL |
| `D7` | `PIN_STARTSTOP_BUTTON` | input | Physical start/stop button input |
| `D8` | `PIN_SOLENOID` | output | Solenoid valve control |
| `D9` | `PIN_TTL_TRIAL_STOP` | output | Trial-stop TTL at `dock_start` |
| `D11` | `PIN_CUE_AUDIO` | output | Audio tone output to amplifier |

### Position code outputs

| Arduino pin | Signal | Direction | Notes |
|---|---|---|---|
| `D23` | `PIN_TTL_POS0` | output | Position code bit 0 |
| `D24` | `PIN_TTL_POS1` | output | Position code bit 1 |
| `D25` | `PIN_TTL_POS2` | output | Position code bit 2 |
| `D26` | `PIN_TTL_POS_STB` | output | Position code strobe |

### Mega-specific notes

- The current firmware expects a digital lick signal and sets `PIN_LICK_IN` as `INPUT_PULLUP`.
- In the current defaults, lick is treated as active-low.
- The cue has two separate outputs:
  - `D5` cue TTL for DAQ/event marking
  - `D11` actual audio waveform to the amplifier/speaker
- The position code is 3 bits plus a strobe:
  - bits are written first
  - then the strobe is pulsed
- Zaber axis assignment is not a fixed wiring pinout on the Mega in the same sense as the Teensy motor-control lines; motion goes out over serial to the Zaber chain.

## Teensy / SMC02 (`Behavior_MobileSpouts_Teensy_v34.ino`)

### Task / DAQ / cue I/O

| Teensy pin | Signal | Direction | Notes |
|---|---|---|---|
| `2` | `PIN_SYNC_OUT` | output | Sync TTL to DAQ / cameras |
| `3` | `PIN_SPEAKER` | output | Audio PWM cue waveform |
| `4` | `PIN_TTL_TRIAL_STOP` | output | Trial-stop TTL at `dock_start` |
| `5` | `PIN_REWARD_LEFT_SOLENOID` | output | Solenoid valve control |
| `6` | `PIN_CUE_TTL` | output | Cue onset TTL |
| `10` | `PIN_TTL_TRIAL` | output | Trial-start TTL |
| `15` | `PIN_LICK_LEFT_IN` | input | Digital lick input |
| `19` | `PIN_REWARD_LEFT_INDICATOR` | output | Reward TTL / indicator pulse |
| `14` | `PIN_TTL_POS0` | output | Position code bit 0 |
| `40` | `PIN_TTL_POS1` | output | Position code bit 1 |
| `41` | `PIN_TTL_POS2` | output | Position code bit 2 |
| `20` | `PIN_TTL_POS_STB` | output | Position code strobe |

### SMC02 motor-control pins

| Axis | CW | CCW | STOP |
|---|---:|---:|---:|
| X | `7` | `8` | `9` |
| Y | `23` | `22` | `21` |
| Z | `18` | `17` | `16` |

### Teensy / SMC02 control method

The current sketch still uses the direct "button emulation" control scheme:

- released state: `pinMode(INPUT)` = high impedance
- pressed/active state: `digitalWrite(LOW)` then `pinMode(OUTPUT)` = pull line to ground

That means the Teensy control outputs are intended to behave like active-low closures to the SMC02 control common.

### Teensy-specific notes

- `USE_HOME_SWITCHES = false` in the current sketch.
- There are no active home-switch input lines in the current exported firmware.
- The current firmware is single-solenoid / single-lick-input:
  - no right solenoid
  - no right lick input
- Like the Mega build, the position code is 3 bits plus a strobe:
  - bits are written first
  - then the strobe is pulsed

## Suggested DAQ wiring set

If you want a compact set of behavior-alignment lines on the DAQ, the highest-value lines are:

- `sync`
- `trial_start`
- `trial_stop`
- `cue TTL`
- `reward TTL`
- `position bit 0`
- `position bit 1`
- `position bit 2`
- `position strobe`
- `lick detector output` directly from the detector board

That gives you:

- trial timing
- cue timing
- reward timing
- target-position decoding
- a cross-system sync fingerprint
- better lick timing than relying on Arduino/Teensy event timestamps alone

## Cautions

- These notes document firmware pin assignments, not the full bench wiring topology.
- The actual detector board, DAQ terminal, amplifier, and solenoid power wiring should still be checked against your rig.
- If you want, the next useful addition would be a second document mapping these MCU pins onto:
  - DAQ channel names
  - camera GPIO
  - lick detector board terminals
  - solenoid driver / valve wiring
