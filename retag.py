#!/usr/bin/env python3
"""Re-run music-tagger tag on already-processed albums.

Reads the musicbrainz_albumid from each album's files and re-applies
tagging with that ID. This fills in any fields that were missing when
the album was first tagged (e.g. composer, producer, performer).

Uses a single MusicBrainzClient to respect the 1 req/sec rate limit.

Usage:
    uv run python retag.py [--dry-run] [--all] [--log FILE] [CHECKLIST]

Defaults:
    CHECKLIST = checklist.json
    LOG       = changes.log (omitted in dry-run mode)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from music_tagger.cli import _print_diff, _write_log
from music_tagger.musicbrainz import MusicBrainzClient
from music_tagger.tags import read_album
from music_tagger.tagger import apply_changes, build_diff


def load_entries(checklist: Path, *, all: bool = False) -> list[dict]:
    entries = json.loads(checklist.read_text())
    if all:
        return entries
    return [e for e in entries if e.get("done")]


def get_album_id(directory: Path) -> str | None:
    album = read_album(directory)
    if not album.tracks:
        return None
    return album.tracks[0].tags.get("musicbrainz_albumid")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checklist", nargs="?", default="checklist.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all", action="store_true", help="Process all entries, not just done ones.")
    parser.add_argument("--log", default="changes.log")
    args = parser.parse_args()

    checklist = Path(args.checklist)
    entries = load_entries(checklist, all=args.all)
    label = "all" if args.all else "completed"
    print(f"Found {len(entries)} {label} albums")

    mb_client = MusicBrainzClient()
    try:
        for i, entry in enumerate(entries, 1):
            d = Path(entry["path"])
            if not d.exists():
                print(f"[{i}/{len(entries)}] SKIP (missing): {d.name}")
                continue

            release_id = get_album_id(d)
            if not release_id:
                print(f"[{i}/{len(entries)}] SKIP (no album ID): {d.name}")
                continue

            try:
                release = mb_client.fetch_release(release_id)
            except Exception as e:
                print(f"[{i}/{len(entries)}] ERROR ({d.name}): {e}")
                continue

            album = read_album(d)
            result = build_diff(album, release)

            if not result.has_changes:
                print(f"[{i}/{len(entries)}] {d.name}: no changes")
                continue

            if args.dry_run:
                print(f"[{i}/{len(entries)}] {d.name}:")
                _print_diff(result)
                continue

            count = apply_changes(result)
            print(f"[{i}/{len(entries)}] {d.name}: updated {count} track(s)")

            if args.log:
                _write_log(Path(args.log), result)
    finally:
        mb_client.close()


if __name__ == "__main__":
    main()
