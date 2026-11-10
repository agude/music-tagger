"""Read a whipper log for its TOC and MusicBrainz disc ID."""

from __future__ import annotations

import hashlib
import re
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PREGAP_FRAMES = 150


@dataclass
class TOCInfo:
    first_track: int
    last_track: int
    leadout_offset: int
    track_offsets: list[int]
    disc_id: str = ""
    source: str = "computed"  # "whipper": the log's lookup URL; "computed": from the TOC section

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


_TOC_START_RE = re.compile(r"Start sector:\s*(\d+)")
_TOC_END_RE = re.compile(r"End sector:\s*(\d+)")
_DISC_ID_RE = re.compile(r"MusicBrainz Disc ID:\s*(\S+)")
_URL_RE = re.compile(r"MusicBrainz lookup URL:\s*(\S+)")
_URL_TOC_RE = re.compile(r"[?&]toc=([0-9+]+)")


def _toc_from_sections(text: str) -> TOCInfo:
    """Build the TOC from the log's TOC section: each track starts at its start sector + 150.

    The leadout is the last track's end sector + 1 + 150.
    """
    starts = [int(m) for m in _TOC_START_RE.findall(text)]
    ends = [int(m) for m in _TOC_END_RE.findall(text)]
    if not starts or len(starts) != len(ends):
        raise ValueError("whipper log has no complete TOC section")
    offsets = [start + PREGAP_FRAMES for start in starts]
    leadout = ends[-1] + 1 + PREGAP_FRAMES
    disc_id = _compute_disc_id(1, len(offsets), leadout, offsets)
    return TOCInfo(
        first_track=1,
        last_track=len(offsets),
        leadout_offset=leadout,
        track_offsets=offsets,
        disc_id=disc_id,
    )


def parse_whipper_log(text: str) -> TOCInfo:
    """Read the TOC and disc ID from a whipper log.

    When whipper logged a lookup URL, its `toc=` value and the logged disc ID are
    authoritative, and they must agree. Without a URL, the TOC is computed from the
    TOC section and checked against the logged disc ID when one is present.
    """
    logged = _DISC_ID_RE.search(text)
    logged_id = logged.group(1) if logged else ""

    url = _URL_RE.search(text)
    if url is None:
        toc = _toc_from_sections(text)
        if logged_id and toc.disc_id != logged_id:
            raise ValueError(
                f"Whipper log disc ID {logged_id} does not match its TOC section, "
                f"which gives {toc.disc_id}."
            )
        return toc

    match = _URL_TOC_RE.search(url.group(1))
    if match is None:
        raise ValueError("whipper log lookup URL has no toc= value")
    values = [int(value) for value in match.group(1).split("+")]
    first, last, leadout, offsets = values[0], values[1], values[2], values[3:]
    if len(offsets) != last:
        raise ValueError(f"whipper log toc= lists {len(offsets)} tracks, not {last}")
    url_id = _compute_disc_id(first, last, leadout, offsets)
    if logged_id and url_id != logged_id:
        raise ValueError(
            f"Whipper log disc ID {logged_id} does not match its lookup URL, which gives {url_id}."
        )
    return TOCInfo(
        first_track=first,
        last_track=last,
        leadout_offset=leadout,
        track_offsets=offsets,
        disc_id=logged_id or url_id,
        source="whipper",
    )


def parse_rip_dir(rip_dir: Path, log: Path | None = None) -> TOCInfo:
    """Read the TOC from a rip directory's whipper log, or from `log` when given.

    A directory with several logs is a multi-disc staging directory; the caller
    must name one log.
    """
    if log is None:
        logs = sorted(rip_dir.glob("*.log"))
        if not logs:
            raise FileNotFoundError(f"No whipper .log file found in {rip_dir}")
        if len(logs) > 1:
            raise ValueError(f"{rip_dir} has {len(logs)} .log files; pick one with --log FILE")
        log = logs[0]
    return parse_whipper_log(log.read_text(errors="replace"))
