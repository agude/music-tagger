from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from music_tagger.matcher import MatchResult
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import AlbumResult, TrackDiff, _build_new_tags, apply_changes, process_album
from music_tagger.tags import AlbumTags, TagChange, TrackTags, read_album


def _make_release() -> MBRelease:
    return MBRelease(
        id="release-1",
        title="Desperado",
        date="1973-04-17",
        country="US",
        status="Official",
        label="Asylum Records",
        catalognum="SD 5068",
        barcode="075596066624",
        format="CD",
        track_count=3,
        discs={
            1: [
                MBTrack(number=1, title="Doolin-Dalton", duration_ms=209000),
                MBTrack(number=2, title="Twenty-One", duration_ms=187000),
                MBTrack(number=3, title="Out of Control", duration_ms=195000),
            ]
        },
        artist_id="artist-1",
        release_group_id="rg-1",
    )


class TestBuildNewTags:
    def test_sets_album_level_tags(self) -> None:
        release = _make_release()
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=209.0, format="flac")
        tags = _build_new_tags(release, track, 0, 1, 1)

        assert tags["album"] == "Desperado"
        assert tags["date"] == "1973-04-17"
        assert tags["country"] == "US"
        assert tags["label"] == "Asylum Records"
        assert tags["catalognumber"] == "SD 5068"
        assert tags["barcode"] == "075596066624"
        assert tags["musicbrainz_albumid"] == "release-1"
        assert tags["musicbrainz_artistid"] == "artist-1"
        assert tags["musicbrainz_releasegroupid"] == "rg-1"
        assert tags["releasestatus"] == "Official"
        assert tags["media"] == "CD"

    def test_sets_track_level_tags(self) -> None:
        release = _make_release()
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=209.0, format="flac")
        tags = _build_new_tags(release, track, 0, 1, 1)

        assert tags["title"] == "Doolin-Dalton"
        assert tags["tracknumber"] == "1"
        assert tags["discnumber"] == "1"

    def test_second_track(self) -> None:
        release = _make_release()
        track = TrackTags(path=Path("/music/02.flac"), tags={}, duration_secs=187.0, format="flac")
        tags = _build_new_tags(release, track, 1, 1, 1)

        assert tags["title"] == "Twenty-One"
        assert tags["tracknumber"] == "2"


class TestProcessAlbum:
    def test_full_pipeline(self, flac_album: Path) -> None:
        release = _make_release()
        match_result = MatchResult(
            release_id="release-1",
            confidence="high",
            reasoning="Perfect match.",
        )

        mock_mb = MagicMock()
        mock_mb.search_releases.return_value = [release]
        mock_mb.fetch_release.return_value = release

        with patch("music_tagger.tagger.match_release", return_value=match_result):
            result = process_album(flac_album, mock_mb)

        assert result.match.release_id == "release-1"
        assert result.release.title == "Desperado"
        assert len(result.diffs) == 3

    def test_empty_dir_raises(self, tmp_path: Path) -> None:
        import pytest
        with pytest.raises(ValueError, match="No audio files"):
            process_album(tmp_path, MagicMock())


class TestApplyChanges:
    def test_writes_changes(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        diffs = [
            TrackDiff(
                track=album.tracks[0],
                changes=[TagChange(field="title", old_value="Desperado", new_value="New Title")],
            ),
            TrackDiff(track=album.tracks[1], changes=[]),
            TrackDiff(track=album.tracks[2], changes=[]),
        ]
        result = AlbumResult(
            album=album,
            match=MatchResult(release_id="x", confidence="high", reasoning="test"),
            release=_make_release(),
            diffs=diffs,
        )

        count = apply_changes(result)
        assert count == 1

        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["title"] == "New Title"
        assert album2.tracks[1].tags["title"] == "Twenty-One"
