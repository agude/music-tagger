"""CD rip and TOC commands."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from ..discid import parse_rip_dir
from .common import EXIT_REVIEW, _output_json


def _toc(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger toc",
        description="Read the TOC and disc ID from a rip directory's whipper log.",
    )
    parser.add_argument("path", type=Path, help="Rip directory containing the whipper .log.")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output JSON file (prints digest to stdout).",
    )
    parser.add_argument("--log", type=Path, default=None, help="Whipper log to read.")
    args = parser.parse_args(argv)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    try:
        toc = parse_rip_dir(target, args.log)
    except (FileNotFoundError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    data = toc.to_dict()

    lines = [f"TOC: {toc.last_track} tracks", f"Disc ID: {toc.disc_id}"]
    lines.append(f"TOC string: {toc.to_toc_string()}")
    _output_json(data, args.out, "\n".join(lines))


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
    parser.add_argument(
        "--disc",
        type=int,
        default=None,
        help="Disc number of an unknown multi-disc album (with --unknown and --of).",
    )
    parser.add_argument(
        "--of",
        type=int,
        dest="disc_total",
        default=None,
        help="Number of discs in the album (with --disc).",
    )
    args = parser.parse_args(argv)
    if (args.disc is None) != (args.disc_total is None):
        parser.error("--disc and --of must be given together")
    unknown_disc: tuple[int, int] | None = None
    if args.disc is not None:
        if not args.unknown:
            parser.error("--disc needs --unknown; MusicBrainz gives the disc position otherwise")
        if not 1 <= args.disc <= args.disc_total:
            parser.error(f"--disc {args.disc} is not one of --of {args.disc_total} discs")
        unknown_disc = (args.disc, args.disc_total)

    from .. import ripper

    target = args.path.resolve() if args.path else Path(tempfile.mkdtemp(prefix="rip-"))

    print(f"Staging: {target}", flush=True)
    try:
        result = ripper.rip_cd(
            target,
            device=args.device,
            release_id=args.release_id,
            unknown=args.unknown,
            replace=args.replace,
            unknown_disc=unknown_disc,
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
    hidden = " plus the hidden track before track 1" if result.hidden_track else ""
    print(f"Ripped {result.track_count} tracks{hidden}.")

    if result.cue_file:
        print(f"CUE: {result.cue_file}")
    if result.log_file is None:
        print("Error: whipper wrote no log; AccurateRip results unknown.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)
    print(f"Log: {result.log_file}")
    accuraterip = ripper.parse_accuraterip(
        result.log_file.read_text(), result.track_count, hidden_track=result.hidden_track
    )
    print("\n".join(accuraterip.describe()))
    disc = f"Disc {result.disc_number} of {result.disc_total}" if result.is_multi_disc else "Disc"
    if accuraterip.needs_review:
        print(f"{disc} needs review: see the tracks above. Safe to eject.", file=sys.stderr)
        sys.exit(EXIT_REVIEW)

    print(f"\n{disc} verified; safe to eject.")
    if result.is_multi_disc and (result.disc_number or 0) < (result.disc_total or 0):
        next_disc = (result.disc_number or 0) + 1
        flags = f" --unknown --disc {next_disc} --of {result.disc_total}" if args.unknown else ""
        print(f"Next: insert disc {next_disc}, then run:")
        print(f"  uv run music-tagger rip {target}{flags}")
        return
    print("Next: review tags and candidates:")
    print(f"  uv run music-tagger candidates {target}")
