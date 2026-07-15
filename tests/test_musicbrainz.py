from __future__ import annotations

from unittest.mock import MagicMock, patch

from music_tagger.musicbrainz import MBRelease, MBTrack, MusicBrainzClient, _build_artist_credit, _parse_recording_credits


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


class TestMBTrackSerialization:
    def test_round_trip(self) -> None:
        track = MBTrack(
            number=1, title="Song", duration_ms=200000,
            artist="The Band", artist_id="a1",
            recording_id="r1", track_id="t1",
            isrc="US1234", composer="Bach", composer_id="c1",
        )
        restored = MBTrack.from_dict(track.to_dict())
        assert restored.number == 1
        assert restored.title == "Song"
        assert restored.duration_ms == 200000
        assert restored.artist == "The Band"
        assert restored.artist_id == "a1"
        assert restored.composer == "Bach"

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
            id="rel-1", title="Album", date="2020", country="US",
            status="Official", label="Label", catalognum="CAT-1",
            barcode="123", format="CD", track_count=2,
            discs={1: [MBTrack(number=1, title="A"), MBTrack(number=2, title="B")]},
            artist="The Artist", artist_id="art-1", release_group_id="rg-1",
            release_group_type="Album", secondary_types=["Compilation"],
            first_release_date="2019", asin="B001", script="Latn",
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
            id="rel-2", title="Album", date="2020", country="JP",
            track_count=10, format="CD",
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
        assert credits["composer"] == "Bach"
        assert credits["composer_id"] == "c1"

    def test_empty_relations(self) -> None:
        credits = _parse_recording_credits({})
        assert credits == {}
        credits = _parse_recording_credits({"relations": []})
        assert credits == {}
