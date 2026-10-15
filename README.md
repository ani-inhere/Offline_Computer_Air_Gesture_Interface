# Air Gesture Control (AGC)

100% offline, webcam-based air gesture control for laptops. AGC tracks one hand with the built-in webcam and maps deliberate hand motions to desktop actions: media control, window switching, scrolling, pointer control, volume/brightness, presentation navigation, and per-application automation (including Microsoft Word through its COM API).

> **Status:** design complete (PRD/TRD), automation backend in development. Items marked 🚧 are planned or in progress.

---

## Highlights

- **Fully offline.** No network connections, no telemetry, no cloud. Frames are never written to disk and never leave the engine process.
- **Perceptually instant.** Software-controlled latency target of 30 ms p95 or less; end-to-end 100 ms p95 or less on a typical 30 fps webcam.
- **Ergonomic.** Micro-gestures (thumb-index tap, two-finger scroll, wrist tilt) work with the forearm resting on the desk.
- **Hard to trigger by accident.** Explicit arming, dwell times, hysteresis, cooldowns, and no destructive default bindings.
- **Context-aware.** Per-application profiles; the same gesture does the right thing in VS Code, a browser, a call app, Figma, or PowerPoint.
- **Tiered automation.** OS adapters, then app APIs (Word COM), then PyAutoGUI as a universal fallback. 🚧
- **Visible state.** A click-through HUD always shows what the system sees and what it is doing.

## How it works

```
Webcam -> Capture (latest frame only) -> MediaPipe HandLandmarker
       -> One Euro filters + feature extraction (NumPy)
       -> Gesture engine (geometric rules + TFLite sequence model)
       -> Interaction state machine (NoHand / Tracking / Armed / modes / Cooldown)
       -> Context-aware mapper: (gesture, profile) -> ActionRequest
       -> Action dispatcher (non-blocking queues) 🚧
            |- Tier 1: OS adapters (media keys, volume, brightness)
            |- Tier 2: App-API adapters (Word via pywin32 COM) 🚧
            '- Tier 3: GUI automation (PyAutoGUI + OpenCV image locate) 🚧
       -> Result events -> IPC -> HUD toasts
```

Two processes keep real-time work isolated from the GUI:

| Process | Contents |
|---|---|
| **Engine** (headless) | Capture, inference, gestures, FSM, mapper, dispatcher, workers |
| **Shell** (UI) | HUD overlay, tray/menu bar, settings, calibration, profile editor (PySide6) |

They communicate over a local pipe/socket carrying landmarks and state only. No pixels cross the boundary.

### Why automation never freezes the camera

The decision loop only calls `dispatcher.submit()`, which is a non-blocking `put_nowait`. Slow work runs elsewhere:

- **COM worker thread:** a single thread that owns all Word COM objects (`CoInitialize` on that thread only).
- **GUI worker thread:** a single thread for PyAutoGUI, guarded by a global lock and arbitrated against air-mouse mode.
- **Priority channel:** key-up, `release_all()`, and panic requests bypass the normal queue and are never dropped.

## Gestures

| Gesture | Type | Default action |
|---|---|---|
| Open palm hold (500 ms) | Static | Arm |
| Open palm hold (1.5 s) | Static | Disarm |
| Fist hold | Static | Play/Pause |
| Swipe right / left | Dynamic | Next / previous track (slides in Presenter mode) |
| Two-finger flick right / left | Dynamic | Next / previous window |
| Peace hold | Static | Window switcher (modifier held, flick to step) |
| Thumb-index tap / double tap | Micro | Play/Pause / next track |
| Long pinch | Micro | Context-specific hold (push-to-mute in call apps) |
| Two-finger scroll | Micro | Mouse wheel |
| Wrist tilt left / right | Micro | Back/forward, or previous/next tab |
| Point pose | Mode | Air mouse (pinch = click/drag) |
| Slider pose | Mode | Volume (brightness with middle finger) |
| Knob pose + twist | Mode | Rate-control dial (volume or scrub) |

AGC treats gestures as **motions bound to commands**. It never interprets hand shapes as language.

## Interaction model

1. **Unarmed (default):** low-duty-cycle tracking. Only the wake gesture works.
2. **Armed:** gestures live; auto-disarm after 8 s idle (configurable).
3. **Modes** (Pointer, Slider, Dial, Presenter) are entered by holding a pose and exited by relaxing it.
4. **Safety:** panic hotkey disarms, releases every held key/button, cancels queued jobs, and pauses tracking.

## Profiles and actions

Profiles are plain YAML files in your config directory. Resolution order: application + title regex, application ID, category, then `global`. Profiles can `inherit` others.

```yaml
profiles:
  - id: word_workflow
    inherit: global
    match: { process: ["WINWORD.EXE"] }
    bindings:
      - gesture: DOUBLE_TAP
        action: { type: app_api, app: word, op: new_document }
      - gesture: LONG_PINCH
        action:
          type: app_api
          app: word
          op: export_pdf
          args: { out_dir: "~/Documents/AGC", name_template: "agc_{timestamp}" }
          gui_fallback: word_export_pdf_macro
      - gesture: FLICK_R
        action: { type: gui_macro, macro: browser_next_tab }
```

Action types: `media`, `hotkey`, `key_hold`, `momentary_toggle`, `hold_key_and_drag`, `scroll`, `pointer`, `slider`, `rate_key`, `app_api` 🚧, `gui_macro` 🚧, and `shell_command` (disabled unless you opt in).

### Automation tiers

| Tier | Mechanism | Strengths | Limits |
|---|---|---|---|
| 1. OS adapters | Media keys, Core Audio / `pactl`, brightness APIs | Fast, reliable, works while apps are in the background | OS-specific |
| 2. App APIs | `pywin32` COM (Word) | Reliable, fast, background-capable, doesn't touch your mouse | Windows-only for Word; needs the app installed |
| 3. GUI automation | PyAutoGUI + OpenCV template matching | Works with any app | Takes over mouse/keyboard; breaks if layouts change; blocked by elevated windows |

With `backend: auto`, the router tries OS, then app API, then GUI macro, and falls back to a GUI macro only when the action declares `gui_fallback`.

### Open a web page with a gesture

Hold a **peace sign** (index and middle finger up in a V) for half a second and AGC opens the project's GitHub page in a new Chrome tab. The link, browser (`chrome`, `edge`, `firefox` or `default`) and an opt-in Selenium backend are set in `agc/profiles/word_workflow.yaml`; see `docs/browser_ops.md`.

```yaml
      - gesture: PEACE_HOLD
        action:
          type: app_api
          app: chrome
          op: open_url
          args: { url: "https://github.com/ani-inhere/Offline_Computer_Air_Gesture_Interface" }
```

## Requirements

- Python 3.10-3.12 (check the MediaPipe wheel support for your platform)
- A standard RGB webcam, 640x480 or higher, 30 fps or higher
- CPU only; no GPU needed
- Windows 10/11 (Tier 1), macOS 12+ (Tier 1, with Camera and Accessibility permissions), Linux X11 (Tier 2), Linux Wayland (experimental)
- Microsoft Word installed for the Word adapter (Windows)

## Tech stack

| Layer | Technology |
|---|---|
| Capture | OpenCV |
| Hand tracking | MediaPipe Tasks `HandLandmarker` (bundled model) |
| Math and filters | NumPy (One Euro filter, feature extraction) |
| Dynamic gestures | TFLite / LiteRT int8 model (trained in PyTorch, dev only) |
| OS injection | PyAutoGUI behind adapters |
| Word automation | pywin32 (COM) 🚧 |
| UI | PySide6 (transparent click-through HUD, tray, settings) |
| IPC | `multiprocessing.connection` + msgpack |
| Config | YAML + pydantic |
| Packaging | PyInstaller or Nuitka, code-signed offline installers |
| Testing | pytest, hypothesis, landmark-trace replay harness |

## Project layout

```
agc/
├─ engine/
│  ├─ capture.py  tracker.py  filters.py  features.py  confidence.py  calibration.py
│  ├─ gestures/        static.py  micro.py  dynamic.py  continuous.py
│  ├─ fsm.py  mapper.py  profiles.py  ipc.py
│  ├─ context/         win.py  mac.py  x11.py  wayland.py
│  ├─ executor/        keyboard, mouse, media, audio, display adapters
│  └─ automation/ 🚧
│     ├─ models.py  dispatcher.py  workers.py  router.py  macros.py  locator.py
│     └─ adapters/     base.py  word_com.py  browser.py  gui_pyautogui.py
├─ shell/              hud.py  tray.py  settings.py  ipc.py
├─ models/             bundled .tflite + hashes
├─ profiles/           shipped default profiles (incl. word_workflow.yaml 🚧)
├─ assets/gui_templates/   image templates for GUI macros 🚧
├─ tools/              trace recorder/replayer, latency reports, training, simulate_gestures.py 🚧
├─ tests/
└─ docs/               PRD/TRD, word_ops.md 🚧, automation_backend.md 🚧
```

## Getting started (development)

```bash
git clone <repo-url> && cd agc
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # pywin32 installs on Windows only

python -m agc.engine               # start the engine (headless)
python -m agc.shell                # start the HUD/tray UI
```

First run walks you through camera and framing checks, dominant hand, calibration, and a guided tutorial.

### Useful developer commands

```bash
pytest                                   # full test suite
pytest -m word_live                      # Windows-only live Word integration test
python -m agc.tools.simulate_gestures    # feed scripted gestures to the mapper 🚧
python -m agc.tools.capture_template     # crop a GUI-macro template from a screenshot 🚧
python -m agc.tools.replay <trace.json>  # replay a recorded landmark trace
```

## Privacy and security

- **No network:** no HTTP/analytics dependencies; CI scans the import graph; the installer can add an outbound-deny firewall rule.
- **No imagery persistence:** frames live only in memory during inference; the UI process never receives pixels.
- **Camera visibility:** the camera opens only when tracking is on; Pause releases it so the hardware light goes off.
- **Safe actions:** no close/quit/delete/shutdown bindings by default; `shell_command` is opt-in; Word output paths are restricted to an approved folder and never overwrite existing files; imported profiles show an action diff before activation.
- **Verifiable:** a Privacy Proof panel lists active sockets, camera state, files written, and model hashes.

## Performance targets

| Metric | Target |
|---|---|
| Software-controlled latency (p95) | <= 30 ms |
| End-to-end latency, continuous controls (p95) | <= 100 ms |
| CPU, armed and tracking | <= 25% (4-core laptop) |
| CPU, unarmed, no hand | <= 8% |
| Memory | <= 500 MB |
| Unintended actions while armed | <= 1 per 2 hours |
| Unintended actions while unarmed | 0 |

## Testing

- Landmark-trace replay and golden traces for every gesture, including boundary cases
- False-activation corpus (typing, mouse use, talking with hands visible)
- State-machine property tests: all held inputs released on every exit path
- Non-blocking loop test: a 30 fps loop stays smooth while automation jobs take seconds 🚧
- Failure injection for the automation layer (Word missing or crashed, elevated windows, gesture storms) 🚧
- 8-hour soak test; network audit with all sockets blocked

## Known limitations

- A resting hand may fall outside the webcam's field of view; calibration checks this and an external webcam is supported.
- Camera sharing with video-call apps depends on the OS and device.
- Wayland restricts global input injection and foreground-app detection.
- PyAutoGUI cannot inject into elevated windows unless AGC is also elevated.
- Word COM automation is Windows-only; GUI macros depend on screen layout and DPI.
- Monocular depth is relative, so gestures use 2D motion and hand-scale-normalized geometry.

## Roadmap

- P0/P1: core macros, arming, HUD, micro-gestures, calibration, profiles, dial, sliders, air mouse, pinch-drag, presenter mode
- 🚧 Automation backend: dispatcher, Word COM adapter, GUI macro runner, router with fallback
- P2: personalized gesture templates, two-hand modifiers, relative/clutch pointer, AppleScript and LibreOffice adapters, Excel and PowerPoint adapters

## Documentation

- `docs/air-gesture-control-prd-trd-architecture.md`: product requirements, technical requirements, stack architecture
- `docs/automation_backend.md` 🚧: threading rules, adding adapters and macros
- `docs/word_ops.md` 🚧: Word operations mapped to VBA object-model calls
- `docs/browser_ops.md`: the peace-sign gesture that opens a URL, browser settings, opt-in Selenium backend

## License

To be decided.
