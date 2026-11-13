"""Renaming whipper's per-disc files after an unknown rip is tagged."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from music_tagger.cli.library import _rename
from music_tagger.placement import unknown_disc_file_renames
from music_tagger.tags import read_album

DISC_ID_1 = "Q_e8TdUj6JRttcRaQtDkZNqD.XU-"


DISC_ID_2 = "0AEt3NH0Rf2IzabYJ3AWUD1qLek-"


def _unknown_rip(directory: Path, fixture: Path, *, tagged: bool) -> None:
    """Two discs of an unknown rip: one track each, whipper's per-disc files."""
    directory.mkdir()
    source = next(fixture.glob("*.flac"))
    for disc, disc_id, name in [
        (1, DISC_ID_1, "01. Unknown Track 1.flac"),
        (2, DISC_ID_2, "2-01. Unknown Track 1.flac"),
    ]:
        path = directory / name
        shutil.copy(source, path)
        audio = FLAC(path)  # type: ignore[no-untyped-call]
        audio.delete()  # type: ignore[no-untyped-call]
        audio["tracknumber"] = "1"
        audio["discnumber"] = str(disc)
        audio["disctotal"] = "2"
        audio["musicbrainz_discid"] = disc_id
        audio["title"] = f"Song {disc}" if tagged else "Unknown Track"
        audio["artist"] = "Little Feat" if tagged else "Unknown Artist"
        audio["album"] = "Dixie Chicken" if tagged else "Unknown Album"
        audio.save()
        stem = f"Unknown Artist - {disc_id}"
        for suffix in (".log", ".toc", ".m3u"):
            (directory / f"{stem}{suffix}").write_text("x\n")
        (directory / f"{stem}.cue").write_text(f'FILE "{name}" WAVE\n  TRACK 01 AUDIO\n')


class TestUnknownDiscFiles:
    def test_tagged_rip_names_files_like_a_matched_rip(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "rip"
        _unknown_rip(album_dir, Path(__file__).parent / "fixtures" / "album_flac", tagged=True)
        renames = {
            src.name: dst.name for src, dst in unknown_disc_file_renames(read_album(album_dir))
        }
        assert renames[f"Unknown Artist - {DISC_ID_1}.log"] == (
            "Little Feat - Dixie Chicken (Disc 1 of 2).log"
        )
        assert renames[f"Unknown Artist - {DISC_ID_2}.cue"] == (
            "Little Feat - Dixie Chicken (Disc 2 of 2).cue"
        )
        assert len(renames) == 6
        assert not any(name.endswith(".m3u") for name in renames)

    def test_untagged_rip_is_left_alone(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "rip"
        _unknown_rip(album_dir, Path(__file__).parent / "fixtures" / "album_flac", tagged=False)
        assert unknown_disc_file_renames(read_album(album_dir)) == []

    def test_rename_moves_disc_files_and_rewrites_cue(
        self, tmp_path: Path, default_log: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        album_dir = tmp_path / "rip"
        _unknown_rip(album_dir, Path(__file__).parent / "fixtures" / "album_flac", tagged=True)
        _rename([str(album_dir)])
        names = sorted(p.name for p in album_dir.iterdir())
        assert "1-01. Song 1.flac" in names
        assert "Little Feat - Dixie Chicken (Disc 2 of 2).toc" in names
        assert not any(
            name.startswith("Unknown Artist") and not name.endswith(".m3u") for name in names
        )
        cue = (album_dir / "Little Feat - Dixie Chicken (Disc 1 of 2).cue").read_text()
        assert 'FILE "1-01. Song 1.flac" WAVE' in cue
        assert "Renamed 6 rip file(s)" in capsys.readouterr().out
        assert "- Rip file `Unknown Artist - " in default_log.read_text()

    def test_dry_run_lists_disc_files_and_moves_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        album_dir = tmp_path / "rip"
        _unknown_rip(album_dir, Path(__file__).parent / "fixtures" / "album_flac", tagged=True)
        before = sorted(p.name for p in album_dir.iterdir())
        _rename([str(album_dir), "--dry-run"])
        assert sorted(p.name for p in album_dir.iterdir()) == before
        out = capsys.readouterr().out
        assert f"Unknown Artist - {DISC_ID_1}.log -> Little Feat - Dixie Chicken" in out

    def test_existing_target_blocks_the_rename(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "rip"
        _unknown_rip(album_dir, Path(__file__).parent / "fixtures" / "album_flac", tagged=True)
        (album_dir / "Little Feat - Dixie Chicken (Disc 1 of 2).log").write_text("other\n")
        with pytest.raises(SystemExit) as error:
            _rename([str(album_dir)])
        assert error.value.code == 1
        assert (album_dir / f"Unknown Artist - {DISC_ID_1}.log").exists()
