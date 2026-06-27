"""CLI entry point for music-tagger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .musicbrainz import MusicBrainzClient
from .tags import AUDIO_EXTENSIONS
from .tagger import AlbumResult, apply_changes, build_diff, search_candidates


def _is_album_dir(path: Path) -> bool:
    return any(
        f.suffix.lower() in AUDIO_EXTENSIONS for f in path.iterdir() if f.is_file()
    )


def _print_candidates(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger candidates",
        description="Show album file data and MusicBrainz candidate releases.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    mb_client = MusicBrainzClient()
    try:
        album, candidates = search_candidates(target, mb_client)
    finally:
        mb_client.close()

    print(f"Album:  {album.artist} — {album.album}")
    print(f"Dir:    {album.directory}")
    print(f"Tracks: {album.track_count}")
    print()

    print("Files (durations from audio stream):")
    for i, track in enumerate(album.tracks, 1):
        dur = f"{track.duration_secs:.1f}s"
        title = track.tags.get("title", "")
        print(f"  {i:2d}. {track.path.name}  ({dur})  [{title}]")
    print()

    existing = album.tracks[0].tags if album.tracks else {}
    tag_lines = []
    for field in (
        "barcode", "catalognumber", "country", "media",
        "date", "label", "musicbrainz_albumid",
    ):
        val = existing.get(field, "")
        if val:
            tag_lines.append(f"  {field}: {val}")
    if tag_lines:
        print("Existing tags (track 1):")
        for line in tag_lines:
            print(line)
        print()

    print(f"MusicBrainz candidates ({len(candidates)}):")
    print()
    for c in candidates:
        print(f"  [{c.id}]")
        print(f"  {c.title} ({c.date}) — {c.country} — {c.label}")
        if c.catalognum:
            print(f"  Catalog#: {c.catalognum}  Barcode: {c.barcode}")
        print(f"  Format: {c.format}  Tracks: {c.track_count}")
        for disc_num in sorted(c.discs):
            tracks = c.discs[disc_num]
            if len(c.discs) > 1:
                print(f"  Disc {disc_num}:")
            for t in tracks:
                dur = f"{t.duration_secs:.1f}s" if t.duration_secs is not None else "?"
                print(f"    {t.number:2d}. {t.title}  ({dur})")
        print()


def _print_diff(result: AlbumResult) -> None:
    print(f"Release: {result.release.title} [{result.release.id}]")
    print(f"         {result.release.date} / {result.release.country} / {result.release.label}")
    print()

    if not result.has_changes:
        print("No tag changes needed.")
        return

    for diff in result.diffs:
        if not diff.changes:
            continue
        print(f"  {diff.track.path.name}:")
        for change in diff.changes:
            old = change.old_value or "(empty)"
            print(f"    {change.field}: {old} → {change.new_value}")
    print()


def _tag(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger tag",
        description="Apply tags from a chosen MusicBrainz release.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument("--release-id", required=True, help="MusicBrainz release UUID.")
    parser.add_argument("--dry-run", action="store_true", help="Show diff without writing.")
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    from .tags import read_album

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    mb_client = MusicBrainzClient()
    try:
        release = mb_client.fetch_release(args.release_id)
    finally:
        mb_client.close()

    result = build_diff(album, release)
    _print_diff(result)

    if not result.has_changes:
        return

    if args.dry_run:
        print("(dry run — skipping write)")
        return

    count = apply_changes(result)
    print(f"Updated {count} track(s).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger",
        description="Fix album tags using MusicBrainz.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("candidates", help="Show MusicBrainz candidates for an album.")
    sub.add_parser("tag", help="Apply tags from a chosen MusicBrainz release.")

    args, remaining = parser.parse_known_args(argv)

    if args.command == "candidates":
        _print_candidates(remaining)
    elif args.command == "tag":
        _tag(remaining)
    else:
        parser.print_help()
        sys.exit(1)
