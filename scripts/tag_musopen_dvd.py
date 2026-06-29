#!/usr/bin/env python3
"""Tag Musopen Kickstarter Recordings against 'The Musopen DVD' MB release.

Matches local files to MB tracks by name similarity + duration, then
writes full MB metadata. Two manual overrides handle duration mismatches.

Usage:
    uv run python tag_musopen_dvd.py [--dry-run]
"""

from __future__ import annotations

import re
from pathlib import Path

from music_tagger.musicbrainz import MBTrack, MusicBrainzClient
from music_tagger.tags import compute_diff, read_album, write_tags
from tag_chopin_mb import build_matched_tags

RELEASE_ID = "954bd739-75ac-410e-adc7-1bf80b0157cb"
ALBUM_DIR = Path("/mnt/synology/media/music/Various Artists/Musopen Kickstarter Recordings (2012)")

MANUAL_OVERRIDES: dict[str, int] = {
    "04. Sonata in A Minor, D. 959 - IV. Rondo. Allegretto.flac": 91,
    "04. String Quartet No. 6 in B Flat Major, Op. 18, No. 6 - IV. (Adagio) La Malinconia.flac": 108,
}


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _trigrams(s: str) -> set[str]:
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def _name_overlap(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def match_files(tracks, mb_tracks):
    mb_by_num = {t.number: t for t in mb_tracks}
    used_files: set[int] = set()
    matches = []

    # Phase 1: manual overrides
    for ft in tracks:
        if ft.path.name in MANUAL_OVERRIDES:
            mb_num = MANUAL_OVERRIDES[ft.path.name]
            mbt = mb_by_num[mb_num]
            matches.append((ft, mbt))
            used_files.add(id(ft))

    used_mb = {id(m[1]) for m in matches}

    # Phase 2: name + duration matching
    for mbt in mb_tracks:
        if id(mbt) in used_mb:
            continue
        mb_dur = mbt.duration_ms / 1000.0 if mbt.duration_ms else None
        mb_norm = _normalize(mbt.title)

        best = None
        best_score = -999.0

        for ft in tracks:
            if id(ft) in used_files:
                continue
            fn_norm = re.sub(r"^0*\d+", "", _normalize(ft.path.stem))
            overlap = _name_overlap(mb_norm, fn_norm)
            dur_diff = abs(ft.duration_secs - mb_dur) if mb_dur else 999.0

            if overlap > 0.25 and dur_diff < 5:
                score = overlap * 20 - dur_diff
                if score > best_score:
                    best = ft
                    best_score = score

        if best is not None:
            matches.append((best, mbt))
            used_files.add(id(best))
            used_mb.add(id(mbt))

    unmatched_files = [ft for ft in tracks if id(ft) not in used_files]
    unmatched_mb = [mbt for mbt in mb_tracks if id(mbt) not in used_mb]
    return matches, unmatched_files, unmatched_mb


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    album = read_album(ALBUM_DIR)
    print(f"Read {album.track_count} files from {ALBUM_DIR}")

    print(f"Fetching MB release {RELEASE_ID}...")
    mb = MusicBrainzClient()
    try:
        release = mb.fetch_release(RELEASE_ID)
    finally:
        mb.close()
    mb_tracks = release.discs[1]
    print(f"MB release: {release.title} ({len(mb_tracks)} tracks)")

    matches, unmatched_files, unmatched_mb = match_files(album.tracks, mb_tracks)
    print(f"\nMatched: {len(matches)}")

    if unmatched_mb:
        print(f"Unmatched MB tracks: {len(unmatched_mb)}")
        for mbt in unmatched_mb:
            print(f"  {mbt.number}. {mbt.title}")
    if unmatched_files:
        print(f"Unmatched files: {len(unmatched_files)}")
        for ft in unmatched_files:
            print(f"  {ft.path.name}")

    if unmatched_mb or unmatched_files:
        print("\nABORT: not all tracks matched.")
        return

    updated = 0
    for ft, mbt in matches:
        new_tags = build_matched_tags(release, mbt)
        changes = compute_diff(ft, new_tags)
        if not changes:
            continue
        updated += 1
        if args.dry_run:
            print(f"\n  {ft.path.name} -> MB #{mbt.number} ({mbt.title}):")
            for c in changes:
                old = c.old_value or "(empty)"
                print(f"    {c.field}: {old} -> {c.new_value}")
        else:
            write_tags(ft, changes)
            print(f"  Tagged: {ft.path.name} -> MB #{mbt.number} ({mbt.title}) [{len(changes)} fields]")

    if args.dry_run:
        print(f"\n{updated} files would be updated (dry run)")
    else:
        print(f"\nUpdated {updated} files")


if __name__ == "__main__":
    main()
