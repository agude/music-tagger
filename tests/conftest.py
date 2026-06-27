from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def flac_album(tmp_path: Path) -> Path:
    """Copy the FLAC fixture album to a temp dir so tests can mutate it."""
    dest = tmp_path / "album_flac"
    shutil.copytree(FIXTURES / "album_flac", dest)
    return dest


@pytest.fixture()
def mp3_album(tmp_path: Path) -> Path:
    """Copy the MP3 fixture album to a temp dir so tests can mutate it."""
    dest = tmp_path / "album_mp3"
    shutil.copytree(FIXTURES / "album_mp3", dest)
    return dest
