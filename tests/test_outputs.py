"""Output path rules: stay inside the allowed folder, safe names, never overwrite."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from agc.engine.automation.outputs import (
    OutputPathError,
    render_name,
    reserve_unique,
    resolve_output_dir,
    sanitize_stem,
)


def test_resolve_creates_allowed_dir_and_subfolders(tmp_path):
    root = tmp_path / "AGC"
    assert resolve_output_dir(root) == root.resolve()
    assert resolve_output_dir(root, "a/b") == (root / "a" / "b").resolve()
    assert resolve_output_dir(root, root / "c") == (root / "c").resolve()


@pytest.mark.parametrize("requested", ["..", "../sibling", "a/../../x"])
def test_resolve_rejects_traversal(tmp_path, requested):
    with pytest.raises(OutputPathError):
        resolve_output_dir(tmp_path / "AGC", requested)


def test_resolve_rejects_prefix_lookalike(tmp_path):
    with pytest.raises(OutputPathError):
        resolve_output_dir(tmp_path / "AGC", tmp_path / "AGC-evil")


def test_resolve_rejects_symlink_escape(tmp_path):
    root = tmp_path / "AGC"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin on Windows")
    with pytest.raises(OutputPathError):
        resolve_output_dir(root, "link")


def test_render_name_placeholders():
    now = datetime(2026, 10, 14, 9, 5, 7)
    assert render_name("agc_{timestamp}", now=now) == "agc_20261014-090507"
    assert render_name("{doc}-{date}", doc="Plan.docx", now=now) == "Plan-2026-10-14"
    with pytest.raises(OutputPathError):
        render_name("{nope}")


def test_render_name_attribute_syntax_is_inert():
    # Without a placeholder match the text stays literal; no format() attribute access happens.
    assert render_name("{0.__class__}") == "{0.__class__}"


@pytest.mark.parametrize("raw, expected", [
    ("a/b\\c:d", "a_b_c_d"),
    ("  ..hidden.  ", "hidden"),
    ("", "agc"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("x" * 300, "x" * 120),
])
def test_sanitize_stem(raw, expected):
    assert sanitize_stem(raw) == expected


def test_reserve_unique_never_overwrites(tmp_path):
    (tmp_path / "r.pdf").write_text("existing")
    first = reserve_unique(tmp_path, "r", ".pdf")
    second = reserve_unique(tmp_path, "r", ".pdf")
    assert (first.name, second.name) == ("r-1.pdf", "r-2.pdf")
    assert (tmp_path / "r.pdf").read_text() == "existing"
    assert Path(first).exists() and first.stat().st_size == 0
