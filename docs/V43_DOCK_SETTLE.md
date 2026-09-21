# v43 dock settle

Based on v42 for both GB219 and 2pRAM. After returning to dock, wait
max(500 ms, task.settle_ms) plus the sampled intertrial interval. The
existing 75 ms post-motion wait remains. At settle=100 ms and ITI=0,
the next approach waits another 500 ms after the dock event. Target
settling remains at the configured value; RPM and acceleration are unchanged.

The existing nonblocking ITI state keeps serial and STOP handling active.
GUI v44 is compatible without modification. Target settle values above
500 ms also lengthen dock settling. Dock waiting is reported as ITI.

Repository and local Arduino-folder copies are provided for both rigs.
v42 is unchanged. Source regression tests cover zero/nonzero ITI, the
500 ms minimum, larger configured delays, pin maps and unrelated behavior.
Not compiled, uploaded, or hardware-tested. Compile and upload the
rig-specific v43 sketch, then bench-test without an animal. The longer
pause provides margin but does not verify that the stage physically stopped
or establish that reversal timing caused the observed session offsets.

## Why this changed

The user reported sudden approximately 1 cm anterior/posterior offsets on
Y during sessions on two setups. Rapid alternating manual CW/CCW button
presses also reproduced a large wrong-direction move on one setup. This
supports investigating reversal timing but does not establish that normal
session timing triggers the same fault; physical disconnection of Teensy
inputs during the manual reproduction has not been confirmed.

In v42, the configured target settle applies only at the trial target.
Return to dock has a 75 ms post-motion wait and the sampled ITI, with
additional time depending on other axis movements. It has no configured
dock-settle interval, and ITI can be zero. v43 adds a predictable pause at
that transition rather than changing motor speed or acceleration, which
would require recalibrating this rig's timed-distance motion.

The initial 500 ms minimum is an engineering test choice with more margin
than 100 ms, not a measured stopping requirement or verified remedy.
Compared with v42, it adds max(500 ms, task.settle_ms) to each completed
trial's dock wait. It does not send an extra STOP pulse, sense motion,
change manual-jog reversal timing, or fix existing open-loop position drift.
Test physical return accuracy over representative repeated trial sequences
without an animal before adopting it for behavior sessions.

## Validation and distribution

Nine combined v42/v43 source regression checks passed. Repository, local
Arduino, and N:\MICROSCOPE\Priya\Arduino v43 sketch copies were verified
identical by SHA-256 for each rig. Older sketch versions were retained.
No firmware compilation, Teensy upload, or hardware validation was performed.
