"""Orchestration: search MusicBrainz candidates, compute diffs, apply tags."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .musicbrainz import MBRelease, MusicBrainzClient
from .tags import AlbumTags, TagChange, TrackTags, compute_diff, read_album, write_tags


@dataclass
class TrackDiff:
    track: TrackTags
    changes: list[TagChange]


@dataclass
class AlbumResult:
    album: AlbumTags
    release: MBRelease
    diffs: list[TrackDiff] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(d.changes for d in self.diffs)


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

    tags["musicbrainz_albumid"] = release.id
    if release.artist_id:
        tags["musicbrainz_artistid"] = release.artist_id
        tags["musicbrainz_albumartistid"] = release.artist_id
    if release.release_group_id:
        tags["musicbrainz_releasegroupid"] = release.release_group_id

    return tags


def search_candidates(
    directory: Path, mb_client: MusicBrainzClient
) -> tuple[AlbumTags, list[MBRelease]]:
    """Read album tags and fetch detailed MusicBrainz candidates."""
    album = read_album(directory)
    if not album.tracks:
        raise ValueError(f"No audio files found in {directory}")

    candidates = mb_client.search_releases(album.artist, album.album)
    if not candidates:
        raise ValueError(
            f"No MusicBrainz results for '{album.artist}' - '{album.album}'"
        )

    detailed: list[MBRelease] = []
    for c in candidates:
        detailed.append(mb_client.fetch_release(c.id))

    return album, detailed


def build_diff(album: AlbumTags, release: MBRelease) -> AlbumResult:
    """Compute the tag diff between current tags and a chosen release."""
    # Group tracks by disc number, tracking each track's position within its disc
    disc_positions: dict[int, int] = {}

    diffs: list[TrackDiff] = []
    for track in album.tracks:
        disc_num = _parse_disc_number(track)
        track_index = disc_positions.get(disc_num, 0)
        disc_positions[disc_num] = track_index + 1

        new_tags = _build_new_tags(release, track, track_index, disc_num)
        changes = compute_diff(track, new_tags)
        diffs.append(TrackDiff(track=track, changes=changes))

    return AlbumResult(album=album, release=release, diffs=diffs)


def apply_changes(result: AlbumResult) -> int:
    """Write all tag changes. Returns the number of tracks modified."""
    count = 0
    for diff in result.diffs:
        if diff.changes:
            write_tags(diff.track, diff.changes)
            count += 1
    return count
