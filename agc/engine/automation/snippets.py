"""Predefined text snippets (PRD non-goal: gestures never become free text).

Snippets live in a local YAML file::

    snippets:
      meeting_header: |
        Meeting notes - {date}

Only ``{date}`` and ``{time}`` are substituted; any other braces stay as typed.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import yaml

DEFAULT_SNIPPETS_PATH = Path(__file__).resolve().parents[2] / "config" / "snippets.yaml"

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FIELDS = re.compile(r"\{(date|time)\}")


class SnippetError(ValueError):
    """Unknown or invalid snippet id, or an unreadable snippets file."""


class SnippetStore:
    """Loads snippets from a YAML file, reloading when the file changes."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_SNIPPETS_PATH) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._snippets: dict[str, str] = {}

    def get(self, snippet_id: str, now: datetime | None = None) -> str:
        """Return the text for ``snippet_id`` with ``{date}``/``{time}`` filled in."""
        if not isinstance(snippet_id, str) or not _ID.match(snippet_id):
            raise SnippetError("snippet_id must be 1-64 letters, digits, '_' or '-'")
        snippets = self._load()
        if snippet_id not in snippets:
            raise SnippetError(f"no snippet named {snippet_id!r} in {self.path.name}")
        now = now or datetime.now()
        values = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M")}
        return _FIELDS.sub(lambda m: values[m.group(1)], snippets[snippet_id])

    def ids(self) -> list[str]:
        """All snippet ids in the file."""
        return sorted(self._load())

    def _load(self) -> dict[str, str]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            raise SnippetError(f"snippets file not found: {self.path}") from exc
        if mtime != self._mtime:
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise SnippetError(f"could not read {self.path.name}: {exc}") from exc
            raw = data.get("snippets") if isinstance(data, dict) else None
            if not isinstance(raw, dict):
                raise SnippetError(f"{self.path.name} must contain a 'snippets:' mapping")
            self._snippets = {
                str(key): value for key, value in raw.items() if isinstance(value, str) and _ID.match(str(key))
            }
            self._mtime = mtime
        return self._snippets
