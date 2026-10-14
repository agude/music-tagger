"""Compute library destination paths and copy files."""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tags import AlbumTags, TrackTags

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
    ".accurip",
}
# Deliberately excludes ".m3u": whipper writes a per-album playlist that
# Navidrome would import as a junk playlist. Curated playlists live in
# /mnt/synology/media/music/playlists, which is where Navidrome reads them.


def compute_placement(
    album: AlbumTags,
    root: Path = DEFAULT_LIBRARY_ROOT,
    include_non_audio: bool = True,
    artist_dir: str | None = None,
    album_dir: str | None = None,
) -> PlacementPlan:
    """Plan the library paths for an album.

    `artist_dir` and `album_dir` replace the directory names computed from
    the tags; they are sanitized the same way.
    """
    artist_name = _sanitize(artist_dir or album.artist or "Unknown Artist")
    if album_dir:
        album_dir_name = _sanitize(album_dir)
    else:
        year = _extract_year(album)
        safe = _sanitize(album.album or "Unknown Album")
        album_dir_name = f"{safe} ({year})" if year else safe
    destination = root / artist_name / album_dir_name

    multi_disc = is_multi_disc(album)
    mappings = [
        FileMapping(source=track.path, dest=destination / track_filename(track, multi_disc))
        for track in album.tracks
    ]

    if include_non_audio and album.directory.is_dir():
        for f in sorted(album.directory.iterdir()):
            if f.is_file() and f.suffix.lower() in _NON_AUDIO_EXTENSIONS:
                # Whipper names these after the disc title, which can hold
                # characters (such as ":") that the NAS share mangles.
                dest_name = _sanitize(f.stem) + f.suffix
                mappings.append(FileMapping(source=f, dest=destination / dest_name))

    return PlacementPlan(album_dir=destination, mappings=mappings)


def is_multi_disc(album: AlbumTags) -> bool:
    return any(_parse_int(t.tags.get("discnumber", "1"), 1) > 1 for t in album.tracks)


def track_filename(track: TrackTags, multi_disc: bool) -> str:
    """Library file name: "NN. Title.ext", or "D-NN. Title.ext" on multi-disc albums."""
    track_num = _parse_int(track.tags.get("tracknumber", "0"), 0)
    title = _sanitize(track.tags.get("title", track.path.stem))
    if multi_disc:
        disc_num = _parse_int(track.tags.get("discnumber", "1"), 1)
        return f"{disc_num}-{track_num:02d}. {title}{track.path.suffix}"
    return f"{track_num:02d}. {title}{track.path.suffix}"


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


def copy_files(
    plan: PlacementPlan,
    dry_run: bool = False,
    on_file: Callable[[FileMapping, str], None] | None = None,
) -> CopyResult:
    """Copy and SHA-256 verify each mapping.

    `on_file` is called after each file with "copied", "skipped (exists)", or
    "failed: <reason>", so a slow copy can show progress.
    """
    copied: list[FileMapping] = []
    skipped: list[FileMapping] = []
    failed: list[tuple[FileMapping, str]] = []
    total_bytes = 0

    for mapping in plan.mappings:
        status = _copy_one(mapping, dry_run, copied, skipped, failed)
        if status == "copied" and mapping.source.exists():
            total_bytes += mapping.source.stat().st_size
        if on_file is not None:
            on_file(mapping, status)

    return CopyResult(
        copied=copied,
        skipped=skipped,
        failed=failed,
        total_bytes=total_bytes,
    )


def _copy_one(
    mapping: FileMapping,
    dry_run: bool,
    copied: list[FileMapping],
    skipped: list[FileMapping],
    failed: list[tuple[FileMapping, str]],
) -> str:
    """Copy one file and record it in the matching list. Returns its status."""
    if mapping.dest.exists():
        skipped.append(mapping)
        return "skipped (exists)"
    if dry_run:
        copied.append(mapping)
        return "copied"

    mapping.dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(mapping.source, mapping.dest)
    except OSError as e:
        failed.append((mapping, str(e)))
        return f"failed: {e}"

    src_size = mapping.source.stat().st_size
    dst_size = mapping.dest.stat().st_size
    if src_size != dst_size:
        mapping.dest.unlink()
        error = f"size mismatch: {src_size} vs {dst_size}"
        failed.append((mapping, error))
        return f"failed: {error}"

    src_hash = _sha256(mapping.source)
    dst_hash = _sha256(mapping.dest)
    if src_hash != dst_hash:
        mapping.dest.unlink()
        error = f"hash mismatch: {src_hash} vs {dst_hash}"
        failed.append((mapping, error))
        return f"failed: {error}"

    copied.append(mapping)
    return "copied"
