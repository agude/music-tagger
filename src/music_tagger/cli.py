"""CLI entry point for music-tagger."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .musicbrainz import MusicBrainzClient
from .tags import AUDIO_EXTENSIONS
from .tagger import AlbumResult, apply_changes, build_diff, search_candidates


def _is_album_dir(path: Path) -> bool:
    return any(
        f.suffix.lower() in AUDIO_EXTENSIONS for f in path.iterdir() if f.is_file()
    )


def _find_album_dirs(root: Path) -> list[Path]:
    if _is_album_dir(root):
        return [root]
    dirs = []
    for child in sorted(root.rglob("*")):
        if child.is_dir() and _is_album_dir(child):
            dirs.append(child)
    return dirs


def _scan(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger scan",
        description="Scan library and output a JSON list of albums to process.",
    )
    parser.add_argument("path", type=Path, help="Library root directory.")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output JSON file (default: stdout).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Include albums that already have a consistent MB ID.",
    )
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    import json

    from .tags import read_album

    dirs = _find_album_dirs(target)
    entries: list[dict[str, object]] = []

    for album_dir in dirs:
        album = read_album(album_dir)
        if not album.tracks:
            continue

        ids = {t.tags.get("musicbrainz_albumid", "") for t in album.tracks}
        ids.discard("")

        if len(ids) > 1:
            status = "split"
        elif len(ids) == 0:
            status = "no_id"
        elif args.all:
            status = "ok"
        else:
            continue

        entry: dict[str, object] = {
            "path": str(album_dir),
            "artist": album.artist,
            "album": album.album,
            "track_count": album.track_count,
            "status": status,
            "done": False,
        }
        if ids:
            entry["musicbrainz_albumid"] = sorted(ids) if len(ids) > 1 else next(iter(ids))

        entries.append(entry)

    if not entries:
        print("All albums have consistent MusicBrainz IDs.")
        return

    output = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        args.output.write_text(output)
        print(f"Wrote {len(entries)} entries to {args.output}")
    else:
        print(output)


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


def _write_log(log_path: Path, result: AlbumResult) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## {result.album.artist} — {result.album.album}",
        f"",
        f"- **Date:** {timestamp}",
        f"- **Dir:** `{result.album.directory}`",
        f"- **Release:** {result.release.title} [`{result.release.id}`]",
        f"- **Release info:** {result.release.date} / {result.release.country} / {result.release.label}",
        f"",
    ]
    for diff in result.diffs:
        if not diff.changes:
            continue
        lines.append(f"  {diff.track.path.name}:")
        for change in diff.changes:
            old = change.old_value or "(empty)"
            lines.append(f"    {change.field}: {old} → {change.new_value}")
    lines.append("")
    lines.append("---")
    lines.append("")

    with open(log_path, "a") as f:
        f.write("\n".join(lines))


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
    parser.add_argument("--log", type=Path, default=None, help="Append changes to this log file.")
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

    if args.log:
        _write_log(args.log, result)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger",
        description="Fix album tags using MusicBrainz.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", help="Generate a checklist of albums needing attention.")
    sub.add_parser("candidates", help="Show MusicBrainz candidates for an album.")
    sub.add_parser("tag", help="Apply tags from a chosen MusicBrainz release.")

    args, remaining = parser.parse_known_args(argv)

    if args.command == "scan":
        _scan(remaining)
    elif args.command == "candidates":
        _print_candidates(remaining)
    elif args.command == "tag":
        _tag(remaining)
    else:
        parser.print_help()
        sys.exit(1)
