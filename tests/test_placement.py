from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from music_tagger import placement
from music_tagger.placement import (
    FileMapping,
    PlacementConflictError,
    PlacementPlan,
    _sanitize,
    compute_placement,
    copy_files,
)
from music_tagger.tags import AlbumTags, TagChange, TrackTags, read_album, write_tags


class TestSanitize:
    def test_removes_slashes(self) -> None:
        assert _sanitize("AC/DC") == "AC-DC"

    def test_removes_colons(self) -> None:
        assert _sanitize("Title: Subtitle") == "Title- Subtitle"

    def test_strips_dots_and_spaces(self) -> None:
        assert _sanitize("  .name. ") == "name"

    def test_empty_becomes_underscore(self) -> None:
        assert _sanitize("") == "_"
        assert _sanitize("...") == "_"

    def test_normal_text_unchanged(self) -> None:
        assert _sanitize("Eagles") == "Eagles"

    def test_unicode_preserved(self) -> None:
        assert _sanitize("Björk") == "Björk"


class TestComputePlacement:
    def test_basic_single_disc(self) -> None:
        tracks = [
            TrackTags(
                path=Path("/rips/album/01.flac"),
                tags={
                    "tracknumber": "1",
                    "title": "Song One",
                    "albumartist": "Eagles",
                    "album": "Desperado",
                    "date": "1973",
                },
                format="flac",
            ),
            TrackTags(
                path=Path("/rips/album/02.flac"),
                tags={
                    "tracknumber": "2",
                    "title": "Song Two",
                    "albumartist": "Eagles",
                    "album": "Desperado",
                    "date": "1973",
                },
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/rips/album"), tracks=tracks)
        root = Path("/music")
        plan = compute_placement(album, root=root)

        assert plan.album_dir == Path("/music/Eagles/Desperado (1973)")
        assert len(plan.mappings) == 2
        assert plan.mappings[0].dest == Path("/music/Eagles/Desperado (1973)/01. Song One.flac")
        assert plan.mappings[1].dest == Path("/music/Eagles/Desperado (1973)/02. Song Two.flac")

    def test_multi_disc(self) -> None:
        tracks = [
            TrackTags(
                path=Path("/rips/01a.flac"),
                tags={
                    "tracknumber": "1",
                    "discnumber": "1",
                    "title": "Disc 1 Track 1",
                    "albumartist": "Prince",
                    "album": "Purple Rain",
                    "date": "1984",
                },
                format="flac",
            ),
            TrackTags(
                path=Path("/rips/01b.flac"),
                tags={
                    "tracknumber": "1",
                    "discnumber": "2",
                    "title": "Disc 2 Track 1",
                    "albumartist": "Prince",
                    "album": "Purple Rain",
                    "date": "1984",
                },
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/rips"), tracks=tracks)
        root = Path("/music")
        plan = compute_placement(album, root=root)

        assert plan.album_dir == Path("/music/Prince/Purple Rain (1984)")
        assert plan.mappings[0].dest.name == "1-01. Disc 1 Track 1.flac"
        assert plan.mappings[1].dest.name == "2-01. Disc 2 Track 1.flac"

    def test_no_date(self) -> None:
        tracks = [
            TrackTags(
                path=Path("/rips/01.flac"),
                tags={
                    "tracknumber": "1",
                    "title": "Song",
                    "albumartist": "Artist",
                    "album": "Album",
                },
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/rips"), tracks=tracks)
        plan = compute_placement(album, root=Path("/music"))
        assert plan.album_dir == Path("/music/Artist/Album")

    def test_includes_non_audio_files(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "rip"
        album_dir.mkdir()
        (album_dir / "cover.jpg").write_bytes(b"\xff\xd8")
        (album_dir / "album.cue").write_text("TRACK 01")
        (album_dir / "rip.log").write_text("log data")

        tracks = [
            TrackTags(
                path=album_dir / "01.flac",
                tags={
                    "tracknumber": "1",
                    "title": "Song",
                    "albumartist": "A",
                    "album": "B",
                    "date": "2000",
                },
                format="flac",
            ),
        ]
        album = AlbumTags(directory=album_dir, tracks=tracks)
        plan = compute_placement(album, root=Path("/music"))

        names = [m.dest.name for m in plan.mappings]
        assert "cover.jpg" in names
        assert "album.cue" in names
        assert "rip.log" in names

    def test_sanitizes_non_audio_names(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "rip"
        album_dir.mkdir()
        (album_dir / "Sugar Ray - 14:59.log").write_text("log data")
        track = TrackTags(
            path=album_dir / "01.flac",
            tags={"tracknumber": "1", "title": "Song", "albumartist": "A", "album": "B"},
            format="flac",
        )

        plan = compute_placement(AlbumTags(directory=album_dir, tracks=[track]), root=Path("/m"))

        assert plan.mappings[-1].dest.name == "Sugar Ray - 14-59.log"

    def test_excludes_non_audio_when_disabled(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "rip"
        album_dir.mkdir()
        (album_dir / "cover.jpg").write_bytes(b"\xff\xd8")

        tracks = [
            TrackTags(
                path=album_dir / "01.flac",
                tags={"tracknumber": "1", "title": "Song", "albumartist": "A", "album": "B"},
                format="flac",
            ),
        ]
        album = AlbumTags(directory=album_dir, tracks=tracks)
        plan = compute_placement(album, root=Path("/music"), include_non_audio=False)
        assert len(plan.mappings) == 1

    def test_sanitizes_unsafe_chars(self) -> None:
        tracks = [
            TrackTags(
                path=Path("/rips/01.flac"),
                tags={
                    "tracknumber": "1",
                    "title": "What?",
                    "albumartist": "AC/DC",
                    "album": "Back in Black",
                    "date": "1980",
                },
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/rips"), tracks=tracks)
        plan = compute_placement(album, root=Path("/music"))
        assert "AC-DC" in str(plan.album_dir)
        assert "?" not in plan.mappings[0].dest.name

    def test_from_fixture(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        plan = compute_placement(album, root=Path("/music"))
        assert "Eagles" in str(plan.album_dir)
        assert "Desperado" in str(plan.album_dir)
        assert len(plan.mappings) == 3


class TestFileMappingSerialization:
    def test_round_trip(self) -> None:
        m = FileMapping(source=Path("/a/b.flac"), dest=Path("/c/d.flac"))
        d = m.to_dict()
        restored = FileMapping.from_dict(d)
        assert restored.source == m.source
        assert restored.dest == m.dest


class TestPlacementPlanSerialization:
    def test_round_trip(self) -> None:
        plan = PlacementPlan(
            album_dir=Path("/music/Eagles/Desperado (1973)"),
            mappings=[
                FileMapping(
                    source=Path("/rips/01.flac"),
                    dest=Path("/music/Eagles/Desperado (1973)/01. Song.flac"),
                ),
            ],
        )
        d = plan.to_dict()
        restored = PlacementPlan.from_dict(d)
        assert restored.album_dir == plan.album_dir
        assert len(restored.mappings) == 1
        assert restored.mappings[0].source == plan.mappings[0].source


class TestCopyFiles:
    def _make_plan(self, src_dir: Path, dest_dir: Path) -> PlacementPlan:
        files = []
        for name in ["01.flac", "02.flac"]:
            f = src_dir / name
            f.write_bytes(b"\x00" * 100)
            files.append(FileMapping(source=f, dest=dest_dir / name))
        return PlacementPlan(album_dir=dest_dir, mappings=files)

    def test_copies_files(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        plan = self._make_plan(src, dest)

        result = copy_files(plan)
        assert len(result.copied) == 2
        assert len(result.skipped) == 0
        assert len(result.failed) == 0
        assert result.total_bytes == 200
        assert (dest / "01.flac").exists()
        assert (dest / "02.flac").exists()

    def test_dry_run_does_not_copy(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        plan = self._make_plan(src, dest)

        result = copy_files(plan, dry_run=True)
        assert len(result.copied) == 2
        assert not dest.exists()

    def test_skips_existing(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        dest.mkdir(parents=True)
        plan = self._make_plan(src, dest)
        (dest / "01.flac").write_bytes(b"existing")

        result = copy_files(plan)
        assert len(result.skipped) == 1
        assert result.skipped[0].dest.name == "01.flac"
        assert len(result.copied) == 1
        assert (dest / "01.flac").read_bytes() == b"existing"

    def test_creates_directories(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "deep" / "nested" / "album"
        plan = self._make_plan(src, dest)

        result = copy_files(plan)
        assert len(result.copied) == 2
        assert dest.exists()

    def test_reports_each_file(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        dest.mkdir(parents=True)
        plan = self._make_plan(src, dest)
        (dest / "01.flac").write_bytes(b"existing")
        reported: list[tuple[str, str]] = []

        copy_files(
            plan, on_file=lambda mapping, status: reported.append((mapping.dest.name, status))
        )

        assert reported == [("01.flac", "skipped (exists, differs)"), ("02.flac", "copied")]

    def test_serialization(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        plan = self._make_plan(src, dest)

        result = copy_files(plan)
        d = result.to_dict()
        assert len(d["copied"]) == 2
        assert d["total_bytes"] == 200
        assert d["skipped"] == []
        assert d["failed"] == []


class TestCopyCommand:
    def test_dry_run(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli.library import _copy

        album = read_album(flac_album)
        dest = tmp_path / "library"
        plan = compute_placement(album, root=dest)
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan.to_dict()))

        _copy(["--plan", str(plan_path), "--dry-run"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out
        assert "Would copy" in captured.out
        assert not dest.exists()

    def test_copies_files(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli.library import _copy

        album = read_album(flac_album)
        dest = tmp_path / "library"
        plan = compute_placement(album, root=dest)
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan.to_dict()))

        _copy(["--plan", str(plan_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Copied 3 file(s)" in captured.out
        for m in plan.mappings:
            assert m.dest.exists()

    def test_log_file(self, flac_album: Path, tmp_path: Path) -> None:
        from music_tagger.cli.library import _copy

        album = read_album(flac_album)
        dest = tmp_path / "library"
        plan = compute_placement(album, root=dest)
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan.to_dict()))
        log_path = tmp_path / "copy.log"

        _copy(["--plan", str(plan_path), "--log", str(log_path)])
        assert log_path.exists()
        content = log_path.read_text()
        assert "Copy to" in content


class TestPathForCommand:
    def test_json_to_stdout(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli.library import _path_for

        album = read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        _path_for(["--evidence", str(evidence_path), "--root", str(tmp_path / "library")])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert "album_dir" in data
        assert len(data["mappings"]) == 3

    def test_digest_with_out(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli.library import _path_for

        album = read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        out = tmp_path / "plan.json"
        _path_for(
            ["--evidence", str(evidence_path), "--root", str(tmp_path / "library"), "-o", str(out)]
        )
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "Desperado" in captured.out
        assert "3 files" in captured.out
        assert out.exists()


class TestPlacementOverrides:
    def test_overrides_directory_names(self, flac_album: Path) -> None:
        album = read_album(flac_album)

        plan = compute_placement(
            album, root=Path("/music"), artist_dir="The Eagles", album_dir="Desperado (Remaster)"
        )

        assert plan.album_dir == Path("/music/The Eagles/Desperado (Remaster)")

    def test_sanitizes_overrides(self, flac_album: Path) -> None:
        plan = compute_placement(read_album(flac_album), root=Path("/music"), album_dir="A/B")

        assert plan.album_dir == Path("/music/Eagles/A-B")


class TestPlaceSourceCleanup:
    AUDIO = ("01 - Track 1.flac", "02 - Track 2.flac", "03 - Track 3.flac")

    def _run_place(self, args: list[str], temp_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from music_tagger.cli.library import _place

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
        _place(args)

    def test_keeps_uncopied_file(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        (flac_album / "valuable.bin").write_bytes(b"keep me")
        library = tmp_path / "library"

        self._run_place([str(flac_album), "--root", str(library)], tmp_path, monkeypatch)

        assert (flac_album / "valuable.bin").read_bytes() == b"keep me"
        assert not any((flac_album / name).exists() for name in self.AUDIO)
        assert "valuable.bin" in capsys.readouterr().out

    def test_keeps_subdirectory(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (flac_album / "extras").mkdir()
        (flac_album / "extras" / "scan.bin").write_bytes(b"extra")
        library = tmp_path / "library"

        self._run_place([str(flac_album), "--root", str(library)], tmp_path, monkeypatch)

        assert (flac_album / "extras" / "scan.bin").read_bytes() == b"extra"

    def test_removes_directory_with_only_planned_files_and_m3u(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (flac_album / "whipper.m3u").write_text("playlist\n")
        library = tmp_path / "library"

        self._run_place([str(flac_album), "--root", str(library)], tmp_path, monkeypatch)

        assert not flac_album.exists()
        assert len(list(library.rglob("*.flac"))) == 3
        assert not list(library.rglob("*.m3u"))

    def test_refuses_destination_inside_source(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = flac_album / "lib"

        with pytest.raises(SystemExit) as exc:
            self._run_place([str(flac_album), "--root", str(library)], tmp_path, monkeypatch)

        assert exc.value.code == 1
        assert not library.exists()
        assert all((flac_album / name).exists() for name in self.AUDIO)

    def test_source_equal_to_temp_root_is_not_removed(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = tmp_path / "library"

        self._run_place([str(flac_album), "--root", str(library)], flac_album, monkeypatch)

        assert flac_album.is_dir()
        assert all((flac_album / name).exists() for name in self.AUDIO)


class TestCopyFilesVerification:
    def _make_plan(self, src_dir: Path, dest_dir: Path) -> PlacementPlan:
        files = []
        for name in ["01.flac", "02.flac"]:
            f = src_dir / name
            f.write_bytes(b"\x00" * 100)
            files.append(FileMapping(source=f, dest=dest_dir / name))
        return PlacementPlan(album_dir=dest_dir, mappings=files)

    def test_interrupted_copy_leaves_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        plan = self._make_plan(src, dest)
        plan.mappings = plan.mappings[:1]

        def partial_copy2(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
            Path(target).write_bytes(b"partial")
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copy2", partial_copy2)

        result = copy_files(plan)

        assert len(result.failed) == 1
        assert not (dest / "01.flac").exists()
        assert not list(dest.glob("*.partial"))

    def test_hash_error_is_recorded_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        plan = self._make_plan(src, dest)
        source_file = plan.mappings[0].source
        real_sha256 = placement._sha256

        def hash_fails_off_source(path: Path) -> str:
            if path != source_file:
                raise OSError("hash failed")
            return real_sha256(path)

        monkeypatch.setattr(placement, "_sha256", hash_fails_off_source)

        result = copy_files(plan)

        assert len(result.failed) == 2
        assert not (dest / "01.flac").exists()

    def test_identical_existing_file_is_skipped_as_identical(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        dest.mkdir(parents=True)
        plan = self._make_plan(src, dest)
        (dest / "01.flac").write_bytes(b"\x00" * 100)
        reported: list[tuple[str, str]] = []

        copy_files(plan, on_file=lambda m, status: reported.append((m.dest.name, status)))

        assert reported[0] == ("01.flac", "skipped (identical)")

    def test_differing_existing_file_is_kept_and_reported(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest" / "album"
        dest.mkdir(parents=True)
        plan = self._make_plan(src, dest)
        (dest / "01.flac").write_bytes(b"different")
        reported: list[tuple[str, str]] = []

        copy_files(plan, on_file=lambda m, status: reported.append((m.dest.name, status)))

        assert reported[0] == ("01.flac", "skipped (exists, differs)")
        assert (dest / "01.flac").read_bytes() == b"different"


def _titled(path: Path, tracknumber: str, title: str) -> TrackTags:
    return TrackTags(
        path=path,
        tags={"tracknumber": tracknumber, "title": title, "albumartist": "Band", "album": "Album"},
        format="flac",
    )


class TestDestinationConflicts:
    def test_sanitized_titles_that_collide_are_refused(self, tmp_path: Path) -> None:
        album = AlbumTags(
            directory=tmp_path,
            tracks=[
                _titled(tmp_path / "a.flac", "1", "A:B"),
                _titled(tmp_path / "b.flac", "1", "A?B"),
            ],
        )

        with pytest.raises(PlacementConflictError) as error:
            compute_placement(album, root=tmp_path / "lib", include_non_audio=False)

        message = str(error.value)
        assert "a.flac" in message
        assert "b.flac" in message

    def test_destinations_differing_only_by_case_are_refused(self, tmp_path: Path) -> None:
        album = AlbumTags(
            directory=tmp_path,
            tracks=[
                _titled(tmp_path / "a.flac", "1", "Song"),
                _titled(tmp_path / "b.flac", "1", "song"),
            ],
        )

        with pytest.raises(PlacementConflictError):
            compute_placement(album, root=tmp_path / "lib", include_non_audio=False)

    def test_colliding_lyric_sidecars_are_refused(self, tmp_path: Path) -> None:
        for name in ("a.lrc", "b.lrc"):
            (tmp_path / name).write_text("[00:01.00]line\n", encoding="utf-8")
        album = AlbumTags(
            directory=tmp_path,
            tracks=[
                _titled(tmp_path / "a.flac", "1", "Song"),
                _titled(tmp_path / "b.flac", "1", "song"),
            ],
        )

        with pytest.raises(PlacementConflictError) as error:
            compute_placement(album, root=tmp_path / "lib", include_non_audio=False)

        assert "a.lrc" in str(error.value)
        assert "b.lrc" in str(error.value)

    def test_place_dry_run_on_conflict_exits_1_and_creates_nothing(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        from music_tagger.cli.library import _place

        tracks = read_album(flac_album).tracks
        write_tags(tracks[0], [TagChange("title", tracks[0].tags["title"], "A:B")])
        write_tags(tracks[1], [TagChange("tracknumber", "2", "1")])
        write_tags(tracks[1], [TagChange("title", tracks[1].tags["title"], "A?B")])
        library = tmp_path / "library"

        with pytest.raises(SystemExit) as error:
            _place([str(flac_album), "--root", str(library), "--dry-run"])

        assert error.value.code == 1
        assert not library.exists()


class TestM4aPlacement:
    def test_m4a_tracks_place_by_their_real_numbers(self, m4a_album: Path) -> None:
        plan = compute_placement(read_album(m4a_album), root=Path("/music"))

        names = sorted(m.dest.name for m in plan.mappings if m.source.suffix == ".m4a")
        assert names == ["01. Desperado.m4a", "02. Twenty-One.m4a", "03. Out of Control.m4a"]


class TestDiscPrefix:
    def _names(self, tags: list[dict[str, str]]) -> list[str]:
        tracks = [
            TrackTags(path=Path(f"/rips/{i:02d}.flac"), tags={"title": f"T{i}", **t}, format="flac")
            for i, t in enumerate(tags, start=1)
        ]
        album = AlbumTags(directory=Path("/rips"), tracks=tracks)
        plan = compute_placement(album, root=Path("/music"), include_non_audio=False)
        return sorted(m.dest.name for m in plan.mappings)

    def test_total_discs_alone_adds_the_prefix(self) -> None:
        names = self._names(
            [
                {"tracknumber": "1", "discnumber": "1", "totaldiscs": "2"},
                {"tracknumber": "2", "discnumber": "1", "totaldiscs": "2"},
            ]
        )

        assert names == ["1-01. T1.flac", "1-02. T2.flac"]

    def test_discnumber_with_a_total_adds_the_prefix(self) -> None:
        names = self._names([{"tracknumber": "1", "discnumber": "1/2"}])

        assert names == ["1-01. T1.flac"]

    def test_single_disc_has_no_prefix(self) -> None:
        names = self._names([{"tracknumber": "1", "discnumber": "1", "totaldiscs": "1"}])

        assert names == ["01. T1.flac"]

    def test_whipper_disctotal_is_read_and_adds_the_prefix(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        from mutagen.flac import FLAC

        for path in flac_album.glob("*.flac"):
            audio = FLAC(path)
            audio.pop("TOTALDISCS", None)
            audio["DISCNUMBER"] = ["1"]
            audio["DISCTOTAL"] = ["2"]
            audio.save()

        plan = compute_placement(read_album(flac_album), root=tmp_path / "library")

        assert "1-01. Desperado.flac" in [m.dest.name for m in plan.mappings]


class TestPlaceUnreadable:
    def test_place_exits_1_and_keeps_the_staging_intact(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from music_tagger.cli.library import _place

        bad = flac_album / "bad.flac"
        bad.write_bytes(b"not audio at all" * 10)
        library = tmp_path / "library"
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        with pytest.raises(SystemExit) as error:
            _place([str(flac_album), "--root", str(library)])

        assert error.value.code == 1
        assert bad.exists()
        assert not library.exists()


class TestNameLengthLimit:
    def test_long_ascii_title_is_truncated_and_keeps_prefix_and_extension(self) -> None:
        track = _titled(Path("/rips/01.flac"), "1", "a" * 300)

        name = placement.track_filename(track, multi_disc=False)

        assert len(name.encode("utf-8")) <= 255
        assert name.startswith("01. a")
        assert name.endswith(".flac")

    def test_cjk_title_is_cut_at_a_character_boundary(self) -> None:
        # Each character is three bytes, so 120 of them are 360 bytes.
        track = _titled(Path("/rips/01.flac"), "1", "漢" * 120)

        name = placement.track_filename(track, multi_disc=False)

        assert len(name.encode("utf-8")) <= 255
        assert name.startswith("01. 漢")
        assert name.endswith(".flac")

    def test_multi_disc_prefix_is_kept_when_truncated(self) -> None:
        track = _titled(Path("/rips/01.flac"), "1", "漢" * 120)

        name = placement.track_filename(track, multi_disc=True)

        assert len(name.encode("utf-8")) <= 255
        assert name.startswith("1-01. 漢")
        assert name.endswith(".flac")

    def test_album_directory_with_year_fits(self) -> None:
        track = TrackTags(
            path=Path("/rips/01.flac"),
            tags={
                "tracknumber": "1",
                "title": "T",
                "albumartist": "Band",
                "album": "a" * 300,
                "date": "1997",
            },
            format="flac",
        )
        plan = compute_placement(
            AlbumTags(directory=Path("/rips"), tracks=[track]),
            root=Path("/music"),
            include_non_audio=False,
        )

        name = plan.album_dir.name

        assert len(name.encode("utf-8")) <= 255
        assert name.endswith(" (1997)")

    def test_artist_directory_fits(self) -> None:
        track = TrackTags(
            path=Path("/rips/01.flac"),
            tags={"tracknumber": "1", "title": "T", "albumartist": "b" * 300, "album": "Album"},
            format="flac",
        )
        plan = compute_placement(
            AlbumTags(directory=Path("/rips"), tracks=[track]),
            root=Path("/music"),
            include_non_audio=False,
        )

        assert len(plan.album_dir.parent.name.encode("utf-8")) <= 255
