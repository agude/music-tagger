from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from music_tagger.cli import (
    _diff,
    _is_album_dir,
    _match,
    _mb_discid,
    _mb_release,
    _mb_search,
    _print_diff,
    _read,
    _rename,
    _scan,
    _toc,
    _write_ratings,
    _write_tags,
)
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import AlbumResult, TrackDiff
from music_tagger.tags import TagChange, read_album


class TestIsAlbumDir:
    def test_flac_dir(self, flac_album: Path) -> None:
        assert _is_album_dir(flac_album) is True

    def test_mp3_dir(self, mp3_album: Path) -> None:
        assert _is_album_dir(mp3_album) is True

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert _is_album_dir(tmp_path) is False

    def test_text_only_dir(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello")
        assert _is_album_dir(tmp_path) is False


class TestPrintDiff:
    def test_no_changes(self, capsys: object, flac_album: Path) -> None:
        album = read_album(flac_album)
        result = AlbumResult(
            album=album,
            release=MBRelease(
                id="r-1", title="Desperado", date="1973", country="US", label="Asylum"
            ),
            diffs=[TrackDiff(track=t, changes=[]) for t in album.tracks],
        )
        _print_diff(result)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes needed" in captured.out

    def test_with_changes(self, capsys: object, flac_album: Path) -> None:
        album = read_album(flac_album)
        diffs = [
            TrackDiff(
                track=album.tracks[0],
                changes=[TagChange(field="title", old_value="Desperado", new_value="New")],
            ),
            TrackDiff(track=album.tracks[1], changes=[]),
            TrackDiff(track=album.tracks[2], changes=[]),
        ]
        result = AlbumResult(
            album=album,
            release=MBRelease(
                id="r-1", title="Desperado", date="1973", country="US", label="Asylum"
            ),
            diffs=diffs,
        )
        _print_diff(result)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "title: Desperado" in captured.out
        assert "→ New" in captured.out


class TestReadCommand:
    def test_json_to_stdout(self, capsys: object, flac_album: Path) -> None:
        _read([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["artist"] == "Eagles"
        assert data["album"] == "Desperado"
        assert data["track_count"] == 3
        assert len(data["tracks"]) == 3
        assert data["tracks"][0]["format"] == "flac"
        assert data["tracks"][0]["duration_secs"] > 0

    def test_json_to_file_with_digest(
        self, capsys: object, flac_album: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "evidence.json"
        _read([str(flac_album), "-o", str(out)])
        data = json.loads(out.read_text())
        assert data["artist"] == "Eagles"
        assert data["track_count"] == 3
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "Desperado" in captured.out
        assert "3 tracks" in captured.out

    def test_mp3_album(self, capsys: object, mp3_album: Path) -> None:
        _read([str(mp3_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["tracks"][0]["format"] == "mp3"

    def test_empty_dir_exits(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(SystemExit):
            _read([str(tmp_path)])


class TestScanCommand:
    def test_outputs_json(self, flac_album: Path, tmp_path: Path) -> None:
        out = tmp_path / "scan.json"
        _scan([str(flac_album.parent), "--all", "-o", str(out)])
        data = json.loads(out.read_text())
        assert len(data) == 1
        assert data[0]["artist"] == "Eagles"
        assert data[0]["album"] == "Desperado"
        assert data[0]["track_count"] == 3
        assert data[0]["status"] == "ok"
        assert data[0]["done"] is False
        assert data[0]["musicbrainz_albumid"] == "test-album-id-1"

    def test_skips_consistent_without_all(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        out = tmp_path / "scan.json"
        _scan([str(flac_album.parent), "-o", str(out)])
        assert not out.exists()
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "All albums have consistent" in captured.out

    def test_no_id_status(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "noalbum"
        album_dir.mkdir()
        import shutil

        from tests.conftest import FIXTURES

        for f in sorted((FIXTURES / "album_flac").iterdir()):
            if f.suffix == ".flac":
                shutil.copy2(f, album_dir / f.name)
        # Strip the MB album ID from the copies
        from mutagen.flac import FLAC

        for f in album_dir.glob("*.flac"):
            audio = FLAC(f)
            if "MUSICBRAINZ_ALBUMID" in audio:
                del audio["MUSICBRAINZ_ALBUMID"]
                audio.save()
        out = tmp_path / "scan.json"
        _scan([str(tmp_path), "-o", str(out)])
        data = json.loads(out.read_text())
        no_id = [e for e in data if e["status"] == "no_id"]
        assert len(no_id) == 1
        assert "musicbrainz_albumid" not in no_id[0]

    def test_split_status(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "splitalbum"
        album_dir.mkdir()
        import shutil

        from tests.conftest import FIXTURES

        for f in sorted((FIXTURES / "album_flac").iterdir()):
            if f.suffix == ".flac":
                shutil.copy2(f, album_dir / f.name)
        from mutagen.flac import FLAC

        files = sorted(album_dir.glob("*.flac"))
        audio = FLAC(files[0])
        audio["MUSICBRAINZ_ALBUMID"] = ["different-id"]
        audio.save()
        out = tmp_path / "scan.json"
        _scan([str(tmp_path), "-o", str(out)])
        data = json.loads(out.read_text())
        split = [e for e in data if e["status"] == "split"]
        assert len(split) == 1
        assert isinstance(split[0]["musicbrainz_albumid"], list)
        assert len(split[0]["musicbrainz_albumid"]) == 2

    def test_stdout_when_no_output(self, flac_album: Path, capsys: object) -> None:
        _scan([str(flac_album.parent), "--all"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["artist"] == "Eagles"


SAMPLE_CUE = """\
FILE "audio.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 01 03:29:37
  TRACK 03 AUDIO
    INDEX 01 06:36:50
"""


class TestTocCommand:
    def test_json_to_stdout(self, capsys: object, tmp_path: Path) -> None:
        (tmp_path / "album.cue").write_text(SAMPLE_CUE)
        _toc([str(tmp_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["first_track"] == 1
        assert data["last_track"] == 3
        assert len(data["track_offsets"]) == 3

    def test_no_cue_exits(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises((SystemExit, ValueError)):
            _toc([str(tmp_path)])


class TestMbDiscidCommand:
    def test_by_discid(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.lookup_discid.return_value = [release]
            _mb_discid(["abc123"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "r-1"

    def test_by_toc(self, capsys: object) -> None:
        release = MBRelease(
            id="r-2",
            title="Album",
            track_count=5,
            format="CD",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.lookup_toc.return_value = [release]
            _mb_discid(["--toc", "1 3 200000 150 15000 30000"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "r-2"


class TestMbSearchCommand:
    def test_json_to_stdout(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.search_releases.return_value = [release]
            _mb_search(["--artist", "Eagles", "--album", "Desperado"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "r-1"
        assert data[0]["title"] == "Desperado"

    def test_json_to_file_with_digest(self, capsys: object, tmp_path: Path) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
        )
        out = tmp_path / "candidates.json"
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.search_releases.return_value = [release]
            _mb_search(["--artist", "Eagles", "--album", "Desperado", "-o", str(out)])
        data = json.loads(out.read_text())
        assert len(data) == 1
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 candidates" in captured.out
        assert "r-1" in captured.out


class TestMbReleaseCommand:
    def test_json_to_stdout(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
            discs={1: [MBTrack(number=1, title="Track 1", duration_ms=200000)]},
            artist_id="art-1",
            release_group_id="rg-1",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _mb_release(["r-1"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["id"] == "r-1"
        assert data["title"] == "Desperado"
        assert "1" in data["discs"]
        assert len(data["discs"]["1"]) == 1

    def test_json_to_file_with_digest(self, capsys: object, tmp_path: Path) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
            discs={1: [MBTrack(number=1, title="Track 1", duration_ms=200000)]},
        )
        out = tmp_path / "release.json"
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _mb_release(["r-1", "-o", str(out)])
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["id"] == "r-1"
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Desperado" in captured.out
        assert "11 tracks" in captured.out


class TestMatchCommand:
    def test_scores_candidates(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(
                        number=1,
                        title="Desperado",
                        duration_ms=int(album.tracks[0].duration_secs * 1000),
                    ),
                    MBTrack(
                        number=2,
                        title="Twenty-One",
                        duration_ms=int(album.tracks[1].duration_secs * 1000),
                    ),
                    MBTrack(
                        number=3,
                        title="Out of Control",
                        duration_ms=int(album.tracks[2].duration_secs * 1000),
                    ),
                ]
            },
        )
        candidates_path = tmp_path / "candidates.json"
        candidates_path.write_text(json.dumps([release.to_dict()]))

        _match(["--evidence", str(evidence_path), "--candidates", str(candidates_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["match"]["tracks_within_2s"] == 3
        assert data[0]["release"]["id"] == "r-1"

    def test_digest_output(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            track_count=3,
            discs={
                1: [
                    MBTrack(
                        number=1, title="A", duration_ms=int(album.tracks[0].duration_secs * 1000)
                    ),
                    MBTrack(
                        number=2, title="B", duration_ms=int(album.tracks[1].duration_secs * 1000)
                    ),
                    MBTrack(
                        number=3, title="C", duration_ms=int(album.tracks[2].duration_secs * 1000)
                    ),
                ]
            },
        )
        candidates_path = tmp_path / "candidates.json"
        candidates_path.write_text(json.dumps([release.to_dict()]))

        out = tmp_path / "matched.json"
        _match(
            ["--evidence", str(evidence_path), "--candidates", str(candidates_path), "-o", str(out)]
        )
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 candidates scored" in captured.out
        assert "r-1" in captured.out
        assert out.exists()


class TestDiffCommand:
    def test_json_to_stdout(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="Doolin-Dalton"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
        )
        release_path = tmp_path / "release.json"
        release_path.write_text(json.dumps(release.to_dict()))

        _diff(["--evidence", str(evidence_path), "--release", str(release_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["release_id"] == "r-1"
        assert len(data["tracks"]) == 3
        changed_tracks = [t for t in data["tracks"] if t["changes"]]
        assert len(changed_tracks) > 0

    def test_digest_shows_changed_fields(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            track_count=3,
            discs={
                1: [
                    MBTrack(number=1, title="Doolin-Dalton"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
            barcode="075596066624",
        )
        release_path = tmp_path / "release.json"
        release_path.write_text(json.dumps(release.to_dict()))

        out = tmp_path / "diff.json"
        _diff(["--evidence", str(evidence_path), "--release", str(release_path), "-o", str(out)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "r-1" in captured.out
        assert "with changes" in captured.out
        assert out.exists()


class TestWriteTagsCommand:
    def _make_diff_json(self, flac_album: Path) -> dict:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        return {
            "release_id": "r-1",
            "release_title": "Desperado",
            "tracks": [
                {
                    "path": str(album.tracks[0].path),
                    "format": "flac",
                    "changes": [
                        {"field": "title", "old_value": "Desperado", "new_value": "New Title"},
                        {"field": "label", "old_value": "", "new_value": "Asylum"},
                    ],
                },
                {
                    "path": str(album.tracks[1].path),
                    "format": "flac",
                    "changes": [],
                },
            ],
        }

    def test_dry_run(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(self._make_diff_json(flac_album)))
        _write_tags(["--diff", str(diff_path), "--dry-run"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out
        assert "title" in captured.out
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        assert album.tracks[0].tags["title"] == "Desperado"

    def test_applies_changes(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(self._make_diff_json(flac_album)))
        _write_tags(["--diff", str(diff_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Updated 1 track" in captured.out
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        assert album.tracks[0].tags["title"] == "New Title"
        assert album.tracks[0].tags["label"] == "Asylum"
        assert album.tracks[1].tags["title"] == "Twenty-One"

    def test_no_changes(self, capsys: object, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(
            json.dumps(
                {
                    "release_id": "r-1",
                    "release_title": "X",
                    "tracks": [{"path": "/x.flac", "format": "flac", "changes": []}],
                }
            )
        )
        _write_tags(["--diff", str(diff_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes" in captured.out

    def test_log_file(self, flac_album: Path, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(self._make_diff_json(flac_album)))
        log_path = tmp_path / "changes.log"
        _write_tags(["--diff", str(diff_path), "--log", str(log_path)])
        log_content = log_path.read_text()
        assert "r-1" in log_content
        assert "title" in log_content


class TestCandidatesCommand:
    def test_json_to_stdout(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.cli import _print_candidates

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="Track 1", duration_ms=200000),
                    MBTrack(number=2, title="Track 2", duration_ms=200000),
                    MBTrack(number=3, title="Track 3", duration_ms=200000),
                ]
            },
        )

        with (
            patch("music_tagger.cli.search_candidates") as mock_search,
            patch("music_tagger.cli.MusicBrainzClient"),
        ):
            album = read_album(flac_album)
            mock_search.return_value = (album, [release])
            _print_candidates([str(flac_album)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["album"]["artist"] == "Eagles"
        assert len(data["matches"]) == 1
        assert data["matches"][0]["release"]["id"] == "r-1"
        assert "tracks_within_2s" in data["matches"][0]["match"]

    def test_digest_with_out(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
        from music_tagger.cli import _print_candidates

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="T1", duration_ms=200000),
                    MBTrack(number=2, title="T2", duration_ms=200000),
                    MBTrack(number=3, title="T3", duration_ms=200000),
                ]
            },
        )

        out = tmp_path / "candidates.json"
        with (
            patch("music_tagger.cli.search_candidates") as mock_search,
            patch("music_tagger.cli.MusicBrainzClient"),
        ):
            album = read_album(flac_album)
            mock_search.return_value = (album, [release])
            _print_candidates([str(flac_album), "-o", str(out)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "r-1" in captured.out
        assert "scored" in captured.out
        data = json.loads(out.read_text())
        assert data["album"]["artist"] == "Eagles"


class TestTagCommand:
    def test_dry_run(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.cli import _tag
        from music_tagger.musicbrainz import MBRelease, MBTrack

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="Doolin-Dalton"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
        )

        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _tag(["--release-id", "r-1", "--dry-run", str(flac_album)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out


class TestRenameCommand:
    def test_dry_run(self, capsys: object, flac_album: Path) -> None:
        for f in sorted(flac_album.glob("*.flac")):
            f.rename(f.parent / f"track{f.stem[:2]}.flac")

        _rename([str(flac_album), "--dry-run"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "track01.flac -> 01 - Desperado.flac" in captured.out
        assert "dry run" in captured.out
        assert (flac_album / "track01.flac").exists()

    def test_renames_files(self, flac_album: Path, capsys: object) -> None:
        for f in sorted(flac_album.glob("*.flac")):
            f.rename(f.parent / f"track{f.stem[:2]}.flac")

        _rename([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Renamed 3 file(s)" in captured.out
        assert (flac_album / "01 - Desperado.flac").exists()
        assert (flac_album / "02 - Twenty-One.flac").exists()
        assert (flac_album / "03 - Out of Control.flac").exists()

    def test_skips_already_correct(self, flac_album: Path, capsys: object) -> None:
        # Fixtures are named "NN - Track N.flac" but title tag is different,
        # so they do get renamed. Rename them to match their title tags first.
        for f in sorted(flac_album.glob("*.flac")):
            from mutagen.flac import FLAC

            audio = FLAC(str(f))
            title = audio["TITLE"][0]
            num = int(audio["TRACKNUMBER"][0])
            correct_name = f"{num:02d} - {title}.flac"
            if f.name != correct_name:
                f.rename(f.parent / correct_name)

        _rename([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Renamed 0 file(s)" in captured.out

    def test_skips_missing_title(self, flac_album: Path, capsys: object) -> None:
        from mutagen.flac import FLAC

        target = sorted(flac_album.glob("*.flac"))[0]
        audio = FLAC(str(target))
        del audio["TITLE"]
        audio.save()
        _rename([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "skipped (missing title or tracknumber)" in captured.out

    def test_sanitizes_unsafe_chars(self, flac_album: Path, capsys: object) -> None:
        from mutagen.flac import FLAC

        target = sorted(flac_album.glob("*.flac"))[0]
        target.rename(flac_album / "track01.flac")
        audio = FLAC(str(flac_album / "track01.flac"))
        audio["TITLE"] = ["What:Is/This?"]
        audio.save()
        _rename([str(flac_album)])
        capsys.readouterr()  # type: ignore[attr-defined]
        assert (flac_album / "01 - What-Is-This-.flac").exists()


class TestWriteRatings:
    def test_writes_to_flac(self, flac_album: Path, capsys: object) -> None:
        ratings = [
            {
                "id": "s1",
                "path": "album_flac/01 - Track 1.flac",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 4,
                "starred": "2024-01-01T00:00:00Z",
            },
        ]
        ratings_file = flac_album.parent / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        _write_ratings(["--from", str(ratings_file), "--root", str(flac_album.parent)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 file(s)" in captured.out

        album = read_album(flac_album)
        assert album.tracks[0].tags["fmps_rating"] == "0.8"
        assert album.tracks[0].tags["rating"] == "4"
        assert album.tracks[0].tags["starred"] == "true"

    def test_dry_run(self, flac_album: Path, capsys: object) -> None:
        ratings = [
            {
                "id": "s1",
                "path": "album_flac/01 - Track 1.flac",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 3,
            },
        ]
        ratings_file = flac_album.parent / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        _write_ratings(
            ["--from", str(ratings_file), "--root", str(flac_album.parent), "--dry-run"]
        )

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out

        album = read_album(flac_album)
        assert "rating" not in album.tracks[0].tags

    def test_skips_missing_files(self, flac_album: Path, capsys: object) -> None:
        ratings = [
            {
                "id": "s1",
                "path": "nonexistent/track.flac",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 5,
            },
        ]
        ratings_file = flac_album.parent / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        _write_ratings(["--from", str(ratings_file), "--root", str(flac_album.parent)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Skipped 1" in captured.err
