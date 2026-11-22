# Burst timing (Stage 2 fixes, F3)

Measured by `scripts/bench_burst.py` on 2026-11-22: 20 samples per camera
mode, interval 2 s, wait = interval minus the sample's time.
Medians in milliseconds. In hold mode the camera opens once, so its open
time is a single value.

| Mode | Camera open | Read | Pose | Start-to-start | Failed reads |
|---|---|---|---|---|---|
| hold | 1744 | 0 | 18 | 2000 | 0 |
| burst | 1039 | 249 | 13 | 2000 | 0 |
