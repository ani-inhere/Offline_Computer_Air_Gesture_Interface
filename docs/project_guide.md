# Air Gesture Control: project guide

Status as of 14 Oct 2026: phases 0–3 of the automation backend are complete (see [agc-automation-backend-flow-and-prompts.md](agc-automation-backend-flow-and-prompts.md)).

## 1. What the project is

Air Gesture Control (AGC) turns hand gestures seen by a laptop webcam into computer actions, such as pausing music, switching browser tabs, or creating and exporting a Word document. Everything runs **100% offline** (PRD SYS-1, PRV-1).

The full pipeline (PRD B2):

```
Webcam -> MediaPipe hand landmarks -> gesture engine -> state machine -> mapper
                                                                           |
                                                     ActionRequest  -------+
                                                                           v
                                                  ACTION DISPATCHER  (built)
                                    +----------------------+----------------------+
                                    v                      v                      v
                             Tier 1: OS            Tier 2: Word COM      Tier 3: PyAutoGUI
                             (Phase 4/5)              (built)                (built)
```

What exists today:

| Part | State |
|---|---|
| `Vision_test.py` | Webcam + MediaPipe prototype that draws hand landmarks (vision team) |
| Automation backend `agc/engine/automation/` | **Built and tested** (phases 1–3) |
| `agc/engine/gesture_bridge.py` + `agc/profiles/word_workflow.yaml` | Connects `Vision_test.py` gestures (TAP, DOUBLE_TAP, LONG_PINCH, PEACE_HOLD) to actions |
| `agc/engine/gestures/static.py` | Peace-sign pose and hold detector ([browser_ops.md](browser_ops.md)) |
| Full gesture engine, FSM, HUD | Not built yet; `gesture_tester.py` fires gestures by hand |

## 2. API keys

**There are none.** AGC uses no cloud service and has no accounts or API keys, and nothing it does goes over the network. That is a hard product requirement (PRV-1), not an oversight. The only "APIs" are local ones:

| Local API | Used for | Library |
|---|---|---|
| Microsoft Word COM object model | Create documents, insert text, export PDF | `pywin32` (`win32com.client`) |
| Windows input (SendInput) | Hotkeys, typing, clicks, scrolling, media keys | `PyAutoGUI` |
| Windows user32/kernel32/advapi32 | Foreground window, elevation check, DPI, monitors | `ctypes` (standard library) |
| Screen capture | Finding buttons on screen for GUI macros | `Pillow` (`ImageGrab`) + `OpenCV` |
| Clipboard | Pasting long text | `pywin32` (`win32clipboard`) |

## 3. Completed work

| Phase | Done | Main files |
|---|---|---|
| 0 | Repo primer and docs: `CLAUDE.md`, PRD/TRD, flow doc | `CLAUDE.md`, `docs/` |
| 1 | Dispatcher, per-tier worker threads, queues, timeouts, cancellation, crash restart | `dispatcher.py`, `workers.py`, `queues.py`, `watchdog.py`, `notifier.py`, `jobs.py`, `cancel.py`, `models.py` |
| 2 | Word automation over COM; safe output paths; text snippets | `adapters/word_com.py`, `adapters/com_wrapper.py`, `outputs.py`, `snippets.py`, `docs/word_ops.md` |
| 3 | Keyboard/mouse automation, on-screen image finder, YAML macros, cursor sharing with the air mouse, admin-window detection, template capture tool | `adapters/gui_pyautogui.py`, `locator.py`, `macros.py`, `cursor.py`, `win32_util.py`, `agc/tools/capture_template.py` |
| – | Manual gesture tester | `gesture_tester.py` |

Tests: 169 automated tests (`python -m pytest`), plus an opt-in test against real Word (`python -m pytest -m word_live`).

## 4. Tutorial: `gesture_tester.py`

The tester stands in for the camera. You pick a gesture, and it sends the matching action through the **real** dispatcher and adapters, exactly as the live gesture loop will in Phase 5.

### 4.1 Setup (once)

```powershell
cd "C:\Users\Anirudh G\OneDrive\Desktop\IDP\Offline_Computer_Air_Gesture_Interface"
python -m pip install pydantic PyYAML pywin32 PyAutoGUI opencv-python pytest
```

### 4.2 Start safely: dry run

```powershell
python gesture_tester.py --dry-run
```

Nothing really happens in this mode; each action prints `[dry run] would do: ...`. Use it to learn the menu:

```
gesture> 1                 <- fire gesture #1 (DOUBLE_TAP)
gesture> tap               <- or type the gesture name (any case)
gesture> s                 <- worker health and queue sizes
gesture> r                 <- release all held keys (priority channel)
gesture> l                 <- show the gesture table again
gesture> q                 <- quit
```

Each gesture prints two lines:

```
  -> TAP: Word: insert the 'meeting_header' snippet  (submit returned in 40 us)   <- sent (never waits)
  <- TAP: [OK ] Text inserted  (85 ms)                                           <- result arrives later
```

The `submit returned in … us` figure shows that the camera loop is never blocked: it only queues the request, and a worker thread does the work.

### 4.3 The gestures

```powershell
python gesture_tester.py --list
```

| # | Gesture | Tier | Action |
|---|---|---|---|
| 1 | DOUBLE_TAP | word | New blank document |
| 2 | TAP | word | Insert the `meeting_header` snippet |
| 3 | PINCH_TEXT | word | Insert a line of sample text |
| 4 | TILT_U | word | Make the last paragraph Heading 1 |
| 5 | LONG_PINCH | word | Export PDF to `Documents\AGC` |
| 6 | TILT_R | word | Save as `.docx` to `Documents\AGC` |
| 7 | TILT_L | word | Status (installed / running / document); never starts Word |
| 8 | FIST_HOLD | gui | Media play/pause |
| 9 | SWIPE_R | gui | Media next track |
| 10 | SWIPE_L | gui | Media previous track |
| 11 | FLICK_R | gui | Browser next tab (macro) |
| 12 | FLICK_L | gui | Browser previous tab (macro) |
| 13 | SCROLL_2F | gui | Scroll down 3 clicks |
| 14 | PEACE_HOLD | browser | Open the project's GitHub page in Edge (`--browser chrome` / `firefox` / `default` to change) |

These bindings are samples kept in `BINDINGS` at the top of `gesture_tester.py`; edit them freely. In Phase 4 they move into a profile YAML file.

### 4.4 Live run: Word walkthrough

```powershell
python gesture_tester.py
```

1. `7` (TILT_L): status. Expect `Word is not running`, or the name of your open document.
2. `1` (DOUBLE_TAP): Word opens with a new document. If Word was already open, AGC uses it and leaves its settings alone.
3. `2` (TAP): the meeting header with today's date appears.
4. `3` (PINCH_TEXT): a sample line is added.
5. `5` (LONG_PINCH): a PDF is written to `C:\Users\<you>\Documents\AGC\agc_<date-time>.pdf`. Run it again and you get `...-1.pdf`; nothing is ever overwritten.
6. `q`: quit. Word stays open because the document is unsaved, so you decide what to do with it.

Or as a script:

```powershell
python gesture_tester.py --run DOUBLE_TAP,TAP,PINCH_TEXT,LONG_PINCH --delay 2
```

### 4.5 Live run: media and browser gestures

GUI gestures send real key presses to whatever window is focused.

- **Media (8–10):** start music or a video first. Media keys reach the player even when it's not the focused window.
- **Browser tabs (11–12):** after you choose the gesture, you get a 3-second countdown. Click your browser during it. If the browser isn't in front when the macro runs, it refuses: `foreground is WindowsTerminal.exe, macro needs chrome.exe/...`.
- **Scroll (13):** same countdown; click the window you want scrolled.

Change the countdown with `--countdown 5`.

### 4.6 Things you will see on purpose

| Message | Why |
|---|---|
| `no document is open in Word` | Export/save with no document. Use DOUBLE_TAP first. |
| `Can't control X: it runs as administrator` | Windows blocks input into admin windows (PRD ACT-7). |
| `Word is busy (close any open dialog in Word…)` | A Word dialog is open; AGC retried 5 times, then gave up. |
| `macro needs chrome.exe/...` | The wrong window was in front for a browser macro. |

### 4.7 Options

| Option | Effect |
|---|---|
| `--dry-run` | Print actions only |
| `--list` | Show the table and exit |
| `--run G1,G2` | Fire gestures in order, then exit |
| `--delay S` | Seconds between gestures with `--run` (default 1) |
| `--countdown S` | Seconds to focus a window before GUI gestures (default 3) |
| `--hidden-word` | Start Word in the background instead of visibly |
| `--browser B` | Browser for PEACE_HOLD: `edge` (default), `chrome`, `firefox`, `default` |

## 5. How the code works

### 5.1 Request lifecycle

```
mapper/tester builds ActionRequest
   -> dispatcher.submit(request)                  returns a job id in microseconds
   -> queue for the tier (os / app_api / gui)
   -> that tier's worker thread runs adapter.execute(request, cancel_token)
   -> ActionResult (ok / failed / timeout / cancelled / unavailable / dropped)
   -> result callback on the notifier thread      (later: HUD toast)
```

Every request ends with exactly one result, whatever happens: success, error, timeout, cancellation, a full queue, or no adapter for the tier.

### 5.2 Data models (`models.py`)

**`ActionRequest`**

| Field | Meaning | Example |
|---|---|---|
| `type` | `os`, `app_api` or `gui_macro` | `app_api` |
| `app` | Target app for `app_api` | `word` |
| `op` | Operation name | `export_pdf` |
| `args` | Operation arguments | `{"name_template": "report"}` |
| `backend` | `auto` or a forced tier | `auto` |
| `timeout_s` | Deadline from submission (queue wait included) | `10` |
| `priority` | `release` skips the queue and is never dropped | `normal` |
| `dedupe_key` | Newer request replaces a queued one with the same key | `volume` |
| `gui_fallback` | GUI macro to try if Tier 2 fails (router, Phase 4) | `word_export_pdf_macro` |

**`ActionResult`**: `id`, `status`, `tier_used`, `fallback_used`, `duration_ms`, `message` (HUD text), `data` (for example the exported PDF path).

### 5.3 Dispatcher (`dispatcher.py`)

| Function | What it does |
|---|---|
| `ActionDispatcher(queue_size=32, stuck_grace_s=1.0)` | Create it |
| `register_backend(tier, adapter)` | Attach an adapter; creates that tier's worker thread |
| `set_result_callback(fn)` | Receive every result (called on the notifier thread) |
| `start()` | Start all threads |
| `submit(request) -> job_id` | Queue a request without waiting (the only call the camera loop makes) |
| `cancel(job_id)` / `cancel_all()` | Cancel one or all jobs (`cancel_all` keeps release jobs) |
| `stats()` | Queue sizes and worker health (for the debug HUD) |
| `shutdown(timeout=3)` | Cancel, run pending releases, stop threads, quit Word if safe |

Supporting modules:
- **`workers.py`:** `OsWorker`, `ComWorker` (the only thread allowed to touch Word: `CoInitialize`/`CoUninitialize`), and `GuiWorker` (holds `gui_lock`). Each restarts itself after a crash.
- **`queues.py`:** a release channel, which is never dropped, plus a bounded normal channel with dedupe and drop-oldest.
- **`watchdog.py`:** timeouts.
- **`notifier.py`:** result delivery.
- **`cancel.py`:** `CancelToken`, which adapters check between steps.

### 5.4 Word adapter (`adapters/word_com.py`)

`WordAdapter(WordConfig(...))` supports these ops: `new_document`, `insert_text`, `insert_paragraph`, `set_style`, `export_pdf`, `save_as_docx`, `status`, and `close_document` (off unless `allow_close=True`). The full VBA mapping is in [word_ops.md](word_ops.md).

Main `WordConfig` settings:

| Setting | Default |
|---|---|
| `visible` | `False` (for a Word instance that AGC starts) |
| `attach_to_running` | `True` (use your open Word) |
| `allowed_output_dir` | `~/Documents/AGC` |
| `default_name_template` | `agc_{timestamp}` |
| `snippets_path` | `agc/config/snippets.yaml` |
| `allow_close` | `False` |
| `max_retries` | `5` |

**Safety rules:**
- Never quits or silences a Word instance you started.
- Never overwrites files.
- Writes only inside the allowed folder.
- Never discards unsaved changes.

**Supporting modules:**
- **`com_wrapper.py`:** `PyWin32Backend` and `translate_com_error()`, which turns COM error codes into messages.
- **`outputs.py`:** `resolve_output_dir()`, `render_name()`, `reserve_unique()`, `next_free_path()`.
- **`snippets.py`:** `SnippetStore.get(id)`.

### 5.5 GUI adapter (`adapters/gui_pyautogui.py`)

`GuiAutomationAdapter(GuiConfig(...))` supports these ops: `hotkey`, `press`, `key_hold`, `key_release`, `type_text`, `click_at`, `move`, `scroll`, `release_all`, `run_macro`, or a macro name used directly as the op.

- PyAutoGUI is configured with `PAUSE=0` and `FAILSAFE=False` (ACT-1, ACT-2).
- Held keys are tracked in `InputLedger`, and `release_all()` works from any thread (ACT-4, FSM-5).
- Text longer than 20 characters, or with non-ASCII characters, is pasted via the clipboard, and your previous clipboard text is restored.
- Clicks put your cursor back where it was afterwards.

Supporting modules:

| Module | Main API |
|---|---|
| `macros.py` | `MacroLibrary.from_dir()`, `MacroRunner.run(macro, token)`; step types: `hotkey`, `type`, `wait`, `wait_for_image`, `click_image`, `click_at_relative`, `scroll`, `assert_foreground` |
| `locator.py` | `ScreenLocator.find_on_screen(template, region, confidence=0.85, scales=(0.9, 1.0, 1.1), timeout_s)`, `match_template()` |
| `cursor.py` | `CursorArbiter.set_pointer_active()` (air mouse, never blocks), `acquire()`/`release()`/`hold()` (macros) |
| `win32_util.py` | `WindowsForegroundProbe.current()` (process, title, elevated, window rect), `current_process_elevated()`, `ensure_dpi_aware()`, `virtual_screen_rect()` |
| `tools/capture_template.py` | `python -m agc.tools.capture_template NAME.png [--delay 3 \| --region L T W H \| --from-image F]` |

### 5.6 Config files

| File | What it holds |
|---|---|
| `agc/config/snippets.yaml` | Text snippets for `insert_text` / `type_text` (`{date}` and `{time}` are filled in) |
| `agc/config/macros/*.yaml` | GUI macros (`browser_next_tab`, `browser_prev_tab`, `word_export_pdf_macro`) |
| `agc/assets/gui_templates/` | Button images captured for macros |

## 6. Running the tests

```powershell
python -m pytest                    # 169 tests, about 6 s, no real Word or input
python -m pytest -m word_live       # starts a hidden Word, exports a real PDF, quits Word
python -m pytest tests/test_word_com.py -v   # one area, verbose
```

## 7. Next phases

| Phase | Work |
|---|---|
| 4 | Router (`auto` tier choice, Word → GUI fallback), profile YAML schema, pure mapper |
| 5 | Wire into the live camera loop; prove the loop never stalls (frame-time p99 < 40 ms) |
| 6 | Panic path, failure-injection tests, network-import CI check, `docs/automation_backend.md` |
