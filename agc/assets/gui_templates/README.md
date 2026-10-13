# GUI templates

Small PNG crops that GUI macros look for on screen (`wait_for_image`, `click_image`).

Capture them on the machine that will run the macros, since theme, language and display scaling change how things look:

```
python -m agc.tools.capture_template save_dialog.png --delay 3        # drag a box around the target
python -m agc.tools.capture_template ok_button.png --region 800 600 90 30
python -m agc.tools.capture_template tab.png --from-image shot.png --region 10 10 40 20
```

Keep crops tight and distinctive: a button label rather than a plain background. The locator matches in grayscale at 90%, 100% and 110% scale by default.
