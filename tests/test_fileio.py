"""The shared atomic write: content, permissions, and what a failed replace leaves behind."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from music_tagger.fileio import atomic_write_bytes, atomic_write_text


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestAtomicWrite:
    def test_writes_bytes_and_text(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        atomic_write_bytes(target, b"\x00\x01binary")
        assert target.read_bytes() == b"\x00\x01binary"

        atomic_write_text(target, "naïve text\n")
        assert target.read_bytes() == "naïve text\n".encode()

    def test_new_file_gets_the_mode_a_plain_write_gives(self, tmp_path: Path) -> None:
        reference = tmp_path / "reference"
        reference.write_bytes(b"x")
        target = tmp_path / "new.txt"

        atomic_write_bytes(target, b"y")

        assert _mode(target) == _mode(reference)

    def test_existing_file_keeps_its_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "private.txt"
        target.write_bytes(b"old")
        target.chmod(0o600)

        atomic_write_bytes(target, b"new")

        assert target.read_bytes() == b"new"
        assert _mode(target) == 0o600

    def test_explicit_mode_is_applied(self, tmp_path: Path) -> None:
        target = tmp_path / "shared.txt"
        target.write_bytes(b"old")
        target.chmod(0o600)

        atomic_write_bytes(target, b"new", mode=0o644)

        assert _mode(target) == 0o644

    def test_writing_through_a_symlink_updates_the_target_and_keeps_the_link(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "real" / "checklist.md"
        real.parent.mkdir()
        real.write_text("old\n")
        link = tmp_path / "checklist.md"
        link.symlink_to(real)

        atomic_write_text(link, "new\n")

        assert link.is_symlink()
        assert real.read_text() == "new\n"

    def test_failed_replace_leaves_no_temp_file_and_the_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "data.txt"
        target.write_bytes(b"original")

        def fail(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="simulated replace failure"):
            atomic_write_bytes(target, b"replacement")

        assert target.read_bytes() == b"original"
        assert _names(tmp_path) == ["data.txt"]
