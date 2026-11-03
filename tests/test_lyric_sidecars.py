"""Lyric sidecars through rename and placement (B3)."""

from __future__ import annotations

from pathlib import Path

from music_tagger.cli import _rename
from music_tagger.lyrics import lyric_sidecars
from music_tagger.placement import compute_placement, copy_files
from music_tagger.tags import AlbumTags, TrackTags

BASE_TAGS = {"tracknumber": "1", "title": "Song", "albumartist": "A", "album": "B"}


def _album(directory: Path, filename: str, **tags: str) -> AlbumTags:
    """A one-track album whose audio file exists on disk, as placement requires."""
    directory.mkdir(parents=True, exist_ok=True)
    audio = directory / filename
    audio.write_bytes(b"audio bytes")
    track = TrackTags(path=audio, tags={**BASE_TAGS, **tags}, format="flac")
    return AlbumTags(directory=directory, tracks=[track])


class TestLyricSidecarsHelper:
    def test_returns_lrc_before_txt(self, tmp_path: Path) -> None:
        (tmp_path / "01 - Track 1.flac").write_bytes(b"x")
        (tmp_path / "01 - Track 1.txt").write_text("plain")
        (tmp_path / "01 - Track 1.lrc").write_text("[00:01.00]timed")
        assert lyric_sidecars(tmp_path / "01 - Track 1.flac") == [
            tmp_path / "01 - Track 1.lrc",
            tmp_path / "01 - Track 1.txt",
        ]

    def test_other_txt_is_not_a_sidecar(self, tmp_path: Path) -> None:
        (tmp_path / "01 - Track 1.flac").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("notes")
        (tmp_path / "01 - Track 1 (copy).lrc").write_text("[00:01.00]x")
        assert lyric_sidecars(tmp_path / "01 - Track 1.flac") == []


class TestPlacementSidecars:
    def test_sidecars_take_the_placed_audio_name(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        (album.directory / "01 - Track 1.lrc").write_text("[00:01.00]Line")
        (album.directory / "01 - Track 1.txt").write_text("Line")

        plan = compute_placement(album, root=Path("/music"))

        dests = {m.source.name: m.dest.name for m in plan.mappings}
        assert dests == {
            "01 - Track 1.flac": "01. Song.flac",
            "01 - Track 1.lrc": "01. Song.lrc",
            "01 - Track 1.txt": "01. Song.txt",
        }

    def test_multi_disc_sidecar_follows_disc_prefix(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "02 - Track 1.flac", discnumber="2")
        (album.directory / "02 - Track 1.lrc").write_text("[00:01.00]Line")

        plan = compute_placement(album, root=Path("/music"))

        assert [m.dest.name for m in plan.mappings if m.source.suffix == ".lrc"] == [
            "2-01. Song.lrc"
        ]

    def test_unicode_and_sanitized_title_match_the_audio_stem(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac", title="Ünïcödé: What/Is?")
        (album.directory / "01 - Track 1.lrc").write_text("[00:01.00]Line")

        plan = compute_placement(album, root=Path("/music"))

        audio = next(m.dest for m in plan.mappings if m.source.suffix == ".flac")
        sidecar = next(m.dest for m in plan.mappings if m.source.suffix == ".lrc")
        assert audio.name == "01. Ünïcödé- What-Is-.flac"
        assert sidecar == audio.with_suffix(".lrc")

    def test_unchanged_names_are_kept(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01. Song.flac")
        (album.directory / "01. Song.lrc").write_text("[00:01.00]Line")

        plan = compute_placement(album, root=Path("/music"))

        assert [m.dest.name for m in plan.mappings] == ["01. Song.flac", "01. Song.lrc"]

    def test_unrelated_txt_keeps_its_own_name(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        (album.directory / "notes.txt").write_text("Recording notes")

        plan = compute_placement(album, root=Path("/music"))

        names = [m.dest.name for m in plan.mappings]
        assert "notes.txt" in names
        assert "01. Song.txt" not in names

    def test_sidecar_is_listed_once(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        (album.directory / "01 - Track 1.txt").write_text("Line")

        plan = compute_placement(album, root=Path("/music"))

        assert sum(m.source.suffix == ".txt" for m in plan.mappings) == 1

    def test_unmatched_lrc_is_copied_under_its_own_name(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        (album.directory / "orphan.lrc").write_text("[00:01.00]Line")

        plan = compute_placement(album, root=Path("/music"))

        assert "orphan.lrc" in [m.dest.name for m in plan.mappings]


class TestCopySidecars:
    def test_copies_sidecar_byte_for_byte_under_placed_name(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        source = album.directory / "01 - Track 1.lrc"
        source.write_text("[00:01.00]Señor", encoding="utf-8")
        plan = compute_placement(album, root=tmp_path / "library")

        result = copy_files(plan)

        dest = plan.album_dir / "01. Song.lrc"
        assert dest.read_bytes() == source.read_bytes()
        assert any(m.dest == dest for m in result.copied)
        assert result.failed == []

    def test_existing_library_sidecar_is_not_overwritten(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        (album.directory / "01 - Track 1.lrc").write_text("[00:01.00]new")
        plan = compute_placement(album, root=tmp_path / "library")
        plan.album_dir.mkdir(parents=True)
        existing = plan.album_dir / "01. Song.lrc"
        existing.write_text("library copy")

        result = copy_files(plan)

        assert existing.read_text() == "library copy"
        assert any(m.dest == existing for m in result.skipped)

    def test_dry_run_copies_nothing(self, tmp_path: Path) -> None:
        album = _album(tmp_path / "rip", "01 - Track 1.flac")
        (album.directory / "01 - Track 1.lrc").write_text("[00:01.00]Line")
        plan = compute_placement(album, root=tmp_path / "library")

        copy_files(plan, dry_run=True)

        assert not (plan.album_dir / "01. Song.lrc").exists()


class TestRenameSidecars:
    """Uses the flac_album fixture; its track 1 has the title "Desperado"."""

    @staticmethod
    def _prefix_names(album: Path) -> None:
        for f in sorted(album.glob("*.flac")):
            f.rename(f.parent / f"track{f.stem[:2]}.flac")

    def test_sidecars_move_with_audio(self, flac_album: Path, capsys: object) -> None:
        self._prefix_names(flac_album)
        (flac_album / "track01.lrc").write_text("[00:01.00]Line")
        (flac_album / "track01.txt").write_text("Line")

        _rename([str(flac_album)])

        assert (flac_album / "01. Desperado.lrc").read_text() == "[00:01.00]Line"
        assert (flac_album / "01. Desperado.txt").read_text() == "Line"
        assert not (flac_album / "track01.lrc").exists()
        assert not (flac_album / "track01.txt").exists()
        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "Renamed 2 lyric sidecar(s)." in out

    def test_dry_run_lists_sidecars_and_moves_nothing(
        self, flac_album: Path, capsys: object
    ) -> None:
        self._prefix_names(flac_album)
        (flac_album / "track01.lrc").write_text("[00:01.00]Line")

        _rename([str(flac_album), "--dry-run"])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "track01.lrc -> 01. Desperado.lrc" in out
        assert (flac_album / "track01.lrc").exists()
        assert not (flac_album / "01. Desperado.lrc").exists()

    def test_sidecar_conflict_leaves_audio_and_sidecar_in_place(
        self, flac_album: Path, capsys: object
    ) -> None:
        self._prefix_names(flac_album)
        (flac_album / "track01.lrc").write_text("source")
        (flac_album / "01. Desperado.lrc").write_text("library")

        _rename([str(flac_album)])

        assert (flac_album / "track01.flac").exists()
        assert (flac_album / "track01.lrc").read_text() == "source"
        assert (flac_album / "01. Desperado.lrc").read_text() == "library"
        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "skipped ('01. Desperado.lrc' already exists)" in out

    def test_unrelated_txt_is_not_renamed(self, flac_album: Path) -> None:
        self._prefix_names(flac_album)
        (flac_album / "notes.txt").write_text("notes")

        _rename([str(flac_album)])

        assert (flac_album / "notes.txt").read_text() == "notes"

    def test_sidecar_rename_is_logged(
        self, flac_album: Path, default_log: Path, capsys: object
    ) -> None:
        self._prefix_names(flac_album)
        (flac_album / "track01.lrc").write_text("[00:01.00]Line")

        _rename([str(flac_album)])

        assert "Lyric sidecar `track01.lrc` → `01. Desperado.lrc`" in default_log.read_text()

    def test_correct_names_leave_sidecar_alone(self, flac_album: Path, capsys: object) -> None:
        from mutagen.flac import FLAC

        audio = sorted(flac_album.glob("*.flac"))[0]
        title = FLAC(str(audio))["TITLE"][0]
        correct = audio.with_name(f"01. {title}.flac")
        audio.rename(correct)
        (flac_album / f"01. {title}.lrc").write_text("[00:01.00]Line")

        _rename([str(flac_album)])

        assert (flac_album / f"01. {title}.lrc").read_text() == "[00:01.00]Line"
