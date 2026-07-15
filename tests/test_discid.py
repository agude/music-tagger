from __future__ import annotations

from pathlib import Path

import pytest

from music_tagger.discid import (
    TOCInfo,
    _compute_disc_id,
    _msf_to_frames,
    parse_cue,
    parse_rip_dir,
)

SAMPLE_CUE = """\
REM GENRE Rock
REM DATE 1973
PERFORMER "Eagles"
TITLE "Desperado"
FILE "Eagles - Desperado.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Doolin-Dalton"
    PERFORMER "Eagles"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Twenty-One"
    PERFORMER "Eagles"
    INDEX 01 03:29:37
  TRACK 03 AUDIO
    TITLE "Out of Control"
    PERFORMER "Eagles"
    INDEX 01 06:36:50
"""


class TestMsfToFrames:
    def test_zero(self) -> None:
        assert _msf_to_frames(0, 0, 0) == 0

    def test_one_minute(self) -> None:
        assert _msf_to_frames(1, 0, 0) == 4500

    def test_mixed(self) -> None:
        assert _msf_to_frames(3, 29, 37) == 15712


class TestComputeDiscId:
    def test_produces_base64_string(self) -> None:
        disc_id = _compute_disc_id(1, 3, 200000, [150, 15000, 30000])
        assert isinstance(disc_id, str)
        assert len(disc_id) == 28

    def test_deterministic(self) -> None:
        a = _compute_disc_id(1, 2, 100000, [150, 50000])
        b = _compute_disc_id(1, 2, 100000, [150, 50000])
        assert a == b

    def test_different_inputs(self) -> None:
        a = _compute_disc_id(1, 2, 100000, [150, 50000])
        b = _compute_disc_id(1, 2, 100000, [150, 60000])
        assert a != b


class TestParseCue:
    def test_parses_sample(self, tmp_path: Path) -> None:
        cue = tmp_path / "album.cue"
        cue.write_text(SAMPLE_CUE)
        toc = parse_cue(cue)
        assert toc.first_track == 1
        assert toc.last_track == 3
        assert len(toc.track_offsets) == 3
        assert toc.track_offsets[0] == 150
        assert toc.track_offsets[1] == _msf_to_frames(3, 29, 37) + 150

    def test_no_index_raises(self, tmp_path: Path) -> None:
        cue = tmp_path / "bad.cue"
        cue.write_text("TITLE 'empty'\n")
        with pytest.raises(ValueError, match="No INDEX"):
            parse_cue(cue)


class TestParseRipDir:
    def test_parses_cue(self, tmp_path: Path) -> None:
        cue = tmp_path / "album.cue"
        cue.write_text(SAMPLE_CUE)
        toc = parse_rip_dir(tmp_path)
        assert toc.last_track == 3

    def test_no_cue_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"No \.cue files"):
            parse_rip_dir(tmp_path)


class TestTOCInfo:
    def test_toc_string(self) -> None:
        toc = TOCInfo(
            first_track=1, last_track=3, leadout_offset=200000, track_offsets=[150, 15000, 30000]
        )
        assert toc.to_toc_string() == "1 3 200000 150 15000 30000"

    def test_to_dict(self) -> None:
        toc = TOCInfo(
            first_track=1,
            last_track=2,
            leadout_offset=100000,
            track_offsets=[150, 50000],
            disc_id="abc123",
        )
        d = toc.to_dict()
        assert d["first_track"] == 1
        assert d["last_track"] == 2
        assert d["disc_id"] == "abc123"
        assert d["toc_string"] == "1 2 100000 150 50000"

    def test_to_dict_no_disc_id(self) -> None:
        toc = TOCInfo(first_track=1, last_track=1, leadout_offset=0, track_offsets=[150])
        d = toc.to_dict()
        assert "disc_id" not in d
