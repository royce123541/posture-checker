from __future__ import annotations

from posture_checker.inference import FORWARD_HEAD, GOOD, SLOUCH
from posture_checker.policy.alerts import AlertPolicy


def feed(policy: AlertPolicy, start: float, seconds: float, label: str, confident=True, step=2.0):
    """Feed one label every ``step`` seconds; return the alerts and the end time."""
    alerts = []
    t = start
    while t <= start + seconds:
        alert = policy.update(t, label, confident)
        if alert:
            alerts.append(alert)
        t += step
    return alerts, t


def make_policy(**kwargs) -> AlertPolicy:
    return AlertPolicy(**{"window_s": 60, "bad_fraction": 0.8, "cooldown_s": 300, **kwargs})


def test_sustained_bad_posture_fires_once_after_the_window() -> None:
    policy = make_policy()
    alerts, _ = feed(policy, 0, 120, SLOUCH)
    assert len(alerts) == 1
    assert alerts[0].ts == 60  # not before a full window has passed
    assert alerts[0].label == SLOUCH
    assert alerts[0].bad_fraction == 1.0


def test_brief_lean_in_does_not_fire() -> None:
    policy = make_policy()
    alerts, t = feed(policy, 0, 120, GOOD)
    more, t = feed(policy, t, 20, FORWARD_HEAD)  # 20 s lean towards the screen
    rest, _ = feed(policy, t, 120, GOOD)
    assert alerts + more + rest == []


def test_good_posture_never_fires() -> None:
    alerts, _ = feed(make_policy(), 0, 600, GOOD)
    assert alerts == []


def test_bad_after_long_good_fires_when_fraction_is_reached() -> None:
    policy = make_policy()
    _, t = feed(policy, 0, 300, GOOD)
    alerts, _ = feed(policy, t, 120, SLOUCH)
    assert len(alerts) == 1
    # 80% of a 60 s window is 48 s of bad posture.
    assert 44 <= alerts[0].ts - t <= 52


def test_low_confidence_samples_never_fire() -> None:
    alerts, _ = feed(make_policy(), 0, 600, SLOUCH, confident=False)
    assert alerts == []


def test_mostly_low_confidence_window_does_not_fire() -> None:
    policy = make_policy()
    alerts = []
    for i in range(200):
        # Only every third sample is trustworthy: the app does not know enough.
        alert = policy.update(i * 2.0, SLOUCH, confident=(i % 3 == 0))
        if alert:
            alerts.append(alert)
    assert alerts == []


def test_cooldown_spaces_out_repeat_alerts() -> None:
    policy = make_policy(cooldown_s=300)
    alerts, _ = feed(policy, 0, 700, SLOUCH)
    assert [a.ts for a in alerts] == [60, 360, 660]


def test_gap_starts_a_new_window() -> None:
    policy = make_policy()
    _, t = feed(policy, 0, 50, SLOUCH)  # nearly a full window of bad posture
    resumed = t + 600  # laptop slept / monitoring paused
    alerts, _ = feed(policy, resumed, 40, SLOUCH)
    assert alerts == []  # the earlier 50 s must not count
    alerts, _ = feed(policy, resumed + 42, 30, SLOUCH)
    assert len(alerts) == 1


def test_reset_clears_the_window() -> None:
    policy = make_policy()
    feed(policy, 0, 58, SLOUCH)
    policy.reset()
    assert policy.update(60, SLOUCH, True) is None


def test_dominant_label_is_reported() -> None:
    policy = make_policy()
    alerts = []
    for i in range(40):
        label = FORWARD_HEAD if i % 4 else SLOUCH
        alert = policy.update(i * 2.0, label, True)
        if alert:
            alerts.append(alert)
    assert alerts[0].label == FORWARD_HEAD


def test_suspect_after_a_streak_of_bad_samples() -> None:
    policy = make_policy()
    policy.update(0, GOOD, True)
    assert not policy.suspect
    for i in range(1, 4):
        policy.update(i * 2.0, SLOUCH, True)
    assert policy.suspect
    policy.update(10, GOOD, True)
    assert not policy.suspect
