"""DATE on a reissue: whipper's release date is replaced by the original date."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import (
    AlbumResult,
    TrackDiff,
    _build_new_tags,
    is_release_date,
    overwrites_needing_review,
)
from music_tagger.tags import AlbumTags, TagChange, TrackTags

REISSUE = MBRelease(
    id="r-2023",
    title="Dixie Chicken",
    date="2023-06-23",
    first_release_date="1973-02",
    track_count=1,
    discs={1: [MBTrack(number=1, title="Dixie Chicken")]},
)


def _track(date: str | None) -> TrackTags:
    tags = {"date": date} if date is not None else {}
    return TrackTags(path=Path("/music/01.flac"), tags=tags, format="flac")


class TestDateTag:
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            (None, "1973-02"),  # empty: filled
            ("2023-06-23", "1973-02"),  # whipper's reissue date: replaced
            ("1973", None),  # a user's original date: kept
            ("2023", None),  # a year only is not the release date: kept
        ],
    )
    def test_date(self, old: str | None, new: str | None) -> None:
        tags = _build_new_tags(REISSUE, _track(old), 0, 1)
        assert tags.get("date") == new

    def test_original_release_keeps_its_date(self) -> None:
        original = MBRelease(
            id="r-1973",
            title="Dixie Chicken",
            date="1973-02",
            first_release_date="1973-02",
            track_count=1,
            discs={1: [MBTrack(number=1, title="Dixie Chicken")]},
        )
        assert not is_release_date("1973-02", original)
        assert "date" not in _build_new_tags(original, _track("1973-02"), 0, 1)


class TestGate:
    def _result(self, change: TagChange) -> AlbumResult:
        track = _track("2023-06-23")
        return AlbumResult(
            AlbumTags(Path("/music"), [track]), REISSUE, [TrackDiff(track, [change])]
        )

    def test_reissue_date_to_original_needs_no_review(self) -> None:
        change = TagChange("date", "2023-06-23", "1973-02")
        assert overwrites_needing_review(self._result(change)) == []

    def test_other_date_changes_still_need_review(self) -> None:
        change = TagChange("date", "1975", "1973-02")
        assert overwrites_needing_review(self._result(change))
