"""Compute library destination paths and copy files."""

from __future__ import annotations

import hashlib
import re
import shutil
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
    ".cue",
    ".log",
    ".toc",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
    ".pdf",
    ".nfo",
    ".m3u",
    ".accurip",
}


def compute_placement(
    album: AlbumTags,
    root: Path = DEFAULT_LIBRARY_ROOT,
    include_non_audio: bool = True,
) -> PlacementPlan:
    artist = _sanitize(album.artist or "Unknown Artist")

    album_name = album.album or "Unknown Album"
    year = _extract_year(album)
    safe = _sanitize(album_name)
    album_dir_name = f"{safe} ({year})" if year else safe

    album_dir = root / artist / album_dir_name

    is_multi_disc = any(_parse_int(t.tags.get("discnumber", "1"), 1) > 1 for t in album.tracks)

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


@dataclass
class CopyResult:
    copied: list[FileMapping]
    skipped: list[FileMapping]
    failed: list[tuple[FileMapping, str]]
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "copied": [m.to_dict() for m in self.copied],
            "skipped": [m.to_dict() for m in self.skipped],
            "failed": [{"mapping": m.to_dict(), "error": e} for m, e in self.failed],
            "total_bytes": self.total_bytes,
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_files(plan: PlacementPlan, dry_run: bool = False) -> CopyResult:
    copied: list[FileMapping] = []
    skipped: list[FileMapping] = []
    failed: list[tuple[FileMapping, str]] = []
    total_bytes = 0

    for mapping in plan.mappings:
        if mapping.dest.exists():
            skipped.append(mapping)
            continue

        if dry_run:
            copied.append(mapping)
            if mapping.source.exists():
                total_bytes += mapping.source.stat().st_size
            continue

        mapping.dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(mapping.source, mapping.dest)
        except OSError as e:
            failed.append((mapping, str(e)))
            continue

        src_size = mapping.source.stat().st_size
        dst_size = mapping.dest.stat().st_size
        if src_size != dst_size:
            mapping.dest.unlink()
            failed.append((mapping, f"size mismatch: {src_size} vs {dst_size}"))
            continue

        src_hash = _sha256(mapping.source)
        dst_hash = _sha256(mapping.dest)
        if src_hash != dst_hash:
            mapping.dest.unlink()
            failed.append((mapping, f"hash mismatch: {src_hash} vs {dst_hash}"))
            continue

        copied.append(mapping)
        total_bytes += src_size

    return CopyResult(
        copied=copied,
        skipped=skipped,
        failed=failed,
        total_bytes=total_bytes,
    )
