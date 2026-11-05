from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from music_tagger.lrclib import (
    APP_ID,
    MIN_INTERVAL_SECS,
    SEARCH_RESULT_CAP,
    LrclibClient,
    LyricsRecord,
    Outcome,
    ProviderError,
    TrackEvidence,
    fetch_explicit,
    lookup,
    pick_lyrics,
)
from music_tagger.lyrics import LyricsKind

Responder = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    """Advances only when the client sleeps, so spacing is tested without real waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(responder: Responder, clock: FakeClock | None = None) -> tuple[LrclibClient, FakeClock]:
    clock = clock or FakeClock()
    client = LrclibClient(
        transport=httpx.MockTransport(responder),
        sleep=clock.sleep,
        clock=clock,
        min_interval=0.0,
    )
    return client, clock


def _record(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1,
        "name": "Song",
        "trackName": "Song",
        "artistName": "Band",
        "albumName": "Album",
        "duration": 200.0,
        "instrumental": False,
        "hasWordSync": True,
        "plainLyrics": "Plain line",
        "syncedLyrics": "[00:01.00]Timed line",
        "lyricsfile": "",
    }
    data.update(overrides)
    return data


def _json_responder(payload: Any, status: int = 200) -> Responder:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return respond


EVIDENCE = TrackEvidence(title="Song", artist="Band", album="Album", duration_secs=200.0)


def _lookup_with(records: list[dict[str, Any]], evidence: TrackEvidence = EVIDENCE) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get":
            return httpx.Response(404, json={"name": "TrackNotFound"})
        return httpx.Response(200, json=records)

    client, _ = _client(respond)
    return lookup(client, evidence)


class TestClient:
    def test_user_agent_names_project_without_email(self) -> None:
        seen: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[])

        client, _ = _client(respond)
        client.search(title="Song", artist="Band")
        agent = seen[0].headers["User-Agent"]
        assert agent.startswith("music-tagger/")
        assert "@" not in agent

    def test_every_request_sends_app_id(self) -> None:
        seen: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/api/search":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=_record())

        client, _ = _client(respond)
        client.search(title="Song", artist="Band")
        client.get(1)
        assert [r.headers["Lrclib-App-Id"] for r in seen] == [APP_ID, APP_ID]

    def test_default_pacing_stays_within_registered_allowance(self) -> None:
        # Registered client-side apps get about 180 requests/min per IP.
        assert 60 / MIN_INTERVAL_SECS <= 180

    def test_only_get_requests(self) -> None:
        methods: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.url.path == "/api/search":
                return httpx.Response(200, json=[_record()])
            return httpx.Response(200, json=_record())

        client, _ = _client(respond)
        client.search(title="Song", artist="Band", album="Album")
        client.get(1)
        assert methods == ["GET", "GET"]

    def test_search_sends_names_and_omits_empty_album(self) -> None:
        urls: list[httpx.URL] = []

        def respond(request: httpx.Request) -> httpx.Response:
            urls.append(request.url)
            return httpx.Response(200, json=[])

        client, _ = _client(respond)
        client.search(title="Song", artist="Band", album="")
        assert urls[0].path == "/api/search"
        assert dict(urls[0].params) == {"track_name": "Song", "artist_name": "Band"}

    def test_search_parses_record_fields(self) -> None:
        client, _ = _client(_json_responder([_record(plainLyrics=None, syncedLyrics=None)]))
        result = client.search(title="Song", artist="Band")
        assert result.truncated is False
        assert result.records == [
            LyricsRecord(
                id=1,
                track_name="Song",
                artist_name="Band",
                album_name="Album",
                duration_secs=200.0,
                instrumental=False,
                plain_lyrics="",
                synced_lyrics="",
            )
        ]

    def test_search_at_cap_is_truncated(self) -> None:
        records = [_record(id=i) for i in range(SEARCH_RESULT_CAP)]
        client, _ = _client(_json_responder(records))
        assert client.search(title="Song", artist="Band").truncated is True

    def test_get_fetches_by_id(self) -> None:
        paths: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json=_record(id=77))

        client, _ = _client(respond)
        record = client.get(77)
        assert record is not None and record.id == 77
        assert paths == ["/api/get/77"]

    def test_get_not_found_is_none(self) -> None:
        client, _ = _client(_json_responder({"name": "TrackNotFound"}, status=404))
        assert client.get(5) is None

    def test_non_list_search_reply_is_provider_error(self) -> None:
        client, _ = _client(_json_responder({"oops": True}))
        with pytest.raises(ProviderError):
            client.search(title="Song", artist="Band")

    def test_non_json_reply_is_provider_error(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        client, _ = _client(respond)
        with pytest.raises(ProviderError, match="not JSON"):
            client.search(title="Song", artist="Band")

    def test_client_error_is_not_retried(self) -> None:
        attempts: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(400, json={})

        client, clock = _client(respond)
        with pytest.raises(ProviderError):
            client.search(title="Song", artist="Band")
        assert len(attempts) == 1
        assert clock.sleeps == []

    def test_rate_limit_then_success_honors_retry_after(self) -> None:
        responses = iter(
            [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json=[])]
        )
        client, clock = _client(lambda request: next(responses))
        assert client.search(title="Song", artist="Band").records == []
        assert clock.sleeps == [2.0]

    def test_rate_limit_exhausted_is_rate_limited(self) -> None:
        attempts: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, headers={"Retry-After": "2"})

        client, clock = _client(respond)
        with pytest.raises(ProviderError) as info:
            client.search(title="Song", artist="Band")
        assert info.value.rate_limited is True
        assert len(attempts) == 4
        assert clock.sleeps == [2.0, 2.0, 2.0]

    def test_long_retry_after_fails_without_waiting(self) -> None:
        attempts: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, headers={"Retry-After": "600"})

        client, clock = _client(respond)
        with pytest.raises(ProviderError) as info:
            client.search(title="Song", artist="Band")
        assert info.value.rate_limited is True
        assert len(attempts) == 1
        assert clock.sleeps == []

    def test_outage_backs_off_exponentially(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client, clock = _client(respond)
        with pytest.raises(ProviderError) as info:
            client.search(title="Song", artist="Band")
        assert info.value.rate_limited is False
        assert clock.sleeps == [1.0, 2.0, 4.0]

    def test_network_error_then_success(self) -> None:
        calls = iter([httpx.ConnectError("refused"), httpx.Response(200, json=_record())])

        def respond(request: httpx.Request) -> httpx.Response:
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        client, _ = _client(respond)
        assert client.get(1) is not None

    def test_network_error_exhausted_is_outage(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow")

        client, _ = _client(respond)
        with pytest.raises(ProviderError) as info:
            client.get(1)
        assert info.value.rate_limited is False

    def test_requests_are_spaced(self) -> None:
        clock = FakeClock()
        client = LrclibClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
            sleep=clock.sleep,
            clock=clock,
            min_interval=0.25,
        )
        client.search(title="Song", artist="Band")
        client.search(title="Song", artist="Band")
        assert clock.sleeps == [0.25]


class TestLookupSelection:
    def test_unique_match_is_selected_with_synchronized_text(self) -> None:
        selection = _lookup_with([_record()])
        assert selection.outcome == Outcome.SELECTED
        assert selection.record is not None and selection.record.id == 1
        assert selection.lyrics is not None
        assert selection.lyrics.kind == LyricsKind.SYNCHRONIZED
        assert selection.lyrics.text == "[00:01.00]Timed line"

    def test_unique_match_with_plain_text_only_is_reviewed(self) -> None:
        selection = _lookup_with([_record(syncedLyrics="")])
        assert selection.outcome == Outcome.REVIEW
        assert "plain text only" in selection.reason
        assert selection.lyrics is None

    def test_unique_instrumental_match_is_still_selected_as_instrumental(self) -> None:
        selection = _lookup_with([_record(instrumental=True, plainLyrics="", syncedLyrics="")])
        assert selection.outcome == Outcome.INSTRUMENTAL

    def test_duration_boundary_is_inclusive(self) -> None:
        assert _lookup_with([_record(duration=202.0)]).outcome == Outcome.SELECTED

    def test_duration_outside_two_seconds_is_no_match(self) -> None:
        selection = _lookup_with([_record(duration=202.5)])
        assert selection.outcome == Outcome.NO_MATCH
        assert any("off by +2.5s" in note for note in selection.verdicts[0].notes)

    def test_alternate_version_is_not_matched(self) -> None:
        selection = _lookup_with([_record(trackName="Song (Live)", name="Song (Live)")])
        assert selection.outcome == Outcome.NO_MATCH
        assert any("title differs" in note for note in selection.verdicts[0].notes)

    def test_remix_title_does_not_match_plain_title(self) -> None:
        assert _lookup_with([_record(trackName="Song (Remix)")]).outcome == Outcome.NO_MATCH

    def test_matching_ignores_case_and_diacritics(self) -> None:
        evidence = TrackEvidence(title="SEÑOR", artist="Band", album="Album", duration_secs=200.0)
        selection = _lookup_with([_record(trackName="senor")], evidence)
        assert selection.outcome == Outcome.SELECTED

    def test_non_latin_titles_do_not_collapse_together(self) -> None:
        evidence = TrackEvidence(title="日本", artist="Band", album="Album", duration_secs=200.0)
        selection = _lookup_with([_record(trackName="中国")], evidence)
        assert selection.outcome == Outcome.NO_MATCH

    def test_no_result(self) -> None:
        assert _lookup_with([]).outcome == Outcome.NO_MATCH

    def test_ambiguous_when_album_does_not_separate_them(self) -> None:
        selection = _lookup_with([_record(id=1), _record(id=2)])
        assert selection.outcome == Outcome.AMBIGUOUS
        assert selection.record is None
        assert len(selection.verdicts) == 2

    def test_album_separates_two_plausible_records(self) -> None:
        records = [_record(id=1, albumName="Single"), _record(id=2, albumName="Album")]
        selection = _lookup_with(records)
        assert selection.outcome == Outcome.SELECTED
        assert selection.record is not None and selection.record.id == 2

    def test_only_plausible_record_on_other_album_is_review(self) -> None:
        selection = _lookup_with([_record(albumName="Greatest Hits")])
        assert selection.outcome == Outcome.REVIEW
        assert "Greatest Hits" in selection.reason

    def test_instrumental_match(self) -> None:
        selection = _lookup_with([_record(instrumental=True, plainLyrics=None, syncedLyrics=None)])
        assert selection.outcome == Outcome.INSTRUMENTAL
        assert selection.record is not None
        assert selection.lyrics is None

    def test_match_without_text_is_no_text(self) -> None:
        selection = _lookup_with([_record(plainLyrics="", syncedLyrics="")])
        assert selection.outcome == Outcome.NO_TEXT
        assert selection.lyrics is None

    def test_missing_title_makes_no_request(self) -> None:
        seen: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(1)
            return httpx.Response(200, json=[])

        client, _ = _client(respond)
        evidence = TrackEvidence(title="", artist="Band", album="Album", duration_secs=200.0)
        selection = lookup(client, evidence)
        assert selection.outcome == Outcome.MISSING_METADATA
        assert "title" in selection.reason
        assert seen == []

    def test_missing_duration_makes_no_request(self) -> None:
        seen: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(1)
            return httpx.Response(200, json=[])

        client, _ = _client(respond)
        evidence = TrackEvidence(title="Song", artist="Band", album="Album", duration_secs=None)
        assert lookup(client, evidence).outcome == Outcome.MISSING_METADATA
        assert seen == []

    def test_missing_album_is_review_not_selection(self) -> None:
        evidence = TrackEvidence(title="Song", artist="Band", album="", duration_secs=200.0)
        assert _lookup_with([_record()], evidence).outcome == Outcome.REVIEW

    def test_truncated_search_never_selects(self) -> None:
        records = [_record(id=i, albumName="Other") for i in range(1, SEARCH_RESULT_CAP)]
        records.append(_record(id=99))
        selection = _lookup_with(records)
        assert selection.outcome == Outcome.REVIEW
        assert "cap" in selection.reason

    def test_capped_search_adds_exact_record_as_candidate(self) -> None:
        capped = [_record(id=i, albumName="Other") for i in range(1, SEARCH_RESULT_CAP + 1)]
        exact = _record(id=500, albumName="Album")

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/get":
                assert request.url.params["duration"] == "200"
                return httpx.Response(200, json=exact)
            return httpx.Response(200, json=capped)

        client, _ = _client(respond)
        selection = lookup(client, EVIDENCE)
        assert selection.outcome == Outcome.REVIEW
        assert 500 in [verdict.record.id for verdict in selection.verdicts]

    def test_capped_search_without_exact_record_stays_review(self) -> None:
        capped = [_record(id=i, albumName="Other") for i in range(1, SEARCH_RESULT_CAP + 1)]

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/get":
                return httpx.Response(404, json={"name": "TrackNotFound"})
            return httpx.Response(200, json=capped)

        client, _ = _client(respond)
        selection = lookup(client, EVIDENCE)
        assert selection.outcome == Outcome.REVIEW
        assert all(verdict.record.id != 500 for verdict in selection.verdicts)

    def test_rate_limited_search(self) -> None:
        client, _ = _client(lambda request: httpx.Response(429, headers={"Retry-After": "600"}))
        assert lookup(client, EVIDENCE).outcome == Outcome.RATE_LIMITED

    def test_outage_during_search(self) -> None:
        client, _ = _client(lambda request: httpx.Response(503))
        assert lookup(client, EVIDENCE).outcome == Outcome.PROVIDER_FAILURE


class TestPickLyrics:
    def test_prefers_synchronized_text(self) -> None:
        record = _record()
        choice = pick_lyrics(_to_record(record))
        assert choice is not None and choice.kind == LyricsKind.SYNCHRONIZED

    def test_falls_back_to_plain_when_synchronized_is_malformed(self) -> None:
        choice = pick_lyrics(_to_record(_record(syncedLyrics="Words but no timestamps")))
        assert choice is not None
        assert choice.kind == LyricsKind.PLAIN
        assert choice.text == "Plain line"

    def test_plain_only(self) -> None:
        choice = pick_lyrics(_to_record(_record(syncedLyrics=None)))
        assert choice is not None and choice.kind == LyricsKind.PLAIN

    def test_nothing_usable_is_none(self) -> None:
        assert pick_lyrics(_to_record(_record(plainLyrics="", syncedLyrics=""))) is None


def _to_record(data: dict[str, Any]) -> LyricsRecord:
    return LyricsRecord(
        id=data["id"],
        track_name=data["trackName"],
        artist_name=data["artistName"],
        album_name=data["albumName"],
        duration_secs=data["duration"],
        instrumental=data["instrumental"],
        plain_lyrics=data["plainLyrics"] or "",
        synced_lyrics=data["syncedLyrics"] or "",
    )


class TestExplicitRecord:
    def test_fetches_only_that_record(self) -> None:
        paths: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json=_record(id=42, artistName="Someone Else"))

        client, _ = _client(respond)
        selection = fetch_explicit(client, 42, EVIDENCE)
        assert paths == ["/api/get/42"]
        assert selection.outcome == Outcome.SELECTED
        assert "explicit record id 42" in selection.reason

    def test_mismatch_is_shown_in_verdict(self) -> None:
        client, _ = _client(_json_responder(_record(id=42, artistName="Someone Else")))
        selection = fetch_explicit(client, 42, EVIDENCE)
        assert any("artist differs" in note for note in selection.verdicts[0].notes)

    def test_instrumental_explicit(self) -> None:
        client, _ = _client(
            _json_responder(_record(instrumental=True, plainLyrics=None, syncedLyrics=None))
        )
        assert fetch_explicit(client, 1, EVIDENCE).outcome == Outcome.INSTRUMENTAL

    def test_unknown_id_is_no_match(self) -> None:
        client, _ = _client(_json_responder({"name": "TrackNotFound"}, status=404))
        assert fetch_explicit(client, 999, EVIDENCE).outcome == Outcome.NO_MATCH

    def test_outage_is_provider_failure(self) -> None:
        client, _ = _client(lambda request: httpx.Response(500))
        assert fetch_explicit(client, 1, EVIDENCE).outcome == Outcome.PROVIDER_FAILURE

    def test_rate_limit_is_rate_limited(self) -> None:
        client, _ = _client(lambda request: httpx.Response(429, headers={"Retry-After": "600"}))
        assert fetch_explicit(client, 1, EVIDENCE).outcome == Outcome.RATE_LIMITED
