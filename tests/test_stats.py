"""Tests for the statistics snapshot contract."""

from __future__ import annotations

import json
import os
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
    write_snapshot_file,
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

    @pytest.mark.parametrize("field", ["rating", "playCount"])
    @pytest.mark.parametrize("value", [False, 0.0, "", [], None, -1])
    def test_native_malformed_value_is_rejected(self, field: str, value: object) -> None:
        with pytest.raises(SnapshotError, match=field):
            SongStats.from_native({"id": "s1", "path": "a.flac", field: value})

    @pytest.mark.parametrize("field", ["title", "artist", "album", "ratedAt", "playDate"])
    def test_native_falsy_metadata_is_not_coerced(self, field: str) -> None:
        with pytest.raises(SnapshotError, match=field):
            SongStats.from_native({"id": "s1", "path": "a.flac", field: False})

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


def _snapshot(rating: int = 5) -> StatsSnapshot:
    return StatsSnapshot(
        source_user="alex",
        exported_at="2026-11-02T12:00:00Z",
        songs=(SongStats(id="s1", path="a.flac", rating=rating, play_count=0, starred=False),),
    )


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


class TestWriteSnapshotFile:
    def test_first_write_creates_snapshot_without_previous(self, tmp_path: Path) -> None:
        dest = tmp_path / "stats.json"

        write_snapshot_file(_snapshot(), dest)

        assert read_snapshot(dest) == _snapshot()
        assert _names(tmp_path) == ["stats.json"]

    def test_second_write_keeps_prior_snapshot_as_previous(self, tmp_path: Path) -> None:
        dest = tmp_path / "stats.json"
        write_snapshot_file(_snapshot(rating=2), dest)

        write_snapshot_file(_snapshot(rating=5), dest)

        assert read_snapshot(dest).songs[0].rating == 5
        assert read_snapshot(tmp_path / "stats.json.previous").songs[0].rating == 2
        assert _names(tmp_path) == ["stats.json", "stats.json.previous"]

    def test_invalid_snapshot_leaves_destination_untouched(self, tmp_path: Path) -> None:
        dest = tmp_path / "stats.json"
        write_snapshot_file(_snapshot(rating=2), dest)
        before = dest.read_bytes()
        invalid = _snapshot(rating=9)

        with pytest.raises(SnapshotError, match=r"rating: 9 is outside"):
            write_snapshot_file(invalid, dest)

        assert dest.read_bytes() == before
        assert _names(tmp_path) == ["stats.json"]

    def test_failed_rotation_leaves_current_snapshot_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "stats.json"
        write_snapshot_file(_snapshot(rating=2), dest)
        before = dest.read_bytes()
        real_replace = os.replace

        def fail_first(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            raise OSError("simulated rotation failure")

        monkeypatch.setattr(os, "replace", fail_first)
        with pytest.raises(OSError, match="simulated rotation failure"):
            write_snapshot_file(_snapshot(rating=5), dest)
        monkeypatch.setattr(os, "replace", real_replace)

        assert dest.read_bytes() == before
        assert _names(tmp_path) == ["stats.json"]

    def test_failed_final_rename_leaves_previous_snapshot_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "stats.json"
        write_snapshot_file(_snapshot(rating=2), dest)
        real_replace = os.replace
        calls: list[str] = []

        def fail_second(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            calls.append(str(dst))
            if len(calls) == 2:
                raise OSError("simulated final rename failure")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_second)
        with pytest.raises(OSError, match="simulated final rename failure"):
            write_snapshot_file(_snapshot(rating=5), dest)
        monkeypatch.setattr(os, "replace", real_replace)

        assert not dest.exists()
        assert read_snapshot(tmp_path / "stats.json.previous").songs[0].rating == 2
        assert _names(tmp_path) == ["stats.json.previous"]


def _library(total: int, rated: int = 0, starred: int = 0) -> StatsSnapshot:
    """A snapshot of `total` songs. The first `rated` are rated; the first `starred` are starred."""
    songs = tuple(
        SongStats(
            id=f"s{index}",
            path=f"{index}.flac",
            rating=3 if index < rated else 0,
            starred=index < starred,
        )
        for index in range(total)
    )
    return StatsSnapshot(
        source_user="alex",
        exported_at="2026-11-02T12:00:00Z",
        songs=songs,
    )


class TestDropGuard:
    @pytest.mark.parametrize(
        ("label", "current", "new"),
        [
            ("song", {"total": 10}, {"total": 8}),
            ("rated", {"total": 10, "rated": 10}, {"total": 10, "rated": 8}),
            ("starred", {"total": 10, "starred": 10}, {"total": 10, "starred": 8}),
        ],
    )
    def test_large_drop_is_refused_and_both_files_stay(
        self, tmp_path: Path, label: str, current: dict[str, int], new: dict[str, int]
    ) -> None:
        dest = tmp_path / "stats.json"
        previous = tmp_path / "stats.json.previous"
        write_snapshot_file(_library(**current), dest)
        write_snapshot_file(_library(**current), dest)
        before_dest, before_previous = dest.read_bytes(), previous.read_bytes()

        with pytest.raises(
            SnapshotError, match=rf"{label} count would fall from \d+ to \d+.*--force"
        ):
            write_snapshot_file(_library(**new), dest)

        assert dest.read_bytes() == before_dest
        assert previous.read_bytes() == before_previous
        assert _names(tmp_path) == ["stats.json", "stats.json.previous"]

    def test_drop_of_exactly_ten_percent_is_allowed(self, tmp_path: Path) -> None:
        dest = tmp_path / "stats.json"
        write_snapshot_file(_library(10, starred=10), dest)

        write_snapshot_file(_library(10, starred=9), dest)

        assert sum(song.starred is True for song in read_snapshot(dest).songs) == 9

    def test_force_replaces_a_large_drop_and_rotates(self, tmp_path: Path) -> None:
        dest = tmp_path / "stats.json"
        write_snapshot_file(_library(10, starred=10), dest)
        write_snapshot_file(_library(10, starred=10), dest)

        write_snapshot_file(_library(10, starred=2), dest, force=True)

        assert sum(song.starred is True for song in read_snapshot(dest).songs) == 2
        previous = read_snapshot(tmp_path / "stats.json.previous")
        assert sum(song.starred is True for song in previous.songs) == 10

    def test_corrupt_current_is_replaced_without_rotating_over_previous(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "stats.json"
        previous = tmp_path / "stats.json.previous"
        write_snapshot_file(_snapshot(rating=2), dest)
        write_snapshot_file(_snapshot(rating=5), dest)
        before_previous = previous.read_bytes()
        dest.write_text("{not json", encoding="utf-8")

        write_snapshot_file(_snapshot(rating=4), dest)

        assert read_snapshot(dest).songs[0].rating == 4
        assert previous.read_bytes() == before_previous
        assert _names(tmp_path) == ["stats.json", "stats.json.previous"]
