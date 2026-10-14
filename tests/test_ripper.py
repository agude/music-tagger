from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_tagger.ripper import _check_tool, parse_accuraterip, read_disc_id, rip_cd


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


def _track_block(number: int, v1: str, v2: str, status: str = "Copy OK") -> str:
    return (
        f"  {number}:\n"
        f"    Filename: /tmp/rip-x/0{number}. Song.flac\n"
        f"    Copy CRC: BC8A83AA\n"
        f"    AccurateRip v1:\n"
        f"      Result: {v1}\n"
        f"      Confidence: 200\n"
        f"    AccurateRip v2:\n"
        f"      Result: {v2}\n"
        f"    Status: {status}\n"
        f"\n"
    )


EXACT = "Found, exact match"
MISMATCH = "Found, NO exact match"
ABSENT = "Track not present in AccurateRip database"


def _log(*blocks: str) -> str:
    """A whipper 0.10 log: a TOC section that also numbers tracks, then Tracks:."""
    toc = "TOC:\n  1:\n    Start: 00:00:00\n  2:\n    Start: 03:00:00\n\n"
    return (
        "Log created by: whipper 0.10.0 (internal logger)\n\n" + toc + "Tracks:\n" + "".join(blocks)
    )


class TestParseAccurateRip:
    def test_all_exact(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT), _track_block(2, EXACT, EXACT))
        )

        assert summary.exact == [1, 2]
        assert summary.mismatched == []
        assert summary.absent == []
        assert summary.not_ok == []
        assert summary.needs_review is False

    def test_either_version_exact_counts_as_exact(self) -> None:
        summary = parse_accuraterip(_log(_track_block(1, MISMATCH, EXACT)))

        assert summary.exact == [1]

    def test_absent_tracks_do_not_need_review(self) -> None:
        summary = parse_accuraterip(_log(_track_block(1, ABSENT, ABSENT)))

        assert summary.absent == [1]
        assert summary.needs_review is False

    def test_mismatch_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT), _track_block(2, MISMATCH, MISMATCH))
        )

        assert summary.mismatched == [2]
        assert summary.needs_review is True

    def test_bad_copy_status_needs_review(self) -> None:
        summary = parse_accuraterip(_log(_track_block(1, EXACT, EXACT, status="Copy NOT OK")))

        assert summary.not_ok == [(1, "Copy NOT OK")]
        assert summary.needs_review is True

    def test_describe(self) -> None:
        summary = parse_accuraterip(
            _log(
                _track_block(1, EXACT, EXACT),
                _track_block(2, MISMATCH, MISMATCH),
                _track_block(3, ABSENT, ABSENT),
            )
        )

        assert summary.describe() == [
            "AccurateRip: 1 exact, 1 mismatched, 1 not in database",
            "Mismatched tracks: 2",
        ]


class TestRipCommandAccurateRip:
    def _rip(self, tmp_path: Path, log_text: str) -> None:
        from music_tagger.cli import _rip
        from music_tagger.ripper import RipResult

        log_file = tmp_path / "rip.log"
        log_file.write_text(log_text)
        with patch("music_tagger.ripper.rip_cd") as rip:
            rip.return_value = RipResult(tracks=[], track_count=2, log_file=log_file)
            _rip([str(tmp_path / "out")])

    def test_prints_summary(self, tmp_path: Path, capsys: object) -> None:
        self._rip(tmp_path, _log(_track_block(1, EXACT, EXACT), _track_block(2, ABSENT, ABSENT)))

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "AccurateRip: 1 exact, 0 mismatched, 1 not in database" in out

    def test_mismatch_exits_review(self, tmp_path: Path) -> None:
        from music_tagger.cli import EXIT_REVIEW

        with pytest.raises(SystemExit) as error:
            self._rip(tmp_path, _log(_track_block(1, MISMATCH, MISMATCH)))

        assert error.value.code == EXIT_REVIEW
