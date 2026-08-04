# Release Notes

## Current export — firmware `v37`, GUI `v44`

- GUI: `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v44.py` (unchanged)
- 2pRAM Teensy / SMC02: `firmware/teensy_smc02/Behavior_MobileSpouts_2pRAM_Teensy_v37/`
- GB219 Teensy / SMC02: `firmware/teensy_smc02/Behavior_MobileSpouts_GB219_Teensy_v37/`
- Widefield Mega / Zaber: `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v37/`
- Bench utility: `tools/LickScan_Teensy/`

**The serial protocol is unchanged from `v36`.** Every command, config key, event name and response string is identical, so GUI `v44` drives all three `v37` builds with no modification.

---

## v37 — what changed

### Cue TTL is now a gate, not a pulse (all three rigs)

`cue TTL` used to be a fixed short pulse (5–8 ms) marking cue onset only, while the tone itself ran for `cue.duration_ms` (default 1000 ms). The DAQ therefore recorded when a cue started and nothing about when it ended.

`cue TTL` is now held HIGH for the whole tone: rising edge = cue onset, falling edge = cue offset. It is driven at the point the tone is generated rather than at the call sites, so manual cues behave identically and it can never drift out of step with `cue.duration_ms`.

Analysis impact: cue duration is now readable directly from the trace. Any pipeline that assumed a short fixed-width cue pulse needs to switch from edge-counting to gate-reading.

### STOP now actually stops (all three rigs)

Previously `STOP` halted only the currently-running axis segment; the multi-axis move sequence carried on to the next axis. A new abort flag is checked by every blocking motion loop so a stop unwinds the whole sequence.

On the **Zaber** rig `STOP` additionally had no effect on the stages at all: the handler set `runState = ST_IDLE` while `zWaitIdle()` kept polling until the axis reached its commanded target, so the move completed after the operator pressed stop. `v37` issues a Zaber `stop` to all three axes and then reads true positions back off the encoders.

### Position tracking survives an abort

- **SMC02 rigs** (open loop): an aborted move used to credit the full requested `deltaMM` to `posMM`, desyncing software position from the physical stage with no home switches to recover from. `v37` credits only the fraction of the move actually elapsed, correctly subtracting the fixed `overheadMs` startup term, and emits `move_aborted` with `requested_mm` and `applied_mm`. This is a time-based estimate bounded by the ms/mm calibration; it does not model acceleration ramp-up or deceleration after release, so **re-reference via Motion tab → "Use current XYZ" after any abort where precision matters.**
- **Zaber rig** (closed loop): no estimation. `move_aborted` reports `actual_mm` read off the encoder. No re-referencing needed.

### Blocking waits no longer drop sync pulses (Teensy rigs)

`delay()` in the motion direction-guard bands and the reward-calibration loop is replaced with a serviced wait that keeps sync, TTL pulse timers, cue timing and serial alive. Previously up to ~80 ms of blind time per multi-axis move.

### Physical start/stop button fixed (Zaber rig)

`updateButton()` was not called during Zaber moves, and it is edge-detected. Since moves take seconds, a press *and release* entirely inside a move left the level back at HIGH by the time the loop resumed, so no edge was ever registered and **the press was silently discarded**. The button is now serviced during moves, and a button-stop halts the stages like the `STOP` command does.

### `PIN_UNUSED` is now safe (Teensy rigs)

`PIN_UNUSED` is `255`, and Teensy 4 indexes its pin lookup tables without bounds checking. `smcPressLine()`, `smcReleaseLine()`, `isHomeTriggered()` and `pulsePin()` now return early on it. Without this, disabling a signal by setting its pin to `PIN_UNUSED` would read off the end of those tables.

### Rig-selectable Teensy pin map

One source file, two ready-to-flash sketches, selected by `#define RIG_2PRAM` / `#define RIG_GB219`. Prevents the two rigs' logic from drifting apart.

### Explicit prototypes

Several functions were relying on the Arduino IDE's auto-generated prototypes (`syncAdaptivePositionsFromGlobal`, `deliverRewardForTrigger` on Teensy; `emitEvent`, `emitConfigKV`, `zRefreshAllAxisPosMM`, `chooseNextBlockPosition` on Zaber). Declared explicitly so the sketches also build under a plain C++ compiler.

---

## Per-rig differences introduced in v37

| | 2pRAM | GB219 | Widefield (Zaber) |
|---|---|---|---|
| Trial-start TTL | **none** — position strobe is the trial marker | pin `10` | pin `6` |
| Trial-stop TTL | pin `20` | pin `4` | pin `9` |
| Cue gate | pin `6` | pin `6` | pin `5` |
| Position bus + strobe | `8` / `17` / `18` + `7` | `14` / `40` / `41` + `20` | `23` / `24` / `25` + `26` |
| SMC02 STOP lines | **removed** | retained (`9` / `21` / `16`) | n/a |
| Lick pin mode | `INPUT_PULLUP` | `INPUT` | `INPUT_PULLUP` |

2pRAM drops the SMC02 STOP lines because in P02 mode the motor halts on CW/CCW release. `axisStop()` is reachable only from the serial `STOP` command and releases CW/CCW first regardless, so nothing is lost.

2pRAM has no hardware trial-start marker. **The position strobe fires after the move completes, so it marks target arrival, not trial onset** — those differ by 2–3 s at default calibration, varying by several hundred ms across positions. True onset remains in the session log as `EVT name=trial_start`.

---

## Packaging notes

- This repo is a lightweight source snapshot of the current GUI and firmware.
- Versioned filenames are preserved from the active lab workflow.
