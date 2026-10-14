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


@pytest.fixture()
def m4a_album(tmp_path: Path) -> Path:
    """Copy the M4A fixture album to a temp dir so tests can mutate it."""
    dest = tmp_path / "album_m4a"
    shutil.copytree(FIXTURES / "album_m4a", dest)
    return dest


@pytest.fixture(autouse=True)
def default_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default change log at a temp file so no test writes the real one."""
    path = tmp_path / "default-changes.log"
    monkeypatch.setenv("MUSIC_TAGGER_LOG", str(path))
    return path
