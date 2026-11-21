# Stage 2: Background app (tray, auto-pause, autostart, install)

Branch `stage-2-background` -> `main`. Spec: `docs/specs/stage-2.md` (approved).
Not opened yet: Stage 1 is not merged to `main`. This body will be used when it is.

## What changed

- **Pause supervisor** (`policy/pausing.py`): combines camera-in-use, screen
  lock, battery (threshold plus 5-point hysteresis, Battery Saver) and manual /
  calibrating reasons into one decision. Camera-in-use clears only after a 10 s
  grace period, tracked separately for each app.
- **Monitor**: takes an optional `supervisor`; never opens the camera while any
  reason is active, and releases it on the first check after a reason appears.
  It writes one `pause` / `resume` event for each reason that starts or stops.
  It adds `camera_mode` (`hold` / `burst`) and presence-check mode (after 5 min
  away: camera closed between samples, 30 s interval). It also gains
  `apply_config` so new settings apply without a restart.
- **Windows probes** (`platform/`): ConsentStore registry parser (`camera_usage.py`)
  that excludes our own `python.exe` / `pythonw.exe` *and* the base interpreter a
  uv venv launcher starts; lock state via the input desktop (`power.py`); battery
  via psutil plus `GetSystemPowerStatus` for Battery Saver; named-mutex single
  instance; HKCU Run-key autostart; a `Registry` protocol with a `winreg` adapter.
- **Tray** (`tray.py`): pure `menu_model` / `tray_status` plus a thin pystray
  icon that refreshes every 2 s and rebuilds the menu only when it changes.
- **Feedback** (`actions.py`, new `labels` table): mark latest alert wrong;
  "I'm slouching now" stores a label linked to the latest sample within 10 s.
- **Calibration and settings windows** (`calibrate_ui.py`, `settings_ui.py`):
  Tkinter views over testable controllers, started as separate `pythonw`
  processes (`python -m posture_checker --gui calibrate|settings`).
  `calibrate_flow.py` pauses with `calibrating`, waits for the window, and
  reloads the calibration on success. `ConfigWatcher` applies saved settings
  within about 2 s. `save_config` now writes a temporary file and swaps it in
  (atomic).
- **App** (`app.py`, `__main__.py`, `posture-checker app`): monitor thread plus
  tray. Logs to a rotating file (1 MB x 3). After an unhandled error it shows one
  toast and restarts after 30 s. Quit joins the monitor thread and releases the
  camera.
- **Install** (`install.ps1`, `uninstall.ps1`, `posture_checker.install`):
  per-user, idempotent; uninstall keeps data unless `-RemoveData`.
- **Camera sharing probe** (`scripts/camera_share_probe.py`) for live check L1;
  `docs/results/camera-sharing.md` is a placeholder marked "not measured yet".
- **Privacy tests extended**: repo scripts are scanned for image writers and
  network imports. The `labels` table must hold only short scalars. A run with
  logging, an error and a label must leave only db, log and config files, and
  the log must hold no image data. `camera-sharing.md` must be text only: no
  image signatures, data URIs, long base64 runs or image links.

## Spec coverage

| # | Behaviour | Tests |
|---|---|---|
| 1 | Camera sharing probe | `test_camera_probe_script_exists_and_imports_without_opening_a_camera`; live part `test_camera_probe_live_part` (skipped, L1); `test_privacy.py::test_no_script_writes_images_or_uses_the_network`, `test_camera_sharing_result_is_text_only` |
| 2 | `camera_mode` hold / burst | `test_burst_mode_closes_camera_after_each_sample`, `test_hold_mode_keeps_camera_open_between_samples`; `test_monitor_background.py::test_switching_to_burst_releases_camera_at_next_sample` |
| 3 | Supervisor, never open while paused | `test_supervisor_each_probe_gives_its_reason`, `test_supervisor_manual_reason_and_combinations`, `test_paused_step_never_opens_camera_and_closes_it_first_time`; `test_monitor_background.py::test_paused_interval_is_at_most_five_seconds`; `test_app.py::test_run_loop_acts_on_a_pause_without_waiting_for_the_next_sample` |
| 4 | Registry camera-in-use probe | `test_registry_entry_in_use_rules`, `test_registry_reports_app_name`, `test_registry_ignores_own_python_executables`, `test_registry_missing_key_is_not_in_use_and_logs_once`, `test_real_registry_key_parses`; `test_camera_usage.py` (publisher-id stripping, dedupe, base interpreter, case, unreadable key) |
| 5 | Pause <= 10 s, resume by 15 s | `test_other_app_starting_pauses_within_10s_and_resume_after_grace`; `test_pausing.py::test_each_camera_app_has_its_own_grace_period`, `test_app_returning_during_grace_restarts_it` |
| 6 | Screen locked | `test_lock_pauses_and_unlock_resumes_within_10s` (probe itself: live check L3) |
| 7 | Low battery | `test_battery_pause_table` (7 cases); `test_pausing.py::test_saver_without_battery_never_pauses`, `test_saver_off_again_uses_threshold_with_hysteresis`, `test_threshold_change_takes_effect` |
| 8 | Manual pause / resume | `test_resume_clears_only_the_manual_reason`, `test_timed_manual_pause_expires`, `test_tray_tooltip_names_automatic_reason_while_manually_paused`; `test_app.py::test_tray_pause_and_resume_actions`; `test_monitor_background.py::test_toast_pause_goes_through_supervisor` |
| 9 | Nobody present | `test_nobody_present_switches_to_presence_check_mode`; `test_monitor_background.py::test_away_streak_is_broken_by_a_present_sample` |
| 10 | Pause / resume events | `test_pause_and_resume_events_once_per_cause`; `test_monitor_background.py::test_toast_pause_goes_through_supervisor` |
| 11 | Tray menu | `test_menu_has_all_items_when_running`, `test_menu_manually_paused_enables_resume`, `test_menu_auto_paused_keeps_resume_disabled`, `test_menu_mark_wrong_enabled_only_for_unmarked_recent_alert`; `test_tray.py`; `test_app.py::test_mark_wrong_state_follows_alerts` |
| 12 | Icon colour and tooltip | `test_tray_status_colour_and_tooltip`, `test_tray_tooltip_exact_camera_text`; `test_tray.py` (multi-reason, truncation, icon drawing). 2 s refresh: live check L3 |
| 13 | Mark latest alert wrong | `test_mark_latest_alert_wrong_marks_newest_only_once` |
| 14 | "I'm slouching now" | `test_slouching_now_labels_recent_sample`, `test_slouching_now_without_recent_sample_has_null_sample_id`; `test_app.py::test_tray_label_action_records_and_confirms`; `test_privacy.py::test_labels_table_holds_only_short_scalars` |
| 15 | Calibrate... | `test_calibrate_success_reloads_calibration`, `test_calibrate_failure_keeps_old_calibration`; `test_calibrate_ui.py` (countdown, progress, success, failure, camera unavailable, cancel); `test_app.py::test_calibrate_action_runs_in_background_once` |
| 16 | Settings... | `test_settings_valid_values_are_saved`, `test_settings_invalid_values_return_message_and_leave_file`, `test_config_watcher_applies_saved_settings_within_5s`; `test_settings_ui.py` |
| 17 | Start without calibration | `test_start_without_calibration_toasts_and_is_amber`; `test_app.py::test_startup_with_calibration_shows_nothing_and_is_green` |
| 18 | Background, rotating log | `test_app_constructs_with_fakes`, `test_logging_goes_to_a_rotating_file_not_stdout`; `test_app.py::test_setup_logging_twice_keeps_one_file_handler`; `test_privacy.py::test_log_file_holds_no_image_data_and_folder_stays_clean` |
| 19 | Single instance | `test_second_instance_lock_fails` (real mutex and toast: live check) |
| 20 | Quit releases camera | `test_run_stops_within_5s_and_releases_camera` |
| 21 | Monitor error restart | `test_monitor_error_is_logged_toasted_once_and_restarts_after_30s`; `test_app.py::test_error_toast_is_shown_only_once` |
| 22 | install.ps1 / autostart | `test_enable_autostart_writes_one_run_value`, `test_register_adds_aumid_and_run_value`, `test_register_without_autostart_skips_run_value`, `test_install_script_mentions_run_value_and_aumid`; `test_app.py::test_register_then_unregister_leaves_registry_empty`; live check L6 |
| 23 | uninstall.ps1 | `test_unregister_removes_run_value_and_aumid`, `test_unregister_on_empty_registry_raises_nothing`, `test_unregister_keeps_data_unless_remove_data`, `test_uninstall_script_exists`; `test_app.py::test_unregister_survives_registry_errors`, `test_remove_data_deletes_the_whole_folder` |
| 24 | Settings autostart toggle | `test_settings_autostart_toggle_uses_the_same_run_value`; `test_settings_ui.py::test_current_includes_autostart` |

Checks run locally: `ruff format --check`, `ruff check`, `mypy`, `pytest`
(319 passed, 1 skipped: the L1 live test) and `scripts/check_ownership.py`, all passing.

## Not done or not verified

**Decision 1 is still open.** The default `camera_mode` stays `"hold"`. The
acceptance tests assume this: tests 3, 5, 6 and 10 expect the camera to stay
open between steps under the default config. `docs/results/camera-sharing.md`
is a placeholder that says "not measured yet". After L1, if holding blocks
another app, change the default to `"burst"` (and adjust whichever tests rely
on the default) in a follow-up.

**Deviation from decision 4.** pystray items with a submenu cannot also be
clicked. The test also fixes the top-level labels exactly. So "I'm slouching
now" is a submenu with *Slouching / Head forward / Too close to the screen*:
recording `slouch` takes a hover plus a click, not one click.

**Not verified on this machine (needs a desktop session, webcam or the live checks):**
- Lock detection (`OpenInputDesktop` / `SwitchDesktop` heuristic): it reported
  "not locked" in a read-only probe, but the locked case is untested (L3).
- Battery Saver detection via `GetSystemPowerStatus.SystemStatusFlag`: it read
  "off" at 100 % on AC power. The on-battery and saver cases are untested (L5).
- The "stop == 0 means in use" registry rule while a real call runs (L2).
  Whether our own camera use is recorded under the venv `python(w).exe` or the
  base interpreter is also untested; both are excluded.
- The pystray icon, its 2 s refresh and the Tk windows were never shown.
  pystray loads (`pystray._win32.Icon`) and the icon image draws in memory.
- Toast `message()` with an "Open" button (Calibrate to start, already
  running, error, label confirmation) is untested on a real desktop.
- Named-mutex single instance and the 2 s "already running" toast.
- install.ps1 / uninstall.ps1 were not executed, only parsed by the test.
- Sign-out and shutdown release the camera only because the process ends:
  there is no explicit session-end handler.
- `posture-checker app` started from a terminal keeps that console. Only
  `pythonw -m posture_checker app` (what autostart and install use) runs
  without a console.
- After 5 min away, presence-check mode samples every 30 s. The run loop still
  re-checks pause reasons every 5 s, so a lock or call is acted on promptly.
- No CPU, memory or battery numbers are claimed (Stage 6).

**Owner live checks still to run** (spec section 5): L1 camera sharing (then
answer decision 1), L2 video call, L3 lock, L4 sign out and in, L5 low battery,
L6 install / uninstall, L7 tray actions, L8 nobody present.

## Review findings addressed

Each fix has a unit test that fails without it.

| # | Severity | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | Major | An entry with `LastUsedTimeStop == 0` left by a crashed or hibernated app paused us forever | Entries whose `LastUsedTimeStart` is older than `STALE_AFTER_S` (12 h) are ignored and logged once. The check uses an injected FILETIME clock, which the app passes (`filetime_now`). See the note below. | `test_camera_usage.py::test_entry_open_longer_than_12_hours_is_stale_and_logged_once`, `test_filetime_now_matches_wall_clock` |
| 2 | Major | Two quick calibrate requests could start two windows | `start_calibration` claims a lock without blocking before it spawns the worker. A second request returns False. The tray shows `calibrating` from the moment of the claim. | `test_app.py::test_second_calibration_request_is_refused_synchronously` |
| 3 | Minor | Tray pause and resume could block on the monitor lock | Tray actions only update the supervisor. The worker's 5 s pause poll releases the camera. | `test_app.py::test_tray_pause_does_not_wait_for_the_monitor` |
| 4 | Minor | A probe that raised crashed every step; lock-probe failures were silent | Each probe is wrapped: a failure is logged once and counts as no reason. `OpenInputDesktop` "access denied" means locked; any other error is logged once and treated as unlocked. | `test_pausing.py::test_failing_probe_counts_as_no_reason_and_logs_once`, `test_failing_battery_probe_never_pauses`; `test_power.py` |
| 5 | Minor | The error toast never showed again after a recovery | Reset after the first successful step | `test_app.py::test_error_toast_shown_again_after_a_recovery` |
| 6 | Minor | Tray refresh cost and thread safety | Refresh every 2 s. The latest-alert state is cached for 10 s and reset at once by a new alert or "mark wrong". `refresh()` runs under a lock. | `test_tray.py::test_refresh_interval_meets_spec_target`, `test_concurrent_refreshes_are_serialised`; `test_app.py::test_alert_state_is_cached_and_refreshed_on_new_alert` |
| 7 | Minor | The scripts matched processes by command-line text | The shared `scripts/posture-checker-processes.ps1` matches this checkout's `.venv\Scripts\{pythonw,python,posture-checker}.exe`, plus their child processes (the base interpreter that the uv launcher starts). Install checks only the app; uninstall also stops the calibrate and settings windows. The helper ran read-only here (0 matches; the app-only regex was checked on sample command lines). | `test_install_scripts.py` |
| 8 | Minor | A second instance opened the first one's log | `claim_single_instance` takes the mutex before `setup_logging()` | `test_app.py::test_second_instance_exits_before_touching_the_log` |
| A | Re-review | After the finding-3 fix, the tray kept its old colour for up to 5 s after Pause or Resume | `pause_manual` / `resume_manual` update the supervisor's current reasons under its lock straight away, adding or removing only `manual` | `test_app.py::test_tray_status_follows_pause_and_resume_without_a_monitor_step` |
| B | Re-review | The stale check depended on untested wiring in `main()` | `build_camera_usage(registry, db)` passes `filetime_now`; `main()` uses it | `test_app.py::test_app_camera_probe_has_the_stale_check_on` |

**Note on finding 1.** The stale check is off unless a FILETIME clock is
passed in; the real app always passes one. The acceptance tests build entries
with `LastUsedTimeStart = 100` and expect them to count as in use. As a
FILETIME, 100 is in the year 1601, so a check that was always on would mark
them stale and fail those tests. The tests can't be edited, so the check is
opt-in.

Left as is, as agreed: the `store()` / `save()` alias, the slouch submenu, and
the default `camera_mode = "hold"`.

## New dependencies

- `pystray>=0.19`: the notification-area icon and menu (spec decision 3,
  behaviour 11). It pulls in `six`.
- `pillow>=10`: draws the coloured tray icon in memory, which pystray
  requires. It was already installed through other dependencies; it is now
  declared because the app imports it directly. No image is loaded or saved.
- Tkinter: part of the standard library; `import tkinter` works in the uv
  Python 3.12 (Tk 8.6). It is not a new package.
