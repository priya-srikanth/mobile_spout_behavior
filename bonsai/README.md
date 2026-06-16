# Bonsai 4-Camera Acquisition Notes

This folder documents the local changes made on the Blackfly S / Bonsai / Spinnaker stack to improve 4-camera acquisition at nominal `250 fps`.

The goal of these changes was not to redesign the workflow, but to make the existing Bonsai-based recording path more tolerant of multi-camera load and easier to reproduce on the acquisition machine.

## Versions Used

- Bonsai: `2.9.0`
- Bonsai.Spinnaker package: `0.9.1`
- Spinnaker SDK: `4.2.0.88`
- Cameras: Blackfly S mono (`BFS-U3-13Y3M`)

## Files In This Folder

- `Priya_bonsai_BlackflyGPIO_4camera_20260603_cam4first.bonsai`
  - Current saved 4-camera Blackfly workflow.
  - The camera branches are ordered with `cam4` first in the graph.
- `Priya_bonsai_BlackflyGPIO_4camera_20260527.bonsai`
  - Earlier archived 4-camera workflow kept for reference.
- `Bonsai.Spinnaker.Patched/SpinnakerCapture.cs`
  - Minimal local source patch used to modify the Bonsai.Spinnaker capture behavior.
- `Bonsai.Spinnaker.dll`
  - Built DLL artifact corresponding to the patched source in this folder.
- `Bonsai.Spinnaker.pdb`
  - Symbols for the patched DLL.
- `RUN_LOG_TEMPLATE.md`
  - Template for logging test runs and dropped-frame results.

## Exact Changes Made, By File

### 1. Workflow files

The current saved workflow is:

- `Priya_bonsai_BlackflyGPIO_4camera_20260603_cam4first.bonsai`

The earlier archived workflow is:

- `Priya_bonsai_BlackflyGPIO_4camera_20260527.bonsai`

These workflow files share the same performance-relevant acquisition settings:

1. Output paths were made explicit on `D:\camera\...`
   - Video files:
     - `D:\camera\cam1_.avi`
     - `D:\camera\cam2_.avi`
     - `D:\camera\cam3_.avi`
     - `D:\camera\cam4_.avi`
   - CSV files:
     - `D:\camera\cam1_.csv`
     - `D:\camera\cam2_.csv`
     - `D:\camera\cam3_.csv`
     - `D:\camera\cam4_.csv`
   - Reason:
     - Avoid Bonsai writing large files into `C:\Users\sabatini\AppData\Local\Bonsai`
     - Reduce risk of local system-drive write failures and hidden disk pressure

2. All four `VideoWriter` nodes were set to buffered output
   - Current saved state in this workflow:
     - `cam1`: `Buffered = true`
     - `cam2`: `Buffered = true`
     - `cam3`: `Buffered = true`
     - `cam4`: `Buffered = true`
   - Reason:
     - Short tests showed clean 4-camera acquisition only when all cameras were buffered

3. CSV chunk-data selector was corrected for this camera / SDK combination
   - Changed from `LineStatusAll` to `ExposureEndLineStatusAll`
   - Current CSV branch selectors include:
     - `Value.ChunkData.FrameID`
     - `Value.ChunkData.Timestamp`
     - `Value.ChunkData.ExposureEndLineStatusAll`
   - Reason:
     - This camera exposed `Exposure End Line Status All` as the usable chunk field in SpinView
     - Bonsai was previously reading the wrong chunk property

4. `ColorProcessing` was normalized to `NONE`
   - Reason:
     - These are mono cameras, so color processing should be a no-op
     - This also fixed workflow open errors caused by `Default` not matching the patched enum serialization on this machine

5. `Flip` was left on the camera-4 branch
   - The current saved workflow has the `cam4` branch first in the graph, and that branch still includes a `Flip` node before `VideoWriter`
   - Reason:
     - This matches the user’s preferred saved orientation state at the time the workflow was archived
   - Note:
     - `Flip` itself was not introduced as an acquisition-performance optimization

6. `FMP4` was retained as the saved codec in the workflow
   - Current saved state:
     - all four `VideoWriter` nodes use `FourCC = FMP4`
   - Reason:
     - The goal here was to preserve the user’s working codec choice while improving buffering and capture behavior elsewhere

### 2. `Bonsai.Spinnaker.Patched/SpinnakerCapture.cs`

This file contains the source-level capture changes made to Bonsai.Spinnaker.

The current active patch does three things:

1. Selective chunk enablement
   - Instead of enabling every available chunk from the camera, the patch enables only:
     - `Timestamp`
     - `FrameID`
     - `LineStatusAll`
     - `ExposureEndLineStatusAll`
   - Reason:
     - Reduce unnecessary per-frame chunk overhead

2. Explicit TLStream buffering
   - The patch configures the stream node map to:
     - `StreamBufferCountMode = Manual`
     - `StreamBufferCountManual = 256`
     - `StreamBufferHandlingMode = OldestFirst`
   - Reason:
     - Approximate the older PointGrey / FlyCapture behavior that tolerated multi-camera acquisition better
     - Absorb short downstream stalls instead of dropping immediately

3. No async callback queue in the final version
   - An intermediate patch introduced a managed queue between the Spinnaker callback and the Bonsai pipeline
   - That version was removed after it caused blank previews and unstable crashes
   - Final state:
     - the queue experiment is **not** present in the current DLL
     - only selective chunk enablement and TLStream buffering remain

### 3. `Bonsai.Spinnaker.dll`

- This is the compiled DLL artifact built from the patched `SpinnakerCapture.cs`
- It is included here so the exact tested binary is archived with the repo

## Installed Local Package Path

On the acquisition machine, the patched DLL was copied into:

- `C:\Users\sabatini\AppData\Local\Bonsai\Packages\Bonsai.Spinnaker.0.9.1\lib\net472\Bonsai.Spinnaker.dll`

Local backups created during testing included:

- `Bonsai.Spinnaker.original.2026-05-31.dll`
- `Bonsai.Spinnaker.pre256.2026-06-01.dll`

These backup names refer to local machine state and are not guaranteed to exist on another machine.

## What Was Tried But Backed Out

These changes were attempted during debugging and are **not** part of the final recommended patch:

1. Async queue between Spinnaker image callback and `observer.OnNext(...)`
   - Outcome:
     - produced blank previews / instability
   - Final action:
     - removed

2. Broad chunk enablement
   - This was the stock behavior in the package
   - Final action:
     - replaced with selective chunk enablement

3. Relative output paths in Bonsai
   - Final action:
     - replaced with explicit `D:\camera\...` paths

## Why These Changes Were Made

The stock `Bonsai.Spinnaker` package looked more fragile than the older `Bonsai.PointGrey` / FlyCapture path for long 4-camera acquisition. In particular:

- the older PointGrey path explicitly configured SDK-side buffering
- the Spinnaker path did not expose similar buffering by default
- the stock Spinnaker package enabled all chunks instead of only the needed ones

The changes in this folder were intended to make the Spinnaker path more conservative and more recording-oriented without making the patch too large to understand or revert.

## Observed Outcome

During testing:

- short buffered runs could be clean (`0` dropped frames across all four cameras in the best short test)
- long runs still showed frame loss, but the pattern became easier to study and compare across settings
- keeping all four `VideoWriter`s buffered mattered much more than global CPU/RAM graphs suggested

See `RUN_LOG_TEMPLATE.md` for how to record future comparisons.

## Rollback

To roll back on the local machine:

1. Close Bonsai.
2. Restore a backup DLL over the installed `Bonsai.Spinnaker.dll`.
3. Reopen Bonsai.

## Notes

- The installer `.exe` files for Bonsai and Spinnaker SDK are intentionally not tracked in this repo.
- The exact versions used are recorded above so another machine can be matched manually.
