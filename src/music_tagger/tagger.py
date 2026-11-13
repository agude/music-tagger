"""Orchestration: search MusicBrainz candidates, compute diffs, apply tags."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .musicbrainz import MBRelease, MBTrack, MusicBrainzClient
from .tags import (
    MULTI_VALUE_FIELDS,
    AlbumTags,
    TagChange,
    TrackTags,
    compute_diff,
    disc_number,
    parse_position,
    read_album,
    read_locks,
    write_locks,
    write_tags,
)


class DuplicateTrackError(ValueError):
    """Two or more files claim the same (disc, track) key."""


def duplicate_track_keys(album: AlbumTags) -> dict[tuple[int, int], list[TrackTags]]:
    """Each (disc, track) key claimed by more than one file, with the files that claim it."""
    groups: dict[tuple[int, int], list[TrackTags]] = {}
    for track in album.tracks:
        track_num = parse_position(track.tags.get("tracknumber", ""))
        if track_num is not None:
            groups.setdefault((disc_number(track), track_num), []).append(track)
    return {key: files for key, files in groups.items() if len(files) > 1}


def duplicate_track_message(duplicates: dict[tuple[int, int], list[TrackTags]]) -> str:
    """Name each duplicated key and the files that claim it."""
    return "; ".join(
        f"disc {disc} track {track}: " + ", ".join(f'"{t.path.name}"' for t in files)
        for (disc, track), files in duplicates.items()
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
    def keeps_conflicting_isrc(self) -> bool:
        """True when an ISRC conflict was resolved by keeping the file's code."""
        return any(
            conflict.field == "isrc" and conflict.resolution == conflict.old_value
            for conflict in self.conflicts
        )

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
    # Album-level notes for the operator, such as positional matching. Not serialized.
    warnings: list[str] = field(default_factory=list)

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
    # "on-files": the files already carry this release ID.
    # "partial": the files are a subset of this release's tracks.
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "release": self.release.to_dict(),
            "match": self.stats.to_dict(),
            "flags": list(self.flags),
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
            flags=list(data.get("flags", [])),
        )


def _is_hidden_track(track: TrackTags) -> bool:
    """True for a hidden track: the audio whipper rips before track 1, numbered 0."""
    return parse_position(track.tags.get("tracknumber", "")) == 0


def _counted_tracks(album: AlbumTags) -> list[TrackTags]:
    """The files compared with a release: every file except a hidden track."""
    return [track for track in album.tracks if not _is_hidden_track(track)]


def counted_track_count(album: AlbumTags) -> int:
    """How many files the album is compared with a release by: hidden tracks not counted."""
    return len(_counted_tracks(album))


def score_candidates(
    album: AlbumTags,
    candidates: list[MBRelease],
) -> list[CandidateMatch]:
    """Score candidates by duration match against local evidence.

    For each candidate, compares local track durations to MB track durations,
    matching by track number within each disc. Returns results ranked by
    `_rank_key`. The ranking is advice; the operator makes the choice.
    """
    results: list[CandidateMatch] = []
    local_release_ids = {
        track.tags["musicbrainz_albumid"]
        for track in album.tracks
        if track.tags.get("musicbrainz_albumid")
    }

    # With duplicate keys the first file keeps the key; the flag reports the problem.
    duplicates = duplicate_track_keys(album)
    local_by_key: dict[tuple[int, int], float] = {}
    for track in _counted_tracks(album):
        disc = disc_number(track)
        tnum = parse_position(track.tags.get("tracknumber", ""))
        if tnum is not None:
            local_by_key.setdefault((disc, tnum), track.duration_secs)

    for candidate in candidates:
        compared, within_2s, max_dev = _match_stats(album, local_by_key, candidate)

        flags = []
        if candidate.id in local_release_ids:
            flags.append("on-files")
        if _is_partial_match(album, local_by_key, candidate):
            flags.append("partial")
        if duplicates:
            flags.append("duplicate-keys")
        results.append(
            CandidateMatch(
                release=candidate,
                stats=MatchStats(
                    track_count_match=candidate.track_count == len(_counted_tracks(album)),
                    tracks_within_2s=within_2s,
                    tracks_compared=compared,
                    max_deviation_secs=max_dev,
                ),
                flags=flags,
            )
        )

    results.sort(key=_rank_key)
    return results


def _is_partial_match(
    album: AlbumTags, local_by_key: dict[tuple[int, int], float], candidate: MBRelease
) -> bool:
    """True when every local track's (disc, number) exists in a longer release."""
    counted = len(_counted_tracks(album))
    if counted >= candidate.track_count or len(local_by_key) != counted:
        return False
    release_keys = {
        (disc_num, mb_track.number)
        for disc_num, disc_tracks in candidate.discs.items()
        for mb_track in disc_tracks
    }
    return set(local_by_key) <= release_keys


def _rank_key(match: CandidateMatch) -> tuple[int, int, bool, bool, bool, str, float]:
    """Exact track count, then partial, then durations, the release on the files, CD, US,
    earliest date."""
    release = match.release
    if match.stats.track_count_match:
        count_rank = 0
    elif "partial" in match.flags:
        count_rank = 1
    else:
        count_rank = 2
    return (
        count_rank,
        -match.stats.tracks_within_2s,
        "on-files" not in match.flags,
        "CD" not in release.format,
        release.country != "US",
        release.date or "9999",
        match.stats.max_deviation_secs,
    )


# Fields _build_new_tags sets only when MusicBrainz has a value. When a file moves
# to a different release, a field the new release lacks must not keep the old value.
RELEASE_OWNED_FIELDS = frozenset(
    {
        "releasedate",
        "country",
        "label",
        "catalognumber",
        "barcode",
        "media",
        "releasestatus",
        "musicbrainz_albumartistid",
        "musicbrainz_releasegroupid",
        "releasetype",
        "compilation",
        "originaldate",
        "asin",
        "script",
    }
)
# Set only when the file matches a release track.
TRACK_OWNED_FIELDS = frozenset(
    {
        "musicbrainz_artistid",
        "musicbrainz_recordingid",
        "musicbrainz_releasetrackid",
        "musicbrainz_workid",
        "composer",
        "musicbrainz_composerid",
        "lyricist",
        "musicbrainz_lyricistid",
        "producer",
        "musicbrainz_producerid",
        "engineer",
        "musicbrainz_engineerid",
        "mixer",
        "musicbrainz_mixerid",
        "conductor",
        "musicbrainz_conductorid",
        "remixer",
        "musicbrainz_remixerid",
        "performer",
        "musicbrainz_performerid",
    }
)


def _stale_owned_fields(track: TrackTags, release: MBRelease, *, matched: bool) -> list[str]:
    """The owned fields the file carries but the new release may not supply.

    Applies only when the file carries another release's ID. Each name is cleared by
    its builder: an empty value for a single field, an empty tuple for a multi field.
    """
    current_id = track.tags.get("musicbrainz_albumid", "")
    if not current_id or current_id == release.id:
        return []
    owned = RELEASE_OWNED_FIELDS | (TRACK_OWNED_FIELDS if matched else frozenset())
    return sorted(name for name in owned if track.tags.get(name) or track.multi.get(name))


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
    tags["totaldiscs"] = str(max(release.discs)) if release.discs else "1"
    tags["totaltracks"] = str(len(disc_tracks)) if disc_tracks else str(release.track_count)

    if release.first_release_date and (
        not track.tags.get("date") or is_release_date(track.tags["date"], release)
    ):
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

    # Artist display credits. The per-person artist values are in _build_new_multi.
    if release.artist:
        tags["albumartist"] = release.artist
    if mb_track and mb_track.artist:
        tags["artist"] = mb_track.artist
    elif release.artist:
        tags["artist"] = release.artist

    # Release-level metadata
    tags["musicbrainz_albumid"] = release.id
    if release.release_group_id:
        tags["musicbrainz_releasegroupid"] = release.release_group_id
    if "Compilation" in release.secondary_types:
        tags["compilation"] = "1"
    if release.first_release_date:
        tags["originaldate"] = release.first_release_date
    if release.asin:
        tags["asin"] = release.asin
    if release.script:
        tags["script"] = release.script

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

        # Performers stay one display string; see _build_new_multi for the roles.
        _set_credit(
            tags,
            "performer",
            _normalize_performers(mb_track.performers),
            "musicbrainz_performerid",
            mb_track.performer_ids,
        )

    for name in _stale_owned_fields(track, release, matched=mb_track is not None):
        if name not in MULTI_VALUE_FIELDS and name not in tags:
            tags[name] = ""
    return tags


def _set_credit(tags: dict[str, str], name_field: str, name: str, id_field: str, aid: str) -> None:
    if name:
        tags[name_field] = name
    if aid:
        tags[id_field] = aid


# Personnel roles: each name and its ID field, one value per person.
_ROLE_CREDITS = ("composer", "lyricist", "producer", "engineer", "mixer", "conductor", "remixer")
_ROLE_CREDIT_FIELDS = _ROLE_CREDITS + tuple(f"musicbrainz_{role}id" for role in _ROLE_CREDITS)


def _merge_credit_values(
    musicbrainz_values: tuple[str, ...], existing_values: tuple[str, ...]
) -> tuple[str, ...]:
    """Keep MusicBrainz order, then append existing credits it no longer lists.

    Existing tags sometimes store several values in one semicolon-joined value. Split
    those legacy values before comparing so the joined string is not duplicated.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for value in musicbrainz_values:
        if value and value not in seen:
            merged.append(value)
            seen.add(value)
    for value in existing_values:
        for credit in value.split(";"):
            credit = credit.strip()
            if credit and credit not in seen:
                merged.append(credit)
                seen.add(credit)
    return tuple(merged)


def _preserve_existing_credits(
    track: TrackTags, new_tags: dict[str, str], new_multi: dict[str, tuple[str, ...]]
) -> None:
    """Append missing old personnel credits after the release's current credits."""
    for field_name in _ROLE_CREDIT_FIELDS:
        existing_values = track.multi.get(field_name)
        if existing_values is None:
            existing = track.tags.get(field_name, "")
            existing_values = (existing,) if existing else ()
        proposed_values = new_multi.get(field_name, ())
        merged_values = _merge_credit_values(proposed_values, existing_values)
        if merged_values or field_name in new_multi:
            new_multi[field_name] = merged_values

    performers = _merge_performers(new_tags.get("performer", ""), track.tags.get("performer", ""))
    if performers or "performer" in new_tags:
        new_tags["performer"] = performers

    proposed_ids = new_tags.get("musicbrainz_performerid", "")
    existing_ids = track.tags.get("musicbrainz_performerid", "")
    merged_ids = _merge_credit_values(
        tuple(value.strip() for value in proposed_ids.split(";") if value.strip()),
        (existing_ids,) if existing_ids else (),
    )
    if merged_ids or "musicbrainz_performerid" in new_tags:
        new_tags["musicbrainz_performerid"] = "; ".join(merged_ids)


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on `separator` outside parentheses, so "drums (drum set)" stays whole."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _parse_performer(entry: str) -> tuple[str, list[str]]:
    """Split "Name (role, role)" into the name and its roles, without duplicate roles."""
    if entry.endswith(")"):
        depth = 0
        for index in range(len(entry) - 1, -1, -1):
            if entry[index] == ")":
                depth += 1
            elif entry[index] == "(":
                depth -= 1
                if depth == 0:
                    name = entry[:index].strip()
                    if name:
                        roles: list[str] = []
                        for role in _split_top_level(entry[index + 1 : -1], ","):
                            if role not in roles:
                                roles.append(role)
                        return name, roles
                    break
    return entry, []


def _format_performer(name: str, roles: list[str]) -> str:
    return f"{name} ({', '.join(roles)})" if roles else name


def _normalize_performers(value: str) -> str:
    """Remove repeated roles within each performer, keeping one entry per position.

    Entries stay in place so they still line up with `musicbrainz_performerid`.
    """
    entries = [_parse_performer(entry) for entry in _split_top_level(value, ";")]
    return "; ".join(_format_performer(name, roles) for name, roles in entries)


def _merge_performers(proposed: str, existing: str) -> str:
    """Keep MusicBrainz performers, then add existing ones it omits.

    An existing performer with the same name as a MusicBrainz one is the same
    entry: its missing roles are added to that entry rather than repeating the
    person.
    """
    merged = [_parse_performer(entry) for entry in _split_top_level(proposed, ";")]
    for entry in _split_top_level(existing, ";"):
        name, roles = _parse_performer(entry)
        match = next((item for item in merged if item[0] == name), None)
        if match is None:
            merged.append((name, roles))
        else:
            match[1].extend(role for role in roles if role not in match[1])
    return "; ".join(_format_performer(name, roles) for name, roles in merged)


def _build_new_multi(
    release: MBRelease,
    track: TrackTags,
    track_index: int,
    disc_num: int,
) -> dict[str, tuple[str, ...]]:
    """Build the target values of each multi-valued field for a track from the matched release."""
    disc_tracks = release.discs.get(disc_num, [])
    mb_track = disc_tracks[track_index] if track_index < len(disc_tracks) else None
    multi: dict[str, tuple[str, ...]] = {}

    # Track artists: the track's own credit, else the release's.
    if mb_track and mb_track.artist_credits:
        credits, legacy_id = mb_track.artist_credits, mb_track.artist_id
    elif mb_track and mb_track.artist:
        credits, legacy_id = (), mb_track.artist_id or release.artist_id
    else:
        credits, legacy_id = release.artist_credits, release.artist_id
    if credits:
        multi["artists"] = tuple(name for name, _ in credits)
        ids = tuple(aid for _, aid in credits if aid)
        if ids:
            multi["musicbrainz_artistid"] = ids
    elif legacy_id:
        multi["musicbrainz_artistid"] = (legacy_id,)

    if release.artist_credits:
        multi["albumartists"] = tuple(name for name, _ in release.artist_credits)
        ids = tuple(aid for _, aid in release.artist_credits if aid)
        if ids:
            multi["musicbrainz_albumartistid"] = ids
    elif release.artist_id:
        multi["musicbrainz_albumartistid"] = (release.artist_id,)

    if release.release_group_type:
        multi["releasetype"] = (release.release_group_type, *release.secondary_types)

    if mb_track:
        for role in _ROLE_CREDITS:
            names = getattr(mb_track, role)
            ids = getattr(mb_track, f"{role}_id")
            if names:
                multi[role] = tuple(names)
            if ids:
                multi[f"musicbrainz_{role}id"] = tuple(ids)

    for name in _stale_owned_fields(track, release, matched=mb_track is not None):
        if name in MULTI_VALUE_FIELDS and name not in multi:
            multi[name] = ()
    return multi


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
    detailed: list[MBRelease] = []
    for c in candidates:
        detailed.append(mb_client.fetch_release(c.id))

    return album, detailed


def _check_isrc(track: TrackTags, mb_track: MBTrack) -> tuple[list[TagConflict], list[str]]:
    existing = track.tags.get("isrc", "")
    if not existing or existing in mb_track.isrcs:
        return [], []
    if not mb_track.isrcs:
        return [], [f"ISRC {existing} is unverified: MusicBrainz lists no ISRCs."]
    # Absence from MusicBrainz does not prove a code read from the CD is wrong.
    return [TagConflict("isrc", existing, list(mb_track.isrcs))], []


_MATCH_TOLERANCE_SECS = 2.0


def _duration_dev(track: TrackTags, mb_track: MBTrack) -> float | None:
    """Absolute duration difference in seconds, or None when either side is unknown."""
    if not track.duration_secs or mb_track.duration_ms is None:
        return None
    return abs(track.duration_secs - mb_track.duration_ms / 1000.0)


def _same_title(a: str, b: str) -> bool:
    return bool(a.strip()) and _style_key(a) == _style_key(b)


def _find_partial_track(track: TrackTags, release: MBRelease, disc_num: int) -> int | None:
    """Index on this disc of the release track that a partial album's file is.

    A file's tracknumber is trusted only when that release track's duration or
    title agrees with the file. Otherwise the disc is searched for tracks within
    2 s of the file's duration, and a matching title breaks a tie. Returns None
    when no single track fits.
    """
    disc_tracks = release.discs.get(disc_num, [])
    file_title = track.tags.get("title", "")

    track_num = parse_position(track.tags.get("tracknumber", ""))
    if track_num is not None:
        for index, mb_track in enumerate(disc_tracks):
            if mb_track.number == track_num:
                dev = _duration_dev(track, mb_track)
                if (dev is not None and dev <= _MATCH_TOLERANCE_SECS) or _same_title(
                    file_title, mb_track.title
                ):
                    return index
                break

    fits: list[int] = []
    for index, mb_track in enumerate(disc_tracks):
        dev = _duration_dev(track, mb_track)
        if dev is not None and dev <= _MATCH_TOLERANCE_SECS:
            fits.append(index)
    if len(fits) > 1:
        fits = [i for i in fits if _same_title(file_title, disc_tracks[i].title)]
    return fits[0] if len(fits) == 1 else None


def _match_stats(
    album: AlbumTags, local_by_key: dict[tuple[int, int], float], candidate: MBRelease
) -> tuple[int, int, float]:
    """Compared tracks, tracks within 2 s, and max deviation for one candidate.

    A partial album is matched per file (see `_find_partial_track`). A full
    album is matched by (disc, tracknumber).
    """
    compared = 0
    within_2s = 0
    max_dev = 0.0
    if len(_counted_tracks(album)) < candidate.track_count:
        for track in _counted_tracks(album):
            disc_num = disc_number(track)
            index = _find_partial_track(track, candidate, disc_num)
            if index is None:
                continue
            dev = _duration_dev(track, candidate.discs[disc_num][index])
            if dev is None:
                continue
            compared += 1
            max_dev = max(max_dev, dev)
            if dev <= _MATCH_TOLERANCE_SECS:
                within_2s += 1
        return compared, within_2s, max_dev

    for disc_num, disc_tracks in candidate.discs.items():
        for mb_track in disc_tracks:
            local_dur = local_by_key.get((disc_num, mb_track.number))
            if local_dur is None or mb_track.duration_ms is None:
                continue
            compared += 1
            dev = abs(local_dur - mb_track.duration_ms / 1000.0)
            max_dev = max(max_dev, dev)
            if dev <= _MATCH_TOLERANCE_SECS:
                within_2s += 1
    return compared, within_2s, max_dev


def _drop_track_artist(new_tags: dict[str, str], new_multi: dict[str, tuple[str, ...]]) -> None:
    """Remove the track artist from a proposal: no release track names it for this file."""
    new_tags.pop("artist", None)
    new_multi.pop("artists", None)
    new_multi.pop("musicbrainz_artistid", None)


def build_diff(
    album: AlbumTags,
    release: MBRelease,
    *,
    ignore_locks: bool = False,
    keep_existing_credits: bool = False,
) -> AlbumResult:
    """Compute the tag diff between current tags and a chosen release.

    For a full album, matches local files to MB tracks by tracknumber tag.
    When no tracks have tracknumber tags (e.g. freshly ripped files), falls
    back to positional matching by file sort order.

    For a partial album (fewer files than release tracks), tracknumber tags
    are not trusted alone; see `_find_partial_track`. A file that matches no
    release track is reported as a warning and gets no changes.

    Changes to a track's locked fields go to `locked_changes` instead of
    `changes`, unless `ignore_locks` is set.

    When `keep_existing_credits` is set, matched tracks keep personnel credits
    MusicBrainz no longer lists. Current MusicBrainz credits stay first.
    """
    # Index MB tracks by (disc, number) for direct lookup
    mb_index: dict[tuple[int, int], int] = {}
    for disc_num, disc_tracks in release.discs.items():
        for i, mbt in enumerate(disc_tracks):
            mb_index[(disc_num, mbt.number)] = i

    # Detect whether any track has a tracknumber tag
    has_any_tracknumber = any(
        parse_position(t.tags.get("tracknumber", "")) is not None for t in album.tracks
    )
    positional = not has_any_tracknumber and len(album.tracks) > 0
    album_warnings: list[str] = []
    if positional:
        album_warnings.append(
            f"no tracknumber tags found; matching {len(album.tracks)} files by position"
        )

    duplicates = duplicate_track_keys(album)
    if duplicates:
        raise DuplicateTrackError(duplicate_track_message(duplicates))

    partial = len(_counted_tracks(album)) < release.track_count
    partial_matches: list[int | None] = []
    shared_with: dict[int, list[TrackTags]] = {}
    if partial:
        partial_matches = [
            None
            if _is_hidden_track(track)
            else _find_partial_track(track, release, disc_number(track))
            for track in album.tracks
        ]
        # A release track claimed by two files is claimed by neither.
        claimants: dict[tuple[int, int], list[int]] = {}
        for position, (track, index) in enumerate(zip(album.tracks, partial_matches, strict=True)):
            if index is not None:
                claimants.setdefault((disc_number(track), index), []).append(position)
        for positions in claimants.values():
            if len(positions) > 1:
                for position in positions:
                    partial_matches[position] = None
                    shared_with[position] = [album.tracks[p] for p in positions if p != position]

    diffs: list[TrackDiff] = []
    # Positional matching uses each file's index within its own disc.
    disc_positions: dict[int, int] = {}
    for position, track in enumerate(album.tracks):
        disc_num = disc_number(track)
        track_num = parse_position(track.tags.get("tracknumber", ""))
        disc_pos = disc_positions.get(disc_num, 0)
        disc_positions[disc_num] = disc_pos + 1

        track_index: int | None = None
        if partial:
            track_index = partial_matches[position]
        elif track_num is not None:
            key = (disc_num, track_num)
            if key in mb_index:
                track_index = mb_index[key]
        elif positional:
            track_index = disc_pos if disc_pos < len(release.discs.get(disc_num, [])) else None

        locked_fields = set() if ignore_locks else set(track.locked_fields)
        conflicts: list[TagConflict] = []
        warnings: list[str] = []
        if track_index is not None:
            new_tags = _build_new_tags(release, track, track_index, disc_num)
            new_multi = _build_new_multi(release, track, track_index, disc_num)
            mb_track = release.discs[disc_num][track_index]
            # A locked ISRC is a decision already made; do not re-raise it.
            if "isrc" not in locked_fields:
                conflicts, warnings = _check_isrc(track, mb_track)
        else:
            unmatched_index = len(release.discs.get(disc_num, []))
            new_tags = _build_new_tags(release, track, unmatched_index, disc_num)
            new_multi = _build_new_multi(release, track, unmatched_index, disc_num)
            if _is_hidden_track(track):
                # Album fields only, so Navidrome keeps the hidden track in the album.
                _drop_track_artist(new_tags, new_multi)
                warnings.append(
                    f"{track.path.name}: hidden track before track 1; album fields only"
                )
            elif partial and position in shared_with:
                others = ", ".join(other.path.name for other in shared_with[position])
                warnings.append(
                    f"{track.path.name}: matches the same release track as {others}; "
                    "title and track number left unchanged"
                )
            elif partial:
                warnings.append(
                    f"{track.path.name}: no release track matches its duration and title "
                    f"(tracknumber {track_num}); title and track number left unchanged"
                )
            else:
                # A full album file that matches no release track: album fields only.
                _drop_track_artist(new_tags, new_multi)
                track_label = "(none)" if track_num is None else str(track_num)
                warnings.append(
                    f"{track.path.name}: no release track for disc {disc_num} track {track_label}; "
                    "only album fields proposed"
                )

        if keep_existing_credits and track_index is not None:
            _preserve_existing_credits(track, new_tags, new_multi)

        all_changes = compute_diff(track, new_tags, new_multi)
        changes = [c for c in all_changes if c.field not in locked_fields]
        locked_changes = [c for c in all_changes if c.field in locked_fields]
        diffs.append(TrackDiff(track, changes, conflicts, warnings, locked_changes))

    return AlbumResult(album=album, release=release, diffs=diffs, warnings=album_warnings)


def apply_changes(
    result: AlbumResult,
    *,
    ignore_locks: bool = False,
    on_written: Callable[[TrackDiff], None] | None = None,
) -> int:
    """Write all tag changes. Returns the number of tracks modified.

    Raises ValueError, before writing any track, for unresolved conflicts or
    for a change to a field the file has locked, unless `ignore_locks` is set.
    `on_written` is called with each diff, in order, right after its track is
    written, or straight away for a diff that needs no write (its locked fields
    are still reported).
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
        if not changes and not diff.keeps_conflicting_isrc:
            if on_written:
                on_written(diff)
            continue
        if changes:
            timestamp = TagChange(
                field="music_tagger_updated",
                old_value=diff.track.tags.get("music_tagger_updated", ""),
                new_value=now,
            )
            write_tags(diff.track, [*changes, timestamp])
        if diff.keeps_conflicting_isrc:
            # Persist the decision so the next `tag` run does not raise it again.
            locked = sorted({*read_locks(diff.track.path), "isrc"})
            write_locks(diff.track.path, locked, timestamp=now)
        count += 1
        if on_written:
            on_written(diff)
    return count


ISRC_POLICIES = ("keep", "musicbrainz")


def resolve_isrc_conflicts(
    result: AlbumResult,
    *,
    policy: str | None = None,
    codes: dict[tuple[int, int], str] | None = None,
) -> None:
    """Set ISRC conflict resolutions in place.

    `policy` "keep" keeps every file code; "musicbrainz" takes MusicBrainz's
    code where it lists exactly one. `codes` maps (disc, track) to a chosen
    code and overrides the policy for that track. Conflicts the policy cannot
    decide stay unresolved.
    """
    if policy is not None and policy not in ISRC_POLICIES:
        raise ValueError(f"unknown ISRC policy {policy!r}; use one of {', '.join(ISRC_POLICIES)}")

    conflicts_by_track = {
        (disc_number(diff.track), parse_position(diff.track.tags.get("tracknumber", ""))): (
            diff,
            conflict,
        )
        for diff in result.diffs
        for conflict in diff.conflicts
        if conflict.field == "isrc"
    }
    codes = codes or {}
    if missing := sorted(key for key in codes if key not in conflicts_by_track):
        tracks = ", ".join(f"{disc}-{track}" for disc, track in missing)
        raise ValueError(f"no ISRC conflict on track(s) {tracks}")

    for key, (diff, conflict) in conflicts_by_track.items():
        if key in codes:
            code = codes[key]
            if code not in [conflict.old_value, *conflict.candidate_values]:
                raise ValueError(
                    f"{code} is neither the file ISRC nor a MusicBrainz ISRC "
                    f"for {diff.track.path.name}"
                )
            conflict.resolution = code
        elif policy == "keep":
            conflict.resolution = conflict.old_value
        elif policy == "musicbrainz" and len(conflict.candidate_values) == 1:
            conflict.resolution = conflict.candidate_values[0]


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


# Fields a user must approve before `tag` overwrites a non-empty value. Other
# fields are IDs or release metadata that MusicBrainz is trusted to correct.
REVIEWED_FIELDS = frozenset({"title", "artist", "album", "albumartist", "date"})

# MusicBrainz style guidelines use typographic punctuation; files from other
# taggers usually use ASCII. Treat the two as the same text.
_STYLE_EQUIVALENTS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
    }
)


# The values whipper writes on a disc ripped with --unknown. Replacing one on an
# unknown rip loses nothing, so the gate treats it as an empty field.
WHIPPER_PLACEHOLDERS = {
    "title": "Unknown Track",
    "artist": "Unknown Artist",
    "albumartist": "Unknown Artist",
    "album": "Unknown Album",
}


def is_unknown_rip(track: TrackTags) -> bool:
    """True for a file as whipper leaves an unknown rip: a disc ID and no release ID."""
    return bool(track.tags.get("musicbrainz_discid")) and not track.tags.get("musicbrainz_albumid")


def is_placeholder(track: TrackTags, field: str, value: str) -> bool:
    """True when value is whipper's placeholder for field, on an unknown-rip file.

    A library file that happens to hold "Unknown Track" is not an unknown rip, so
    its value still needs review.
    """
    return is_unknown_rip(track) and WHIPPER_PLACEHOLDERS.get(field) == value


def is_release_date(value: str, release: MBRelease) -> bool:
    """True when a date tag holds this release's own date, not the original one.

    Whipper writes the release date into DATE on a matched rip, so a reissue got
    its reissue year. DATE is the original date; this value is replaced by the
    release group's first release date when the two differ.
    """
    return (
        bool(value and release.date and release.first_release_date)
        and value == release.date
        and release.first_release_date != release.date
    )


def overwrites_needing_review(result: AlbumResult) -> list[tuple[Path, TagChange]]:
    """Return changes that replace a reviewed field's value with different text.

    Filling an empty field, replacing whipper's placeholder on an unknown rip,
    replacing a reissue's release date in `date` with the original date, and
    changes of case, Unicode punctuation, or whitespace only, do not need review.
    """
    overwrites = []
    for diff in result.diffs:
        for change in diff.effective_changes:
            if change.field not in REVIEWED_FIELDS or not change.old_value:
                continue
            if is_placeholder(diff.track, change.field, change.old_value):
                continue
            if (
                change.field == "date"
                and is_release_date(change.old_value, result.release)
                and change.new_value == result.release.first_release_date
            ):
                continue
            if _style_key(change.old_value) == _style_key(change.new_value):
                continue
            overwrites.append((diff.track.path, change))
    return overwrites


def _style_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).translate(_STYLE_EQUIVALENTS)
    return re.sub(r"\s+", " ", normalized).strip().casefold()
