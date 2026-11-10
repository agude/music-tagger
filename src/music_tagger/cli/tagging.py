"""Tag commands: read, diff, tag, apply, and locks."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..coverart import UnsupportedImageError, embeddable_cover, find_cover
from ..musicbrainz import MBRelease, MusicBrainzClient
from ..tagger import (
    ISRC_POLICIES,
    AlbumResult,
    CandidateMatch,
    DuplicateTrackError,
    TrackDiff,
    apply_changes,
    build_diff,
    duplicate_track_keys,
    duplicate_track_message,
    resolve_isrc_conflicts,
    score_candidates,
    search_candidates,
)
from ..tags import (
    LOCKABLE_FIELDS,
    AlbumTags,
    TrackTags,
    disc_number,
    parse_position,
    read_album,
    write_locks,
)
from .common import (
    _add_log_arguments,
    _BatchLog,
    _check_gate,
    _exit_if_unreadable,
    _format_change_lines,
    _log_path,
    _output_json,
    _print_album_warnings,
    _print_diff,
    _print_gate,
    _print_tag_issues,
    _warn_unreadable,
    _write_lock_log,
)
from .library import _art, _genre, _mark_checklist_done, _rename, _replaygain


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

    try:
        result = build_diff(album, release)
    except DuplicateTrackError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    _print_album_warnings(result)
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
    parser.add_argument(
        "--approve-overwrites",
        action="store_true",
        help="Allow overwrites of non-empty title, artist, album, albumartist, or date.",
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
        _check_gate(result, approve_overwrites=args.approve_overwrites, dry_run=True)
        return

    _check_gate(result, approve_overwrites=args.approve_overwrites, dry_run=False)
    stale = _stale_changes(result)
    if stale:
        print("Error: a file changed since the diff was made; nothing written:", file=sys.stderr)
        for line in stale:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)

    count = _apply_and_log(result, args, ignore_locks=False)
    if count:
        print(f"Updated {count} track(s).")
    else:
        print("No tag changes to apply.")


def _tag_log_header(result: AlbumResult) -> list[str]:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return [
        f"## {result.release.title} [{result.release.id}]",
        "",
        f"- **Date:** {timestamp}",
        "",
    ]


def _log_tag_diff(log: _BatchLog, diff: TrackDiff) -> None:
    """Append one track's block of the tag log. Writes nothing for a track with no lines."""
    change_lines = _format_change_lines(diff)
    if not change_lines and not diff.conflicts:
        return
    log.add(f"  {diff.track.path.name}:")
    for line in change_lines:
        log.add(line)
    for conflict in diff.conflicts:
        candidates = ", ".join(conflict.candidate_values)
        kept = "; locked" if conflict.resolution == conflict.old_value else ""
        log.add(
            f"    {conflict.field} conflict: file={conflict.old_value}; "
            f"MusicBrainz=[{candidates}]; resolution={conflict.resolution}{kept}"
        )


def _apply_and_log(result: AlbumResult, args: argparse.Namespace, *, ignore_locks: bool) -> int:
    """Write the tags and log each track right after it is written. Returns the count.

    The log is closed on every path, so a failure partway leaves an entry for each
    track that was changed. Exits 1 on an unresolved conflict, a locked field, or a
    write error.
    """
    log = _BatchLog(_log_path(args), _tag_log_header(result))
    try:
        return apply_changes(
            result,
            ignore_locks=ignore_locks,
            on_written=lambda diff: _log_tag_diff(log, diff),
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        log.close()


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

    _warn_unreadable(album)
    matches = score_candidates(album, candidates)
    data = {"album": album.to_dict(), "matches": [m.to_dict() for m in matches]}
    if args.json:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return
    if args.out:
        args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if duplicates := duplicate_track_keys(album):
        print(f"Warning: duplicate track keys: {duplicate_track_message(duplicates)}")

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


def _stale_changes(result: AlbumResult) -> list[str]:
    """Each change whose old_value no longer matches the file, read fresh from disk."""
    stale: list[str] = []
    current_by_dir: dict[Path, dict[Path, TrackTags]] = {}
    for diff in result.diffs:
        if not diff.effective_changes:
            continue
        directory = diff.track.path.parent
        if directory not in current_by_dir:
            current_by_dir[directory] = {t.path: t for t in read_album(directory).tracks}
        current = current_by_dir[directory].get(diff.track.path)
        if current is None:
            stale.append(f"{diff.track.path.name}: file is missing")
            continue
        for change in diff.effective_changes:
            if change.multi:
                now_values = current.multi.get(change.field, ())
                if now_values != change.old_values:
                    stale.append(
                        f"{diff.track.path.name}: {change.field} is "
                        f"{'; '.join(now_values)!r}, the diff expects {change.old_value!r}"
                    )
                continue
            now = current.tags.get(change.field, "")
            if now != change.old_value:
                stale.append(
                    f"{diff.track.path.name}: {change.field} is {now!r}, "
                    f"the diff expects {change.old_value!r}"
                )
    return stale


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

    from ..genres import GENRE_LIST_PATH, load_canonical

    canonical = load_canonical()
    unknown = [genre for genre in args.genre if canonical and genre not in canonical]
    if unknown and not args.force_genre:
        print(f"Error: not on the canonical list: {', '.join(unknown)}", file=sys.stderr)
        print(f"See {GENRE_LIST_PATH}, or pass --force-genre.", file=sys.stderr)
        sys.exit(1)
    return cover_path


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
    field_list = ", ".join(sorted(fields))
    changed: list[TrackTags] = []
    try:
        _rewrite_locks(tracks, fields, locking=locking, dry_run=args.dry_run, changed=changed)
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Logged on failure too: the tracks listed are the ones whose locks were written.
        log_path = _log_path(args)
        if log_path and changed and not args.dry_run:
            _write_lock_log(log_path, album, f"{command.capitalize()}: {field_list}", changed)

    if args.dry_run:
        print(f"\n(dry run — {len(changed)} of {len(tracks)} tracks would change)")
        return
    print(f"{command.capitalize()}ed {field_list} on {len(changed)} track(s).")


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
        if (
            track_number is None
            or parse_position(track.tags.get("tracknumber", "")) == track_number
        )
        and (disc is None or disc_number(track) == disc)
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
    tracks: list[TrackTags],
    fields: set[str],
    *,
    locking: bool,
    dry_run: bool,
    changed: list[TrackTags],
) -> None:
    """Add or remove `fields` from each track's locks. Appends each changed track to `changed`.

    A track is appended only after its locks are written, so after a failure
    `changed` lists exactly the tracks that were written.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    _exit_if_unreadable(album)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    mb_client = MusicBrainzClient()
    try:
        release = mb_client.fetch_release(args.release_id)
    finally:
        mb_client.close()
    if not release.discs:
        print("Error: release has no audio media", file=sys.stderr)
        sys.exit(1)

    try:
        result = build_diff(album, release, ignore_locks=args.ignore_locks)
    except DuplicateTrackError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    _print_album_warnings(result)
    try:
        resolve_isrc_conflicts(result, policy=args.isrc, codes=dict(args.isrc_code))
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    _print_diff(result)

    if not result.has_changes and not any(diff.conflicts for diff in result.diffs):
        # Printed even with nothing to write, so a scripted caller always reads a verdict.
        _print_gate(result)
        return

    _check_gate(result, approve_overwrites=args.approve_overwrites, dry_run=args.dry_run)
    if args.dry_run:
        return

    count = _apply_and_log(result, args, ignore_locks=args.ignore_locks)
    print(f"Updated {count} track(s).")
