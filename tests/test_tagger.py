from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import (
    AlbumResult,
    CandidateMatch,
    TrackDiff,
    _build_new_tags,
    _parse_disc_number,
    _parse_track_number,
    apply_changes,
    build_diff,
    score_candidates,
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


class TestParseTrackNumber:
    def test_simple(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={"tracknumber": "5"}, format="flac")
        assert _parse_track_number(t) == 5

    def test_fraction(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={"tracknumber": "3/10"}, format="flac")
        assert _parse_track_number(t) == 3

    def test_missing(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={}, format="flac")
        assert _parse_track_number(t) is None

    def test_garbage(self) -> None:
        t = TrackTags(path=Path("x.flac"), tags={"tracknumber": "abc"}, format="flac")
        assert _parse_track_number(t) is None


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

    def test_uses_track_artist_id(self) -> None:
        release = MBRelease(
            id="va-release",
            title="Concert",
            track_count=2,
            discs={
                1: [
                    MBTrack(number=1, title="Song A", artist_id="artist-a"),
                    MBTrack(number=2, title="Song B", artist_id="artist-b"),
                ]
            },
            artist_id="album-artist",
            release_group_id="rg-va",
        )
        track = TrackTags(path=Path("/music/01.flac"), tags={}, format="flac")
        tags = _build_new_tags(release, track, 0, 1)
        assert tags["musicbrainz_artistid"] == "artist-a"
        assert tags["musicbrainz_albumartistid"] == "album-artist"

        tags2 = _build_new_tags(release, track, 1, 1)
        assert tags2["musicbrainz_artistid"] == "artist-b"
        assert tags2["musicbrainz_albumartistid"] == "album-artist"

    def test_sets_release_group_fields(self) -> None:
        release = MBRelease(
            id="r1",
            title="Greatest Hits",
            track_count=1,
            discs={1: [MBTrack(number=1, title="Song")]},
            artist_id="a1",
            release_group_id="rg-1",
            release_group_type="Album",
            secondary_types=["Compilation"],
            first_release_date="1990-01-01",
            asin="B00001234",
            script="Latn",
        )
        track = TrackTags(path=Path("/music/01.flac"), tags={}, format="flac")
        tags = _build_new_tags(release, track, 0, 1)
        assert tags["releasetype"] == "Album; Compilation"
        assert tags["compilation"] == "1"
        assert tags["originaldate"] == "1990-01-01"
        assert tags["asin"] == "B00001234"
        assert tags["script"] == "Latn"

    def test_sets_track_credits(self) -> None:
        release = MBRelease(
            id="r1",
            title="Album",
            track_count=1,
            discs={
                1: [
                    MBTrack(
                        number=1, title="Song",
                        recording_id="rec-1", track_id="trk-1",
                        isrc="USEE10400287", work_id="work-1",
                        composer="Bach", composer_id="c1",
                        producer="George Martin", producer_id="p1",
                        performers="John (guitar)", performer_ids="j1",
                    ),
                ]
            },
        )
        track = TrackTags(path=Path("/music/01.flac"), tags={}, format="flac")
        tags = _build_new_tags(release, track, 0, 1)
        assert tags["musicbrainz_recordingid"] == "rec-1"
        assert tags["musicbrainz_releasetrackid"] == "trk-1"
        assert tags["isrc"] == "USEE10400287"
        assert tags["musicbrainz_workid"] == "work-1"
        assert tags["composer"] == "Bach"
        assert tags["musicbrainz_composerid"] == "c1"
        assert tags["producer"] == "George Martin"
        assert tags["performer"] == "John (guitar)"

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

    def test_partial_album_matches_by_tracknumber(self) -> None:
        """A single file with tracknumber=3 should match MB track 3, not track 1."""
        release = _make_release()
        tracks = [
            TrackTags(
                path=Path("03.flac"),
                tags={"tracknumber": "3", "title": "Old Title"},
                duration_secs=195.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        assert len(result.diffs) == 1
        title_change = next(c for c in result.diffs[0].changes if c.field == "title")
        assert title_change.new_value == "Out of Control"

    def test_partial_album_does_not_match_track_1(self) -> None:
        """Ensure a file with tracknumber=3 does NOT get track 1's title."""
        release = _make_release()
        tracks = [
            TrackTags(
                path=Path("03.flac"),
                tags={"tracknumber": "3"},
                duration_secs=195.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        title_change = next(c for c in result.diffs[0].changes if c.field == "title")
        assert title_change.new_value != "Doolin-Dalton"

    def test_missing_tracknumber_gets_album_tags_only(self) -> None:
        """A file with no tracknumber tag should still get album-level tags."""
        release = _make_release()
        tracks = [
            TrackTags(
                path=Path("unknown.flac"),
                tags={},
                duration_secs=200.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        assert len(result.diffs) == 1
        fields = {c.field for c in result.diffs[0].changes}
        assert "album" in fields
        assert "musicbrainz_albumid" in fields
        assert "title" not in fields


class TestScoreCandidates:
    def test_scores_matching_release(self) -> None:
        tracks = [
            TrackTags(path=Path("01.flac"), tags={"tracknumber": "1", "discnumber": "1"},
                      duration_secs=209.0, format="flac"),
            TrackTags(path=Path("02.flac"), tags={"tracknumber": "2", "discnumber": "1"},
                      duration_secs=187.0, format="flac"),
            TrackTags(path=Path("03.flac"), tags={"tracknumber": "3", "discnumber": "1"},
                      duration_secs=195.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        release = _make_release()
        results = score_candidates(album, [release])
        assert len(results) == 1
        assert results[0].stats.track_count_match is True
        assert results[0].stats.tracks_within_2s == 3
        assert results[0].stats.tracks_compared == 3
        assert results[0].stats.max_deviation_secs == 0.0

    def test_sorts_by_match_quality(self) -> None:
        tracks = [
            TrackTags(path=Path("01.flac"), tags={"tracknumber": "1", "discnumber": "1"},
                      duration_secs=209.0, format="flac"),
            TrackTags(path=Path("02.flac"), tags={"tracknumber": "2", "discnumber": "1"},
                      duration_secs=187.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)

        good = MBRelease(
            id="good", title="Good", track_count=2,
            discs={1: [
                MBTrack(number=1, title="A", duration_ms=209000),
                MBTrack(number=2, title="B", duration_ms=187000),
            ]},
        )
        bad = MBRelease(
            id="bad", title="Bad", track_count=2,
            discs={1: [
                MBTrack(number=1, title="A", duration_ms=220000),
                MBTrack(number=2, title="B", duration_ms=200000),
            ]},
        )
        results = score_candidates(album, [bad, good])
        assert results[0].release.id == "good"
        assert results[1].release.id == "bad"

    def test_no_discs_in_candidate(self) -> None:
        tracks = [
            TrackTags(path=Path("01.flac"), tags={"tracknumber": "1"},
                      duration_secs=209.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        release = MBRelease(id="no-discs", title="X", track_count=1)
        results = score_candidates(album, [release])
        assert results[0].stats.tracks_compared == 0

    def test_round_trip_serialization(self) -> None:
        stats = CandidateMatch(
            release=_make_release(),
            stats=__import__("music_tagger.tagger", fromlist=["MatchStats"]).MatchStats(
                track_count_match=True, tracks_within_2s=3,
                tracks_compared=3, max_deviation_secs=0.5,
            ),
        )
        d = stats.to_dict()
        restored = CandidateMatch.from_dict(d)
        assert restored.release.id == stats.release.id
        assert restored.stats.tracks_within_2s == 3
        assert restored.stats.max_deviation_secs == 0.5


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
        assert album2.tracks[0].tags["music_tagger_updated"] != ""
        assert "music_tagger_updated" not in album2.tracks[1].tags
        assert album2.tracks[1].tags["title"] == "Twenty-One"
