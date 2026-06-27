from __future__ import annotations

from unittest.mock import MagicMock, patch

from music_tagger.musicbrainz import MusicBrainzClient, _parse_recording_credits


SEARCH_RESPONSE = {
    "releases": [
        {
            "id": "release-1",
            "title": "Desperado",
            "date": "1973-04-17",
            "country": "US",
            "status": "Official",
            "barcode": "075596066624",
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
        assert release.artist_id == "artist-1"
        assert release.release_group_id == "rg-1"
        assert release.track_count == 2
        assert 1 in release.discs
        assert len(release.discs[1]) == 2
        assert release.discs[1][0].title == "Doolin-Dalton"
        assert release.discs[1][0].duration_ms == 209000
        assert release.discs[1][1].title == "Twenty-One"

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
        assert track.isrc == "USEE10400287"

    def test_track_credits(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = FETCH_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        release = client.fetch_release("release-1")
        track = release.discs[1][0]
        assert track.producer == "Glyn Johns"
        assert track.producer_id == "prod-1"
        assert track.composer == "Glenn Frey"
        assert track.lyricist == "J.D. Souther"
        assert track.work_id == "work-id-1"
        assert "Glenn Frey (lead vocals, guitar)" in track.performers


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
        assert credits["composer"] == "Bach"
        assert credits["composer_id"] == "c1"

    def test_empty_relations(self) -> None:
        credits = _parse_recording_credits({})
        assert credits == {}
        credits = _parse_recording_credits({"relations": []})
        assert credits == {}
