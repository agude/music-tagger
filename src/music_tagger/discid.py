"""Parse rip artifacts (CUE sheets) and compute MusicBrainz disc IDs."""

from __future__ import annotations

import hashlib
import re
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FRAMES_PER_SECOND = 75
PREGAP_FRAMES = 150


@dataclass
class TOCInfo:
    first_track: int
    last_track: int
    leadout_offset: int
    track_offsets: list[int]
    disc_id: str = ""

    def to_toc_string(self) -> str:
        parts = [str(self.first_track), str(self.last_track), str(self.leadout_offset)]
        parts.extend(str(o) for o in self.track_offsets)
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "first_track": self.first_track,
            "last_track": self.last_track,
            "leadout_offset": self.leadout_offset,
            "track_offsets": self.track_offsets,
            "toc_string": self.to_toc_string(),
        }
        if self.disc_id:
            d["disc_id"] = self.disc_id
        return d


def _msf_to_frames(m: int, s: int, f: int) -> int:
    return m * 60 * FRAMES_PER_SECOND + s * FRAMES_PER_SECOND + f


def _compute_disc_id(first: int, last: int, leadout: int, offsets: list[int]) -> str:
    """Compute MusicBrainz disc ID from TOC offsets.

    Follows https://musicbrainz.org/doc/Disc_ID_Calculation.
    SHA-1 of: first track, last track, leadout offset, then offsets
    for tracks 1-99 (zero-padded). All values as 8-digit uppercase hex.
    """
    sha = hashlib.sha1()
    sha.update(f"{first:02X}".encode())
    sha.update(f"{last:02X}".encode())

    all_offsets = [leadout, *offsets]
    while len(all_offsets) < 100:
        all_offsets.append(0)

    for o in all_offsets:
        sha.update(f"{o:08X}".encode())

    raw = b64encode(sha.digest()).decode()
    return raw.replace("+", ".").replace("/", "_").replace("=", "-")


_INDEX_RE = re.compile(r"INDEX\s+01\s+(\d+):(\d+):(\d+)")


def parse_cue(cue_path: Path) -> TOCInfo:
    """Parse a CUE sheet and extract TOC info.

    Returns track offsets in LBA sectors (with 150-frame pregap added).
    The leadout offset must be set from audio file duration separately.
    """
    text = cue_path.read_text(errors="replace")
    offsets: list[int] = []

    for line in text.splitlines():
        match = _INDEX_RE.search(line)
        if match:
            m, s, f = int(match.group(1)), int(match.group(2)), int(match.group(3))
            offsets.append(_msf_to_frames(m, s, f) + PREGAP_FRAMES)

    if not offsets:
        raise ValueError(f"No INDEX 01 entries found in {cue_path}")

    return TOCInfo(
        first_track=1,
        last_track=len(offsets),
        leadout_offset=0,
        track_offsets=offsets,
    )


def parse_rip_dir(rip_dir: Path) -> TOCInfo:
    """Parse rip artifacts from a directory.

    Looks for CUE sheets; computes leadout from audio file durations
    when possible.
    """
    cue_files = sorted(rip_dir.glob("*.cue"))
    if not cue_files:
        raise ValueError(f"No .cue files found in {rip_dir}")

    toc = parse_cue(cue_files[0])

    audio_files = sorted(
        f for f in rip_dir.iterdir() if f.suffix.lower() in {".flac", ".wav", ".mp3"}
    )
    if audio_files:
        try:
            from mutagen import File as MutagenFile

            total_frames = 0
            for af in audio_files:
                audio = MutagenFile(af)
                if audio and audio.info:
                    total_frames += int(audio.info.length * FRAMES_PER_SECOND)
            if total_frames > 0:
                toc.leadout_offset = total_frames + PREGAP_FRAMES
        except Exception:
            pass

    if toc.leadout_offset > 0:
        toc.disc_id = _compute_disc_id(
            toc.first_track,
            toc.last_track,
            toc.leadout_offset,
            toc.track_offsets,
        )

    return toc
