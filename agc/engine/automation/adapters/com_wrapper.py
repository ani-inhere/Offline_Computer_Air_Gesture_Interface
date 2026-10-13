"""Thin COM layer: object creation, clipboard and error translation behind one interface.

App adapters never import pywin32 directly. They talk to a :class:`ComBackend`;
the real one (:class:`PyWin32Backend`) imports pywin32 lazily, and tests swap
in a fake, so adapter logic runs on any OS.

All objects a backend returns are COM objects bound to the calling thread's
apartment: call the backend only from the ComWorker thread.
"""

from __future__ import annotations

import sys
import time
from enum import Enum
from typing import Any, Protocol

# HRESULTs, as unsigned 32-bit values.
RPC_E_CALL_REJECTED = 0x80010001
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
RPC_E_DISCONNECTED = 0x80010108
RPC_E_SERVER_DIED = 0x80010007
RPC_E_SERVER_DIED_DNE = 0x80010012
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
RPC_S_CALL_FAILED = 0x800706BE
CO_E_OBJNOTCONNECTED = 0x800401FD
MK_E_UNAVAILABLE = 0x800401E3
REGDB_E_CLASSNOTREG = 0x80040154
CO_E_CLASSSTRING = 0x800401F3
CO_E_SERVER_EXEC_FAILURE = 0x80080005
DISP_E_EXCEPTION = 0x80020009
E_ACCESSDENIED = 0x80070005

_BUSY = {RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER}
_GONE = {RPC_E_DISCONNECTED, RPC_E_SERVER_DIED, RPC_E_SERVER_DIED_DNE, RPC_S_SERVER_UNAVAILABLE,
         RPC_S_CALL_FAILED, CO_E_OBJNOTCONNECTED}
_NOT_INSTALLED = {REGDB_E_CLASSNOTREG, CO_E_CLASSSTRING, CO_E_SERVER_EXEC_FAILURE}


class ComErrorKind(str, Enum):
    """What a COM failure means for the caller."""

    BUSY = "busy"                    # app has a modal dialog or is mid-operation: retry later
    GONE = "gone"                    # app process exited or disconnected: re-attach
    NOT_INSTALLED = "not_installed"  # class not registered / server would not start
    APP_ERROR = "app_error"          # the app raised an error (DISP_E_EXCEPTION)
    OTHER = "other"


class ComError(Exception):
    """A COM failure translated into a kind plus a message fit for the HUD."""

    def __init__(self, kind: ComErrorKind, message: str, hresult: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hresult = hresult


def _u32(value: Any) -> int | None:
    return value & 0xFFFFFFFF if isinstance(value, int) else None


def translate_com_error(exc: BaseException, app_name: str = "Word") -> ComError:
    """Turn a ``pywintypes.com_error`` (or a fake with the same ``args`` shape) into a :class:`ComError`.

    ``com_error.args`` is ``(hresult, text, excepinfo, argerror)`` where
    ``excepinfo`` is ``(wcode, source, description, helpfile, helpctx, scode)``.
    """
    args = getattr(exc, "args", ())
    hresult = _u32(args[0]) if args else None
    text = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
    excepinfo = args[2] if len(args) > 2 and isinstance(args[2], tuple) else None
    description = excepinfo[2] if excepinfo and len(excepinfo) > 2 else None
    scode = _u32(excepinfo[5]) if excepinfo and len(excepinfo) > 5 else None
    codes = {hresult, scode}

    if codes & _BUSY:
        return ComError(ComErrorKind.BUSY, f"{app_name} is busy (close any open dialog in {app_name} and try again)",
                        hresult)
    if codes & _GONE:
        return ComError(ComErrorKind.GONE, f"{app_name} was closed or stopped responding", hresult)
    if codes & _NOT_INSTALLED:
        return ComError(ComErrorKind.NOT_INSTALLED, f"Microsoft {app_name} is not installed or could not be started",
                        hresult)
    if hresult == DISP_E_EXCEPTION:
        detail = description.strip() if isinstance(description, str) and description.strip() else "an error"
        return ComError(ComErrorKind.APP_ERROR, f"{app_name} reported: {detail}", hresult)
    if hresult == E_ACCESSDENIED:
        return ComError(ComErrorKind.OTHER, f"access denied while talking to {app_name}", hresult)
    code = f"0x{hresult:08X}" if hresult is not None else "unknown"
    return ComError(ComErrorKind.OTHER, f"COM error {code}" + (f": {text}" if text else ""), hresult)


class ComBackend(Protocol):
    """What app adapters need from COM. Implemented by :class:`PyWin32Backend` and test fakes."""

    #: Exception types raised by COM calls on objects this backend returns.
    error_types: tuple[type[BaseException], ...]

    def check(self, progid: str) -> tuple[bool, str]:
        """``(usable, reason)``: COM support present and ``progid`` registered. Thread-agnostic."""
        ...

    def get_active(self, progid: str) -> Any | None:
        """Attach to a running instance, or None if none is running."""
        ...

    def dispatch_ex(self, progid: str) -> Any:
        """Start a new, separate instance."""
        ...

    def read_clipboard_text(self) -> str | None:
        """Unicode text on the clipboard, or None if there is none."""
        ...


class PyWin32Backend:
    """:class:`ComBackend` over pywin32, imported lazily so this module loads anywhere."""

    def __init__(self) -> None:
        self._error_types: tuple[type[BaseException], ...] | None = None

    @property
    def error_types(self) -> tuple[type[BaseException], ...]:
        if self._error_types is None:
            try:
                import pywintypes  # type: ignore[import-not-found]
                self._error_types = (pywintypes.com_error,)
            except ImportError:
                self._error_types = ()
        return self._error_types

    def check(self, progid: str) -> tuple[bool, str]:
        if sys.platform != "win32":
            return False, "COM automation needs Windows"
        try:
            import win32com.client  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False, "pywin32 is not installed"
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID"):
                return True, ""
        except OSError:
            app = progid.split(".")[0]
            return False, f"Microsoft {app} is not installed"

    def get_active(self, progid: str) -> Any | None:
        import pywintypes  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]

        try:
            return win32com.client.GetActiveObject(progid)
        except pywintypes.com_error as exc:
            if _u32(exc.args[0]) == MK_E_UNAVAILABLE:
                return None
            raise

    def dispatch_ex(self, progid: str) -> Any:
        import win32com.client  # type: ignore[import-not-found]

        return win32com.client.DispatchEx(progid)

    def read_clipboard_text(self) -> str | None:
        import win32clipboard  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]

        for _ in range(5):  # another app may hold the clipboard briefly
            try:
                win32clipboard.OpenClipboard()
            except Exception:  # noqa: BLE001 - pywintypes.error while the clipboard is locked
                time.sleep(0.02)
                continue
            try:
                if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return None
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        raise ComError(ComErrorKind.BUSY, "the clipboard is in use by another app")
