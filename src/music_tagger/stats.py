"""Statistics snapshot: per-song ratings and play counts exported from Navidrome.

Navidrome stays the source of truth. A snapshot is a complete, versioned copy
of one user's statistics, written by `nd stats` and read by `write-stats`.

The native API omits zero and false values (plan A0), so the exporter writes
them explicitly. In a snapshot, an absent key means "no instruction" and leaves
the matching file value unchanged. `rating: 0` means unrated, `playCount: 0`
means never played, and `starred: false` means not a favorite.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 1
MAX_RATING = 5
PREVIOUS_SUFFIX = ".previous"

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")


class SnapshotError(ValueError):
    """A snapshot cannot be read or is invalid. Raised before any file write."""


def _text(data: dict[str, Any], key: str, where: str, *, required: bool) -> str:
    if key not in data:
        if required:
            raise SnapshotError(f"{where}.{key}: missing")
        return ""
    value = data[key]
    if not isinstance(value, str):
        raise SnapshotError(f"{where}.{key}: expected a string, got {value!r}")
    if required and not value:
        raise SnapshotError(f"{where}.{key}: must not be empty")
    return value


def _optional_int(
    data: dict[str, Any], key: str, where: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(f"{where}.{key}: expected an integer, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f"at least {minimum}"
        raise SnapshotError(f"{where}.{key}: {value} is outside {bounds}")
    return value


def _optional_bool(data: dict[str, Any], key: str, where: str) -> bool | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, bool):
        raise SnapshotError(f"{where}.{key}: expected true or false, got {value!r}")
    return value


def _optional_timestamp(data: dict[str, Any], key: str, where: str) -> str | None:
    # Absent and null both mean unset. Timestamps never drive a file write.
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise SnapshotError(f"{where}.{key}: expected an RFC 3339 timestamp, got {value!r}")
    return value


@dataclass(frozen=True)
class SongStats:
    """One song's statistics. None means the snapshot gives no instruction."""

    id: str
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    rating: int | None = None
    rated_at: str | None = None
    starred: bool | None = None
    starred_at: str | None = None
    play_count: int | None = None
    play_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }
        optional: dict[str, Any] = {
            "rating": self.rating,
            "ratedAt": self.rated_at,
            "starred": self.starred,
            "starredAt": self.starred_at,
            "playCount": self.play_count,
            "playDate": self.play_date,
        }
        # Keep explicit zero and false; drop only None, which is "no instruction".
        data.update({key: value for key, value in optional.items() if value is not None})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> SongStats:
        return cls(
            id=_text(data, "id", where, required=True),
            path=_text(data, "path", where, required=True),
            title=_text(data, "title", where, required=False),
            artist=_text(data, "artist", where, required=False),
            album=_text(data, "album", where, required=False),
            rating=_optional_int(data, "rating", where, minimum=0, maximum=MAX_RATING),
            rated_at=_optional_timestamp(data, "ratedAt", where),
            starred=_optional_bool(data, "starred", where),
            starred_at=_optional_timestamp(data, "starredAt", where),
            play_count=_optional_int(data, "playCount", where, minimum=0),
            play_date=_optional_timestamp(data, "playDate", where),
        )

    @classmethod
    def from_native(cls, data: dict[str, Any]) -> SongStats:
        """Map one native /api/song record.

        The native API omits zero and false, so an absent rating, play count,
        or star becomes an explicit 0 or false here. Supplied values are
        validated without coercion before they enter the snapshot.
        """
        return cls.from_dict(
            {
                **data,
                "rating": data.get("rating", 0),
                "starred": data.get("starred", False),
                "playCount": data.get("playCount", 0),
            },
            "native song",
        )


@dataclass(frozen=True)
class StatsSnapshot:
    """A complete export of one user's statistics."""

    source_user: str
    exported_at: str
    songs: tuple[SongStats, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "sourceUser": self.source_user,
            "exportedAt": self.exported_at,
            "songs": [song.to_dict() for song in self.songs],
        }


def parse_snapshot(data: object) -> StatsSnapshot:
    """Validate decoded JSON and return a snapshot. Performs no I/O."""
    if isinstance(data, list):
        raise SnapshotError(
            "this is a legacy ratings file, not a statistics snapshot; "
            "create one with `music-tagger nd stats`"
        )
    if not isinstance(data, dict):
        raise SnapshotError("snapshot must be a JSON object")

    version = data.get("version")
    if isinstance(version, bool) or version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"unsupported snapshot version {version!r}; this build reads version {SNAPSHOT_VERSION}"
        )

    source_user = _text(data, "sourceUser", "snapshot", required=True)
    exported_at = _text(data, "exportedAt", "snapshot", required=True)
    if not _TIMESTAMP.fullmatch(exported_at) or not exported_at.endswith("Z"):
        raise SnapshotError(
            f"snapshot.exportedAt: expected a UTC timestamp ending in Z, got {exported_at!r}"
        )

    raw_songs = data.get("songs")
    if not isinstance(raw_songs, list):
        raise SnapshotError("snapshot.songs: expected a list")

    songs: list[SongStats] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_songs):
        where = f"songs[{index}]"
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where}: expected an object")
        song = SongStats.from_dict(raw, where)
        if song.id in seen_ids:
            raise SnapshotError(f"{where}.id: duplicate song id {song.id!r}")
        seen_ids.add(song.id)
        songs.append(song)

    return StatsSnapshot(source_user=source_user, exported_at=exported_at, songs=tuple(songs))


def read_snapshot(path: Path) -> StatsSnapshot:
    """Read and validate a snapshot file. Read-only."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SnapshotError(f"{path}: cannot read snapshot: {exc}") from exc
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"{path}: not valid JSON: {exc}") from exc
    try:
        return parse_snapshot(data)
    except SnapshotError as exc:
        raise SnapshotError(f"{path}: {exc}") from exc


def write_snapshot_file(snapshot: StatsSnapshot, dest: Path) -> None:
    """Write a snapshot, keeping the prior one as `<name>.previous`.

    The snapshot is validated first, so an invalid one never touches `dest`.
    It is then written to a temporary file in the same directory, the current
    file is renamed to `.previous`, and the temporary file is renamed to `dest`.
    A failure before the last rename leaves a complete valid snapshot at `dest`
    or at `.previous`. Rename atomicity depends on the mount, so that is the
    full guarantee.
    """
    data = snapshot.to_dict()
    parse_snapshot(data)

    previous = dest.with_name(dest.name + PREVIOUS_SUFFIX)
    fd, temp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; match a file opened with the usual umask.
        os.chmod(temp, 0o644)
        if dest.exists():
            os.replace(dest, previous)
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
