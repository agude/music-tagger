"""CD ripping: extract audio tracks and encode to FLAC."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RipResult:
    tracks: list[Path]
    track_count: int


def _check_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"'{name}' not found. Install it: sudo apt-get install {name}")


def rip_cd(
    output_dir: Path,
    device: str = "/dev/cdrom",
    track_count: int | None = None,
) -> RipResult:
    """Rip a CD to FLAC files in output_dir.

    Uses cdparanoia for extraction and flac for encoding. Files are named
    NN.flac (01.flac, 02.flac, ...) with no metadata tags — the caller
    is responsible for tagging.
    """
    _check_tool("cdparanoia")
    _check_tool("flac")

    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Rip all tracks to WAV (stderr streams progress to the terminal)
        subprocess.run(
            ["cdparanoia", "-B", "-d", device],
            cwd=str(tmp),
            check=True,
        )

        wav_files = sorted(tmp.glob("track*.cdda.wav"))
        if not wav_files:
            raise RuntimeError("cdparanoia produced no WAV files")

        if track_count is not None and len(wav_files) != track_count:
            raise RuntimeError(
                f"Expected {track_count} tracks but cdparanoia produced {len(wav_files)}"
            )

        # Encode each WAV to FLAC
        flac_paths: list[Path] = []
        for i, wav in enumerate(wav_files, 1):
            flac_name = f"{i:02d}.flac"
            flac_path = output_dir / flac_name
            subprocess.run(
                ["flac", "--best", "--silent", str(wav), "-o", str(flac_path)],
                check=True,
                capture_output=True,
            )
            flac_paths.append(flac_path)

    return RipResult(tracks=flac_paths, track_count=len(flac_paths))


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
