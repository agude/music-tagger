#!/usr/bin/env python3
"""Repair albums damaged by the build_diff position-matching bug.

For each album directory:
1. Restore tracknumber from filename (ground truth).
2. Re-fetch the MB release from the musicbrainz_albumid tag.
3. Run the fixed build_diff (matches by tracknumber, not position).
4. Show the diff for review.
5. Apply if not --dry-run.

Usage:
    uv run python repair.py <album-dir> [--dry-run]
    uv run python repair.py --all [--dry-run]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from music_tagger.musicbrainz import MusicBrainzClient
from music_tagger.tags import TagChange, read_album, write_tags
from music_tagger.tagger import build_diff


DAMAGED_DIRS = [
    "/mnt/synology/media/music/Bruce Springsteen/18 Tracks (1999)",
    "/mnt/synology/media/music/Bruce Springsteen/The Essential Bruce Springsteen (2003)",
    "/mnt/synology/media/music/Joe Walsh/But Seriously Folks (1978)",
    "/mnt/synology/media/music/Gustav Holst/Holst_ The Planets (2001)",
    "/mnt/synology/media/music/Elvis Costello & The Attractions/Punch the Clock (1983)",
    "/mnt/synology/media/music/Jackson Browne/The Very Best Of Jackson Browne (2004)",
    "/mnt/synology/media/music/Jerry Douglas/Traveler (2012)",
    "/mnt/synology/media/music/John Lennon/Shaved Fish (1974)",
    "/mnt/synology/media/music/Journey/Frontiers (2006)",
    "/mnt/synology/media/music/Kristen Anderson-Lopez, Robert Lopez & Henry Jackman/Winnie the Pooh (2011)",
    "/mnt/synology/media/music/Men at Work/Business as Usual (1981)",
    "/mnt/synology/media/music/Neil Young/Freedom (1989)",
    "/mnt/synology/media/music/Phil Collins/Hits (1998)",
    "/mnt/synology/media/music/Radiokeys/The Shelter Sessions (2020)",
    "/mnt/synology/media/music/Richard Marx/Greatest Hits (1997)",
    "/mnt/synology/media/music/The Byrds/Greatest Hits (2001)",
    "/mnt/synology/media/music/The Byrds/Greatest Hits (2003)",
    "/mnt/synology/media/music/The Byrds/Younger Than Yesterday (1992)",
    "/mnt/synology/media/music/The Pretenders/Learning to Crawl (1984)",
    "/mnt/synology/media/music/Tom Petty and the Heartbreakers/Greatest Hits (1993)",
    "/mnt/synology/media/music/U2/Rattle and Hum (1989)",
    "/mnt/synology/media/music/U2/The Best of 1980-1990 (1998)",
    "/mnt/synology/media/music/U2/Zooropa (1993)",
    # Excluded — tags already correct, MB release merged (301 redirect):
    # "/mnt/synology/media/music/Frank Sinatra/Sinatra & Company (1969)",
    #
    # Excluded — wrong MB release, need manual fix:
    # "/mnt/synology/media/music/Eagles/On the Border (1974)",
    # "/mnt/synology/media/music/Meat Loaf/Couldn't Have Said It Better (2003)",
    # "/mnt/synology/media/music/Pernice Brothers/Yours, Mine and Ours (2003)",
    # "/mnt/synology/media/music/Various Artists/The Top 100 Masterpieces of Classical Music (1995)",
    # "/mnt/synology/media/music/Various Artists/Commentary! The Musical (2008)",
    # "/mnt/synology/media/music/Modest Petrovich Mussorgsky/Mussorgsky_ Pictures at an Exhibition _ Night on the Bare Mountain _ Borodin_ In the Steppes of Central Asia _ Polovtsian Dances (1867)",
]


def extract_tracknumber(filename: str) -> int | None:
    """Extract track number from filename like '08. Life's Been Good.mp3'."""
    m = re.match(r"^0*(\d+)", Path(filename).stem)
    if m:
        return int(m.group(1))
    return None



def repair_album(album_dir: Path, mb: MusicBrainzClient, dry_run: bool) -> None:
    """Repair a single album directory."""
    print(f"\n{'=' * 60}")
    print(f"Album: {album_dir.name}")
    print(f"Path:  {album_dir}")

    if not album_dir.exists():
        print("  SKIP: directory does not exist")
        return

    # Step 1: read album and fix tracknumbers in memory (and on disk if not dry-run)
    album = read_album(album_dir)
    print("\n  Step 1: Restore tracknumbers from filenames")
    tn_fixes = 0
    for track in album.tracks:
        fn_num = extract_tracknumber(track.path.name)
        if fn_num is None:
            continue
        tag_num_str = track.tags.get("tracknumber", "")
        try:
            tag_num = int(tag_num_str.split("/")[0])
        except (ValueError, IndexError):
            tag_num = None
        if tag_num != fn_num:
            print(f"  [tracknumber fix] {track.path.name}: {tag_num_str} -> {fn_num}")
            track.tags["tracknumber"] = str(fn_num)
            if not dry_run:
                change = TagChange(field="tracknumber", old_value=tag_num_str, new_value=str(fn_num))
                write_tags(track, [change])
            tn_fixes += 1
    if tn_fixes == 0:
        print("  (no tracknumber fixes needed)")
    if not album.tracks:
        print("  SKIP: no audio files")
        return

    album_id = None
    for t in album.tracks:
        aid = t.tags.get("musicbrainz_albumid")
        if aid:
            album_id = aid
            break

    if not album_id:
        print("  SKIP: no musicbrainz_albumid tag found")
        return

    print(f"\n  Step 2: Fetch MB release {album_id}")
    release = mb.fetch_release(album_id)
    print(f"  MB: {release.title} ({release.track_count} tracks)")

    # Step 3: run fixed build_diff
    print(f"\n  Step 3: Compute diff ({album.track_count} local files)")
    result = build_diff(album, release)

    if not result.has_changes:
        print("  No changes needed.")
        return

    # Step 4: show diff
    changed = 0
    for diff in result.diffs:
        if not diff.changes:
            continue
        changed += 1
        print(f"\n  {diff.track.path.name}:")
        for c in diff.changes:
            old = c.old_value or "(empty)"
            print(f"    {c.field}: {old} -> {c.new_value}")

    print(f"\n  {changed} files with changes")

    # Step 5: apply if not dry-run
    if not dry_run:
        from music_tagger.tagger import apply_changes
        count = apply_changes(result)
        print(f"  Applied changes to {count} files.")
    else:
        print("  (dry run — no changes written)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album_dir", nargs="?", help="Album directory to repair")
    parser.add_argument("--all", action="store_true", help="Repair all known damaged albums")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.album_dir and not args.all:
        parser.error("Provide an album directory or --all")

    mb = MusicBrainzClient()
    try:
        if args.all:
            for d in DAMAGED_DIRS:
                repair_album(Path(d), mb, args.dry_run)
        else:
            repair_album(Path(args.album_dir), mb, args.dry_run)
    finally:
        mb.close()


if __name__ == "__main__":
    main()
