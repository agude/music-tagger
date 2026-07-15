from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_tagger.ripper import _check_tool, read_disc_id, rip_cd


class TestCheckTool:
    def test_found(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/ls"):
            _check_tool("ls")

    def test_not_found(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="'fake' not found"),
        ):
            _check_tool("fake")


class TestRipCd:
    def test_rips_with_whipper(self, tmp_path: Path) -> None:
        output = tmp_path / "output"

        def fake_whipper(cmd: list[str], **kwargs: object) -> MagicMock:
            out = Path(cmd[cmd.index("-O") + 1])
            out.mkdir(parents=True, exist_ok=True)
            for i in range(1, 4):
                (out / f"{i:02d}. Track {i}.flac").write_bytes(b"\x00" * 50)
            (out / "Artist - Album.log").write_text("rip log")
            (out / "Artist - Album.cue").write_text("cue sheet")
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=fake_whipper),
        ):
            result = rip_cd(output, device="/dev/sr0")

        assert result.track_count == 3
        assert len(result.tracks) == 3
        assert all(p.suffix == ".flac" for p in result.tracks)
        assert result.log_file is not None
        assert result.cue_file is not None

    def test_passes_release_id(self, tmp_path: Path) -> None:
        output = tmp_path / "output"

        def fake_whipper(cmd: list[str], **kwargs: object) -> MagicMock:
            out = Path(cmd[cmd.index("-O") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "01. Track.flac").write_bytes(b"\x00" * 50)
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=fake_whipper) as mock_run,
        ):
            rip_cd(output, release_id="abc-123")

        cmd = mock_run.call_args[0][0]
        assert "-R" in cmd
        assert cmd[cmd.index("-R") + 1] == "abc-123"

    def test_passes_unknown_flag(self, tmp_path: Path) -> None:
        output = tmp_path / "output"

        def fake_whipper(cmd: list[str], **kwargs: object) -> MagicMock:
            out = Path(cmd[cmd.index("-O") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "01. Track.flac").write_bytes(b"\x00" * 50)
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=fake_whipper) as mock_run,
        ):
            rip_cd(output, unknown=True)

        cmd = mock_run.call_args[0][0]
        assert "-U" in cmd

    def test_no_flac_files_raises(self, tmp_path: Path) -> None:
        output = tmp_path / "output"

        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", return_value=MagicMock(returncode=0)),
            pytest.raises(RuntimeError, match="no FLAC files"),
        ):
            rip_cd(output)


class TestReadDiscId:
    def test_missing_discid_package(self) -> None:
        with (
            patch.dict("sys.modules", {"discid": None}),
            pytest.raises(RuntimeError, match="discid"),
        ):
            read_disc_id()

    def test_returns_disc_info(self) -> None:
        mock_track = MagicMock()
        mock_track.number = 1
        mock_track.offset = 150
        mock_track.sectors = 20000
        mock_track.seconds = 266

        mock_disc = MagicMock()
        mock_disc.id = "abc123"
        mock_disc.tracks = [mock_track]

        mock_discid = MagicMock()
        mock_discid.read.return_value = mock_disc

        with patch.dict("sys.modules", {"discid": mock_discid}):
            result = read_disc_id("/dev/sr0")

        assert result["disc_id"] == "abc123"
        assert result["track_count"] == 1
        assert result["tracks"][0]["number"] == 1
        assert result["tracks"][0]["seconds"] == 266
