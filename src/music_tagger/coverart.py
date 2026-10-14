"""Fetch cover art from the Cover Art Archive."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

CAA_BASE_URL = "https://coverartarchive.org"
USER_AGENT = "music-tagger/0.1 (alex.public.account@gmail.com)"


@dataclass
class ArtResult:
    saved: bool
    path: Path | None = None
    size_bytes: int = 0
    skipped: bool = False
    not_found: bool = False


def use_local_cover(
    source: Path,
    dest_dir: Path,
    filename: str = "cover.jpg",
    force: bool = False,
) -> ArtResult:
    dest = dest_dir / filename

    if dest.exists() and not force:
        return ArtResult(saved=False, path=dest, skipped=True)

    if not source.is_file():
        return ArtResult(saved=False, not_found=True)

    import shutil

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return ArtResult(saved=True, path=dest, size_bytes=dest.stat().st_size)


def fetch_cover_art(
    release_id: str,
    dest_dir: Path,
    filename: str = "cover.jpg",
    full_size: bool = False,
    force: bool = False,
) -> ArtResult:
    dest = dest_dir / filename

    if dest.exists() and not force:
        return ArtResult(saved=False, path=dest, skipped=True)

    endpoint = "front" if full_size else "front-500"
    url = f"{CAA_BASE_URL}/release/{release_id}/{endpoint}"

    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(url)

    if resp.status_code == 404:
        return ArtResult(saved=False, not_found=True)

    resp.raise_for_status()

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)

    return ArtResult(saved=True, path=dest, size_bytes=len(resp.content))


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# JPEG start-of-frame markers carry the dimensions. C4, C8, and CC share the
# range but are not frame headers.
_JPEG_SOF_MARKERS = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """Return (width, height) of a JPEG or PNG file, or None if unrecognized."""
    data = path.read_bytes()
    if data.startswith(_PNG_SIGNATURE) and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not data.startswith(b"\xff\xd8"):
        return None

    offset = 2
    while offset + 9 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        segment_length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if marker in _JPEG_SOF_MARKERS:
            height = int.from_bytes(data[offset + 5 : offset + 7], "big")
            width = int.from_bytes(data[offset + 7 : offset + 9], "big")
            return width, height
        offset += 2 + segment_length
    return None
