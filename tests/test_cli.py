from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from music_tagger.cli import _find_album_dirs, _is_album_dir, _print_result
from music_tagger.matcher import MatchResult
from music_tagger.musicbrainz import MBRelease
from music_tagger.tags import AlbumTags, TagChange, TrackTags
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


class TestFindAlbumDirs:
    def test_single_album(self, flac_album: Path) -> None:
        dirs = _find_album_dirs(flac_album)
        assert dirs == [flac_album]

    def test_nested_albums(self, tmp_path: Path, flac_album: Path, mp3_album: Path) -> None:
        dirs = _find_album_dirs(tmp_path)
        assert len(dirs) == 2
        assert flac_album in dirs
        assert mp3_album in dirs

    def test_no_albums(self, tmp_path: Path) -> None:
        dirs = _find_album_dirs(tmp_path)
        assert dirs == []


class TestPrintResult:
    def test_no_changes(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.tags import read_album

        album = read_album(flac_album)
        result = AlbumResult(
            album=album,
            match=MatchResult(release_id="r-1", confidence="high", reasoning="Good match."),
            release=MBRelease(id="r-1", title="Desperado", date="1973", country="US", label="Asylum"),
            diffs=[TrackDiff(track=t, changes=[]) for t in album.tracks],
        )
        _print_result(result)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes needed" in captured.out

    def test_with_changes(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.tags import read_album

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
            match=MatchResult(release_id="r-1", confidence="high", reasoning="Good match."),
            release=MBRelease(id="r-1", title="Desperado", date="1973", country="US", label="Asylum"),
            diffs=diffs,
        )
        _print_result(result)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "title: Desperado" in captured.out
        assert "→ New" in captured.out


class TestMainDryRun:
    def test_dry_run_no_write(self, flac_album: Path) -> None:
        from music_tagger.cli import main
        from music_tagger.tagger import AlbumResult

        mock_result = AlbumResult(
            album=MagicMock(artist="Eagles", album="Desperado", directory=flac_album, tracks=[]),
            match=MatchResult(release_id="r-1", confidence="high", reasoning="match"),
            release=MBRelease(id="r-1", title="Desperado", date="1973", country="US", label="Asylum"),
            diffs=[],
        )

        with (
            patch("music_tagger.cli._find_album_dirs", return_value=[flac_album]),
            patch("music_tagger.cli.process_album", return_value=mock_result),
            patch("music_tagger.cli.MusicBrainzClient") as mock_mb_cls,
        ):
            mock_mb_cls.return_value = MagicMock()
            main(["--dry-run", str(flac_album)])
