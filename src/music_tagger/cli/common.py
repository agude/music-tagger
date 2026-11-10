"""Helpers shared by the cli command modules: logs, gates, and warnings."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from ..coverart import ArtResult, EmbeddedCover
from ..placement import PlacementPlan
from ..tagger import AlbumResult, TrackDiff, overwrites_needing_review
from ..tags import AlbumTags, TrackTags

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


# Exit codes for `tag`, so the operator can branch without parsing output.
EXIT_REVIEW = 3  # An overwrite needs --approve-overwrites.


EXIT_BLOCKED = 4  # Unresolved conflicts; nothing can be written.


def _output_json(data: object, out: Path | None, digest: str) -> None:
    """Write JSON to file (printing digest to stdout) or JSON to stdout."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if out:
        out.write_text(text)
        print(digest)
    else:
        sys.stdout.write(text)


def _warn_unreadable(album: AlbumTags) -> None:
    """Warn about audio files that could not be read. Read-only commands stop here."""
    if album.unreadable:
        names = ", ".join(path.name for path in album.unreadable)
        print(f"Warning: {len(album.unreadable)} unreadable file(s): {names}", file=sys.stderr)


def _exit_if_unreadable(album: AlbumTags) -> None:
    """Warn, then exit 1 when an album has unreadable files. A partial read must not write."""
    if album.unreadable:
        _warn_unreadable(album)
        sys.exit(1)


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


def _check_gate(result: AlbumResult, *, approve_overwrites: bool, dry_run: bool) -> None:
    """Print the gate verdict, then exit unless the run may write. Shared by `tag` and `write-tags`.

    A dry run prints the verdict and exits with its code when it is not auto.
    """
    gate = _print_gate(result)
    if dry_run:
        print("(dry run — skipping write)")
        if gate:
            sys.exit(gate)
        return
    if gate == EXIT_BLOCKED:
        print("Error: resolve the ISRC conflicts first; nothing written.", file=sys.stderr)
        sys.exit(EXIT_BLOCKED)
    if gate == EXIT_REVIEW and not approve_overwrites:
        print("Error: overwrites need --approve-overwrites; nothing written.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)


def _print_album_warnings(result: AlbumResult) -> None:
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)


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
        new = change.new_value or "(cleared)"
        lines.append(f"    {change.field}: {old} → {new}")
    for change in diff.locked_changes:
        old = change.old_value or "(empty)"
        new = change.new_value or "(cleared)"
        lines.append(f"    {change.field}: {old} (locked; release has {new})")
    return lines


def _write_copy_log(log_path: Path, plan: PlacementPlan, result: object) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## Copy to {plan.album_dir}",
        "",
        f"- **Date:** {timestamp}",
        f"- **Files:** {len(plan.mappings)}",
        "",
    ]
    from ..placement import CopyResult

    if isinstance(result, CopyResult):
        for m in result.copied:
            lines.append(f"  {m.source.name} → {m.dest}")
    lines.append("")
    lines.append("---")
    lines.append("")
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


def _write_art_log(
    log_path: Path,
    album_dir: Path,
    source: str,
    result: ArtResult,
    cover: EmbeddedCover | None,
    embedded_count: int,
    *,
    failed: str | None = None,
) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    saved = f"{result.path} ({result.size_bytes / 1024:.0f} KB)" if result.saved else "no"
    if cover is None:
        embedded_text = "0 file(s)"
    else:
        conversion = ", converted to JPEG" if cover.converted else ""
        embedded_text = (
            f"{embedded_count} file(s) ({cover.mime}, {len(cover.data) / 1024:.0f} KB{conversion})"
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
        *([f"- **Failed:** {failed}"] if failed else []),
        "",
        "---",
        "",
    ]
    with open(log_path, "a") as f:
        f.write("\n".join(lines))


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
