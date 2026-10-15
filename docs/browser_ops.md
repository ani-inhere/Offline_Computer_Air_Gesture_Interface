# Browser ops: open a URL with a gesture

Make a **peace sign** (index and middle finger up in a V, ring and pinky folded) and hold it for half a second. AGC opens the project's GitHub page in a new Microsoft Edge tab.

```
Vision_test.py ──PEACE_HOLD──> gesture_bridge ──> profile: app browser, op open_url ──> BrowserAdapter ──> msedge.exe <url>
```

| Piece | File |
|---|---|
| Pose detection (`is_peace_pose`, `PoseHoldDetector`) | `agc/engine/gestures/static.py` |
| Gesture fed from the camera loop | `Vision_test.py` |
| Binding (`PEACE_HOLD` → `open_url`) and `browser:` settings | `agc/profiles/word_workflow.yaml` |
| Adapter | `agc/engine/automation/adapters/browser.py` |

## Change the link or add more

Edit the binding in `agc/profiles/word_workflow.yaml`:

```yaml
  - id: browser_shortcuts
    bindings:
      - gesture: PEACE_HOLD
        cooldown_s: 3.0
        action:
          type: app_api
          app: browser
          op: open_url
          args: { url: "https://github.com/ani-inhere/Offline_Computer_Air_Gesture_Interface", new_window: false }
          timeout_s: 30
```

| `args` | Meaning |
|---|---|
| `url` | Required. Only `http://` and `https://`; checked when the profile loads, so a bad URL stops startup with an error naming the gesture |
| `new_window` | `true` opens a new window instead of a tab |
| `backend` | `direct` or `selenium`, overriding the `browser:` setting for this one binding |

## `browser:` settings

| Setting | Default | Meaning |
|---|---|---|
| `browser` | `edge` | `edge`, `chrome`, `firefox`, or `default` (whatever Windows opens links with) |
| `browser_path` | found automatically | Path to the browser executable, if it's somewhere unusual |
| `backend` | `direct` | `direct` or `selenium` (see below) |
| `new_window` | `false` | Default for `new_window` |
| `allowed_hosts` | any | e.g. `[github.com]` to allow only those hosts and their subdomains |
| `chromedriver_path` | — | Local chromedriver; required for `selenium` |
| `selenium_keep_open` | `true` | Leave the Selenium window open when AGC exits |

The browser is found through the Windows "App Paths" registry entry, then the standard install folders, then `PATH`. Edge ships with Windows, so it is the default. If the chosen browser isn't installed, the gesture reports, for example, `Google Chrome is not installed …` (UNAVAILABLE) instead of failing silently.

## Backends

### `direct` (default, recommended)

AGC starts `msedge.exe <url>` (or `chrome.exe` / `firefox.exe`) itself, with no shell, and returns immediately. The browser opens the URL as a new tab in your normal window, with your logins and extensions. This needs no extra packages and AGC opens no network connection of its own, so it stays within the offline rules (PRD SYS-1, PRV-1).

### `selenium` (opt-in)

This drives Chrome through Selenium WebDriver. It's **Chrome only**, so Chrome must be installed. Setup:

1. `pip install selenium` (or `pip install -e .[selenium]`).
2. Download the chromedriver that matches your Chrome version from <https://googlechromelabs.github.io/chrome-for-testing/>, and unzip it, for example to `C:/tools/chromedriver.exe`.
3. In the profile:
   ```yaml
   browser:
     browser: chrome
     backend: selenium
     chromedriver_path: C:/tools/chromedriver.exe
   ```

Behavior:
- The first gesture starts a Chrome window controlled by Selenium; later gestures open new tabs in that same window.
- If you close that window, the next gesture starts a fresh one.
- On exit, the window stays open (`selenium_keep_open: true`) or closes (`false`).

**Trade-offs vs `direct`, and why it is opt-in:**
- **Separate profile.** It's a separate "Chrome is being controlled by automated test software" window with a fresh profile, so you aren't signed in to GitHub there.
- **Offline rules.** AGC always passes an explicit `chromedriver_path` and sets `SE_OFFLINE=true` and `SE_AVOID_STATS=true`, so Selenium Manager never downloads drivers or sends usage statistics (SYS-6, PRV-5).
- **Local socket.** Selenium still talks to chromedriver over a localhost HTTP socket, and pulls in `urllib3`. That's an exception to PRV-1 that you accept when you enable it, and the Phase 6 no-network import check will need to allow it.
- **Speed and ordering.** Starting Selenium takes a few seconds. Browser ops share the app-API worker with Word, so a Word job queued behind it waits.
- **Chrome only.**

## Safety

- **Allowed schemes:** only `http` and `https`; `file:`, `javascript:` and `chrome://` are rejected. This also stops a "URL" like `--remote-debugging-port=9222` from being read as a browser command-line flag.
- **Size and characters:** URLs are limited to 2048 characters, with no spaces or control characters.
- **Hosts:** set `allowed_hosts` to restrict which sites gestures may open.
- **Rate:** `cooldown_s: 3.0` plus a one-shot hold detector mean holding the pose opens one tab, not a stream of them.

## Tuning the pose

`agc/engine/gestures/static.py`:

| Constant | Default | Meaning |
|---|---|---|
| `EXTENDED_RATIO` | 1.15 | A finger counts as straight when its tip is this much farther from the wrist than its middle joint |
| `FOLDED_RATIO` | 0.95 | A finger counts as folded below this; in between counts as neither, so half-bent fingers don't trigger |
| `PEACE_MIN_SPREAD` | 0.30 | Minimum gap between index and middle tips, in hand lengths. Fingers held together are the PRD's two-finger-scroll pose, not a peace sign |
| `PEACE_HOLD_MS` (in `Vision_test.py`) | 500 | How long to hold |

The checks compare distances only, so the pose is recognized at any hand rotation or distance from the camera. The thumb is ignored.
