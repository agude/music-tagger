"""Rating and play-count export and write commands."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from mutagen import MutagenError

from ..navidrome import NavidromeClient, SongRating
from ..stats import SnapshotError, SongStats, StatsSnapshot, read_snapshot, write_snapshot_file
from ..tags import (
    MalformedStatsError,
    UnsupportedFileError,
    write_rating_to_file,
    write_stats_to_file,
)
from .common import _DEFAULT_LIBRARY_ROOT, _add_log_arguments, _BatchLog, _log_path, _output_json

# write-stats lists at most this many missing files, then a count of the rest.
_MAX_LISTED_MISSING = 20


# write-stats processes only songs in this Navidrome library. Podcasts and
# audiobooks live in other libraries, whose paths are not under the music root.
_DEFAULT_LIBRARY = "Music Library"


_ALL_LIBRARIES = "all"


_DEFAULT_RATINGS_PATH = _DEFAULT_LIBRARY_ROOT / "ratings.json"


_DEFAULT_STATS_PATH = _DEFAULT_LIBRARY_ROOT / "stats.json"


def _resolve_song_path(root: Path, relative: str) -> Path | None:
    """Return a song's real path under root, or None when it lies outside root.

    root must already be resolved. Symlinks are followed, so a link that leads
    out of the library is refused too. The root itself is not a song.
    """
    resolved = (root / relative).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved


def _in_library(song: SongStats, library: str) -> bool:
    """True when the song is in the library named, or numbered if all digits."""
    if library.isdigit():
        return song.library_id == int(library)
    return song.library_name == library


def _write_ratings(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger write-ratings",
        description=(
            "Write rating tags to audio files from a ratings JSON. Favorites are not written."
        ),
    )
    parser.add_argument(
        "--from",
        type=Path,
        default=_DEFAULT_RATINGS_PATH,
        dest="from_file",
        help=f"Ratings JSON (default: {_DEFAULT_RATINGS_PATH}).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_LIBRARY_ROOT,
        help=f"Library root to resolve paths (default: {_DEFAULT_LIBRARY_ROOT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)
    log_path = _log_path(args)

    songs = [SongRating.from_dict(s) for s in json.loads(args.from_file.read_text())]

    # Check every path before the first write: one bad path stops the whole run.
    root = args.root.resolve()
    targets: list[tuple[SongRating, Path]] = []
    outside: list[str] = []
    for song in songs:
        resolved = _resolve_song_path(root, song.path)
        if resolved is None:
            outside.append(song.path)
        else:
            targets.append((song, resolved))
    if outside:
        for rel in outside:
            print(f"  outside --root: {rel}", file=sys.stderr)
        print(
            f"Error: {len(outside)} path(s) resolve outside {root}. Nothing written.",
            file=sys.stderr,
        )
        sys.exit(1)

    total_files = 0
    skipped = 0
    unchanged = 0
    failed = 0
    unsupported: list[str] = []
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    log = _BatchLog(
        None if args.dry_run else log_path,
        ["## Rating tags", "", f"- **Date:** {timestamp}", ""],
    )

    try:
        for song, path in targets:
            try:
                if not path.exists():
                    skipped += 1
                    continue
                changes = write_rating_to_file(path, song.rating, dry_run=args.dry_run)
            except UnsupportedFileError as exc:
                unsupported.append(song.path)
                print(f"  unsupported: {song.path}: {exc}", file=sys.stderr)
                continue
            except (MalformedStatsError, MutagenError, OSError, ValueError) as exc:
                failed += 1
                print(f"  failed: {song.path}: {exc}", file=sys.stderr)
                continue
            if not changes:
                unchanged += 1
                continue
            total_files += 1
            if args.dry_run:
                print(f"  {song.path}:")
                for c in changes:
                    old = c.old_value or "(empty)"
                    print(f"    {c.field}: {old} → {c.new_value}")
            else:
                log.add(f"  {song.path}: {', '.join(c.field for c in changes)}")
    finally:
        log.close()

    if skipped:
        print(f"Skipped {skipped} file(s) (not found).", file=sys.stderr)

    if args.dry_run:
        print(
            f"\n(dry run — {len(songs)} songs, {total_files} would change, "
            f"{unchanged} unchanged, {skipped} missing, {len(unsupported)} unsupported, "
            f"{failed} failed)"
        )
    else:
        print(
            f"Wrote rating tags to {total_files} file(s); {unchanged} unchanged, "
            f"{skipped} missing, {len(unsupported)} unsupported, {failed} failed."
        )
        if total_files == 0 and skipped > 0:
            sys.exit(1)

    if unsupported or failed:
        sys.exit(1)


def _write_stats(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger write-stats",
        description=(
            "Write ratings and play counts from a statistics snapshot to audio files. "
            "Favorites are not written."
        ),
    )
    parser.add_argument(
        "--from",
        type=Path,
        default=_DEFAULT_STATS_PATH,
        dest="from_file",
        help=f"Statistics snapshot (default: {_DEFAULT_STATS_PATH}).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_LIBRARY_ROOT,
        help=f"Library root to resolve paths (default: {_DEFAULT_LIBRARY_ROOT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change, and the rewrite cost, without writing.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the rating of files the snapshot marks unrated (0). Without it they are kept.",
    )
    parser.add_argument(
        "--allow-decrease",
        action="store_true",
        help="Write play counts lower than the file's count. Without it they are kept.",
    )
    parser.add_argument(
        "--library",
        default=_DEFAULT_LIBRARY,
        help=(
            "Navidrome library to write, by name or numeric id; songs in other libraries "
            f"are skipped (default: {_DEFAULT_LIBRARY!r}). 'all' writes every library."
        ),
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)
    log_path = _log_path(args)

    try:
        snapshot = read_snapshot(args.from_file)
    except SnapshotError as exc:
        print(f"Error: {exc}. Nothing written.", file=sys.stderr)
        sys.exit(1)

    # Filter first, so songs outside the library are never checked against --root.
    # Check every path before the first write: one bad path stops the whole run.
    root = args.root.resolve()
    targets: list[tuple[SongStats, Path]] = []
    outside: list[str] = []
    other_library = unlabelled = 0
    for song in snapshot.songs:
        if args.library != _ALL_LIBRARIES:
            if song.library_id is None and song.library_name is None:
                unlabelled += 1
            elif not _in_library(song, args.library):
                other_library += 1
                continue
        resolved = _resolve_song_path(root, song.path)
        if resolved is None:
            outside.append(song.path)
        else:
            targets.append((song, resolved))
    if other_library and not targets and not outside:
        names = sorted({str(s.library_name or s.library_id) for s in snapshot.songs})
        print(
            f"Error: no song is in library {args.library!r}. "
            f"Libraries in the snapshot: {', '.join(names)}. Nothing written.",
            file=sys.stderr,
        )
        sys.exit(1)
    if unlabelled:
        print(
            "warning: snapshot predates library fields; "
            f"--library could not be applied to {unlabelled} song(s).",
            file=sys.stderr,
        )
    if outside:
        for rel in outside:
            print(f"  outside --root: {rel}", file=sys.stderr)
        print(
            f"Error: {len(outside)} path(s) resolve outside {root}. Nothing written.",
            file=sys.stderr,
        )
        sys.exit(1)

    changed = unchanged = clear_withheld = decrease_withheld = 0
    missing = unsupported = failed = 0
    rewrite_files = rewrite_bytes = 0
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    log = _BatchLog(
        None if args.dry_run else log_path,
        [
            "## Statistics",
            "",
            f"- **Date:** {timestamp}",
            f"- **Snapshot:** {args.from_file}",
            "",
        ],
    )

    try:
        for song, path in targets:
            try:
                if not path.exists():
                    missing += 1
                    if missing <= _MAX_LISTED_MISSING:
                        print(f"  missing: {song.path}", file=sys.stderr)
                    continue
                size = path.stat().st_size
                plan = write_stats_to_file(
                    path,
                    rating=song.rating,
                    play_count=song.play_count,
                    clear=args.clear,
                    allow_decrease=args.allow_decrease,
                    dry_run=args.dry_run,
                )
            except UnsupportedFileError as exc:
                unsupported += 1
                print(f"  unsupported: {song.path}: {exc}", file=sys.stderr)
                continue
            except (MalformedStatsError, MutagenError, OSError, ValueError) as exc:
                failed += 1
                print(f"  failed: {song.path}: {exc}", file=sys.stderr)
                continue

            if plan.clear_withheld:
                clear_withheld += 1
                print(f"  clear withheld: {song.path} (snapshot rating 0; pass --clear)")
            if plan.decrease_withheld:
                decrease_withheld += 1
                print(
                    f"  decrease withheld: {song.path} "
                    "(snapshot count is lower; pass --allow-decrease)"
                )
            if not plan.changes:
                unchanged += 1
                continue

            changed += 1
            rewrite_files += 1
            rewrite_bytes += size
            if args.dry_run:
                print(f"  {song.path}:")
                for c in plan.changes:
                    print(f"    {c.field}: {c.old_value or '(empty)'} → {c.new_value}")
            else:
                detail = "; ".join(
                    f"{c.field} {c.old_value or '(empty)'} → {c.new_value}" for c in plan.changes
                )
                log.add(f"  {song.path}: {detail}")
    finally:
        log.close()

    if missing > _MAX_LISTED_MISSING:
        print(f"  … and {missing - _MAX_LISTED_MISSING} more missing", file=sys.stderr)

    verb = "would change" if args.dry_run else "changed"
    rewrite_verb = "would rewrite" if args.dry_run else "rewrote"
    print(
        f"{changed} {verb}, {unchanged} unchanged, {clear_withheld} clear withheld, "
        f"{decrease_withheld} decrease withheld, {other_library} other library, "
        f"{missing} missing, {unsupported} unsupported, {failed} failed."
    )
    print(f"{rewrite_verb} {rewrite_files} file(s), {rewrite_bytes} bytes.")

    if missing or unsupported or failed:
        sys.exit(1)


def _nd_ratings(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger nd ratings",
        description="Export rated and starred songs from Navidrome as JSON.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=_DEFAULT_RATINGS_PATH,
        help=f"Output JSON file (default: {_DEFAULT_RATINGS_PATH}).",
    )
    args = parser.parse_args(argv)

    if not args.out.parent.is_dir():
        print(f"Error: output directory does not exist: {args.out.parent}", file=sys.stderr)
        if args.out == _DEFAULT_RATINGS_PATH:
            print("Check that the music library is mounted.", file=sys.stderr)
        sys.exit(1)

    client = NavidromeClient()
    try:
        songs = client.get_all_ratings()
    finally:
        client.close()

    data = [s.to_dict() for s in songs]

    rated = sum(1 for s in songs if s.rating)
    starred = sum(1 for s in songs if s.starred)
    lines = [
        f"{len(songs)} song(s): {rated} rated, {starred} starred",
    ]

    _output_json(data, args.out, "\n".join(lines))


def _nd_stats(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger nd stats",
        description=(
            "Export a complete statistics snapshot (ratings, play counts, stars) "
            "from Navidrome as JSON. The previous snapshot is kept as <name>.previous."
        ),
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=_DEFAULT_STATS_PATH,
        help=f"Output snapshot file (default: {_DEFAULT_STATS_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Replace the current snapshot even if it loses more than 10%% of its songs, "
            "rated songs, or starred songs."
        ),
    )
    args = parser.parse_args(argv)

    if not args.out.parent.is_dir():
        print(f"Error: output directory does not exist: {args.out.parent}", file=sys.stderr)
        if args.out == _DEFAULT_STATS_PATH:
            print("Check that the music library is mounted.", file=sys.stderr)
        sys.exit(1)

    client = NavidromeClient()
    try:
        songs = client.get_all_stats()
        user = client.user
    except (RuntimeError, httpx.HTTPError, SnapshotError) as exc:
        print(f"Error: export failed; no snapshot written: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()

    snapshot = StatsSnapshot(
        source_user=user,
        exported_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        songs=tuple(songs),
    )
    try:
        write_snapshot_file(snapshot, args.out, force=args.force)
    except SnapshotError as exc:
        print(f"Error: no file written: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: could not write {args.out}: {exc}", file=sys.stderr)
        sys.exit(1)

    rated = sum(1 for s in songs if s.rating)
    played = sum(1 for s in songs if s.play_count)
    starred = sum(1 for s in songs if s.starred)
    print(
        f"Exported {len(songs)} song(s): {rated} rated, {played} played, "
        f"{starred} starred, to {args.out}"
    )
