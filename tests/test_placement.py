from __future__ import annotations

import json
from pathlib import Path

from music_tagger.placement import (
    CopyResult,
    FileMapping,
    PlacementPlan,
    _sanitize,
    compute_placement,
    copy_files,
)
from music_tagger.tags import AlbumTags, TrackTags, read_album


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
                tags={"tracknumber": "1", "title": "Song One", "albumartist": "Eagles",
                      "album": "Desperado", "date": "1973"},
                format="flac",
            ),
            TrackTags(
                path=Path("/rips/album/02.flac"),
                tags={"tracknumber": "2", "title": "Song Two", "albumartist": "Eagles",
                      "album": "Desperado", "date": "1973"},
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
                tags={"tracknumber": "1", "discnumber": "1", "title": "Disc 1 Track 1",
                      "albumartist": "Prince", "album": "Purple Rain", "date": "1984"},
                format="flac",
            ),
            TrackTags(
                path=Path("/rips/01b.flac"),
                tags={"tracknumber": "1", "discnumber": "2", "title": "Disc 2 Track 1",
                      "albumartist": "Prince", "album": "Purple Rain", "date": "1984"},
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
                tags={"tracknumber": "1", "title": "Song", "albumartist": "Artist",
                      "album": "Album"},
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
                tags={"tracknumber": "1", "title": "Song", "albumartist": "A", "album": "B", "date": "2000"},
                format="flac",
            ),
        ]
        album = AlbumTags(directory=album_dir, tracks=tracks)
        plan = compute_placement(album, root=Path("/music"))

        names = [m.dest.name for m in plan.mappings]
        assert "cover.jpg" in names
        assert "album.cue" in names
        assert "rip.log" in names

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
                tags={"tracknumber": "1", "title": "What?", "albumartist": "AC/DC",
                      "album": "Back in Black", "date": "1980"},
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
                FileMapping(source=Path("/rips/01.flac"), dest=Path("/music/Eagles/Desperado (1973)/01. Song.flac")),
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
        from music_tagger.cli import _copy

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
        from music_tagger.cli import _copy

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
        from music_tagger.cli import _copy

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
        from music_tagger.cli import _path_for

        album = read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        _path_for(["--evidence", str(evidence_path), "--root", str(tmp_path / "library")])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert "album_dir" in data
        assert len(data["mappings"]) == 3

    def test_digest_with_out(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _path_for

        album = read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        out = tmp_path / "plan.json"
        _path_for(["--evidence", str(evidence_path), "--root", str(tmp_path / "library"), "-o", str(out)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "Desperado" in captured.out
        assert "3 files" in captured.out
        assert out.exists()
