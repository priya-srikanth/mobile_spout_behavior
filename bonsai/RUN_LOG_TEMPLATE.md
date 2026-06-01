# Bonsai Run Log Template

Use one section per acquisition test.

---

## Run

- Date:
- Start time:
- Operator:
- Workflow file:
- Bonsai version:
- Spinnaker DLL:
  - `stock`
  - `patched-selective-chunks+stream-buffer`
  - other:

## Camera Settings

- Cameras used:
- Target FPS:
- Pixel format:
- ROI / frame size:
- Exposure:
- Gain:

## Workflow Settings

- Codec / FourCC:
- VideoWriter buffered:
  - cam1:
  - cam2:
  - cam3:
  - cam4:
- CSV writers enabled:
- Preview windows open:
- Flip enabled on:
- Output drive / folder:

## Session

- Intended duration:
- Actual duration:
- Any pauses / restarts:
- Any visible preview lag:
- Any Bonsai errors / crashes:

## Results

| Camera | CSV file | AVI file | Recorded FPS | Dropped frames | Dropped % | Notes |
|---|---|---|---:|---:|---:|---|
| cam1 |  |  |  |  |  |  |
| cam2 |  |  |  |  |  |  |
| cam3 |  |  |  |  |  |  |
| cam4 |  |  |  |  |  |  |

## Interpretation

- Overall outcome:
- Likely bottleneck:
- Worth repeating with same settings?

## Next Change To Test

- One change only:
- Why:

---

## Example Comparison Fields

If useful, track these across runs in a separate table:

| Run ID | Duration min | Codec | Buffered cams | Preview count | Patch version | cam1 drop % | cam2 drop % | cam3 drop % | cam4 drop % | Outcome |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---|
|  |  |  |  |  |  |  |  |  |  |  |
