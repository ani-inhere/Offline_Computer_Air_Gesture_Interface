"""In-memory stand-in for Word's COM object model plus a fake ComBackend.

Covers only what WordAdapter touches. Faults are injected per member, e.g.
``world.fail("Documents.Add", RPC_E_CALL_REJECTED, times=2)``; ``app.kill()``
simulates the user closing Word (every later call raises RPC_E_DISCONNECTED).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from agc.engine.automation.adapters.com_wrapper import DISP_E_EXCEPTION, RPC_E_DISCONNECTED


def _signed(hresult: int) -> int:
    return hresult - (1 << 32) if hresult >= (1 << 31) else hresult


class FakeComError(Exception):
    """Same ``args`` shape as ``pywintypes.com_error``: (hresult, text, excepinfo, argerror)."""

    def __init__(self, hresult: int, text: str = "", description: str | None = None, scode: int = 0) -> None:
        excepinfo = (0, "Microsoft Word", description, None, 0, _signed(scode)) if description is not None else None
        super().__init__(_signed(hresult), text, excepinfo, None)


class FakeWorld:
    """Shared fault table, call log and COM-thread record."""

    def __init__(self) -> None:
        self.faults: dict[str, list[FakeComError]] = {}
        self.log: list[str] = []
        self.threads: set[int] = set()

    def fail(self, member: str, hresult: int, times: int = 1, description: str | None = None,
             scode: int = 0) -> None:
        self.faults.setdefault(member, []).extend(
            FakeComError(hresult, description=description, scode=scode) for _ in range(times))

    def hit(self, member: str) -> None:
        self.log.append(member)
        self.threads.add(threading.get_ident())
        queue = self.faults.get(member)
        if queue:
            raise queue.pop(0)


class _ComObject:
    """Routes every public attribute get/set through ``_guard`` like a COM proxy would."""

    _kind = "Object"

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            object.__getattribute__(self, "_guard")(name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_"):
            self._guard(name)
        object.__setattr__(self, name, value)

    def _guard(self, name: str) -> None:
        app = self._app_ref()
        app._world.hit(f"{self._kind}.{name}")
        if app._dead:
            raise FakeComError(RPC_E_DISCONNECTED, "The object invoked has disconnected from its clients.")
        self._check_alive()

    def _app_ref(self) -> FakeWordApp:
        raise NotImplementedError

    def _check_alive(self) -> None:
        pass


class FakeRange(_ComObject):
    _kind = "Range"

    def __init__(self, doc: FakeDocument, is_selection: bool = False) -> None:
        self._doc = doc
        self._is_selection = is_selection
        self._kind = "Selection" if is_selection else "Range"

    def _app_ref(self) -> FakeWordApp:
        return self._doc._app

    def InsertAfter(self, text: str) -> None:  # noqa: N802 - COM member names
        self._doc._text += text
        self._doc._saved = False

    def InsertParagraphAfter(self) -> None:  # noqa: N802
        self._doc._text += "\n"
        self._doc._saved = False

    def Collapse(self, direction: int) -> None:  # noqa: N802
        self._doc._collapses.append(direction)

    @property
    def Range(self) -> FakeRange:  # noqa: N802 - Selection.Range
        return FakeRange(self._doc)

    @property
    def Style(self) -> Any:  # noqa: N802
        return self._doc._styles[-1] if self._doc._styles else -1

    @Style.setter
    def Style(self, value: Any) -> None:  # noqa: N802
        self._doc._styles.append(value)
        self._doc._saved = False


class FakeParagraph(_ComObject):
    _kind = "Paragraph"

    def __init__(self, doc: FakeDocument) -> None:
        self._doc = doc

    def _app_ref(self) -> FakeWordApp:
        return self._doc._app

    @property
    def Range(self) -> FakeRange:  # noqa: N802
        return FakeRange(self._doc)


class FakeParagraphs(FakeParagraph):
    _kind = "Paragraphs"

    @property
    def Last(self) -> FakeParagraph:  # noqa: N802
        return FakeParagraph(self._doc)


class FakeDocument(_ComObject):
    _kind = "Document"

    def __init__(self, app: FakeWordApp, name: str) -> None:
        self._app = app
        self._name = name
        self._path = ""
        self._text = ""
        self._saved = True
        self._closed = False
        self._styles: list[Any] = []
        self._collapses: list[int] = []
        self._close_args: list[Any] = []
        self._exports: list[tuple[str, int]] = []

    def _app_ref(self) -> FakeWordApp:
        return self._app

    def _check_alive(self) -> None:
        if self._closed:
            raise FakeComError(DISP_E_EXCEPTION, description="Object has been deleted.", scode=0x800A16D0)

    @property
    def Name(self) -> str:  # noqa: N802
        return self._name

    @property
    def FullName(self) -> str:  # noqa: N802
        return str(Path(self._path) / self._name) if self._path else self._name

    @property
    def Path(self) -> str:  # noqa: N802
        return self._path

    @property
    def Saved(self) -> bool:  # noqa: N802
        return self._saved

    @property
    def Content(self) -> FakeRange:  # noqa: N802
        return FakeRange(self)

    @property
    def Paragraphs(self) -> FakeParagraphs:  # noqa: N802
        return FakeParagraphs(self)

    def ExportAsFixedFormat(self, output_file_name: str, export_format: int) -> None:  # noqa: N802
        Path(output_file_name).write_bytes(b"%PDF-1.7\n% fake export\n")
        self._exports.append((output_file_name, export_format))

    def SaveAs2(self, file_name: str, file_format: int) -> None:  # noqa: N802
        path = Path(file_name)
        path.write_bytes(b"PK fake docx")
        self._path, self._name, self._saved = str(path.parent), path.name, True

    def Save(self) -> None:  # noqa: N802
        if not self._path:
            raise AssertionError("Save() on an untitled document would open a Save As dialog")
        self._saved = True

    def Close(self, save_changes: int = 0) -> None:  # noqa: N802
        self._close_args.append(save_changes)
        if save_changes == 0 and not self._saved:
            raise AssertionError("closed with wdDoNotSaveChanges while unsaved: changes discarded")
        self._closed = True
        self._app._docs.remove(self)


class FakeDocuments(_ComObject):
    _kind = "Documents"

    def __init__(self, app: FakeWordApp) -> None:
        self._app = app

    def _app_ref(self) -> FakeWordApp:
        return self._app

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self._app._docs)

    def Add(self) -> FakeDocument:  # noqa: N802
        self._app._counter += 1
        doc = FakeDocument(self._app, f"Document{self._app._counter}")
        self._app._docs.append(doc)
        return doc

    def Item(self, index: int) -> FakeDocument:  # noqa: N802
        return self._app._docs[index - 1]


class FakeWordApp(_ComObject):
    _kind = "App"

    def __init__(self, world: FakeWorld, visible: bool = False) -> None:
        self._world = world
        self._dead = False
        self._docs: list[FakeDocument] = []
        self._counter = 0
        self._quit_called = False
        self._visible = visible
        self._alerts = -1  # wdAlertsAll
        self._alert_writes: list[int] = []

    def _app_ref(self) -> FakeWordApp:
        return self

    def kill(self) -> None:
        """Simulate the user closing Word."""
        self._dead = True

    def add_user_document(self, name: str = "Thesis.docx", saved: bool = True) -> FakeDocument:
        """A document the user had open before AGC attached."""
        doc = FakeDocument(self, name)
        doc._path, doc._saved = "C:\\Users\\me\\Documents", saved
        self._docs.append(doc)
        return doc

    @property
    def Documents(self) -> FakeDocuments:  # noqa: N802
        return FakeDocuments(self)

    @property
    def ActiveDocument(self) -> FakeDocument:  # noqa: N802
        if not self._docs:
            raise FakeComError(DISP_E_EXCEPTION, description="This command is not available because no "
                                                             "document is open.", scode=0x800A1098)
        return self._docs[-1]

    @property
    def Selection(self) -> FakeRange:  # noqa: N802
        return FakeRange(self.ActiveDocument, is_selection=True)

    @property
    def Visible(self) -> bool:  # noqa: N802
        return self._visible

    @Visible.setter
    def Visible(self, value: bool) -> None:  # noqa: N802
        self._visible = bool(value)

    @property
    def DisplayAlerts(self) -> int:  # noqa: N802
        return self._alerts

    @DisplayAlerts.setter
    def DisplayAlerts(self, value: int) -> None:  # noqa: N802
        self._alerts = value
        self._alert_writes.append(value)

    def Quit(self) -> None:  # noqa: N802
        self._quit_called = True
        self._dead = True


class FakeComBackend:
    """ComBackend over the fake object model."""

    error_types: tuple[type[BaseException], ...] = (FakeComError,)

    def __init__(self, *, installed: bool = True, running: FakeWordApp | None = None,
                 world: FakeWorld | None = None) -> None:
        self.world = world or (running._world if running else FakeWorld())
        self.installed = installed
        self.running = running
        self.launched: list[FakeWordApp] = []
        self.clipboard: str | None = None
        self.get_active_calls = 0

    def check(self, progid: str) -> tuple[bool, str]:
        return (True, "") if self.installed else (False, "Microsoft Word is not installed")

    def get_active(self, progid: str) -> FakeWordApp | None:
        self.get_active_calls += 1
        self.world.threads.add(threading.get_ident())
        if self.running is not None and not self.running._dead:
            return self.running
        return None

    def dispatch_ex(self, progid: str) -> FakeWordApp:
        self.world.threads.add(threading.get_ident())
        app = FakeWordApp(self.world)
        self.launched.append(app)
        return app

    def read_clipboard_text(self) -> str | None:
        return self.clipboard
