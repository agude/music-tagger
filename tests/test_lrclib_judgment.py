"""Candidate widening past the result cap, and the lyric-availability evidence."""

from __future__ import annotations

from typing import Any

import httpx

from music_tagger.lrclib import (
    SEARCH_RESULT_CAP,
    LrclibClient,
    Outcome,
    TrackEvidence,
    lookup,
)

EVIDENCE = TrackEvidence(title="Song", artist="Band", album="Album", duration_secs=200.0)


def _record(record_id: int, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": record_id,
        "name": "Song",
        "trackName": "Song",
        "artistName": "Band",
        "albumName": "Album",
        "duration": 200.0,
        "instrumental": False,
        "plainLyrics": "Plain line",
        "syncedLyrics": "[00:01.00]Timed line",
    }
    data.update(overrides)
    return data


def _client(responder: Any) -> LrclibClient:
    return LrclibClient(
        transport=httpx.MockTransport(responder), sleep=lambda _s: None, min_interval=0.0
    )


def _search_responder(narrow: list[dict[str, Any]], broad: list[dict[str, Any]]) -> Any:
    """Answer the album-filtered search with `narrow` and the unfiltered one with `broad`."""

    def respond(request: httpx.Request) -> httpx.Response:
        if "album_name" in request.url.params:
            return httpx.Response(200, json=narrow)
        return httpx.Response(200, json=broad)

    return respond


class TestCapWidening:
    def test_capped_album_search_is_settled_by_uncapped_broader_search(self) -> None:
        # The narrow search fills the cap with records that do not match duration.
        narrow = [_record(i, duration=999.0) for i in range(1, SEARCH_RESULT_CAP + 1)]
        broad = [_record(500)]
        selection = lookup(_client(_search_responder(narrow, broad)), EVIDENCE)
        assert selection.outcome == Outcome.SELECTED
        assert selection.record is not None and selection.record.id == 500

    def test_capped_in_both_searches_stays_review(self) -> None:
        narrow = [_record(i, duration=999.0) for i in range(1, SEARCH_RESULT_CAP + 1)]
        broad = [_record(i, duration=999.0) for i in range(100, 100 + SEARCH_RESULT_CAP)]
        selection = lookup(_client(_search_responder(narrow, broad)), EVIDENCE)
        assert selection.outcome == Outcome.REVIEW
        assert "cap" in selection.reason

    def test_uncapped_narrow_search_is_not_widened(self) -> None:
        requests: list[bool] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append("album_name" in request.url.params)
            return httpx.Response(200, json=[_record(1)])

        lookup(_client(respond), EVIDENCE)
        assert requests == [True]

    def test_merged_candidates_are_unique_by_id(self) -> None:
        narrow = [_record(i, duration=999.0) for i in range(1, SEARCH_RESULT_CAP + 1)]
        broad = [_record(1), _record(500)]
        selection = lookup(_client(_search_responder(narrow, broad)), EVIDENCE)
        ids = [verdict.record.id for verdict in selection.verdicts]
        assert len(ids) == len(set(ids)) == SEARCH_RESULT_CAP + 1


class TestLyricAvailabilityNotes:
    def _notes_for(self, record: dict[str, Any]) -> tuple[str, ...]:
        selection = lookup(_client(lambda request: httpx.Response(200, json=[record])), EVIDENCE)
        return selection.verdicts[0].notes

    def test_synced_and_plain(self) -> None:
        assert "has synced text and plain text" in self._notes_for(_record(1))

    def test_plain_only(self) -> None:
        notes = self._notes_for(_record(1, syncedLyrics=None))
        assert "has plain text" in notes

    def test_no_text(self) -> None:
        assert "no lyric text" in self._notes_for(_record(1, plainLyrics=None, syncedLyrics=None))

    def test_instrumental(self) -> None:
        notes = self._notes_for(_record(1, instrumental=True, plainLyrics=None, syncedLyrics=None))
        assert "instrumental" in notes
