"""Embedding sidecar lyrics into tags: source choice, tag layout, replace rules, and the CLI."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from music_tagger.cli import lyrics_cmd
from music_tagger.cli.lyrics_cmd import _lyrics
from music_tagger.lrclib import LrclibClient
from music_tagger.lyrics import inspect_track
from music_tagger.lyrics_embed import apply_embed, parse_lrc, plan_embed
from music_tagger.lyrics_write import WriteStatus

TRACK = "01 - Track 1"
LRC = "[00:01.00]First line\n[00:04.50]Second line\n[00:08.00]\n[00:09.25]Last\n"
LRC_PAIRS = [("First line", 1000), ("Second line", 4500), ("", 8000), ("Last", 9250)]
PLAIN = "First line\nSecond line\n"


def _embed_file(path: Path, *, replace: bool = False) -> WriteStatus:
    plan = plan_embed(inspect_track(path), replace=replace)
    if plan.status is WriteStatus.WRITE:
        apply_embed(plan)
    return plan.status


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _without_lyrics(tags: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in tags.items() if k != key}


def _syncsafe(value: int) -> bytes:
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def _write_id3v22(path: Path) -> None:
    """Replace the MP3's ID3 tag with a v2.2 one (a TT2 title frame). mutagen cannot write v2.2."""
    data = path.read_bytes()
    if data.startswith(b"ID3"):
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        data = data[10 + size :]
    text = b"\x00Track 1"
    frame = b"TT2" + len(text).to_bytes(3, "big") + text
    path.write_bytes(b"ID3\x02\x00\x00" + _syncsafe(len(frame)) + frame + data)


class TestParseLrc:
    def test_fractions_are_tenths_hundredths_or_milliseconds(self) -> None:
        assert parse_lrc("[00:01.5]a\n[00:02.25]b\n[00:03.250]c") == [
            ("a", 1500),
            ("b", 2250),
            ("c", 3250),
        ]

    def test_minutes_and_repeated_timestamps(self) -> None:
        assert parse_lrc("[01:02.00][00:10.00]chorus\n[00:05.00]verse") == [
            ("verse", 5000),
            ("chorus", 10000),
            ("chorus", 62000),
        ]

    def test_metadata_and_blank_lines(self) -> None:
        assert parse_lrc("[ar:Band]\n[00:01.00]\n[00:02.00]x") == [("", 1000), ("x", 2000)]


class TestSourceChoice:
    def test_lrc_is_used_when_both_sidecars_exist(self, flac_album: Path) -> None:
        audio = flac_album / f"{TRACK}.flac"
        (flac_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        (flac_album / f"{TRACK}.txt").write_text(PLAIN, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        assert FLAC(audio)["LYRICS"] == [LRC.rstrip("\n")]

    def test_empty_lrc_falls_back_to_txt(self, flac_album: Path) -> None:
        audio = flac_album / f"{TRACK}.flac"
        (flac_album / f"{TRACK}.lrc").write_text("", encoding="utf-8")
        (flac_album / f"{TRACK}.txt").write_text(PLAIN, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        assert FLAC(audio)["LYRICS"] == [PLAIN.rstrip("\n")]

    def test_malformed_lrc_is_refused_and_nothing_is_written(self, flac_album: Path) -> None:
        audio = flac_album / f"{TRACK}.flac"
        (flac_album / f"{TRACK}.lrc").write_text("untimed words\n", encoding="utf-8")
        before = _digest_tree(flac_album)
        assert _embed_file(audio) is WriteStatus.REFUSED
        assert _digest_tree(flac_album) == before

    def test_no_sidecar_means_no_text(self, flac_album: Path) -> None:
        plan = plan_embed(inspect_track(flac_album / f"{TRACK}.flac"), replace=False)
        assert plan.status is WriteStatus.NO_TEXT


class TestFlac:
    def test_lrc_text_goes_to_lyrics_and_other_tags_survive(self, flac_album: Path) -> None:
        audio = flac_album / f"{TRACK}.flac"
        # mutagen keys Vorbis comments in lowercase when read as a dict.
        before = _without_lyrics(dict(FLAC(audio)), "lyrics")
        (flac_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        assert FLAC(audio)["LYRICS"] == [LRC.rstrip("\n")]
        assert _without_lyrics(dict(FLAC(audio)), "lyrics") == before

    def test_txt_text_goes_to_lyrics(self, flac_album: Path) -> None:
        audio = flac_album / f"{TRACK}.flac"
        (flac_album / f"{TRACK}.txt").write_text(PLAIN, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        assert FLAC(audio)["LYRICS"] == [PLAIN.rstrip("\n")]


class TestM4a:
    def test_lrc_text_goes_to_lyr_atom(self, m4a_album: Path) -> None:
        audio = m4a_album / f"{TRACK}.m4a"
        (m4a_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        assert MP4(audio)["\xa9lyr"] == [LRC.rstrip("\n")]

    def test_txt_text_goes_to_lyr_atom(self, m4a_album: Path) -> None:
        audio = m4a_album / f"{TRACK}.m4a"
        (m4a_album / f"{TRACK}.txt").write_text(PLAIN, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        assert MP4(audio)["\xa9lyr"] == [PLAIN.rstrip("\n")]


class TestMp3:
    def test_lrc_becomes_sylt_in_milliseconds_with_no_uslt(self, mp3_album: Path) -> None:
        audio = mp3_album / f"{TRACK}.mp3"
        (mp3_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        tags = MP3(audio).tags
        assert tags is not None
        sylt = tags.getall("SYLT")
        assert len(sylt) == 1
        assert (sylt[0].lang, sylt[0].format, sylt[0].type, sylt[0].desc) == (
            "eng",
            2,
            1,
            "",
        )
        assert [(t, ms) for t, ms in sylt[0].text] == LRC_PAIRS
        assert tags.getall("USLT") == []

    def test_txt_becomes_uslt_with_no_sylt(self, mp3_album: Path) -> None:
        audio = mp3_album / f"{TRACK}.mp3"
        (mp3_album / f"{TRACK}.txt").write_text(PLAIN, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        tags = MP3(audio).tags
        assert tags is not None
        assert [f.text for f in tags.getall("USLT") if f.lang == "eng" and f.desc == ""] == [
            PLAIN.rstrip("\n")
        ]
        assert tags.getall("SYLT") == []

    def test_other_language_frames_survive_and_id3_version_is_kept(self, mp3_album: Path) -> None:
        audio = mp3_album / f"{TRACK}.mp3"
        mp3 = MP3(audio)
        if mp3.tags is None:
            mp3.add_tags()  # type: ignore[no-untyped-call]
        assert mp3.tags is not None
        mp3.tags.add(USLT(encoding=3, lang="fra", desc="", text="Bonjour"))
        mp3.save(v2_version=3)  # type: ignore[no-untyped-call]

        (mp3_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE

        tags = MP3(audio).tags
        assert tags is not None
        assert tags.version[1] == 3
        assert [f.text for f in tags.getall("USLT") if f.lang == "fra"] == ["Bonjour"]
        assert [(t, ms) for t, ms in tags.getall("SYLT")[0].text] == LRC_PAIRS

    def test_writing_sylt_removes_a_managed_uslt(self, mp3_album: Path) -> None:
        audio = mp3_album / f"{TRACK}.mp3"
        mp3 = MP3(audio)
        if mp3.tags is None:
            mp3.add_tags()  # type: ignore[no-untyped-call]
        assert mp3.tags is not None
        mp3.tags.add(USLT(encoding=3, lang="eng", desc="", text="old plain"))
        mp3.save()  # type: ignore[no-untyped-call]

        (mp3_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.PRESERVED
        assert _embed_file(audio, replace=True) is WriteStatus.WRITE

        tags = MP3(audio).tags
        assert tags is not None
        assert tags.getall("USLT") == []
        assert len(tags.getall("SYLT")) == 1


class TestReplaceRules:
    def test_different_existing_lyrics_are_preserved_without_replace(
        self, flac_album: Path
    ) -> None:
        audio = flac_album / f"{TRACK}.flac"
        flac = FLAC(audio)
        flac["LYRICS"] = ["someone else's words"]
        flac.save()  # type: ignore[no-untyped-call]
        (flac_album / f"{TRACK}.txt").write_text(PLAIN, encoding="utf-8")
        before = _digest_tree(flac_album)

        assert _embed_file(audio) is WriteStatus.PRESERVED
        assert _digest_tree(flac_album) == before

        assert _embed_file(audio, replace=True) is WriteStatus.WRITE
        assert FLAC(audio)["LYRICS"] == [PLAIN.rstrip("\n")]

    def test_identical_text_is_unchanged_on_a_second_run(self, flac_album: Path) -> None:
        audio = flac_album / f"{TRACK}.flac"
        (flac_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        assert _embed_file(audio) is WriteStatus.WRITE
        before = _digest_tree(flac_album)
        assert _embed_file(audio, replace=True) is WriteStatus.UNCHANGED
        assert _digest_tree(flac_album) == before


class TestCli:
    @pytest.fixture()
    def no_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def factory() -> LrclibClient:
            raise AssertionError("embedding must not construct an LRCLIB client")

        monkeypatch.setattr(lyrics_cmd, "LrclibClient", factory)

    def test_embed_writes_logs_and_makes_no_request(
        self, flac_album: Path, no_network: None
    ) -> None:
        audio = flac_album / f"{TRACK}.flac"
        (flac_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        _lyrics([str(flac_album), "--embed"])
        assert FLAC(audio)["LYRICS"] == [LRC.rstrip("\n")]
        log_text = Path(os.environ["MUSIC_TAGGER_LOG"]).read_text(encoding="utf-8")
        assert f"embedded `{TRACK}.lrc` as LYRICS (new tag)" in log_text
        assert "First line" not in log_text

    def test_dry_run_writes_nothing_and_logs_nothing(
        self, flac_album: Path, no_network: None
    ) -> None:
        (flac_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        before = _digest_tree(flac_album)
        _lyrics([str(flac_album), "--embed", "--dry-run"])
        assert _digest_tree(flac_album) == before
        log = Path(os.environ["MUSIC_TAGGER_LOG"])
        assert not log.exists() or log.read_text(encoding="utf-8") == ""

    def test_malformed_sidecar_exits_one(self, flac_album: Path, no_network: None) -> None:
        (flac_album / f"{TRACK}.lrc").write_text("untimed words\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _lyrics([str(flac_album), "--embed"])
        assert exc.value.code == 1

    def test_id3v22_track_is_failed_and_its_file_is_unchanged(
        self,
        mp3_album: Path,
        no_network: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audio = mp3_album / f"{TRACK}.mp3"
        _write_id3v22(audio)
        tags = MP3(audio).tags
        assert tags is not None
        assert tags.version[1] == 2
        (mp3_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        before = audio.read_bytes()

        with pytest.raises(SystemExit) as exc:
            _lyrics([str(mp3_album), "--embed"])

        assert exc.value.code == 1
        assert audio.read_bytes() == before
        assert "ID3v2.2" in capsys.readouterr().err

    def test_lyrics_id_is_rejected_with_embed(self, flac_album: Path, no_network: None) -> None:
        with pytest.raises(SystemExit) as exc:
            _lyrics([str(flac_album), "--embed", "--track", "1", "--lyrics-id", "5"])
        assert exc.value.code == 2

    def test_embed_reports_preserved_without_failing(
        self,
        flac_album: Path,
        no_network: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audio = flac_album / f"{TRACK}.flac"
        flac = FLAC(audio)
        flac["LYRICS"] = ["existing"]
        flac.save()  # type: ignore[no-untyped-call]
        (flac_album / f"{TRACK}.lrc").write_text(LRC, encoding="utf-8")
        _lyrics([str(flac_album), "--embed"])
        assert "Summary: 2 no_text, 1 preserved" in capsys.readouterr().out
        assert FLAC(audio)["LYRICS"] == ["existing"]
