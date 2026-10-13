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

## Repo notes

- Automation backend flow, routing rules, phases and per-phase prompts: docs/agc-automation-backend-flow-and-prompts.md.
- Existing code: Vision_test.py (MediaPipe hand-landmarker prototype, uses hand_landmarker.task). Treat it as vision code: do not modify.
- docs/air-gesture-control-prd-trd-architecture.md is not in the repo yet. Until it is added, PRD IDs cited in the flow doc (CTX-5, CTX-6, ACT-7, FSM-5) are interpreted from that doc's descriptions.
- Work branch: Anii_part (remote: github.com/ani-inhere/Offline_Computer_Air_Gesture_Interface).
