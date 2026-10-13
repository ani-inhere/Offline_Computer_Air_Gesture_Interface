"""Windows helpers over ctypes: foreground window, elevation, DPI awareness, virtual screen.

Every function is safe to import anywhere and returns a neutral value
(``None`` / ``False``) off Windows. Foreground detection reads only process
identity, title and window rectangle, never window contents (PRD CTX-4).
"""

from __future__ import annotations

import ctypes
import os
import sys

from .interfaces import ForegroundInfo

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:  # ctypes.wintypes is unreliable to import elsewhere
    from ctypes import wintypes

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79

_dpi_done = False


def ensure_dpi_aware() -> None:
    """Make this process per-monitor DPI aware so screenshots and cursor use physical pixels.

    Must run before the first screenshot or cursor call. Idempotent.
    """
    global _dpi_done
    if _dpi_done or not IS_WINDOWS:
        return
    _dpi_done = True
    user32 = ctypes.windll.user32
    try:  # Windows 10 1703+: per-monitor v2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:  # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` of the whole multi-monitor desktop. Left/top can be negative."""
    if not IS_WINDOWS:
        return 0, 0, 0, 0
    ensure_dpi_aware()
    metric = ctypes.windll.user32.GetSystemMetrics
    return (metric(_SM_XVIRTUALSCREEN), metric(_SM_YVIRTUALSCREEN),
            metric(_SM_CXVIRTUALSCREEN), metric(_SM_CYVIRTUALSCREEN))


def _token_elevated(process_handle: int) -> bool | None:
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(wintypes.HANDLE(process_handle), _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevation = wintypes.DWORD()
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(elevation),
                                            ctypes.sizeof(elevation), ctypes.byref(size)):
            return None
        return bool(elevation.value)
    finally:
        kernel32.CloseHandle(token)


def current_process_elevated() -> bool:
    """True if AGC itself runs elevated (as administrator)."""
    if not IS_WINDOWS:
        return False
    result = _token_elevated(ctypes.windll.kernel32.GetCurrentProcess())
    return bool(result)


def _process_info(pid: int) -> tuple[str, bool | None]:
    """``(exe basename, elevated)``. A process we cannot open at all is treated as elevated."""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", True
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        name = ""
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value)
        return name, _token_elevated(handle)
    finally:
        kernel32.CloseHandle(handle)


class WindowsForegroundProbe:
    """:class:`~agc.engine.automation.interfaces.ForegroundProbe` for Windows."""

    def __init__(self) -> None:
        ensure_dpi_aware()

    def current(self) -> ForegroundInfo | None:
        """Foreground window identity, or None (no window, e.g. the secure desktop/UAC prompt)."""
        if not IS_WINDOWS:
            return None
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = wintypes.RECT()
        has_rect = user32.GetWindowRect(hwnd, ctypes.byref(rect))
        process, elevated = _process_info(pid.value)
        return ForegroundInfo(
            process=process,
            title=title.value,
            pid=pid.value,
            elevated=elevated,
            rect=(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) if has_rect else None,
        )


class NullForegroundProbe:
    """Probe for platforms without foreground detection: always unknown."""

    def current(self) -> ForegroundInfo | None:
        return None


def default_foreground_probe() -> WindowsForegroundProbe | NullForegroundProbe:
    """The best probe for this platform."""
    return WindowsForegroundProbe() if IS_WINDOWS else NullForegroundProbe()
