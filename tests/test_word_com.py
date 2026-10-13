"""WordAdapter against the fake COM object model (runs on any OS)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fake_word import FakeComBackend, FakeComError, FakeWordApp, FakeWorld
from helpers import make_request

from agc.engine.automation import ActionType, CancelToken, Status, Tier
from agc.engine.automation.adapters.com_wrapper import (
    DISP_E_EXCEPTION,
    REGDB_E_CLASSNOTREG,
    RPC_E_CALL_REJECTED,
    RPC_E_SERVERCALL_RETRYLATER,
    ComErrorKind,
    translate_com_error,
)
from agc.engine.automation.adapters.word_com import (
    WD_ALERTS_NONE,
    WD_EXPORT_FORMAT_PDF,
    WD_SAVE_CHANGES,
    WordAdapter,
    WordConfig,
)


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "AGC"


@pytest.fixture
def snippets_file(tmp_path: Path) -> Path:
    path = tmp_path / "snippets.yaml"
    path.write_text("snippets:\n  meeting_header: \"Meeting notes - {date}\\n\"\n  plain: \"{not a field}\"\n",
                    encoding="utf-8")
    return path


@pytest.fixture
def make_adapter(out_dir: Path, snippets_file: Path):
    def factory(backend: FakeComBackend | None = None, **overrides) -> tuple[WordAdapter, FakeComBackend]:
        backend = backend or FakeComBackend()
        settings = {"allowed_output_dir": out_dir, "snippets_path": snippets_file,
                    "retry_initial_s": 0.001, "retry_max_s": 0.005, **overrides}
        config = WordConfig(**settings)
        adapter = WordAdapter(config, backend)
        adapter.thread_started()
        return adapter, backend

    return factory


def run(adapter: WordAdapter, op: str, token: CancelToken | None = None, **args):
    request = make_request(op, type=ActionType.APP_API, app="word", args=args)
    result = adapter.execute(request, token or CancelToken())
    assert result.id == request.id
    return result


# ------------------------------------------------------------ availability


def test_word_not_installed_is_unavailable(make_adapter):
    adapter, _ = make_adapter(FakeComBackend(installed=False))
    assert adapter.is_available() == (False, "Microsoft Word is not installed")
    result = run(adapter, "new_document")
    assert result.status is Status.UNAVAILABLE and "not installed" in result.message
    status = run(adapter, "status")
    assert status.status is Status.OK and status.data["installed"] is False


def test_class_not_registered_at_dispatch_is_unavailable(make_adapter):
    adapter, backend = make_adapter()

    def not_registered(progid):
        raise FakeComError(REGDB_E_CLASSNOTREG)

    backend.dispatch_ex = not_registered
    result = run(adapter, "new_document")
    assert result.status is Status.UNAVAILABLE and "not installed" in result.message


def test_unknown_op_fails_cleanly(make_adapter):
    adapter, _ = make_adapter()
    result = run(adapter, "delete_everything")
    assert result.status is Status.FAILED and "unknown Word op" in result.message


# ------------------------------------------------------------ new_document


def test_new_document_launches_hidden_word_and_silences_only_it(make_adapter):
    adapter, backend = make_adapter()
    result = run(adapter, "new_document")
    assert result.ok and result.data == {"document": "Document1", "launched": True}
    (app,) = backend.launched
    assert app._visible is False
    assert app._alert_writes == [WD_ALERTS_NONE]
    assert [d._name for d in app._docs] == ["Document1"]


def test_new_document_attaches_to_users_word_without_touching_settings(make_adapter):
    world = FakeWorld()
    users_word = FakeWordApp(world, visible=True)
    adapter, backend = make_adapter(FakeComBackend(running=users_word))
    result = run(adapter, "new_document")
    assert result.ok and result.data["launched"] is False
    assert backend.launched == []
    assert users_word._alert_writes == [] and users_word._visible is True


def test_attach_to_running_can_be_disabled(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    adapter, backend = make_adapter(FakeComBackend(running=users_word), attach_to_running=False)
    assert run(adapter, "new_document").data["launched"] is True
    assert len(backend.launched) == 1 and users_word._docs == []


def test_visible_setting_applies_to_launched_word(make_adapter):
    adapter, backend = make_adapter(visible=True)
    run(adapter, "new_document")
    assert backend.launched[0]._visible is True


# ------------------------------------------------------------- insert_text


def test_insert_text_sources(make_adapter):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    doc = backend.launched[0]._docs[0]

    assert run(adapter, "insert_text", text="Hello. ").ok
    assert run(adapter, "insert_text", snippet_id="meeting_header").ok
    backend.clipboard = "from clipboard"
    assert run(adapter, "insert_text", source="clipboard").ok
    assert doc._text.startswith("Hello. Meeting notes - 20")
    assert doc._text.endswith("\nfrom clipboard")


@pytest.mark.parametrize("args, message", [
    ({}, "exactly one of"),
    ({"text": "a", "snippet_id": "meeting_header"}, "exactly one of"),
    ({"source": "keyboard"}, "source must be 'clipboard'"),
    ({"source": "clipboard"}, "clipboard has no text"),
    ({"snippet_id": "../../etc/passwd"}, "snippet_id must be"),
    ({"snippet_id": "nope"}, "no snippet named"),
    ({"text": ""}, "non-empty"),
    ({"text": "x" * 20_001}, "longer than"),
    ({"text": "x", "position": "top"}, "position must be"),
])
def test_insert_text_rejects_bad_sources(make_adapter, args, message):
    adapter, _ = make_adapter()
    result = run(adapter, "insert_text", **args)
    assert result.status is Status.FAILED and message in result.message


def test_snippet_braces_other_than_date_time_stay_literal(make_adapter):
    adapter, backend = make_adapter()
    assert run(adapter, "insert_text", snippet_id="plain").ok
    assert backend.launched[0]._docs[0]._text == "{not a field}"


def test_insert_text_creates_a_document_when_none_is_open(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    adapter, _ = make_adapter(FakeComBackend(running=users_word))
    assert run(adapter, "insert_text", text="hi").ok
    assert [d._text for d in users_word._docs] == ["hi"]


def test_insert_at_selection_in_visible_word_collapses_caret(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    doc = users_word.add_user_document()
    adapter, _ = make_adapter(FakeComBackend(running=users_word))
    assert run(adapter, "insert_text", text="typed", position="selection").ok
    assert doc._text == "typed" and doc._collapses == [0]
    assert "Selection.InsertAfter" in users_word._world.log


def test_selection_falls_back_to_end_for_hidden_word(make_adapter):
    adapter, backend = make_adapter()
    assert run(adapter, "insert_text", text="x", position="selection").ok
    assert "Selection.InsertAfter" not in backend.world.log
    assert "Range.InsertAfter" in backend.world.log


def test_insert_paragraph_and_set_style(make_adapter):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    assert run(adapter, "insert_paragraph", text="Agenda").ok
    assert run(adapter, "set_style", style="heading 1").ok
    doc = backend.launched[0]._docs[0]
    assert doc._text == "\nAgenda" and doc._styles == [-2]
    bad = run(adapter, "set_style", style="Title")
    assert bad.status is Status.FAILED and "Heading 1" in bad.message


# --------------------------------------------------------------- export_pdf


def test_export_pdf_writes_unique_files_inside_allowed_dir(make_adapter, out_dir):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    paths = [Path(run(adapter, "export_pdf", name_template="report").data["path"]) for _ in range(3)]
    assert [p.name for p in paths] == ["report.pdf", "report-1.pdf", "report-2.pdf"]
    assert all(p.parent == out_dir.resolve() and p.read_bytes().startswith(b"%PDF") for p in paths)
    doc = backend.launched[0]._docs[0]
    assert [fmt for _, fmt in doc._exports] == [WD_EXPORT_FORMAT_PDF] * 3


def test_export_pdf_default_template_uses_timestamp(make_adapter, out_dir):
    adapter, _ = make_adapter()
    run(adapter, "new_document")
    name = Path(run(adapter, "export_pdf").data["path"]).name
    assert name.startswith("agc_20") and name.endswith(".pdf")


@pytest.mark.parametrize("args, message", [
    ({"out_dir": "../escape"}, "must be inside"),
    ({"out_dir": "C:/Windows" if Path("C:/").exists() else "/etc"}, "must be inside"),
    ({"name_template": "{secret}"}, "unknown placeholder"),
    ({"name_template": 5}, "must be a string"),
])
def test_export_pdf_rejects_unsafe_paths(make_adapter, args, message):
    adapter, _ = make_adapter()
    run(adapter, "new_document")
    result = run(adapter, "export_pdf", **args)
    assert result.status is Status.FAILED and message in result.message


def test_export_pdf_template_cannot_escape_folder(make_adapter, out_dir):
    adapter, _ = make_adapter()
    run(adapter, "new_document")
    path = Path(run(adapter, "export_pdf", name_template="../../{doc}/evil").data["path"])
    assert path.parent == out_dir.resolve()
    assert path.name == "_.._Document1_evil.pdf"


def test_export_pdf_subfolder_inside_allowed_dir_is_created(make_adapter, out_dir):
    adapter, _ = make_adapter()
    run(adapter, "new_document")
    path = Path(run(adapter, "export_pdf", out_dir="minutes", name_template="m").data["path"])
    assert path == out_dir.resolve() / "minutes" / "m.pdf"


def test_export_pdf_without_document_fails_clearly(make_adapter):
    adapter, _ = make_adapter()
    result = run(adapter, "export_pdf")
    assert result.status is Status.FAILED and result.message == "no document is open in Word"


def test_failed_export_removes_reserved_file(make_adapter, out_dir):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    backend.world.fail("Document.ExportAsFixedFormat", DISP_E_EXCEPTION, description="Disk is full.")
    result = run(adapter, "export_pdf", name_template="r")
    assert result.status is Status.FAILED and result.message == "Word reported: Disk is full."
    assert list(out_dir.iterdir()) == []


def test_save_as_docx(make_adapter, out_dir):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    first = run(adapter, "save_as_docx", name_template="notes")
    second = run(adapter, "save_as_docx", name_template="notes")
    assert Path(first.data["path"]).name == "notes.docx"
    assert Path(second.data["path"]).name == "notes-1.docx"


# ------------------------------------------------------- busy, gone, cancel


def test_busy_word_is_retried_with_backoff(make_adapter):
    adapter, backend = make_adapter()
    backend.world.fail("Documents.Add", RPC_E_CALL_REJECTED, times=2)
    backend.world.fail("Documents.Add", RPC_E_SERVERCALL_RETRYLATER, times=1)
    assert run(adapter, "new_document").ok
    assert backend.world.log.count("Documents.Add") == 4


def test_busy_word_gives_up_after_max_retries(make_adapter):
    adapter, backend = make_adapter(max_retries=2)
    backend.world.fail("Documents.Add", RPC_E_CALL_REJECTED, times=10)
    result = run(adapter, "new_document")
    assert result.status is Status.FAILED and "busy" in result.message
    assert backend.world.log.count("Documents.Add") == 3


def test_busy_inside_excepinfo_scode_is_retried(make_adapter):
    adapter, backend = make_adapter()
    backend.world.fail("Documents.Add", DISP_E_EXCEPTION, description="Call was rejected by callee.",
                       scode=RPC_E_CALL_REJECTED)
    assert run(adapter, "new_document").ok


def test_user_closed_word_mid_session_reattaches(make_adapter):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    first = backend.launched[0]
    first.kill()
    result = run(adapter, "insert_text", text="after restart")
    assert result.ok
    assert len(backend.launched) == 2
    assert backend.launched[1]._docs[0]._text == "after restart"


def test_word_dying_during_an_op_reattaches_once(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    users_word.add_user_document()
    backend = FakeComBackend(running=users_word)
    adapter, _ = make_adapter(backend)
    assert run(adapter, "status").data["running"]

    original = FakeWordApp.Documents.fget

    def die_then_work(self):
        if self is users_word:
            users_word.kill()
            backend.running = None
        return original(self)

    FakeWordApp.Documents = property(die_then_work)
    try:
        result = run(adapter, "new_document")
    finally:
        FakeWordApp.Documents = property(original)
    assert result.ok and result.data["launched"] is True


def test_user_closed_agc_document_falls_back_to_active(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    user_doc = users_word.add_user_document()
    adapter, _ = make_adapter(FakeComBackend(running=users_word))
    run(adapter, "new_document")
    users_word._docs[-1].Close(WD_SAVE_CHANGES)  # the user closes AGC's document
    assert run(adapter, "insert_text", text="here").ok
    assert user_doc._text == "here"


def test_cancelled_before_start(make_adapter):
    adapter, backend = make_adapter()
    token = CancelToken()
    token.cancel()
    result = run(adapter, "new_document", token)
    assert result.status is Status.CANCELLED
    assert backend.launched == []


def test_timeout_during_busy_retries_stops_early(make_adapter):
    adapter, backend = make_adapter(max_retries=20, retry_initial_s=0.05, retry_max_s=0.05)
    backend.world.fail("Documents.Add", RPC_E_CALL_REJECTED, times=20)
    token = CancelToken()
    threading.Timer(0.08, token.cancel, args=("timeout",)).start()
    result = run(adapter, "new_document", token)
    assert result.status is Status.TIMEOUT
    assert backend.world.log.count("Documents.Add") < 6


def test_refuses_calls_off_its_com_thread(make_adapter):
    adapter, _ = make_adapter()
    results = []
    worker = threading.Thread(target=lambda: results.append(run(adapter, "status")))
    worker.start()
    worker.join()
    assert results[0].status is Status.FAILED and "off its COM thread" in results[0].message


# ------------------------------------------------------------------- status


def test_status_reports_without_launching_word(make_adapter):
    adapter, backend = make_adapter()
    result = run(adapter, "status")
    assert result.ok and result.data["running"] is False and result.message == "Word is not running"
    assert backend.launched == []

    users_word = FakeWordApp(FakeWorld(), visible=True)
    users_word.add_user_document("Plan.docx")
    backend.running = users_word
    data = run(adapter, "status").data
    assert data["running"] and data["active_document"] == "Plan.docx" and data["document_count"] == 1
    assert data["launched_by_agc"] is False


# ---------------------------------------------------------- close_document


def test_close_document_disabled_by_default(make_adapter):
    adapter, backend = make_adapter()
    run(adapter, "new_document")
    result = run(adapter, "close_document")
    assert result.status is Status.FAILED and "disabled" in result.message
    assert len(backend.launched[0]._docs) == 1


def test_close_document_saves_untitled_doc_first_and_never_discards(make_adapter, out_dir):
    adapter, backend = make_adapter(allow_close=True)
    run(adapter, "new_document")
    run(adapter, "insert_text", text="keep me")
    doc = backend.launched[0]._docs[0]
    result = run(adapter, "close_document", name_template="autosave")
    assert result.ok and Path(result.data["saved_to"]).name == "autosave.docx"
    assert doc._close_args == [WD_SAVE_CHANGES]
    assert backend.launched[0]._docs == []


def test_close_document_never_closes_users_documents(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    users_word.add_user_document()
    adapter, _ = make_adapter(FakeComBackend(running=users_word), allow_close=True)
    result = run(adapter, "close_document")
    assert result.status is Status.FAILED and "only closes documents it created" in result.message
    assert len(users_word._docs) == 1


# ----------------------------------------------------------------- shutdown


def test_shutdown_quits_word_we_launched_when_all_saved(make_adapter):
    adapter, backend = make_adapter()
    run(adapter, "new_document")  # blank new document counts as saved
    adapter.shutdown()
    assert backend.launched[0]._quit_called


def test_shutdown_keeps_word_with_unsaved_work_and_shows_it(make_adapter):
    adapter, backend = make_adapter()
    run(adapter, "insert_text", text="unsaved")
    adapter.shutdown()
    app = backend.launched[0]
    assert not app._quit_called and app._visible is True


def test_shutdown_never_quits_users_word(make_adapter):
    users_word = FakeWordApp(FakeWorld(), visible=True)
    adapter, _ = make_adapter(FakeComBackend(running=users_word))
    run(adapter, "new_document")
    adapter.shutdown()
    assert not users_word._quit_called


# ------------------------------------------------------- error translation


def test_translate_com_error_messages():
    word_err = translate_com_error(FakeComError(DISP_E_EXCEPTION, description="  Bad file name.  "))
    assert word_err.kind is ComErrorKind.APP_ERROR and word_err.message == "Word reported: Bad file name."
    other = translate_com_error(FakeComError(0x80004005, "Unspecified error"))
    assert other.kind is ComErrorKind.OTHER and other.message == "COM error 0x80004005: Unspecified error"
    busy = translate_com_error(FakeComError(RPC_E_CALL_REJECTED))
    assert busy.kind is ComErrorKind.BUSY


# ------------------------------------------------------ through the dispatcher


def test_runs_on_the_com_worker_thread(make_dispatcher, collector, make_adapter, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: None,
                                                                        CoUninitialize=lambda: None))
    adapter, backend = make_adapter()
    adapter._owner_thread = None  # let the worker thread claim it
    d = make_dispatcher()
    d.register_backend(Tier.APP_API, adapter)
    d.start()
    ids = [d.submit(make_request(op, type=ActionType.APP_API, app="word", args=args))
           for op, args in [("new_document", {}), ("insert_text", {"snippet_id": "meeting_header"}),
                            ("export_pdf", {"name_template": "minutes"})]]
    results = collector.wait_all(ids)
    assert [r.status for r in results] == [Status.OK] * 3
    assert all(r.tier_used is Tier.APP_API for r in results)
    assert Path(results[2].data["path"]).name == "minutes.pdf"
    d.shutdown(timeout=2)
    assert backend.world.threads == {d.worker(Tier.APP_API).thread_ident}
    # Dispatcher shutdown must reach adapter.shutdown while the COM handles still exist:
    # the document has unsaved text, so Word stays open and is made visible (not quit).
    app = backend.launched[0]
    assert app._visible is True and not app._quit_called


def test_dispatcher_shutdown_quits_launched_word_when_saved(make_dispatcher, collector, make_adapter, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: None,
                                                                        CoUninitialize=lambda: None))
    adapter, backend = make_adapter()
    d = make_dispatcher()
    d.register_backend(Tier.APP_API, adapter)
    d.start()
    job = d.submit(make_request("new_document", type=ActionType.APP_API, app="word"))
    assert collector.wait_for(job).ok
    assert d.shutdown(timeout=2)
    assert backend.launched[0]._quit_called
