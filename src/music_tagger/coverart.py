"""Fetch cover art from the Cover Art Archive."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import httpx

CAA_BASE_URL = "https://coverartarchive.org"
USER_AGENT = "music-tagger/0.1 (alex.public.account@gmail.com)"

# Navidrome checks `cover.*` in the album folder before embedded art, and
# which of two `cover.*` files wins is unspecified, so an album keeps one.
COVER_STEM = "cover"
_EXTENSIONS = {"jpeg": ".jpg", "png": ".png"}
_MIME_TYPES = {"jpeg": "image/jpeg", "png": "image/png"}
_COVER_SUFFIXES = (".jpg", ".jpeg", ".png")

# A FLAC picture lives in one metadata block whose length field is 24 bits.
# The picture header (type, MIME, description, dimensions) needs the margin.
MAX_EMBEDDED_BYTES = 2**24 - 1 - 1024
# Quality 95 with 4:4:4 chroma keeps a converted cover visually lossless.
EMBED_JPEG_QUALITY = 95
_DOWNSCALE_FACTOR = 0.75
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class UnsupportedImageError(ValueError):
    """The image is not a JPEG or PNG, or cannot be decoded."""


@dataclass
class ArtResult:
    saved: bool
    path: Path | None = None
    size_bytes: int = 0
    skipped: bool = False
    not_found: bool = False
    removed: list[Path] = field(default_factory=list)


@dataclass
class EmbeddedCover:
    """Image bytes to embed in audio files."""

    data: bytes
    mime: str
    converted: bool = False


def image_format(data: bytes) -> str | None:
    """Return "jpeg" or "png" from the file signature, or None."""
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    return None


def cover_filename(data: bytes) -> str:
    """Folder cover name for this image: cover.jpg or cover.png."""
    fmt = image_format(data)
    if fmt is None:
        raise UnsupportedImageError("not a JPEG or PNG image")
    return COVER_STEM + _EXTENSIONS[fmt]


def find_cover(album_dir: Path) -> Path | None:
    """The album's folder cover (cover.jpg, cover.jpeg, or cover.png), if any."""
    for suffix in _COVER_SUFFIXES:
        path = album_dir / (COVER_STEM + suffix)
        if path.is_file():
            return path
    return None


def install_cover(data: bytes, dest_dir: Path, force: bool = False) -> ArtResult:
    """Write the folder cover named for its format and remove any other cover.* file.

    Without `force`, an existing cover is kept and the result is skipped.
    """
    existing = find_cover(dest_dir)
    if existing is not None and not force:
        return ArtResult(saved=False, path=existing, skipped=True)

    dest = dest_dir / cover_filename(data)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    removed = []
    for suffix in _COVER_SUFFIXES:
        other = dest_dir / (COVER_STEM + suffix)
        if other != dest and other.is_file():
            other.unlink()
            removed.append(other)
    return ArtResult(saved=True, path=dest, size_bytes=len(data), removed=removed)


def use_local_cover(source: Path, dest_dir: Path, force: bool = False) -> ArtResult:
    if not source.is_file():
        return ArtResult(saved=False, not_found=True)
    existing = find_cover(dest_dir)
    if existing is not None and existing.resolve() == source.resolve():
        return ArtResult(saved=False, path=existing, skipped=True)
    return install_cover(source.read_bytes(), dest_dir, force=force)


def fetch_cover_art(
    release_id: str,
    dest_dir: Path,
    full_size: bool = False,
    force: bool = False,
) -> ArtResult:
    existing = find_cover(dest_dir)
    if existing is not None and not force:
        return ArtResult(saved=False, path=existing, skipped=True)

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
    return install_cover(resp.content, dest_dir, force=True)


def embeddable_cover(path: Path) -> EmbeddedCover:
    """The bytes to embed for this image.

    A JPEG that fits in a FLAC picture block is embedded unchanged. A PNG is
    converted to JPEG, since the folder keeps the lossless original. A JPEG
    too large to embed is re-encoded, then downscaled until it fits.
    """
    data = path.read_bytes()
    fmt = image_format(data)
    if fmt is None:
        raise UnsupportedImageError(f"{path}: not a JPEG or PNG image")
    if fmt == "jpeg" and len(data) <= MAX_EMBEDDED_BYTES:
        return EmbeddedCover(data=data, mime=_MIME_TYPES[fmt])
    return EmbeddedCover(data=_encode_jpeg(path, data), mime="image/jpeg", converted=True)


def _encode_jpeg(path: Path, data: bytes) -> bytes:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as decoded:
            image = decoded.convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise UnsupportedImageError(f"{path}: cannot decode image: {error}") from error

    while True:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=EMBED_JPEG_QUALITY, subsampling=0)
        if buffer.tell() <= MAX_EMBEDDED_BYTES:
            return buffer.getvalue()
        width, height = image.size
        image = image.resize(
            (int(width * _DOWNSCALE_FACTOR), int(height * _DOWNSCALE_FACTOR)),
            Image.Resampling.LANCZOS,
        )


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
