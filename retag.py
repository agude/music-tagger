#!/usr/bin/env python3
"""Re-run music-tagger tag on already-processed albums.

Reads the musicbrainz_albumid from each album's files and re-applies
tagging with that ID. This fills in any fields that were missing when
the album was first tagged (e.g. composer, producer, performer).

Usage:
    uv run python retag.py [--dry-run] [--log FILE] [CHECKLIST]

Defaults:
    CHECKLIST = checklist.json
    LOG       = changes.log (omitted in dry-run mode)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from music_tagger.tags import read_album


def load_done_entries(checklist: Path) -> list[dict]:
    entries = json.loads(checklist.read_text())
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
    parser.add_argument("--log", default="changes.log")
    args = parser.parse_args()

    checklist = Path(args.checklist)
    entries = load_done_entries(checklist)
    print(f"Found {len(entries)} completed albums")

    for i, entry in enumerate(entries, 1):
        d = Path(entry["path"])
        if not d.exists():
            print(f"[{i}/{len(entries)}] SKIP (missing): {d.name}")
            continue

        release_id = get_album_id(d)
        if not release_id:
            print(f"[{i}/{len(entries)}] SKIP (no album ID): {d.name}")
            continue

        cmd = [
            "uv", "run", "music-tagger", "tag",
            str(d), "--release-id", release_id,
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        else:
            cmd.extend(["--log", args.log])

        result = subprocess.run(cmd, capture_output=True, text=True)
        last_line = (result.stdout.strip().splitlines() or [""])[-1]
        print(f"[{i}/{len(entries)}] {d.name}: {last_line}")

        if result.returncode != 0:
            stderr_tail = (result.stderr.strip().splitlines() or [""])[-1]
            print(f"  ERROR: {stderr_tail}")


if __name__ == "__main__":
    main()
