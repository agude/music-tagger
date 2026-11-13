"""Compute library destination paths and copy files."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lyrics import lyric_sidecars
from .tagger import WHIPPER_PLACEHOLDERS
from .tags import AlbumTags, TrackTags, disc_number, parse_position, parse_total

DEFAULT_LIBRARY_ROOT = Path("/mnt/synology/media/music")


class PlacementConflictError(ValueError):
    """Two or more sources map to the same destination."""


def find_destination_conflicts(mappings: Iterable[FileMapping]) -> list[tuple[Path, list[Path]]]:
    """Destinations that more than one source maps to, with those sources.

    Names are compared case-insensitively, because the library share may be
    case-insensitive.
    """
    groups: dict[str, list[FileMapping]] = {}
    for mapping in mappings:
        key = f"{mapping.dest.parent}/{mapping.dest.name.casefold()}"
        groups.setdefault(key, []).append(mapping)
    return [
        (group[0].dest, [m.source for m in group]) for group in groups.values() if len(group) > 1
    ]


def conflict_message(conflicts: list[tuple[Path, list[Path]]]) -> str:
    lines = [f"  {dest.name}: " + ", ".join(src.name for src in srcs) for dest, srcs in conflicts]
    return "destination collisions:\n" + "\n".join(lines)


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00]')
_STRIP_EDGES = re.compile(r"^[\s.]+|[\s.]+$")


# The longest file or directory name most filesystems accept, in UTF-8 bytes.
MAX_NAME_BYTES = 255


def _fit_bytes(text: str, budget: int) -> str:
    """The longest leading run of characters whose UTF-8 encoding fits in budget bytes."""
    kept: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > budget:
            break
        kept.append(char)
        used += size
    return "".join(kept)


def sanitize_name(name: str, budget: int = MAX_NAME_BYTES) -> str:
    """Make name safe as one path component, cut to at most budget UTF-8 bytes."""
    name = _UNSAFE_CHARS.sub("-", name)
    name = _STRIP_EDGES.sub("", _fit_bytes(name, budget))
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
    ".lrc",
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
    artist_name = sanitize_name(artist_dir or album.artist or "Unknown Artist")
    if album_dir:
        album_dir_name = sanitize_name(album_dir)
    else:
        year = _extract_year(album)
        year_suffix = f" ({year})" if year else ""
        budget = MAX_NAME_BYTES - len(year_suffix.encode("utf-8"))
        safe = sanitize_name(album.album or "Unknown Album", budget=budget)
        album_dir_name = safe + year_suffix
    destination = root / artist_name / album_dir_name

    multi_disc = is_multi_disc(album)
    mappings: list[FileMapping] = []
    # Lyric sidecars take the placed audio's name, so Navidrome pairs them
    # with the right track. They are claimed here, so the generic non-audio
    # pass below does not also copy them under their own names.
    claimed: set[Path] = set()
    for track in album.tracks:
        audio_name = track_filename(track, multi_disc)
        mappings.append(FileMapping(source=track.path, dest=destination / audio_name))
        for sidecar in lyric_sidecars(track.path):
            claimed.add(sidecar)
            dest_name = Path(audio_name).stem + sidecar.suffix
            mappings.append(FileMapping(source=sidecar, dest=destination / dest_name))

    if include_non_audio and album.directory.is_dir():
        for f in sorted(album.directory.iterdir()):
            if f in claimed:
                continue
            if f.is_file() and f.suffix.lower() in _NON_AUDIO_EXTENSIONS:
                # Whipper names these after the disc title, which can hold
                # characters (such as ":") that the NAS share mangles.
                budget = MAX_NAME_BYTES - len(f.suffix.encode("utf-8"))
                dest_name = sanitize_name(f.stem, budget=budget) + f.suffix
                mappings.append(FileMapping(source=f, dest=destination / dest_name))

    conflicts = find_destination_conflicts(mappings)
    if conflicts:
        raise PlacementConflictError(conflict_message(conflicts))
    return PlacementPlan(album_dir=destination, mappings=mappings)


def is_multi_disc(album: AlbumTags) -> bool:
    """True when the release has more than one disc, by any track's disc number or total.

    A total comes from `totaldiscs`, or from a "1/2" discnumber. Each of rip,
    rename, and place uses this rule, so they agree on the disc prefix.
    """
    return any(disc_number(t) > 1 or disc_total(t) > 1 for t in album.tracks)


def disc_total(track: TrackTags) -> int:
    """The release's disc count as this track states it, or 0 when it states none."""
    from_number = parse_total(track.tags.get("discnumber", "")) or 0
    return max(from_number, parse_position(track.tags.get("totaldiscs", "")) or 0)


def track_filename(track: TrackTags, multi_disc: bool) -> str:
    """Library file name: "NN. Title.ext", or "D-NN. Title.ext" on multi-disc albums."""
    track_num = parse_position(track.tags.get("tracknumber", "")) or 0
    if multi_disc:
        disc_num = disc_number(track)
        prefix = f"{disc_num}-{track_num:02d}. "
    else:
        prefix = f"{track_num:02d}. "
    suffix = track.path.suffix
    budget = MAX_NAME_BYTES - len(f"{prefix}{suffix}".encode())
    title = sanitize_name(track.tags.get("title", track.path.stem), budget=budget)
    return f"{prefix}{title}{suffix}"


# Whipper's disc template "%A - %d" on a disc ripped with --unknown: the artist
# placeholder, then the disc ID in place of the title.
_UNKNOWN_DISC_STEM = re.compile(r"^Unknown Artist - (?P<disc_id>[A-Za-z0-9._-]{27}-)$")
# Whipper's per-disc files. Its .m3u is left alone: `place` deletes it.
_DISC_FILE_SUFFIXES = frozenset({".cue", ".log", ".toc"})


def unknown_disc_file_renames(album: AlbumTags) -> list[tuple[Path, Path]]:
    """Whipper's per-disc files from an unknown rip, renamed as a matched rip names them.

    A file named "Unknown Artist - <disc ID>.<ext>" gets "<Artist> - <Album>.<ext>",
    with " (Disc N of M)" on a multi-disc album, once the tracks that carry that
    disc ID (MUSICBRAINZ_DISCID) are tagged. Files whose tracks still hold
    whipper's placeholders are left alone.
    """
    by_disc_id: dict[str, list[TrackTags]] = {}
    for track in album.tracks:
        if disc_id := track.tags.get("musicbrainz_discid", ""):
            by_disc_id.setdefault(disc_id, []).append(track)

    multi_disc = is_multi_disc(album)
    total = max((max(disc_number(t), disc_total(t)) for t in album.tracks), default=1)
    renames: list[tuple[Path, Path]] = []
    for path in sorted(album.directory.iterdir()):
        if path.suffix.lower() not in _DISC_FILE_SUFFIXES or not path.is_file():
            continue
        found = _UNKNOWN_DISC_STEM.match(path.stem)
        tracks = by_disc_id.get(found["disc_id"], []) if found else []
        if not tracks:
            continue
        artist = tracks[0].tags.get("albumartist") or tracks[0].tags.get("artist", "")
        title = tracks[0].tags.get("album", "")
        if not artist or not title:
            continue
        if artist == WHIPPER_PLACEHOLDERS["artist"] or title == WHIPPER_PLACEHOLDERS["album"]:
            continue
        name = f"{artist} - {title}"
        if multi_disc:
            name += f" (Disc {disc_number(tracks[0])} of {total})"
        budget = MAX_NAME_BYTES - len(path.suffix.encode())
        renames.append((path, path.with_name(sanitize_name(name, budget=budget) + path.suffix)))
    return renames


def _extract_year(album: AlbumTags) -> str:
    for track in album.tracks:
        date = track.tags.get("date", "")
        if date and len(date) >= 4:
            return date[:4]
    return ""


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

    `on_file` is called after each file with "copied", "skipped (identical)",
    "skipped (exists, differs)", "skipped (exists)" (dry run only, not hashed),
    or "failed: <reason>", so a slow copy can show progress.
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


def remove_copied_sources(result: CopyResult, source: Path) -> list[Path]:
    """Delete the source files that were copied and verified; return what remains.

    Top-level ".m3u" files are deleted too: whipper writes them, and they are
    deliberately not copied. The source directory is removed only when it is
    empty. Anything else, such as an uncopied file or a subdirectory, is kept.
    """
    for mapping in result.copied:
        mapping.source.unlink()
    for entry in source.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".m3u":
            entry.unlink()
    remaining = sorted(source.iterdir())
    if not remaining:
        source.rmdir()
    return remaining


def _copy_one(
    mapping: FileMapping,
    dry_run: bool,
    copied: list[FileMapping],
    skipped: list[FileMapping],
    failed: list[tuple[FileMapping, str]],
) -> str:
    """Copy one file and record it in the matching list. Returns its status.

    Every error, including one from stat or hashing, is recorded as a failure.
    """
    try:
        if mapping.dest.exists():
            if dry_run:
                skipped.append(mapping)
                return "skipped (exists)"
            identical = _sha256(mapping.source) == _sha256(mapping.dest)
            skipped.append(mapping)
            return "skipped (identical)" if identical else "skipped (exists, differs)"
        if dry_run:
            copied.append(mapping)
            return "copied"
        _copy_verified(mapping)
    except OSError as e:
        failed.append((mapping, str(e)))
        return f"failed: {e}"

    copied.append(mapping)
    return "copied"


def _copy_verified(mapping: FileMapping) -> None:
    """Copy to a temporary file beside the destination, verify it, then move it into place.

    The destination never holds a partial or unverified file. On any error the
    temporary file is removed and the error is raised.
    """
    mapping.dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=mapping.dest.parent, prefix=f".{mapping.dest.name}.", suffix=".partial"
    )
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(mapping.source, temp)
        src_size = mapping.source.stat().st_size
        dst_size = temp.stat().st_size
        if src_size != dst_size:
            raise OSError(f"size mismatch: {src_size} vs {dst_size}")
        src_hash = _sha256(mapping.source)
        dst_hash = _sha256(temp)
        if src_hash != dst_hash:
            raise OSError(f"hash mismatch: {src_hash} vs {dst_hash}")
        os.replace(temp, mapping.dest)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
