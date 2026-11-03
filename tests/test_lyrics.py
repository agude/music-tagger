from __future__ import annotations

import hashlib
import shutil
import struct
from pathlib import Path

import pytest
from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from music_tagger.lyrics import (
    MISSING,
    UNREADABLE,
    LyricsEntry,
    LyricsKind,
    Source,
    TrackLyrics,
    classify,
    inspect_album,
    inspect_track,
)
from music_tagger.tags import EmbeddedLyrics, UnsupportedFileError, read_embedded_lyrics

FLAC_TRACK = "01 - Track 1.flac"
MP3_TRACK = "01 - Track 1.mp3"
M4A_TRACK = "01 - Track 1.m4a"


def _set_flac_lyrics(path: Path, *values: str) -> None:
    audio = FLAC(path)
    audio["LYRICS"] = list(values)
    audio.save()


def _set_mp3_uslt(path: Path, text: str, lang: str = "eng", desc: str = "") -> None:
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()  # type: ignore[no-untyped-call]
    assert audio.tags is not None
    audio.tags.add(USLT(encoding=3, lang=lang, desc=desc, text=text))
    audio.save()


def _set_m4a_lyrics(path: Path, text: str) -> None:
    audio = MP4(path)
    audio["\xa9lyr"] = [text]  # type: ignore[no-untyped-call]
    audio.save()


def _minimal_wav() -> bytes:
    """A valid one-sample WAVE file: mutagen identifies it, and it is not a lyric format."""
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    data = b"\x00\x00"
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestClassify:
    def test_plain_text(self) -> None:
        assert classify("Hello there\nGeneral Kenobi") == LyricsKind.PLAIN

    def test_empty_and_whitespace(self) -> None:
        assert classify("") == LyricsKind.EMPTY
        assert classify("  \n\t\n") == LyricsKind.EMPTY

    def test_metadata_only_is_empty(self) -> None:
        assert classify("[ar:Artist]\n[ti:Title]\n", expect_timed=True) == LyricsKind.EMPTY

    def test_timed_lines_are_synchronized(self) -> None:
        text = "[00:12.34]First line\n[00:15.00]Second line"
        assert classify(text, expect_timed=True) == LyricsKind.SYNCHRONIZED

    def test_several_timestamps_on_one_line(self) -> None:
        assert classify("[00:12.00][01:30.5]Chorus") == LyricsKind.SYNCHRONIZED

    def test_colon_separated_centiseconds(self) -> None:
        assert classify("[00:12:50]Line") == LyricsKind.SYNCHRONIZED

    def test_timestamp_only_line_is_synchronized(self) -> None:
        assert classify("[00:05.00]", expect_timed=True) == LyricsKind.SYNCHRONIZED

    def test_metadata_header_before_timed_lines(self) -> None:
        text = "[ti:Song]\n[offset:+500]\n[00:01.00]Words"
        assert classify(text, expect_timed=True) == LyricsKind.SYNCHRONIZED

    def test_out_of_range_seconds_are_malformed(self) -> None:
        assert classify("[00:75.00]Line") == LyricsKind.MALFORMED

    def test_bracketed_label_is_plain_text(self) -> None:
        assert classify("[Chorus]\nLa la la") == LyricsKind.PLAIN

    def test_untimed_text_in_lrc_is_malformed(self) -> None:
        assert classify("Just words\nno times", expect_timed=True) == LyricsKind.MALFORMED

    def test_timed_text_in_txt_is_malformed(self) -> None:
        assert classify("[00:01.00]Words", expect_timed=False) == LyricsKind.MALFORMED

    def test_unicode_plain_text(self) -> None:
        assert classify("Señor 日本語") == LyricsKind.PLAIN


class TestEmbeddedFormats:
    def test_flac_plain(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / FLAC_TRACK, "Plain words")
        track = inspect_track(flac_album / FLAC_TRACK)
        assert track.entries == [LyricsEntry(source=Source.EMBEDDED, kind=LyricsKind.PLAIN)]
        assert track.status == "plain"

    def test_flac_synchronized(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / FLAC_TRACK, "[00:01.00]Timed")
        assert inspect_track(flac_album / FLAC_TRACK).status == "synchronized"

    def test_flac_without_lyrics_is_missing(self, flac_album: Path) -> None:
        track = inspect_track(flac_album / FLAC_TRACK)
        assert track.entries == []
        assert track.status == MISSING

    def test_flac_empty_value_is_empty(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / FLAC_TRACK, "")
        assert inspect_track(flac_album / FLAC_TRACK).status == "empty"

    def test_mp3_uslt_keeps_language_and_description(self, mp3_album: Path) -> None:
        _set_mp3_uslt(mp3_album / MP3_TRACK, "Plain words", lang="eng", desc="")
        track = inspect_track(mp3_album / MP3_TRACK)
        assert track.entries == [
            LyricsEntry(
                source=Source.EMBEDDED,
                kind=LyricsKind.PLAIN,
                language="eng",
                description="",
            )
        ]

    def test_mp3_several_uslt_frames_are_reported_separately(self, mp3_album: Path) -> None:
        path = mp3_album / MP3_TRACK
        _set_mp3_uslt(path, "English words", lang="eng")
        _set_mp3_uslt(path, "[00:01.00]Deutsch", lang="deu", desc="german")
        track = inspect_track(path)
        assert [(e.language, e.description, e.kind) for e in track.entries] == [
            ("eng", "", LyricsKind.PLAIN),
            ("deu", "german", LyricsKind.SYNCHRONIZED),
        ]
        assert track.status == "synchronized"

    def test_m4a_plain(self, m4a_album: Path) -> None:
        _set_m4a_lyrics(m4a_album / M4A_TRACK, "Plain words")
        track = inspect_track(m4a_album / M4A_TRACK)
        assert track.entries == [LyricsEntry(source=Source.EMBEDDED, kind=LyricsKind.PLAIN)]

    def test_m4a_without_lyrics_is_missing(self, m4a_album: Path) -> None:
        assert inspect_track(m4a_album / M4A_TRACK).status == MISSING

    def test_read_embedded_lyrics_unsupported_format(self, tmp_path: Path) -> None:
        path = tmp_path / "tone.wav"
        path.write_bytes(_minimal_wav())
        with pytest.raises(UnsupportedFileError):
            read_embedded_lyrics(path)

    def test_read_embedded_lyrics_garbage_raises_mutagen_error(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.flac"
        path.write_bytes(b"not an audio file")
        with pytest.raises(MutagenError):
            read_embedded_lyrics(path)

    def test_read_embedded_returns_values(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / FLAC_TRACK, "One", "Two")
        assert read_embedded_lyrics(flac_album / FLAC_TRACK) == [
            EmbeddedLyrics(text="One"),
            EmbeddedLyrics(text="Two"),
        ]


class TestSidecars:
    def test_lrc_matched_by_basename(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.lrc").write_text("[00:01.00]Timed\n", encoding="utf-8")
        track = inspect_track(flac_album / FLAC_TRACK)
        assert track.entries == [
            LyricsEntry(
                source=Source.LRC,
                kind=LyricsKind.SYNCHRONIZED,
                path=flac_album / "01 - Track 1.lrc",
            )
        ]

    def test_txt_matched_by_basename(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain words\n", encoding="utf-8")
        track = inspect_track(flac_album / FLAC_TRACK)
        assert [(e.source, e.kind) for e in track.entries] == [(Source.TXT, LyricsKind.PLAIN)]

    def test_embedded_and_both_sidecars_reported_separately(self, flac_album: Path) -> None:
        path = flac_album / FLAC_TRACK
        _set_flac_lyrics(path, "Embedded words")
        (flac_album / "01 - Track 1.lrc").write_text("[00:01.00]Timed", encoding="utf-8")
        (flac_album / "01 - Track 1.txt").write_text("Plain", encoding="utf-8")
        track = inspect_track(path)
        assert sorted(e.source.value for e in track.entries) == ["embedded", "lrc", "txt"]

    def test_sidecar_for_another_track_is_not_matched(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1 (copy).lrc").write_text("[00:01.00]X", encoding="utf-8")
        assert inspect_track(flac_album / FLAC_TRACK).status == MISSING

    def test_unrelated_txt_is_ignored_by_album(self, flac_album: Path) -> None:
        (flac_album / "notes.txt").write_text("Recording notes", encoding="utf-8")
        album = inspect_album(flac_album)
        assert album.coverage() == {MISSING: 3}

    def test_disc_prefix_and_unicode_names(self, flac_album: Path) -> None:
        name = "1-01. Ünïcödé Title"
        shutil.copy(flac_album / FLAC_TRACK, flac_album / f"{name}.flac")
        (flac_album / f"{name}.lrc").write_text("[00:01.00]Timed", encoding="utf-8")
        track = inspect_track(flac_album / f"{name}.flac")
        assert [e.source for e in track.entries] == [Source.LRC]

    def test_utf8_bom_is_accepted(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_bytes(b"\xef\xbb\xbfPlain words")
        assert inspect_track(flac_album / FLAC_TRACK).status == "plain"

    def test_latin1_lrc_is_malformed(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.lrc").write_bytes("[00:01.00]Señor".encode("latin-1"))
        assert inspect_track(flac_album / FLAC_TRACK).status == "malformed"

    def test_utf16_txt_is_malformed(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_bytes("Plain".encode("utf-16"))
        assert inspect_track(flac_album / FLAC_TRACK).status == "malformed"

    def test_empty_lrc_is_empty(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.lrc").write_bytes(b"")
        assert inspect_track(flac_album / FLAC_TRACK).status == "empty"

    def test_lrc_without_timestamps_is_malformed(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.lrc").write_text("Words only", encoding="utf-8")
        assert inspect_track(flac_album / FLAC_TRACK).status == "malformed"


class TestAlbumInspection:
    def test_all_missing(self, flac_album: Path) -> None:
        album = inspect_album(flac_album)
        assert len(album.tracks) == 3
        assert album.coverage() == {MISSING: 3}

    def test_mixed_album_coverage(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / "01 - Track 1.flac", "Plain")
        (flac_album / "02 - Track 2.lrc").write_text("[00:01.00]Timed", encoding="utf-8")
        (flac_album / "03 - Track 3.txt").write_text("[00:01.00]Wrong", encoding="utf-8")
        album = inspect_album(flac_album)
        assert [t.status for t in album.tracks] == ["plain", "synchronized", "malformed"]
        assert album.coverage() == {"plain": 1, "synchronized": 1, "malformed": 1}

    def test_mixed_album_with_missing_track(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / "01 - Track 1.flac", "Plain")
        album = inspect_album(flac_album)
        assert album.coverage() == {"plain": 1, MISSING: 2}

    def test_tracks_in_name_order(self, flac_album: Path) -> None:
        album = inspect_album(flac_album)
        assert [t.path.name for t in album.tracks] == [
            "01 - Track 1.flac",
            "02 - Track 2.flac",
            "03 - Track 3.flac",
        ]

    def test_unreadable_audio_is_reported_and_others_still_read(self, flac_album: Path) -> None:
        (flac_album / "04 - Broken.flac").write_bytes(b"garbage, not audio")
        (flac_album / "04 - Broken.txt").write_text("Plain", encoding="utf-8")
        album = inspect_album(flac_album)
        broken = next(t for t in album.tracks if t.path.name == "04 - Broken.flac")
        assert broken.status == UNREADABLE
        assert broken.errors
        assert [e.source for e in broken.entries] == [Source.TXT]
        assert album.coverage()[UNREADABLE] == 1

    def test_inspection_writes_nothing(self, flac_album: Path) -> None:
        _set_flac_lyrics(flac_album / FLAC_TRACK, "Plain")
        (flac_album / "01 - Track 1.lrc").write_text("[00:01.00]Timed", encoding="utf-8")
        before = _digest_tree(flac_album)
        inspect_album(flac_album)
        assert _digest_tree(flac_album) == before


class TestTrackStatusRanking:
    @staticmethod
    def _track(*kinds: LyricsKind) -> TrackLyrics:
        entries = [LyricsEntry(source=Source.TXT, kind=kind) for kind in kinds]
        return TrackLyrics(path=Path("/a.flac"), entries=entries)

    def test_synchronized_beats_plain(self) -> None:
        assert self._track(LyricsKind.PLAIN, LyricsKind.SYNCHRONIZED).status == "synchronized"

    def test_plain_beats_malformed(self) -> None:
        assert self._track(LyricsKind.MALFORMED, LyricsKind.PLAIN).status == "plain"

    def test_malformed_beats_empty(self) -> None:
        assert self._track(LyricsKind.EMPTY, LyricsKind.MALFORMED).status == "malformed"

    def test_empty_alone(self) -> None:
        assert self._track(LyricsKind.EMPTY).status == "empty"

    def test_no_entries_is_missing(self) -> None:
        assert self._track().status == MISSING

    def test_error_overrides_good_sources(self) -> None:
        track = self._track(LyricsKind.SYNCHRONIZED)
        track.errors.append("embedded: broken")
        assert track.status == UNREADABLE
