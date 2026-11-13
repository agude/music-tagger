"""Library commands: scan, checklist, place, copy, rename, art, genre, and ReplayGain."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mutagen import MutagenError

from ..checklist import Checklist, ChecklistEntry
from ..coverart import (
    ArtResult,
    EmbeddedCover,
    UnsupportedImageError,
    embeddable_cover,
    fetch_cover_art,
    find_cover,
    image_dimensions,
    use_local_cover,
)
from ..cue import missing_cue_references, rewrite_cue_references
from ..lyrics import lyric_sidecars
from ..placement import (
    FileMapping,
    PlacementConflictError,
    PlacementPlan,
    compute_placement,
    conflict_message,
    copy_files,
    find_destination_conflicts,
    remove_copied_sources,
)
from ..tags import (
    AUDIO_EXTENSIONS,
    AlbumTags,
    EmbedResult,
    TrackTags,
    embed_cover_art,
    list_audio,
    read_album,
)
from .common import (
    _DEFAULT_LIBRARY_ROOT,
    _add_log_arguments,
    _exit_if_unreadable,
    _log_path,
    _output_json,
    _warn_unreadable,
    _write_art_log,
    _write_copy_log,
    _write_cue_log,
    _write_genre_log,
    _write_rename_log,
    _write_replaygain_log,
)


def _default_checklist_path() -> Path:
    override = os.environ.get("MUSIC_TAGGER_CHECKLIST")
    return Path(override) if override else Path.home() / "Projects/music-tagger/checklist.md"


def _is_album_dir(path: Path) -> bool:
    return bool(list_audio(path).files)


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
        _warn_unreadable(album)
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
    if args.action == "reopen" and args.matching is not None and not args.matching.strip():
        reopen.error("--matching needs text; a blank pattern would reopen every skip")

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

    try:
        plan = compute_placement(album, **kwargs)
    except PlacementConflictError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
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
    if collisions := find_destination_conflicts(plan.mappings):
        print(f"Error: {conflict_message(collisions)}", file=sys.stderr)
        sys.exit(1)
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

    # Logged before the exit, so the copied files are recorded even when some failed.
    log_path = _log_path(args)
    if log_path and not args.dry_run:
        _write_copy_log(log_path, plan, result)

    if result.failed:
        sys.exit(1)


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

    progress = _ArtProgress()
    failure: Exception | None = None
    try:
        _install_and_embed(target, args, progress)
    except (UnsupportedImageError, MutagenError, OSError) as error:
        failure = error

    # Logged on failure too: the cover may already be saved, and some files embedded.
    log_path = _log_path(args)
    if log_path and progress.result is not None and (progress.result.saved or progress.cover):
        source = (
            str(args.cover_file)
            if args.cover_file
            else f"Cover Art Archive release {args.release_id}"
        )
        _write_art_log(
            log_path,
            target,
            source,
            progress.result,
            progress.cover,
            len(progress.embedded),
            failed=None if failure is None else str(failure),
        )
    if failure is not None:
        print(f"Error: cover art failed: {failure}", file=sys.stderr)
        sys.exit(1)


class _ArtProgress:
    """What `art` has changed so far, kept even when a later step fails."""

    def __init__(self) -> None:
        self.result: ArtResult | None = None
        self.cover: EmbeddedCover | None = None
        self.embedded: list[Path] = []


def _install_and_embed(target: Path, args: argparse.Namespace, progress: _ArtProgress) -> None:
    """Install the folder cover, then embed it when --embed is set.

    Records the install result and, when embedding runs, the image and each file
    embedded, in `progress`.
    """
    if args.cover_file:
        result = use_local_cover(args.cover_file.resolve(), target, force=args.force)
    else:
        result = fetch_cover_art(args.release_id, target, full_size=args.full, force=args.force)

    progress.result = result
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
        return
    cover = embeddable_cover(result.path)
    progress.cover = cover
    embed = embed_cover_art(target, cover.data, cover.mime, on_written=progress.embedded.append)
    converted = f" (converted to JPEG, {len(cover.data) / 1024:.0f} KB)" if cover.converted else ""
    print(f"Embedded cover art into {embed.count} file(s){converted}.")
    _print_embed_skips(embed)


def _print_embed_skips(embed: EmbedResult) -> None:
    """List each file that did not get the cover and why."""
    skipped = [(path, "M4A: no cover support") for path in embed.m4a]
    skipped += [(path, "not a regular file") for path in embed.other]
    skipped += [(path, "unreadable") for path in embed.unreadable]
    if not skipped:
        return
    print(f"Skipped {len(skipped)} file(s):")
    for path, reason in skipped:
        print(f"  {path.name} ({reason})")


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


def _scan_library_genres(root: Path) -> tuple[list[tuple[str, int]], list[Path]]:
    """Count the genres on each readable audio file under root, skipping hidden directories.

    Returns (genre, count) sorted by count descending, and the files that could not be read.
    """
    from ..tags import read_genres

    counts: Counter[str] = Counter()
    unreadable: list[Path] = []
    for directory, dirnames, _filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        listing = list_audio(Path(directory))
        unreadable.extend(listing.other)
        for path in listing.files:
            try:
                genres = read_genres(path)
            except (MutagenError, OSError):
                unreadable.append(path)
                continue
            counts.update(genre for genre in genres if genre)
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return ranked, unreadable


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
        "--track",
        type=int,
        action="append",
        dest="tracks",
        default=[],
        metavar="N",
        help="Limit --add or --remove to this track number (repeatable).",
    )
    parser.add_argument(
        "--disc", type=int, default=None, help="Disc number for --track on a multi-disc album."
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
        genres, unreadable = _scan_library_genres(args.root)
        if unreadable:
            names = ", ".join(path.name for path in unreadable)
            print(f"Warning: {len(unreadable)} unreadable file(s): {names}", file=sys.stderr)
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
    if args.tracks and not (args.add or args.remove):
        parser.error("--track needs --add or --remove")
    if args.disc is not None and not args.tracks:
        parser.error("--disc needs --track")

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    from ..genres import GENRE_LIST_PATH, load_canonical
    from ..tags import read_genres, write_genres

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

    targets = album.tracks
    if args.tracks:
        from ..tags import disc_number

        if args.disc is None and len({disc_number(t) for t in album.tracks}) > 1:
            parser.error("--track on a multi-disc album needs --disc")
        try:
            targets = _select_tracks(album.tracks, args.tracks, args.disc)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    log_path = _log_path(args)
    try:
        for track in targets:
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
                try:
                    write_genres(track.path, new, timestamp=stamp)
                except OSError as error:
                    print(f"Error: {track.path}: {error}", file=sys.stderr)
                    sys.exit(1)
            count += 1
    finally:
        # Logged even when a write fails, so the count matches what is on disk.
        if log_path and not args.dry_run and count > 0:
            on_disk = sorted({g for track in targets for g in read_genres(track.path)})
            _write_genre_log(log_path, album, str(on_disk), count)

    applied = sorted({g for track in targets for g in read_genres(track.path)})
    if args.dry_run:
        print(f"\n(dry run — {count} of {len(targets)} tracks would change)")
    else:
        print(f"Set genre={applied} on {count} track(s).")


def _select_tracks(
    tracks: list[TrackTags], numbers: list[int], disc: int | None
) -> list[TrackTags]:
    """The tracks numbered in `numbers`, on `disc` when one is given.

    Raises ValueError when a number matches no track, so a typo cannot pass silently.
    """
    from ..tags import disc_number, parse_position

    chosen: list[TrackTags] = []
    for number in dict.fromkeys(numbers):
        matches = [
            track
            for track in tracks
            if parse_position(track.tags.get("tracknumber")) == number
            and (disc is None or disc_number(track) == disc)
        ]
        if not matches:
            where = f" on disc {disc}" if disc is not None else ""
            raise ValueError(f"no track {number}{where}")
        chosen.extend(matches)
    return chosen


def _print_artist_genres(root: Path, artist: str) -> None:
    from ..genres import album_genres, find_artist_dirs, similar_artist_dirs

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
            if album.unreadable:
                names = ", ".join(path.name for path in album.unreadable)
                print(
                    f"Warning: {len(album.unreadable)} unreadable file(s): {names}",
                    file=sys.stderr,
                )
            for genre in album.genres:
                album_counts[genre] = album_counts.get(genre, 0) + 1

    ranked = sorted(album_counts.items(), key=lambda item: (-item[1], item[0]))
    summary = ", ".join(f"{genre} ({count} of {album_total})" for genre, count in ranked)
    print(f"Genres by album count: {summary or 'none'}")


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
    _warn_unreadable(album)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)

    from ..placement import (
        FileMapping,
        conflict_message,
        find_destination_conflicts,
        is_multi_disc,
        track_filename,
        unknown_disc_file_renames,
    )

    multi_disc = is_multi_disc(album)
    # Plan every rename first, so no file moves until every target is known.
    plan: list[tuple[TrackTags, str, Path, list[tuple[Path, Path]]]] = []
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

        # Lyric sidecars move with their audio.
        moves = [(s, s.with_name(new_path.stem + s.suffix)) for s in lyric_sidecars(track.path)]
        plan.append((track, new_name, new_path, moves))

    # Whipper's .cue/.log/.toc from an unknown rip, named for the tagged album.
    disc_moves = unknown_disc_file_renames(album)

    collisions = find_destination_conflicts(
        FileMapping(source=src, dest=dst)
        for src, dst in [
            *((t.path, p) for t, _, p, _ in plan),
            *(m for _, _, _, moves in plan for m in moves),
            *disc_moves,
        ]
    )
    if collisions:
        print(f"Error: {conflict_message(collisions)}", file=sys.stderr)
        sys.exit(1)

    # A target that exists blocks the rename unless it is a file this plan moves
    # (a swap) or the same file as one (a case-only change).
    planned_sources = [
        *(track.path for track, _, _, _ in plan),
        *(src for _, _, _, moves in plan for src, _ in moves),
        *(src for src, _ in disc_moves),
    ]
    targets = [
        *(
            (track.path, dst)
            for track, _, new_path, moves in plan
            for dst in [new_path, *(d for _, d in moves)]
        ),
        *disc_moves,
    ]
    blocked = [
        f"  {src.name}: '{dst.name}' already exists and is not part of this rename"
        for src, dst in targets
        if dst.exists() and not any(_is_same_file(dst, s) for s in planned_sources)
    ]
    if blocked:
        print("\n".join(blocked), file=sys.stderr)
        print(f"Error: {len(blocked)} rename(s) blocked; nothing renamed.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        for track, new_name, _, moves in plan:
            print(f"  {track.path.name} -> {new_name}")
            for src, dst in moves:
                print(f"  {src.name} -> {dst.name}")
        for src, dst in disc_moves:
            print(f"  {src.name} -> {dst.name}")
        print(f"\n(dry run — {len(album.tracks)} tracks)")
        return

    pairs = [
        *(pair for track, _, new_path, moves in plan for pair in [(track.path, new_path), *moves]),
        *disc_moves,
    ]
    audio_pairs = [(track.path, new_path) for track, _, new_path, _ in plan]
    sidecar_pairs = [pair for _, _, _, moves in plan for pair in moves]
    try:
        _two_phase_rename(pairs)
    except RenameFailedError as error:
        # A normal revert leaves nothing renamed and nothing logged. If a revert
        # itself failed, report where each stranded file is and log the ones that
        # reached their planned name.
        if not error.stranded:
            print(f"Error: rename failed and was reverted: {error.cause}", file=sys.stderr)
            sys.exit(1)
        planned = set(pairs)
        landed = {(src, now) for src, now, _ in error.stranded if (src, now) in planned}
        log_path = _log_path(args)
        if log_path and landed:
            _write_rename_log(
                log_path,
                album,
                [(s.name, d.name) for s, d in audio_pairs if (s, d) in landed],
                [],
                [(s.name, d.name) for s, d in sidecar_pairs if (s, d) in landed],
                [(s.name, d.name) for s, d in disc_moves if (s, d) in landed],
            )
        print(
            f"Error: rename failed and {len(error.stranded)} file(s) could not be reverted: "
            f"{error.cause}",
            file=sys.stderr,
        )
        for src, now, reason in error.stranded:
            print(f"  {src.name} is now {now.name} ({reason})", file=sys.stderr)
        sys.exit(1)

    renames: list[tuple[str, str]] = []
    sidecar_renames: list[tuple[str, str]] = []
    for track, new_name, _, moves in plan:
        print(f"  {track.path.name} -> {new_name}")
        renames.append((track.path.name, new_name))
        for src, dst in moves:
            print(f"  {src.name} -> {dst.name}")
            sidecar_renames.append((src.name, dst.name))

    disc_renames = [(src.name, dst.name) for src, dst in disc_moves]
    for old, new in disc_renames:
        print(f"  {old} -> {new}")

    print(f"Renamed {len(renames)} file(s).")
    if sidecar_renames:
        print(f"Renamed {len(sidecar_renames)} lyric sidecar(s).")
    if disc_renames:
        print(f"Renamed {len(disc_renames)} rip file(s) from the unknown-disc name.")
    cues = _update_cue_sheets(target, dict(renames))
    log_path = _log_path(args)
    if log_path and (renames or disc_renames):
        _write_rename_log(log_path, album, renames, cues, sidecar_renames, disc_renames)


def _path_key(path: Path) -> tuple[Path, str]:
    """A path's identity within its directory, case-insensitive as the NAS share is."""
    return (path.parent, path.name.casefold())


def _is_same_file(a: Path, b: Path) -> bool:
    """True when both paths name one file. Falls back to a case-insensitive name match
    when the file system cannot answer (the target does not exist, or is missing)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return a.parent == b.parent and a.name.casefold() == b.name.casefold()


class RenameFailedError(OSError):
    """A rename failed. `stranded` lists each file the revert could not move back.

    Each entry is (original path, path the file has now, the revert's error text).
    An empty list means the revert restored every file.
    """

    def __init__(self, cause: OSError, stranded: list[tuple[Path, Path, str]]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.stranded = stranded


def _two_phase_rename(pairs: list[tuple[Path, Path]]) -> None:
    """Rename each source to its target, so that sources may trade names.

    Phase 1 moves every source to a unique temporary name in its own directory;
    phase 2 moves each temporary name to its target. If a step fails, the renames
    already made are reverted and RenameFailedError is raised, naming any file the
    revert could not restore.
    """
    staged: list[tuple[Path, Path, Path]] = []  # (source, temporary, target)
    finished: list[tuple[Path, Path, Path]] = []
    try:
        for source, target in pairs:
            temporary = source.with_name(f".{source.name}.{secrets.token_hex(4)}.tmp")
            source.rename(temporary)
            staged.append((source, temporary, target))
        for item in staged:
            _, temporary, target = item
            temporary.rename(target)
            finished.append(item)
    except OSError as cause:
        stranded: list[tuple[Path, Path, str]] = []
        at_target: set[Path] = set()
        # The source whose stranded file sits at each occupied path, by path key.
        held: dict[tuple[Path, str], Path] = {}
        for source, temporary, target in reversed(finished):
            try:
                target.rename(temporary)
            except OSError as error:
                stranded.append((source, target, str(error)))
                at_target.add(source)
                held[_path_key(target)] = source
        for source, temporary, _ in reversed(staged):
            if source in at_target:
                continue
            holder = held.get(_path_key(source))
            if holder is not None:
                # Renaming onto this path would replace the stranded file that sits there.
                stranded.append((source, temporary, f"{source.name} is held by {holder.name}"))
                continue
            try:
                temporary.rename(source)
            except OSError as error:
                stranded.append((source, temporary, str(error)))
        raise RenameFailedError(cause, stranded) from cause


def _update_cue_sheets(album_dir: Path, renames: dict[str, str]) -> list[Path]:
    """Rewrite CUE FILE lines for renamed files and warn about dangling ones."""
    cues = rewrite_cue_references(album_dir, renames) if renames else []
    for cue in cues:
        print(f"Updated CUE: {cue.name}")
    for cue, name in missing_cue_references(album_dir):
        print(f"Warning: {cue.name} references missing file '{name}'.", file=sys.stderr)
    return cues


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
    parser.add_argument(
        "--track-only",
        action="store_true",
        help="Write each file's track gain and peak only. Album tags are not written, "
        "so use this when the directory holds files from more than one album.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    from .. import replaygain

    result = replaygain.scan_replaygain(
        target,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        track_only=args.track_only,
    )

    if args.dry_run:
        print(f"\n(dry run — {result.track_count} tracks scanned, no tags written)")
        return
    kind = "track" if args.track_only else "ReplayGain"
    print(f"Wrote {kind} tags to {result.track_count} track(s).")

    log_path = _log_path(args)
    if log_path:
        if args.track_only:
            album_gain = "(not written; track only)"
        else:
            album_gain = replaygain.read_album_gain(target) or "(not found)"
        _write_replaygain_log(log_path, target, result.track_count, album_gain)


def _mark_checklist_done(album_dir: Path) -> None:
    path = _default_checklist_path()
    checklist = Checklist.load(path)
    entry = checklist.find(album_dir)
    if entry is None:
        return
    entry.status, entry.reason = "done", ""
    checklist.save(path)
    print(f"Checklist: marked {album_dir} done.")


def _place(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger place",
        description="Copy a finished album into the library with SHA-256 verification. "
        "Afterward, removes the copied source files when it is a temp staging directory; "
        "anything it did not copy is kept.",
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
    _exit_if_unreadable(album)
    if not album.tracks:
        print(f"Error: no audio files in {source}.", file=sys.stderr)
        sys.exit(1)

    try:
        plan = compute_placement(
            album, root=args.root, artist_dir=args.artist_dir, album_dir=args.album_dir
        )
    except PlacementConflictError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    if plan.album_dir.resolve().is_relative_to(source):
        print(
            f"Error: destination {plan.album_dir} is inside the source {source}",
            file=sys.stderr,
        )
        sys.exit(1)
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

    # Delete only the verified copies in a temp staging directory. The temp root
    # itself is never cleaned up.
    temp_root = Path(tempfile.gettempdir()).resolve()
    if source != temp_root and source.is_relative_to(temp_root):
        kept = remove_copied_sources(result, source)
        if kept:
            noun = "entry" if len(kept) == 1 else "entries"
            print(f"Kept staging directory {source}: {len(kept)} uncopied {noun}:")
            for entry in kept:
                print(f"  {entry.name}")
        else:
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
