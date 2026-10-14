"""CLI entry point for music-tagger."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mutagen import MutagenError

from .checklist import Checklist, ChecklistEntry
from .coverart import (
    ArtResult,
    EmbeddedCover,
    UnsupportedImageError,
    embeddable_cover,
    fetch_cover_art,
    find_cover,
    image_dimensions,
    use_local_cover,
)
from .discid import parse_rip_dir
from .musicbrainz import MBRelease, MusicBrainzClient
from .navidrome import NavidromeClient, SongRating
from .placement import FileMapping, PlacementPlan, compute_placement, copy_files
from .tagger import (
    ISRC_POLICIES,
    AlbumResult,
    CandidateMatch,
    TrackDiff,
    _parse_disc_number,
    _parse_track_number,
    apply_changes,
    build_diff,
    overwrites_needing_review,
    resolve_isrc_conflicts,
    score_candidates,
    search_candidates,
)
from .tags import (
    AUDIO_EXTENSIONS,
    LOCKABLE_FIELDS,
    AlbumTags,
    TrackTags,
    embed_cover_art,
    read_album,
    write_locks,
    write_rating_to_file,
)

_DEFAULT_LIBRARY_ROOT = Path("/mnt/synology/media/music")

_LOG_ENV_VAR = "MUSIC_TAGGER_LOG"


def _default_log_path() -> Path:
    override = os.environ.get(_LOG_ENV_VAR)
    return Path(override) if override else Path.home() / "Projects/music-tagger/changes.log"


def _add_log_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--log",
        type=Path,
        default=None,
        help=f"Append changes to this log file (default: ${_LOG_ENV_VAR}, "
        "else ~/Projects/music-tagger/changes.log).",
    )
    group.add_argument("--no-log", action="store_true", help="Do not write the change log.")


def _log_path(args: argparse.Namespace) -> Path | None:
    if args.no_log:
        return None
    log_path: Path = args.log or _default_log_path()
    return log_path


def _default_checklist_path() -> Path:
    override = os.environ.get("MUSIC_TAGGER_CHECKLIST")
    return Path(override) if override else Path.home() / "Projects/music-tagger/checklist.md"


# Exit codes for `tag`, so the operator can branch without parsing output.
EXIT_REVIEW = 3  # An overwrite needs --approve-overwrites.
EXIT_BLOCKED = 4  # Unresolved conflicts; nothing can be written.
_DEFAULT_RATINGS_PATH = _DEFAULT_LIBRARY_ROOT / "ratings.json"


def _output_json(data: object, out: Path | None, digest: str) -> None:
    """Write JSON to file (printing digest to stdout) or JSON to stdout."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if out:
        out.write_text(text)
        print(digest)
    else:
        sys.stdout.write(text)


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


def _read(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger read",
        description="Read album tags and durations as JSON evidence.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout). Without this, JSON goes to stdout.",
    )
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    data = album.to_dict()

    fmt = album.tracks[0].format.upper()
    lines = [f"{album.artist} — {album.album} ({album.track_count} tracks, {fmt})"]
    for track in album.tracks:
        dur = f"{track.duration_secs:.1f}s"
        title = track.tags.get("title", track.path.name)
        num = track.tags.get("tracknumber", "?")
        lines.append(f"  {num:>2s}. {title}  ({dur})")
    _output_json(data, args.out, "\n".join(lines))


def _toc(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger toc",
        description="Parse rip artifacts (CUE sheet) and extract TOC / disc ID.",
    )
    parser.add_argument("path", type=Path, help="Rip directory containing .cue file.")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    toc = parse_rip_dir(target)
    data = toc.to_dict()

    lines = [f"TOC: {toc.last_track} tracks"]
    if toc.disc_id:
        lines.append(f"Disc ID: {toc.disc_id}")
    else:
        lines.append("Disc ID: (unavailable — leadout offset unknown)")
    lines.append(f"TOC string: {toc.to_toc_string()}")
    _output_json(data, args.out, "\n".join(lines))


def _mb_search(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb search",
        description="Search MusicBrainz for candidate releases.",
    )
    parser.add_argument("--artist", required=True, help="Artist name.")
    parser.add_argument("--album", required=True, help="Album title.")
    parser.add_argument("--format", default="CD", help="Media format filter (default: CD).")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10).")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    mb_client = MusicBrainzClient()
    try:
        results = mb_client.search_releases(
            args.artist,
            args.album,
            format=args.format,
            limit=args.limit,
        )
    finally:
        mb_client.close()

    data = [r.to_dict() for r in results]

    lines = [f'{len(results)} candidates for "{args.artist}" — "{args.album}":']
    for r in results:
        info = (
            f"  [{r.id}]  {r.title} ({r.date})"
            f" {r.country} / {r.label} / {r.format} / {r.track_count}t"
        )
        lines.append(info)
    _output_json(data, args.out, "\n".join(lines))


def _mb_release(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb release",
        description="Fetch full release detail from MusicBrainz.",
    )
    parser.add_argument("release_id", help="MusicBrainz release UUID.")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    mb_client = MusicBrainzClient()
    try:
        release = mb_client.fetch_release(args.release_id)
    finally:
        mb_client.close()

    data = release.to_dict()

    disc_info = f"{len(release.discs)} disc(s)" if release.discs else "no disc info"
    lines = [
        f"{release.title} [{release.id}]",
        f"{release.date} / {release.country} / {release.label}"
        f" / {release.catalognum or '—'} / {release.format}",
        f"{release.track_count} tracks, {disc_info}",
    ]
    _output_json(data, args.out, "\n".join(lines))


def _mb_discid(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb discid",
        description="Look up releases by disc ID or TOC offsets.",
    )
    parser.add_argument("discid", nargs="?", default=None, help="MusicBrainz disc ID.")
    parser.add_argument(
        "--toc", default=None, help="TOC string for fuzzy lookup (from `toc` command)."
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    if not args.discid and not args.toc:
        print("Error: provide a disc ID or --toc string.", file=sys.stderr)
        sys.exit(1)

    mb_client = MusicBrainzClient()
    try:
        if args.discid:
            results = mb_client.lookup_discid(args.discid)
        else:
            results = mb_client.lookup_toc(args.toc)
    finally:
        mb_client.close()

    data = [r.to_dict() for r in results]

    method = f"disc ID {args.discid}" if args.discid else "TOC fuzzy match"
    lines = [f"{len(results)} releases via {method}:"]
    for r in results:
        lines.append(
            f"  [{r.id}]  {r.title} ({r.date})"
            f" {r.country} / {r.label} / {r.format} / {r.track_count}t"
        )
    _output_json(data, args.out, "\n".join(lines))


def _mb(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = []

    mb_commands = {
        "search": ("Search for candidate releases.", _mb_search),
        "release": ("Fetch full release detail.", _mb_release),
        "discid": ("Look up by disc ID or TOC.", _mb_discid),
    }

    if not argv or argv[0] in ("-h", "--help"):
        print("usage: music-tagger mb <command> [options]\n")
        print("MusicBrainz query commands.\n")
        print("commands:")
        for name, (helptext, _) in mb_commands.items():
            print(f"  {name:<10}  {helptext}")
        sys.exit(0 if argv else 1)

    command = argv[0]
    remaining = argv[1:]

    if command in mb_commands:
        _, handler = mb_commands[command]
        handler(remaining)
    else:
        print(f"music-tagger mb: unknown command '{command}'", file=sys.stderr)
        sys.exit(1)


def _match(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger match",
        description="Score candidates by duration match against local evidence.",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="Evidence JSON from `read`.",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        required=True,
        help="Candidates JSON from `mb search`.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    album = AlbumTags.from_dict(json.loads(args.evidence.read_text()))
    candidates = [MBRelease.from_dict(c) for c in json.loads(args.candidates.read_text())]

    matches = score_candidates(album, candidates)
    data = [m.to_dict() for m in matches]

    lines = [f"{len(matches)} candidates scored:"]
    for m in matches:
        r = m.release
        s = m.stats
        count_ok = "=" if s.track_count_match else "!"
        lines.append(
            f"  [{r.id}]  {r.title}  {s.tracks_within_2s}/{s.tracks_compared} within 2s  "
            f"max_dev={s.max_deviation_secs:.1f}s  count{count_ok}"
        )
    _output_json(data, args.out, "\n".join(lines))


def _diff(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger diff",
        description="Compute tag diff between local evidence and a MusicBrainz release.",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="Evidence JSON from `read`.",
    )
    parser.add_argument(
        "--release",
        type=Path,
        required=True,
        help="Release JSON from `mb release`.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    album = AlbumTags.from_dict(json.loads(args.evidence.read_text()))
    release = MBRelease.from_dict(json.loads(args.release.read_text()))

    result = build_diff(album, release)
    data = result.to_dict()
    _print_tag_issues(result)

    changed = [d for d in result.diffs if d.changes]
    lines = [f"Release: {release.title} [{release.id}]"]
    lines.append(f"{len(result.diffs)} tracks, {len(changed)} with changes:")
    for d in changed:
        fields = ", ".join(c.field for c in d.changes)
        lines.append(f"  {d.track.path.name}: {fields}")
    for diff in result.diffs:
        if diff.conflicts:
            lines.append(f"  {diff.track.path.name}: {len(diff.conflicts)} unresolved conflict(s)")
        if diff.locked_changes:
            lines.append(
                f"  {diff.track.path.name}: {len(diff.locked_changes)} locked field(s) kept"
            )
        for warning in diff.warnings:
            lines.append(f"  {diff.track.path.name}: {warning}")
    if not changed:
        lines.append("  (no changes)")
    _output_json(data, args.out, "\n".join(lines))


def _write_tags(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger write-tags",
        description="Apply tag changes from a diff JSON.",
    )
    parser.add_argument(
        "--diff",
        type=Path,
        required=True,
        dest="diff_file",
        help="Diff JSON from `diff` (possibly LLM-edited).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    result = AlbumResult.from_dict(json.loads(args.diff_file.read_text()))
    _print_tag_issues(result)

    if args.dry_run:
        for d in result.diffs:
            change_lines = _format_change_lines(d)
            if change_lines:
                print(f"  {d.track.path.name}:")
                print("\n".join(change_lines))
        print("\n(dry run — skipping write)")
        return

    try:
        count = apply_changes(result)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    if count:
        print(f"Updated {count} track(s).")
    else:
        print("No tag changes to apply.")

    log_path = _log_path(args)
    if log_path and (count or any(diff.conflicts for diff in result.diffs)):
        _write_log_entry(log_path, result)


def _write_log_entry(log_path: Path, result: AlbumResult) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## {result.release.title} [{result.release.id}]",
        "",
        f"- **Date:** {timestamp}",
        "",
    ]
    for diff in result.diffs:
        change_lines = _format_change_lines(diff)
        if not change_lines and not diff.conflicts:
            continue
        lines.append(f"  {diff.track.path.name}:")
        lines.extend(change_lines)
        for conflict in diff.conflicts:
            candidates = ", ".join(conflict.candidate_values)
            kept = "; locked" if conflict.resolution == conflict.old_value else ""
            lines.append(
                f"    {conflict.field} conflict: file={conflict.old_value}; "
                f"MusicBrainz=[{candidates}]; resolution={conflict.resolution}{kept}"
            )
    lines.append("")
    lines.append("---")
    lines.append("")

    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _write_ratings(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger write-ratings",
        description="Write rating and starred tags to audio files from a ratings JSON.",
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
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Append changes to this log file.",
    )
    args = parser.parse_args(argv)

    songs = [SongRating.from_dict(s) for s in json.loads(args.from_file.read_text())]

    total_files = 0
    skipped = 0
    log_lines: list[str] = []

    for song in songs:
        path = args.root / song.path
        if not path.exists():
            skipped += 1
            continue

        changes = write_rating_to_file(path, song.rating, song.starred, dry_run=args.dry_run)
        if changes:
            total_files += 1
            if args.dry_run:
                print(f"  {song.path}:")
                for c in changes:
                    old = c.old_value or "(empty)"
                    print(f"    {c.field}: {old} → {c.new_value}")
            if args.log:
                log_lines.append(f"  {song.path}: {', '.join(c.field for c in changes)}")

    if skipped:
        print(f"Skipped {skipped} file(s) (not found).", file=sys.stderr)

    if args.dry_run:
        print(f"\n(dry run — {len(songs)} songs, {total_files} would change)")
    else:
        print(f"Wrote rating tags to {total_files} file(s).")
        if total_files == 0 and skipped > 0:
            sys.exit(1)

    if args.log and not args.dry_run and log_lines:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        header = ["## Rating tags", "", f"- **Date:** {timestamp}", ""]
        with open(args.log, "a") as f:
            f.write("\n".join(header + log_lines + ["", "---", ""]))


def _scan(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger scan",
        description="Scan library and output a JSON list of albums to process.",
    )
    parser.add_argument("path", type=Path, help="Library root directory.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output JSON file (default: stdout).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include albums that already have a consistent MB ID.",
    )
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

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

    if args.output and args.output.suffix == ".md":
        _write_scan_checklist(args.output, entries)
        return

    output = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        args.output.write_text(output)
        print(f"Wrote {len(entries)} entries to {args.output}")
    else:
        print(output)


def _write_scan_checklist(path: Path, scanned: list[dict[str, object]]) -> None:
    """Add scanned albums to a markdown checklist, keeping existing entries as they are."""
    checklist = Checklist.load(path)
    new_entries = []
    for album in scanned:
        album_ids = album.get("musicbrainz_albumid")
        if isinstance(album_ids, list):
            ids = ", ".join(album_ids)
        else:
            ids = str(album_ids) if album_ids else "NO MB ID"
        description = (
            f"{album['artist']} — {album['album']} ({album['track_count']} tracks) [{ids}]"
        )
        new_entries.append(ChecklistEntry(str(album["path"]), description))
    added = checklist.add_new(new_entries)
    checklist.save(path)
    print(f"Wrote {len(checklist.entries)} entries ({added} new) to {path}")


def _checklist(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger checklist", description="Read and update the album checklist."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Checklist file (default: $MUSIC_TAGGER_CHECKLIST, "
        "else ~/Projects/music-tagger/checklist.md).",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("next", help="Show the next unchecked album.")
    done = actions.add_parser("done", help="Mark an album done.")
    done.add_argument("album", help="Album directory as listed.")
    skip = actions.add_parser("skip", help="Mark an album skipped, with the reason.")
    skip.add_argument("album", help="Album directory as listed.")
    skip.add_argument("--reason", required=True, help="Why it was skipped.")
    reopen = actions.add_parser("reopen", help="Uncheck an album, or every skip matching text.")
    reopen.add_argument("album", nargs="?", default=None, help="Album directory as listed.")
    reopen.add_argument("--matching", default=None, help="Reopen skips whose reason contains this.")
    for action in (done, skip, reopen):
        action.add_argument("--file", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action == "reopen" and (args.album is None) == (args.matching is None):
        reopen.error("give an album or --matching, not both")

    path: Path = args.file or _default_checklist_path()
    checklist = Checklist.load(path)

    if args.action == "next":
        _print_next_entry(checklist)
        return
    if args.action == "reopen" and args.matching is not None:
        matched = [
            candidate
            for candidate in checklist.entries
            if candidate.status == "skipped" and args.matching in candidate.reason
        ]
        for skipped in matched:
            skipped.status, skipped.reason = "todo", ""
        checklist.save(path)
        print(f"Reopened {len(matched)} entry(ies).")
        return

    entry = checklist.find(args.album)
    if entry is None:
        print(f"Error: {args.album} is not in {path}.", file=sys.stderr)
        sys.exit(1)
    if args.action == "done":
        entry.status, entry.reason = "done", ""
    elif args.action == "skip":
        entry.status, entry.reason = "skipped", args.reason
    else:
        entry.status, entry.reason = "todo", ""
    checklist.save(path)
    print(entry.format())


def _print_next_entry(checklist: Checklist) -> None:
    entry = checklist.next_todo()
    if entry is None:
        print("Checklist complete.")
        return
    remaining = sum(e.status == "todo" for e in checklist.entries)
    print(f"Next: {entry.path}")
    print(f"Album: {entry.description}")
    print(f"Remaining: {remaining}")


def _print_candidates(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger candidates",
        description="Read album, search MusicBrainz, and score candidates.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "--artist", default=None, help="Override artist name (for freshly ripped files)."
    )
    parser.add_argument(
        "--album", default=None, help="Override album title (for freshly ripped files)."
    )
    parser.add_argument("--barcode", default=None, help="Filter results by barcode (UPC/EAN).")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Also write the full results as JSON to this file.",
    )
    output.add_argument(
        "--json", action="store_true", help="Print JSON to stdout instead of the table."
    )
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    mb_client = MusicBrainzClient()
    try:
        album, candidates = search_candidates(
            target,
            mb_client,
            artist=args.artist,
            album_title=args.album,
            barcode=args.barcode,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        mb_client.close()

    matches = score_candidates(album, candidates)
    data = {"album": album.to_dict(), "matches": [m.to_dict() for m in matches]}
    if args.json:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return
    if args.out:
        args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    searched = f'artist="{args.artist or album.artist}" album="{args.album or album.album}"'
    if args.barcode:
        searched += f' barcode="{args.barcode}"'
    print(f"Local: {album.artist} — {album.album} ({album.track_count} tracks)")
    print(f"Searched: {searched}")
    print()
    if not matches:
        print("No candidates found.")
        return
    print(
        _format_table(
            _CANDIDATE_COLUMNS, [_candidate_row(i, m, album) for i, m in enumerate(matches, 1)]
        )
    )


_CANDIDATE_COLUMNS = [
    "#",
    "Release ID",
    "Tracks",
    "Within 2s",
    "Max dev",
    "Country",
    "Date",
    "Label",
    "Barcode",
    "Flags",
    "Title",
]


def _candidate_row(rank: int, match: CandidateMatch, album: AlbumTags) -> list[str]:
    release = match.release
    stats = match.stats
    return [
        str(rank),
        release.id,
        f"{album.track_count}/{release.track_count}",
        f"{stats.tracks_within_2s}/{stats.tracks_compared}",
        f"{stats.max_deviation_secs:.1f}s",
        release.country or "-",
        release.date or "-",
        release.label or "-",
        release.barcode or "-",
        ",".join(match.flags) or "-",
        release.title,
    ]


def _format_table(header: list[str], rows: list[list[str]]) -> str:
    """Align columns with two spaces; the last column is not padded."""
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header) - 1)]
    lines = []
    for row in [header, *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        lines.append("  ".join([*cells, row[-1]]))
    return "\n".join(lines)


def _print_tag_issues(result: AlbumResult) -> None:
    for diff in result.diffs:
        for warning in diff.warnings:
            print(f"Warning: {diff.track.path}: {warning}", file=sys.stderr)
        for conflict in diff.conflicts:
            candidates = ", ".join(conflict.candidate_values)
            status = f"resolved as {conflict.resolution}" if conflict.is_resolved else "unresolved"
            print(
                f"{conflict.field.upper()} conflict: {diff.track.path}: file={conflict.old_value}; "
                f"MusicBrainz=[{candidates}]; {status}",
                file=sys.stderr,
            )


def _print_diff(result: AlbumResult) -> None:
    print(f"Release: {result.release.title} [{result.release.id}]")
    print(f"         {result.release.date} / {result.release.country} / {result.release.label}")
    print()
    _print_tag_issues(result)

    for diff in result.diffs:
        change_lines = _format_change_lines(diff)
        if change_lines:
            print(f"  {diff.track.path.name}:")
            print("\n".join(change_lines))

    # "Locked" must never read as "already correct".
    locked_count = sum(len(diff.locked_changes) for diff in result.diffs)
    if result.has_changes:
        print()
        if locked_count:
            print(f"{locked_count} locked field(s) kept.")
    elif result.has_unresolved_conflicts or any(diff.warnings for diff in result.diffs):
        print("No tag changes proposed; review the reported ISRC issues.")
    elif locked_count:
        print(f"No tag changes to apply; {locked_count} locked field(s) kept.")
    else:
        print("No tag changes needed.")


def _print_gate(result: AlbumResult) -> int:
    """Print the safety-gate verdict. Returns 0, EXIT_REVIEW, or EXIT_BLOCKED."""
    if result.has_unresolved_conflicts:
        count = sum(
            not conflict.is_resolved for diff in result.diffs for conflict in diff.conflicts
        )
        print(
            f"Gate: blocked — {count} unresolved ISRC conflict(s); see stderr. "
            "Rerun with --isrc keep, --isrc musicbrainz, or --isrc-code [DISC-]TRACK=CODE."
        )
        return EXIT_BLOCKED

    overwrites = overwrites_needing_review(result)
    if overwrites:
        print("Gate: review — these overwrites need approval:")
        for path, change in overwrites:
            print(f"  {path.name}: {change.field}: {change.old_value} → {change.new_value}")
        return EXIT_REVIEW

    print("Gate: auto — no overwrites need approval.")
    return 0


def _format_change_lines(diff: TrackDiff) -> list[str]:
    lines = []
    for change in diff.effective_changes:
        old = change.old_value or "(empty)"
        lines.append(f"    {change.field}: {old} → {change.new_value}")
    for change in diff.locked_changes:
        old = change.old_value or "(empty)"
        lines.append(f"    {change.field}: {old} (locked; release has {change.new_value})")
    return lines


def _path_for(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger path-for",
        description="Compute library destination paths for album files.",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="Evidence JSON from `read`.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Library root (default: /mnt/synology/media/music).",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    album = AlbumTags.from_dict(json.loads(args.evidence.read_text()))

    kwargs = {}
    if args.root:
        kwargs["root"] = args.root

    plan = compute_placement(album, **kwargs)
    data = plan.to_dict()

    artist = album.artist or "Unknown Artist"
    album_name = album.album or "Unknown Album"
    lines = [
        f"{artist} — {album_name}",
        f"{len(plan.mappings)} files → {plan.album_dir}",
    ]
    _output_json(data, args.out, "\n".join(lines))


def _copy(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger copy",
        description="Copy files to library destinations per a placement plan.",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        dest="plan_file",
        help="Plan JSON from `path-for` (possibly LLM-edited).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without copying.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    plan = PlacementPlan.from_dict(json.loads(args.plan_file.read_text()))
    result = copy_files(plan, dry_run=args.dry_run)

    if result.skipped:
        print(f"Skipped {len(result.skipped)} file(s) (already exist):")
        for m in result.skipped:
            print(f"  {m.dest}")

    if result.failed:
        print(f"Failed {len(result.failed)} file(s):")
        for m, err in result.failed:
            print(f"  {m.dest}: {err}")

    size_mb = result.total_bytes / (1024 * 1024)
    if args.dry_run:
        n = len(result.copied)
        print(f"\n(dry run) Would copy {n} file(s) ({size_mb:.1f} MB) → {plan.album_dir}")
    else:
        print(f"Copied {len(result.copied)} file(s) ({size_mb:.1f} MB) → {plan.album_dir}")

    if result.failed:
        sys.exit(1)

    log_path = _log_path(args)
    if log_path and not args.dry_run:
        _write_copy_log(log_path, plan, result)


def _write_copy_log(log_path: Path, plan: PlacementPlan, result: object) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## Copy to {plan.album_dir}",
        "",
        f"- **Date:** {timestamp}",
        f"- **Files:** {len(plan.mappings)}",
        "",
    ]
    from .placement import CopyResult

    if isinstance(result, CopyResult):
        for m in result.copied:
            lines.append(f"  {m.source.name} → {m.dest}")
    lines.append("")
    lines.append("---")
    lines.append("")
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _art(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger art",
        description="Fetch cover art from the Cover Art Archive, or use a local image.",
    )
    parser.add_argument("path", type=Path, help="Album directory to save cover art into.")
    parser.add_argument("--release-id", default=None, help="MusicBrainz release UUID.")
    parser.add_argument(
        "--cover-file", type=Path, default=None, help="Use a local image instead of fetching."
    )
    parser.add_argument(
        "--full", action="store_true", help="Fetch full-size image instead of 500px."
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace the album's existing cover.* file."
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Embed cover art into all audio files. A PNG, or a JPEG too large for a FLAC "
        "picture block, is embedded as a converted JPEG; the folder keeps the original.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Download the release's full-size front cover to a temp file for review. "
        "Writes nothing to the album.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    if not args.release_id and not args.cover_file:
        parser.error("provide --release-id or --cover-file")
    if args.preview and args.cover_file:
        parser.error("--preview applies to --release-id only")

    target: Path = args.path.resolve()
    if args.preview:
        _preview_cover(args.release_id, target)
        return

    try:
        result, embedded = _install_and_embed(target, args)
    except (UnsupportedImageError, MutagenError, OSError) as error:
        print(f"Error: cover art failed: {error}", file=sys.stderr)
        sys.exit(1)

    log_path = _log_path(args)
    if log_path and (result.saved or embedded):
        source = (
            str(args.cover_file)
            if args.cover_file
            else f"Cover Art Archive release {args.release_id}"
        )
        _write_art_log(log_path, target, source, result, embedded)


def _install_and_embed(
    target: Path, args: argparse.Namespace
) -> tuple[ArtResult, tuple[int, EmbeddedCover] | None]:
    """Install the folder cover, then embed it when --embed is set.

    Returns the install result and, when embedding ran, the file count and image.
    """
    if args.cover_file:
        result = use_local_cover(args.cover_file.resolve(), target, force=args.force)
    else:
        result = fetch_cover_art(args.release_id, target, full_size=args.full, force=args.force)

    if result.not_found:
        if args.cover_file:
            print(f"Cover file not found: {args.cover_file}")
        else:
            print("No cover art available for this release.")
    elif result.skipped and result.path is not None:
        print(f"{result.path.name} already exists at {result.path} (use --force to replace).")
    elif result.saved and result.path is not None:
        print(f"Saved {result.path.name} ({result.size_bytes / 1024:.0f} KB) to {result.path}")
    for removed in result.removed:
        print(f"Removed {removed}")

    if not args.embed or result.path is None:
        return result, None
    cover = embeddable_cover(result.path)
    count = embed_cover_art(target, cover.data, cover.mime)
    converted = f" (converted to JPEG, {len(cover.data) / 1024:.0f} KB)" if cover.converted else ""
    print(f"Embedded cover art into {count} file(s){converted}.")
    return result, (count, cover)


def _preview_cover(release_id: str, album_dir: Path) -> None:
    result = fetch_cover_art(
        release_id, Path(tempfile.mkdtemp(prefix="cover-")), full_size=True, force=True
    )
    if result.not_found or result.path is None:
        print(f"No cover art available for release {release_id}.")
        sys.exit(1)

    print(f"Candidate: {result.path} ({_describe_image(result.path)})")
    current = find_cover(album_dir)
    if current is not None:
        print(f"Current: {current} ({_describe_image(current)})")
    else:
        print("Current: none")


def _describe_image(path: Path) -> str:
    size_kb = path.stat().st_size / 1024
    dimensions = image_dimensions(path)
    if dimensions is None:
        return f"unknown format, {size_kb:.0f} KB"
    width, height = dimensions
    return f"{width}x{height}, {size_kb:.0f} KB"


def _write_art_log(
    log_path: Path,
    album_dir: Path,
    source: str,
    result: ArtResult,
    embedded: tuple[int, EmbeddedCover] | None,
) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    saved = f"{result.path} ({result.size_bytes / 1024:.0f} KB)" if result.saved else "no"
    if embedded is None:
        embedded_text = "0 file(s)"
    else:
        count, cover = embedded
        conversion = ", converted to JPEG" if cover.converted else ""
        embedded_text = (
            f"{count} file(s) ({cover.mime}, {len(cover.data) / 1024:.0f} KB{conversion})"
        )
    lines = [
        "## Cover art",
        "",
        f"- **Date:** {timestamp}",
        f"- **Album:** {album_dir}",
        f"- **Source:** {source}",
        f"- **Saved:** {saved}",
        *(f"- **Removed:** {path}" for path in result.removed),
        f"- **Embedded:** {embedded_text}",
        "",
        "---",
        "",
    ]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _rip(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger rip",
        description="Rip a CD to FLAC via whipper with AccurateRip verification.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="Output directory for FLAC files (default: a new temp staging directory).",
    )
    parser.add_argument(
        "--device", default="/dev/cdrom", help="CD drive device (default: /dev/cdrom)."
    )
    parser.add_argument("--release-id", default=None, help="MusicBrainz release ID to match.")
    parser.add_argument(
        "--unknown",
        action="store_true",
        help="Continue ripping if the CD is not in MusicBrainz.",
    )
    args = parser.parse_args(argv)

    from . import ripper

    target = args.path.resolve() if args.path else Path(tempfile.mkdtemp(prefix="rip-"))

    print(f"Staging: {target}", flush=True)
    try:
        result = ripper.rip_cd(
            target,
            device=args.device,
            release_id=args.release_id,
            unknown=args.unknown,
        )
    except subprocess.CalledProcessError as error:
        print(f"Error: whipper failed (exit {error.returncode}).", file=sys.stderr)
        print(
            "If whipper reported the disc is not in MusicBrainz, rerun with --unknown.",
            file=sys.stderr,
        )
        sys.exit(1)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"Ripped {result.track_count} tracks.")

    if result.cue_file:
        print(f"CUE: {result.cue_file}")
    if result.log_file is None:
        print("Error: whipper wrote no log; AccurateRip results unknown.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)
    print(f"Log: {result.log_file}")
    accuraterip = ripper.parse_accuraterip(result.log_file.read_text())
    print("\n".join(accuraterip.describe()))
    if accuraterip.needs_review:
        print("Rip needs review: see the mismatched tracks above.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)

    print("\nNext: review tags and candidates:")
    print(f"  uv run music-tagger candidates {target}")


def _nd_rescan(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger nd rescan",
        description="Trigger a Navidrome library scan.",
    )
    parser.parse_args(argv)

    client = NavidromeClient()
    try:
        status = client.start_scan()
    finally:
        client.close()

    scanning = status.get("scanning", False)
    count = status.get("count", 0)
    if scanning:
        print(f"Scan started ({count} files scanned so far).")
    else:
        print(f"Scan complete ({count} files).")


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
    if songs:
        sample = songs[0]
        lines.append(f"  e.g. {sample.artist} — {sample.title}")

    _output_json(data, args.out, "\n".join(lines))


def _nd_set_rating(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger nd set-rating",
        description="Push ratings and stars from JSON back into Navidrome.",
    )
    parser.add_argument(
        "--from",
        type=Path,
        default=_DEFAULT_RATINGS_PATH,
        dest="from_file",
        help=f"Ratings JSON (default: {_DEFAULT_RATINGS_PATH}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be set without making changes.",
    )
    args = parser.parse_args(argv)

    songs = [SongRating.from_dict(s) for s in json.loads(args.from_file.read_text())]

    if args.dry_run:
        for s in songs:
            parts = []
            if s.rating:
                parts.append(f"rating={s.rating}")
            if s.starred:
                parts.append("starred")
            if parts:
                print(f"  {s.artist} — {s.title}: {', '.join(parts)}")
        print(f"\n(dry run — {len(songs)} song(s))")
        return

    client = NavidromeClient()
    try:
        rated = 0
        starred = 0
        for s in songs:
            if not s.id:
                print(f"  skipped (no id): {s.artist} — {s.title}", file=sys.stderr)
                continue
            if s.rating:
                client.set_rating(s.id, s.rating)
                rated += 1
            if s.starred:
                client.star(s.id)
                starred += 1
    finally:
        client.close()

    print(f"Set {rated} rating(s) and {starred} star(s).")


def _nd(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = []

    nd_commands: dict[str, tuple[str, Callable[[list[str] | None], None]]] = {
        "rescan": ("Trigger a library scan.", _nd_rescan),
        "ratings": ("Export rated/starred songs as JSON.", _nd_ratings),
        "set-rating": ("Push ratings/stars back into Navidrome.", _nd_set_rating),
    }

    if not argv or argv[0] in ("-h", "--help"):
        print("usage: music-tagger nd <command> [options]\n")
        print("Navidrome commands.\n")
        print("commands:")
        for name, (helptext, _) in nd_commands.items():
            print(f"  {name:<10}  {helptext}")
        sys.exit(0 if argv else 1)

    command = argv[0]
    remaining = argv[1:]

    if command in nd_commands:
        _, handler = nd_commands[command]
        handler(remaining)
    else:
        print(f"music-tagger nd: unknown command '{command}'", file=sys.stderr)
        sys.exit(1)


def _scan_library_genres(root: Path) -> list[tuple[str, int]]:
    """Scan library for all GENRE tags, return (count, genre) sorted by count descending."""
    import subprocess

    result = subprocess.run(
        ["find", str(root), "-name", "*.flac", "-exec", "metaflac", "--show-tag=GENRE", "{}", ";"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if line.upper().startswith("GENRE="):
            genre = line.split("=", 1)[1]
            if genre:
                counts[genre] = counts.get(genre, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def _genre(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger genre",
        description="Set or show the genre tag on all tracks in an album directory.",
    )
    parser.add_argument("path", nargs="?", type=Path, default=None, help="Album directory.")
    parser.add_argument(
        "genres",
        nargs="*",
        default=None,
        help="Genres to set, replacing what is there. Omit to show current genres.",
    )
    parser.add_argument(
        "--add", action="append", default=[], metavar="GENRE", help="Add a genre, keeping the rest."
    )
    parser.add_argument(
        "--remove", action="append", default=[], metavar="GENRE", help="Remove a genre."
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow genres that are not on the canonical list."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all genres in the library with track counts.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_LIBRARY_ROOT,
        help=f"Library root for --list (default: {_DEFAULT_LIBRARY_ROOT}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing."
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    if args.list:
        genres = _scan_library_genres(args.root)
        if not genres:
            print("No genres found.")
            return
        width = len(str(genres[0][1]))
        for genre, count in genres:
            print(f"  {count:>{width}}  {genre}")
        print(f"\n{len(genres)} genre(s), {sum(c for _, c in genres)} track(s)")
        return

    if args.path is None:
        parser.error("path is required (unless using --list)")

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    from .genres import GENRE_LIST_PATH, load_canonical
    from .tags import read_genres, write_genres

    if not args.genres and not args.add and not args.remove:
        current = {g for track in album.tracks for g in read_genres(track.path)}
        for g in sorted(current):
            print(g)
        if not current:
            print("(no genre set)")
        return

    if args.genres and (args.add or args.remove):
        parser.error("give genres to set, or --add/--remove, not both")

    canonical = load_canonical()
    if not args.force:
        if not canonical:
            print(f"Warning: {GENRE_LIST_PATH} not found — genres unvalidated.", file=sys.stderr)
        elif unknown := [g for g in list(args.genres) + list(args.add) if g not in canonical]:
            print(f"Error: not on the canonical list: {', '.join(unknown)}", file=sys.stderr)
            print(f"See {GENRE_LIST_PATH}, or pass --force.", file=sys.stderr)
            sys.exit(1)

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    for track in album.tracks:
        old = read_genres(track.path)
        if args.genres:
            new = list(dict.fromkeys(args.genres))
        else:
            new = [g for g in old if g not in args.remove]
            new += [g for g in args.add if g not in new]
        if new == old:
            continue
        if args.dry_run:
            # List repr, not a joined string: one delimited value and two
            # separate values must not render identically.
            print(f"  {track.path.name}: {old} → {new}")
        else:
            write_genres(track.path, new, timestamp=stamp)
        count += 1

    applied = sorted({g for track in album.tracks for g in read_genres(track.path)})
    if args.dry_run:
        print(f"\n(dry run — {count} of {len(album.tracks)} tracks would change)")
    else:
        print(f"Set genre={applied} on {count} track(s).")

    log_path = _log_path(args)
    if log_path and not args.dry_run and count > 0:
        _write_genre_log(log_path, album, str(applied), count)


def _write_genre_log(log_path: Path, album: AlbumTags, genre: str, count: int) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## Genre: {genre}",
        "",
        f"- **Date:** {timestamp}",
        f"- **Album:** {album.artist} — {album.album}",
        f"- **Tracks:** {count}",
        "",
        "---",
        "",
    ]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _rename(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger rename",
        description="Rename audio files to 'NN. Title.ext' (or 'D-NN. Title.ext') from tags.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument("--dry-run", action="store_true", help="Show renames without executing.")
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    from .placement import is_multi_disc, track_filename

    multi_disc = is_multi_disc(album)
    renames: list[tuple[str, str]] = []
    for track in album.tracks:
        title = track.tags.get("title")
        tracknumber = track.tags.get("tracknumber")
        if not title or not tracknumber:
            print(f"  {track.path.name}: skipped (missing title or tracknumber)")
            continue

        try:
            int(tracknumber.split("/")[0])
        except ValueError:
            print(f"  {track.path.name}: skipped (unparseable tracknumber '{tracknumber}')")
            continue

        # Same names `place` gives files in the library.
        new_name = track_filename(track, multi_disc)
        new_path = track.path.parent / new_name

        if new_path == track.path:
            continue

        if new_path.exists():
            print(f"  {track.path.name}: skipped ('{new_name}' already exists)")
            continue

        if args.dry_run:
            print(f"  {track.path.name} -> {new_name}")
        else:
            track.path.rename(new_path)
            print(f"  {track.path.name} -> {new_name}")
            renames.append((track.path.name, new_name))

    if args.dry_run:
        print(f"\n(dry run — {len(album.tracks)} tracks)")
    else:
        print(f"Renamed {len(renames)} file(s).")

    log_path = _log_path(args)
    if log_path and not args.dry_run and renames:
        _write_rename_log(log_path, album, renames)


def _write_rename_log(log_path: Path, album: AlbumTags, renames: list[tuple[str, str]]) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "## Rename",
        "",
        f"- **Date:** {timestamp}",
        f"- **Album:** {album.artist} — {album.album}",
        f"- **Files:** {len(renames)}",
        "",
    ]
    for old, new in renames:
        lines.append(f"- `{old}` → `{new}`")
    lines += ["", "---", ""]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _replaygain(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger replaygain",
        description="Compute and write ReplayGain 2.0 tags via rsgain.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument("--dry-run", action="store_true", help="Show gains without writing tags.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already have ReplayGain tags.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    from . import replaygain

    result = replaygain.scan_replaygain(
        target, dry_run=args.dry_run, skip_existing=args.skip_existing
    )

    if args.dry_run:
        print(f"\n(dry run — {result.track_count} tracks scanned, no tags written)")
        return
    print(f"Wrote ReplayGain tags to {result.track_count} track(s).")

    log_path = _log_path(args)
    if log_path:
        album_gain = replaygain.read_album_gain(target) or "(not found)"
        _write_replaygain_log(log_path, target, result.track_count, album_gain)


def _write_replaygain_log(
    log_path: Path, album_dir: Path, track_count: int, album_gain: str
) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "## ReplayGain",
        "",
        f"- **Date:** {timestamp}",
        f"- **Album:** {album_dir}",
        f"- **Tracks:** {track_count}",
        f"- **Album gain:** {album_gain}",
        "",
        "---",
        "",
    ]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _apply(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger apply",
        description="Run tag, art, genre, rename, and replaygain on an album, stopping at the "
        "first failure. Inputs are checked before anything is written.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument("--release-id", required=True, help="MusicBrainz release UUID.")
    cover = parser.add_mutually_exclusive_group(required=True)
    cover.add_argument("--cover-file", type=Path, help="Install and embed this reviewed image.")
    cover.add_argument(
        "--keep-cover", action="store_true", help="Embed the album's existing cover.* image."
    )
    parser.add_argument(
        "--genre", action="append", required=True, help="Genre to set. Repeat for several."
    )
    parser.add_argument(
        "--force-genre", action="store_true", help="Allow genres not on the canonical list."
    )
    parser.add_argument("--approve-overwrites", action="store_true", help="Passed to `tag`.")
    parser.add_argument("--ignore-locks", action="store_true", help="Passed to `tag`.")
    parser.add_argument("--isrc", choices=ISRC_POLICIES, default=None, help="Passed to `tag`.")
    parser.add_argument(
        "--isrc-code",
        action="append",
        default=[],
        metavar="[DISC-]TRACK=CODE",
        help="Passed to `tag`.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    cover_path = _check_apply_inputs(target, args)

    log_args = ["--no-log"] if args.no_log else ["--log", str(_log_path(args))]
    tag_args = _apply_tag_args(args)
    # The album's own cover only needs embedding; --force would replace it with itself.
    art_args = ["--cover-file", str(cover_path), "--embed"]
    if cover_path != find_cover(target):
        art_args.append("--force")
    genre_args = [*args.genre, *(["--force"] if args.force_genre else [])]

    steps: list[tuple[str, Callable[[list[str] | None], None], list[str]]] = [
        ("tag", _tag, tag_args),
        ("art", _art, art_args),
        ("genre", _genre, genre_args),
        ("rename", _rename, []),
        ("replaygain", _replaygain, []),
    ]
    for name, handler, step_args in steps:
        print(f"\n== {name} ==")
        try:
            handler([str(target), *step_args, *log_args])
        except SystemExit as error:
            if error.code:
                print(f"apply stopped at {name} (exit {error.code}).", file=sys.stderr)
                raise
        except Exception as error:
            # Unexpected step failures still get a named step, not a bare traceback.
            print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
            print(f"apply stopped at {name} (exit 1).", file=sys.stderr)
            sys.exit(1)
    _mark_checklist_done(target)
    print("\nDone.")


def _mark_checklist_done(album_dir: Path) -> None:
    path = _default_checklist_path()
    checklist = Checklist.load(path)
    entry = checklist.find(album_dir)
    if entry is None:
        return
    entry.status, entry.reason = "done", ""
    checklist.save(path)
    print(f"Checklist: marked {album_dir} done.")


def _apply_tag_args(args: argparse.Namespace) -> list[str]:
    """Rebuild the `tag` arguments that `apply` passes through."""
    tag_args = ["--release-id", args.release_id]
    if args.approve_overwrites:
        tag_args.append("--approve-overwrites")
    if args.ignore_locks:
        tag_args.append("--ignore-locks")
    if args.isrc:
        tag_args += ["--isrc", args.isrc]
    for spec in args.isrc_code:
        tag_args += ["--isrc-code", spec]
    return tag_args


def _check_apply_inputs(target: Path, args: argparse.Namespace) -> Path:
    """Validate `apply` inputs before any step writes. Returns the cover image path."""
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if args.keep_cover:
        existing = find_cover(target)
        if existing is None:
            print(f"Error: --keep-cover, but {target} has no cover.* image.", file=sys.stderr)
            sys.exit(1)
        cover_path = existing
    else:
        cover_path = args.cover_file.resolve()
        if not cover_path.is_file():
            print(f"Error: cover image not found: {cover_path}", file=sys.stderr)
            sys.exit(1)
    try:
        embeddable_cover(cover_path)
    except UnsupportedImageError as error:
        print(f"Error: cover image cannot be embedded: {error}", file=sys.stderr)
        sys.exit(1)

    from .genres import GENRE_LIST_PATH, load_canonical

    canonical = load_canonical()
    unknown = [genre for genre in args.genre if canonical and genre not in canonical]
    if unknown and not args.force_genre:
        print(f"Error: not on the canonical list: {', '.join(unknown)}", file=sys.stderr)
        print(f"See {GENRE_LIST_PATH}, or pass --force-genre.", file=sys.stderr)
        sys.exit(1)
    return cover_path


def _place(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger place",
        description="Copy a finished album into the library with SHA-256 verification. "
        "Removes the source afterward when it is a temp staging directory.",
    )
    parser.add_argument("path", type=Path, help="Album directory to copy.")
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_LIBRARY_ROOT,
        help=f"Library root (default: {_DEFAULT_LIBRARY_ROOT}).",
    )
    parser.add_argument("--artist-dir", default=None, help="Override the artist directory name.")
    parser.add_argument("--album-dir", default=None, help="Override the album directory name.")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Allow copying into an existing album directory. Existing files are kept.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show the destination and files without copying."
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    source: Path = args.path.resolve()
    album = read_album(source)
    if not album.tracks:
        print(f"Error: no audio files in {source}.", file=sys.stderr)
        sys.exit(1)

    plan = compute_placement(
        album, root=args.root, artist_dir=args.artist_dir, album_dir=args.album_dir
    )
    destination_exists = plan.album_dir.exists()
    print(f"{album.artist} — {album.album}")
    print(f"Destination: {plan.album_dir}" + (" (exists)" if destination_exists else ""))
    for mapping in plan.mappings:
        print(f"  {mapping.source.name} → {mapping.dest.name}")

    if destination_exists and not args.merge:
        print("Error: destination exists; pass --merge to copy into it.", file=sys.stderr)
        sys.exit(1)
    if args.dry_run:
        print("\n(dry run — nothing copied)")
        return

    print(f"\nCopying {len(plan.mappings)} file(s) with SHA-256 verification:", flush=True)
    result = copy_files(plan, on_file=_copy_progress_printer(len(plan.mappings)))
    log_path = _log_path(args)
    if log_path:
        _write_copy_log(log_path, plan, result)
    print(f"Copied {len(result.copied)} of {len(plan.mappings)} file(s).")
    if result.failed:
        sys.exit(1)

    # Delete only a temp staging directory whose every file was copied and verified.
    if not result.skipped and source.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        shutil.rmtree(source)
        print(f"Removed staging directory {source}")


def _copy_progress_printer(total: int) -> Callable[[FileMapping, str], None]:
    """Print one flushed line per file as `copy_files` finishes it."""
    done = 0

    def report(mapping: FileMapping, status: str) -> None:
        nonlocal done
        done += 1
        line = f"  [{done}/{total}] {mapping.dest.name}: {status}"
        stream = sys.stderr if status.startswith("failed") else sys.stdout
        print(line, file=stream, flush=True)

    return report


def _parse_isrc_code(spec: str) -> tuple[tuple[int, int], str]:
    """Parse "[DISC-]TRACK=CODE" into ((disc, track), code)."""
    position, _, code = spec.partition("=")
    disc_text, _, track_text = position.rpartition("-")
    try:
        disc = int(disc_text) if disc_text else 1
        track = int(track_text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected [DISC-]TRACK=CODE, got {spec!r}") from None
    if not code:
        raise argparse.ArgumentTypeError(f"missing ISRC after '=' in {spec!r}")
    return (disc, track), code


def _lock(argv: list[str] | None = None) -> None:
    _edit_locks(argv, locking=True)


def _unlock(argv: list[str] | None = None) -> None:
    _edit_locks(argv, locking=False)


def _edit_locks(argv: list[str] | None, *, locking: bool) -> None:
    command = "lock" if locking else "unlock"
    args = _lock_parser(command, locking).parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    if not args.fields:
        for track in album.tracks:
            print(f"  {track.path.name}: {', '.join(track.locked_fields) or '(none)'}")
        return

    fields = set(args.fields)
    tracks = _lock_targets(album, fields, args.track, args.disc)
    changed = _rewrite_locks(tracks, fields, locking=locking, dry_run=args.dry_run)

    field_list = ", ".join(sorted(fields))
    if args.dry_run:
        print(f"\n(dry run — {len(changed)} of {len(tracks)} tracks would change)")
        return
    print(f"{command.capitalize()}ed {field_list} on {len(changed)} track(s).")

    log_path = _log_path(args)
    if log_path and changed:
        _write_lock_log(log_path, album, f"{command.capitalize()}: {field_list}", changed)


def _lock_parser(command: str, locking: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"music-tagger {command}",
        description=(
            "Lock fields so `tag` keeps their current value, or show locks."
            if locking
            else "Unlock fields so `tag` can change them again."
        ),
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "fields",
        nargs="*" if locking else "+",
        help="Tag fields, e.g. isrc title." + (" Omit to show current locks." if locking else ""),
    )
    parser.add_argument("--track", type=int, default=None, help="Only this track number.")
    parser.add_argument("--disc", type=int, default=None, help="Only this disc number.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing."
    )
    _add_log_arguments(parser)
    return parser


def _lock_targets(
    album: AlbumTags, fields: set[str], track_number: int | None, disc: int | None
) -> list[TrackTags]:
    """Validate a lock edit and return its tracks, or exit before anything is written."""
    if unknown := sorted(fields - LOCKABLE_FIELDS):
        print(f"Error: unknown field(s): {', '.join(unknown)}", file=sys.stderr)
        sys.exit(1)

    selected = [
        track
        for track in album.tracks
        if (track_number is None or _parse_track_number(track) == track_number)
        and (disc is None or _parse_disc_number(track) == disc)
    ]
    selector = " ".join(
        f"--{name} {value}"
        for name, value in (("track", track_number), ("disc", disc))
        if value is not None
    )
    if not selected:
        print(f"Error: no track matches {selector}.", file=sys.stderr)
        sys.exit(1)
    if track_number is not None and len(selected) > 1:
        names = ", ".join(track.path.name for track in selected)
        print(f"Error: {selector} matches {names}; add --disc.", file=sys.stderr)
        sys.exit(1)

    # Check every file first so an M4A in the album cannot leave a partial edit.
    if unsupported := [t.path.name for t in selected if t.format not in ("flac", "mp3")]:
        print(f"Error: locks are not supported for: {', '.join(unsupported)}", file=sys.stderr)
        sys.exit(1)
    return selected


def _rewrite_locks(
    tracks: list[TrackTags], fields: set[str], *, locking: bool, dry_run: bool
) -> list[TrackTags]:
    """Add or remove `fields` from each track's locks. Returns the tracks that changed."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    changed = []
    for track in tracks:
        old = sorted(track.locked_fields)
        new = sorted(set(old) | fields) if locking else sorted(set(old) - fields)
        if new == old:
            continue
        if dry_run:
            print(f"  {track.path.name}: {old} → {new}")
        else:
            write_locks(track.path, new, timestamp=stamp)
        changed.append(track)
    return changed


def _write_lock_log(
    log_path: Path, album: AlbumTags, heading: str, tracks: list[TrackTags]
) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## {heading}",
        "",
        f"- **Date:** {timestamp}",
        f"- **Album:** {album.artist} — {album.album}",
        f"- **Tracks:** {len(tracks)}",
        "",
    ]
    lines += [f"- `{track.path.name}`" for track in tracks]
    lines += ["", "---", ""]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _tag(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger tag",
        description="Fetch release, compute diff, and apply tags.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument("--release-id", required=True, help="MusicBrainz release UUID.")
    parser.add_argument("--dry-run", action="store_true", help="Show diff without writing.")
    _add_log_arguments(parser)
    parser.add_argument(
        "--ignore-locks",
        action="store_true",
        help="Overwrite locked fields too. The locks themselves stay in place.",
    )
    parser.add_argument(
        "--approve-overwrites",
        action="store_true",
        help="Write changes that replace a non-empty title, artist, album, albumartist, or date.",
    )
    parser.add_argument(
        "--isrc",
        choices=ISRC_POLICIES,
        default=None,
        help="Resolve ISRC conflicts: keep (and lock) the file codes, or take MusicBrainz's "
        "code where it lists exactly one.",
    )
    parser.add_argument(
        "--isrc-code",
        action="append",
        type=_parse_isrc_code,
        default=[],
        metavar="[DISC-]TRACK=CODE",
        help="Resolve one track's ISRC conflict to CODE. Overrides --isrc for that track.",
    )
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    mb_client = MusicBrainzClient()
    try:
        release = mb_client.fetch_release(args.release_id)
    finally:
        mb_client.close()

    result = build_diff(album, release, ignore_locks=args.ignore_locks)
    try:
        resolve_isrc_conflicts(result, policy=args.isrc, codes=dict(args.isrc_code))
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    _print_diff(result)

    if not result.has_changes and not any(diff.conflicts for diff in result.diffs):
        return

    gate = _print_gate(result)
    if args.dry_run:
        print("(dry run — skipping write)")
        if gate:
            sys.exit(gate)
        return

    if gate == EXIT_BLOCKED:
        print("Error: resolve the ISRC conflicts first; nothing written.", file=sys.stderr)
        sys.exit(EXIT_BLOCKED)
    if gate == EXIT_REVIEW and not args.approve_overwrites:
        print("Error: overwrites need --approve-overwrites; nothing written.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)

    try:
        count = apply_changes(result, ignore_locks=args.ignore_locks)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated {count} track(s).")

    log_path = _log_path(args)
    if log_path:
        _write_log_entry(log_path, result)


_COMMANDS: dict[str, tuple[str, object]] = {}


def _register_commands() -> dict[str, tuple[str, Callable[[list[str] | None], None]]]:
    """Build command table: name -> (help text, handler function)."""
    return {
        "read": ("Read album tags and durations as JSON.", _read),
        "toc": ("Parse rip artifacts for TOC / disc ID.", _toc),
        "mb": ("MusicBrainz query commands.", _mb),
        "match": ("Score candidates by duration match.", _match),
        "diff": ("Compute tag diff against a MusicBrainz release.", _diff),
        "write-tags": ("Apply tag changes from a diff JSON.", _write_tags),
        "write-ratings": ("Write rating/starred tags to audio files.", _write_ratings),
        "path-for": ("Compute library destination paths.", _path_for),
        "copy": ("Copy files to library per a placement plan.", _copy),
        "art": ("Fetch or install cover art for an album.", _art),
        "nd": ("Navidrome commands.", _nd),
        "scan": ("Generate a checklist of albums needing attention.", _scan),
        "checklist": ("Show the next album, or mark one done, skipped, or reopened.", _checklist),
        "candidates": ("Show MusicBrainz candidates for an album.", _print_candidates),
        "tag": ("Apply tags from a chosen MusicBrainz release.", _tag),
        "apply": ("Run tag, art, genre, rename, and replaygain on an album.", _apply),
        "place": ("Copy a finished album into the library.", _place),
        "genre": ("Set or show the genre meta-grouping tag.", _genre),
        "lock": ("Lock fields so `tag` keeps them, or show locks.", _lock),
        "unlock": ("Unlock fields so `tag` can change them.", _unlock),
        "rename": ("Rename files to 'NN. Title.ext' from tags.", _rename),
        "replaygain": ("Compute and write ReplayGain 2.0 tags.", _replaygain),
        "rip": ("Rip a CD to FLAC with bootstrap tags.", _rip),
    }


def _print_main_help() -> None:
    commands = _register_commands()
    print("usage: music-tagger <command> [options]\n")
    print("Fix album tags using MusicBrainz.\n")
    print("commands:")
    width = max(len(name) for name in commands)
    for name, (helptext, _) in commands.items():
        print(f"  {name:<{width}}  {helptext}")
    print("\nRun 'music-tagger <command> --help' for command-specific help.")


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    # Piped stdout is block-buffered, which hides progress until exit and puts
    # it after whipper's and rsgain's output. Line-buffer it.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    if not argv or argv[0] in ("-h", "--help"):
        _print_main_help()
        sys.exit(0 if argv else 1)

    commands = _register_commands()
    command = argv[0]
    remaining = argv[1:]

    if command in commands:
        _, handler = commands[command]
        handler(remaining)
    else:
        print(f"music-tagger: unknown command '{command}'", file=sys.stderr)
        _print_main_help()
        sys.exit(1)
