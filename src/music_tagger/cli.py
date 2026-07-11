"""CLI entry point for music-tagger."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .musicbrainz import MusicBrainzClient
from .tags import AUDIO_EXTENSIONS, AlbumTags, read_album
from .discid import parse_rip_dir
from .musicbrainz import MBRelease
from .placement import PlacementPlan, compute_placement, copy_files
from .tagger import AlbumResult, apply_changes, build_diff, score_candidates, search_candidates


def _output_json(data: object, out: Path | None, digest: str) -> None:
    """Write JSON to file (printing digest to stdout) or JSON to stdout."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if out:
        out.write_text(text)
        print(digest)
    else:
        sys.stdout.write(text)


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


def _read(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger read",
        description="Read album tags and durations as JSON evidence.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
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

    lines = [f"{album.artist} — {album.album} ({album.track_count} tracks, {album.tracks[0].format.upper()})"]
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
        "-o", "--out", type=Path, default=None,
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
        "-o", "--out", type=Path, default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    mb_client = MusicBrainzClient()
    try:
        results = mb_client.search_releases(
            args.artist, args.album, format=args.format, limit=args.limit,
        )
    finally:
        mb_client.close()

    data = [r.to_dict() for r in results]

    lines = [f'{len(results)} candidates for "{args.artist}" — "{args.album}":']
    for r in results:
        info = f"  [{r.id}]  {r.title} ({r.date}) {r.country} / {r.label} / {r.format} / {r.track_count}t"
        lines.append(info)
    _output_json(data, args.out, "\n".join(lines))


def _mb_release(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb release",
        description="Fetch full release detail from MusicBrainz.",
    )
    parser.add_argument("release_id", help="MusicBrainz release UUID.")
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
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
        f"{release.date} / {release.country} / {release.label} / {release.catalognum or '—'} / {release.format}",
        f"{release.track_count} tracks, {disc_info}",
    ]
    _output_json(data, args.out, "\n".join(lines))


def _mb_discid(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb discid",
        description="Look up releases by disc ID or TOC offsets.",
    )
    parser.add_argument("discid", nargs="?", default=None, help="MusicBrainz disc ID.")
    parser.add_argument("--toc", default=None, help="TOC string for fuzzy lookup (from `toc` command).")
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
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
        lines.append(f"  [{r.id}]  {r.title} ({r.date}) {r.country} / {r.label} / {r.format} / {r.track_count}t")
    _output_json(data, args.out, "\n".join(lines))


def _mb(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb",
        description="MusicBrainz query commands.",
    )
    sub = parser.add_subparsers(dest="mb_command")
    sub.add_parser("search", help="Search for candidate releases.")
    sub.add_parser("release", help="Fetch full release detail.")
    sub.add_parser("discid", help="Look up by disc ID or TOC.")

    args, remaining = parser.parse_known_args(argv)

    if args.mb_command == "search":
        _mb_search(remaining)
    elif args.mb_command == "release":
        _mb_release(remaining)
    elif args.mb_command == "discid":
        _mb_discid(remaining)
    else:
        parser.print_help()
        sys.exit(1)


def _match(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger match",
        description="Score candidates by duration match against local evidence.",
    )
    parser.add_argument(
        "--evidence", type=Path, required=True,
        help="Evidence JSON from `read`.",
    )
    parser.add_argument(
        "--candidates", type=Path, required=True,
        help="Candidates JSON from `mb search`.",
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
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
        "--evidence", type=Path, required=True,
        help="Evidence JSON from `read`.",
    )
    parser.add_argument(
        "--release", type=Path, required=True,
        help="Release JSON from `mb release`.",
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    args = parser.parse_args(argv)

    album = AlbumTags.from_dict(json.loads(args.evidence.read_text()))
    release = MBRelease.from_dict(json.loads(args.release.read_text()))

    result = build_diff(album, release)
    data = result.to_dict()

    changed = [d for d in result.diffs if d.changes]
    lines = [f"Release: {release.title} [{release.id}]"]
    lines.append(f"{len(result.diffs)} tracks, {len(changed)} with changes:")
    for d in changed:
        fields = ", ".join(c.field for c in d.changes)
        lines.append(f"  {d.track.path.name}: {fields}")
    if not changed:
        lines.append("  (no changes)")
    _output_json(data, args.out, "\n".join(lines))


def _write_tags(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger write-tags",
        description="Apply tag changes from a diff JSON.",
    )
    parser.add_argument(
        "--diff", type=Path, required=True, dest="diff_file",
        help="Diff JSON from `diff` (possibly LLM-edited).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing.",
    )
    parser.add_argument(
        "--log", type=Path, default=None,
        help="Append changes to this log file.",
    )
    args = parser.parse_args(argv)

    result = AlbumResult.from_dict(json.loads(args.diff_file.read_text()))

    if not result.has_changes:
        print("No tag changes to apply.")
        return

    if args.dry_run:
        for d in result.diffs:
            if d.changes:
                print(f"  {d.track.path.name}:")
                for c in d.changes:
                    old = c.old_value or "(empty)"
                    print(f"    {c.field}: {old} → {c.new_value}")
        print("\n(dry run — skipping write)")
        return

    count = apply_changes(result)
    print(f"Updated {count} track(s).")

    if args.log:
        _write_log_entry(args.log, result)


def _write_log_entry(log_path: Path, result: AlbumResult) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## {result.release.title} [{result.release.id}]",
        f"",
        f"- **Date:** {timestamp}",
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
        description="Read album, search MusicBrainz, and score candidates.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
        help="Output JSON file (prints digest to stdout).",
    )
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

    matches = score_candidates(album, candidates)

    data = {
        "album": album.to_dict(),
        "matches": [m.to_dict() for m in matches],
    }

    lines = [
        f"{album.artist} — {album.album}  ({album.track_count} tracks)",
        f"",
        f"{len(matches)} candidates scored:",
    ]
    for m in matches:
        r = m.release
        s = m.stats
        count_ok = "=" if s.track_count_match else "!"
        lines.append(
            f"  [{r.id}]  {r.title} ({r.date}) {r.country} / {r.label} / {r.format}"
            f"  {s.tracks_within_2s}/{s.tracks_compared} within 2s  max_dev={s.max_deviation_secs:.1f}s"
            f"  count{count_ok}"
        )
    _output_json(data, args.out, "\n".join(lines))


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


def _path_for(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger path-for",
        description="Compute library destination paths for album files.",
    )
    parser.add_argument(
        "--evidence", type=Path, required=True,
        help="Evidence JSON from `read`.",
    )
    parser.add_argument(
        "--root", type=Path, default=None,
        help="Library root (default: /mnt/synology/media/music).",
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
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
        "--plan", type=Path, required=True, dest="plan_file",
        help="Plan JSON from `path-for` (possibly LLM-edited).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be copied without copying.",
    )
    parser.add_argument(
        "--log", type=Path, default=None,
        help="Append copy results to this log file.",
    )
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
        print(f"\n(dry run) Would copy {len(result.copied)} file(s) ({size_mb:.1f} MB) → {plan.album_dir}")
    else:
        print(f"Copied {len(result.copied)} file(s) ({size_mb:.1f} MB) → {plan.album_dir}")

    if result.failed:
        sys.exit(1)

    if args.log and not args.dry_run:
        _write_copy_log(args.log, plan, result)


def _write_copy_log(log_path: Path, plan: PlacementPlan, result: object) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## Copy to {plan.album_dir}",
        f"",
        f"- **Date:** {timestamp}",
        f"- **Files:** {len(plan.mappings)}",
        f"",
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


def _tag(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger tag",
        description="Fetch release, compute diff, and apply tags (wrapper over mb release + diff + write-tags).",
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
        _write_log_entry(args.log, result)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger",
        description="Fix album tags using MusicBrainz.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("read", help="Read album tags and durations as JSON.")
    sub.add_parser("toc", help="Parse rip artifacts for TOC / disc ID.")
    sub.add_parser("mb", help="MusicBrainz query commands.")
    sub.add_parser("match", help="Score candidates by duration match.")
    sub.add_parser("diff", help="Compute tag diff against a MusicBrainz release.")
    sub.add_parser("write-tags", help="Apply tag changes from a diff JSON.")
    sub.add_parser("path-for", help="Compute library destination paths.")
    sub.add_parser("copy", help="Copy files to library per a placement plan.")
    sub.add_parser("scan", help="Generate a checklist of albums needing attention.")
    sub.add_parser("candidates", help="Show MusicBrainz candidates for an album.")
    sub.add_parser("tag", help="Apply tags from a chosen MusicBrainz release.")

    args, remaining = parser.parse_known_args(argv)

    if args.command == "read":
        _read(remaining)
    elif args.command == "toc":
        _toc(remaining)
    elif args.command == "mb":
        _mb(remaining)
    elif args.command == "match":
        _match(remaining)
    elif args.command == "diff":
        _diff(remaining)
    elif args.command == "write-tags":
        _write_tags(remaining)
    elif args.command == "path-for":
        _path_for(remaining)
    elif args.command == "copy":
        _copy(remaining)
    elif args.command == "scan":
        _scan(remaining)
    elif args.command == "candidates":
        _print_candidates(remaining)
    elif args.command == "tag":
        _tag(remaining)
    else:
        parser.print_help()
        sys.exit(1)
