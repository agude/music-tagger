"""CD rip and TOC commands."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..discid import parse_rip_dir
from ..musicbrainz import MusicBrainzClient
from .common import EXIT_REVIEW, _format_table, _output_json

if TYPE_CHECKING:
    from ..identify import Identification, MediumMatch


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


def _identify(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger identify",
        description=(
            "Read the disc in the drive, find its MusicBrainz release by disc ID, "
            "barcode, and title, and print the rip command to run."
        ),
    )
    parser.add_argument(
        "staging",
        nargs="?",
        default=None,
        help="Staging directory of the album's earlier discs, for the printed rip command.",
    )
    parser.add_argument("--device", default="/dev/cdrom", help="CD device (default: /dev/cdrom).")
    parser.add_argument("--barcode", default=None, help="Barcode; default: the disc's MCN.")
    parser.add_argument("--artist", default=None, help="Artist name for a title search.")
    parser.add_argument("--album", default=None, help="Album title for a title search.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the table.")
    args = parser.parse_args(argv)
    if args.artist and not args.album:
        parser.error("--artist needs --album")

    from .. import ripper
    from ..identify import attach_url, identify_disc, rip_command

    try:
        disc, mcn = ripper.read_drive_toc(args.device)
    except Exception as error:  # libdiscid raises its own DiscError
        print(f"Error: cannot read the disc in {args.device}: {error}", file=sys.stderr)
        sys.exit(1)

    client = MusicBrainzClient()
    try:
        result = identify_disc(
            disc, client, mcn=mcn, barcode=args.barcode, artist=args.artist, album=args.album
        )
    except httpx.HTTPError as error:
        print(f"Error: MusicBrainz request failed: {error}", file=sys.stderr)
        print("Nothing was ripped. Run identify again when MusicBrainz responds.", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()

    best = result.best
    if args.json:
        data = {
            "disc_id": disc.disc_id,
            "mcn": mcn,
            "toc": disc.offsets,
            "leadout": disc.leadout,
            "matches": [_match_dict(m) for m in result.matches],
            "release_missing": result.release_missing,
            "rip_command": rip_command(None, args.device, args.staging)
            if result.release_missing
            else rip_command(best, args.device, args.staging)
            if best and best.routable
            else None,
            "attach_url": attach_url(disc, best.release.id)
            if best and best.routable and not best.exact and not result.release_missing
            else None,
        }
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        minutes, seconds = divmod(round((disc.leadout - disc.offsets[0]) / 75), 60)
        print(
            f"Disc: {len(disc.offsets)} tracks, {minutes}:{seconds:02d}, "
            f"disc ID {disc.disc_id}, MCN {mcn or 'none'}"
        )
        searched = ["disc ID"]
        if result.barcode:
            searched.append(f"barcode {result.barcode}")
        if args.album:
            searched.append(f'artist="{args.artist or ""}" album="{args.album}"')
        print(f"Searched: {'; '.join(searched)}\n")
        if result.matches:
            rows = [_match_row(i, m) for i, m in enumerate(result.matches, 1)]
            print(_format_table(_IDENTIFY_COLUMNS, rows) + "\n")

    if result.release_missing:
        if not args.json:
            _print_missing_release(result, args.device, args.staging)
        sys.exit(EXIT_RELEASE_MISSING)

    if best is None or not best.routable:
        if not args.json:
            print("Route: no release medium matches this disc's track lengths.")
            print("Do not rip yet. Search again with --barcode, or --artist and --album.")
        sys.exit(1)

    if args.json:
        sys.exit(0 if best.exact else EXIT_REVIEW)

    medium = f"medium {best.position} of {best.medium_count}"
    if best.exact:
        print(f"Route: the disc ID is attached to {medium} of {best.release.id}.")
        print(f"  Rip: {rip_command(best, args.device, args.staging)}")
        return
    drift = (
        f"; offsets drift up to {best.max_drift_frames} frames from its attached disc ID"
        if best.max_drift_frames is not None
        else "; it has no disc ID with this track count"
    )
    lengths = "track lengths match"
    if not best.lengths_match:
        off = ", ".join(str(n) for n in best.outliers)
        lengths = f"the barcode and all track lengths but track {off} match"
    print(
        f"Route: {lengths} {medium} of {best.release.id}, "
        f"but its disc IDs do not include this disc{drift}."
    )
    print("  This is a different pressing of a listed release, not a missing album.")
    print(f"  Attach (preferred): open {attach_url(disc, best.release.id)}")
    print(f"    pick medium {best.position} ({len(disc.offsets)} tracks), and confirm the edit.")
    print("    Then run identify again: it exits 0 and prints a --release-id rip.")
    print("  Fallback, when the disc ID cannot be attached now:")
    print(f"    1. Rip: {rip_command(best, args.device, args.staging)}")
    print(f"    2. Tag: apply <staging> --release-id {best.release.id} ...")
    sys.exit(EXIT_REVIEW)


# identify: the barcode names a release that MusicBrainz does not have.
EXIT_RELEASE_MISSING = 4


def _print_missing_release(result: Identification, device: str, staging: str | None) -> None:
    from ..identify import attach_url, rip_command

    print(f"Route: no MusicBrainz release has barcode {result.barcode}.")
    if result.best is not None:
        closest = result.best.release
        print(
            f"  The closest by track lengths, {closest.id} ({closest.date or 'no date'}, "
            f"barcode {closest.barcode}), is a different release."
        )
    print("  Do not attach this disc ID to another release, and do not tag from one.")
    print("  1. Rip now; the audio does not depend on the release:")
    print(f"     {rip_command(None, device, staging)}")
    print("  2. Add this release to MusicBrainz. Its attach page can start a new release")
    print(f"     with this disc's track lengths: {attach_url(result.disc)}")
    print(f"  3. Find it: uv run music-tagger mb search --barcode {result.barcode}")
    print("  4. Tag: apply <staging> --release-id <new release ID> ...")


_IDENTIFY_COLUMNS = [
    "#",
    "Release ID",
    "Medium",
    "Within 2s",
    "Max dev",
    "Disc ID",
    "Drift",
    "Barcode",
    "Country",
    "Date",
    "Title",
]


def _match_row(rank: int, match: MediumMatch) -> list[str]:
    release = match.release
    drift = "-" if match.max_drift_frames is None else f"{match.max_drift_frames}f"
    return [
        str(rank),
        release.id,
        f"{match.position}/{match.medium_count}",
        f"{match.tracks_within_2s}/{match.tracks_compared}"
        + (f" (off: {', '.join(map(str, match.outliers))})" if match.outliers else ""),
        f"{match.max_deviation_secs:.1f}s",
        "attached" if match.exact else "differs",
        drift,
        f"{release.barcode or '-'}{' (match)' if match.barcode_match else ''}",
        release.country or "-",
        release.date or "-",
        f"{release.artist} — {release.title}",
    ]


def _match_dict(match: MediumMatch) -> dict[str, Any]:
    return {
        "release_id": match.release.id,
        "title": match.release.title,
        "artist": match.release.artist,
        "barcode": match.release.barcode,
        "country": match.release.country,
        "date": match.release.date,
        "position": match.position,
        "medium_count": match.medium_count,
        "tracks_within_2s": match.tracks_within_2s,
        "tracks_compared": match.tracks_compared,
        "max_deviation_secs": round(match.max_deviation_secs, 3),
        "exact": match.exact,
        "max_drift_frames": match.max_drift_frames,
        "barcode_match": match.barcode_match,
        "outliers": match.outliers,
        "routable": match.routable,
    }


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
            "If whipper found no release for the disc ID, run `music-tagger identify`: "
            "it finds the release by barcode and track lengths and prints the rip command.",
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
