"""Tier 2 adapter: open a URL in Chrome (or another browser).

Ops (``request.args`` in brackets):

* ``open_url`` [url, new_window, backend]  - open ``url`` in a new tab (or window)
* ``status``                               - which browser/backend would be used

Two backends:

* ``direct`` (default): start the browser executable with the URL as an
  argument. No shell, no new dependency, no socket opened by AGC; the tab
  opens in the user's normal browser (their logins, their profile).
* ``selenium`` (opt-in): drive Chrome through Selenium + chromedriver. This
  needs ``pip install selenium`` and a chromedriver you downloaded yourself
  (``chromedriver_path``). AGC never lets Selenium Manager download drivers
  or send usage stats (``SE_OFFLINE``/``SE_AVOID_STATS``). Note that Selenium
  talks to chromedriver over a localhost HTTP socket and opens a separate,
  automation-controlled Chrome profile.

Only ``http``/``https`` URLs are accepted, which also keeps a URL from being
read as a browser command-line flag.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Literal, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..cancel import CancelledError, CancelToken
from ..models import ActionRequest, ActionResult, Status
from .base import AppAdapter

log = logging.getLogger("agc.automation.browser")

BrowserName = Literal["chrome", "edge", "firefox", "default"]
BackendName = Literal["direct", "selenium"]

_EXE = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe"}
_WINDOWS_DIRS = {
    "chrome": [("ProgramFiles", r"Google\Chrome\Application"), ("ProgramFiles(x86)", r"Google\Chrome\Application"),
               ("LOCALAPPDATA", r"Google\Chrome\Application")],
    "edge": [("ProgramFiles(x86)", r"Microsoft\Edge\Application"), ("ProgramFiles", r"Microsoft\Edge\Application")],
    "firefox": [("ProgramFiles", "Mozilla Firefox"), ("ProgramFiles(x86)", "Mozilla Firefox")],
}
_POSIX_NAMES = {
    "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
    "firefox": ["firefox"],
}
_NEW_WINDOW_FLAG = {"chrome": "--new-window", "edge": "--new-window", "firefox": "-new-window"}
_DISPLAY = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "firefox": "Mozilla Firefox", "default": "the default browser"}
MAX_URL_LENGTH = 2048


class BrowserConfig(BaseModel):
    """Settings for :class:`BrowserAdapter` (``browser:`` section of a profile file)."""

    model_config = ConfigDict(extra="forbid")

    browser: BrowserName = "chrome"
    browser_path: Path | None = Field(None, description="Browser executable; found automatically if unset")
    backend: BackendName = "direct"
    new_window: bool = False
    allowed_hosts: list[str] = Field(default_factory=list,
                                     description="If set, only these hosts (and their subdomains) may be opened")
    chromedriver_path: Path | None = Field(None, description="Local chromedriver for the selenium backend")
    selenium_keep_open: bool = Field(True, description="Leave the Selenium Chrome window open when AGC exits")


class BrowserUnavailable(Exception):
    """The browser or backend is missing; becomes an UNAVAILABLE result."""


def validate_url(url: Any, allowed_hosts: Sequence[str] = ()) -> str:
    """Return ``url`` if it is a safe ``http(s)`` URL, else raise ``ValueError`` saying why."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("open_url needs args.url, e.g. https://github.com")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"url is longer than {MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError("url must not contain spaces or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("only http:// and https:// URLs can be opened")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("url has no host name")
    if allowed_hosts and not any(host == h.lower() or host.endswith("." + h.lower()) for h in allowed_hosts):
        raise ValueError(f"{host} is not in browser.allowed_hosts")
    return url


def find_browser(browser: str) -> Path | None:
    """Locate a browser executable: Windows App Paths registry, install folders, then PATH."""
    if browser == "default":
        return None
    if sys.platform == "win32":
        exe = _EXE[browser]
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                    value, _ = winreg.QueryValueEx(key, "")
                path = Path(str(value).strip('"'))
                if path.is_file():
                    return path
            except OSError:
                pass
        for env, sub in _WINDOWS_DIRS[browser]:
            base = os.environ.get(env)
            if base and (Path(base) / sub / exe).is_file():
                return Path(base) / sub / exe
        found = shutil.which(exe)
        return Path(found) if found else None
    for name in _POSIX_NAMES[browser]:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _launch(argv: list[str]) -> None:
    """Start the browser and return immediately (no shell, no waiting, no inherited console)."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)


def _open_with_default_browser(url: str) -> None:
    if sys.platform == "win32":
        os.startfile(url)  # ShellExecute: the user's default http handler, no command shell
    else:
        opener = shutil.which("xdg-open") or shutil.which("open")
        if opener is None:
            raise BrowserUnavailable("no default-browser opener (xdg-open/open) found")
        _launch([opener, url])


def _selenium_chrome(driver_path: str, binary_location: str | None, keep_open: bool) -> Any:
    """Start Chrome under Selenium with an explicit local chromedriver (Selenium Manager never runs)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    if binary_location:
        options.binary_location = binary_location
    if keep_open:
        options.add_experimental_option("detach", True)  # window survives driver/service shutdown
    return webdriver.Chrome(service=Service(executable_path=driver_path), options=options)


DriverFactory = Callable[[str, "str | None", bool], Any]


class BrowserAdapter(AppAdapter):
    """Opens URLs in a browser. Runs on the app-API worker thread like the Word adapter."""

    app = "chrome"
    name = "browser"
    ops = frozenset({"open_url", "status"})

    def __init__(self, config: BrowserConfig | None = None, *,
                 launcher: Callable[[list[str]], None] | None = None,
                 default_opener: Callable[[str], None] | None = None,
                 finder: Callable[[str], Path | None] | None = None,
                 driver_factory: DriverFactory | None = None) -> None:
        self.config = config or BrowserConfig()
        self._launch = launcher or _launch
        self._open_default = default_opener or _open_with_default_browser
        self._find = finder or find_browser
        self._driver_factory = driver_factory
        self._driver: Any = None

    # ------------------------------------------------------------ adapter API

    def validate_args(self, op: str, args: dict[str, Any]) -> None:
        """Check a binding's args when a profile loads. Raises ``ValueError``."""
        if op != "open_url":
            return
        validate_url(args.get("url"), self.config.allowed_hosts)
        if args.get("backend", self.config.backend) not in ("direct", "selenium"):
            raise ValueError("backend must be 'direct' or 'selenium'")
        if not isinstance(args.get("new_window", False), bool):
            raise ValueError("new_window must be true or false")

    def is_available(self) -> tuple[bool, str]:
        try:
            if self.config.backend == "selenium":
                self._selenium_ready()
            else:
                self._browser_path()
        except BrowserUnavailable as exc:
            return False, str(exc)
        return True, ""

    def execute(self, request: ActionRequest, cancel_token: CancelToken) -> ActionResult:
        """Run one op. Never raises."""
        try:
            cancel_token.raise_if_cancelled()
            if request.op == "status":
                return self._op_status(request)
            if request.op != "open_url":
                return ActionResult.for_request(request, Status.FAILED, f"unknown browser op {request.op!r}")
            self.validate_args(request.op, request.args)
            url = validate_url(request.args["url"], self.config.allowed_hosts)
            backend = request.args.get("backend", self.config.backend)
            new_window = request.args.get("new_window", self.config.new_window)
            if backend == "selenium":
                self._open_selenium(url, new_window)
            else:
                self._open_direct(url, new_window)
            host = urlsplit(url).hostname
            return ActionResult.for_request(request, Status.OK, f"Opened {host} in {self._label(backend)}",
                                            {"url": url, "browser": self.config.browser, "backend": backend})
        except CancelledError as exc:
            status = Status.TIMEOUT if exc.reason == "timeout" else Status.CANCELLED
            return ActionResult.for_request(request, status, exc.reason)
        except BrowserUnavailable as exc:
            return ActionResult.for_request(request, Status.UNAVAILABLE, str(exc))
        except ValueError as exc:
            return ActionResult.for_request(request, Status.FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - contract: never raise
            log.warning("open_url failed: %s", type(exc).__name__)
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            return ActionResult.for_request(request, Status.FAILED,
                                            f"could not open the URL: {type(exc).__name__}: {first_line}")

    def shutdown(self) -> None:
        """Stop the Selenium session, if any. With ``selenium_keep_open`` the window stays open."""
        driver, self._driver = self._driver, None
        if driver is None:
            return
        try:
            if self.config.selenium_keep_open:
                driver.service.stop()  # end chromedriver; the detached Chrome window stays
            else:
                driver.quit()
        except Exception:  # noqa: BLE001
            log.debug("selenium shutdown failed", exc_info=True)

    # ------------------------------------------------------------- backends

    def _browser_path(self) -> Path | None:
        """Executable for the configured browser (None means "use the OS default browser")."""
        if self.config.browser == "default":
            return None
        if self.config.browser_path is not None:
            if not self.config.browser_path.is_file():
                raise BrowserUnavailable(f"browser_path {self.config.browser_path} does not exist")
            return self.config.browser_path
        path = self._find(self.config.browser)
        if path is None:
            name = _DISPLAY[self.config.browser]
            raise BrowserUnavailable(f"{name} is not installed (set browser.browser_path, "
                                     "or browser: edge / firefox / default in the profile)")
        return path

    def _open_direct(self, url: str, new_window: bool) -> None:
        path = self._browser_path()
        if path is None:
            self._open_default(url)
            return
        argv = [str(path)]
        if new_window:
            argv.append(_NEW_WINDOW_FLAG[self.config.browser])
        argv.append(url)
        self._launch(argv)

    def _selenium_ready(self) -> str:
        """Check the opt-in Selenium setup; returns the chromedriver path."""
        if self.config.browser != "chrome":
            raise BrowserUnavailable("the selenium backend supports browser: chrome only")
        if self._driver_factory is None and importlib.util.find_spec("selenium") is None:
            raise BrowserUnavailable("Selenium is not installed (pip install selenium)")
        driver_path = self.config.chromedriver_path
        if driver_path is None:
            raise BrowserUnavailable("set browser.chromedriver_path to a chromedriver you downloaded; "
                                     "AGC never lets Selenium download one")
        if not driver_path.is_file():
            raise BrowserUnavailable(f"chromedriver_path {driver_path} does not exist")
        return str(driver_path)

    def _open_selenium(self, url: str, new_window: bool) -> None:
        driver, fresh = self._selenium_driver()
        if not fresh:
            driver.switch_to.new_window("window" if new_window else "tab")
        driver.get(url)

    def _selenium_driver(self) -> tuple[Any, bool]:
        """The live Selenium session, or a new one. Returns ``(driver, newly_created)``."""
        if self._driver is not None:
            try:
                self._driver.window_handles  # raises if the user closed the window / chromedriver died
                return self._driver, False
            except Exception:  # noqa: BLE001
                log.info("selenium session gone; starting a new one")
                self._driver = None
        driver_path = self._selenium_ready()
        # Keep Selenium Manager (if anything calls it) offline and silent.
        os.environ["SE_OFFLINE"] = "true"
        os.environ["SE_AVOID_STATS"] = "true"
        binary = self._find("chrome") if self.config.browser_path is None else self.config.browser_path
        factory = self._driver_factory or _selenium_chrome
        self._driver = factory(driver_path, str(binary) if binary else None, self.config.selenium_keep_open)
        return self._driver, True

    # --------------------------------------------------------------- status

    def _op_status(self, request: ActionRequest) -> ActionResult:
        available, reason = self.is_available()
        data: dict[str, Any] = {"browser": self.config.browser, "backend": self.config.backend,
                                "available": available, "selenium_session": self._driver is not None}
        if available and self.config.backend == "direct":
            path = self._browser_path()
            data["path"] = str(path) if path else "(default browser)"
        message = f"{self._label(self.config.backend)} ready" if available else reason
        if not available:
            data["reason"] = reason
        return ActionResult.for_request(request, Status.OK, message, data)

    def _label(self, backend: str) -> str:
        name = _DISPLAY[self.config.browser]
        return f"{name} (Selenium)" if backend == "selenium" else name
