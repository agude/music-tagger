from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from music_tagger.cli import _is_album_dir, _print_diff
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
