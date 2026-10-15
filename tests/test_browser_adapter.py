"""BrowserAdapter: URL safety, direct launch, browser discovery, opt-in Selenium backend."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from helpers import make_request

from agc.engine.automation import ActionType, CancelToken, Status, Tier
from agc.engine.automation.adapters import browser as browser_mod
from agc.engine.automation.adapters.browser import BrowserAdapter, BrowserConfig, find_browser, validate_url

GITHUB = "https://github.com/ani-inhere/Offline_Computer_Air_Gesture_Interface"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
FIREFOX = Path("C:/Program Files/Mozilla Firefox/firefox.exe")
EXE = {"chrome": CHROME, "edge": EDGE, "firefox": FIREFOX}


def run(adapter: BrowserAdapter, op: str = "open_url", token: CancelToken | None = None, **args):
    request = make_request(op, type=ActionType.APP_API, app="browser", args=args)
    result = adapter.execute(request, token or CancelToken())
    assert result.id == request.id
    return result


def direct(**config):
    launched: list[list[str]] = []
    adapter = BrowserAdapter(BrowserConfig(**config), launcher=launched.append, finder=EXE.get)
    return adapter, launched


# ------------------------------------------------------------- URL safety


@pytest.mark.parametrize("url", [GITHUB, "http://localhost:8000/x?a=1#top", "HTTPS://Example.com"])
def test_valid_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url, message", [
    (None, "needs args.url"),
    ("", "needs args.url"),
    ("file:///C:/Windows/System32", "only http"),
    ("javascript:alert(1)", "only http"),
    ("chrome://settings", "only http"),
    ("--remote-debugging-port=9222", "only http"),   # would otherwise be read as a Chrome flag
    ("https://exa mple.com", "spaces"),
    ("https://example.com/\n--flag", "spaces or control"),
    ("https:///path-only", "no host"),
    ("https://" + "a" * 2050 + ".com", "longer than"),
])
def test_unsafe_urls_rejected(url, message):
    with pytest.raises(ValueError, match=message):
        validate_url(url)


def test_allowed_hosts_include_subdomains():
    assert validate_url("https://gist.github.com/x", ["github.com"])
    with pytest.raises(ValueError, match="allowed_hosts"):
        validate_url("https://evilgithub.com", ["github.com"])


# --------------------------------------------------------------- direct


def test_default_browser_is_edge():
    assert BrowserConfig().browser == "edge"
    adapter, launched = direct()
    result = run(adapter, url=GITHUB)
    assert result.ok and result.message == "Opened github.com in Microsoft Edge"
    assert result.data == {"url": GITHUB, "browser": "edge", "backend": "direct"}
    assert launched == [[str(EDGE), GITHUB]]


def test_open_url_launches_chrome_with_the_url_only():
    adapter, launched = direct(browser="chrome")
    result = run(adapter, url=GITHUB)
    assert result.ok and result.message == "Opened github.com in Google Chrome"
    assert launched == [[str(CHROME), GITHUB]]


@pytest.mark.parametrize("browser, flag", [("chrome", "--new-window"), ("edge", "--new-window"),
                                           ("firefox", "-new-window")])
def test_new_window_flag_per_browser(browser, flag):
    adapter, launched = direct(browser=browser)
    assert run(adapter, url=GITHUB, new_window=True).ok
    assert launched == [[str(EXE[browser]), flag, GITHUB]]


@pytest.mark.parametrize("browser, name", [("edge", "Microsoft Edge"), ("chrome", "Google Chrome")])
def test_missing_browser_is_unavailable_with_a_hint(browser, name):
    adapter = BrowserAdapter(BrowserConfig(browser=browser), launcher=lambda argv: pytest.fail("must not launch"),
                             finder=lambda name: None)
    result = run(adapter, url=GITHUB)
    assert result.status is Status.UNAVAILABLE
    assert result.message.startswith(f"{name} is not installed")
    assert adapter.is_available()[0] is False


def test_configured_browser_path_must_exist(tmp_path):
    adapter = BrowserAdapter(BrowserConfig(browser_path=tmp_path / "nope.exe"), launcher=lambda argv: None)
    result = run(adapter, url=GITHUB)
    assert result.status is Status.UNAVAILABLE and "does not exist" in result.message


def test_configured_browser_path_is_used(tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    launched: list[list[str]] = []
    adapter = BrowserAdapter(BrowserConfig(browser_path=exe), launcher=launched.append,
                             finder=lambda name: pytest.fail("must not search"))
    assert run(adapter, url=GITHUB).ok
    assert launched == [[str(exe), GITHUB]]


def test_default_browser_uses_the_os_handler():
    opened: list[str] = []
    adapter = BrowserAdapter(BrowserConfig(browser="default"), default_opener=opened.append,
                             launcher=lambda argv: pytest.fail("must not launch"))
    result = run(adapter, url=GITHUB)
    assert result.ok and result.message == "Opened github.com in the default browser"
    assert opened == [GITHUB]


def test_bad_request_url_fails_without_launching():
    adapter, launched = direct()
    result = run(adapter, url="file:///etc/passwd")
    assert result.status is Status.FAILED and "only http" in result.message
    assert launched == []


def test_launch_error_becomes_failed_result():
    def broken(argv):
        raise PermissionError("Access is denied")

    adapter = BrowserAdapter(launcher=broken, finder=lambda name: CHROME)
    result = run(adapter, url=GITHUB)
    assert result.status is Status.FAILED and "PermissionError: Access is denied" in result.message


def test_cancelled_before_start():
    adapter, launched = direct()
    token = CancelToken()
    token.cancel()
    assert run(adapter, token=token, url=GITHUB).status is Status.CANCELLED
    assert launched == []


def test_status_and_unknown_op():
    adapter, _ = direct()
    status = run(adapter, "status")
    assert status.ok and status.data["path"] == str(EDGE) and status.data["available"] is True
    assert run(adapter, "close_all_tabs").status is Status.FAILED


@pytest.mark.skipif(sys.platform != "win32", reason="Windows install-folder lookup")
def test_find_browser_checks_install_folders(tmp_path, monkeypatch):
    import winreg

    def no_registry(*args, **kwargs):
        raise OSError("not registered")

    monkeypatch.setattr(winreg, "OpenKey", no_registry)
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(env, str(tmp_path / "empty"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    assert find_browser("chrome") == exe
    assert find_browser("firefox") is None
    assert find_browser("default") is None


# ------------------------------------------------------------- selenium


class FakeSwitch:
    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver

    def new_window(self, kind: str) -> None:
        self.driver.calls.append(("new_window", kind))


class FakeService:
    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver

    def stop(self) -> None:
        self.driver.calls.append(("service_stop",))


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.closed = False
        self.switch_to = FakeSwitch(self)
        self.service = FakeService(self)

    @property
    def window_handles(self) -> list[str]:
        if self.closed:
            raise RuntimeError("no such window")
        return ["main"]

    def get(self, url: str) -> None:
        self.calls.append(("get", url))

    def quit(self) -> None:
        self.calls.append(("quit",))


@pytest.fixture
def chromedriver(tmp_path) -> Path:
    path = tmp_path / "chromedriver.exe"
    path.write_bytes(b"")
    return path


@pytest.fixture
def isolated_env(monkeypatch):
    env = dict(os.environ)
    monkeypatch.setattr(browser_mod.os, "environ", env)
    return env


def selenium_adapter(chromedriver: Path, **config):
    drivers: list[FakeDriver] = []
    factory_args: list[tuple] = []

    def factory(driver_path, binary, keep_open):
        factory_args.append((driver_path, binary, keep_open))
        drivers.append(FakeDriver())
        return drivers[-1]

    adapter = BrowserAdapter(BrowserConfig(browser="chrome", backend="selenium", chromedriver_path=chromedriver,
                                           **config),
                             launcher=lambda argv: pytest.fail("direct launch must not run"),
                             finder=lambda name: CHROME, driver_factory=factory)
    return adapter, drivers, factory_args


def test_selenium_reuses_one_session_and_opens_new_tabs(chromedriver, isolated_env):
    adapter, drivers, factory_args = selenium_adapter(chromedriver)
    first = run(adapter, url=GITHUB)
    assert first.ok and first.message == "Opened github.com in Google Chrome (Selenium)"
    assert run(adapter, url="https://example.com").ok
    assert run(adapter, url="https://example.org", new_window=True).ok
    assert len(drivers) == 1
    assert drivers[0].calls == [("get", GITHUB), ("new_window", "tab"), ("get", "https://example.com"),
                                ("new_window", "window"), ("get", "https://example.org")]
    assert factory_args == [(str(chromedriver), str(CHROME), True)]
    # Selenium Manager is kept offline and silent.
    assert isolated_env["SE_OFFLINE"] == "true" and isolated_env["SE_AVOID_STATS"] == "true"


def test_selenium_restarts_when_the_window_was_closed(chromedriver, isolated_env):
    adapter, drivers, _ = selenium_adapter(chromedriver)
    run(adapter, url=GITHUB)
    drivers[0].closed = True
    assert run(adapter, url=GITHUB).ok
    assert len(drivers) == 2 and drivers[1].calls == [("get", GITHUB)]


def test_selenium_per_request_backend_override(chromedriver, isolated_env):
    launched: list[list[str]] = []
    adapter = BrowserAdapter(BrowserConfig(browser="chrome", chromedriver_path=chromedriver), launcher=launched.append,
                             finder=lambda name: CHROME, driver_factory=lambda *a: FakeDriver())
    assert run(adapter, url=GITHUB, backend="selenium").data["backend"] == "selenium"
    assert launched == []
    assert run(adapter, url=GITHUB).data["backend"] == "direct"
    assert launched == [[str(CHROME), GITHUB]]


@pytest.mark.parametrize("config, message", [
    ({"chromedriver_path": None}, "set browser.chromedriver_path"),
    ({"browser": "edge"}, "supports browser: chrome only"),
])
def test_selenium_setup_errors_are_unavailable(chromedriver, isolated_env, config, message):
    settings = {"browser": "chrome", "backend": "selenium", "chromedriver_path": chromedriver, **config}
    adapter = BrowserAdapter(BrowserConfig(**settings), finder=lambda name: CHROME,
                             driver_factory=lambda *a: FakeDriver())
    result = run(adapter, url=GITHUB)
    assert result.status is Status.UNAVAILABLE and message in result.message


def test_selenium_not_installed(chromedriver, monkeypatch):
    monkeypatch.setattr(browser_mod.importlib.util, "find_spec", lambda name: None)
    adapter = BrowserAdapter(BrowserConfig(browser="chrome", backend="selenium", chromedriver_path=chromedriver),
                             finder=lambda name: CHROME)
    result = run(adapter, url=GITHUB)
    assert result.status is Status.UNAVAILABLE and "pip install selenium" in result.message


def test_missing_chromedriver_file(tmp_path, isolated_env):
    adapter = BrowserAdapter(BrowserConfig(browser="chrome", backend="selenium",
                                           chromedriver_path=tmp_path / "none.exe"),
                             driver_factory=lambda *a: FakeDriver())
    result = run(adapter, url=GITHUB)
    assert result.status is Status.UNAVAILABLE and "does not exist" in result.message


@pytest.mark.parametrize("keep_open, last_call", [(True, ("service_stop",)), (False, ("quit",))])
def test_shutdown_keeps_or_closes_the_selenium_window(chromedriver, isolated_env, keep_open, last_call):
    adapter, drivers, _ = selenium_adapter(chromedriver, selenium_keep_open=keep_open)
    run(adapter, url=GITHUB)
    adapter.shutdown()
    assert drivers[0].calls[-1] == last_call
    adapter.shutdown()  # idempotent
    assert drivers[0].calls.count(last_call) == 1


def test_runs_on_the_app_worker_through_the_dispatcher(make_dispatcher, collector):
    launched: list[list[str]] = []
    d = make_dispatcher()
    d.register_backend(Tier.APP_API, BrowserAdapter(launcher=launched.append, finder=lambda name: CHROME))
    d.start()
    job = d.submit(make_request("open_url", type=ActionType.APP_API, app="browser", args={"url": GITHUB}))
    result = collector.wait_for(job)
    assert result.ok and result.tier_used is Tier.APP_API
    assert launched == [[str(CHROME), GITHUB]]


def test_module_import_does_not_load_selenium():
    import subprocess

    code = ("import sys, agc.engine.automation.adapters.browser; "
            "sys.exit(1 if 'selenium' in sys.modules else 0)")
    repo_root = Path(__file__).resolve().parents[1]
    assert subprocess.run([sys.executable, "-c", code], cwd=repo_root).returncode == 0
