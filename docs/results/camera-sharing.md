# Camera sharing probe (live check L1)

Status: **not measured yet.** This file is overwritten by
`uv run python scripts/camera_share_probe.py` when the owner runs live check
L1. Text only: counts and yes/no answers, no frames.

| Mode | Other app | Other app got a picture | Our reads OK | Registry saw in use |
|---|---|---|---|---|
| hold | Windows Camera app | not measured yet | not measured yet | not measured yet |
| hold | Meet (browser) or Zoom preview | not measured yet | not measured yet | not measured yet |
| burst | Windows Camera app | not measured yet | not measured yet | not measured yet |
| burst | Meet (browser) or Zoom preview | not measured yet | not measured yet | not measured yet |

Decision 1 is open until L1 runs. The default `camera_mode` stays `"hold"`
(the Stage 1 behaviour) until then; if any `hold` row shows the other app got
no picture, the default changes to `"burst"`.
