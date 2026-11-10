from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mutagen.flac import FLAC

from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import (
    AlbumResult,
    CandidateMatch,
    DuplicateTrackError,
    TagConflict,
    TrackDiff,
    _build_new_multi,
    _build_new_tags,
    apply_changes,
    build_diff,
    duplicate_track_keys,
    overwrites_needing_review,
    resolve_isrc_conflicts,
    score_candidates,
    search_candidates,
)
from music_tagger.tags import (
    AlbumTags,
    TagChange,
    TrackTags,
    read_album,
    write_locks,
    write_tags,
)


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
                MBTrack(number=1, title="Doolin-Dalton", duration_ms=209000, artist="Eagles"),
                MBTrack(number=2, title="Twenty-One", duration_ms=187000, artist="Eagles"),
                MBTrack(number=3, title="Out of Control", duration_ms=195000, artist="Eagles"),
            ]
        },
        artist="Eagles",
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
                MBTrack(number=1, title="Let's Go Crazy", duration_ms=278000, artist="Prince"),
                MBTrack(number=2, title="Take Me With U", duration_ms=233000, artist="Prince"),
            ],
            2: [
                MBTrack(number=1, title="The Dance Electric", duration_ms=689000, artist="Prince"),
                MBTrack(number=2, title="Love and Sex", duration_ms=300000, artist="Prince"),
            ],
        },
        artist="Prince",
        artist_id="artist-2",
        release_group_id="rg-2",
    )


class TestTotalDiscsFromPositions:
    def test_totaldiscs_is_the_highest_disc_position(self) -> None:
        release = _make_release()
        release.discs = {2: release.discs[1]}
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=209.0, format="flac")

        tags = _build_new_tags(release, track, 0, 2)

        assert tags["totaldiscs"] == "2"


class TestBuildNewTags:
    def test_sets_album_level_tags(self) -> None:
        release = _make_release()
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=209.0, format="flac")
        tags = _build_new_tags(release, track, 0, 1)

        assert tags["album"] == "Desperado"
        assert tags["artist"] == "Eagles"
        assert tags["albumartist"] == "Eagles"
        assert tags["totaltracks"] == "3"
        assert tags["totaldiscs"] == "1"
        assert tags["releasedate"] == "1973-04-17"
        assert tags["country"] == "US"
        assert tags["label"] == "Asylum Records"
        assert tags["catalognumber"] == "SD 5068"
        assert tags["barcode"] == "075596066624"
        assert tags["musicbrainz_albumid"] == "release-1"
        assert _build_new_multi(release, track, 0, 1)["musicbrainz_artistid"] == ("artist-1",)
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
                    MBTrack(number=1, title="Song A", artist="Artist A", artist_id="artist-a"),
                    MBTrack(number=2, title="Song B", artist="Artist B", artist_id="artist-b"),
                ]
            },
            artist="Various Artists",
            artist_id="album-artist",
            release_group_id="rg-va",
        )
        track = TrackTags(path=Path("/music/01.flac"), tags={}, format="flac")
        tags = _build_new_tags(release, track, 0, 1)
        assert tags["artist"] == "Artist A"
        assert tags["albumartist"] == "Various Artists"
        multi = _build_new_multi(release, track, 0, 1)
        assert multi["musicbrainz_artistid"] == ("artist-a",)
        assert multi["musicbrainz_albumartistid"] == ("album-artist",)

        tags2 = _build_new_tags(release, track, 1, 1)
        assert tags2["artist"] == "Artist B"
        assert tags2["albumartist"] == "Various Artists"
        multi2 = _build_new_multi(release, track, 1, 1)
        assert multi2["musicbrainz_artistid"] == ("artist-b",)
        assert multi2["musicbrainz_albumartistid"] == ("album-artist",)

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
        assert _build_new_multi(release, track, 0, 1)["releasetype"] == ("Album", "Compilation")
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
                        number=1,
                        title="Song",
                        recording_id="rec-1",
                        track_id="trk-1",
                        isrcs=["USEE10400287"],
                        work_id="work-1",
                        composer=("Bach",),
                        composer_id=("c1",),
                        producer=("George Martin",),
                        producer_id=("p1",),
                        performers="John (guitar)",
                        performer_ids="j1",
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
        multi = _build_new_multi(release, track, 0, 1)
        assert multi["composer"] == ("Bach",)
        assert multi["musicbrainz_composerid"] == ("c1",)
        assert multi["producer"] == ("George Martin",)
        assert tags["performer"] == "John (guitar)"

    def test_disc_two_track(self) -> None:
        release = _make_two_disc_release()
        track = TrackTags(path=Path("/music/01.flac"), tags={}, duration_secs=689.0, format="flac")
        tags = _build_new_tags(release, track, 0, 2)

        assert tags["title"] == "The Dance Electric"
        assert tags["tracknumber"] == "1"
        assert tags["discnumber"] == "2"
        assert tags["totaldiscs"] == "2"
        assert tags["totaltracks"] == "2"


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

    def test_no_results_returns_empty_list(self, flac_album: Path) -> None:
        mock_mb = MagicMock()
        mock_mb.search_releases.return_value = []

        album, candidates = search_candidates(flac_album, mock_mb)

        assert album.track_count == 3
        assert candidates == []

    def test_empty_dir_raises(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(ValueError, match="No audio files"):
            search_candidates(tmp_path, MagicMock())


class TestBuildDiff:
    @pytest.mark.parametrize("existing_tags", [{}, {"isrc": ""}], ids=["absent-tag", "empty-tag"])
    def test_fills_missing_isrc(self, existing_tags: dict[str, str]) -> None:
        track = TrackTags(
            path=Path("/music/01.flac"),
            tags={"tracknumber": "1", **existing_tags},
            duration_secs=209.0,
            format="flac",
        )
        release = _make_release()
        release.discs[1][0].isrcs = ["USIR19902220"]

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        changes = [change for change in result.diffs[0].changes if change.field == "isrc"]
        assert changes == [TagChange("isrc", "", "USIR19902220")]
        assert result.diffs[0].conflicts == []

    @pytest.mark.parametrize("isrcs", [[], ["USIR19902220", "USDW10110279"]])
    def test_does_not_guess_missing_isrc(self, isrcs: list[str]) -> None:
        track = TrackTags(
            Path("/music/01.flac"), {"tracknumber": "1"}, duration_secs=209.0, format="flac"
        )
        release = _make_release()
        release.discs[1][0].isrcs = isrcs

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        assert not any(change.field == "isrc" for change in result.diffs[0].changes)
        assert result.diffs[0].conflicts == []

    @pytest.mark.parametrize(
        "isrcs",
        [
            ["USIR19902220"],
            ["USIR19902220", "USDW10110279"],
            ["USDW10110279", "USIR19902220"],
        ],
    )
    def test_preserves_existing_isrc_in_recording_list(self, isrcs: list[str]) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "isrc": "USIR19902220"},
            duration_secs=209.0,
            format="flac",
        )
        release = _make_release()
        release.discs[1][0].isrcs = isrcs

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        assert not any(change.field == "isrc" for change in result.diffs[0].changes)
        assert result.diffs[0].conflicts == []
        assert result.diffs[0].warnings == []

    @pytest.mark.parametrize("isrcs", [["USDW10110279"], ["USDW10110279", "USIR19701434"]])
    def test_reports_isrc_conflict_without_proposing_replacement(self, isrcs: list[str]) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "isrc": "USIR19902220"},
            duration_secs=209.0,
            format="flac",
        )
        release = _make_release()
        release.discs[1][0].isrcs = isrcs

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        assert result.has_unresolved_conflicts
        assert result.diffs[0].conflicts == [TagConflict("isrc", "USIR19902220", isrcs)]
        assert not any(change.field == "isrc" for change in result.diffs[0].changes)

    def test_preserves_unverified_isrc_when_recording_has_no_codes(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "isrc": "USIR19902220"},
            duration_secs=209.0,
            format="flac",
        )

        result = build_diff(AlbumTags(Path("/music"), [track]), _make_release())

        assert not result.has_unresolved_conflicts
        assert not any(change.field == "isrc" for change in result.diffs[0].changes)
        assert result.diffs[0].warnings == [
            "ISRC USIR19902220 is unverified: MusicBrainz lists no ISRCs."
        ]

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
                    MBTrack(number=1, title="Desperado", artist="Eagles"),
                    MBTrack(number=2, title="Twenty-One", artist="Eagles"),
                    MBTrack(number=3, title="Out of Control", artist="Eagles"),
                ]
            },
            artist="Eagles",
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
            TrackTags(
                path=Path("01a.flac"),
                tags={"discnumber": "1", "tracknumber": "1"},
                duration_secs=278.0,
                format="flac",
            ),
            TrackTags(
                path=Path("01b.flac"),
                tags={"discnumber": "2", "tracknumber": "1"},
                duration_secs=689.0,
                format="flac",
            ),
            TrackTags(
                path=Path("02a.flac"),
                tags={"discnumber": "1", "tracknumber": "2"},
                duration_secs=233.0,
                format="flac",
            ),
            TrackTags(
                path=Path("02b.flac"),
                tags={"discnumber": "2", "tracknumber": "2"},
                duration_secs=300.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        titles = [
            next((c.new_value for c in d.changes if c.field == "title"), None) for d in result.diffs
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

    def test_partial_album_distrusts_tracknumber_with_wrong_duration(self) -> None:
        """A file whose tracknumber disagrees with its duration matches by duration."""
        release = _make_release()
        tracks = [
            TrackTags(
                path=Path("03.flac"),
                tags={"tracknumber": "3"},
                duration_secs=209.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        title_change = next(c for c in result.diffs[0].changes if c.field == "title")
        assert title_change.new_value == "Doolin-Dalton"
        assert result.diffs[0].warnings == []

    def test_partial_album_unmatched_file_keeps_title_and_tracknumber(self) -> None:
        """A file that fits no release track gets album tags only, plus a warning."""
        release = _make_release()
        tracks = [
            TrackTags(
                path=Path("03.flac"),
                tags={"tracknumber": "3", "title": "Unrelated"},
                duration_secs=300.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        changed = {c.field for c in result.diffs[0].changes}
        assert "album" in changed
        assert "title" not in changed
        assert "tracknumber" not in changed
        assert len(result.diffs[0].warnings) == 1
        assert "left unchanged" in result.diffs[0].warnings[0]

    def test_partial_album_title_breaks_duration_tie(self) -> None:
        """Several release tracks within 2 s: the matching title decides."""
        release = MBRelease(
            id="release-tie",
            title="Tie",
            track_count=3,
            discs={
                1: [
                    MBTrack(number=1, title="Alpha", duration_ms=200000, artist="A"),
                    MBTrack(number=2, title="Beta", duration_ms=195000, artist="A"),
                    MBTrack(number=3, title="Gamma", duration_ms=195000, artist="A"),
                ]
            },
        )
        tracks = [
            TrackTags(
                path=Path("x.flac"),
                tags={"tracknumber": "7", "title": "Gamma"},
                duration_secs=195.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        track_change = next(c for c in result.diffs[0].changes if c.field == "tracknumber")
        assert track_change.new_value == "3"
        assert result.diffs[0].warnings == []

    def test_no_tracknumbers_uses_positional_fallback(self) -> None:
        """When no files have tracknumber tags, match by file position."""
        release = _make_release()
        tracks = [
            TrackTags(path=Path("01.flac"), tags={}, duration_secs=209.0, format="flac"),
            TrackTags(path=Path("02.flac"), tags={}, duration_secs=187.0, format="flac"),
            TrackTags(path=Path("03.flac"), tags={}, duration_secs=195.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        titles = [
            next((c.new_value for c in d.changes if c.field == "title"), None) for d in result.diffs
        ]
        assert titles == ["Doolin-Dalton", "Twenty-One", "Out of Control"]

    def test_mixed_tracknumbers_untagged_gets_album_only(self) -> None:
        """When some files have tracknumber and one doesn't, the untagged
        file gets album-level tags only (not positional fallback)."""
        release = _make_release()
        tracks = [
            TrackTags(
                path=Path("01.flac"), tags={"tracknumber": "1"}, duration_secs=209.0, format="flac"
            ),
            TrackTags(path=Path("unknown.flac"), tags={}, duration_secs=200.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        # The tagged file gets a title
        tagged_fields = {c.field for c in result.diffs[0].changes}
        assert "title" in tagged_fields

        # The untagged file gets album-level tags but no title
        untagged_fields = {c.field for c in result.diffs[1].changes}
        assert "album" in untagged_fields
        assert "musicbrainz_albumid" in untagged_fields
        assert "title" not in untagged_fields

    def test_positional_fallback_returns_a_warning_and_prints_nothing(self, capsys) -> None:
        """Positional fallback is reported in the result; build_diff itself prints nothing."""
        release = _make_release()
        tracks = [
            TrackTags(path=Path("01.flac"), tags={}, duration_secs=209.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        result = build_diff(album, release)

        assert capsys.readouterr().err == ""
        assert len(result.warnings) == 1
        assert "no tracknumber tags found" in result.warnings[0]
        assert "matching 1 files by position" in result.warnings[0]


class TestScoreCandidates:
    def test_scores_matching_release(self) -> None:
        tracks = [
            TrackTags(
                path=Path("01.flac"),
                tags={"tracknumber": "1", "discnumber": "1"},
                duration_secs=209.0,
                format="flac",
            ),
            TrackTags(
                path=Path("02.flac"),
                tags={"tracknumber": "2", "discnumber": "1"},
                duration_secs=187.0,
                format="flac",
            ),
            TrackTags(
                path=Path("03.flac"),
                tags={"tracknumber": "3", "discnumber": "1"},
                duration_secs=195.0,
                format="flac",
            ),
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
            TrackTags(
                path=Path("01.flac"),
                tags={"tracknumber": "1", "discnumber": "1"},
                duration_secs=209.0,
                format="flac",
            ),
            TrackTags(
                path=Path("02.flac"),
                tags={"tracknumber": "2", "discnumber": "1"},
                duration_secs=187.0,
                format="flac",
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)

        good = MBRelease(
            id="good",
            title="Good",
            track_count=2,
            discs={
                1: [
                    MBTrack(number=1, title="A", duration_ms=209000),
                    MBTrack(number=2, title="B", duration_ms=187000),
                ]
            },
        )
        bad = MBRelease(
            id="bad",
            title="Bad",
            track_count=2,
            discs={
                1: [
                    MBTrack(number=1, title="A", duration_ms=220000),
                    MBTrack(number=2, title="B", duration_ms=200000),
                ]
            },
        )
        results = score_candidates(album, [bad, good])
        assert results[0].release.id == "good"
        assert results[1].release.id == "bad"

    def test_no_discs_in_candidate(self) -> None:
        tracks = [
            TrackTags(
                path=Path("01.flac"), tags={"tracknumber": "1"}, duration_secs=209.0, format="flac"
            ),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        release = MBRelease(id="no-discs", title="X", track_count=1)
        results = score_candidates(album, [release])
        assert results[0].stats.tracks_compared == 0

    def test_round_trip_serialization(self) -> None:
        stats = CandidateMatch(
            release=_make_release(),
            stats=__import__("music_tagger.tagger", fromlist=["MatchStats"]).MatchStats(
                track_count_match=True,
                tracks_within_2s=3,
                tracks_compared=3,
                max_deviation_secs=0.5,
            ),
        )
        d = stats.to_dict()
        restored = CandidateMatch.from_dict(d)
        assert restored.release.id == stats.release.id
        assert restored.stats.tracks_within_2s == 3
        assert restored.stats.max_deviation_secs == 0.5


class TestAlbumResultSerialization:
    @pytest.mark.parametrize(
        ("resolution", "has_unresolved_conflicts"),
        [(None, True), ("USIR19902220", False), ("USDW10110279", False)],
    )
    def test_preserves_conflicts_and_warnings(
        self, resolution: str | None, has_unresolved_conflicts: bool
    ) -> None:
        track = TrackTags(Path("/music/01.flac"), {}, format="flac")
        conflict = TagConflict("isrc", "USIR19902220", ["USDW10110279"], resolution)
        result = AlbumResult(
            AlbumTags(Path("/music"), [track]),
            _make_release(),
            [TrackDiff(track, [], [conflict], ["Unverified metadata."])],
        )

        restored = AlbumResult.from_dict(result.to_dict())

        assert restored.diffs[0].conflicts == [conflict]
        assert restored.diffs[0].warnings == ["Unverified metadata."]
        assert restored.has_unresolved_conflicts is has_unresolved_conflicts

    def test_round_trip(self) -> None:
        track = TrackTags(path=Path("/music/01.flac"), tags={"title": "Old"}, format="flac")
        changes = [TagChange(field="title", old_value="Old", new_value="New")]
        result = AlbumResult(
            album=AlbumTags(directory=Path("/music"), tracks=[track]),
            release=_make_release(),
            diffs=[TrackDiff(track=track, changes=changes)],
        )
        d = result.to_dict()
        assert d["release_id"] == "release-1"
        assert len(d["tracks"]) == 1
        assert d["tracks"][0]["path"] == "/music/01.flac"
        assert d["tracks"][0]["changes"][0]["field"] == "title"

        restored = AlbumResult.from_dict(d)
        assert restored.release.id == "release-1"
        assert len(restored.diffs) == 1
        assert restored.diffs[0].track.path == Path("/music/01.flac")
        assert restored.diffs[0].changes[0].new_value == "New"

    def test_empty_changes(self) -> None:
        track = TrackTags(path=Path("/music/01.flac"), tags={}, format="flac")
        result = AlbumResult(
            album=AlbumTags(directory=Path("/music"), tracks=[track]),
            release=_make_release(),
            diffs=[TrackDiff(track=track, changes=[])],
        )
        d = result.to_dict()
        assert d["tracks"][0]["changes"] == []
        assert not result.has_changes


class TestApplyChanges:
    def test_writes_missing_isrc(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        release = _make_release()
        release.discs[1][0].isrcs = ["USIR19902220"]

        result = build_diff(album, release)
        apply_changes(result)

        updated_album = read_album(flac_album)
        assert updated_album.tracks[0].tags["isrc"] == "USIR19902220"

    def test_preserves_existing_isrc_while_writing_other_tags(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        write_tags(album.tracks[0], [TagChange("isrc", "", "USIR19902220")])
        release = _make_release()
        release.discs[1][0].isrcs = ["USDW10110279", "USIR19902220"]

        result = build_diff(read_album(flac_album), release)
        apply_changes(result)

        updated_album = read_album(flac_album)
        assert updated_album.tracks[0].tags["isrc"] == "USIR19902220"
        assert updated_album.tracks[0].tags["musicbrainz_albumid"] == "release-1"

    @pytest.mark.parametrize("resolution", [None, "NOT-A-CANDIDATE"])
    def test_blocks_entire_album_with_unresolved_or_invalid_conflict(
        self, flac_album: Path, resolution: str | None
    ) -> None:
        album = read_album(flac_album)
        write_tags(album.tracks[2], [TagChange("isrc", "", "USIR19902220")])
        before = {track.path: track.path.read_bytes() for track in album.tracks}
        release = _make_release()
        release.discs[1][2].isrcs = ["USDW10110279"]
        release.discs[1][2].recording_id = "new-recording"
        result = build_diff(read_album(flac_album), release)
        result.diffs[2].conflicts[0].resolution = resolution

        with pytest.raises(ValueError, match="Unresolved tag conflicts"):
            apply_changes(result)

        assert {path: path.read_bytes() for path in before} == before

    @pytest.mark.parametrize("resolution", ["USIR19902220", "USDW10110279"])
    def test_applies_explicit_conflict_resolution_from_json(
        self, flac_album: Path, resolution: str
    ) -> None:
        album = read_album(flac_album)
        write_tags(album.tracks[0], [TagChange("isrc", "", "USIR19902220")])
        release = _make_release()
        release.discs[1][0].isrcs = ["USDW10110279"]
        release.discs[1][0].recording_id = "new-recording"
        data = build_diff(read_album(flac_album), release).to_dict()
        data["tracks"][0]["conflicts"][0]["resolution"] = resolution

        apply_changes(AlbumResult.from_dict(data))

        updated = read_album(flac_album).tracks[0].tags
        assert updated["isrc"] == resolution
        assert updated["musicbrainz_recordingid"] == "new-recording"

    def test_rejects_duplicate_change_for_resolved_conflict(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        before = track.path.read_bytes()
        conflict = TagConflict("isrc", "USIR19902220", ["USDW10110279"], "USIR19902220")
        result = AlbumResult(
            album,
            _make_release(),
            [TrackDiff(track, [TagChange("isrc", "USIR19902220", "USDW10110279")], [conflict])],
        )

        with pytest.raises(ValueError, match="duplicate tag change"):
            apply_changes(result)

        assert track.path.read_bytes() == before

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


class TestBuildDiffLocks:
    def test_withholds_locked_field_and_keeps_other_changes(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "title": "Doolin-Dalton (Edit)"},
            duration_secs=209.0,
            format="flac",
            locked_fields=["title"],
        )

        result = build_diff(AlbumTags(Path("/music"), [track]), _make_release())

        diff = result.diffs[0]
        assert not any(change.field == "title" for change in diff.changes)
        assert diff.locked_changes == [TagChange("title", "Doolin-Dalton (Edit)", "Doolin-Dalton")]
        assert any(change.field == "musicbrainz_albumid" for change in diff.changes)

    def test_withholds_fill_of_locked_empty_field(self) -> None:
        """A lock keeps a deliberately removed ID removed."""
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1"},
            duration_secs=209.0,
            format="flac",
            locked_fields=["musicbrainz_recordingid"],
        )
        release = _make_release()
        release.discs[1][0].recording_id = "recording-1"

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        diff = result.diffs[0]
        assert not any(change.field == "musicbrainz_recordingid" for change in diff.changes)
        assert diff.locked_changes == [TagChange("musicbrainz_recordingid", "", "recording-1")]

    def test_unchanged_locked_field_is_not_reported(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "title": "Doolin-Dalton"},
            format="flac",
            locked_fields=["title"],
        )

        result = build_diff(AlbumTags(Path("/music"), [track]), _make_release())

        assert result.diffs[0].locked_changes == []

    def test_ignore_locks_proposes_locked_change(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "title": "Doolin-Dalton (Edit)"},
            duration_secs=209.0,
            format="flac",
            locked_fields=["title"],
        )

        result = build_diff(AlbumTags(Path("/music"), [track]), _make_release(), ignore_locks=True)

        diff = result.diffs[0]
        assert TagChange("title", "Doolin-Dalton (Edit)", "Doolin-Dalton") in diff.changes
        assert diff.locked_changes == []

    @pytest.mark.parametrize("isrcs", [["USDW10110279"], []], ids=["conflict", "unverified"])
    def test_locked_isrc_skips_conflict_check(self, isrcs: list[str]) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "isrc": "USIR19902220"},
            duration_secs=209.0,
            format="flac",
            locked_fields=["isrc"],
        )
        release = _make_release()
        release.discs[1][0].isrcs = isrcs

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        assert result.diffs[0].conflicts == []
        assert result.diffs[0].warnings == []
        assert not result.has_unresolved_conflicts

    def test_ignore_locks_restores_isrc_conflict_check(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"),
            {"tracknumber": "1", "isrc": "USIR19902220"},
            duration_secs=209.0,
            format="flac",
            locked_fields=["isrc"],
        )
        release = _make_release()
        release.discs[1][0].isrcs = ["USDW10110279"]

        result = build_diff(AlbumTags(Path("/music"), [track]), release, ignore_locks=True)

        assert result.has_unresolved_conflicts

    def test_locked_changes_do_not_count_as_changes(self) -> None:
        track = TrackTags(Path("/music/01.flac"), {}, format="flac", locked_fields=["title"])
        result = AlbumResult(
            AlbumTags(Path("/music"), [track]),
            _make_release(),
            [TrackDiff(track, [], locked_changes=[TagChange("title", "Old", "New")])],
        )

        assert not result.has_changes
        assert result.has_locked_changes


class TestAlbumResultLockSerialization:
    def test_preserves_locked_changes(self) -> None:
        track = TrackTags(Path("/music/01.flac"), {}, format="flac")
        locked = [TagChange("title", "Old", "New")]
        result = AlbumResult(
            AlbumTags(Path("/music"), [track]),
            _make_release(),
            [TrackDiff(track, [], locked_changes=locked)],
        )

        restored = AlbumResult.from_dict(result.to_dict())

        assert restored.diffs[0].locked_changes == locked

    def test_missing_key_means_no_locked_changes(self) -> None:
        data = {"release_id": "r-1", "tracks": [{"path": "/music/01.flac", "changes": []}]}

        restored = AlbumResult.from_dict(data)

        assert restored.diffs[0].locked_changes == []


class TestApplyChangesLocks:
    def test_write_keeps_locked_field(self, flac_album: Path) -> None:
        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title"])
        release = _make_release()

        apply_changes(build_diff(read_album(flac_album), release))

        updated = read_album(flac_album).tracks[0]
        assert updated.tags["title"] == "Desperado"
        assert updated.tags["musicbrainz_albumid"] == "release-1"
        assert updated.locked_fields == ["title"]

    def test_refuses_change_to_field_locked_in_file(self, flac_album: Path) -> None:
        """A stale or hand-edited diff must not bypass a lock set on the file."""
        album = read_album(flac_album)
        write_locks(album.tracks[1].path, ["title"])
        before = {track.path: track.path.read_bytes() for track in album.tracks}
        result = AlbumResult(
            album,
            _make_release(),
            [
                TrackDiff(album.tracks[0], [TagChange("album", "Desperado", "New Album")]),
                TrackDiff(album.tracks[1], [TagChange("title", "Twenty-One", "New Title")]),
            ],
        )

        with pytest.raises(ValueError, match="Locked fields"):
            apply_changes(result)

        assert {path: path.read_bytes() for path in before} == before

    def test_ignore_locks_writes_locked_field_and_keeps_lock(self, flac_album: Path) -> None:
        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title"])

        result = build_diff(read_album(flac_album), _make_release(), ignore_locks=True)
        apply_changes(result, ignore_locks=True)

        updated = read_album(flac_album).tracks[0]
        assert updated.tags["title"] == "Doolin-Dalton"
        assert updated.locked_fields == ["title"]


class TestOverwritesNeedingReview:
    @staticmethod
    def _result(*changes: TagChange) -> AlbumResult:
        track = TrackTags(Path("/music/01.flac"), {}, format="flac")
        return AlbumResult(
            AlbumTags(Path("/music"), [track]), _make_release(), [TrackDiff(track, list(changes))]
        )

    @pytest.mark.parametrize("field_name", ["title", "artist", "album", "albumartist", "date"])
    def test_flags_overwrite_of_reviewed_field(self, field_name: str) -> None:
        change = TagChange(field_name, "Old", "New")

        overwrites = overwrites_needing_review(self._result(change))

        assert overwrites == [(Path("/music/01.flac"), change)]

    def test_ignores_fill_of_empty_field(self) -> None:
        assert overwrites_needing_review(self._result(TagChange("title", "", "New"))) == []

    def test_ignores_overwrite_of_unreviewed_field(self) -> None:
        change = TagChange("label", "Asylum", "Asylum Records")

        assert overwrites_needing_review(self._result(change)) == []

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("I'm Gonna Be (500 Miles)", "I\u2019m Gonna Be (500 Miles)"),
            ("Greatest Hits 1991-2003", "Greatest Hits 1991\u20132003"),
            ("Dorsey-Itis", "Dorsey\u2010itis"),
            ("Love Is Strange: En Vivo Con Tino", "Love Is Strange: En vivo con Tino"),
            ('"Heroes"', "\u201cHeroes\u201d"),
            ("Wait...", "Wait\u2026"),
            ("Two  Spaces", "Two Spaces"),
        ],
    )
    def test_ignores_style_only_overwrite(self, old: str, new: str) -> None:
        assert overwrites_needing_review(self._result(TagChange("title", old, new))) == []

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("Space Intro", "Space Intro."),
            ("Greatest Hits Volume III 1983-1997", "Greatest Hits, Volume III"),
            ("Yours, Mine and Ours", "Gilmore Girls: The Ultimate Soundtrack, Volume 2"),
        ],
    )
    def test_flags_substantive_overwrite(self, old: str, new: str) -> None:
        assert overwrites_needing_review(self._result(TagChange("album", old, new)))

    def test_ignores_locked_changes(self) -> None:
        track = TrackTags(Path("/music/01.flac"), {}, format="flac")
        result = AlbumResult(
            AlbumTags(Path("/music"), [track]),
            _make_release(),
            [TrackDiff(track, [], locked_changes=[TagChange("title", "Old", "New")])],
        )

        assert overwrites_needing_review(result) == []


def _conflicted_result(*candidate_lists: list[str]) -> AlbumResult:
    """One track per candidate list, each with file ISRC USIR19902220 in conflict."""
    tracks = [
        TrackTags(Path(f"/music/0{n}.flac"), {"tracknumber": str(n)}, format="flac")
        for n in range(1, len(candidate_lists) + 1)
    ]
    diffs = [
        TrackDiff(track, [], [TagConflict("isrc", "USIR19902220", list(candidates))])
        for track, candidates in zip(tracks, candidate_lists, strict=True)
    ]
    return AlbumResult(AlbumTags(Path("/music"), tracks), _make_release(), diffs)


class TestResolveIsrcConflicts:
    def test_keep_resolves_every_conflict_to_file_code(self) -> None:
        result = _conflicted_result(["USDW10110279"], ["USDW10110279", "USIR19701434"])

        resolve_isrc_conflicts(result, policy="keep")

        assert [d.conflicts[0].resolution for d in result.diffs] == ["USIR19902220"] * 2
        assert not result.has_unresolved_conflicts

    def test_musicbrainz_resolves_only_single_candidates(self) -> None:
        result = _conflicted_result(["USDW10110279"], ["USDW10110279", "USIR19701434"])

        resolve_isrc_conflicts(result, policy="musicbrainz")

        assert result.diffs[0].conflicts[0].resolution == "USDW10110279"
        assert result.diffs[1].conflicts[0].resolution is None

    def test_code_overrides_policy_for_one_track(self) -> None:
        result = _conflicted_result(["USDW10110279"], ["USDW10110279", "USIR19701434"])

        resolve_isrc_conflicts(result, policy="musicbrainz", codes={(1, 2): "USIR19701434"})

        assert result.diffs[1].conflicts[0].resolution == "USIR19701434"
        assert not result.has_unresolved_conflicts

    def test_rejects_code_that_is_not_a_candidate(self) -> None:
        result = _conflicted_result(["USDW10110279"])

        with pytest.raises(ValueError, match="USXX00000000"):
            resolve_isrc_conflicts(result, codes={(1, 1): "USXX00000000"})

    def test_rejects_code_for_track_without_conflict(self) -> None:
        result = _conflicted_result(["USDW10110279"])

        with pytest.raises(ValueError, match="no ISRC conflict"):
            resolve_isrc_conflicts(result, codes={(1, 5): "USDW10110279"})

    def test_rejects_unknown_policy(self) -> None:
        with pytest.raises(ValueError, match="policy"):
            resolve_isrc_conflicts(_conflicted_result(["USDW10110279"]), policy="first")


class TestApplyChangesLocksKeptIsrc:
    def test_kept_code_is_locked(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        write_tags(album.tracks[0], [TagChange("isrc", "", "USIR19902220")])
        release = _make_release()
        release.discs[1][0].isrcs = ["USDW10110279"]
        result = build_diff(read_album(flac_album), release)
        resolve_isrc_conflicts(result, policy="keep")

        apply_changes(result)

        updated = read_album(flac_album).tracks[0]
        assert updated.tags["isrc"] == "USIR19902220"
        assert updated.locked_fields == ["isrc"]
        assert build_diff(read_album(flac_album), release).diffs[0].conflicts == []

    def test_replaced_code_is_not_locked(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        write_tags(album.tracks[0], [TagChange("isrc", "", "USIR19902220")])
        release = _make_release()
        release.discs[1][0].isrcs = ["USDW10110279"]
        result = build_diff(read_album(flac_album), release)
        resolve_isrc_conflicts(result, policy="musicbrainz")

        apply_changes(result)

        updated = read_album(flac_album).tracks[0]
        assert updated.tags["isrc"] == "USDW10110279"
        assert updated.locked_fields == []


def _local_album(durations: dict[int, float], albumid: str = "") -> AlbumTags:
    tags = {"musicbrainz_albumid": albumid} if albumid else {}
    tracks = [
        TrackTags(Path(f"/music/0{n}.flac"), {"tracknumber": str(n), **tags}, seconds, "flac")
        for n, seconds in durations.items()
    ]
    return AlbumTags(Path("/music"), tracks)


def _candidate(
    release_id: str,
    seconds: list[float],
    *,
    country: str = "US",
    date: str = "1973",
    media: str = "CD",
) -> MBRelease:
    tracks = [
        MBTrack(number=n, title=f"T{n}", duration_ms=int(value * 1000))
        for n, value in enumerate(seconds, start=1)
    ]
    return MBRelease(
        id=release_id,
        title="Desperado",
        country=country,
        date=date,
        format=media,
        track_count=len(tracks),
        discs={1: tracks},
    )


LOCAL_DURATIONS = {1: 200.0, 2: 180.0, 3: 210.0}


class TestCdPlusDvdCandidate:
    def test_cd_plus_dvd_search_result_counts_the_cd_only(self) -> None:
        from music_tagger.musicbrainz import MusicBrainzClient

        tracks = [
            TrackTags(
                path=Path(f"{n:02d}.flac"),
                tags={"tracknumber": str(n), "discnumber": "1"},
                duration_secs=200.0,
                format="flac",
            )
            for n in range(1, 13)
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        candidate = MusicBrainzClient._parse_search_release(
            {
                "id": "release-cd-dvd",
                "title": "Album",
                "media": [
                    {"format": "CD", "track-count": 12},
                    {"format": "DVD-Video", "track-count": 5},
                ],
            }
        )

        results = score_candidates(album, [candidate])

        assert results[0].stats.track_count_match is True
        assert "partial" not in results[0].flags


class TestRankCandidates:
    def _ranked_ids(self, album: AlbumTags, *candidates: MBRelease) -> list[str]:
        return [match.release.id for match in score_candidates(album, list(candidates))]

    def test_track_count_mismatch_ranks_last(self) -> None:
        longer = _candidate("longer", [200.0, 180.0, 210.0, 190.0])
        exact = _candidate("exact", [201.5, 181.5, 211.5])

        assert self._ranked_ids(_local_album(LOCAL_DURATIONS), longer, exact) == ["exact", "longer"]

    def test_more_tracks_within_2s_ranks_first(self) -> None:
        close = _candidate("close", [200.0, 180.0, 210.0])
        far = _candidate("far", [200.0, 190.0, 220.0])

        assert self._ranked_ids(_local_album(LOCAL_DURATIONS), far, close) == ["close", "far"]

    def test_release_on_files_ranks_first_among_equals(self) -> None:
        album = _local_album(LOCAL_DURATIONS, albumid="current")
        other = _candidate("other", [200.0, 180.0, 210.0])
        current = _candidate("current", [201.0, 181.0, 211.0])

        matches = score_candidates(album, [other, current])

        assert [m.release.id for m in matches] == ["current", "other"]
        assert matches[0].flags == ["on-files"]
        assert matches[1].flags == []

    def test_cd_then_us_then_earliest_date(self) -> None:
        same = [200.0, 180.0, 210.0]
        candidates = [
            _candidate("digital", same, media="Digital Media"),
            _candidate("gb", same, country="GB"),
            _candidate("us-late", same, date="1999"),
            _candidate("us-early", same, date="1973"),
        ]

        assert self._ranked_ids(_local_album(LOCAL_DURATIONS), *candidates) == [
            "us-early",
            "us-late",
            "gb",
            "digital",
        ]

    def test_partial_album_flagged_and_ranked_with_count_matches(self) -> None:
        album = _local_album({1: 200.0, 3: 210.0})
        full = _candidate("full", [200.0, 180.0, 210.0])
        mismatched = _candidate("mismatched", [200.0])

        matches = score_candidates(album, [mismatched, full])

        assert [m.release.id for m in matches] == ["full", "mismatched"]
        assert matches[0].flags == ["partial"]
        assert matches[1].flags == []

    def test_flags_survive_serialization(self) -> None:
        album = _local_album(LOCAL_DURATIONS, albumid="current")
        match = score_candidates(album, [_candidate("current", [200.0, 180.0, 210.0])])[0]

        restored = CandidateMatch.from_dict(match.to_dict())

        assert restored.flags == ["on-files"]


def _file(name: str, **tags: str) -> TrackTags:
    return TrackTags(path=Path("/music") / name, tags=dict(tags), format="flac")


class TestDuplicateTrackKeys:
    def test_two_files_with_the_same_key_raise_and_name_both(self) -> None:
        album = AlbumTags(
            Path("/music"),
            [
                _file("a.flac", tracknumber="1", discnumber="1"),
                _file("b.flac", tracknumber="1", discnumber="1"),
                _file("c.flac", tracknumber="1"),
                _file("d.flac", tracknumber="2"),
            ],
        )

        with pytest.raises(DuplicateTrackError) as error:
            build_diff(album, _make_two_disc_release())

        message = str(error.value)
        assert "disc 1 track 1" in message
        assert '"a.flac"' in message
        assert '"b.flac"' in message

    def test_score_candidates_flags_duplicates_without_raising(self) -> None:
        album = AlbumTags(
            Path("/music"),
            [
                _file("a.flac", tracknumber="1", discnumber="1"),
                _file("b.flac", tracknumber="1", discnumber="1"),
            ],
        )

        [match] = score_candidates(album, [_make_two_disc_release()])

        assert "duplicate-keys" in match.flags


class TestPositionalMatchingPerDisc:
    def test_files_use_their_index_within_their_disc(self) -> None:
        album = AlbumTags(
            Path("/music"),
            [
                _file("01.flac", discnumber="1"),
                _file("02.flac", discnumber="1"),
                _file("03.flac", discnumber="2"),
                _file("04.flac", discnumber="2"),
            ],
        )

        result = build_diff(album, _make_two_disc_release())

        titles = [
            next((c.new_value for c in d.changes if c.field == "title"), None) for d in result.diffs
        ]
        assert titles == [
            "Let's Go Crazy",
            "Take Me With U",
            "The Dance Electric",
            "Love and Sex",
        ]

    def test_unequal_discs_get_the_right_titles(self) -> None:
        release = MBRelease(
            id="release-unequal",
            title="Unequal",
            track_count=4,
            discs={
                1: [
                    MBTrack(number=1, title="One A"),
                    MBTrack(number=2, title="One B"),
                    MBTrack(number=3, title="One C"),
                ],
                2: [MBTrack(number=1, title="Two A")],
            },
        )
        album = AlbumTags(
            Path("/music"),
            [
                _file("01.flac", discnumber="1"),
                _file("02.flac", discnumber="1"),
                _file("03.flac", discnumber="1"),
                _file("04.flac", discnumber="2"),
            ],
        )

        result = build_diff(album, release)

        titles = [
            next((c.new_value for c in d.changes if c.field == "title"), None) for d in result.diffs
        ]
        assert titles == ["One A", "One B", "One C", "Two A"]


class TestPartialEvidence:
    def _release_without_track_1_length(self) -> MBRelease:
        release = _make_release()
        release.discs[1][0].duration_ms = None
        return release

    def test_tracknumber_without_length_needs_a_matching_title(self) -> None:
        track = TrackTags(
            path=Path("01.flac"),
            tags={"tracknumber": "1", "title": "Different song"},
            duration_secs=200.0,
            format="flac",
        )

        result = build_diff(
            AlbumTags(Path("/music"), [track]), self._release_without_track_1_length()
        )

        assert not any(c.field == "title" for c in result.diffs[0].changes)
        assert result.diffs[0].warnings

    def test_tracknumber_without_length_with_matching_title_is_matched(self) -> None:
        track = TrackTags(
            path=Path("01.flac"),
            tags={"tracknumber": "1", "title": "doolin-dalton"},
            duration_secs=200.0,
            format="flac",
        )

        result = build_diff(
            AlbumTags(Path("/music"), [track]), self._release_without_track_1_length()
        )

        title = next(c for c in result.diffs[0].changes if c.field == "title")
        assert title.new_value == "Doolin-Dalton"
        assert result.diffs[0].warnings == []

    def test_two_files_claiming_one_release_track_get_neither_title(self) -> None:
        release = _make_release()
        release.discs[1][1].duration_ms = 100000
        release.discs[1][2].duration_ms = 100000
        album = AlbumTags(
            Path("/music"),
            [
                TrackTags(
                    path=Path("a.flac"),
                    tags={"tracknumber": "1", "title": "Different one"},
                    duration_secs=209.0,
                    format="flac",
                ),
                TrackTags(
                    path=Path("b.flac"),
                    tags={"tracknumber": "2", "title": "Different two"},
                    duration_secs=208.5,
                    format="flac",
                ),
            ],
        )

        result = build_diff(album, release)

        for diff in result.diffs:
            assert not any(c.field == "title" for c in diff.changes)
            assert any("matches the same release track as" in w for w in diff.warnings)


class TestReleaseOwnedFields:
    def _track(self, **tags: str) -> TrackTags:
        return TrackTags(
            path=Path("/music/01.flac"),
            tags={"tracknumber": "1", **tags},
            duration_secs=0.0,
            format="flac",
        )

    def test_retagging_to_a_release_without_barcode_clears_it(self) -> None:
        track = self._track(musicbrainz_albumid="release-A", barcode="123")
        other = MBRelease(id="release-B", title="Other", discs={1: [MBTrack(number=1, title="T")]})

        result = build_diff(AlbumTags(Path("/music"), [track]), other)

        assert TagChange("barcode", "123", "") in result.diffs[0].changes

    def test_same_release_without_barcode_keeps_it(self) -> None:
        track = self._track(musicbrainz_albumid="release-A", barcode="123")
        same = MBRelease(id="release-A", title="Same", discs={1: [MBTrack(number=1, title="T")]})

        result = build_diff(AlbumTags(Path("/music"), [track]), same)

        assert not any(c.field == "barcode" for c in result.diffs[0].changes)

    def test_locked_field_is_not_cleared(self) -> None:
        track = self._track(musicbrainz_albumid="release-A", label="Old Label")
        track.locked_fields = ["label"]
        other = MBRelease(id="release-B", title="Other", discs={1: [MBTrack(number=1, title="T")]})

        result = build_diff(AlbumTags(Path("/music"), [track]), other)

        assert not any(c.field == "label" for c in result.diffs[0].changes)
        assert TagChange("label", "Old Label", "") in result.diffs[0].locked_changes

    def test_release_without_artist_does_not_clear_the_artist(self) -> None:
        track = self._track(musicbrainz_albumid="release-A", artist="Eagles")
        other = MBRelease(id="release-B", title="Other", discs={1: [MBTrack(number=1, title="T")]})

        result = build_diff(AlbumTags(Path("/music"), [track]), other)

        assert not any(c.field == "artist" for c in result.diffs[0].changes)

    def test_clearing_a_reviewed_title_needs_review(self) -> None:
        track = self._track(musicbrainz_albumid="release-A", title="Doolin-Dalton")
        result = AlbumResult(
            album=AlbumTags(Path("/music"), [track]),
            release=MBRelease(id="release-B", title="Other"),
            diffs=[TrackDiff(track, [TagChange("title", "Doolin-Dalton", "")])],
        )

        assert overwrites_needing_review(result) == [
            (track.path, TagChange("title", "Doolin-Dalton", ""))
        ]


class TestUnmatchedFullAlbumFile:
    def _album_with_track_13(self) -> AlbumTags:
        tracks = [
            _file("01.flac", tracknumber="1", discnumber="1"),
            _file("02.flac", tracknumber="2", discnumber="1"),
            _file("13.flac", tracknumber="13", discnumber="1"),
        ]
        return AlbumTags(Path("/music"), tracks)

    def _release(self) -> MBRelease:
        return MBRelease(
            id="release-1",
            title="Album",
            artist="Eagles",
            track_count=3,
            discs={
                1: [
                    MBTrack(number=1, title="One"),
                    MBTrack(number=2, title="Two"),
                    MBTrack(number=3, title="Three"),
                ]
            },
        )

    def test_unmatched_file_gets_a_warning(self) -> None:
        result = build_diff(self._album_with_track_13(), self._release())

        assert any(
            "no release track for disc 1 track 13; only album fields proposed" in w
            for w in result.diffs[2].warnings
        )

    def test_unmatched_file_is_not_proposed_the_artist(self) -> None:
        result = build_diff(self._album_with_track_13(), self._release())

        assert not any(c.field == "artist" for c in result.diffs[2].changes)

    def test_unmatched_file_keeps_its_track_ids_after_a_release_change(self) -> None:
        """Track-owned fields are cleared only for matched files, so an unmatched one keeps them."""
        album = AlbumTags(
            Path("/music"),
            [
                _file("01.flac", tracknumber="1", discnumber="1"),
                _file("02.flac", tracknumber="2", discnumber="1"),
                _file(
                    "13.flac",
                    tracknumber="13",
                    discnumber="1",
                    artist="Old Band",
                    musicbrainz_albumid="other-release",
                ),
            ],
        )

        result = build_diff(album, self._release())

        assert not any(
            c.field in {"artist", "musicbrainz_artistid"} for c in result.diffs[2].changes
        )


def _two_artist_release() -> MBRelease:
    release = _make_release()
    release.artist = "Alice feat. Bob"
    release.artist_credits = (("Alice", "idA"), ("Bob", "idB"))
    for track in release.discs[1]:
        track.artist = "Alice feat. Bob"
        track.artist_credits = (("Alice", "idA"), ("Bob", "idB"))
    return release


def _three_track_album(extra: dict[str, str] | None = None) -> AlbumTags:
    tracks = [
        TrackTags(
            path=Path(f"0{n}.flac"),
            tags={"tracknumber": str(n), "discnumber": "1", **(extra or {})},
            duration_secs=200.0,
            format="flac",
        )
        for n in (1, 2, 3)
    ]
    return AlbumTags(directory=Path("/music"), tracks=tracks)


class TestMultiValuedArtists:
    def test_build_diff_proposes_every_artist_and_id(self) -> None:
        result = build_diff(_three_track_album(), _two_artist_release())
        changes = {c.field: c for c in result.diffs[0].changes}

        assert changes["artist"].new_value == "Alice feat. Bob"
        assert changes["artists"].new_values == ("Alice", "Bob")
        assert changes["musicbrainz_artistid"].new_values == ("idA", "idB")

    def test_existing_single_id_becomes_a_two_value_change(self) -> None:
        album = _three_track_album({"musicbrainz_artistid": "idA"})

        result = build_diff(album, _two_artist_release())
        change = {c.field: c for c in result.diffs[0].changes}["musicbrainz_artistid"]

        assert (change.old_value, change.new_value) == ("idA", "idA; idB")
        assert change.old_values == ("idA",)

    def test_mp3_retag_after_writing_proposes_nothing(self, mp3_album: Path) -> None:
        first = build_diff(read_album(mp3_album), _two_artist_release())
        apply_changes(first)

        second = build_diff(read_album(mp3_album), _two_artist_release())

        assert [c.field for d in second.diffs for c in d.changes] == []

    def test_retag_after_writing_proposes_nothing(self, flac_album: Path) -> None:
        first = build_diff(read_album(flac_album), _two_artist_release())
        apply_changes(first)

        second = build_diff(read_album(flac_album), _two_artist_release())

        assert [c.field for d in second.diffs for c in d.changes] == []


class TestMultiValuedDiffJson:
    def test_multi_change_survives_the_json_round_trip(self) -> None:
        track = TrackTags(Path("/music/01.flac"), {}, format="flac")
        change = TagChange(
            field="artists",
            old_value="",
            new_value="Alice; Bob",
            new_values=("Alice", "Bob"),
            multi=True,
        )
        result = AlbumResult(
            AlbumTags(Path("/music"), [track]),
            _make_release(),
            [TrackDiff(track, [change])],
        )

        restored = AlbumResult.from_dict(result.to_dict())

        assert restored.diffs[0].changes == [change]


def _hidden_file(name: str = "00. Hidden Track One Audio.flac", **extra: str) -> TrackTags:
    tags = {"tracknumber": "0", "discnumber": "1", "title": "Hidden Track One Audio", **extra}
    return TrackTags(Path("/music") / name, tags, duration_secs=209.0, format="flac")


def _album_with_hidden_track() -> AlbumTags:
    numbered = [_file(f"0{n}.flac", tracknumber=str(n), discnumber="1") for n in (1, 2, 3)]
    return AlbumTags(Path("/music"), [*numbered, _hidden_file()])


class TestHiddenTrack:
    def test_hidden_file_is_not_counted_against_the_release(self) -> None:
        [match] = score_candidates(_album_with_hidden_track(), [_make_release()])

        assert match.stats.track_count_match
        assert "partial" not in match.flags

    def test_numbered_files_are_matched_and_the_album_is_not_partial(self) -> None:
        result = build_diff(_album_with_hidden_track(), _make_release())

        titles = [
            {c.field: c.new_value for c in diff.changes}.get("title") for diff in result.diffs[:3]
        ]
        assert titles == ["Doolin-Dalton", "Twenty-One", "Out of Control"]
        assert all("no release track" not in w for diff in result.diffs for w in diff.warnings)

    def test_hidden_file_gets_album_fields_only_and_a_note(self) -> None:
        result = build_diff(_album_with_hidden_track(), _make_release())
        hidden = result.diffs[3]
        fields = {c.field for c in hidden.changes}

        assert "musicbrainz_albumid" in fields
        assert "album" in fields
        assert fields.isdisjoint({"title", "tracknumber", "artist"})
        assert hidden.warnings == [
            "00. Hidden Track One Audio.flac: hidden track before track 1; album fields only"
        ]

    def test_single_hidden_file_is_not_a_duplicate_key(self) -> None:
        assert duplicate_track_keys(_album_with_hidden_track()) == {}

    def test_two_hidden_files_on_one_disc_are_still_a_duplicate(self) -> None:
        album = AlbumTags(
            Path("/music"),
            [
                _hidden_file("00 a.flac"),
                _hidden_file("00 b.flac"),
                _file("01.flac", tracknumber="1"),
            ],
        )

        assert set(duplicate_track_keys(album)) == {(1, 0)}

    def test_hidden_file_is_not_matched_to_a_release_track_in_a_partial_album(self) -> None:
        # Two numbered files and a hidden file: the release has three tracks, so the album is
        # partial. The hidden file's duration fits release track 1, but it must not take it.
        album = AlbumTags(
            Path("/music"),
            [
                _file("01.flac", tracknumber="1", discnumber="1", title="Doolin-Dalton"),
                _file("02.flac", tracknumber="2", discnumber="1", title="Twenty-One"),
                _hidden_file(),
            ],
        )

        result = build_diff(album, _make_release())
        hidden = result.diffs[2]

        assert not {"title", "tracknumber"} & {c.field for c in hidden.changes}
        assert hidden.warnings == [
            "00. Hidden Track One Audio.flac: hidden track before track 1; album fields only"
        ]


class TestDiscTotalReadOrder:
    def test_build_diff_proposes_the_disctotal_of_a_one_disc_release(
        self, flac_album: Path
    ) -> None:
        for path in flac_album.glob("*.flac"):
            audio = FLAC(path)
            audio["TOTALDISCS"] = ["1"]
            audio["DISCTOTAL"] = ["2"]
            audio.save()

        result = build_diff(read_album(flac_album), _make_release())
        changes = {c.field: c for c in result.diffs[0].changes}

        assert "totaldiscs" in changes
        assert (changes["totaldiscs"].old_value, changes["totaldiscs"].new_value) == ("2", "1")
