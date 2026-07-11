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
