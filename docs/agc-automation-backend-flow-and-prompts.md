# AGC Automation Backend: Flow + Claude Code Prompts

Extends the existing PRD/TRD (engine process, FSM, mapper, executor). This adds a **tiered automation layer** so gestures can drive (1) native OS adapters, (2) app-specific APIs such as Word COM, and (3) PyAutoGUI as a universal fallback, all without ever blocking the camera loop.

---

## Part 1: Updated project flow

```
Webcam -> Capture thread (latest frame only)
       -> Inference thread (MediaPipe) -> filters/features
       -> Gesture engine (rules + TFLite) -> FSM (Armed/Modes/Cooldown)
       -> Mapper (gesture_event, profile) -> ActionRequest      <-- decision loop ends here (<6 ms)
                                                |
                                  bounded queue (non-blocking put)
                                                v
                                       ACTION DISPATCHER
                                 (job IDs, timeouts, cancel, results)
                                                |
                       +------------------------+-------------------------+
                       v                        v                         v
                TIER 1: OS adapters     TIER 2: App-API adapters   TIER 3: GUI automation
                (media keys, volume,    (Word via pywin32 COM,     (PyAutoGUI + OpenCV
                 brightness)             later Excel/PPT, etc.)     image locate, hotkeys)
                       |                        |                         |
                       +------------+-----------+-------------------------+
                                    v
                         ActionResult (ok / fail / fallback_used / duration)
                                    v
                         IPC -> HUD toast ("Exported PDF", "Word unavailable")
```

### Routing rule (per action, declared in the profile)

1. Try the **preferred tier** declared in the action (`backend: auto | os | app_api | gui`).
2. With `auto`: Tier 1 if an OS adapter exists, else Tier 2 if the app adapter is available and healthy, else Tier 3 if a GUI macro is defined, else fail with a clear HUD message.
3. A Tier 2 failure (app not installed, COM error, timeout) may fall back to Tier 3 only if the action defines `gui_fallback`. The fallback is logged and shown on the HUD.

### Threading model (the part that keeps the camera from freezing)

| Worker | Why it is separate |
|---|---|
| Capture + inference + decision loop | Never calls automation directly. Only does `dispatcher.submit(request)`, which is a non-blocking `put_nowait`. |
| **COM worker thread** (Windows) | COM objects have apartment affinity. One dedicated thread calls `pythoncom.CoInitialize()`, creates and owns the Word objects, and processes all Word jobs serially. Never touch COM objects from other threads. |
| **GUI worker thread** | PyAutoGUI is not thread-safe and takes over mouse/keyboard. One thread, one global `gui_lock`, serialized jobs. Arbitrates against the air-mouse (Pointer mode) so the two never move the cursor at once. |
| Release/panic path | `key-up`, `release_all()` and `cancel_all()` bypass the normal queue (priority channel). |

Rules: bounded queues; drop-oldest for duplicate/idempotent requests (e.g. repeated volume), never for releases; every job has a timeout; every job emits a result event; long jobs (Word PDF export) run async and report progress while FSM cooldown prevents re-trigger.

### New action types (extend CTX-5 in the TRD)

```yaml
- gesture: DOUBLE_TAP
  action:
    type: app_api
    app: word
    op: new_document            # open blank doc, background (Visible=False) or visible per setting
- gesture: LONG_PINCH
  action:
    type: app_api
    app: word
    op: export_pdf
    args: { out_dir: "~/Documents/AGC", name_template: "agc_{timestamp}" }
    gui_fallback: word_export_pdf_macro
- gesture: FLICK_R
  action:
    type: gui_macro
    macro: browser_next_tab      # PyAutoGUI hotkeys / image-locate steps
```

### Safety additions

- Output paths restricted to a user-approved folder; never overwrite without a unique name.
- No close/quit/delete ops in default profiles. Word `close` is opt-in and never discards unsaved changes.
- Text entered into Word comes from **predefined snippets/templates or the clipboard**, never from interpreting gestures as language (PRD non-goal).
- GUI macros run only when the target window is verified foreground; abort on mismatch.
- Panic gesture/hotkey: cancel queued jobs, release all inputs, stop GUI macros mid-run, leave COM objects intact.

### Build phases

| Phase | Deliverable |
|---|---|
| 0 | Repo primer (`CLAUDE.md`), dependency check |
| 1 | Action schema + dispatcher + worker infrastructure (no real apps yet, fakes only) |
| 2 | Word COM adapter on its own STA thread |
| 3 | PyAutoGUI GUI adapter + locator + macro runner + cursor arbitration |
| 4 | Router, fallback logic, profile/mapper integration |
| 5 | Wire into the live gesture loop + non-blocking proof (latency test) |
| 6 | Safety, panic, failure injection, full test suite |

---

## Part 2: Prompts for Claude Code (run in order, one per session or step)

### Prompt 0: Primer (save as `CLAUDE.md`, then run)

```
You are working on "Air Gesture Control" (AGC), a 100% offline Python app that maps webcam hand gestures to OS actions. The architecture is in docs/air-gesture-control-prd-trd-architecture.md (read Parts B7, B12, B15 and C1-C4 first). Engine process = capture thread, inference thread, gesture engine, FSM, mapper, executor. Do not modify vision/gesture code in this task unless I say so.

Task scope: build the AUTOMATION BACKEND under agc/engine/automation/. It lets gesture events trigger commands in apps via three tiers:
  Tier 1 OS adapters (media/volume), Tier 2 app-API adapters (Microsoft Word via pywin32 COM), Tier 3 PyAutoGUI GUI automation.

Hard rules:
- The decision loop must NEVER block on automation. It only calls dispatcher.submit(), which is non-blocking.
- COM objects live on ONE dedicated thread that calls pythoncom.CoInitialize()/CoUninitialize(). Never touch COM objects from other threads.
- PyAutoGUI: PAUSE=0, FAILSAFE=False, all calls on one GUI worker thread guarded by a global lock.
- Every job has an id, timeout, result event, and is cancellable. Release/panic requests use a priority channel and are never dropped.
- Fully offline; no network libraries. No destructive default actions.
- Code must be import-safe on non-Windows (pywin32 imported lazily; adapters report "unavailable").
- Python 3.10-3.12, type hints, pydantic models for schemas, pytest for tests. Small modules, docstrings on public APIs.

Before coding, reply with a short plan (files, classes, threading diagram) and wait for my "go".
```

### Prompt 1: Core schema, dispatcher, workers

```
Implement the automation core in agc/engine/automation/:

1. models.py (pydantic):
   - ActionRequest: id (uuid), type (os|app_api|gui_macro), app (optional), op, args (dict), backend (auto|os|app_api|gui), timeout_s, gui_fallback (optional macro name), priority (normal|release), dedupe_key (optional), created_ts (monotonic).
   - ActionResult: id, status (ok|failed|timeout|cancelled|unavailable|dropped), tier_used, fallback_used (bool), duration_ms, message, data (dict).
2. dispatcher.py: class ActionDispatcher
   - submit(req) -> job_id, non-blocking (put_nowait). Bounded queues; for normal priority drop-oldest when full AND coalesce by dedupe_key; priority=release goes to a separate unbounded-small channel that workers poll first.
   - register_backend(tier, adapter), set_result_callback(cb) (callbacks invoked on a separate notifier thread, never on a worker).
   - cancel(job_id), cancel_all(), shutdown(timeout).
   - Per-job timeout enforcement without killing threads (cooperative cancel token passed to adapters + watchdog that marks the job timed out and moves on).
3. workers.py: BaseWorker (thread with loop, health state, restart-on-crash with backoff), plus a ComWorker and GuiWorker subclass shells (no app logic yet). ComWorker calls pythoncom.CoInitialize on start when available.
4. adapters/base.py: abstract AppAdapter / OsAdapter / GuiAdapter with: is_available() -> (bool, reason), execute(request, cancel_token) -> ActionResult, shutdown().
5. Fake adapters in tests/fakes.py that sleep configurable durations and can raise.

Tests (pytest): submit never blocks >1 ms even when a worker is stuck sleeping 5 s; queue overflow coalescing; release priority jumps the queue; timeout produces a result and the worker recovers; cancel_all; worker crash restarts; callbacks fire off-thread.
Run the tests and show me the results.
```

### Prompt 2: Word COM adapter

```
Implement agc/engine/automation/adapters/word_com.py (WordAdapter, runs only inside ComWorker's thread).

Ops (all return ActionResult, never raise):
- new_document: attach to a running Word via GetActiveObject("Word.Application") else DispatchEx; Visible per setting (default False for background); Documents.Add(); remember the active doc handle.
- insert_text: args.text OR args.snippet_id (from a local snippets.yaml) OR args.source="clipboard". Insert at selection/end via Range.InsertAfter. Reject if no source given.
- insert_paragraph / set_style (Normal, Heading 1..3), basic and optional.
- export_pdf: ActiveDocument.ExportAsFixedFormat(OutputFileName, ExportFormat=17 /*wdExportFormatPDF*/). Output path must be inside the configured allowed output dir (default ~/Documents/AGC), created if missing; filename from name_template with timestamp; never overwrite (append -1, -2...). Return the path in data.
- save_as_docx: same path rules.
- status: Word installed? running? active doc name? (used by HUD/diagnostics)
- close_document: opt-in only (disabled unless config allow_close=true); never discards unsaved changes (SaveChanges=wdDoNotSaveChanges is forbidden; prompt-free save first or refuse).

Requirements:
- Handle: Word not installed (unavailable with reason), Word busy/RPC_E_CALL_REJECTED (retry with backoff, max N), user closed Word mid-session (re-attach), no active doc (create one if op allows, else fail clearly), pywintypes.com_error translated to readable messages.
- Respect cancel token between steps.
- Always set DisplayAlerts=wdAlertsNone only for the app instance we created, never for a Word the user already had open.
- Never quit a Word instance we did not launch. If we launched it, quit only on adapter shutdown and only if no unsaved documents.
- Include a thin COM wrapper layer so tests can run on Linux/macOS/CI with a mocked win32com.

Provide: unit tests with the mocked wrapper, plus a Windows-only integration test (pytest marker `word_live`) that creates a doc, inserts a snippet, exports a PDF to a temp allowed dir, and asserts the PDF exists. Also write docs/word_ops.md with a table mapping each op to the VBA object-model call it uses, since I am translating from VBA docs.
```

### Prompt 3: PyAutoGUI GUI adapter and macro runner

```
Implement the Tier 3 GUI automation in agc/engine/automation/adapters/gui_pyautogui.py plus macros.py.

1. GuiAdapter (runs in GuiWorker thread only):
   - Config at import: pyautogui.PAUSE=0, FAILSAFE=False.
   - Primitives: hotkey, press, key_hold/release (tracked in a ledger), type_text (clipboard-paste preferred for long text), click_at, scroll, move.
   - global gui_lock; release_all() always safe to call from any thread via the priority channel.
2. Locator (locator.py): find_on_screen(template_path, region=None, confidence=0.85, scales=(0.9,1.0,1.1), timeout_s) using OpenCV matchTemplate (grayscale, multi-scale), DPI-aware coordinates, multi-monitor aware. Returns center point + score or None. Templates live in agc/assets/gui_templates/. No network, no image saving to disk except explicit developer "capture template" tool.
3. Macro runner (macros.py): YAML-defined macros, e.g.
     name: word_export_pdf_macro
     requires_foreground: { process: ["WINWORD.EXE"] }
     steps: [ {hotkey:[f12]}, {wait_for_image: save_dialog.png, timeout: 3}, {click_image: pdf_type_dropdown.png}, ... ]
   Step types: hotkey, type, wait, wait_for_image, click_image, click_at_relative, scroll, assert_foreground.
   Each step checks the cancel token; macro aborts if the foreground window does not match requires_foreground (use the existing ForegroundAdapter interface; stub it if absent) and reports which step failed.
4. Cursor arbitration: expose a CursorOwner lock/flag shared with the Pointer-mode code; the macro runner must wait (bounded) or refuse if Pointer mode is active, and must restore the original cursor position after click_image steps.
5. Elevated-window detection: if injection is blocked, return status=failed with a message the HUD can show (see ACT-7).

Tests: locator on synthetic images (scaled/noisy), macro runner with a fake GuiAdapter (step order, abort on foreground mismatch, cancel mid-macro), ledger release_all. Add a developer CLI `python -m agc.tools.capture_template` to crop and save a template from a screenshot.
```

### Prompt 4: Router, fallback and profile integration

```
Implement agc/engine/automation/router.py and integrate with profiles/mapper.

1. Router: given an ActionRequest, choose the tier via the rules:
   - explicit backend wins (fail clearly if that backend is unavailable);
   - auto: os adapter if one handles the op -> app_api adapter if is_available() -> gui macro if request.gui_fallback or type==gui_macro -> unavailable.
   - If a Tier 2 result is failed/timeout/unavailable AND gui_fallback is set, enqueue the fallback on the GuiWorker, mark fallback_used=true, and keep a single logical job id for the HUD.
   - Cache adapter health (is_available) with a short TTL so we do not poll COM every gesture.
2. Extend profile schema (pydantic) with action types app_api and gui_macro (fields from docs: app, op, args, backend, gui_fallback, timeout_s, requires_foreground). Validate: unknown app/op rejected at load time with a specific error; out_dir must be inside the allowed folder; keep previous valid profile on failure (CTX-6).
3. Mapper stays pure: (gesture_event, profile) -> ActionRequest. Add the type conversion only.
4. Add shipped example profile profiles/word_workflow.yaml: DOUBLE_TAP -> word.new_document, LONG_PINCH -> word.export_pdf (with gui_fallback), TAP -> insert snippet "meeting_header".
5. Result handling: dispatcher result callbacks forward ActionResult to the IPC layer as HUD toast events (success text, failure reason, "used fallback").

Tests: routing matrix (all combos of availability), fallback single-job-id behaviour, profile validation errors, mapper purity.
```

### Prompt 5: Wire into the live loop and prove it is non-blocking

```
Integrate the automation layer into the running engine.

1. In the engine startup (agc/engine/app.py or equivalent) construct ActionDispatcher, register adapters, start ComWorker and GuiWorker, and pass dispatcher.submit into the existing mapper/executor hook. Keep the existing media/volume path working: those become Tier 1 requests with dedupe_key for continuous controls.
2. FSM integration: when a discrete action is submitted, enter Cooldown immediately (do not wait for the result). For long jobs (export_pdf), also set a per-op "busy" flag so the same op cannot be queued twice.
3. Create tools/simulate_gestures.py: feeds scripted gesture events (and optionally a recorded landmark trace via the existing replay harness) into the mapper at realistic timing while a synthetic 30 fps loop runs.
4. Create tests/test_nonblocking_loop.py: run a 30 fps fake vision loop for 20 s while the Word fake adapter takes 4 s per job and the GUI fake takes 2 s; assert the loop's frame interval p99 stays under 40 ms and no frame is skipped by more than one period; log per-stage timings.
5. Shutdown path: engine stop -> cancel_all -> release_all -> stop workers -> CoUninitialize, with a 3 s bound; verify no thread leaks.

Show me the latency report and any places where the decision loop touched a lock held by a worker (there must be none).
```

### Prompt 6: Safety, failure injection, hardening

```
Harden the automation backend.

1. Panic path: a single dispatcher.panic() that cancels queued jobs, signals cooperative cancel to running jobs, calls GuiAdapter.release_all(), clears busy flags, and emits a HUD event. Hook it to the existing panic hotkey/gesture and to hand-loss > 150 ms (FSM-5).
2. Failure injection tests: Word not installed, Word crashes mid-export, COM call rejected repeatedly, disk full / path not writable, foreground window changes during a macro, elevated window in foreground, duplicate gesture storms (100 events in 1 s), worker thread exception. Each must end with a result event, a released input ledger, and a working dispatcher afterwards.
3. Security review pass: confirm no shell execution, no network imports (add a CI check scanning the import graph for requests/urllib3/socket-based clients), output path traversal tests ("..", symlinks, UNC paths), snippet ids cannot inject code.
4. Observability: structured local logs (op, tier, duration, status; never text content, never imagery), and a Debug-HUD stats provider exposing queue depths, worker health, last results.
5. Write docs/automation_backend.md: architecture diagram, threading rules, how to add a new app adapter (checklist), how to add a GUI macro and capture templates, known limits (Windows-only Word COM, elevated windows, DPI scaling).

Finish by running the entire test suite and report coverage for agc/engine/automation/.
```

---

## Part 3: Notes for you

- **Why COM gets its own thread:** COM objects are bound to the apartment that created them. Using the same Word object from the camera thread or a thread pool causes intermittent `RPC_E_WRONG_THREAD` errors, so route everything through one owner thread.
- **Word COM is Windows-only.** For macOS/Linux, the same `AppAdapter` interface can later wrap AppleScript/JXA (macOS) or LibreOffice UNO; the router needs no changes.
- **Typing text:** keep it to snippets, templates, or clipboard content. Gestures stay motions mapped to commands, per the PRD non-goal.
- **Run order matters:** each prompt assumes the earlier ones finished with passing tests. Paste Prompt 0 as `CLAUDE.md` once, then use plan mode for Prompts 1 to 6.
- **Developer-only dependencies:** `pywin32` (Windows), `PyAutoGUI`, `opencv-python`, `pydantic`, `pytest`, `hypothesis`. No new runtime network libraries.
