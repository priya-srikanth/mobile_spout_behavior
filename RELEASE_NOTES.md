# Release Notes

## Current export — mixed firmware versions, GUI `v50`

- GUI: `gui/BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v50.py` — stable `v47`-line build with the fixes below
- 2pRAM Teensy / SMC02: `firmware/teensy_smc02/Behavior_MobileSpouts_2pRAM_Teensy_v37/`
- GB219 Teensy / SMC02: `firmware/teensy_smc02/Behavior_MobileSpouts_GB219_Teensy_v37/`
- Widefield Mega / Zaber: `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v36/`
- Alternate Widefield Mega / Zaber build: `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v39/`
- Bench utility: `tools/LickScan_Teensy/`

As of August 7, 2026, the widefield rig is intentionally staying on Arduino Mega / Zaber `v36`, not `v37`;
the `v37` faults that caused that (see "v37 fixes, 2026-08-09" below) are now fixed but not yet bench-tested.

**The serial protocol is unchanged from `v36`.** Every command, config key, event name and response string is identical, so GUI `v44` drives all three `v37` builds with no modification.

---

## GUI v50 — stable `v47` line with trial-log and reward-hold fixes (2026-08-23)

This GUI intentionally starts from the `v47` line because that is the branch behaving reliably on the
rig, rather than layering more changes onto the newer `v48`/`v49` line.

- **One row per trial in `trials.csv`.** `trial_start` rows are now normalized onto the ACTUAL trial
  number (`raw device trial id + 1` for `trial_start` only), so the `trial_start` placeholder and the
  later cue/hit/miss/reward rows coalesce into a single trial record.
- **Coordinate-only loads now refresh the displayed current profile.** Loading just mouth/dock/safe-Z
  from a mouse profile or config updates the GUI's "current profile" display instead of leaving the old
  profile name on screen.
- **User-facing note matches the intended auto-hold behavior.** The help text now reflects that rewards
  resume on any detected lick once the firmware supports that behavior.

`events.csv` is unchanged: all raw events, including `trial_start`, are still written exactly as emitted
by the device.

---

## Firmware v39 — any detected lick clears AUTO reward hold (2026-08-23, Arduino Mega / Zaber)

`firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v39/` is based directly on `v38`
(interrupt-driven lick onset / ENL fix) and keeps the same serial protocol, pin map, and motion logic.

**Change.** If rewards were auto-held after too many missed rewarded trials, **any detected lick level**
now clears that AUTO hold immediately. This is broader than the prior onset-only behavior: if the animal
is already contacting the spout between trials when the hold is active, rewards resume without waiting for
a brand-new lick edge.

**Why this matters.** It lets a mouse consume residual reward between trial end and the next cue and still
resume normal reward delivery, instead of leaving the spout empty because no fresh lick onset occurred
after the hold engaged.

This only bypasses the AUTO hold path. Manual holds still stay active until explicitly released.

### Late v39/v50 bench fixes (2026-08-23)

- **Reward valve pulses are now hard-timed in Arduino/Zaber `v39`.** `openRewardValve()` no longer
  services serial/status/sync/lick-printing while the solenoid is open. This was added after logs showed
  rare cue-to-reward-event delays of ~120-222 ms in the pre-fix build; the repeat test after the change
  showed auto rewards clustered at ~23-34 ms cue-to-reward-event with `task.reward_ms=18`. Lick onsets
  are still interrupt-captured, but licks that occur during the solenoid-open window may be logged a few
  ms late. The DAQ lick-detector line remains the authoritative high-precision lick timing source.
- **AUTO reward hold stays close to `v38` lick behavior.** `updateLick()` is intentionally kept identical
  to `v38`; the added level check only prevents AUTO hold from engaging if the lick input is already active
  when the hold threshold is reached. Manual holds are unchanged.
- **Lick release of AUTO hold now resets the miss streak.** If AUTO hold is cleared by a detected lick, or
  prevented because the lick line is already active at the miss threshold, `miss_streak` is reset to zero so
  the next single missed trial does not immediately re-engage reward hold.
- **Cumulative raster marker priority changed in GUI `v50`.** Auto/free/manual rewards are shown as teal
  rings even if a later lick makes the trial count as a hit. Contingent/earned hits remain green, misses red.
- **Auto STATUS poll is saved, but deferred during serial connection.** The checkbox and interval are stored
  in the GUI config. During connect/handshake the GUI temporarily disables polling, then restores the saved
  state once the Arduino/Teensy is ready or the handshake times out, preventing saved polling from disrupting
  fragile Arduino serial-open handshakes.
- **`trials.csv` duplicate rows from early move licks are fixed.** Lick events during `move_to_target`
  can still carry the firmware's previous raw trial id; GUI `v50` now keeps those licks on the already-open
  normalized trial row instead of writing an extra previous-trial summary row.
- **Arduino connect-time solenoid blip is considered a hardware reset-window issue.** Opening the Mega serial
  port resets the board; before firmware `setup()` runs, pins can float. Firmware already drives `D8` LOW as
  early as possible in `setup()`, and no additional software change was made. If needed later, the preferred
  fix is a pulldown on the solenoid driver input (`D8`) rather than more GUI/firmware logic.

---

## GUI v49 — one row per trial in `trials.csv` (2026-08-14)

**Problem.** `trials.csv` held roughly TWO rows per trial. PS92 2026-08-12 wrote 160 rows for 81
trials in one segment, and 563 rows for 283 trials across the day. Nothing downstream computed a wrong
number — every consumer selects scored rows (`hit` XOR `miss`) — but anyone reading row counts read
double the real trial count, and the widefield analysis did exactly that, reporting "563 trials" for a
283-trial session before it was caught.

**Cause — a consequence of the v47 fix, not a regression in it.** The firmware emits `trial_start`
BEFORE `totalTrials++`, so it carries the PREVIOUS trial's id while that trial's own cue/hit/reward
events carry id+1. v47 correctly closes the open row on every `trial_start` (that is what fixed the
`pos_idx` mislabelling), but closing it necessarily emits a placeholder row that only ever received
position and timing — never a cue, lick, hit, miss or reward.

**Fix.** `_finalize_trial` drops a row that never received any of `hit` / `miss` / `reward_delivered` /
`lick_in_response_window`. Nothing is lost: `events.csv` retains every `trial_start` in full with its
own position and timestamp, and an aborted trial was already excluded by every consumer.

**Unchanged:** the v47 position guarantee, the trial ids, and every column. `tests/test_trial_logging.py`
(v47 invariants) and `tests/test_trial_logging_v49.py` (one row per trial, a miss still written, an
aborted trial omitted) both pass.

**Note for analysis:** sessions recorded on v40–v48 keep the doubled rows. Count trials by filtering
`hit` XOR `miss`, never by row count.

---

## Firmware v38 — interrupt-driven lick capture (2026-08-10, all three rigs; NOT yet bench-tested)

`firmware/**/*_v38/` adds a hardware-interrupt lick-onset path to all three builds (Zaber Arduino,
GB219 Teensy, 2pRAM Teensy).

**Problem (v37 and earlier).** Lick detection was polled once per main-loop iteration, gated by a 20 ms
*lockout* debounce (it only accepted a state change if ≥20 ms had passed since the last accepted change —
not a stability wait). So a brief/fast contact, or any contact that fell in a loop-poll gap, could produce
**no `lick_on` at all**, and the ENL was therefore never reset on it. Measured against the widefield DAQ
(continuous 5 kHz on the raw analog), the firmware missed **~16 % of trials' final-window pre-cue licks**
(PS92 8/7: 70/430 cues) — all **full 0 V contacts**, so this was never a threshold problem, and the GUI
logged them correctly whenever the firmware emitted them (the misses simply never became events).

**Fix.** A `CHANGE` interrupt on the lick pin (`digitalPinToInterrupt`, works on Uno/Mega and every Teensy
pin) latches every onset the instant it happens — immune to loop / serial / Zaber / motion latency and to
the debounce lockout. `updateLick()` drains the captured onsets to emit `lick_on` and arm the ENL reset;
the release/offset stays on the debounced poll. Serial protocol, pinout, and config keys are **unchanged**,
so GUI `v47` drives it with no modification.

**Behavioral note.** This makes the ENL strictly enforced, so it resets more often (animals already reset it
~8–9×/trial). With a very impulsive animal the ENL may take longer to clear — bench-test and watch trial
timing before deploying, and prefer promoting **between cohorts**. On the bench, verify `lick_on` and
`pre_cue_reset_by_lick` now fire on light/quick touches that v37 missed.

---

## GUI v47 — trial_start closes the previous trial's row (2026-08-09)

**All three rigs. Use `v47`.** `v46` and earlier wrote the NEXT trial's `pos_idx` into `trials.csv`
on every trial whose position changed — ~15% of trials, every session, `v40`..`v46`.

The firmware emits `trial_start` before `totalTrials++`, so it reports the PREVIOUS trial's id,
which is the id already on the open row; the row was therefore never closed, and the position
fields were overwritten with the new trial's position. A `trial_start` now always finalizes the
open row. Same lag exists on all three rigs' firmware, so all three were affected.

The corruption is a uniform one-trial shift (`gui[N] == true[N+1]`), so **existing logs are
invertible**: `true[N] = gui[N-1]`, first trial unrecoverable, last row uncorrupted as a check.

Row counts are unchanged in practice — scored rows (hit XOR miss) are still one per trial. Not
fixed: `trial_id` still lags the device trial number by one, and each trial still yields an
unscored `trial_start` row; both need `totalTrials++` moved ahead of the emit in firmware.

`v46` is kept exactly as it shipped. Regression tests: `tests/test_trial_logging.py`.

---

## v37 fixes, Arduino Mega / Zaber only (2026-08-09)

Applied in place to `firmware/arduino_zaber/Behavior_MobileSpouts_Zaber_Arduino_v37/`. **Not yet
bench-tested** — verify STOP mid-move, then confirm a later `GET`/move still reports correct
positions, before running a session.

- **`PIN_TTL_POS1` 24 → 29.** The bench wire is on `D29`; `D24` is the co-tenant's
  `DelayIndicatorPin`. On `D24` the DAQ's `spout_bit1` never toggles and position codes collapse
  onto `{0,1,4,5}`.
- **STOP no longer corrupts the Zaber serial link.** This is what made `v37` unusable. The link is
  request/response and `zWaitIdle()` blocks for a `get pos` reply, but `handleSerialDuringMotionWait()`
  runs from inside that loop — so a STOP mid-move issued 3×`stop` + 3×`get pos` re-entrantly and
  every reply thereafter matched the wrong request. A stop requested during a wait now only sets a
  flag; the move path issues it once the connection is free. The abort feature is unchanged.
- **Physical start/stop button disabled** (`ENABLE_STARTSTOP_BUTTON = false`) — host-only start/stop.
  Note this leaves no local abort if the host link drops.
- **`closeTrialAndCueGates()` ported from the Teensy rigs** — STOP now forces the cue gate LOW
  instead of leaving the DAQ cue line high for the rest of `cue.duration_ms`.

The Teensy `v37` builds need no changes: their pinouts are correct, they have no physical button, and
their stop path sets a flag without touching the stages, so the re-entrancy fault cannot occur.

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
