"""Compute library destination paths from album evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tags import AlbumTags

DEFAULT_LIBRARY_ROOT = Path("/mnt/synology/media/music")

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00]')
_STRIP_EDGES = re.compile(r"^[\s.]+|[\s.]+$")


def _sanitize(name: str) -> str:
    name = _UNSAFE_CHARS.sub("-", name)
    name = _STRIP_EDGES.sub("", name)
    return name or "_"


@dataclass
class FileMapping:
    source: Path
    dest: Path

    def to_dict(self) -> dict[str, str]:
        return {"source": str(self.source), "dest": str(self.dest)}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> FileMapping:
        return cls(source=Path(data["source"]), dest=Path(data["dest"]))


@dataclass
class PlacementPlan:
    album_dir: Path
    mappings: list[FileMapping]

    def to_dict(self) -> dict[str, Any]:
        return {
            "album_dir": str(self.album_dir),
            "mappings": [m.to_dict() for m in self.mappings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlacementPlan:
        return cls(
            album_dir=Path(data["album_dir"]),
            mappings=[FileMapping.from_dict(m) for m in data["mappings"]],
        )


_NON_AUDIO_EXTENSIONS = {
    ".cue", ".log", ".toc", ".txt", ".jpg", ".jpeg", ".png",
    ".pdf", ".nfo", ".m3u", ".accurip",
}


def compute_placement(
    album: AlbumTags,
    root: Path = DEFAULT_LIBRARY_ROOT,
    include_non_audio: bool = True,
) -> PlacementPlan:
    artist = _sanitize(album.artist or "Unknown Artist")

    album_name = album.album or "Unknown Album"
    year = _extract_year(album)
    if year:
        album_dir_name = f"{_sanitize(album_name)} ({year})"
    else:
        album_dir_name = _sanitize(album_name)

    album_dir = root / artist / album_dir_name

    is_multi_disc = any(
        _parse_int(t.tags.get("discnumber", "1"), 1) > 1
        for t in album.tracks
    )

    mappings: list[FileMapping] = []
    for track in album.tracks:
        track_num = _parse_int(track.tags.get("tracknumber", "0"), 0)
        title = _sanitize(track.tags.get("title", track.path.stem))
        ext = track.path.suffix

        if is_multi_disc:
            disc_num = _parse_int(track.tags.get("discnumber", "1"), 1)
            filename = f"{disc_num}-{track_num:02d}. {title}{ext}"
        else:
            filename = f"{track_num:02d}. {title}{ext}"

        mappings.append(FileMapping(source=track.path, dest=album_dir / filename))

    if include_non_audio and album.directory.is_dir():
        for f in sorted(album.directory.iterdir()):
            if f.is_file() and f.suffix.lower() in _NON_AUDIO_EXTENSIONS:
                mappings.append(FileMapping(source=f, dest=album_dir / f.name))

    return PlacementPlan(album_dir=album_dir, mappings=mappings)


def _extract_year(album: AlbumTags) -> str:
    for track in album.tracks:
        date = track.tags.get("date", "")
        if date and len(date) >= 4:
            return date[:4]
    return ""


def _parse_int(raw: str, default: int) -> int:
    try:
        return int(raw.split("/")[0])
    except (ValueError, IndexError):
        return default
