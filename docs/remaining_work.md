# Remaining work

Status as of 15 Oct 2026. Phases 0–3 of the automation backend are complete (see [project_guide.md](project_guide.md)). This file lists everything still to do, starting with the automation backend and then the rest of the PRD.

Done since the first version of this list:
- `agc/engine/gesture_bridge.py` and `agc/profiles/word_workflow.yaml` connect `Vision_test.py` gestures (TAP, DOUBLE_TAP, LONG_PINCH) to Word. This is a first version of the Phase 4 mapper and profile.
- New PEACE_HOLD gesture opens a URL in Chrome ([browser_ops.md](browser_ops.md)).
- `Anii_part` merged into `main`.

## 1. Automation backend: remaining phases

Prompts for each phase are in [agc-automation-backend-flow-and-prompts.md](agc-automation-backend-flow-and-prompts.md).

### Phase 4: router, fallback and profiles

- [ ] `agc/engine/automation/router.py`
  - An explicit `backend` wins; fail clearly if that backend is unavailable.
  - `auto`: OS adapter → app-API adapter (if available) → GUI macro → unavailable.
  - If Tier 2 fails, times out or is unavailable and `gui_fallback` is set, run the macro on the GUI worker. Keep **one job id** and set `fallback_used`.
  - Cache adapter health (`is_available`) with a short TTL.
- [ ] Profile schema (pydantic) with `app_api` and `gui_macro` action types: `app`, `op`, `args`, `backend`, `gui_fallback`, `timeout_s`, `requires_foreground`.
  - Unknown app/op rejected at load with a specific error.
  - `out_dir` must be inside the allowed folder.
  - Keep the previous valid profile when a new one fails (CTX-6).
- [ ] Pure mapper: `(gesture_event, profile) -> ActionRequest` (CTX-8).
- [ ] Example profile `profiles/word_workflow.yaml`:
  - `DOUBLE_TAP` → `word.new_document`
  - `LONG_PINCH` → `word.export_pdf`, with GUI fallback
  - `TAP` → `meeting_header` snippet
- [ ] Forward results to the IPC layer as HUD toast events: success text, failure reason, "used fallback".
- [ ] Tests: routing matrix, single-job-id fallback, profile validation errors, mapper purity.

### Phase 5: wire into the live loop

- [ ] Engine startup in `agc/engine/app.py`: build the dispatcher, register adapters, start workers.
- [ ] FSM integration:
  - Enter Cooldown as soon as a discrete action is submitted.
  - Per-op "busy" flag for long jobs such as `export_pdf`.
- [ ] `tools/simulate_gestures.py`: scripted gesture events at realistic timing alongside a synthetic 30 fps loop.
- [ ] `tests/test_nonblocking_loop.py`:
  - Run for 20 s at 30 fps, with Word fake jobs taking 4 s and GUI fake jobs 2 s.
  - Frame-interval p99 must stay below 40 ms.
  - No frame may be skipped by more than one period.
- [ ] Shutdown path: stop → `cancel_all` → `release_all` → stop workers → `CoUninitialize`, bounded at 3 s, with no thread leaks.
- [ ] Latency report, plus an audit that the decision loop never touches a lock held by a worker.

### Phase 6: safety and hardening

- [ ] `dispatcher.panic()`: cancel queued jobs, cancel running ones, `release_all`, clear busy flags, emit a HUD event.
  - Hook it to the panic hotkey/gesture and to hand loss longer than 150 ms (FSM-5).
- [ ] Failure-injection tests:
  - Word not installed, Word crashes mid-export, COM call repeatedly rejected.
  - Disk full or path not writable.
  - Foreground window changes mid-macro, elevated window in front.
  - 100 gesture events in 1 s, worker exception.
- [ ] Security pass:
  - No shell execution.
  - CI import-graph check that no network library is imported (PRV-1).
  - Path traversal tests for `..`, symlinks and UNC paths.
  - Snippet-id injection tests.
- [ ] Observability:
  - Structured local logs (op, tier, duration, status; never text or images).
  - A debug-HUD stats provider.
- [ ] `docs/automation_backend.md`: architecture, threading rules, how to add an adapter or macro, known limits.
- [ ] Full test suite with a coverage report for `agc/engine/automation/`.

## 2. Gaps from earlier phases

- [ ] **Tier 1 OS adapter.** No prompt assigns it, but Phase 5 assumes it exists.
  - Native media keys (MAC-3).
  - Volume via `pycaw` and brightness via `screen_brightness_control` (SLD-3).
  - Media keys currently go through PyAutoGUI.
- [ ] **ACT-8 rate limits:** at most 10 key events per second per binding, 30 scroll events per second, 60 pointer moves per second.
- [ ] **`word_export_pdf_macro` end-to-end run.**
  - Capture `word_save_as_dialog.png` and `word_save_as_type.png` with `python -m agc.tools.capture_template`.
  - Verify the steps; they assume the English Word UI.
- [ ] **UWP apps:** resolve the real app behind `ApplicationFrameHost.exe` (B7.1).
- [ ] **Python 3.10–3.12:** compatibility is unverified; only 3.13 is installed on the dev machine.
- [ ] **Symlink-escape test:** run it with Windows Developer Mode on; it's skipped today.

## 3. Repository housekeeping

- [x] **Sync the branches.** `Anii_part` was merged into `main` on 15 Oct 2026; `main` is now the branch to work on.
- [ ] **Install Google Chrome**, or set `browser: edge` / `default` in the profile; the dev machine has no Chrome.
- [ ] **Delete `tempCodeRunnerFile.py`** (a VS Code Code Runner leftover on `main`) and add it to `.gitignore`.
- [ ] **Remove the duplicate flow doc** from the repo root; the copy in `docs/` is the one to keep.
- [ ] **Install MediaPipe** on the dev machine (`pip install mediapipe`) to run `Vision_test.py`; Phase 5 needs it.

## 4. Tap / Double Tap code vs the PRD

`Vision_test.py` on `main`. This is vision code owned by the vision team; listed here for alignment before Phase 5.

| Current code | PRD says |
|---|---|
| Double-tap gap 500 ms | 600 ms (B8.2) |
| ~~Emits `TAP` on the first tap and `DOUBLE_TAP` on the second~~ (**fixed on `main`**: `TAP` now waits for the double-tap window) | `DOUBLE_TAP` should replace the first `TAP` |
| Hand scale `s` from 2D image landmarks | World landmarks (B3.4) |
| `num_hands=2`; detection, presence and tracking confidence all 0.3 | `num_hands=1` (SYS-4); 0.6 / 0.5 / 0.5 (VIS-9) |
| `time.time()` timestamps; frames not mirrored | Strictly increasing monotonic timestamps (VIS-8); mirrored frames (VIS-4) |
| `pinch_frames` not reset when a brief pinch doesn't count as held | Debounce on consecutive frames (FSM-3) |
| No palm-travel check on `TAP` | Palm centroid travel < 0.15·s during a tap (B8.2) |

## 5. Rest of the product (PRD Part C4)

Not started; likely split across the team.

**Engine (`agc/engine/`)**
- [ ] `capture.py`: capture thread with a latest-frame-only buffer (VIS-1…6)
- [ ] `tracker.py`: MediaPipe HandLandmarker wrapper (VIS-7…10)
- [ ] `filters.py`: One Euro filter, EMA, hysteresis (VIS-11…13)
- [ ] `features.py`: hand scale, pinch ratio, finger extension, palm normal, roll and tilt (B3.4)
- [ ] `confidence.py`: composite confidence score and bands (B3.5)
- [ ] `calibration.py`: per-user thresholds and neutral pose (A7.1)
- [ ] `gestures/`: static poses, micro-gestures, dynamic gestures (TFLite), continuous controls (B5, B8–B11)
- [ ] `fsm.py`: arming, modes, cooldown, release on every exit (B4)
- [ ] `context/`: foreground-app watcher (B7.1)
- [ ] `ipc.py`: engine-side channel carrying landmarks and state only, never pixels (C1)

**UI process (`agc/shell/`)**
- [ ] HUD overlay, tray icon, settings and calibration wizard, profile editor (PySide6; A7, B9.2)

**Delivery**
- [ ] Packaging (PyInstaller or Nuitka), signed offline installer (C6)
- [ ] Privacy Proof panel (PRV-7)

## Suggested order

1. Sync the branches.
2. Phase 4: router and profiles.
3. Tier 1 OS adapter.
4. Phase 5: connect the real tap detector to Word actions.
5. Phase 6: hardening.
