# Wiring Notes

Derived from the current source files in this repo:

- `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v44.py`
- `firmware/teensy_smc02/Behavior_MobileSpouts_2pRAM_Teensy_v37/`
- `firmware/teensy_smc02/Behavior_MobileSpouts_GB219_Teensy_v37/`
- `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v37/`

There are now **three rigs with three different pinouts**. The signal *meanings* are identical across all of them; only the pin numbers and the motion backend differ.

A printable bench version of the 2pRAM section is in `docs/Wiring_2pRAM_Teensy_v37.html`.

## Signal meanings (all rigs)

| Signal | Meaning |
|---|---|
| `sync` | Irregular pulse train for alignment across systems |
| `cue TTL` | **Gate**: rising = cue onset, falling = cue offset. Spans the whole tone |
| `reward TTL` | Reward delivery marker, short pulse |
| `position bits + strobe` | Target position code, latched on the strobe edge, emitted once the spout reaches target |
| `trial_stop TTL` | Trial ended / spout begins moving away toward dock. Pulses at `dock_start`, not after docking completes |
| `trial_start` event | Trial initiated / move-to-target begins. Serial event; a hardware TTL only on GB219 and the Zaber rig |

### Timing traps worth knowing

- **`trial_start` is not "spout arrived at target".** The position strobe is the arrival marker. On the SMC02 rigs the gap between them is the move duration, roughly 2–3 s at default calibration, and it **varies by several hundred ms across positions** because travel distance from dock depends on distance tier and azimuth. Aligning to the strobe as if it were trial onset injects a timing offset correlated with the variable the task manipulates.
- **2pRAM has no hardware trial-start line at all.** Take onset from `EVT name=trial_start` in the session log if you need it.
- **`emitPositionCode()` also runs on manual moves**, tagged `manual=1` in the serial log. A strobe recorded during setup is not necessarily a trial.
- **Cue TTL is a gate as of v37**, not a short pulse. Pipelines that counted fixed-width cue pulses need updating.
- The GUI's lick and position displays come from the serial protocol, not from these physical TTL outputs.

---

## 2pRAM — Teensy 4.1 on a **shared** breakout board

This Teensy is shared with a co-tenant 2-spout task (`teensy2spout.ino`); each user flashes their own firmware onto the same physical board.

### The shared-board rule

**SMC02 motor lines must live on pins 25–39.** Those pins are never declared in the co-tenant sketch, so they sit high-Z when his firmware runs, which is exactly "button released". Every pin in 1–24 is configured as an output and actively driven by his firmware, and because a pressed button is a pin pulled LOW, putting a motor line there means his sessions command your stages. His `ITIIndicatorPin` idles LOW for most of every trial, so an axis on that pin would be driven into its limit for hours.

**Pin 24 is not safe** despite being in the 24–39 range: it is his `DelayIndicatorPin` and he does configure it.

Every other signal is a plain TTL output in both firmwares. Only one firmware runs at a time, so there is no contention and nothing needs disconnecting when swapping.

### Never configured by this firmware

Pins `1`, `4`, `9`, `13`, `15`, `21`, `22`, `40`, `41` — right spout solenoid, right lick detector, opto control and opto mod, second speaker, background light, external sync input, opto reporter LED.

Pins `9` and `13` are additionally unreachable on this board: `13` is the Teensy's own onboard LED (no header exists) and `9` feeds an on-PCB level converter and RC filter, so only that chain's filtered output is exposed.

### Task / DAQ / cue I/O

| Teensy pin | Signal | Direction | Breakout net | Work needed |
|---|---|---|---|---|
| `2` | `PIN_SPEAKER` | output | Speaker L | none |
| `3` | `PIN_SYNC_OUT` | output | Sync BNC | none |
| `5` | `PIN_REWARD_LEFT_SOLENOID` | output | Left spout driver | none |
| `6` | `PIN_CUE_TTL` (gate) | output | SpeakerTTL BNC | none |
| `14` | `PIN_LICK_LEFT_IN` | input | Left lick detector | none |
| `19` | `PIN_REWARD_LEFT_INDICATOR` | output | LeftRewardIndicator BNC | none |
| `7` | `PIN_TTL_POS_STB` | output | TrialStartPin BNC | patch cable |
| `8` | `PIN_TTL_POS0` | output | ITIIndicator BNC | patch cable |
| `17` | `PIN_TTL_POS1` | output | LeftCueIndicator BNC | patch cable |
| `18` | `PIN_TTL_POS2` | output | RightCueIndicator BNC | patch cable |
| `20` | `PIN_TTL_TRIAL_STOP` | output | RightRewardIndicator BNC | patch cable |

`PIN_TTL_TRIAL` is `PIN_UNUSED` on this rig.

### SMC02 motor-control pins

| Axis | CW | CCW | STOP |
|---|---:|---:|---:|
| X | `25` | `26` | none |
| Y | `27` | `28` | none |
| Z | `29` | `30` | none |

Six new wires. STOP lines are omitted: in P02 mode the motor halts on CW/CCW release. Pins `31`–`39` stay free if the per-axis STOP lines are ever wanted back. Spare: `0`, `16`, `23`.

### 2pRAM-specific notes

- Left spout set only. The right spout stays physically connected for the co-tenant, so none of its nets may be repurposed.
- Lick input is `INPUT_PULLUP`, active-LOW (idles HIGH), matching the GB219-validated configuration. The co-tenant firmware uses `INPUT_PULLDOWN` on the same pin, implying *its* detector is active-high — if licks never register or register constantly, `SET lick.active_low=false` is the first thing to try. `tools/LickScan_Teensy/` settles it safely.
- A Teensy GND pin must be bonded to each SMC02's control ground, or "pressed" is undefined. Watch for ground loops: the Teensy ends up bonded to three stepper-driver grounds while also grounded to the DAQ through BNC shields.
- SMC02 button lines must sit at or below 3.3 V open-circuit. Teensy 4.1 is not 5 V tolerant and the idle state is a floating pin.

---

## GB219 — Teensy 4.1, dedicated

Pin map unchanged from `v36`. Only the `v37` logic fixes apply; no rewiring.

### Task / DAQ / cue I/O

| Teensy pin | Signal | Direction | Notes |
|---|---|---|---|
| `2` | `PIN_SYNC_OUT` | output | Sync TTL to DAQ / cameras |
| `3` | `PIN_SPEAKER` | output | Audio PWM cue waveform |
| `4` | `PIN_TTL_TRIAL_STOP` | output | Trial-stop TTL at `dock_start` |
| `5` | `PIN_REWARD_LEFT_SOLENOID` | output | Solenoid valve control |
| `6` | `PIN_CUE_TTL` (gate) | output | Cue onset/offset |
| `10` | `PIN_TTL_TRIAL` | output | Trial-start TTL |
| `15` | `PIN_LICK_LEFT_IN` | input | Digital lick input, `INPUT`, active-low |
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

### GB219-specific notes

- `USE_HOME_SWITCHES = false`; no home-switch inputs in this build.
- Single solenoid, single lick input: no right solenoid, no right lick input.
- The LabJack photometry rig records lick timing from the detector board wired directly to LabJack `FIO4`, not from a Teensy-generated lick TTL.

---

## Widefield — Arduino Mega / Zaber

### Task / DAQ / cue I/O

| Arduino pin | Signal | Direction | Notes |
|---|---|---|---|
| `D2` | `PIN_LICK_IN` | input | Digital lick input, `INPUT_PULLUP`, active-low |
| `D3` | `PIN_TTL_REWARD` | output | Reward TTL to DAQ |
| `D4` | `PIN_SYNC` | output | Sync TTL to DAQ / cameras |
| `D5` | `PIN_CUE_TTL` (gate) | output | Cue onset/offset |
| `D6` | `PIN_TRIAL_START` | output | Trial-start TTL |
| `D7` | `PIN_STARTSTOP_BUTTON` | input | Physical start/stop button |
| `D8` | `PIN_SOLENOID` | output | Solenoid valve control |
| `D9` | `PIN_TTL_TRIAL_STOP` | output | Trial-stop TTL at `dock_start` |
| `D11` | `PIN_CUE_AUDIO` | output | Audio tone/PWM to amplifier |

### Position code outputs

| Arduino pin | Signal | Notes |
|---|---|---|
| `D23` | `PIN_TTL_POS0` | Position code bit 0 |
| `D24` | `PIN_TTL_POS1` | Position code bit 1 |
| `D25` | `PIN_TTL_POS2` | Position code bit 2 |
| `D26` | `PIN_TTL_POS_STB` | Position code strobe |

### Widefield-specific notes

- The cue has two separate outputs: `D5` is the DAQ gate, `D11` is the actual audio waveform to the amplifier. Loudness is set with the external amplifier knob, not in firmware.
- Motion goes out over serial to the Zaber chain; there is no per-axis motor pinout as on the Teensy rigs.
- **Closed loop.** `STOP` issues a Zaber `stop` to all three axes and then reads true positions off the encoders, so `move_aborted` carries `actual_mm` and no manual re-referencing is needed. This is the one rig where an aborted move leaves position exactly known.

---

## SMC02 control method (both Teensy rigs)

Direct "button emulation":

- released: `pinMode(INPUT)` — high impedance
- pressed: `digitalWrite(LOW)` then `pinMode(OUTPUT)` — pull line to the SMC02 control common

The control outputs therefore behave like active-low closures. This is why the shared-board rule above exists: any other firmware driving those pins as ordinary outputs will assert them.

---

## Suggested DAQ wiring set

Highest-value alignment lines:

- `sync`
- `trial_start` (GB219 and widefield only)
- `trial_stop`
- `cue TTL` gate
- `reward TTL`
- `position bit 0` / `bit 1` / `bit 2`
- `position strobe`
- `lick detector output` directly from the detector board

Sample at 1 kHz minimum; 10 kHz is comfortable. Latch the position bits on the strobe edge: `index = POS0 + 2·POS1 + 4·POS2`.

## Cautions

- These notes document firmware pin assignments, not full bench wiring topology.
- Detector board, DAQ terminal, amplifier and solenoid power wiring should still be checked against the rig.
- The next useful addition would be a document mapping these MCU pins onto DAQ channel names, camera GPIO, lick detector terminals, and solenoid driver wiring.
