"""The review gate on a whipper unknown rip: placeholders count as empty there only."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_tagger.musicbrainz import MBRelease
from music_tagger.tagger import AlbumResult, TrackDiff, overwrites_needing_review
from music_tagger.tags import AlbumTags, TagChange, TrackTags

DISC_ID = "Q_e8TdUj6JRttcRaQtDkZNqD.XU-"
RELEASE = MBRelease(id="rel-1", title="Dixie Chicken")


class TestPlaceholderGate:
    def _result(self, change: TagChange) -> AlbumResult:
        return self._result_for(change, {"musicbrainz_discid": DISC_ID})

    @staticmethod
    def _result_for(change: TagChange, tags: dict[str, str]) -> AlbumResult:
        track = TrackTags(Path("/x/01.flac"), tags, format="flac")
        return AlbumResult(AlbumTags(Path("/x"), [track]), RELEASE, [TrackDiff(track, [change])])

    @pytest.mark.parametrize(
        ("field", "old"),
        [
            ("title", "Unknown Track"),
            ("artist", "Unknown Artist"),
            ("albumartist", "Unknown Artist"),
            ("album", "Unknown Album"),
        ],
    )
    def test_whipper_placeholder_needs_no_review(self, field: str, old: str) -> None:
        assert overwrites_needing_review(self._result(TagChange(field, old, "Real"))) == []

    @pytest.mark.parametrize(
        ("field", "old"),
        [("title", "Unknown Artist"), ("album", "Unknown Track"), ("title", "unknown track")],
    )
    def test_placeholder_in_another_field_or_case_needs_review(self, field: str, old: str) -> None:
        assert overwrites_needing_review(self._result(TagChange(field, old, "Real")))

    @pytest.mark.parametrize(
        "tags",
        [
            {},
            {"musicbrainz_albumid": "rel-0"},
            {"musicbrainz_discid": DISC_ID, "musicbrainz_albumid": "rel-0"},
        ],
        ids=["library file", "tagged file", "tagged rip"],
    )
    def test_placeholder_outside_an_unknown_rip_needs_review(self, tags: dict[str, str]) -> None:
        change = TagChange("title", "Unknown Track", "Real")
        assert overwrites_needing_review(self._result_for(change, tags))
