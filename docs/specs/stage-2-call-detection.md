Status: approved

Approved by the owner on 2026-11-22 with the recommended answer to all three decisions.

# Stage 2 addition: pause during calls by window title

Addition to the approved `docs/specs/stage-2.md`. That spec is unchanged; the
numbering below (C1, C2, ...) is local to this file.

## 1. Goal

Live check L2 failed on the owner's laptop (Windows 11 build 26300): Windows no
longer writes the ConsentStore `LastUsedTimeStart/Stop` values, so the registry
camera-in-use signal never fires and posture alerts can appear during calls.
This addition gives the pause supervisor a second signal, "in a call", read from
visible top-level window titles and their process names. The registry signal
stays as it is.

## 2. Decisions for the owner

1. **Can the owner turn call detection off?** Recommended: yes, a "Pause during
   calls" checkbox in Settings, on by default. Trade-off: one more setting, but
   it is the only way out if a heuristic keeps pausing wrongly.
2. **Should a minimised meeting window count?** Recommended: yes. Trade-off: a
   meeting left minimised still pauses (the user is still in the call, so this is
   usually right). A window that is hidden, not minimised, never counts.
3. **Meet only when the meeting tab is the active tab?** Recommended: accept
   it, since that is all a window title shows. Trade-off: switching to another
   tab in the same browser window ends the pause after the grace period. The
   owner can keep Meet in its own window to avoid this.

## 3. Behaviour

**Window probe** (`src/posture_checker/platform/windows_list.py`)

C1. A `WindowLister` protocol has `windows() -> list[WindowInfo]`, where
    `WindowInfo` has `title: str` and `process: str` (the executable file name,
    for example `chrome.exe`). The Windows version uses `EnumWindows`,
    `IsWindowVisible` and `GetWindowTextW` through `ctypes`, and gets the
    process name with `QueryFullProcessImageNameW`. No new dependency. Windows
    that are not visible or have empty titles are skipped. Minimised windows are
    still listed (decision 2).
C2. A `CallDetector` (in `src/posture_checker/policy/calls.py`, no Windows
    imports) takes a `WindowLister` and has `apps_in_call() -> list[str]`. It
    returns the sorted, de-duplicated names of the matched apps, chosen from
    `meet`, `zoom` and `teams`. Process names are compared case-insensitively.
    These matching rules are heuristics:
    - **meet**: process is `chrome.exe` or `msedge.exe` and the title starts with
      `Meet - ` followed by a meeting code in the form `xxx-xxxx-xxx` (3, 4 and
      3 lowercase letters). This is how Meet titles a tab that is in a meeting.
      The landing page (`Google Meet`) does not match.
    - **zoom**: process is `Zoom.exe` and the title is exactly `Zoom Meeting` or
      `Zoom Webinar`. The main client window (`Zoom Workplace`, `Zoom`) does not
      match.
    - **teams**: process is `ms-teams.exe` or `Teams.exe` and the title, with the
      ` | Microsoft Teams` suffix removed, starts with `Meeting with`,
      `Meeting in` or `Call with`. The exact Teams titles are confirmed or
      corrected by live check L2b.
C3. If listing windows raises, `apps_in_call` returns `[]` and logs the failure
    once. Window titles never appear in logs, events or the database; only the
    matched app name does.

**Pause supervisor** (`policy/pausing.py`)

C4. `PauseSupervisor` takes an optional `calls` probe (with `apps_in_call()`).
    Each `check` adds the reason `in_call:<app>` for each app reported now or in
    the last `CAMERA_GRACE_S` (10 s). This reuses the camera grace: a meeting
    window that briefly loses its title (for example while a tab reloads) does
    not cause an unpause and then a re-pause. A failing probe counts as "no
    call" through the existing `_probe` helper.
C5. Timing reuses the Stage 2 cadence. The supervisor is checked before every
    sample and every 5 s while paused, so the app pauses no more than 10 s after
    the matching window appears (one sample interval at most, as in Stage 2
    behaviour 5). It resumes 10-15 s after the window goes away (10 s grace
    plus up to one 5 s poll).
C6. `in_call:*` and `camera_in_use:*` can both be active at once. Each writes its
    own `pause`/`resume` event row (Stage 2 behaviour 10, detail = reason).
C7. The tray tooltip for `in_call:meet` reads "Paused: in a call (Google Meet)",
    and the other two read "(Zoom)" and "(Microsoft Teams)". The icon is grey,
    like any other automatic pause.
C8. Setting `pause_in_calls` (default true) is shown as a "Pause during calls"
    checkbox in Settings (decision 1). When it is false the supervisor does not
    call the probe at all. The running app picks up a change within 5 s, as in
    Stage 2 behaviour 16.

## 4. Acceptance tests (no webcam)

`FakeWindowLister` returns a list of `WindowInfo` that the test sets.
`FakeClock` drives time.

- C1: a smoke test marked `@pytest.mark.camera` (real hardware and Windows,
  skipped in CI) checks that the Windows lister returns a list of `WindowInfo`.
  CI tests import only the protocol and the fake.
- C2: a table test, with titles and processes in, apps out. It must include:
  `("Meet - abc-defg-hij", "chrome.exe") -> meet`; the same in `msedge.exe` ->
  meet; `("Google Meet", "chrome.exe") -> none`; `("Meet - abc-defg-hij",
  "notepad.exe") -> none`; `("Zoom Meeting", "Zoom.exe") -> zoom`; `("Zoom
  Workplace", "Zoom.exe") -> none`; `("Meeting with Ana | Microsoft Teams",
  "ms-teams.exe") -> teams`; `("Chat | Microsoft Teams", "ms-teams.exe") ->
  none`; two Meet windows -> `["meet"]` once; Meet plus Zoom -> `["meet",
  "zoom"]`.
- C3: the fake raises `OSError`. `[]` is returned, and one log record is written
  over two calls. With `caplog`, a run with a title containing a marker string
  shows that the marker is in no log record. An event-row check shows that the
  marker is in no database row.
- C4/C5: a Meet window appears, and the next `check` reports
  `in_call:meet` (paused). The window is removed; the decision is still paused
  at +9 s and runs at +10 s. Through the monitor loop with `FakeCamera`, the
  camera is closed after the first paused step, and `FakeCamera.opened` does not
  increase while paused.
- C6: with the fake camera-usage probe reporting `Zoom.exe` and the lister
  reporting a Zoom meeting, both reasons are listed. The event table has one
  pause row and one resume row for each.
- C7: the tooltip function's output is asserted for all three apps.
- C8: with `pause_in_calls=False` the fake lister's call counter stays 0 and no
  `in_call:*` reason appears. Saving the setting through the Settings controller
  writes it to the isolated config path.
- Privacy: `tests/test_privacy.py` keeps passing, with no new network or file
  writes.

## 5. Live checks

- **L2b Calls (replaces L2, about 6 min).** With the app running and the tray
  visible:
  1. Open meet.google.com in Chrome. The tray stays green on the landing page.
  2. Join a meeting. Within about 10 s the tray turns grey with "in a call
     (Google Meet)". Switch to another tab in that window: it turns green within
     about 15 s (decision 3). Switch back: grey again.
  3. Leave the meeting. Green within about 15 s.
  4. If Zoom or Teams is installed, repeat with a Zoom meeting or a Teams call.
     If a Teams call does not pause, write down the call window's title (from
     the taskbar hover) and report it, so the rule can be corrected.
  5. Untick "Pause during calls" in Settings, rejoin Meet: the tray stays green.

## 6. Out of scope

- Reading tab titles other than the active one, browser extensions, or UI
  Automation of browser tabs.
- Detecting the microphone, audio sessions or any other call app (Slack,
  Discord, Webex). They can be added later as rules.
- Changing or removing the registry camera-in-use signal.
- Any network access, such as calendar lookups.

## 7. Risks and limitations

- **Heuristics.** App updates or a different UI language can change titles and
  silently break a rule (false negative). The Meet code pattern and the Zoom
  titles are fairly stable. The Teams titles are the least certain and are only
  confirmed by L2b.
- **False positives:** a browser tab whose page title happens to start with
  `Meet - xxx-xxxx-xxx` (for example the post-call "rejoin" page, which may keep
  the code), a Teams calendar item opened in its own window titled "Meeting
  with ...", or a Zoom meeting window left open after the call ends.
- **False negatives:** the Meet tab is not the active tab, Meet is installed as
  an app whose window process is not `chrome.exe` or `msedge.exe`, Firefox (not
  covered), or the meeting runs in a remote desktop or another Windows account.
- Window titles can contain personal text (meeting names, people's names). This
  is why titles stay in memory and only `meet`, `zoom` or `teams` is kept.
- Listing windows every 5 s costs a little CPU. Not measured yet.
