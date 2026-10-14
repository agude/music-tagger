"""CD ripping via whipper with AccurateRip verification."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RipResult:
    tracks: list[Path]
    track_count: int
    log_file: Path | None = None
    cue_file: Path | None = None


@dataclass
class AccurateRipSummary:
    """Per-track AccurateRip outcome from a whipper log.

    A track is exact when either AccurateRip v1 or v2 found an exact match.
    Tracks absent from the database are normal for less common discs; a
    mismatch or a copy status other than "Copy OK" means the rip needs review.
    """

    exact: list[int] = field(default_factory=list)
    mismatched: list[int] = field(default_factory=list)
    absent: list[int] = field(default_factory=list)
    not_ok: list[tuple[int, str]] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.mismatched or self.not_ok)

    def describe(self) -> list[str]:
        lines = [
            f"AccurateRip: {len(self.exact)} exact, {len(self.mismatched)} mismatched, "
            f"{len(self.absent)} not in database"
        ]
        if self.mismatched:
            lines.append(f"Mismatched tracks: {', '.join(map(str, self.mismatched))}")
        for track, status in self.not_ok:
            lines.append(f"Track {track} status: {status}")
        return lines


_TRACK_HEADER = re.compile(r"^  (\d+):$")


def parse_accuraterip(log_text: str) -> AccurateRipSummary:
    """Summarize the Tracks section of a whipper 0.10 log."""
    results: dict[int, list[str]] = {}
    statuses: dict[int, str] = {}
    in_tracks = False
    track: int | None = None
    for line in log_text.splitlines():
        if line == "Tracks:":
            in_tracks = True
            continue
        if not in_tracks:
            continue
        if line and not line.startswith(" "):
            break
        if header := _TRACK_HEADER.match(line):
            track = int(header[1])
            results[track] = []
        elif track is not None and line.strip().startswith("Result:"):
            results[track].append(line.split(":", 1)[1].strip())
        elif track is not None and line.startswith("    Status:"):
            statuses[track] = line.split(":", 1)[1].strip()

    summary = AccurateRipSummary()
    for number, track_results in results.items():
        if any(result.endswith("exact match") and "NO" not in result for result in track_results):
            summary.exact.append(number)
        elif any(result.startswith("Found") for result in track_results):
            summary.mismatched.append(number)
        else:
            summary.absent.append(number)
        status = statuses.get(number, "")
        if status != "Copy OK":
            summary.not_ok.append((number, status or "(missing)"))
    return summary


def _check_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"'{name}' not found. Install it: sudo apt-get install {name}")


def rip_cd(
    output_dir: Path,
    device: str = "/dev/cdrom",
    release_id: str | None = None,
    unknown: bool = False,
) -> RipResult:
    """Rip a CD to FLAC files using whipper.

    Whipper handles disc ID lookup, MusicBrainz matching, AccurateRip
    verification, and FLAC encoding. Output files are named
    ``NN. Title.flac`` in output_dir.
    """
    _check_tool("whipper")

    output_dir.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        "whipper",
        "-e",
        "never",
        "cd",
        "-d",
        device,
        "rip",
        "-O",
        str(output_dir),
        "--track-template",
        "%t. %n",
        "--disc-template",
        "%A - %d",
    ]

    if release_id:
        cmd.extend(["-R", release_id])
    if unknown:
        cmd.extend(["-U"])

    subprocess.run(cmd, check=True)

    flac_paths = sorted(output_dir.glob("*.flac"))
    if not flac_paths:
        raise RuntimeError("whipper produced no FLAC files")

    log_files = list(output_dir.glob("*.log"))
    cue_files = list(output_dir.glob("*.cue"))

    return RipResult(
        tracks=flac_paths,
        track_count=len(flac_paths),
        log_file=log_files[0] if log_files else None,
        cue_file=cue_files[0] if cue_files else None,
    )


def read_disc_id(device: str = "/dev/cdrom") -> dict[str, Any]:
    """Read disc ID and TOC from a CD using libdiscid.

    Returns a dict with disc_id, track_count, and tracks (list of
    dicts with number, offset, sectors, seconds).
    """
    try:
        import discid
    except ImportError as err:
        raise RuntimeError(
            "python 'discid' package not installed. "
            "Install: uv add discid && sudo apt-get install libdiscid-dev"
        ) from err

    disc = discid.read(device)
    tracks = []
    for t in disc.tracks:
        tracks.append(
            {
                "number": t.number,
                "offset": t.offset,
                "sectors": t.sectors,
                "seconds": t.seconds,
            }
        )

    return {
        "disc_id": disc.id,
        "track_count": len(disc.tracks),
        "tracks": tracks,
    }
