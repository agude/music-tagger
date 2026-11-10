"""CLI entry point for music-tagger."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable

from .library import _art, _checklist, _copy, _genre, _path_for, _place, _rename, _replaygain, _scan
from .lyrics_cmd import _lyrics, _lyrics_batch, _lyrics_decide
from .ratings import _write_ratings, _write_stats
from .rip import _rip, _toc
from .services import _mb, _nd
from .tagging import (
    _apply,
    _diff,
    _lock,
    _match,
    _print_candidates,
    _read,
    _tag,
    _unlock,
    _write_tags,
)


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
