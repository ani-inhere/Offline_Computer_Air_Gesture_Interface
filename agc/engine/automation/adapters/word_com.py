"""Tier 2 adapter: Microsoft Word over COM (pywin32), run only on the ComWorker thread.

Ops (``request.args`` in brackets):

* ``new_document``                     - attach to a running Word (or start one) and add a blank document.
* ``insert_text`` [text | snippet_id | source="clipboard", position]
* ``insert_paragraph`` [text, position]
* ``set_style`` [style: Normal | Heading 1..3, position]
* ``export_pdf`` [out_dir, name_template] - writes a new PDF inside the allowed output folder.
* ``save_as_docx`` [out_dir, name_template]
* ``status``                           - installed / running / documents, for HUD and diagnostics.
* ``close_document``                   - opt-in (``allow_close``); saves first, never discards changes.

Target document: the last document AGC created, if still open; otherwise
Word's active document. ``position`` is ``end`` (default) or ``selection``
(only honoured when the target is the active document of a visible Word).

Safety: never quits or silences (DisplayAlerts) a Word the user started;
never overwrites files; writes only inside ``allowed_output_dir``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..cancel import CancelledError, CancelToken
from ..models import ActionRequest, ActionResult, Status
from ..outputs import DEFAULT_OUTPUT_DIR, OutputPathError, render_name, reserve_unique, resolve_output_dir
from ..snippets import DEFAULT_SNIPPETS_PATH, SnippetError, SnippetStore
from .base import AppAdapter
from .com_wrapper import ComBackend, ComError, ComErrorKind, PyWin32Backend, translate_com_error

log = logging.getLogger("agc.automation.word")

WORD_PROGID = "Word.Application"

# Word object-model constants (values from the VBA reference; see docs/word_ops.md).
WD_EXPORT_FORMAT_PDF = 17      # WdExportFormat.wdExportFormatPDF
WD_FORMAT_XML_DOCUMENT = 12    # WdSaveFormat.wdFormatXMLDocument (.docx)
WD_ALERTS_NONE = 0             # WdAlertLevel.wdAlertsNone
WD_SAVE_CHANGES = -1           # WdSaveOptions.wdSaveChanges
WD_COLLAPSE_END = 0            # WdCollapseDirection.wdCollapseEnd
#: WdBuiltinStyle ids work in every Word language, unlike style names.
WD_BUILTIN_STYLES = {"normal": -1, "heading 1": -2, "heading 2": -3, "heading 3": -4}

T = TypeVar("T")


class WordConfig(BaseModel):
    """Settings for :class:`WordAdapter`."""

    model_config = ConfigDict(extra="forbid")

    visible: bool = Field(False, description="Show a Word instance that AGC starts itself")
    attach_to_running: bool = Field(True, description="Use the user's open Word instead of starting a new one")
    allowed_output_dir: Path = DEFAULT_OUTPUT_DIR
    default_name_template: str = "agc_{timestamp}"
    snippets_path: Path = DEFAULT_SNIPPETS_PATH
    allow_close: bool = False
    max_retries: int = Field(5, ge=0, le=20)
    retry_initial_s: float = Field(0.1, gt=0)
    retry_max_s: float = Field(1.0, gt=0)
    max_text_chars: int = Field(20_000, gt=0)


class OpError(Exception):
    """A request the adapter refuses or cannot complete; becomes a FAILED result."""


class WordAdapter(AppAdapter):
    """Word automation. Every COM object it holds belongs to the ComWorker thread."""

    app = "word"
    name = "word-com"
    ops = frozenset({
        "new_document", "insert_text", "insert_paragraph", "set_style",
        "export_pdf", "save_as_docx", "status", "close_document",
    })

    def __init__(self, config: WordConfig | None = None, backend: ComBackend | None = None) -> None:
        self.config = config or WordConfig()
        self.backend: ComBackend = backend or PyWin32Backend()
        self.snippets = SnippetStore(self.config.snippets_path)
        self._owner_thread: int | None = None
        self._app: Any = None
        self._doc: Any = None          # last document AGC created
        self._launched = False         # True if AGC started the current Word instance
        self._handlers: dict[str, Callable[[ActionRequest, CancelToken], ActionResult]] = {
            "new_document": self._op_new_document,
            "insert_text": self._op_insert_text,
            "insert_paragraph": self._op_insert_paragraph,
            "set_style": self._op_set_style,
            "export_pdf": self._op_export_pdf,
            "save_as_docx": self._op_save_as_docx,
            "status": self._op_status,
            "close_document": self._op_close_document,
        }

    # ------------------------------------------------------------ adapter API

    def is_available(self) -> tuple[bool, str]:
        """Word installed and pywin32 present. Reads the registry only; safe from any thread."""
        return self.backend.check(WORD_PROGID)

    def thread_started(self) -> None:
        self._owner_thread = threading.get_ident()

    def thread_stopped(self) -> None:
        # The apartment is going away (stop or crash-restart): COM references die with it.
        self._drop_handles()

    def execute(self, request: ActionRequest, cancel_token: CancelToken) -> ActionResult:
        """Run one op. Never raises: every outcome is an ActionResult."""
        if self._owner_thread is None:
            self._owner_thread = threading.get_ident()
        elif threading.get_ident() != self._owner_thread:
            return ActionResult.for_request(request, Status.FAILED, "WordAdapter called off its COM thread")
        handler = self._handlers.get(request.op)
        if handler is None:
            return ActionResult.for_request(request, Status.FAILED, f"unknown Word op {request.op!r}")
        if request.op != "status":
            available, reason = self.is_available()
            if not available:
                return ActionResult.for_request(request, Status.UNAVAILABLE, reason)
        try:
            try:
                return handler(request, cancel_token)
            except ComError as exc:
                if exc.kind is not ComErrorKind.GONE:
                    raise
                log.info("Word disconnected during %s; re-attaching", request.op)
                self._drop_handles()
                return handler(request, cancel_token)
        except CancelledError as exc:
            status = Status.TIMEOUT if exc.reason == "timeout" else Status.CANCELLED
            return ActionResult.for_request(request, status, exc.reason)
        except ComError as exc:
            status = Status.UNAVAILABLE if exc.kind is ComErrorKind.NOT_INSTALLED else Status.FAILED
            return ActionResult.for_request(request, status, exc.message)
        except (OpError, OutputPathError, SnippetError) as exc:
            return ActionResult.for_request(request, Status.FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - contract: never raise
            log.exception("Word op %s failed unexpectedly", request.op)
            return ActionResult.for_request(request, Status.FAILED, f"unexpected error: {type(exc).__name__}")

    def shutdown(self) -> None:
        """Quit Word only if AGC started it and it has no unsaved documents.

        With unsaved documents AGC's instance is left running and made visible.
        """
        try:
            if self._launched and self._app is not None and self._app_alive():
                app = self._app
                count = self._call(lambda: app.Documents.Count)
                unsaved = 0
                for i in range(1, count + 1):
                    doc = self._call(lambda i=i: app.Documents.Item(i))
                    if not self._call(lambda: doc.Saved):
                        unsaved += 1
                if unsaved:
                    # Never discard work; surface our hidden instance so the user can save it.
                    log.info("leaving Word running: %d unsaved document(s)", unsaved)
                    self._call(lambda: setattr(app, "Visible", True))
                else:
                    self._call(lambda: app.Quit())
        except (ComError, CancelledError):
            log.info("Word already gone at shutdown")
        finally:
            self._drop_handles()

    # ------------------------------------------------------------------- ops

    def _op_new_document(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        app = self._ensure_app(token)
        doc = self._new_document(app, token)
        name = self._call(lambda: doc.Name, token)
        return ActionResult.for_request(request, Status.OK, f"New Word document: {name}",
                                        {"document": name, "launched": self._launched})

    def _op_insert_text(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        text = self._resolve_text(request.args)
        app, doc = self._target(token, create=True)
        self._insert(app, doc, text, request.args, token)
        return ActionResult.for_request(request, Status.OK, "Text inserted",
                                        {"document": self._call(lambda: doc.Name, token), "chars": len(text)})

    def _op_insert_paragraph(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        text = request.args.get("text", "")
        if not isinstance(text, str) or len(text) > self.config.max_text_chars:
            raise OpError("insert_paragraph text must be a string within the size limit")
        app, doc = self._target(token, create=True)
        rng, is_selection = self._insertion_range(app, doc, request.args, token)
        self._call(lambda: rng.InsertParagraphAfter(), token)
        if text:
            self._call(lambda: rng.InsertAfter(text), token)
        if is_selection:
            self._call(lambda: rng.Collapse(WD_COLLAPSE_END), token)
        return ActionResult.for_request(request, Status.OK, "Paragraph inserted")

    def _op_set_style(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        style = str(request.args.get("style", "")).strip()
        style_id = WD_BUILTIN_STYLES.get(style.lower())
        if style_id is None:
            raise OpError("style must be one of: Normal, Heading 1, Heading 2, Heading 3")
        app, doc = self._target(token, create=False)
        if self._use_selection(app, doc, request.args, token):
            rng = self._call(lambda: app.Selection.Range, token)
        else:
            paragraphs = self._call(lambda: doc.Paragraphs, token)
            last = self._call(lambda: paragraphs.Last, token)
            rng = self._call(lambda: last.Range, token)
        self._call(lambda: setattr(rng, "Style", style_id), token)
        return ActionResult.for_request(request, Status.OK, f"Style: {style}")

    def _op_export_pdf(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        _, doc = self._target(token, create=False)
        path = self._reserve_output(request, doc, ".pdf", token)
        try:
            self._call(lambda: doc.ExportAsFixedFormat(str(path), WD_EXPORT_FORMAT_PDF), token)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return ActionResult.for_request(request, Status.OK, f"Exported PDF: {path.name}", {"path": str(path)})

    def _op_save_as_docx(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        _, doc = self._target(token, create=False)
        path = self._save_docx(request, doc, token)
        return ActionResult.for_request(request, Status.OK, f"Saved: {path.name}", {"path": str(path)})

    def _op_status(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        installed, reason = self.is_available()
        data: dict[str, Any] = {"installed": installed, "running": False, "launched_by_agc": False,
                                "visible": None, "document_count": 0, "active_document": None,
                                "agc_document": None}
        if not installed:
            data["reason"] = reason
            return ActionResult.for_request(request, Status.OK, reason, data)
        app = self._app if self._app is not None and self._app_alive() else None
        if app is None:
            app = self._call(lambda: self.backend.get_active(WORD_PROGID), token)  # never launches Word
        if app is not None:
            data["running"] = True
            data["launched_by_agc"] = app is self._app and self._launched
            data["visible"] = bool(self._call(lambda: app.Visible, token))
            count = self._call(lambda: app.Documents.Count, token)
            data["document_count"] = count
            if count:
                active = self._call(lambda: app.ActiveDocument, token)
                data["active_document"] = self._call(lambda: active.Name, token)
            if app is self._app and self._doc_alive(self._doc, token):
                data["agc_document"] = self._call(lambda: self._doc.Name, token)
        if not data["running"]:
            message = "Word is not running"
        elif data["active_document"]:
            message = f"Word: {data['active_document']}"
        else:
            message = "Word is running, no document open"
        return ActionResult.for_request(request, Status.OK, message, data)

    def _op_close_document(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        if not self.config.allow_close:
            raise OpError("close_document is disabled (set allow_close: true to enable it)")
        doc = self._doc
        if doc is None or self._app is None or not self._app_alive() or not self._doc_alive(doc, token):
            raise OpError("no AGC-created document is open (AGC only closes documents it created)")
        data: dict[str, Any] = {"document": self._call(lambda: doc.Name, token)}
        if not self._call(lambda: doc.Saved, token):
            if self._call(lambda: doc.Path, token):
                self._call(lambda: doc.Save(), token)
            else:
                data["saved_to"] = str(self._save_docx(request, doc, token))
        # Everything is saved now; wdSaveChanges can never discard anything.
        self._call(lambda: doc.Close(WD_SAVE_CHANGES), token)
        self._doc = None
        return ActionResult.for_request(request, Status.OK, f"Closed {data['document']}", data)

    # -------------------------------------------------------------- helpers

    def _call(self, fn: Callable[[], T], token: CancelToken | None = None) -> T:
        """One COM call: cancel check, retry while Word is busy, errors translated to ComError."""
        delay = self.config.retry_initial_s
        attempt = 0
        while True:
            if token is not None:
                token.raise_if_cancelled()
            try:
                return fn()
            except ComError:
                raise
            except self.backend.error_types as exc:
                err = translate_com_error(exc)
            if err.kind is not ComErrorKind.BUSY or attempt >= self.config.max_retries:
                raise err
            attempt += 1
            log.debug("Word busy, retry %d/%d in %.2fs", attempt, self.config.max_retries, delay)
            if token is not None:
                token.wait(delay)
            else:
                time.sleep(delay)
            delay = min(delay * 2, self.config.retry_max_s)

    def _ensure_app(self, token: CancelToken) -> Any:
        if self._app is not None and self._app_alive(token):
            return self._app
        self._drop_handles()
        app = None
        if self.config.attach_to_running:
            app = self._call(lambda: self.backend.get_active(WORD_PROGID), token)
        if app is not None:
            self._launched = False
        else:
            app = self._call(lambda: self.backend.dispatch_ex(WORD_PROGID), token)
            self._launched = True
            # Only for our own instance: the user's Word keeps its visibility and dialogs.
            self._call(lambda: setattr(app, "Visible", self.config.visible), token)
            self._call(lambda: setattr(app, "DisplayAlerts", WD_ALERTS_NONE), token)
        self._app = app
        return app

    def _app_alive(self, token: CancelToken | None = None) -> bool:
        try:
            self._call(lambda: self._app.Documents.Count, token)
            return True
        except ComError as exc:
            if exc.kind is ComErrorKind.GONE:
                return False
            raise

    def _doc_alive(self, doc: Any, token: CancelToken | None = None) -> bool:
        if doc is None:
            return False
        try:
            self._call(lambda: doc.Name, token)
            return True
        except ComError as exc:
            if exc.kind in (ComErrorKind.BUSY, ComErrorKind.GONE):
                raise
            return False  # document was closed by the user

    def _new_document(self, app: Any, token: CancelToken) -> Any:
        doc = self._call(lambda: app.Documents.Add(), token)
        self._doc = doc
        return doc

    def _target(self, token: CancelToken, *, create: bool) -> tuple[Any, Any]:
        app = self._ensure_app(token)
        if self._doc is not None and not self._doc_alive(self._doc, token):
            self._doc = None
        if self._doc is not None:
            return app, self._doc
        if self._call(lambda: app.Documents.Count, token) > 0:
            return app, self._call(lambda: app.ActiveDocument, token)
        if create:
            return app, self._new_document(app, token)
        raise OpError("no document is open in Word")

    def _resolve_text(self, args: dict[str, Any]) -> str:
        sources = [key for key in ("text", "snippet_id", "source") if args.get(key) is not None]
        if len(sources) != 1:
            raise OpError("insert_text needs exactly one of: text, snippet_id, source='clipboard'")
        if sources[0] == "text":
            text = args["text"]
        elif sources[0] == "snippet_id":
            text = self.snippets.get(args["snippet_id"])
        else:
            if args["source"] != "clipboard":
                raise OpError("source must be 'clipboard'")
            text = self.backend.read_clipboard_text()
            if not text:
                raise OpError("the clipboard has no text")
        if not isinstance(text, str) or not text:
            raise OpError("text must be a non-empty string")
        if len(text) > self.config.max_text_chars:
            raise OpError(f"text is longer than {self.config.max_text_chars} characters")
        return text

    def _use_selection(self, app: Any, doc: Any, args: dict[str, Any], token: CancelToken) -> bool:
        position = args.get("position", "end")
        if position not in ("end", "selection"):
            raise OpError("position must be 'end' or 'selection'")
        if position == "end" or not self._call(lambda: app.Visible, token):
            return False
        active = self._call(lambda: app.ActiveDocument, token)
        return self._call(lambda: active.FullName, token) == self._call(lambda: doc.FullName, token)

    def _insertion_range(self, app: Any, doc: Any, args: dict[str, Any], token: CancelToken) -> tuple[Any, bool]:
        """``(range, is_selection)``: the user's Selection, or the whole document Content (append at end)."""
        if self._use_selection(app, doc, args, token):
            return self._call(lambda: app.Selection, token), True
        return self._call(lambda: doc.Content, token), False

    def _insert(self, app: Any, doc: Any, text: str, args: dict[str, Any], token: CancelToken) -> None:
        rng, is_selection = self._insertion_range(app, doc, args, token)
        self._call(lambda: rng.InsertAfter(text), token)
        if is_selection:  # leave the caret after the inserted text
            self._call(lambda: rng.Collapse(WD_COLLAPSE_END), token)

    def _reserve_output(self, request: ActionRequest, doc: Any, suffix: str, token: CancelToken) -> Path:
        directory = resolve_output_dir(self.config.allowed_output_dir, request.args.get("out_dir"))
        template = request.args.get("name_template", self.config.default_name_template)
        if not isinstance(template, str):
            raise OpError("name_template must be a string")
        stem = render_name(template, doc=self._call(lambda: doc.Name, token))
        return reserve_unique(directory, stem, suffix)

    def _save_docx(self, request: ActionRequest, doc: Any, token: CancelToken) -> Path:
        path = self._reserve_output(request, doc, ".docx", token)
        try:
            self._call(lambda: doc.SaveAs2(str(path), WD_FORMAT_XML_DOCUMENT), token)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _drop_handles(self) -> None:
        self._doc = None
        self._app = None
        self._launched = False
