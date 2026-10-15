"""Canonical genre list, parsed from the rip-album skill reference.

The markdown table in `.claude/skills/rip-album/references/genre-list.md` is the
single source of truth. It is parsed at runtime rather than mirrored here so the
list cannot drift.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .tags import AUDIO_EXTENSIONS, read_genres

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


@dataclass
class AlbumGenres:
    album_dir: Path
    genres: list[str]


def find_artist_dirs(root: Path, artist: str) -> list[Path]:
    """Artist directories under root whose name matches `artist`.

    Matching ignores case, a leading "The", Unicode width, and punctuation,
    so "the beatles" finds "The Beatles" and "Guns N Roses" finds
    "Guns N' Roses".
    """
    key = _artist_key(artist)
    return sorted(
        path for path in root.iterdir() if path.is_dir() and _artist_key(path.name) == key
    )


def similar_artist_dirs(root: Path, artist: str, limit: int = 5) -> list[str]:
    """Artist directory names close to `artist`, for a failed lookup."""
    names = {_artist_key(path.name): path.name for path in root.iterdir() if path.is_dir()}
    close = difflib.get_close_matches(_artist_key(artist), list(names), n=limit, cutoff=0.6)
    return [names[key] for key in close]


def album_genres(artist_dir: Path) -> list[AlbumGenres]:
    """Genres on each album directory of an artist, in first-seen order per album."""
    albums: list[AlbumGenres] = []
    for album_dir in sorted(path for path in artist_dir.iterdir() if path.is_dir()):
        seen: dict[str, None] = {}
        for track in sorted(album_dir.iterdir()):
            if track.suffix.lower() in AUDIO_EXTENSIONS:
                seen.update(dict.fromkeys(read_genres(track)))
        albums.append(AlbumGenres(album_dir=album_dir, genres=list(seen)))
    return albums


def _artist_key(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name).casefold()
    folded = re.sub(r"^the\s+", "", folded)
    return re.sub(r"[\W_]+", "", folded)
