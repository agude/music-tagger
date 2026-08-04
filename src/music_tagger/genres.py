"""Canonical genre list, parsed from the rip-album skill reference.

The markdown table in `.claude/skills/rip-album/references/genre-list.md` is the
single source of truth. It is parsed at runtime rather than mirrored here so the
list cannot drift.
"""

from __future__ import annotations

import re
from pathlib import Path

GENRE_LIST_PATH = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "rip-album"
    / "references"
    / "genre-list.md"
)

_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|")


def load_canonical(path: Path | None = None) -> frozenset[str]:
    """Parse the genre table. Returns an empty set if the file is missing."""
    target = path or GENRE_LIST_PATH
    if not target.is_file():
        return frozenset()
    names = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        name = match.group(1).strip()
        # Skip the header cell and the |---|---| separator row.
        if not name or name.lower() == "genre" or set(name) <= set("-: "):
            continue
        names.add(name)
    return frozenset(names)


def unknown_genres(genres: list[str], path: Path | None = None) -> list[str]:
    """Return the given genres that are not on the canonical list.

    An empty canonical list (missing reference file) validates everything, so
    callers should warn rather than silently accept.
    """
    canonical = load_canonical(path)
    if not canonical:
        return []
    return [g for g in genres if g not in canonical]
