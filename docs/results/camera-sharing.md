# Camera sharing probe (live check L1)

Run on 2026-11-22 with `scripts/camera_share_probe.py`.
Text only: counts and yes/no answers, no frames.

| Mode | Other app | Other app got a picture | Our reads OK | Registry saw in use |
|---|---|---|---|---|
| hold | Windows Camera app | no | 26/26 | nothing |
| hold | Meet (browser) or Zoom preview | no | 36/36 | nothing |
| burst | Windows Camera app | yes | 12/12 | nothing |
| burst | Meet (browser) or Zoom preview | yes | 13/13 | nothing |

Decision 1: if any `hold` row says the other app got no picture, set the default
`camera_mode` to `"burst"`.
