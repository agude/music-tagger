"""CLI entry point for music-tagger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .musicbrainz import MusicBrainzClient
from .tags import AUDIO_EXTENSIONS
from .tagger import AlbumResult, apply_changes, process_album


def _is_album_dir(path: Path) -> bool:
    return any(f.suffix.lower() in AUDIO_EXTENSIONS for f in path.iterdir() if f.is_file())


def _find_album_dirs(root: Path) -> list[Path]:
    if _is_album_dir(root):
        return [root]
    dirs = []
    for child in sorted(root.rglob("*")):
        if child.is_dir() and _is_album_dir(child):
            dirs.append(child)
    return dirs


def _print_result(result: AlbumResult) -> None:
    print(f"\n{'=' * 72}")
    print(f"Album:  {result.album.artist} — {result.album.album}")
    print(f"Dir:    {result.album.directory}")
    print(f"Match:  {result.release.title} [{result.release.id}]")
    print(f"        {result.release.date} / {result.release.country} / {result.release.label}")
    print(f"        Confidence: {result.match.confidence}")
    print(f"Reason: {result.match.reasoning}")
    print()

    if not result.has_changes:
        print("  No tag changes needed.")
        return

    for diff in result.diffs:
        if not diff.changes:
            continue
        print(f"  {diff.track.path.name}:")
        for change in diff.changes:
            old = change.old_value or "(empty)"
            print(f"    {change.field}: {old} → {change.new_value}")
    print()


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger",
        description="Fix album tags using MusicBrainz + LLM matching.",
    )
    parser.add_argument("path", type=Path, help="Album directory or library root.")
    parser.add_argument("--dry-run", action="store_true", help="Show diffs without writing.")
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    dirs = _find_album_dirs(target)
    if not dirs:
        print(f"No album directories found under {target}.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(dirs)} album(s) to process.")

    mb_client = MusicBrainzClient()
    try:
        for album_dir in dirs:
            try:
                result = process_album(album_dir, mb_client)
            except Exception as e:
                print(f"\nError processing {album_dir}: {e}", file=sys.stderr)
                continue

            _print_result(result)

            if not result.has_changes:
                continue

            if args.dry_run:
                print("  (dry run — skipping write)")
                continue

            if _confirm("  Apply these changes?"):
                count = apply_changes(result)
                print(f"  Updated {count} track(s).")
            else:
                print("  Skipped.")
    finally:
        mb_client.close()
