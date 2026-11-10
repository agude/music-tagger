"""ReplayGain 2.0 tagging via rsgain."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from .tags import audio_files


def _check_tool() -> None:
    if shutil.which("rsgain") is None:
        raise RuntimeError("'rsgain' not found. Install it: sudo apt-get install rsgain")


def _audio_files(directory: Path) -> list[Path]:
    return audio_files(directory)


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
    track_only: bool = False,
) -> ReplayGainResult:
    """Scan the directory's audio files and write ReplayGain tags.

    By default this computes album and track gain over the directory, which is
    only right when the directory is one album. With `track_only`, each file
    gets its own track gain and peak, and album tags are not written.
    """
    _check_tool()

    files = _audio_files(directory)
    if not files:
        raise RuntimeError(f"No audio files in {directory}")

    if track_only:
        cmd: list[str] = ["rsgain", "custom", "-O"] if dry_run else ["rsgain", "custom", "-s", "i"]
        if skip_existing:
            cmd.append("-S")
        cmd.extend(str(f) for f in files)
    elif dry_run:
        cmd = ["rsgain", "custom", "-a", "--tagmode=s", "-O"]
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


def read_album_gain(directory: Path) -> str:
    """Return the album gain rsgain wrote to the first audio file, or ""."""
    files = _audio_files(directory)
    if not files:
        return ""
    audio = MutagenFile(files[0])
    if isinstance(audio, FLAC):
        values = audio.get("REPLAYGAIN_ALBUM_GAIN", [])  # type: ignore[no-untyped-call]
        return str(values[0]) if values else ""
    if isinstance(audio, MP3) and audio.tags is not None:
        for frame in audio.tags.getall("TXXX"):
            if frame.desc.lower() == "replaygain_album_gain" and frame.text:
                return str(frame.text[0])
        return ""
    if isinstance(audio, MP4):
        for key, values in audio.items():  # type: ignore[no-untyped-call]
            if key.lower() == "----:com.apple.itunes:replaygain_album_gain" and values:
                return bytes(values[0]).decode("utf-8", "replace")
        return ""
    return ""
