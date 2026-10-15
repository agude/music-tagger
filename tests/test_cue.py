from __future__ import annotations

from pathlib import Path

from music_tagger.cue import missing_cue_references, rewrite_cue_references

CUE = (
    "REM DISCID 9A0B5F0C\r\n"
    'FILE "01. Long Day.flac" WAVE\r\n'
    "  TRACK 01 AUDIO\r\n"
    "    INDEX 01 00:00:00\r\n"
    'FILE "02. Push.flac" WAVE\r\n'
    "  TRACK 02 AUDIO\r\n"
)


class TestRewriteCueReferences:
    def test_rewrites_file_lines_and_keeps_crlf(self, tmp_path: Path) -> None:
        cue = tmp_path / "Album (Disc 2 of 2).cue"
        cue.write_bytes(CUE.encode())

        changed = rewrite_cue_references(tmp_path, {"01. Long Day.flac": "2-01. Long Day.flac"})

        assert changed == [cue]
        assert (
            cue.read_bytes() == CUE.replace('"01. Long Day.flac"', '"2-01. Long Day.flac"').encode()
        )

    def test_unrelated_cue_is_not_rewritten(self, tmp_path: Path) -> None:
        cue = tmp_path / "album.cue"
        cue.write_bytes(CUE.encode())

        assert rewrite_cue_references(tmp_path, {"other.flac": "x.flac"}) == []
        assert cue.read_bytes() == CUE.encode()

    def test_latin1_bytes_round_trip(self, tmp_path: Path) -> None:
        cue = tmp_path / "album.CUE"
        original = 'TITLE "Caf\xe9"\nFILE "01. A.flac" WAVE\n'.encode("latin-1")
        cue.write_bytes(original)

        rewrite_cue_references(tmp_path, {"01. A.flac": "01. B.flac"})

        assert cue.read_bytes() == original.replace(b"01. A.flac", b"01. B.flac")


class TestMissingCueReferences:
    def test_reports_files_not_in_the_directory(self, tmp_path: Path) -> None:
        cue = tmp_path / "album.cue"
        cue.write_text(CUE)
        (tmp_path / "02. Push.flac").write_bytes(b"")

        assert missing_cue_references(tmp_path) == [(cue, "01. Long Day.flac")]
