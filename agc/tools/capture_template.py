"""Developer CLI: crop a GUI template from a screenshot and save it for macros.

    python -m agc.tools.capture_template NAME.png [--delay 3]           # screenshot, drag a box
    python -m agc.tools.capture_template NAME.png --region L T W H      # screen coordinates
    python -m agc.tools.capture_template NAME.png --from-image shot.png [--region X Y W H]

This is the only place AGC writes an image to disk, and only when a developer
runs it. The full screenshot is never saved, only the cropped template.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

from agc.engine.automation.locator import DEFAULT_TEMPLATE_DIR
from agc.engine.automation.macros import _TEMPLATE
from agc.engine.automation.win32_util import ensure_dpi_aware, virtual_screen_rect


def _screenshot():
    """Colour (BGR) screenshot of the whole virtual desktop and its origin."""
    import cv2
    import numpy as np
    from PIL import ImageGrab

    ensure_dpi_aware()
    left, top, width, height = virtual_screen_rect()
    if width > 0:
        image = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    else:
        image, left, top = ImageGrab.grab(), 0, 0
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR), (left, top)


def _load_image(path: Path):
    import cv2
    import numpy as np

    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"error: cannot read image {path}")
    return image


def _select_interactively(image) -> tuple[int, int, int, int]:
    import cv2

    window = "Drag a box around the template, then press Enter (Esc cancels)"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    x, y, w, h = cv2.selectROI(window, image, showCrosshair=True)
    cv2.destroyWindow(window)
    return int(x), int(y), int(w), int(h)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agc.tools.capture_template", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="template file name, e.g. save_dialog.png")
    parser.add_argument("--region", nargs=4, type=int, metavar=("LEFT", "TOP", "WIDTH", "HEIGHT"),
                        help="crop box: screen coordinates, or pixel coordinates with --from-image")
    parser.add_argument("--from-image", type=Path, help="crop from an existing screenshot instead of the screen")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait before the screenshot")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--force", action="store_true", help="overwrite an existing template")
    args = parser.parse_args(argv)

    import cv2
    import numpy as np

    name = args.name if Path(args.name).suffix else f"{args.name}.png"
    if not _TEMPLATE.match(name) or not name.lower().endswith(".png"):
        parser.error("name must be a plain file name ending in .png (letters, digits, _ - . space)")
    out_path = args.out_dir / name
    if out_path.exists() and not args.force:
        parser.error(f"{out_path} exists (use --force to replace it)")

    if args.from_image:
        image, origin = _load_image(args.from_image), (0, 0)
    else:
        if args.delay > 0:
            print(f"Taking a screenshot in {args.delay:g}s...")
            time.sleep(args.delay)
        image, origin = _screenshot()

    if args.region:
        left, top, width, height = args.region
        x, y = left - origin[0], top - origin[1]
    else:
        x, y, width, height = _select_interactively(image)
    if width <= 0 or height <= 0:
        print("cancelled: empty selection", file=sys.stderr)
        return 1
    img_h, img_w = image.shape[:2]
    if x < 0 or y < 0 or x + width > img_w or y + height > img_h:
        parser.error(f"region is outside the image ({img_w}x{img_h})")

    crop = image[y:y + height, x:x + width]
    if float(np.std(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))) < 1.0:
        print("warning: the crop is a flat colour and will not match reliably", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        print("error: PNG encoding failed", file=sys.stderr)
        return 1
    encoded.tofile(str(out_path))
    print(f"saved {out_path} ({width}x{height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
