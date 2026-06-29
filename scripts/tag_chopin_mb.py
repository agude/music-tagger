#!/usr/bin/env python3
"""Match Musopen Chopin FLAC files to MusicBrainz tracks and apply tags.

Fuzzy-matches local files to MB tracks by name similarity and duration,
then writes full MB metadata to matched files. Unmatched files are
listed but not modified.

Usage:
    uv run python tag_chopin_mb.py [--dry-run] [DIRECTORY]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from music_tagger.musicbrainz import MBTrack, MusicBrainzClient
from music_tagger.tags import TagChange, TrackTags, compute_diff, read_album, write_tags
from music_tagger.tagger import _build_new_tags, _set_credit

RELEASE_ID = "ab46c4ff-af73-4ed4-bbfb-665d6358b7ec"

ALBUM_FIELDS = {
    "album", "albumartist", "musicbrainz_albumid", "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid", "releasetype", "releasestatus", "releasedate",
    "originaldate", "label", "catalognumber", "country", "media", "barcode",
    "script", "asin", "compilation",
}


def _trigrams(s: str) -> set[str]:
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def _name_overlap(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_files(
    tracks: list[TrackTags], mb_tracks: list[MBTrack],
) -> tuple[list[tuple[TrackTags, MBTrack]], list[TrackTags], list[MBTrack]]:
    used_files: set[int] = set()
    used_mb: set[int] = set()
    matches: list[tuple[TrackTags, MBTrack, float, float]] = []

    for mb_t in mb_tracks:
        mb_norm = _normalize(mb_t.title)
        mb_dur = mb_t.duration_ms / 1000.0 if mb_t.duration_ms else None

        best: TrackTags | None = None
        best_score: float = 999.0
        best_overlap: float = 0.0
        best_dur_diff: float = 999.0

        for ft in tracks:
            if id(ft) in used_files:
                continue
            fn_norm = _normalize(ft.path.stem)
            overlap = _name_overlap(mb_norm, fn_norm)
            dur_diff = abs(ft.duration_secs - mb_dur) if mb_dur else 999.0

            if overlap > 0.3 and dur_diff < 10:
                score = dur_diff - (overlap * 20)
                if score < best_score:
                    best = ft
                    best_score = score
                    best_overlap = overlap
                    best_dur_diff = dur_diff

        if best is not None:
            matches.append((best, mb_t, best_dur_diff, best_overlap))
            used_files.add(id(best))
            used_mb.add(id(mb_t))

    paired = [(ft, mbt) for ft, mbt, _, _ in matches]
    unmatched_files = [ft for ft in tracks if id(ft) not in used_files]
    unmatched_mb = [mbt for mbt in mb_tracks if id(mbt) not in used_mb]

    return paired, unmatched_files, unmatched_mb


def build_matched_tags(release, mb_track: MBTrack) -> dict[str, str]:
    """Build full tag set for a file matched to an MB track."""
    tags: dict[str, str] = {}

    tags["title"] = mb_track.title
    tags["tracknumber"] = str(mb_track.number)
    tags["album"] = release.title
    tags["albumartist"] = "Various Artists"

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
        tags["musicbrainz_albumartistid"] = release.artist_id
    if release.release_group_id:
        tags["musicbrainz_releasegroupid"] = release.release_group_id
    if release.release_group_type:
        types = [release.release_group_type] + release.secondary_types
        tags["releasetype"] = "; ".join(types)
    if "Compilation" in release.secondary_types:
        tags["compilation"] = "1"
    if release.first_release_date:
        tags["originaldate"] = release.first_release_date
    if release.asin:
        tags["asin"] = release.asin
    if release.script:
        tags["script"] = release.script

    if mb_track.artist_id:
        tags["musicbrainz_artistid"] = mb_track.artist_id
    elif release.artist_id:
        tags["musicbrainz_artistid"] = release.artist_id

    if mb_track.recording_id:
        tags["musicbrainz_recordingid"] = mb_track.recording_id
    if mb_track.track_id:
        tags["musicbrainz_releasetrackid"] = mb_track.track_id
    if mb_track.isrc:
        tags["isrc"] = mb_track.isrc
    if mb_track.work_id:
        tags["musicbrainz_workid"] = mb_track.work_id

    _set_credit(tags, "composer", mb_track.composer, "musicbrainz_composerid", mb_track.composer_id)
    _set_credit(tags, "lyricist", mb_track.lyricist, "musicbrainz_lyricistid", mb_track.lyricist_id)
    _set_credit(tags, "producer", mb_track.producer, "musicbrainz_producerid", mb_track.producer_id)
    _set_credit(tags, "engineer", mb_track.engineer, "musicbrainz_engineerid", mb_track.engineer_id)
    _set_credit(tags, "mixer", mb_track.mixer, "musicbrainz_mixerid", mb_track.mixer_id)
    _set_credit(tags, "conductor", mb_track.conductor, "musicbrainz_conductorid", mb_track.conductor_id)
    _set_credit(tags, "remixer", mb_track.remixer, "musicbrainz_remixerid", mb_track.remixer_id)
    _set_credit(tags, "performer", mb_track.performers, "musicbrainz_performerid", mb_track.performer_ids)

    return tags


def build_album_tags(release) -> dict[str, str]:
    """Build album-level-only tags for unmatched files."""
    tags: dict[str, str] = {}
    tags["album"] = release.title
    tags["albumartist"] = "Various Artists"
    tags["composer"] = "Frédéric Chopin"
    tags["genre"] = "Classical"
    tags["musicbrainz_albumid"] = release.id
    if release.artist_id:
        tags["musicbrainz_albumartistid"] = release.artist_id
    if release.release_group_id:
        tags["musicbrainz_releasegroupid"] = release.release_group_id
    if release.release_group_type:
        types = [release.release_group_type] + release.secondary_types
        tags["releasetype"] = "; ".join(types)
    if "Compilation" in release.secondary_types:
        tags["compilation"] = "1"
    if release.date:
        tags["releasedate"] = release.date
    if release.first_release_date:
        tags["originaldate"] = release.first_release_date
    if release.country:
        tags["country"] = release.country
    if release.label:
        tags["label"] = release.label
    if release.format:
        tags["media"] = release.format
    if release.status:
        tags["releasestatus"] = release.status
    if release.script:
        tags["script"] = release.script
    return tags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?",
                        default=str(Path.home() / "Downloads/Chopin flac/Chopin flac"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    directory = Path(args.directory)
    album = read_album(directory)
    print(f"Read {album.track_count} files from {directory}")

    print(f"Fetching MB release {RELEASE_ID}...")
    mb = MusicBrainzClient()
    try:
        release = mb.fetch_release(RELEASE_ID)
    finally:
        mb.close()
    mb_tracks = release.discs[1]
    print(f"MB release: {release.title} ({len(mb_tracks)} tracks)")

    paired, unmatched_files, unmatched_mb = match_files(album.tracks, mb_tracks)
    print(f"\nMatched: {len(paired)}")
    print(f"Unmatched files: {len(unmatched_files)}")
    print(f"Unmatched MB tracks: {len(unmatched_mb)}")

    if unmatched_mb:
        print("\nMB tracks with no local file:")
        for mbt in unmatched_mb:
            print(f"  {mbt.number:3d}. {mbt.title}")

    # Phase 1: tag matched files with full MB metadata
    print(f"\n--- Phase 1: Tag {len(paired)} matched files ---")
    matched_count = 0
    for ft, mbt in paired:
        new_tags = build_matched_tags(release, mbt)
        changes = compute_diff(ft, new_tags)
        if not changes:
            continue
        matched_count += 1
        if args.dry_run:
            print(f"  {ft.path.name}:")
            for c in changes[:3]:
                old = c.old_value or "(empty)"
                print(f"    {c.field}: {old} → {c.new_value}")
            if len(changes) > 3:
                print(f"    ... and {len(changes) - 3} more")
        else:
            write_tags(ft, changes)

    if args.dry_run:
        print(f"\n{matched_count} files would be updated (dry run)")
    else:
        print(f"\nUpdated {matched_count} matched files")

    # Phase 2: propagate album-level tags to unmatched files
    print(f"\n--- Phase 2: Album tags for {len(unmatched_files)} unmatched files ---")
    album_tags = build_album_tags(release)
    unmatched_count = 0
    for ft in unmatched_files:
        changes = compute_diff(ft, album_tags)
        if not changes:
            continue
        unmatched_count += 1
        if args.dry_run:
            print(f"  {ft.path.name}:")
            for c in changes[:3]:
                old = c.old_value or "(empty)"
                print(f"    {c.field}: {old} → {c.new_value}")
            if len(changes) > 3:
                print(f"    ... and {len(changes) - 3} more")
        else:
            write_tags(ft, changes)

    if args.dry_run:
        print(f"\n{unmatched_count} files would be updated (dry run)")
    else:
        print(f"\nUpdated {unmatched_count} unmatched files")


if __name__ == "__main__":
    main()
