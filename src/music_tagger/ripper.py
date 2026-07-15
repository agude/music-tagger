"""CD ripping via whipper with AccurateRip verification."""

from __future__ import annotations

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
    accuraterip: list[str] = field(default_factory=list)


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
