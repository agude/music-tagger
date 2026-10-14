"""Orchestration: search MusicBrainz candidates, compute diffs, apply tags."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .musicbrainz import MBRelease, MBTrack, MusicBrainzClient
from .tags import (
    AlbumTags,
    TagChange,
    TrackTags,
    compute_diff,
    read_album,
    read_locks,
    write_tags,
)


@dataclass
class TagConflict:
    field: str
    old_value: str
    candidate_values: list[str]
    resolution: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.resolution is not None and self.resolution in [
            self.old_value,
            *self.candidate_values,
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "old_value": self.old_value,
            "candidate_values": list(self.candidate_values),
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TagConflict:
        return cls(
            field=data["field"],
            old_value=data["old_value"],
            candidate_values=list(data["candidate_values"]),
            resolution=data.get("resolution"),
        )


@dataclass
class TrackDiff:
    track: TrackTags
    changes: list[TagChange]
    conflicts: list[TagConflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Changes the release proposes for locked fields; reported, never written.
    locked_changes: list[TagChange] = field(default_factory=list)

    @property
    def effective_changes(self) -> list[TagChange]:
        changes = list(self.changes)
        for conflict in self.conflicts:
            resolution = conflict.resolution
            if conflict.is_resolved and resolution is not None and resolution != conflict.old_value:
                changes.append(TagChange(conflict.field, conflict.old_value, resolution))
        return changes

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.track.path),
            "format": self.track.format,
            "changes": [c.to_dict() for c in self.changes],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "warnings": list(self.warnings),
            "locked_changes": [change.to_dict() for change in self.locked_changes],
        }


@dataclass
class AlbumResult:
    album: AlbumTags
    release: MBRelease
    diffs: list[TrackDiff] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(d.effective_changes for d in self.diffs)

    @property
    def has_locked_changes(self) -> bool:
        return any(d.locked_changes for d in self.diffs)

    @property
    def has_unresolved_conflicts(self) -> bool:
        return any(not conflict.is_resolved for diff in self.diffs for conflict in diff.conflicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release.id,
            "release_title": self.release.title,
            "tracks": [d.to_dict() for d in self.diffs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlbumResult:
        diffs = []
        for td in data.get("tracks", []):
            track = TrackTags(
                path=Path(td["path"]),
                format=td.get("format", ""),
            )
            changes = [TagChange.from_dict(c) for c in td.get("changes", [])]
            conflicts = [TagConflict.from_dict(c) for c in td.get("conflicts", [])]
            warnings = list(td.get("warnings", []))
            locked_changes = [TagChange.from_dict(c) for c in td.get("locked_changes", [])]
            diffs.append(TrackDiff(track, changes, conflicts, warnings, locked_changes))
        release = MBRelease(id=data["release_id"], title=data.get("release_title", ""))
        album = AlbumTags(directory=Path("."))
        return cls(album=album, release=release, diffs=diffs)


@dataclass
class MatchStats:
    track_count_match: bool
    tracks_within_2s: int
    tracks_compared: int
    max_deviation_secs: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_count_match": self.track_count_match,
            "tracks_within_2s": self.tracks_within_2s,
            "tracks_compared": self.tracks_compared,
            "max_deviation_secs": round(self.max_deviation_secs, 2),
        }


@dataclass
class CandidateMatch:
    release: MBRelease
    stats: MatchStats

    def to_dict(self) -> dict[str, Any]:
        return {
            "release": self.release.to_dict(),
            "match": self.stats.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateMatch:
        return cls(
            release=MBRelease.from_dict(data["release"]),
            stats=MatchStats(
                track_count_match=data["match"]["track_count_match"],
                tracks_within_2s=data["match"]["tracks_within_2s"],
                tracks_compared=data["match"]["tracks_compared"],
                max_deviation_secs=data["match"]["max_deviation_secs"],
            ),
        )


def score_candidates(
    album: AlbumTags,
    candidates: list[MBRelease],
) -> list[CandidateMatch]:
    """Score candidates by duration match against local evidence.

    For each candidate, compares local track durations to MB track durations,
    matching by track number within each disc. Returns results sorted by
    match quality (most tracks within 2s first, then lowest max deviation).
    """
    results: list[CandidateMatch] = []

    local_by_key: dict[tuple[int, int], float] = {}
    for track in album.tracks:
        disc = _parse_disc_number(track)
        tnum = _parse_track_number(track)
        if tnum is not None:
            local_by_key[(disc, tnum)] = track.duration_secs

    for candidate in candidates:
        within_2s = 0
        compared = 0
        max_dev = 0.0

        for disc_num, disc_tracks in candidate.discs.items():
            for mb_track in disc_tracks:
                local_dur = local_by_key.get((disc_num, mb_track.number))
                if local_dur is None or mb_track.duration_ms is None:
                    continue
                compared += 1
                dev = abs(local_dur - mb_track.duration_ms / 1000.0)
                max_dev = max(max_dev, dev)
                if dev <= 2.0:
                    within_2s += 1

        results.append(
            CandidateMatch(
                release=candidate,
                stats=MatchStats(
                    track_count_match=candidate.track_count == album.track_count,
                    tracks_within_2s=within_2s,
                    tracks_compared=compared,
                    max_deviation_secs=max_dev,
                ),
            )
        )

    results.sort(key=lambda m: (-m.stats.tracks_within_2s, m.stats.max_deviation_secs))
    return results


def _parse_disc_number(track: TrackTags) -> int:
    """Extract disc number from tags. Handles '2', '2/3', etc. Falls back to 1."""
    raw = track.tags.get("discnumber", "1")
    try:
        return int(raw.split("/")[0])
    except (ValueError, IndexError):
        return 1


def _build_new_tags(
    release: MBRelease,
    track: TrackTags,
    track_index: int,
    disc_num: int,
) -> dict[str, str]:
    """Build the target tag dict for a track from the matched release."""
    disc_tracks = release.discs.get(disc_num, [])
    mb_track = disc_tracks[track_index] if track_index < len(disc_tracks) else None

    tags: dict[str, str] = {}

    if mb_track:
        tags["title"] = mb_track.title
        tags["tracknumber"] = str(mb_track.number)

    tags["album"] = release.title
    tags["discnumber"] = str(disc_num)
    tags["totaldiscs"] = str(len(release.discs)) if release.discs else "1"
    tags["totaltracks"] = str(len(disc_tracks)) if disc_tracks else str(release.track_count)

    if release.first_release_date and not track.tags.get("date"):
        tags["date"] = release.first_release_date
    if release.date:
        tags["releasedate"] = release.date
    if release.country:
        tags["country"] = release.country
    if release.label:
        tags["label"] = release.label
    if release.catalognum:
        tags["catalognumber"] = release.catalognum
    if release.barcode:
        tags["barcode"] = release.barcode
    if release.format:
        tags["media"] = release.format
    if release.status:
        tags["releasestatus"] = release.status

    # Artist
    if release.artist:
        tags["albumartist"] = release.artist
    if mb_track and mb_track.artist:
        tags["artist"] = mb_track.artist
    elif release.artist:
        tags["artist"] = release.artist

    # Release-level metadata
    tags["musicbrainz_albumid"] = release.id
    if release.artist_id:
        tags["musicbrainz_albumartistid"] = release.artist_id
    if release.release_group_id:
        tags["musicbrainz_releasegroupid"] = release.release_group_id
    if release.release_group_type:
        types = [release.release_group_type, *release.secondary_types]
        tags["releasetype"] = "; ".join(types)
    if "Compilation" in release.secondary_types:
        tags["compilation"] = "1"
    if release.first_release_date:
        tags["originaldate"] = release.first_release_date
    if release.asin:
        tags["asin"] = release.asin
    if release.script:
        tags["script"] = release.script

    # Track-level artist
    if mb_track and mb_track.artist_id:
        tags["musicbrainz_artistid"] = mb_track.artist_id
    elif release.artist_id:
        tags["musicbrainz_artistid"] = release.artist_id

    # Track-level IDs and credits
    if mb_track:
        if mb_track.recording_id:
            tags["musicbrainz_recordingid"] = mb_track.recording_id
        if mb_track.track_id:
            tags["musicbrainz_releasetrackid"] = mb_track.track_id
        # ISRCs belong to recordings; list order does not identify this disc's code.
        if not track.tags.get("isrc") and len(mb_track.isrcs) == 1:
            tags["isrc"] = mb_track.isrcs[0]
        if mb_track.work_id:
            tags["musicbrainz_workid"] = mb_track.work_id

        # Personnel
        _set_credit(
            tags, "composer", mb_track.composer, "musicbrainz_composerid", mb_track.composer_id
        )
        _set_credit(
            tags, "lyricist", mb_track.lyricist, "musicbrainz_lyricistid", mb_track.lyricist_id
        )
        _set_credit(
            tags, "producer", mb_track.producer, "musicbrainz_producerid", mb_track.producer_id
        )
        _set_credit(
            tags, "engineer", mb_track.engineer, "musicbrainz_engineerid", mb_track.engineer_id
        )
        _set_credit(tags, "mixer", mb_track.mixer, "musicbrainz_mixerid", mb_track.mixer_id)
        _set_credit(
            tags, "conductor", mb_track.conductor, "musicbrainz_conductorid", mb_track.conductor_id
        )
        _set_credit(tags, "remixer", mb_track.remixer, "musicbrainz_remixerid", mb_track.remixer_id)
        _set_credit(
            tags,
            "performer",
            mb_track.performers,
            "musicbrainz_performerid",
            mb_track.performer_ids,
        )

    return tags


def _set_credit(tags: dict[str, str], name_field: str, name: str, id_field: str, aid: str) -> None:
    if name:
        tags[name_field] = name
    if aid:
        tags[id_field] = aid


def search_candidates(
    directory: Path,
    mb_client: MusicBrainzClient,
    *,
    artist: str | None = None,
    album_title: str | None = None,
    barcode: str | None = None,
) -> tuple[AlbumTags, list[MBRelease]]:
    """Read album tags and fetch detailed MusicBrainz candidates."""
    album = read_album(directory)
    if not album.tracks:
        raise ValueError(f"No audio files found in {directory}")

    search_artist = artist or album.artist
    search_album = album_title or album.album

    if not search_artist and not search_album:
        raise ValueError("No artist/album in tags and none provided via --artist/--album")

    candidates = mb_client.search_releases(search_artist, search_album, barcode=barcode)
    if not candidates:
        raise ValueError(f"No MusicBrainz results for '{search_artist}' - '{search_album}'")

    detailed: list[MBRelease] = []
    for c in candidates:
        detailed.append(mb_client.fetch_release(c.id))

    return album, detailed


def _parse_track_number(track: TrackTags) -> int | None:
    """Extract track number from tags. Returns None if missing/unparseable."""
    raw = track.tags.get("tracknumber", "")
    try:
        return int(raw.split("/")[0])
    except (ValueError, IndexError):
        return None


def _check_isrc(track: TrackTags, mb_track: MBTrack) -> tuple[list[TagConflict], list[str]]:
    existing = track.tags.get("isrc", "")
    if not existing or existing in mb_track.isrcs:
        return [], []
    if not mb_track.isrcs:
        return [], [f"ISRC {existing} is unverified: MusicBrainz lists no ISRCs."]
    # Absence from MusicBrainz does not prove a code read from the CD is wrong.
    return [TagConflict("isrc", existing, list(mb_track.isrcs))], []


def build_diff(album: AlbumTags, release: MBRelease, *, ignore_locks: bool = False) -> AlbumResult:
    """Compute the tag diff between current tags and a chosen release.

    Matches local files to MB tracks by tracknumber tag. When no tracks
    have tracknumber tags (e.g. freshly ripped files), falls back to
    positional matching by file sort order.

    Changes to a track's locked fields go to `locked_changes` instead of
    `changes`, unless `ignore_locks` is set.
    """
    import sys

    # Index MB tracks by (disc, number) for direct lookup
    mb_index: dict[tuple[int, int], int] = {}
    for disc_num, disc_tracks in release.discs.items():
        for i, mbt in enumerate(disc_tracks):
            mb_index[(disc_num, mbt.number)] = i

    # Detect whether any track has a tracknumber tag
    has_any_tracknumber = any(_parse_track_number(t) is not None for t in album.tracks)
    positional = not has_any_tracknumber and len(album.tracks) > 0
    if positional:
        print(
            f"Warning: no tracknumber tags found; matching {len(album.tracks)} files by position",
            file=sys.stderr,
        )

    diffs: list[TrackDiff] = []
    for file_pos, track in enumerate(album.tracks):
        disc_num = _parse_disc_number(track)
        track_num = _parse_track_number(track)

        track_index: int | None = None
        if track_num is not None:
            key = (disc_num, track_num)
            if key in mb_index:
                track_index = mb_index[key]
        elif positional:
            track_index = file_pos if file_pos < len(release.discs.get(disc_num, [])) else None

        locked_fields = set() if ignore_locks else set(track.locked_fields)
        conflicts: list[TagConflict] = []
        warnings: list[str] = []
        if track_index is not None:
            new_tags = _build_new_tags(release, track, track_index, disc_num)
            mb_track = release.discs[disc_num][track_index]
            # A locked ISRC is a decision already made; do not re-raise it.
            if "isrc" not in locked_fields:
                conflicts, warnings = _check_isrc(track, mb_track)
        else:
            new_tags = _build_new_tags(
                release, track, len(release.discs.get(disc_num, [])), disc_num
            )

        all_changes = compute_diff(track, new_tags)
        changes = [c for c in all_changes if c.field not in locked_fields]
        locked_changes = [c for c in all_changes if c.field in locked_fields]
        diffs.append(TrackDiff(track, changes, conflicts, warnings, locked_changes))

    return AlbumResult(album=album, release=release, diffs=diffs)


def apply_changes(result: AlbumResult, *, ignore_locks: bool = False) -> int:
    """Write all tag changes. Returns the number of tracks modified.

    Raises ValueError, before writing any track, for unresolved conflicts or
    for a change to a field the file has locked, unless `ignore_locks` is set.
    """
    # Check the whole album before writing any track or changing recording IDs.
    if result.has_unresolved_conflicts:
        paths = [
            str(diff.track.path)
            for diff in result.diffs
            if any(not conflict.is_resolved for conflict in diff.conflicts)
        ]
        raise ValueError(
            "Unresolved tag conflicts: "
            + ", ".join(paths)
            + ". Set each conflict's resolution to its old value or a candidate in the diff JSON."
        )
    for diff in result.diffs:
        conflict_fields = {conflict.field for conflict in diff.conflicts}
        if any(change.field in conflict_fields for change in diff.changes):
            raise ValueError(
                f"{diff.track.path}: resolve the conflict instead of adding a duplicate tag change."
            )
    if not ignore_locks:
        _check_locks(result)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    for diff in result.diffs:
        changes = diff.effective_changes
        if changes:
            timestamp = TagChange(
                field="music_tagger_updated",
                old_value=diff.track.tags.get("music_tagger_updated", ""),
                new_value=now,
            )
            write_tags(diff.track, [*changes, timestamp])
            count += 1
    return count


def _check_locks(result: AlbumResult) -> None:
    """Refuse changes to fields locked in the file itself.

    Reads the locks from disk because a diff JSON may be stale or hand-edited.
    """
    violations = []
    for diff in result.diffs:
        changed_fields = {change.field for change in diff.effective_changes}
        if not changed_fields:
            continue
        locked = changed_fields & set(read_locks(diff.track.path))
        if locked:
            violations.append(f"{diff.track.path} ({', '.join(sorted(locked))})")
    if violations:
        raise ValueError(
            "Locked fields: "
            + "; ".join(violations)
            + ". Run `unlock` on these fields, or `tag --ignore-locks`."
        )
