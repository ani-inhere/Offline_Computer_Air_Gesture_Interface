"""Developer CLI: crop a template from an existing screenshot."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from fake_gui import distinctive_template, noise_screen, paste

from agc.tools.capture_template import main


@pytest.fixture
def screenshot(tmp_path):
    image = cv2.cvtColor(paste(noise_screen(), distinctive_template(), 120, 80), cv2.COLOR_GRAY2BGR)
    path = tmp_path / "shot.png"
    cv2.imencode(".png", image)[1].tofile(str(path))
    return path


def test_crops_region_and_saves_png(tmp_path, screenshot, capsys):
    out = tmp_path / "templates"
    assert main(["button", "--from-image", str(screenshot), "--region", "120", "80", "48", "30",
                 "--out-dir", str(out)]) == 0
    saved = cv2.imdecode(np.fromfile(str(out / "button.png"), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert saved.shape == (30, 48)
    assert np.array_equal(saved, distinctive_template())
    assert "saved" in capsys.readouterr().out


def test_refuses_overwrite_without_force(tmp_path, screenshot):
    args = ["b.png", "--from-image", str(screenshot), "--region", "0", "0", "10", "10", "--out-dir", str(tmp_path)]
    assert main(args) == 0
    with pytest.raises(SystemExit):
        main(args)
    assert main([*args, "--force"]) == 0


@pytest.mark.parametrize("name", ["../evil.png", "x.exe", "a/b.png"])
def test_rejects_unsafe_names(tmp_path, screenshot, name):
    with pytest.raises(SystemExit):
        main([name, "--from-image", str(screenshot), "--region", "0", "0", "5", "5", "--out-dir", str(tmp_path)])


def test_rejects_region_outside_image(tmp_path, screenshot):
    with pytest.raises(SystemExit):
        main(["b.png", "--from-image", str(screenshot), "--region", "590", "0", "50", "50",
              "--out-dir", str(tmp_path)])
