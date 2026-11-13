from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from itertools import pairwise
from threading import Barrier, Lock
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from music_tagger.musicbrainz import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECS,
    MBRelease,
    MBTrack,
    MusicBrainzClient,
    _build_artist_credit,
    _parse_recording_credits,
)

SEARCH_RESPONSE = {
    "releases": [
        {
            "id": "release-1",
            "title": "Desperado",
            "date": "1973-04-17",
            "country": "US",
            "status": "Official",
            "barcode": "075596066624",
            "artist-credit": [{"artist": {"id": "artist-1", "name": "Eagles"}}],
            "label-info": [
                {
                    "catalog-number": "SD 5068",
                    "label": {"name": "Asylum Records"},
                }
            ],
            "media": [{"format": "CD", "track-count": 11}],
        },
        {
            "id": "release-2",
            "title": "Desperado",
            "date": "1990",
            "country": "DE",
            "status": "Official",
            "barcode": "",
            "label-info": [],
            "media": [{"format": "CD", "track-count": 11}],
        },
    ]
}

FETCH_RESPONSE = {
    "id": "release-1",
    "title": "Desperado",
    "date": "1973-04-17",
    "country": "US",
    "status": "Official",
    "barcode": "075596066624",
    "asin": "B000002GXF",
    "text-representation": {"script": "Latn"},
    "label-info": [
        {
            "catalog-number": "SD 5068",
            "label": {"name": "Asylum Records"},
        }
    ],
    "artist-credit": [{"artist": {"id": "artist-1", "name": "Eagles"}}],
    "release-group": {
        "id": "rg-1",
        "primary-type": "Album",
        "secondary-types": [],
        "first-release-date": "1973-04-17",
    },
    "media": [
        {
            "position": 1,
            "format": "CD",
            "tracks": [
                {
                    "id": "track-id-1",
                    "number": "1",
                    "title": "Doolin-Dalton",
                    "length": 209000,
                    "artist-credit": [{"artist": {"id": "artist-1", "name": "Eagles"}}],
                    "recording": {
                        "id": "rec-id-1",
                        "title": "Doolin-Dalton",
                        "length": 209000,
                        "isrcs": ["USEE10400287"],
                        "relations": [
                            {
                                "type": "producer",
                                "target-type": "artist",
                                "artist": {"id": "prod-1", "name": "Glyn Johns"},
                            },
                            {
                                "type": "vocal",
                                "target-type": "artist",
                                "attributes": ["lead vocals"],
                                "artist": {"id": "artist-1", "name": "Glenn Frey"},
                            },
                            {
                                "type": "instrument",
                                "target-type": "artist",
                                "attributes": ["guitar"],
                                "artist": {"id": "artist-1", "name": "Glenn Frey"},
                            },
                            {
                                "type": "performance",
                                "target-type": "work",
                                "work": {
                                    "id": "work-id-1",
                                    "title": "Doolin-Dalton",
                                    "relations": [
                                        {
                                            "type": "composer",
                                            "target-type": "artist",
                                            "artist": {"id": "comp-1", "name": "Glenn Frey"},
                                        },
                                        {
                                            "type": "lyricist",
                                            "target-type": "artist",
                                            "artist": {"id": "comp-2", "name": "J.D. Souther"},
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                },
                {
                    "id": "track-id-2",
                    "number": "2",
                    "title": "Twenty-One",
                    "length": 187000,
                    "recording": {
                        "id": "rec-id-2",
                        "title": "Twenty-One",
                        "length": 187000,
                    },
                },
            ],
        }
    ],
}


class TestSearchReleases:
    def test_parses_results(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = SEARCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.search_releases("Eagles", "Desperado")

        assert len(releases) == 2
        assert releases[0].id == "release-1"
        assert releases[0].title == "Desperado"
        assert releases[0].artist == "Eagles"
        assert releases[0].country == "US"
        assert releases[0].label == "Asylum Records"
        assert releases[0].catalognum == "SD 5068"
        assert releases[0].barcode == "075596066624"
        assert releases[0].track_count == 11

    def test_second_release(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = SEARCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.search_releases("Eagles", "Desperado")

        assert releases[1].id == "release-2"
        assert releases[1].artist == ""
        assert releases[1].country == "DE"
        assert releases[1].label == ""

    def test_empty_results(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"releases": []}
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.search_releases("Nobody", "Nothing")
        assert releases == []


class TestFetchRelease:
    def test_parses_tracks(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = FETCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        release = client.fetch_release("release-1")

        assert release.id == "release-1"
        assert release.title == "Desperado"
        assert release.artist == "Eagles"
        assert release.artist_id == "artist-1"
        assert release.release_group_id == "rg-1"
        assert release.track_count == 2
        assert 1 in release.discs
        assert len(release.discs[1]) == 2
        assert release.discs[1][0].title == "Doolin-Dalton"
        assert release.discs[1][0].artist == "Eagles"
        assert release.discs[1][0].duration_ms == 209000
        assert release.discs[1][1].title == "Twenty-One"
        assert release.discs[1][1].artist == ""

    def test_follows_official_canonical_release_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("music_tagger.musicbrainz.RATE_LIMIT_SECS", 0)
        requested_id = "f7890790-ff4b-4524-9298-5c3bc66af976"
        resolved_id = "e6b4a290-2e81-4563-aed9-5e1af43d220d"
        redirect_location = f"https://musicbrainz.org/ws/2/release/{resolved_id}"
        first_url = f"https://musicbrainz.org/ws/2/release/{requested_id}"
        redirect = httpx.Response(
            301,
            headers={"Location": redirect_location},
            request=httpx.Request("GET", first_url),
        )
        response_data = deepcopy(FETCH_RESPONSE)
        response_data["id"] = resolved_id
        redirected_url = (
            f"{redirect_location}?fmt=json&inc=recordings%2Bartist-credits%2Blabels%2Brelease-groups"
            "%2Bisrcs%2Brecording-level-rels%2Bwork-level-rels%2Bwork-rels%2Bartist-rels"
        )
        canonical = httpx.Response(
            200,
            json=response_data,
            request=httpx.Request("GET", redirected_url),
        )
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.side_effect = [_response(503), redirect, _response(503), canonical]

        with patch("music_tagger.musicbrainz.time.sleep"):
            release = client.fetch_release(requested_id)

        assert release.id == resolved_id
        assert client.last_release_redirect == {
            "requested_release_id": requested_id,
            "resolved_release_id": resolved_id,
            "url": redirected_url,
        }
        assert client._client.get.call_count == 4
        assert client.last_request_retry_count == 1
        assert client.last_release_retry_count == 2
        assert client.retry_count == 2
        redirected_request = client._client.get.call_args_list[3].args[0]
        assert urlparse(redirected_request).path == urlparse(redirect_location).path
        assert parse_qs(urlparse(redirected_request).query) == {
            "fmt": ["json"],
            "inc": [
                "recordings+artist-credits+labels+release-groups+isrcs+recording-level-rels"
                "+work-level-rels+work-rels+artist-rels"
            ],
        }

    def test_redirect_sends_requested_includes_through_httpx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("music_tagger.musicbrainz.RATE_LIMIT_SECS", 0)
        requested_id = "f7890790-ff4b-4524-9298-5c3bc66af976"
        resolved_id = "e6b4a290-2e81-4563-aed9-5e1af43d220d"
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith(requested_id):
                return httpx.Response(301, headers={"Location": f"/ws/2/release/{resolved_id}"})
            assert request.url.params["fmt"] == "json"
            includes = set(request.url.params["inc"].split("+"))
            assert {"recordings", "isrcs", "artist-rels", "work-rels"} <= includes
            response_data = deepcopy(FETCH_RESPONSE)
            response_data["id"] = resolved_id
            return httpx.Response(200, json=response_data)

        client = MusicBrainzClient()
        client._client.close()
        client._client = httpx.Client(
            base_url="https://musicbrainz.org/ws/2", transport=httpx.MockTransport(respond)
        )
        try:
            release = client.fetch_release(requested_id)
        finally:
            client.close()

        assert len(requests) == 2
        assert release.id == resolved_id
        assert release.track_count == 2

    def test_rejects_release_redirect_to_another_host(self) -> None:
        requested_id = "f7890790-ff4b-4524-9298-5c3bc66af976"
        redirect_url = "https://example.org/ws/2/release/e6b4a290-2e81-4563-aed9-5e1af43d220d"
        response = httpx.Response(
            301,
            headers={"Location": redirect_url},
            request=httpx.Request("GET", f"https://musicbrainz.org/ws/2/release/{requested_id}"),
        )
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = response

        with pytest.raises(ValueError, match="unsupported release redirect"):
            client.fetch_release(requested_id)

        assert client._client.get.call_count == 1

    def test_track_duration_secs(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = FETCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        release = client.fetch_release("release-1")
        assert release.discs[1][0].duration_secs == 209.0

    def test_release_group_fields(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = FETCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        release = client.fetch_release("release-1")
        assert release.release_group_type == "Album"
        assert release.first_release_date == "1973-04-17"
        assert release.asin == "B000002GXF"
        assert release.script == "Latn"

    def test_track_ids_and_isrc(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = FETCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        release = client.fetch_release("release-1")
        track = release.discs[1][0]
        assert track.recording_id == "rec-id-1"
        assert track.track_id == "track-id-1"
        assert track.isrcs == ["USEE10400287"]

    @pytest.mark.parametrize(
        "isrcs",
        [
            [],
            ["USIR19902220"],
            ["USDW10110279", "USIR19902220"],
            ["USIR19902220", "USDW10110279"],
        ],
    )
    def test_retains_all_recording_isrcs(self, isrcs: list[str]) -> None:
        response = deepcopy(FETCH_RESPONSE)
        response["media"][0]["tracks"][0]["recording"]["isrcs"] = isrcs
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value.json.return_value = response

        release = client.fetch_release("release-1")

        assert release.discs[1][0].isrcs == isrcs

    def test_track_credits(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = FETCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        release = client.fetch_release("release-1")
        track = release.discs[1][0]
        assert track.producer == ("Glyn Johns",)
        assert track.producer_id == ("prod-1",)
        assert track.composer == ("Glenn Frey",)
        assert track.lyricist == ("J.D. Souther",)
        assert track.work_id == "work-id-1"
        assert "Glenn Frey (lead vocals, guitar)" in track.performers


class TestMBTrackSerialization:
    def test_round_trip(self) -> None:
        track = MBTrack(
            number=1,
            title="Song",
            duration_ms=200000,
            artist="The Band",
            artist_id="a1",
            recording_id="r1",
            track_id="t1",
            isrcs=["USIR19902220", "USDW10110279"],
            composer=("Bach",),
            composer_id="c1",
        )
        restored = MBTrack.from_dict(track.to_dict())
        assert restored.number == 1
        assert restored.title == "Song"
        assert restored.duration_ms == 200000
        assert restored.artist == "The Band"
        assert restored.artist_id == "a1"
        assert restored.composer == ("Bach",)
        assert restored.isrcs == ["USIR19902220", "USDW10110279"]
        assert track.to_dict()["isrcs"] == ["USIR19902220", "USDW10110279"]

    def test_rejects_legacy_single_isrc_that_may_hide_multiple_codes(self) -> None:
        with pytest.raises(ValueError, match="fetch the release again"):
            MBTrack.from_dict({"number": 1, "title": "Song", "isrc": "USIR19902220"})

    def test_prefers_explicit_list_over_legacy_isrc(self) -> None:
        track = MBTrack.from_dict(
            {"number": 1, "title": "Song", "isrc": "USIR19902220", "isrcs": []}
        )
        assert track.isrcs == []

    def test_omits_empty_fields(self) -> None:
        track = MBTrack(number=1, title="Song")
        d = track.to_dict()
        assert "artist_id" not in d
        assert "duration_ms" not in d

    def test_round_trip_no_duration(self) -> None:
        track = MBTrack(number=2, title="Interlude")
        restored = MBTrack.from_dict(track.to_dict())
        assert restored.duration_ms is None


class TestMBReleaseSerialization:
    def test_round_trip(self) -> None:
        release = MBRelease(
            id="rel-1",
            title="Album",
            date="2020",
            country="US",
            status="Official",
            label="Label",
            catalognum="CAT-1",
            barcode="123",
            format="CD",
            track_count=2,
            discs={1: [MBTrack(number=1, title="A"), MBTrack(number=2, title="B")]},
            artist="The Artist",
            artist_id="art-1",
            release_group_id="rg-1",
            release_group_type="Album",
            secondary_types=["Compilation"],
            first_release_date="2019",
            asin="B001",
            script="Latn",
        )
        restored = MBRelease.from_dict(release.to_dict())
        assert restored.id == "rel-1"
        assert restored.title == "Album"
        assert restored.artist == "The Artist"
        assert restored.track_count == 2
        assert len(restored.discs[1]) == 2
        assert restored.discs[1][0].title == "A"
        assert restored.secondary_types == ["Compilation"]
        assert restored.asin == "B001"

    def test_search_result_round_trip(self) -> None:
        release = MBRelease(
            id="rel-2",
            title="Album",
            date="2020",
            country="JP",
            track_count=10,
            format="CD",
        )
        d = release.to_dict()
        assert "discs" not in d
        restored = MBRelease.from_dict(d)
        assert restored.id == "rel-2"
        assert restored.discs == {}


class TestLookupDiscid:
    def test_returns_releases(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "releases": [
                {
                    "id": "release-disc",
                    "title": "Desperado",
                    "date": "1973",
                    "country": "US",
                    "status": "Official",
                    "barcode": "",
                    "label-info": [],
                    "media": [{"format": "CD", "track-count": 11}],
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.lookup_discid("test-discid")
        assert len(releases) == 1
        assert releases[0].id == "release-disc"
        assert releases[0].track_count == 11

    def test_not_found_returns_empty(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.lookup_discid("nonexistent")
        assert releases == []


def _medium(position: int, fmt: str, count: int) -> dict[str, Any]:
    """A medium with `count` numbered tracks, shaped like a MusicBrainz release's media."""
    return {
        "position": position,
        "format": fmt,
        "track-count": count,
        "tracks": [
            {
                "id": f"track-{position}-{n}",
                "number": str(n),
                "position": n,
                "title": f"Track {position}-{n}",
                "length": 200000,
                "recording": {"id": f"rec-{position}-{n}", "title": f"Track {position}-{n}"},
            }
            for n in range(1, count + 1)
        ],
    }


def _fetch_json(response: dict[str, Any]) -> MBRelease:
    mock_resp = MagicMock()
    mock_resp.json.return_value = response
    mock_resp.raise_for_status = MagicMock()

    client = MusicBrainzClient()
    client._client = MagicMock()
    client._client.get.return_value = mock_resp
    return client.fetch_release("release-1")


def _fetch_with_media(media: list[dict[str, Any]]) -> MBRelease:
    response = deepcopy(FETCH_RESPONSE)
    response["media"] = media
    return _fetch_json(response)


TWO_ARTISTS: list[dict[str, Any]] = [
    {"artist": {"id": "idA", "name": "Alice"}, "joinphrase": " feat. "},
    {"artist": {"id": "idB", "name": "Bob"}},
]


class TestMultiArtistCredits:
    def test_fetch_keeps_every_credited_artist_name_and_id(self) -> None:
        response = deepcopy(FETCH_RESPONSE)
        response["artist-credit"] = deepcopy(TWO_ARTISTS)
        medium = _medium(1, "CD", 2)
        medium["tracks"][0]["artist-credit"] = deepcopy(TWO_ARTISTS)
        response["media"] = [medium]

        release = _fetch_json(response)

        assert release.artist == "Alice feat. Bob"
        assert release.artist_credits == (("Alice", "idA"), ("Bob", "idB"))
        assert release.discs[1][0].artist_credits == (("Alice", "idA"), ("Bob", "idB"))


class TestMixedMedia:
    def test_unknown_medium_format_keeps_audio_tracks(self) -> None:
        medium = _medium(1, "CD", 2)
        medium["format"] = None

        release = _fetch_with_media([medium])
        search_release = MusicBrainzClient._parse_search_release(
            {"id": "unknown-format", "title": "Album", "media": [medium]}
        )

        assert release.track_count == search_release.track_count == 2
        assert len(release.discs[1]) == 2
        assert release.format == search_release.format == ""

    def test_fetch_counts_only_audio_media(self) -> None:
        release = _fetch_with_media([_medium(1, "CD", 12), _medium(2, "DVD-Video", 5)])

        assert release.track_count == 12
        assert set(release.discs) == {1}
        assert len(release.discs[1]) == 12
        assert release.format == "CD, DVD-Video"

    def test_disc_numbers_stay_medium_positions(self) -> None:
        release = _fetch_with_media([_medium(1, "DVD-Video", 5), _medium(2, "CD", 12)])

        assert release.track_count == 12
        assert set(release.discs) == {2}

    def test_fetch_with_only_non_audio_media_has_no_discs(self) -> None:
        release = _fetch_with_media([_medium(1, "DVD-Video", 5)])

        assert release.discs == {}
        assert release.track_count == 0
        assert release.format == "DVD-Video"

    def test_search_count_excludes_non_audio_media(self) -> None:
        release = MusicBrainzClient._parse_search_release(
            {
                "id": "release-cd-dvd",
                "title": "Album",
                "media": [
                    {"format": "CD", "track-count": 12},
                    {"format": "DVD-Video", "track-count": 5},
                ],
            }
        )

        assert release.track_count == 12
        assert release.format == "CD, DVD-Video"


class TestLookupToc:
    def test_returns_releases(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "releases": [
                {
                    "id": "release-toc",
                    "title": "Album",
                    "date": "2020",
                    "country": "US",
                    "status": "Official",
                    "barcode": "",
                    "label-info": [],
                    "media": [{"format": "CD", "track-count": 5}],
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.lookup_toc("1 3 200000 150 15000 30000")
        assert len(releases) == 1
        assert releases[0].id == "release-toc"

    def test_medium_with_null_format_is_counted_as_audio(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "releases": [
                {
                    "id": "release-null-format",
                    "title": "Album",
                    "date": "2020",
                    "country": "US",
                    "status": "Official",
                    "barcode": "",
                    "label-info": [],
                    "media": [{"format": None, "track-count": 5}],
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        releases = client.lookup_toc("1 3 200000 150 15000 30000")
        assert len(releases) == 1
        assert releases[0].track_count == 5


class TestBuildArtistCredit:
    def test_single_artist(self) -> None:
        credit = [{"artist": {"id": "a1", "name": "Eagles"}}]
        assert _build_artist_credit(credit) == "Eagles"

    def test_multi_artist_with_joinphrase(self) -> None:
        credit = [
            {"artist": {"id": "a1", "name": "Simon"}, "joinphrase": " & "},
            {"artist": {"id": "a2", "name": "Garfunkel"}},
        ]
        assert _build_artist_credit(credit) == "Simon & Garfunkel"

    def test_credited_name_overrides_artist_name(self) -> None:
        credit = [{"name": "Snoop Dogg", "artist": {"id": "a1", "name": "Snoop Lion"}}]
        assert _build_artist_credit(credit) == "Snoop Dogg"

    def test_empty_list(self) -> None:
        assert _build_artist_credit([]) == ""


class TestParseRecordingCredits:
    def test_merges_performer_attributes(self) -> None:
        recording = {
            "relations": [
                {
                    "type": "vocal",
                    "target-type": "artist",
                    "attributes": ["lead vocals"],
                    "artist": {"id": "a1", "name": "Singer"},
                },
                {
                    "type": "instrument",
                    "target-type": "artist",
                    "attributes": ["guitar"],
                    "artist": {"id": "a1", "name": "Singer"},
                },
            ]
        }
        credits = _parse_recording_credits(recording)
        assert credits["performers"] == "Singer (lead vocals, guitar)"
        assert credits["performer_ids"] == "a1"

    def test_deduplicates_composers(self) -> None:
        recording = {
            "relations": [
                {
                    "type": "performance",
                    "target-type": "work",
                    "work": {
                        "id": "w1",
                        "relations": [
                            {
                                "type": "composer",
                                "target-type": "artist",
                                "artist": {"id": "c1", "name": "Bach"},
                            },
                            {
                                "type": "composer",
                                "target-type": "artist",
                                "artist": {"id": "c1", "name": "Bach"},
                            },
                        ],
                    },
                }
            ]
        }
        credits = _parse_recording_credits(recording)
        assert credits["composer"] == ("Bach",)
        assert credits["composer_id"] == ("c1",)

    def test_role_credits_keep_one_value_per_person(self) -> None:
        recording = {
            "relations": [
                {
                    "type": "performance",
                    "target-type": "work",
                    "work": {
                        "id": "w1",
                        "relations": [
                            {
                                "type": "composer",
                                "target-type": "artist",
                                "artist": {"id": "c1", "name": "Bach"},
                            },
                            {
                                "type": "composer",
                                "target-type": "artist",
                                "artist": {"id": "c2", "name": "Handel"},
                            },
                        ],
                    },
                }
            ]
        }
        credits = _parse_recording_credits(recording)
        assert credits["composer"] == ("Bach", "Handel")
        assert credits["composer_id"] == ("c1", "c2")

    def test_empty_relations(self) -> None:
        credits = _parse_recording_credits({})
        assert credits == {}
        credits = _parse_recording_credits({"relations": []})
        assert credits == {}


def _response(status_code: int, payload: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://musicbrainz.org/ws/2/release/")
    return httpx.Response(status_code, json=payload or {"releases": []}, request=request)


class TestRetry:
    @pytest.mark.parametrize("status_code", [503, 429])
    def test_retries_then_succeeds(self, status_code: int) -> None:
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.side_effect = [_response(status_code), _response(200)]

        with patch("music_tagger.musicbrainz.time.sleep") as sleep:
            releases = client.search_releases("Eagles", "Desperado")

        assert releases == []
        assert client._client.get.call_count == 2
        assert any(call.args[0] >= RETRY_BACKOFF_SECS for call in sleep.call_args_list)

    def test_gives_up_after_max_attempts(self) -> None:
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = _response(503)

        with (
            patch("music_tagger.musicbrainz.time.sleep"),
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.fetch_release("r-1")

        assert client._client.get.call_count == MAX_ATTEMPTS

    def test_does_not_retry_client_errors(self) -> None:
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = _response(404)

        with (
            patch("music_tagger.musicbrainz.time.sleep"),
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.fetch_release("r-1")

        assert client._client.get.call_count == 1

    def test_concurrent_request_starts_share_one_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("music_tagger.musicbrainz.RATE_LIMIT_SECS", 0.02)
        client = MusicBrainzClient()
        started = []

        def record_start() -> None:
            client._rate_limit()
            started.append(time.monotonic())

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: record_start(), range(4)))

        ordered = sorted(started)
        assert all(later - earlier >= 0.015 for earlier, later in pairwise(ordered))
        client.close()

    def test_concurrent_retry_counts_are_request_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("music_tagger.musicbrainz.RATE_LIMIT_SECS", 0)
        barrier = Barrier(2)
        lock = Lock()
        calls: dict[str, int] = {}
        client = MusicBrainzClient()
        client._client = MagicMock()

        def fake_get(_path: str, *, params: dict[str, str]) -> httpx.Response:
            key = params["key"]
            with lock:
                calls[key] = calls.get(key, 0) + 1
                attempt = calls[key]
            if attempt == 1:
                barrier.wait(timeout=2)
            if key == "retry" and attempt == 1:
                return _response(503)
            return _response(200)

        client._client.get.side_effect = fake_get

        def get_retry_count(key: str) -> tuple[str, int]:
            client._get("/release/", {"key": key})
            return key, client.last_request_retry_count

        with patch("music_tagger.musicbrainz.time.sleep"), ThreadPoolExecutor(2) as executor:
            retry_counts = dict(executor.map(get_retry_count, ["retry", "clean"]))

        assert retry_counts == {"retry": 1, "clean": 0}
        assert client.retry_count == 1
        client.close()


class TestSearchQueryEscaping:
    def _query(self, artist: str, album: str) -> str:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"releases": []}
        mock_resp.raise_for_status = MagicMock()
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        client.search_releases(artist, album)

        return str(client._client.get.call_args.kwargs["params"]["query"])

    def test_quotes_in_the_album_are_escaped(self) -> None:
        assert r'release:"\"Heroes\""' in self._query("Bowie", '"Heroes"')

    def test_backslashes_in_the_album_are_escaped(self) -> None:
        assert r'release:"AC\\DC"' in self._query("AC/DC", r"AC\DC")

    def test_empty_artist_leaves_out_the_artist_clause(self) -> None:
        assert "artist:" not in self._query("  ", "Heroes")
