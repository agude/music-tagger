from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import (
    AlbumResult,
    TrackDiff,
    _build_new_tags,
    _parse_disc_number,
    apply_changes,
    build_diff,
    search_candidates,
)
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


def _make_two_disc_release() -> MBRelease:
    return MBRelease(
        id="release-2disc",
        title="Purple Rain",
        date="2017-06-23",
        country="XE",
        status="Official",
        label="Warner Bros.",
        format="CD, CD",
        track_count=4,
        discs={
            1: [
                MBTrack(number=1, title="Let's Go Crazy", duration_ms=278000),
                MBTrack(number=2, title="Take Me With U", duration_ms=233000),
            ],
            2: [
                MBTrack(number=1, title="The Dance Electric", duration_ms=689000),
                MBTrack(number=2, title="Love and Sex", duration_ms=300000),
            ],
        },
        artist_id="artist-2",
        release_group_id="rg-2",
    )


class TestParseDiscNumber:
    def test_simple(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={"discnumber": "2"}, format="flac")
        assert _parse_disc_number(t) == 2

    def test_fraction(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={"discnumber": "1/2"}, format="flac")
        assert _parse_disc_number(t) == 1

    def test_missing(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={}, format="flac")
        assert _parse_disc_number(t) == 1

    def test_garbage(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={"discnumber": "abc"}, format="flac")
        assert _parse_disc_number(t) == 1


class TestBuildNewTags:
    def test_sets_album_level_tags(self) -> None:
        release = _make_release()
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=209.0, format="flac")
        tags = _build_new_tags(release, track, 0, 1)

        assert tags["album"] == "Desperado"
        assert tags["releasedate"] == "1973-04-17"
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
        tags = _build_new_tags(release, track, 0, 1)

        assert tags["title"] == "Doolin-Dalton"
        assert tags["tracknumber"] == "1"
        assert tags["discnumber"] == "1"

    def test_second_track(self) -> None:
        release = _make_release()
        track = TrackTags(path=Path("/music/02.flac"), tags={}, duration_secs=187.0, format="flac")
        tags = _build_new_tags(release, track, 1, 1)

        assert tags["title"] == "Twenty-One"
        assert tags["tracknumber"] == "2"

    def test_disc_two_track(self) -> None:
        release = _make_two_disc_release()
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=689.0, format="flac")
        tags = _build_new_tags(release, track, 0, 2)

        assert tags["title"] == "The Dance Electric"
        assert tags["tracknumber"] == "1"
        assert tags["discnumber"] == "2"


class TestSearchCandidates:
    def test_returns_album_and_candidates(self, flac_album: Path) -> None:
        release = _make_release()

        mock_mb = MagicMock()
        mock_mb.search_releases.return_value = [release]
        mock_mb.fetch_release.return_value = release

        album, candidates = search_candidates(flac_album, mock_mb)

        assert album.track_count == 3
        assert len(candidates) == 1
        assert candidates[0].id == "release-1"

    def test_empty_dir_raises(self, tmp_path: Path) -> None:
        import pytest
        with pytest.raises(ValueError, match="No audio files"):
            search_candidates(tmp_path, MagicMock())


class TestBuildDiff:
    def test_computes_changes(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        release = _make_release()
        result = build_diff(album, release)

        assert len(result.diffs) == 3
        assert result.release.id == "release-1"

    def test_no_changes_when_matching(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        release = MBRelease(
            id="test-album-id-1",
            title="Desperado",
            track_count=3,
            discs={
                1: [
                    MBTrack(number=1, title="Desperado"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
            date="",
            country="",
            label="",
            barcode="075596066624",
        )
        result = build_diff(album, release)
        assert not result.has_changes

    def test_multi_disc(self) -> None:
        release = _make_two_disc_release()
        tracks = [
            TrackTags(path=Path("01a.flac"), tags={"discnumber": "1", "tracknumber": "1"},
                      duration_secs=278.0, format="flac"),
            TrackTags(path=Path("01b.flac"), tags={"discnumber": "2", "tracknumber": "1"},
                      duration_secs=689.0, format="flac"),
            TrackTags(path=Path("02a.flac"), tags={"discnumber": "1", "tracknumber": "2"},
                      duration_secs=233.0, format="flac"),
            TrackTags(path=Path("02b.flac"), tags={"discnumber": "2", "tracknumber": "2"},
                      duration_secs=300.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        titles = [
            next((c.new_value for c in d.changes if c.field == "title"), None)
            for d in result.diffs
        ]
        assert titles == ["Let's Go Crazy", "The Dance Electric", "Take Me With U", "Love and Sex"]


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
            release=_make_release(),
            diffs=diffs,
        )

        count = apply_changes(result)
        assert count == 1

        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["title"] == "New Title"
        assert album2.tracks[1].tags["title"] == "Twenty-One"
