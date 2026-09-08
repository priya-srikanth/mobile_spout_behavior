# v42 trial movement and v36 lick compatibility

Session update: 2026-09-08.

## Requested rollout and current status

The requested trial-end order is Y to dock Y, then Z to dock Z, then X to dock X.
Trial start reverses the order: X to target X, then Z to target Z, then Y to target Y.
Each calibrated, timed axis movement completes before the next begins. Trial moves
use destination Z directly, without the old safe-Z waypoint. Manual moves retain
their v36 safe-Z path.

| Target | Status |
| --- | --- |
| GB219, v36-derived v41 | Candidate prepared separately; no confirmed upload or bench validation |
| Dual-rig GB219 / 2pRAM v42 | Two candidate sketches prepared in this change; compilation and rig validation pending; rollout not completed |
| Arduino / Zaber | v40 candidate now implements the order, based on v39; copied to repository and local Arduino folder; compilation and hardware validation pending |

The user reported that lick detection did not work in the intervening dual-rig
Teensy iterations. The cause is not established. v42 deliberately starts from the
user's Arduino-folder v36 (SHA256
DA185AB5BF286B3C13A4F3B48E0D165532BF3E2BCE187846C1A72D5D1388168C), via the v41
movement candidate. It is not a cumulative upgrade from v40.

## Scope and compatibility

- Only rig pin selection, guards for unconnected pins, and the trial movement
  helper/call sites are added to v36. The two exports differ only in rig selection.
- GB219 uses its exact v36 pin map. 2pRAM uses the later shared-board pin map:
  left lick input pin 14, motor CW/CCW pins 25-30, no STOP lines, and no separate
  trial-start TTL. Pin 7's position strobe marks target arrival; the serial
  trial_start event remains at trial onset.
- Both builds retain v36 INPUT mode, active-low default, polling/debounce,
  refractory handling, lick latching, debug output, and serial configuration and
  status behavior. No pull-up, ISR, early-confirm latch, or later GUI change is
  imported. GUI v44 is the intended companion and is not modified.
- The 2pRAM lick pin must differ from GB219 to match the board. Keeping v36
  detection logic does not establish that the 2pRAM detector has the same physical
  polarity or drive characteristics; verify these on the rig.
- The helper checks sessionRunning between axis moves and during the existing
  post-motion rearm wait. The v36 timed-motion primitive and its position
  bookkeeping on interrupted moves are unchanged. Later motion/reward fixes
  are outside this narrowly scoped candidate.

## Validation and next steps

Four source regression tests pass: exact v36 logic preservation outside the
explicit edits, rig export equivalence, movement ordering/guards, and both pin
maps. Run `python -m unittest discover -s tests -p test_firmware_v42.py`.

Arduino CLI compilation was attempted but blocked by denied access to Arduino15
package directories and attempted dependency initialization. Neither sketch is
compiled, flashed, or hardware-validated. Compile each same-named sketch folder
for Teensy 4.1 with the existing rig settings before uploading.

Bench-test without a mouse: verify pin outputs, physical Y withdrawal beyond lick
reach, Y-Z-X return and X-Z-Y approach for representative positions, lick_on/off,
pre-cue reset, reward timing, TTLs and STOP. Motor completion uses calibrated time,
not encoder feedback. Confirm lick detection with GUI v44 on each rig before
marking rollout complete. The new order alone cannot guarantee clearance when
dock Y is within licking reach.

## Integration / session handoff

The candidates were initially prepared in an isolated repository copy. At the
user's explicit request, the patch was then applied to the original repository
for committing and pushing. The unrelated uncommitted v40 firmware and GUI v49
edits remain outside these commits.

The integration uses separate commits for the two v42 sketches plus regression
tests, and for this note plus the release-note pointer. Remote main was found to
contain 13 newer commits (through 842db8a), including GUI v50 and Zaber v39 work;
those changes must be retained when integrating. v42 remains paired with GUI v44,
and the subsequent Zaber v40 candidate now implements the same trial order. Reconcile shared
documentation during integration without staging the existing v40/v49 work.

The earlier supplied conversation refers to Phase 7 and COWORK_SESSION_STATE.md,
but that session-state file is absent from this repository. This note is the
handoff record for the active Claude workflow; reconcile it with the actual
session-state file on the destination branch without overwriting parallel work.

## Zaber v40 follow-up — 2026-09-08

At the user's request, added a new v39-derived Zaber v40 sketch. Trial return is
Y-Z-X and approach is X-Z-Y, using destination coordinates directly. Manual
safe-Z moves, v39 interrupt lick detection, reward timing/hold behavior, TTLs,
EEPROM layout, and serial protocol remain unchanged. Each existing absolute move
waits for IDLE; errors or STOP short-circuit the remaining axes. This is not a
v36-based Zaber build and is not a change to the original v39 sketch.

Source regression checks cover complete v39 preservation outside the new helper
and two trial call sites, axis order and error/abort guards. Compile and bench-test
before use; no flash or hardware validation is claimed. Confirm dock Y physically
withdraws beyond licking reach before Z/X movement. Local Arduino and repository
copies are provided; the rig's actual deployed version is not changed by copying.
