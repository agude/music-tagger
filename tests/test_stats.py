"""Tests for the statistics snapshot contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from music_tagger.stats import (
    SNAPSHOT_VERSION,
    SnapshotError,
    SongStats,
    StatsSnapshot,
    parse_snapshot,
    read_snapshot,
)


def _envelope(songs: Any, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "sourceUser": "alex",
        "exportedAt": "2026-11-02T12:00:00Z",
        "songs": songs,
    }
    data.update(overrides)
    return data


def _song(**fields: Any) -> dict[str, Any]:
    return {"id": "s1", "path": "Artist/Album/01 Title.flac", **fields}


class TestRoundTrip:
    def test_preserves_explicit_zero_and_false(self) -> None:
        song = SongStats(id="s1", path="a.flac", rating=0, play_count=0, starred=False)

        data = song.to_dict()

        assert data["rating"] == 0
        assert data["playCount"] == 0
        assert data["starred"] is False
        assert SongStats.from_dict(data, "songs[0]") == song

    def test_missing_fields_stay_missing(self) -> None:
        song = SongStats(id="s1", path="a.flac")

        data = song.to_dict()

        for key in ("rating", "ratedAt", "starred", "starredAt", "playCount", "playDate"):
            assert key not in data
        assert SongStats.from_dict(data, "songs[0]") == song

    def test_native_omitted_values_export_as_explicit_zero_and_false(self) -> None:
        song = SongStats.from_native({"id": "s1", "path": "a.flac", "title": "A"})

        data = song.to_dict()

        assert data["rating"] == 0
        assert data["playCount"] == 0
        assert data["starred"] is False
        assert "ratedAt" not in data
        assert "playDate" not in data

    def test_native_present_values_map_to_snapshot_fields(self) -> None:
        song = SongStats.from_native(
            {
                "id": "s1",
                "path": "a.flac",
                "rating": 4,
                "ratedAt": "2026-06-25T22:10:03.50115421Z",
                "starred": True,
                "starredAt": "2026-06-25T22:10:05.077424375Z",
                "playCount": 18,
                "playDate": "2014-03-01T18:48:33Z",
            }
        )

        assert song.rating == 4
        assert song.rated_at == "2026-06-25T22:10:03.50115421Z"
        assert song.starred is True
        assert song.starred_at == "2026-06-25T22:10:05.077424375Z"
        assert song.play_count == 18
        assert song.play_date == "2014-03-01T18:48:33Z"

    def test_native_malformed_value_fails_snapshot_validation(self) -> None:
        song = SongStats.from_native({"id": "s1", "path": "a.flac", "rating": 9})

        with pytest.raises(SnapshotError, match="rating: 9 is outside"):
            parse_snapshot(_envelope([song.to_dict()]))

    def test_timestamps_round_trip_exactly(self) -> None:
        song = SongStats(
            id="s1",
            path="a.flac",
            rated_at="2026-06-25T22:10:03.50115421Z",
            starred=True,
            starred_at="2026-06-25T22:10:05.077424375Z",
            play_count=18,
            play_date="2014-03-01T18:48:33Z",
        )

        restored = SongStats.from_dict(song.to_dict(), "songs[0]")

        assert restored == song

    def test_null_timestamp_reads_as_unset(self) -> None:
        song = SongStats.from_dict(_song(ratedAt=None, playDate=None), "songs[0]")

        assert song.rated_at is None
        assert song.play_date is None

    def test_snapshot_envelope_round_trip(self) -> None:
        snapshot = StatsSnapshot(
            source_user="alex",
            exported_at="2026-11-02T12:00:00Z",
            songs=(
                SongStats(id="s1", path="a.flac", rating=5, play_count=0, starred=True),
                SongStats(id="s2", path="b.flac"),
            ),
        )

        assert parse_snapshot(snapshot.to_dict()) == snapshot


class TestLegacyInput:
    def test_legacy_ratings_list_is_refused_with_guidance(self) -> None:
        legacy = [{"id": "s1", "path": "a.flac", "rating": 4}]

        with pytest.raises(SnapshotError, match="legacy ratings file"):
            parse_snapshot(legacy)


class TestRejectsInvalidInput:
    @pytest.mark.parametrize(
        ("data", "message"),
        [
            (_envelope([], version=2), "unsupported snapshot version 2"),
            (_envelope([], version=True), "unsupported snapshot version True"),
            ({"sourceUser": "a", "exportedAt": "2026-11-02T12:00:00Z", "songs": []}, "version"),
            (_envelope([], sourceUser=""), "snapshot.sourceUser: must not be empty"),
            (_envelope([], exportedAt="2026-11-02T12:00:00+02:00"), "ending in Z"),
            (_envelope([], exportedAt="yesterday"), "ending in Z"),
            (_envelope({"id": "s1"}), "snapshot.songs: expected a list"),
            (_envelope(["not an object"]), r"songs\[0\]: expected an object"),
            (_envelope([_song(id="")]), r"songs\[0\].id: must not be empty"),
            (_envelope([{"path": "a.flac"}]), r"songs\[0\].id: missing"),
            (_envelope([_song(path="")]), r"songs\[0\].path: must not be empty"),
            (_envelope([_song(rating=6)]), r"songs\[0\].rating: 6 is outside 0..5"),
            (_envelope([_song(rating=-1)]), r"songs\[0\].rating: -1 is outside 0..5"),
            (_envelope([_song(rating=3.0)]), r"songs\[0\].rating: expected an integer"),
            (_envelope([_song(rating=True)]), r"songs\[0\].rating: expected an integer"),
            (_envelope([_song(rating=None)]), r"songs\[0\].rating: expected an integer"),
            (_envelope([_song(playCount=-1)]), r"songs\[0\].playCount: -1 is outside at least 0"),
            (_envelope([_song(playCount="3")]), r"songs\[0\].playCount: expected an integer"),
            (_envelope([_song(playCount=1.5)]), r"songs\[0\].playCount: expected an integer"),
            (_envelope([_song(playCount=False)]), r"songs\[0\].playCount: expected an integer"),
            (_envelope([_song(starred="true")]), r"songs\[0\].starred: expected true or false"),
            (_envelope([_song(starred=None)]), r"songs\[0\].starred: expected true or false"),
            (_envelope([_song(ratedAt="yesterday")]), r"songs\[0\].ratedAt: expected an RFC 3339"),
            (_envelope([_song(playDate=20260101)]), r"songs\[0\].playDate: expected an RFC 3339"),
            (
                _envelope([_song(id="s1"), _song(id="s1", path="b.flac")]),
                r"songs\[1\].id: duplicate song id 's1'",
            ),
        ],
    )
    def test_rejects_with_actionable_message(self, data: Any, message: str) -> None:
        with pytest.raises(SnapshotError, match=message):
            parse_snapshot(data)

    def test_rejects_non_object_document(self) -> None:
        with pytest.raises(SnapshotError, match="must be a JSON object"):
            parse_snapshot("text")


class TestReadSnapshot:
    def test_reads_valid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(_envelope([_song(rating=0, playCount=0)])))

        snapshot = read_snapshot(path)

        assert snapshot.songs[0].rating == 0
        assert snapshot.songs[0].play_count == 0

    def test_error_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(_envelope([_song(rating=9)])))

        with pytest.raises(SnapshotError, match=f"{path}: songs"):
            read_snapshot(path)

    def test_invalid_json_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        path.write_text("{not json")

        with pytest.raises(SnapshotError, match="not valid JSON"):
            read_snapshot(path)

    def test_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(SnapshotError, match="cannot read snapshot"):
            read_snapshot(tmp_path / "absent.json")
