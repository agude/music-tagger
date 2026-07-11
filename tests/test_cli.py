from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from music_tagger.cli import _is_album_dir, _mb_search, _print_diff, _read, _scan
from music_tagger.musicbrainz import MBRelease
from music_tagger.tags import AlbumTags, TagChange, TrackTags, read_album
from music_tagger.tagger import AlbumResult, TrackDiff


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
            release=MBRelease(id="r-1", title="Desperado", date="1973", country="US", label="Asylum"),
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
            release=MBRelease(id="r-1", title="Desperado", date="1973", country="US", label="Asylum"),
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

    def test_json_to_file_with_digest(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
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

    def test_skips_consistent_without_all(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
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


class TestMbSearchCommand:
    def test_json_to_stdout(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1", title="Desperado", date="1973", country="US",
            label="Asylum", track_count=11, format="CD",
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
            id="r-1", title="Desperado", date="1973", country="US",
            label="Asylum", track_count=11, format="CD",
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


class TestCandidatesCommand:
    def test_prints_candidates(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.cli import _print_candidates
        from music_tagger.musicbrainz import MBRelease, MBTrack

        release = MBRelease(
            id="r-1", title="Desperado", date="1973", country="US",
            label="Asylum", track_count=3, format="CD",
            discs={1: [
                MBTrack(number=1, title="Track 1", duration_ms=200000),
            ]},
        )

        with (
            patch("music_tagger.cli.search_candidates") as mock_search,
            patch("music_tagger.cli.MusicBrainzClient"),
        ):
            album = read_album(flac_album)
            mock_search.return_value = (album, [release])
            _print_candidates([str(flac_album)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "r-1" in captured.out


class TestTagCommand:
    def test_dry_run(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.cli import _tag
        from music_tagger.musicbrainz import MBRelease, MBTrack

        release = MBRelease(
            id="r-1", title="Desperado", date="1973", country="US",
            label="Asylum", track_count=3, format="CD",
            discs={1: [
                MBTrack(number=1, title="Doolin-Dalton"),
                MBTrack(number=2, title="Twenty-One"),
                MBTrack(number=3, title="Out of Control"),
            ]},
        )

        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _tag(["--release-id", "r-1", "--dry-run", str(flac_album)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out
