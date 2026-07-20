"""ReplayGain 2.0 tagging via rsgain."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .tags import AUDIO_EXTENSIONS


def _check_tool() -> None:
    if shutil.which("rsgain") is None:
        raise RuntimeError("'rsgain' not found. Install it: sudo apt-get install rsgain")


def _audio_files(directory: Path) -> list[Path]:
    return sorted(
        f for f in directory.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )


@dataclass
class ReplayGainResult:
    directory: Path
    track_count: int
    dry_run: bool


def scan_replaygain(
    directory: Path,
    *,
    dry_run: bool = False,
    skip_existing: bool = False,
) -> ReplayGainResult:
    _check_tool()

    files = _audio_files(directory)
    if not files:
        raise RuntimeError(f"No audio files in {directory}")

    if dry_run:
        cmd: list[str] = ["rsgain", "custom", "-a", "--tagmode=s", "-O"]
        if skip_existing:
            cmd.append("-S")
        cmd.extend(str(f) for f in files)
    else:
        cmd = ["rsgain", "easy"]
        if skip_existing:
            cmd.append("-S")
        cmd.append(str(directory))

    subprocess.run(cmd, check=True)

    return ReplayGainResult(
        directory=directory,
        track_count=len(files),
        dry_run=dry_run,
    )
