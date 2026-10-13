"""Output-file rules shared by every adapter that writes files.

* Files go only inside the user-approved output directory (created if missing).
* File names come from a template with a fixed set of ``{placeholders}``; no
  ``str.format``, so templates cannot reach attributes or indexes.
* Existing files are never overwritten: ``name.pdf``, ``name-1.pdf``, ``name-2.pdf`` ...
  The chosen name is reserved atomically (exclusive create) before the app writes it.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path("~/Documents/AGC")

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_STEM = 120
MAX_SUFFIX = 999


class OutputPathError(ValueError):
    """The requested output location or name is not allowed."""


def resolve_output_dir(allowed_dir: str | os.PathLike[str], requested: str | os.PathLike[str] | None = None) -> Path:
    """Return the directory to write into, creating it if needed.

    ``requested`` may be absolute or relative to ``allowed_dir``; ``~`` is
    expanded. The resolved path (symlinks followed) must be ``allowed_dir``
    or inside it, otherwise :class:`OutputPathError` is raised.
    """
    root = Path(allowed_dir).expanduser().resolve()
    target = root
    if requested not in (None, ""):
        candidate = Path(requested).expanduser()
        target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise OutputPathError(f"output folder must be inside {root}")
    target.mkdir(parents=True, exist_ok=True)
    # Re-check after creation in case a component was swapped for a symlink.
    if target.resolve() != target:
        raise OutputPathError(f"output folder must be inside {root}")
    return target


def render_name(template: str, *, doc: str = "", now: datetime | None = None) -> str:
    """Fill ``{timestamp}``, ``{date}``, ``{time}`` and ``{doc}`` and sanitize the result into a file stem."""
    now = now or datetime.now()
    values = {
        "timestamp": now.strftime("%Y%m%d-%H%M%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H%M%S"),
        "doc": Path(doc).stem if doc else "document",
    }

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise OutputPathError(f"unknown placeholder {{{key}}} in name template")
        return values[key]

    return sanitize_stem(_PLACEHOLDER.sub(fill, template))


def sanitize_stem(stem: str) -> str:
    """Make ``stem`` a safe single path component (no separators, no reserved device names)."""
    cleaned = _INVALID_CHARS.sub("_", stem).strip(" .")
    cleaned = cleaned[:MAX_STEM].strip(" .")
    if not cleaned:
        cleaned = "agc"
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def reserve_unique(directory: Path, stem: str, suffix: str) -> Path:
    """Atomically create an empty ``stem[-n]suffix`` in ``directory`` and return its path."""
    for n in range(MAX_SUFFIX + 1):
        path = directory / f"{stem}{'' if n == 0 else f'-{n}'}{suffix}"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise OutputPathError(f"too many files named {stem}{suffix} in {directory}")
