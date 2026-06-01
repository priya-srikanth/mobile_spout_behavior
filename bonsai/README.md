# Bonsai Artifacts

This folder captures the local Bonsai/Spinnaker changes made on June 1, 2026 for the 4-camera Blackfly S recording workflow.

## Included files

- `Priya_bonsai_BlackflyGPIO_4camera_20260527.bonsai`
  - Most recent 4-camera Blackfly workflow file.
  - `ColorProcessing` entries were updated from `Default` to `NONE` so the workflow opens against the patched Spinnaker DLL on this machine.
- `Bonsai.Spinnaker.dll`
  - Patched build used for testing.
- `Bonsai.Spinnaker.pdb`
  - Symbols for the patched DLL.
- `Bonsai.Spinnaker.Patched/`
  - Minimal local source project used to build the patched DLL.

## What changed in the patched DLL

The patch was intentionally kept small and reversible.

1. Selective chunk enablement
   - Instead of enabling every available chunk, the patched capture code enables only:
     - `Timestamp`
     - `FrameID`
     - `LineStatusAll`
     - `ExposureEndLineStatusAll`

2. SDK-side stream buffering
   - The patched capture code configures the TLStream node map to:
     - `StreamBufferCountMode = Manual`
     - `StreamBufferCountManual = 64`
     - `StreamBufferHandlingMode = OldestFirst`

3. No async queue patch
   - An intermediate callback-to-queue handoff was tried and then removed after it caused unstable native crashes / blank previews.
   - The current patched DLL keeps only the chunk-selection and TLStream buffering changes.

## Local install / rollback notes

## Version notes

- Bonsai installed version used for this setup:
  - `2.9.0`
  - local executable version: `2.9.0.0`
  - product version: `2.9.0+bd3ec7e29dff8c4c0d1ddef5dd2a322a1293f342`
- Spinnaker SDK installed version used for this setup:
  - `4.2.0.88`
  - local DLL version from `SpinnakerNET_v140.dll`: `4.2.0.88`

Installer binaries are intentionally not tracked in this repo. They are stored separately outside Git, but the exact versions used here were:

- `Bonsai-2.9.0.exe`
- `SpinnakerSDK_FULL_4.2.0.88_x64.exe`

On the test machine, the active Bonsai package DLL path was:

- `C:\Users\sabatini\AppData\Local\Bonsai\Packages\Bonsai.Spinnaker.0.9.1\lib\net472\Bonsai.Spinnaker.dll`

The original local backup was kept next to it as:

- `C:\Users\sabatini\AppData\Local\Bonsai\Packages\Bonsai.Spinnaker.0.9.1\lib\net472\Bonsai.Spinnaker.original.2026-05-31.dll`

To roll back locally:

1. Close Bonsai.
2. Copy `Bonsai.Spinnaker.original.2026-05-31.dll` over `Bonsai.Spinnaker.dll`.
3. Reopen Bonsai.

## Why this patch was attempted

The stock `Bonsai.Spinnaker` package appeared more fragile than the older `Bonsai.PointGrey` / FlyCapture path for 4-camera `250 fps` recording.

The old PointGrey package explicitly configured SDK-side buffering, while the stock Spinnaker package did not expose comparable buffering behavior in its capture code. This patch was an attempt to bring the Spinnaker path closer to that older behavior while keeping the change set easy to audit and undo.
