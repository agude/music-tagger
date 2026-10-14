from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import (
    AlbumResult,
    CandidateMatch,
    TagConflict,
    TrackDiff,
    _build_new_tags,
    _parse_disc_number,
    _parse_track_number,
    apply_changes,
    build_diff,
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
        assert tags["musicbrainz_artistid"] == "artist-a"
        assert tags["musicbrainz_albumartistid"] == "album-artist"

        tags2 = _build_new_tags(release, track, 1, 1)
        assert tags2["artist"] == "Artist B"
        assert tags2["albumartist"] == "Various Artists"
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
                        number=1,
                        title="Song",
                        recording_id="rec-1",
                        track_id="trk-1",
                        isrcs=["USEE10400287"],
                        work_id="work-1",
                        composer="Bach",
                        composer_id="c1",
                        producer="George Martin",
                        producer_id="p1",
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
        track = TrackTags(Path("/music/01.flac"), {"tracknumber": "1"}, format="flac")
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
            Path("/music/01.flac"), {"tracknumber": "1", "isrc": "USIR19902220"}, format="flac"
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
            Path("/music/01.flac"), {"tracknumber": "1", "isrc": "USIR19902220"}, format="flac"
        )
        release = _make_release()
        release.discs[1][0].isrcs = isrcs

        result = build_diff(AlbumTags(Path("/music"), [track]), release)

        assert result.has_unresolved_conflicts
        assert result.diffs[0].conflicts == [TagConflict("isrc", "USIR19902220", isrcs)]
        assert not any(change.field == "isrc" for change in result.diffs[0].changes)

    def test_preserves_unverified_isrc_when_recording_has_no_codes(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"), {"tracknumber": "1", "isrc": "USIR19902220"}, format="flac"
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

    def test_positional_fallback_warns(self, capsys) -> None:
        """Positional fallback prints a warning to stderr."""
        release = _make_release()
        tracks = [
            TrackTags(path=Path("01.flac"), tags={}, duration_secs=209.0, format="flac"),
        ]
        album = AlbumTags(directory=Path("/music"), tracks=tracks)
        build_diff(album, release)

        captured = capsys.readouterr()
        assert "no tracknumber tags found" in captured.err
        assert "matching 1 files by position" in captured.err


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
