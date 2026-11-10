from __future__ import annotations

import re
from pathlib import Path

import pytest

from music_tagger.discid import (
    TOCInfo,
    _compute_disc_id,
    parse_rip_dir,
    parse_whipper_log,
)

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "whipper" / "a-boy-named-goo.log"
FIXTURE_DISC_ID = "7tC7YhpmtJHHazk_KJODeabAwPo-"
FIXTURE_TOC = (
    "1 13 190645 182 15820 26997 43792 64057 76157 96415 111265 122582 137567 149072 166780 180312"
)


def _fixture_text() -> str:
    return FIXTURE_LOG.read_text()


class TestParseWhipperLog:
    def test_parses_fixture_disc_id_and_toc(self) -> None:
        toc = parse_whipper_log(_fixture_text())
        assert toc.disc_id == FIXTURE_DISC_ID
        assert toc.to_toc_string() == FIXTURE_TOC
        assert toc.source == "whipper"

    def test_url_toc_disagreeing_with_logged_id_raises(self) -> None:
        text = _fixture_text().replace("toc=1+13+190645+182+", "toc=1+13+190645+183+")
        with pytest.raises(ValueError) as error:
            parse_whipper_log(text)
        message = str(error.value)
        assert FIXTURE_DISC_ID in message
        assert len(re.findall(r"[A-Za-z0-9._-]{28}", message)) == 2

    def test_altered_toc_section_without_url_raises(self) -> None:
        text = "\n".join(
            line for line in _fixture_text().splitlines() if "MusicBrainz lookup URL" not in line
        )
        text = text.replace("Start sector: 15670", "Start sector: 15671")
        with pytest.raises(ValueError) as error:
            parse_whipper_log(text)
        message = str(error.value)
        assert FIXTURE_DISC_ID in message
        assert len(re.findall(r"[A-Za-z0-9._-]{28}", message)) == 2

    def test_without_url_computes_from_toc_section(self) -> None:
        text = "\n".join(
            line for line in _fixture_text().splitlines() if "MusicBrainz lookup URL" not in line
        )
        toc = parse_whipper_log(text)
        assert toc.disc_id == FIXTURE_DISC_ID
        assert toc.source == "computed"


class TestParseRipDir:
    def test_no_log_raises_naming_the_directory(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=re.escape(str(tmp_path))):
            parse_rip_dir(tmp_path)

    def test_two_logs_raise_asking_for_one(self, tmp_path: Path) -> None:
        for name in ["disc 1.log", "disc 2.log"]:
            (tmp_path / name).write_text(_fixture_text())
        with pytest.raises(ValueError, match="--log"):
            parse_rip_dir(tmp_path)


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
