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
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import httpx
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
from .cue import missing_cue_references, rewrite_cue_references
from .discid import parse_rip_dir
from .lrclib import (
    LrclibClient,
    Outcome,
    Selection,
    TrackEvidence,
    Verdict,
    fetch_explicit,
    lookup,
)
from .lyrics import LyricsEntry, Source, inspect_track, lyric_sidecars
from .lyrics_batch import (
    AUTO_LENGTH_SECS,
    HAS_LYRICS,
    INSPECTION_LABEL,
    NEEDS_LOOKUP,
    QUEUED_OUTCOMES,
    AlbumReport,
    Candidate,
    Decision,
    PendingEntry,
    confident_choice,
    load_decisions,
    load_pending,
    merge_pending,
    render_pending,
    save_decisions,
    save_pending,
    undecided,
)
from .lyrics_embed import apply_embed, parse_lrc, plan_embed
from .lyrics_write import SidecarPlan, WriteStatus, apply_plan, plan_sidecar_write
from .musicbrainz import MBRelease, MusicBrainzClient
from .navidrome import NavidromeClient, SongRating
from .placement import FileMapping, PlacementPlan, compute_placement, copy_files
from .stats import SnapshotError, SongStats, StatsSnapshot, read_snapshot, write_snapshot_file
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
    MalformedStatsError,
    TrackTags,
    UnsupportedFileError,
    embed_cover_art,
    read_album,
    write_locks,
    write_rating_to_file,
    write_stats_to_file,
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


# write-stats lists at most this many missing files, then a count of the rest.
_MAX_LISTED_MISSING = 20
# write-stats processes only songs in this Navidrome library. Podcasts and
# audiobooks live in other libraries, whose paths are not under the music root.
_DEFAULT_LIBRARY = "Music Library"
_ALL_LIBRARIES = "all"


class _BatchLog:
    """Audit log for one run that writes entries as each file changes.

    The header is written with the first entry and the footer on close, so an
    interrupted run keeps the entries for the files it already changed. A None
    path writes nothing.
    """

    def __init__(self, path: Path | None, header: list[str]) -> None:
        self._path = path
        self._header = header
        self._file: IO[str] | None = None

    def add(self, line: str) -> None:
        if self._path is None:
            return
        if self._file is None:
            self._file = self._path.open("a")
            self._file.write("\n".join(self._header) + "\n")
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is None:
            return
        self._file.write("\n---\n")
        self._file.close()
        self._file = None


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
_DEFAULT_STATS_PATH = _DEFAULT_LIBRARY_ROOT / "stats.json"


def _output_json(data: object, out: Path | None, digest: str) -> None:
    """Write JSON to file (printing digest to stdout) or JSON to stdout."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if out:
        out.write_text(text)
        print(digest)
    else:
        sys.stdout.write(text)


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
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite files from an earlier rip of this disc in the output directory.",
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
            replace=args.replace,
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
    disc = f"Disc {result.disc_number} of {result.disc_total}" if result.is_multi_disc else "Disc"
    if accuraterip.needs_review:
        print(f"{disc} needs review: see the tracks above. Safe to eject.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)

    print(f"\n{disc} verified; safe to eject.")
    if result.is_multi_disc and (result.disc_number or 0) < (result.disc_total or 0):
        print(f"Next: insert disc {(result.disc_number or 0) + 1}, then run:")
        print(f"  uv run music-tagger rip {target}")
        return
    print("Next: review tags and candidates:")
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
        "stats": ("Export a complete statistics snapshot as JSON.", _nd_stats),
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
        "--artist",
        default=None,
        help="Show the genres on each of this artist's albums in the library.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_LIBRARY_ROOT,
        help=f"Library root for --list and --artist (default: {_DEFAULT_LIBRARY_ROOT}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing."
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    if args.artist is not None:
        _print_artist_genres(args.root, args.artist)
        return

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


def _print_artist_genres(root: Path, artist: str) -> None:
    from .genres import album_genres, find_artist_dirs, similar_artist_dirs

    artist_dirs = find_artist_dirs(root, artist)
    if not artist_dirs:
        print(f"No artist directory matches '{artist}' under {root}.")
        if similar := similar_artist_dirs(root, artist):
            print(f"Similar: {', '.join(similar)}")
        return

    album_counts: dict[str, int] = {}
    album_total = 0
    for artist_dir in artist_dirs:
        print(f"{artist_dir.name} ({artist_dir}):")
        for album in album_genres(artist_dir):
            album_total += 1
            print(f"  {album.album_dir.name}: {', '.join(album.genres) or '(no genre)'}")
            for genre in album.genres:
                album_counts[genre] = album_counts.get(genre, 0) + 1

    ranked = sorted(album_counts.items(), key=lambda item: (-item[1], item[0]))
    summary = ", ".join(f"{genre} ({count} of {album_total})" for genre, count in ranked)
    print(f"Genres by album count: {summary or 'none'}")


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


EXIT_LYRICS_UNRESOLVED = 3  # A requested write waits on a choice only the operator can make.
_LYRICS_UNRESOLVED = frozenset({Outcome.AMBIGUOUS, Outcome.REVIEW, Outcome.MISSING_METADATA})
_LYRICS_FAILED = frozenset({Outcome.PROVIDER_FAILURE, Outcome.RATE_LIMITED})


def _lyrics_usage_error(args: argparse.Namespace) -> str | None:
    """Return why the lyrics flags conflict, or None. Checked before any work starts."""
    modes = [
        flag
        for flag, on in (
            ("--lookup", args.lookup),
            ("--fetch", args.fetch),
            ("--embed", args.embed),
        )
        if on
    ]
    writing = args.fetch or args.embed
    if len(modes) > 1:
        return f"{' and '.join(modes)} cannot be combined; choose one mode"
    if args.dry_run and not writing:
        return "--dry-run needs --fetch or --embed; inspect and lookup never write"
    if args.replace and not writing:
        return "--replace needs --fetch or --embed; there is nothing to replace"
    if args.lyrics_id is not None and not (args.fetch or args.lookup):
        return "--lyrics-id needs --fetch or --lookup; it selects an LRCLIB record"
    if args.lyrics_id is not None and args.embed:
        return "--embed reads local sidecars only; it cannot take --lyrics-id"
    if args.lyrics_id is not None and args.track is None:
        return "--lyrics-id needs --track; a record must not apply to the whole album"
    if args.disc is not None and args.track is None:
        return "--disc needs --track"
    return None


def _lyrics_tracks(album: AlbumTags, track_no: int | None, disc: int | None) -> list[TrackTags]:
    if track_no is None:
        return list(album.tracks)
    matches = [
        t
        for t in album.tracks
        if _parse_track_number(t) == track_no and (disc is None or _parse_disc_number(t) == disc)
    ]
    if len(matches) != 1:
        where = f" on disc {disc}" if disc is not None else ""
        raise ValueError(f"track {track_no}{where} matches {len(matches)} tracks; need one")
    return matches


def _describe_lyric_entry(entry: LyricsEntry) -> str:
    if entry.source is Source.EMBEDDED:
        detail = " ".join(part for part in (entry.language, entry.description) if part)
        where = f"embedded ({detail})" if detail else "embedded"
    else:
        where = f"{entry.source.value} ({entry.path.name if entry.path else ''})"
    return f"{where}: {entry.kind.value}"


def _print_lyrics_inspection(tracks: list[TrackTags]) -> None:
    results = [inspect_track(track.path) for track in tracks]
    counts: dict[str, int] = {}
    for result in results:
        print(f"{result.path.name}: {result.status}")
        for entry in result.entries:
            print(f"    {_describe_lyric_entry(entry)}")
        for error in result.errors:
            print(f"    error: {error}")
        counts[result.status] = counts.get(result.status, 0) + 1
    print("\nCoverage: " + ", ".join(f"{s} {n}" for s, n in sorted(counts.items())))


def _print_lyrics_selection(track: TrackTags, selection: Selection) -> None:
    print(f"{track.path.name}: {selection.outcome.value}")
    print(f"    {selection.reason}")
    for verdict in selection.verdicts:
        mark = " (plausible)" if verdict.plausible else ""
        print(f"    record {verdict.record.id}{mark}: {'; '.join(verdict.notes)}")


def _fetch_lyrics_track(
    track: TrackTags,
    selection: Selection,
    *,
    replace: bool,
    dry_run: bool,
    log: _BatchLog,
) -> tuple[str, bool]:
    """Plan and, unless dry run, write one track's sidecar. Returns (label, failed).

    Only a SELECTED record reaches the writer. Any other outcome is labelled
    with that outcome and writes nothing.
    """
    if selection.outcome is not Outcome.SELECTED or selection.lyrics is None:
        return selection.outcome.value, False
    if selection.record is None:
        raise AssertionError("a selected lyric always has a record")

    plan: SidecarPlan = plan_sidecar_write(
        inspect_track(track.path),
        selection.lyrics.kind,
        selection.lyrics.text,
        replace=replace,
    )
    print(f"    {plan.status.value}: {plan.reason}")
    if plan.status is WriteStatus.REFUSED:
        return plan.status.value, True
    if plan.status is not WriteStatus.WRITE:
        return plan.status.value, False
    if dry_run:
        return "would write", False

    try:
        apply_plan(plan)
    except OSError as exc:
        print(f"    error: {exc}", file=sys.stderr)
        return "failed", True

    kind = selection.lyrics.kind.value
    if plan.content is not None and plan.target is not None:
        how = "replaced existing lyrics" if plan.replaces else "new file"
        log.add(
            f"- `{track.path.name}`: wrote `{plan.target.name}` from LRCLIB record "
            f"{selection.record.id} ({kind}, {how})"
        )
    for path in plan.remove:
        log.add(f"- `{track.path.name}`: removed stale `{path.name}`")
    return "wrote", False


def _embed_lyrics_track(
    track: TrackTags,
    *,
    replace: bool,
    dry_run: bool,
    log: _BatchLog,
) -> tuple[str, bool]:
    """Plan and, unless dry run, embed one track's sidecar. Returns (label, failed)."""
    plan = plan_embed(inspect_track(track.path), replace=replace)
    print(f"    {plan.status.value}: {plan.reason}")
    if plan.status is WriteStatus.REFUSED:
        return plan.status.value, True
    if plan.status is not WriteStatus.WRITE:
        return plan.status.value, False
    if dry_run:
        return "would write", False
    if plan.source is None:
        raise AssertionError("a write plan always has a source")

    try:
        apply_embed(plan)
    except (OSError, MutagenError) as exc:
        print(f"    error: {exc}", file=sys.stderr)
        return "failed", True

    how = "replaced existing lyrics" if plan.replaces else "new tag"
    log.add(f"- `{track.path.name}`: embedded `{plan.source.path.name}` as {plan.tag} ({how})")
    return "wrote", False


def _embed_lyrics(
    album: AlbumTags,
    tracks: list[TrackTags],
    *,
    replace: bool,
    dry_run: bool,
    log_path: Path | None,
) -> None:
    log = _BatchLog(log_path, _lyrics_log_header("## Lyrics embed", album))
    labels: dict[str, int] = {}
    failed = False
    try:
        for track in tracks:
            label, track_failed = _embed_lyrics_track(
                track, replace=replace, dry_run=dry_run, log=log
            )
            labels[label] = labels.get(label, 0) + 1
            failed = failed or track_failed
    finally:
        log.close()

    summary = ", ".join(f"{count} {label}" for label, count in sorted(labels.items()))
    print(f"\nSummary: {summary or 'no tracks'}")
    if dry_run:
        print("(dry run — nothing written)")
    if failed:
        sys.exit(1)


def _lyrics(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger lyrics",
        description="Inspect lyrics for an album, look up LRCLIB candidates, or write "
        "sidecar lyrics. With no mode, only inspects: no network, no writes.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "--lookup",
        action="store_true",
        help="Search LRCLIB and show each candidate with its evidence. "
        "Uses the network; writes nothing.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Choose LRCLIB lyrics and save them as <audio-stem>.lrc or .txt beside each track.",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Write each track's sidecar lyrics into its tags: .lrc if present, else .txt. "
        "A backup for when sidecars are lost. No network.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --fetch or --embed, show what would be written.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="With --fetch, replace existing lyrics and remove a stale sidecar of the other type. "
        "With --embed, replace a different value in the lyric tag.",
    )
    parser.add_argument("--track", type=int, metavar="N", help="Only track number N.")
    parser.add_argument(
        "--disc", type=int, metavar="D", help="Disc number D, with --track, on multi-disc albums."
    )
    parser.add_argument(
        "--lyrics-id",
        type=int,
        metavar="ID",
        help="Use LRCLIB record ID for the --track track instead of searching. Needs --track.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    if message := _lyrics_usage_error(args):
        parser.error(message)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)
    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)
    try:
        tracks = _lyrics_tracks(album, args.track, args.disc)
    except ValueError as exc:
        print(f"Error: {exc}.", file=sys.stderr)
        sys.exit(1)

    if args.embed:
        _embed_lyrics(
            album,
            tracks,
            replace=args.replace,
            dry_run=args.dry_run,
            log_path=None if args.dry_run else _log_path(args),
        )
        return
    if not args.lookup and not args.fetch:
        _print_lyrics_inspection(tracks)
        return

    client = LrclibClient()
    log_path = None if args.dry_run else _log_path(args)
    log = _BatchLog(log_path, _lyrics_log_header("## Lyrics", album))
    labels: dict[str, int] = {}
    failed = unresolved = False
    try:
        for track in tracks:
            evidence = _track_evidence(track)
            if args.lyrics_id is not None:
                selection = fetch_explicit(client, args.lyrics_id, evidence)
            else:
                selection = lookup(client, evidence)
            _print_lyrics_selection(track, selection)
            if selection.outcome in _LYRICS_FAILED:
                failed = True
            if args.fetch and selection.outcome in _LYRICS_UNRESOLVED:
                unresolved = True
            if args.fetch:
                label, track_failed = _fetch_lyrics_track(
                    track,
                    selection,
                    replace=args.replace,
                    dry_run=args.dry_run,
                    log=log,
                )
                failed = failed or track_failed
            else:
                label = selection.outcome.value
            labels[label] = labels.get(label, 0) + 1
    finally:
        log.close()

    summary = ", ".join(f"{count} {label}" for label, count in sorted(labels.items()))
    print(f"\nSummary: {summary or 'no tracks'}")
    if args.dry_run:
        print("(dry run — nothing written)")
    if failed:
        sys.exit(1)
    if unresolved:
        sys.exit(EXIT_LYRICS_UNRESOLVED)


def _track_evidence(track: TrackTags) -> TrackEvidence:
    return TrackEvidence(
        title=track.tags.get("title", ""),
        artist=track.tags.get("artist", ""),
        album=track.tags.get("album", ""),
        duration_secs=track.duration_secs or None,
    )


def _lyrics_log_header(title: str, album: AlbumTags) -> list[str]:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return [
        title,
        "",
        f"- **Date:** {stamp}",
        f"- **Album:** {album.artist} — {album.album}",
        "",
    ]


_PENDING_ENV_VAR = "MUSIC_TAGGER_LYRICS_PENDING"
_DECISIONS_ENV_VAR = "MUSIC_TAGGER_LYRICS_DECISIONS"


def _state_path(override: Path | None, env_var: str, name: str) -> Path:
    if override is not None:
        return override
    value = os.environ.get(env_var)
    return Path(value) if value else Path.home() / "Projects/music-tagger" / name


def _add_state_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pending",
        type=Path,
        default=None,
        help=f"Pending options file (default: ${_PENDING_ENV_VAR}, "
        "else ~/Projects/music-tagger/lyrics-pending.json).",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=None,
        help=f"Decisions file (default: ${_DECISIONS_ENV_VAR}, "
        "else ~/Projects/music-tagger/lyrics-decisions.json).",
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class _RateLimited(Exception):
    """LRCLIB refused requests for longer than the client waits. The batch stops."""


def _candidate(verdict: Verdict, evidence: TrackEvidence) -> Candidate:
    record = verdict.record
    delta: float | None = None
    if record.duration_secs is not None and evidence.duration_secs is not None:
        delta = round(record.duration_secs - evidence.duration_secs, 1)
    last_line_delta: float | None = None
    if record.synced_lyrics and evidence.duration_secs is not None:
        stamps = parse_lrc(record.synced_lyrics)
        if stamps:
            last_line_delta = round(max(ms for _, ms in stamps) / 1000 - evidence.duration_secs, 1)
    offers: tuple[str, ...]
    if record.instrumental:
        offers = ("instrumental",)
    else:
        offers = tuple(
            label
            for label, text in (("synced", record.synced_lyrics), ("plain", record.plain_lyrics))
            if text
        )
    return Candidate(
        id=record.id,
        title=record.track_name,
        artist=record.artist_name,
        album=record.album_name,
        duration_delta=delta,
        plausible=verdict.plausible,
        album_matches=verdict.album_matches,
        offers=offers,
        notes=verdict.notes,
        last_line_delta=last_line_delta,
    )


def _pending_entry(
    track: TrackTags,
    album_dir: Path,
    outcome: str,
    reason: str,
    candidates: tuple[Candidate, ...],
) -> PendingEntry:
    return PendingEntry(
        album=str(album_dir),
        file=track.path.name,
        track=_parse_track_number(track),
        disc=_parse_disc_number(track),
        outcome=outcome,
        reason=reason,
        candidates=candidates,
    )


def _batch_fetch_track(
    track: TrackTags,
    client: LrclibClient,
    report: AlbumReport,
    *,
    decisions: dict[tuple[str, str], Decision],
    dry_run: bool,
    log: _BatchLog,
) -> None:
    """Fetch one track's sidecar when exactly one record is certain; save the rest."""
    if (str(report.directory), track.path.name) in decisions:
        report.counts["decided"] += 1
        return
    status = inspect_track(track.path).status
    if status in HAS_LYRICS:
        report.counts["has lyrics"] += 1
        return
    if status not in NEEDS_LOOKUP:
        report.counts[INSPECTION_LABEL] += 1
        report.pending.append(
            _pending_entry(
                track, report.directory, INSPECTION_LABEL, f"lyric source is {status}", ()
            )
        )
        return

    evidence = _track_evidence(track)
    selection = lookup(client, evidence)
    _print_lyrics_selection(track, selection)
    if selection.outcome is Outcome.RATE_LIMITED:
        raise _RateLimited(f"{track.path.name}: {selection.reason}")
    if selection.outcome is Outcome.NO_MATCH:
        # A record can match the album and differ in length from this file. Keep those
        # for a decision, with the last-line check as evidence; drop the rest.
        hits = tuple(_candidate(v, evidence) for v in selection.verdicts if v.album_matches)
        if hits:
            report.counts["no_match pending"] += 1
            report.pending.append(
                _pending_entry(
                    track,
                    report.directory,
                    "no_match",
                    f"{selection.reason}; album matches are listed for a decision",
                    hits,
                )
            )
            return
    if selection.outcome.value in QUEUED_OUTCOMES:
        report.counts[selection.outcome.value] += 1
        candidates = tuple(_candidate(v, evidence) for v in selection.verdicts)
        report.pending.append(
            _pending_entry(
                track, report.directory, selection.outcome.value, selection.reason, candidates
            )
        )
        return

    if selection.outcome is Outcome.SELECTED:
        label, failed = _fetch_lyrics_track(
            track, selection, replace=False, dry_run=dry_run, log=log
        )
    else:
        label, failed = selection.outcome.value, selection.outcome is Outcome.PROVIDER_FAILURE
    report.counts[label] += 1
    report.failed = report.failed or failed


def _choose_record(
    album_dir: Path,
    track: TrackTags,
    entry: PendingEntry,
    record_id: int,
    *,
    client: LrclibClient,
    decisions: dict[tuple[str, str], Decision],
    decisions_path: Path,
    dry_run: bool,
    log: _BatchLog,
    plain_only: bool = False,
) -> tuple[bool, str]:
    """Write one saved record's lyrics for a pending track and record the decision.

    Returns (ok, message). A decision is recorded only when the sidecar is written or
    already matches. A dry run records nothing. plain_only writes the record's plain
    text. A decision that replaces an earlier skip keeps that skip's reason.
    """
    selection = fetch_explicit(client, record_id, _track_evidence(track), plain_only=plain_only)
    if selection.outcome is not Outcome.SELECTED:
        reason = selection.outcome.value
        return False, f"{track.path.name}: record {record_id} cannot be written: {reason}"
    label, failed = _fetch_lyrics_track(track, selection, replace=False, dry_run=dry_run, log=log)
    if dry_run:
        return label == "would write", f"{track.path.name}: {label} (record {record_id})"
    if failed or label not in ("wrote", "unchanged"):
        return False, f"{track.path.name}: {label}; no decision recorded"
    key = (str(album_dir), track.path.name)
    superseded = decisions.get(key)
    reason = f"chose record {record_id}" + (" (plain text)" if plain_only else "")
    if superseded is not None:
        reason += f"; replaces skip: {superseded.reason}"
    decisions[key] = Decision(
        album=str(album_dir),
        file=track.path.name,
        track=entry.track,
        choice=record_id,
        reason=reason,
        decided=_now(),
    )
    save_decisions(decisions_path, decisions)
    return True, f"{track.path.name}: recorded record {record_id}"


def _auto_choose(
    report: AlbumReport,
    album: AlbumTags,
    client: LrclibClient,
    *,
    decisions: dict[tuple[str, str], Decision],
    decisions_path: Path,
    dry_run: bool,
    log: _BatchLog,
) -> None:
    """Record the choices the rules accept without a judgment. The rest stay pending."""
    tracks = {track.path.name: track for track in album.tracks}
    for entry in list(report.pending):
        candidate = confident_choice(entry)
        if candidate is None:
            continue
        ok, message = _choose_record(
            report.directory,
            tracks[entry.file],
            entry,
            candidate.id,
            client=client,
            decisions=decisions,
            decisions_path=decisions_path,
            dry_run=dry_run,
            log=log,
        )
        print(f"    auto: {message}")
        if not ok:
            report.counts["auto not written"] += 1
            report.failed = True
        else:
            report.counts["auto would choose" if dry_run else "auto chosen"] += 1
            report.pending.remove(entry)


def _batch_album(
    album_dir: Path,
    client: LrclibClient,
    *,
    decisions: dict[tuple[str, str], Decision],
    decisions_path: Path,
    auto: bool,
    embed: bool,
    dry_run: bool,
    log_path: Path | None,
) -> AlbumReport:
    """Fetch missing sidecars, record the confident choices, then embed when asked."""
    report = AlbumReport(album_dir)
    try:
        album = read_album(album_dir)
    except (OSError, MutagenError, ValueError) as exc:
        report.failed = True
        report.error = str(exc)
        return report

    fetch_log = _BatchLog(log_path, _lyrics_log_header("## Lyrics", album))
    try:
        for track in album.tracks:
            _batch_fetch_track(
                track, client, report, decisions=decisions, dry_run=dry_run, log=fetch_log
            )
        if auto:
            _auto_choose(
                report,
                album,
                client,
                decisions=decisions,
                decisions_path=decisions_path,
                dry_run=dry_run,
                log=fetch_log,
            )
    finally:
        fetch_log.close()

    if embed:
        # Embedding runs after the fetch and the choices, so new sidecars are embedded too.
        embed_log = _BatchLog(log_path, _lyrics_log_header("## Lyrics embed", album))
        try:
            for track in album.tracks:
                # A track without a sidecar has nothing to embed. In a dry run, a
                # sidecar that would be fetched is not on disk yet, so it is skipped too.
                if not lyric_sidecars(track.path):
                    continue
                label, failed = _embed_lyrics_track(
                    track, replace=False, dry_run=dry_run, log=embed_log
                )
                report.counts[f"embed {label}"] += 1
                report.failed = report.failed or failed
        finally:
            embed_log.close()
    return report


def _print_album_report(root: Path, report: AlbumReport) -> None:
    name = report.directory.name if report.directory == root else report.directory.relative_to(root)
    if report.error:
        print(f"{name}: error: {report.error}", file=sys.stderr)
        return
    summary = ", ".join(f"{n} {label}" for label, n in sorted(report.counts.items()))
    print(f"{name}: {summary or 'nothing to do'}")


def _lyrics_batch(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger lyrics-batch",
        description="Backfill lyrics across a library. Each track without lyrics gets a "
        "sidecar only when exactly one LRCLIB record is certain. Every other track is "
        "saved to the pending file with its candidate records, for lyrics-decide. "
        "With --embed, sidecars are also written into tags. Tracks that already have "
        "lyrics, or a recorded decision, are never searched.",
    )
    parser.add_argument(
        "path", type=Path, help="Library root, artist directory, or album directory."
    )
    parser.add_argument(
        "--embed", action="store_true", help="Also write each track's sidecar into its tags."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written. Writes no sidecars, tags, log, or pending file.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Also record the choices the rules accept without a judgment: a plausible "
        "album match with synchronized text, or an album match within "
        f"{AUTO_LENGTH_SECS:.0f} s whose last line falls inside the audio. The rest stay pending.",
    )
    _add_state_arguments(parser)
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    root: Path = args.path.resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)
    album_dirs = _find_album_dirs(root)
    if not album_dirs:
        print(f"Error: no audio files under {root}.", file=sys.stderr)
        sys.exit(1)

    pending_path = _state_path(args.pending, _PENDING_ENV_VAR, "lyrics-pending.json")
    decisions_path = _state_path(args.decisions, _DECISIONS_ENV_VAR, "lyrics-decisions.json")
    try:
        decisions = load_decisions(decisions_path)
        previous = load_pending(pending_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    log_path = None if args.dry_run else _log_path(args)
    client = LrclibClient()
    reports: list[AlbumReport] = []
    stopped = ""
    try:
        for album_dir in album_dirs:
            report = _batch_album(
                album_dir,
                client,
                decisions=decisions,
                decisions_path=decisions_path,
                auto=args.auto,
                embed=args.embed,
                dry_run=args.dry_run,
                log_path=log_path,
            )
            reports.append(report)
            _print_album_report(root, report)
    except _RateLimited as exc:
        stopped = str(exc)

    # Albums this run could not read keep their saved entries; albums it processed
    # are replaced, so a decided or settled track leaves the pending file.
    processed = {str(r.directory) for r in reports if not r.error}
    fresh = [entry for r in reports if not r.error for entry in r.pending]
    queued = sum(len(r.pending) for r in reports)
    if not args.dry_run:
        merged = merge_pending(previous, fresh, processed)
        save_pending(pending_path, merged, _now())
        print(f"Pending: {len(merged)} tracks in {pending_path}")

    totals: Counter[str] = Counter()
    for report in reports:
        totals.update(report.counts)
    failed = any(report.failed for report in reports)
    print(f"\nAlbums: {len(reports)} of {len(album_dirs)} processed")
    print("Tracks: " + (", ".join(f"{n} {k}" for k, n in sorted(totals.items())) or "none"))
    print(f"Pending this run: {queued}")
    if args.dry_run:
        print("(dry run — nothing written)")
    if stopped:
        print(f"Stopped on LRCLIB rate limit at {stopped}. Rerun to finish.", file=sys.stderr)
    if stopped or failed:
        sys.exit(1)
    if queued:
        sys.exit(EXIT_LYRICS_UNRESOLVED)


def _track_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "--track",
        type=int,
        metavar="N",
        help="Track number N. Use --file when two files share a number.",
    )
    parser.add_argument(
        "--disc", type=int, metavar="D", help="Disc number D, on multi-disc albums."
    )
    parser.add_argument(
        "--file",
        metavar="NAME",
        help="Audio file name, as lyrics-decide list shows it. Selects the file directly.",
    )


def _select_track(album: AlbumTags, args: argparse.Namespace) -> TrackTags:
    """Return the one track a decision names: by file name when given, else by number."""
    if args.file is None:
        return _lyrics_tracks(album, args.track, args.disc)[0]
    named = [t for t in album.tracks if t.path.name == args.file]
    if len(named) != 1:
        raise ValueError(f"no audio file named {args.file!r} in this album")
    track = named[0]
    if args.track is not None and _parse_track_number(track) != args.track:
        raise ValueError(f"{args.file!r} is not track {args.track}")
    if args.disc is not None and _parse_disc_number(track) != args.disc:
        raise ValueError(f"{args.file!r} is not on disc {args.disc}")
    return track


def _may_replace_skip(args: argparse.Namespace, decided: Decision) -> bool:
    """A choice with --replace-skip may replace a skip. Nothing replaces a choice."""
    return decided.choice is None and bool(getattr(args, "replace_skip", False))


def _resolve_pending_track(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    pending_path: Path,
    decisions: dict[tuple[str, str], Decision],
) -> tuple[Path, AlbumTags, TrackTags, PendingEntry]:
    """Find the one track a decision names. Refuses with exit 2 when it is not pending."""
    if args.track is None and args.file is None:
        parser.error("give --track N or --file NAME")
    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)
    album = read_album(target)
    try:
        track = _select_track(album, args)
    except ValueError as exc:
        print(f"Error: {exc}.", file=sys.stderr)
        sys.exit(2)
    key = (str(target), track.path.name)
    if key in decisions and not _may_replace_skip(args, decisions[key]):
        decided = decisions[key]
        what = "skipped" if decided.choice is None else f"record {decided.choice}"
        print(f"Error: {track.path.name} is already decided ({what}).", file=sys.stderr)
        sys.exit(2)
    entry = next(
        (
            e
            for e in load_pending(pending_path)
            if e.album == str(target) and e.file == track.path.name
        ),
        None,
    )
    if entry is None:
        print(
            f"Error: {track.path.name} has no pending options; run lyrics-batch first.",
            file=sys.stderr,
        )
        sys.exit(2)
    return target, album, track, entry


def _lyrics_decide(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger lyrics-decide",
        description="Show the lyric options lyrics-batch saved, and record a choice or a "
        "skip for each track. A choice must be one of the saved record IDs and is written "
        "as a sidecar through the lyrics --lyrics-id path.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser(
        "list", help="Show undecided tracks and their options. No network."
    )
    listing.add_argument("--album", type=Path, default=None, help="Only this album directory.")
    _add_state_arguments(listing)

    choose = actions.add_parser("choose", help="Write one saved record's lyrics for a track.")
    _track_arguments(choose)
    choose.add_argument(
        "--id",
        dest="record_id",
        type=int,
        required=True,
        metavar="ID",
        help="A record ID from the saved options.",
    )
    choose.add_argument(
        "--plain",
        action="store_true",
        help="Write the record's plain text as a .txt, even when it has synchronized text.",
    )
    choose.add_argument(
        "--replace-skip",
        action="store_true",
        help="Replace an earlier skip for this track. A recorded choice is never replaced.",
    )
    choose.add_argument(
        "--dry-run", action="store_true", help="Show what would be written; record nothing."
    )
    _add_state_arguments(choose)
    _add_log_arguments(choose)

    skip = actions.add_parser("skip", help="Record that a track gets no lyrics. Writes no file.")
    _track_arguments(skip)
    skip.add_argument("--reason", required=True, help="Why the track has no lyrics.")
    _add_state_arguments(skip)

    args = parser.parse_args(argv)
    pending_path = _state_path(args.pending, _PENDING_ENV_VAR, "lyrics-pending.json")
    decisions_path = _state_path(args.decisions, _DECISIONS_ENV_VAR, "lyrics-decisions.json")
    try:
        decisions = load_decisions(decisions_path)
        if args.action == "list":
            entries = undecided(load_pending(pending_path), decisions)
            if args.album is not None:
                wanted = str(args.album.resolve())
                entries = [e for e in entries if e.album == wanted]
            print(render_pending(entries), end="")
            return
        target, album, track, entry = _resolve_pending_track(parser, args, pending_path, decisions)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.action == "skip":
        reason = args.reason.strip()
        if not reason:
            parser.error("--reason must not be empty")
        decisions[(str(target), track.path.name)] = Decision(
            album=str(target),
            file=track.path.name,
            track=entry.track,
            choice=None,
            reason=reason,
            decided=_now(),
        )
        save_decisions(decisions_path, decisions)
        print(f"{track.path.name}: skipped. {reason}")
        return

    saved = {c.id: c for c in entry.candidates}
    if args.record_id not in saved:
        ids = ", ".join(str(i) for i in sorted(saved)) or "none"
        print(
            f"Error: record {args.record_id} is not a saved option for {track.path.name} "
            f"(saved: {ids}).",
            file=sys.stderr,
        )
        sys.exit(2)

    client = LrclibClient()
    log = _BatchLog(
        None if args.dry_run else _log_path(args), _lyrics_log_header("## Lyrics", album)
    )
    try:
        ok, message = _choose_record(
            target,
            track,
            entry,
            args.record_id,
            client=client,
            decisions=decisions,
            decisions_path=decisions_path,
            dry_run=args.dry_run,
            log=log,
            plain_only=args.plain,
        )
    finally:
        log.close()
    print(message, file=sys.stdout if ok else sys.stderr)
    if args.dry_run:
        print("(dry run — nothing written)")
    if not ok:
        sys.exit(1)


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
    sidecar_renames: list[tuple[str, str]] = []
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

        # Lyric sidecars move with their audio. Every target is checked before
        # anything moves, so a conflict never leaves audio renamed and its
        # sidecar behind.
        moves = [(s, s.with_name(new_path.stem + s.suffix)) for s in lyric_sidecars(track.path)]
        blocked = next((dst for _, dst in moves if dst.exists()), None)
        if blocked is not None:
            print(f"  {track.path.name}: skipped ('{blocked.name}' already exists)")
            continue

        if args.dry_run:
            print(f"  {track.path.name} -> {new_name}")
            for src, dst in moves:
                print(f"  {src.name} -> {dst.name}")
        else:
            track.path.rename(new_path)
            print(f"  {track.path.name} -> {new_name}")
            renames.append((track.path.name, new_name))
            for src, dst in moves:
                src.rename(dst)
                print(f"  {src.name} -> {dst.name}")
                sidecar_renames.append((src.name, dst.name))

    if args.dry_run:
        print(f"\n(dry run — {len(album.tracks)} tracks)")
        return

    print(f"Renamed {len(renames)} file(s).")
    if sidecar_renames:
        print(f"Renamed {len(sidecar_renames)} lyric sidecar(s).")
    cues = _update_cue_sheets(target, dict(renames))
    log_path = _log_path(args)
    if log_path and renames:
        _write_rename_log(log_path, album, renames, cues, sidecar_renames)


def _update_cue_sheets(album_dir: Path, renames: dict[str, str]) -> list[Path]:
    """Rewrite CUE FILE lines for renamed files and warn about dangling ones."""
    cues = rewrite_cue_references(album_dir, renames) if renames else []
    for cue in cues:
        print(f"Updated CUE: {cue.name}")
    for cue, name in missing_cue_references(album_dir):
        print(f"Warning: {cue.name} references missing file '{name}'.", file=sys.stderr)
    return cues


def _write_rename_log(
    log_path: Path,
    album: AlbumTags,
    renames: list[tuple[str, str]],
    cues: list[Path],
    sidecars: list[tuple[str, str]],
) -> None:
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
    for old, new in sidecars:
        lines.append(f"- Lyric sidecar `{old}` → `{new}`")
    for cue in cues:
        lines.append(f"- CUE updated: `{cue.name}`")
    lines += ["", "---", ""]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _write_cue_log(log_path: Path, album_dir: Path, cues: list[Path]) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "## CUE update",
        "",
        f"- **Date:** {timestamp}",
        f"- **Album:** {album_dir}",
        *(f"- `{cue.name}`: FILE lines point at the placed file names" for cue in cues),
        "",
        "---",
        "",
    ]
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

    renamed_audio = {
        mapping.source.name: mapping.dest.name
        for mapping in plan.mappings
        if mapping.source.suffix.lower() in AUDIO_EXTENSIONS
        and mapping.source.name != mapping.dest.name
    }
    cues = _update_cue_sheets(plan.album_dir, renamed_audio)
    if log_path and cues:
        _write_cue_log(log_path, plan.album_dir, cues)

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
        "write-ratings": ("Write rating tags to audio files.", _write_ratings),
        "write-stats": ("Write ratings and play counts from a snapshot.", _write_stats),
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
        "lyrics": ("Inspect, look up, or write lyric sidecars for an album.", _lyrics),
        "lyrics-batch": (
            "Backfill lyrics across a library and save tracks that need a decision.",
            _lyrics_batch,
        ),
        "lyrics-decide": (
            "Show saved lyric options and record a choice or skip for each track.",
            _lyrics_decide,
        ),
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
