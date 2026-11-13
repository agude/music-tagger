"""MusicBrainz (`mb`) and Navidrome (`nd`) subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from ..musicbrainz import MusicBrainzClient
from ..navidrome import NavidromeClient, SongRating
from .common import _output_json
from .ratings import _DEFAULT_RATINGS_PATH, _nd_ratings, _nd_stats


def _mb_search(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger mb search",
        description="Search MusicBrainz for candidate releases.",
    )
    parser.add_argument("--artist", default="", help="Artist name.")
    parser.add_argument("--album", default="", help="Album title.")
    parser.add_argument(
        "--barcode", default=None, help="Barcode (UPC/EAN); enough on its own to search."
    )
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
    if not args.album and not args.barcode:
        parser.error("give --album (with or without --artist), or --barcode")

    mb_client = MusicBrainzClient()
    try:
        results = mb_client.search_releases(
            args.artist,
            args.album,
            format=args.format,
            limit=args.limit,
            barcode=args.barcode,
        )
    finally:
        mb_client.close()

    data = [r.to_dict() for r in results]

    searched = f'"{args.artist}" — "{args.album}"' if args.album else ""
    if args.barcode:
        searched += f"{' ' if searched else ''}barcode {args.barcode}"
    lines = [f"{len(results)} candidates for {searched}:"]
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
