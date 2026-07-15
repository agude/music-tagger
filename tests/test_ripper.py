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
    def test_rips_and_encodes(self, tmp_path: Path) -> None:
        def fake_cdparanoia(cmd, cwd, **kwargs):
            # Create fake WAV files in the cwd (tmpdir)
            cwd_path = Path(cwd)
            for i in range(1, 4):
                (cwd_path / f"track{i:02d}.cdda.wav").write_bytes(b"\x00" * 100)
            return MagicMock(returncode=0)

        def fake_flac(cmd, **kwargs):
            # Create the output file
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"\x00" * 50)
            return MagicMock(returncode=0)

        output = tmp_path / "output"

        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch("music_tagger.ripper.subprocess.run") as mock_run,
        ):

            def side_effect(cmd, **kwargs):
                if cmd[0] == "cdparanoia":
                    return fake_cdparanoia(cmd, **kwargs)
                elif cmd[0] == "flac":
                    return fake_flac(cmd, **kwargs)
                return MagicMock(returncode=0)

            mock_run.side_effect = side_effect
            result = rip_cd(output, device="/dev/sr0", track_count=3)

        assert result.track_count == 3
        assert len(result.tracks) == 3
        assert all(p.suffix == ".flac" for p in result.tracks)
        assert result.tracks[0].name == "01.flac"
        assert result.tracks[2].name == "03.flac"

    def test_wrong_track_count_raises(self, tmp_path: Path) -> None:
        def fake_cdparanoia(cmd, cwd, **kwargs):
            cwd_path = Path(cwd)
            for i in range(1, 3):  # Only 2 tracks
                (cwd_path / f"track{i:02d}.cdda.wav").write_bytes(b"\x00" * 100)
            return MagicMock(returncode=0)

        output = tmp_path / "output"

        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch("music_tagger.ripper.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = lambda cmd, **kw: fake_cdparanoia(cmd, **kw)

            with pytest.raises(RuntimeError, match="Expected 5 tracks"):
                rip_cd(output, track_count=5)

    def test_no_wav_files_raises(self, tmp_path: Path) -> None:
        output = tmp_path / "output"

        with (
            patch("shutil.which", return_value="/usr/bin/fake"),
            patch("music_tagger.ripper.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(RuntimeError, match="no WAV files"):
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
