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
