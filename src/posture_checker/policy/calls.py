"""Decide whether the user is in a call from window titles (behaviours C2, C3).

The rules are heuristics based on how each app titles its meeting window; see
docs/specs/stage-2-call-detection.md. Titles are never logged or stored: only
the matched app name (``meet``, ``zoom`` or ``teams``) leaves this module.
"""

from __future__ import annotations

import logging
import re

from posture_checker.platform.windows_list import WindowInfo, WindowLister

log = logging.getLogger(__name__)

MEET = "meet"
ZOOM = "zoom"
TEAMS = "teams"

_BROWSERS = frozenset({"chrome.exe", "msedge.exe"})
#: Meet titles a tab in a meeting "Meet - " plus the meeting code: 3, 4 and 3
#: lowercase letters (spec C2).
_MEET_TITLE = re.compile(r"^Meet - [a-z]{3}-[a-z]{4}-[a-z]{3}\b")
_ZOOM_PROCESSES = frozenset({"zoom.exe"})
_ZOOM_TITLES = frozenset({"Zoom Meeting", "Zoom Webinar"})
_TEAMS_PROCESSES = frozenset({"ms-teams.exe", "teams.exe"})
_TEAMS_SUFFIX = " | Microsoft Teams"
#: Teams call window prefixes (spec C2); to be confirmed by live check L2b.
_TEAMS_PREFIXES = ("Meeting with", "Meeting in", "Call with")


def match_window(window: WindowInfo) -> str | None:
    """The call app this window belongs to, or None."""
    process = window.process.lower()
    title = window.title
    if process in _BROWSERS and _MEET_TITLE.match(title):
        return MEET
    if process in _ZOOM_PROCESSES and title in _ZOOM_TITLES:
        return ZOOM
    if process in _TEAMS_PROCESSES:
        title = title.removesuffix(_TEAMS_SUFFIX)
        if title.startswith(_TEAMS_PREFIXES):
            return TEAMS
    return None


class CallDetector:
    """Call probe for the pause supervisor: ``apps_in_call()``."""

    def __init__(self, lister: WindowLister) -> None:
        self.lister = lister
        self._reported_failure = False

    def apps_in_call(self) -> list[str]:
        try:
            windows = self.lister.windows()
        except Exception as error:
            if not self._reported_failure:
                self._reported_failure = True
                # Only the exception type: its text could in theory carry a title.
                log.warning("window listing failed (%s); assuming no call", type(error).__name__)
            return []
        self._reported_failure = False
        apps = {app for w in windows if (app := match_window(w)) is not None}
        return sorted(apps)
