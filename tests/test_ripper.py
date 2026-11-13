from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_tagger.ripper import (
    RipResult,
    _check_tool,
    parse_accuraterip,
    read_disc_id,
    rip_cd,
)


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
            _log(_track_block(1, EXACT, EXACT), _track_block(2, EXACT, EXACT)), expected_tracks=2
        )

        assert summary.exact == [1, 2]
        assert summary.mismatched == []
        assert summary.absent == []
        assert summary.not_ok == []
        assert summary.needs_review is False

    def test_either_version_exact_counts_as_exact(self) -> None:
        summary = parse_accuraterip(_log(_track_block(1, MISMATCH, EXACT)), expected_tracks=1)

        assert summary.exact == [1]

    def test_absent_tracks_do_not_need_review(self) -> None:
        summary = parse_accuraterip(_log(_track_block(1, ABSENT, ABSENT)), expected_tracks=1)

        assert summary.absent == [1]
        assert summary.needs_review is False

    def test_mismatch_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT), _track_block(2, MISMATCH, MISMATCH)),
            expected_tracks=2,
        )

        assert summary.mismatched == [2]
        assert summary.needs_review is True

    def test_bad_copy_status_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT, status="Copy NOT OK")), expected_tracks=1
        )

        assert summary.not_ok == [(1, "Copy NOT OK")]
        assert summary.needs_review is True

    def test_describe(self) -> None:
        summary = parse_accuraterip(
            _log(
                _track_block(1, EXACT, EXACT),
                _track_block(2, MISMATCH, MISMATCH),
                _track_block(3, ABSENT, ABSENT),
            ),
            expected_tracks=3,
        )

        assert summary.describe() == [
            "AccurateRip: 1 exact, 1 mismatched, 1 not in database",
            "Mismatched tracks: 2",
        ]


class TestAccurateRipEvidence:
    def test_empty_evidence_needs_review(self) -> None:
        summary = parse_accuraterip("unexpected log format", expected_tracks=3)

        assert summary.needs_review is True
        assert summary.missing == [1, 2, 3]

    def test_missing_section_is_listed_and_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT), _track_block(2, EXACT, EXACT)),
            expected_tracks=3,
        )

        assert summary.missing == [3]
        assert summary.needs_review is True
        assert "Missing from log: 3" in summary.describe()

    def test_unexpected_track_number_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT), _track_block(4, EXACT, EXACT)),
            expected_tracks=3,
        )

        assert summary.unexpected == [4]
        assert summary.needs_review is True
        assert "Unexpected in log: 4" in summary.describe()

    def test_complete_absent_tracks_do_not_need_review(self) -> None:
        summary = parse_accuraterip(
            _log(
                _track_block(1, ABSENT, ABSENT),
                _track_block(2, ABSENT, ABSENT),
                _track_block(3, ABSENT, ABSENT),
            ),
            expected_tracks=3,
        )

        assert summary.missing == []
        assert summary.needs_review is False


class TestHiddenTrack:
    def test_track_zero_is_expected_with_a_hidden_track(self) -> None:
        summary = parse_accuraterip(
            _log(
                _track_block(0, ABSENT, ABSENT),
                _track_block(1, EXACT, EXACT),
                _track_block(2, EXACT, EXACT),
            ),
            expected_tracks=2,
            hidden_track=True,
        )

        assert summary.missing == []
        assert summary.unexpected == []
        assert summary.needs_review is False

    def test_track_zero_without_a_hidden_track_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(0, ABSENT, ABSENT), _track_block(1, EXACT, EXACT)),
            expected_tracks=1,
        )

        assert summary.unexpected == [0]
        assert summary.needs_review is True

    def test_missing_track_zero_needs_review(self) -> None:
        summary = parse_accuraterip(
            _log(_track_block(1, EXACT, EXACT)), expected_tracks=1, hidden_track=True
        )

        assert summary.missing == [0]
        assert summary.needs_review is True

    def test_rip_counts_numbered_tracks_and_flags_the_hidden_one(self, tmp_path: Path) -> None:
        output = tmp_path / "output"

        def fake_whipper(cmd: list[str], **kwargs: object) -> MagicMock:
            out = Path(cmd[cmd.index("-O") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "00. Hidden Track One Audio.flac").write_bytes(b"\x00" * 50)
            for i in range(1, 3):
                (out / f"{i:02d}. Track {i}.flac").write_bytes(b"\x00" * 50)
            (out / "Artist - Album.log").write_text("rip log")
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=fake_whipper),
        ):
            result = rip_cd(output, device="/dev/sr0")

        assert result.track_count == 2
        assert len(result.tracks) == 3
        assert result.hidden_track is True

    def test_rip_command_verifies_a_disc_with_a_hidden_track(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from music_tagger.cli.rip import _rip
        from music_tagger.ripper import RipResult

        log_file = tmp_path / "rip.log"
        log_file.write_text(
            _log(
                _track_block(0, ABSENT, ABSENT),
                _track_block(1, EXACT, EXACT),
                _track_block(2, EXACT, EXACT),
            )
        )
        with patch("music_tagger.ripper.rip_cd") as rip:
            rip.return_value = RipResult(
                tracks=[], track_count=2, log_file=log_file, hidden_track=True
            )
            _rip([str(tmp_path / "out")])

        out = capsys.readouterr().out
        assert "Ripped 2 tracks plus the hidden track before track 1." in out
        assert "verified; safe to eject" in out


class TestRipCommandMissingEvidence:
    def test_log_without_tracks_section_is_not_verified(
        self, tmp_path: Path, capsys: object
    ) -> None:
        from music_tagger.cli.common import EXIT_REVIEW
        from music_tagger.cli.rip import _rip
        from music_tagger.ripper import RipResult

        log_file = tmp_path / "rip.log"
        log_file.write_text("Log created by: whipper 0.10.0 (internal logger)\n")
        with patch("music_tagger.ripper.rip_cd") as rip:
            rip.return_value = RipResult(tracks=[], track_count=2, log_file=log_file)
            with pytest.raises(SystemExit) as error:
                _rip([str(tmp_path / "out")])

        assert error.value.code == EXIT_REVIEW
        assert "verified; safe to eject" not in capsys.readouterr().out  # type: ignore[attr-defined]


class TestRipCommandAccurateRip:
    def _rip(self, tmp_path: Path, log_text: str) -> None:
        from music_tagger.cli.rip import _rip
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
        from music_tagger.cli.common import EXIT_REVIEW

        with pytest.raises(SystemExit) as error:
            self._rip(tmp_path, _log(_track_block(1, MISMATCH, MISMATCH)))

        assert error.value.code == EXIT_REVIEW


FIXTURE_FLAC = Path(__file__).parent / "fixtures" / "album_flac" / "01 - Track 1.flac"


def _whipper_disc(
    tracks: list[str], disc: tuple[int, int] | None, title: str = "Album"
) -> Callable[..., MagicMock]:
    """A fake whipper that writes tagged FLACs, a CUE, and a log, as 0.10 does."""
    import shutil

    from mutagen.flac import FLAC

    def run(cmd: list[str], **kwargs: object) -> MagicMock:
        out = Path(cmd[cmd.index("-O") + 1])
        assert not list(out.glob("*.log")), "whipper refuses a directory holding a .log"
        for name in tracks:
            shutil.copy(FIXTURE_FLAC, out / name)
            audio = FLAC(out / name)
            if disc:
                audio["DISCNUMBER"], audio["DISCTOTAL"] = str(disc[0]), str(disc[1])
            audio.save()
        (out / f"Artist - {title}.cue").write_text(
            "".join(f'FILE "{name}" WAVE\n' for name in tracks)
        )
        (out / f"Artist - {title}.log").write_text("rip log")
        return MagicMock(returncode=0)

    return run


def _rip_disc(output: Path, whipper: Callable[..., MagicMock], replace: bool = False) -> RipResult:
    with (
        patch("shutil.which", return_value="/usr/bin/whipper"),
        patch("music_tagger.ripper.subprocess.run", side_effect=whipper),
    ):
        return rip_cd(output, replace=replace)


class TestRipDiscs:
    def test_single_disc_keeps_whipper_names(self, tmp_path: Path) -> None:
        result = _rip_disc(tmp_path, _whipper_disc(["01. A.flac"], (1, 1)))

        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "01. A.flac",
            "Artist - Album.cue",
            "Artist - Album.log",
        ]
        assert result.is_multi_disc is False

    def test_discs_of_one_album_share_the_output_directory(self, tmp_path: Path) -> None:
        _rip_disc(tmp_path, _whipper_disc(["01. A.flac"], (1, 2), "Album (Disc 1 of 2)"))
        result = _rip_disc(
            tmp_path, _whipper_disc(["01. A.flac", "02. B.flac"], (2, 2), "Album (Disc 2 of 2)")
        )

        assert (result.disc_number, result.disc_total) == (2, 2)
        assert [path.name for path in result.tracks] == ["2-01. A.flac", "2-02. B.flac"]
        assert sorted(path.name for path in tmp_path.glob("*.flac")) == [
            "1-01. A.flac",
            "2-01. A.flac",
            "2-02. B.flac",
        ]
        cue = tmp_path / "Artist - Album (Disc 2 of 2).cue"
        assert cue.read_text() == 'FILE "2-01. A.flac" WAVE\nFILE "2-02. B.flac" WAVE\n'
        assert not [path for path in tmp_path.iterdir() if path.is_dir()]

    def test_same_disc_again_is_refused_and_kept_aside(self, tmp_path: Path) -> None:
        whipper = _whipper_disc(["01. A.flac"], (1, 2), "Album (Disc 1 of 2)")
        _rip_disc(tmp_path, whipper)
        before = (tmp_path / "1-01. A.flac").read_bytes()

        with pytest.raises(RuntimeError, match="already in"):
            _rip_disc(tmp_path, whipper)

        assert (tmp_path / "1-01. A.flac").read_bytes() == before
        assert len([path for path in tmp_path.iterdir() if path.is_dir()]) == 1

    def test_replace_overwrites_an_earlier_rip(self, tmp_path: Path) -> None:
        whipper = _whipper_disc(["01. A.flac"], (1, 2), "Album (Disc 1 of 2)")
        _rip_disc(tmp_path, whipper)
        (tmp_path / "1-01. A.flac").write_bytes(b"stale")

        _rip_disc(tmp_path, whipper, replace=True)

        assert (tmp_path / "1-01. A.flac").read_bytes() != b"stale"

    def test_unreadable_flac_counts_as_single_disc(self, tmp_path: Path) -> None:
        from music_tagger.ripper import read_disc_position

        path = tmp_path / "bad.flac"
        path.write_bytes(b"\x00" * 10)

        assert read_disc_position(path) == (None, None)


class TestUnknownDisc:
    def test_position_is_written_into_the_tags_and_prefixed(self, tmp_path: Path) -> None:
        from music_tagger.ripper import read_disc_position

        whipper = _whipper_disc(["01. Unknown Track 1.flac", "02. Unknown Track 2.flac"], None)
        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=whipper),
        ):
            result = rip_cd(tmp_path, unknown=True, unknown_disc=(2, 2))

        assert [path.name for path in result.tracks] == [
            "2-01. Unknown Track 1.flac",
            "2-02. Unknown Track 2.flac",
        ]
        assert (result.disc_number, result.disc_total) == (2, 2)
        assert read_disc_position(result.tracks[0]) == (2, 2)
        assert (tmp_path / "Artist - Album.cue").read_text() == (
            'FILE "2-01. Unknown Track 1.flac" WAVE\nFILE "2-02. Unknown Track 2.flac" WAVE\n'
        )

    def test_second_unknown_disc_joins_the_first_without_collision(self, tmp_path: Path) -> None:
        first = _whipper_disc(["01. Unknown Track 1.flac"], None, "Unknown disc one")
        second = _whipper_disc(["01. Unknown Track 1.flac"], None, "Unknown disc two")
        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=first),
        ):
            rip_cd(tmp_path, unknown=True)
        disc_one = (tmp_path / "01. Unknown Track 1.flac").read_bytes()
        with (
            patch("shutil.which", return_value="/usr/bin/whipper"),
            patch("music_tagger.ripper.subprocess.run", side_effect=second),
        ):
            result = rip_cd(tmp_path, unknown=True, unknown_disc=(2, 2))

        assert sorted(path.name for path in tmp_path.glob("*.flac")) == [
            "01. Unknown Track 1.flac",
            "2-01. Unknown Track 1.flac",
        ]
        assert (tmp_path / "01. Unknown Track 1.flac").read_bytes() == disc_one
        assert result.disc_number == 2

    def test_unknown_disc_needs_unknown(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="needs unknown=True"):
            rip_cd(tmp_path, unknown_disc=(2, 2))

    def test_unknown_disc_number_must_be_one_of_the_total(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not one of 2 discs"):
            rip_cd(tmp_path, unknown=True, unknown_disc=(3, 2))


class TestRipCommandDiscs:
    def _rip(self, tmp_path: Path, disc: tuple[int, int] | None) -> str:
        from music_tagger.cli.rip import _rip

        log_file = tmp_path / "rip.log"
        log_file.write_text(_log(_track_block(1, EXACT, EXACT)))
        number, total = disc or (None, None)
        with patch("music_tagger.ripper.rip_cd") as rip:
            rip.return_value = RipResult(
                tracks=[], track_count=1, log_file=log_file, disc_number=number, disc_total=total
            )
            _rip([str(tmp_path / "out")])
        return str(tmp_path / "out")

    def test_first_of_two_discs_asks_for_the_next(self, tmp_path: Path, capsys: object) -> None:
        target = self._rip(tmp_path, (1, 2))

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "Disc 1 of 2 verified; safe to eject." in out
        assert f"insert disc 2, then run:\n  uv run music-tagger rip {target}" in out

    def test_last_disc_moves_on_to_candidates(self, tmp_path: Path, capsys: object) -> None:
        self._rip(tmp_path, (2, 2))

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "Disc 2 of 2 verified; safe to eject." in out
        assert "music-tagger candidates" in out

    def test_single_disc(self, tmp_path: Path, capsys: object) -> None:
        self._rip(tmp_path, None)

        assert "Disc verified; safe to eject." in capsys.readouterr().out  # type: ignore[attr-defined]


class TestRipCommandUnknownDisc:
    def _run(self, tmp_path: Path, argv: list[str]) -> MagicMock:
        from music_tagger.cli.rip import _rip

        log_file = tmp_path / "rip.log"
        log_file.write_text(_log(_track_block(1, EXACT, EXACT)))
        with patch("music_tagger.ripper.rip_cd") as rip:
            rip.return_value = RipResult(
                tracks=[], track_count=1, log_file=log_file, disc_number=2, disc_total=2
            )
            _rip([str(tmp_path / "out"), *argv])
        return rip

    def test_passes_the_position_to_rip_cd(self, tmp_path: Path) -> None:
        rip = self._run(tmp_path, ["--unknown", "--disc", "2", "--of", "2"])

        assert rip.call_args.kwargs["unknown"] is True
        assert rip.call_args.kwargs["unknown_disc"] == (2, 2)

    def test_no_position_passes_none(self, tmp_path: Path) -> None:
        rip = self._run(tmp_path, ["--unknown"])

        assert rip.call_args.kwargs["unknown_disc"] is None

    def test_disc_needs_unknown(self, tmp_path: Path, capsys: object) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self._run(tmp_path, ["--disc", "2", "--of", "2"])

        assert exit_info.value.code == 2
        assert "--disc needs --unknown" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_disc_and_total_come_together(self, tmp_path: Path, capsys: object) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self._run(tmp_path, ["--unknown", "--disc", "2"])

        assert exit_info.value.code == 2
        assert "must be given together" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_disc_must_be_one_of_the_total(self, tmp_path: Path, capsys: object) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self._run(tmp_path, ["--unknown", "--disc", "3", "--of", "2"])

        assert exit_info.value.code == 2
        assert "is not one of --of 2 discs" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_next_disc_repeats_the_unknown_flags(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli.rip import _rip

        log_file = tmp_path / "rip.log"
        log_file.write_text(_log(_track_block(1, EXACT, EXACT)))
        target = tmp_path / "out"
        with patch("music_tagger.ripper.rip_cd") as rip:
            rip.return_value = RipResult(
                tracks=[], track_count=1, log_file=log_file, disc_number=1, disc_total=2
            )
            _rip([str(target), "--unknown", "--disc", "1", "--of", "2"])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert f"  uv run music-tagger rip {target} --unknown --disc 2 --of 2" in out
