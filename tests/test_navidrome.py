from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from music_tagger.navidrome import NavidromeClient, SongRating, _auth_params


class TestAuthParams:
    def test_has_required_keys(self) -> None:
        params = _auth_params("admin", "secret")
        assert params["u"] == "admin"
        assert "t" in params
        assert "s" in params
        assert params["f"] == "json"
        assert params["c"] == "music-tagger"

    def test_token_is_md5(self) -> None:
        import hashlib

        params = _auth_params("admin", "secret")
        expected = hashlib.md5(("secret" + params["s"]).encode()).hexdigest()
        assert params["t"] == expected


class TestNavidromeClient:
    def test_start_scan_success(self) -> None:
        client = NavidromeClient(url="http://localhost:4533", user="admin", password="secret")
        response_data = {
            "subsonic-response": {
                "status": "ok",
                "scanStatus": {"scanning": True, "count": 42},
            }
        }
        request = httpx.Request("GET", "http://localhost:4533/rest/startScan")
        mock_resp = httpx.Response(200, json=response_data, request=request)

        with patch.object(client._client, "get", return_value=mock_resp):
            status = client.start_scan()

        assert status["scanning"] is True
        assert status["count"] == 42

    def test_start_scan_error(self) -> None:
        client = NavidromeClient(url="http://localhost:4533", user="admin", password="secret")
        response_data = {
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 40, "message": "Wrong credentials"},
            }
        }
        request = httpx.Request("GET", "http://localhost:4533/rest/startScan")
        mock_resp = httpx.Response(200, json=response_data, request=request)

        with (
            patch.object(client._client, "get", return_value=mock_resp),
            pytest.raises(RuntimeError, match="Wrong credentials"),
        ):
            client.start_scan()

    def test_missing_env_vars(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(RuntimeError, match="NAVIDROME_URL"),
        ):
            NavidromeClient()


class TestSongRating:
    def test_from_native_full(self) -> None:
        data = {
            "id": "abc123",
            "path": "Eagles/Hotel California (1977)/01. Hotel California.flac",
            "title": "Hotel California",
            "artist": "Eagles",
            "album": "Hotel California",
            "rating": 5,
            "starredAt": "2024-01-15T10:30:00Z",
        }
        song = SongRating.from_native(data)
        assert song.rating == 5
        assert song.starred == "2024-01-15T10:30:00Z"

    def test_from_native_no_starred(self) -> None:
        data = {
            "id": "x",
            "path": "p",
            "title": "t",
            "artist": "a",
            "album": "al",
            "rating": 3,
        }
        song = SongRating.from_native(data)
        assert song.rating == 3
        assert song.starred == ""

    def test_to_dict_omits_zero_rating_and_empty_starred(self) -> None:
        song = SongRating(id="x", path="p", title="t", artist="a", album="al", rating=0, starred="")
        d = song.to_dict()
        assert "rating" not in d
        assert "starred" not in d

    def test_to_dict_includes_rating_and_starred(self) -> None:
        song = SongRating(
            id="x", path="p", title="t", artist="a", album="al", rating=3, starred="2024-01-01"
        )
        d = song.to_dict()
        assert d["rating"] == 3
        assert d["starred"] == "2024-01-01"

    def test_roundtrip(self) -> None:
        song = SongRating(
            id="x", path="p", title="t", artist="a", album="al", rating=4, starred="2024-01-01"
        )
        restored = SongRating.from_dict(song.to_dict())
        assert restored == song

    def test_from_dict_missing_optional(self) -> None:
        d = {"id": "x", "path": "p", "title": "t", "artist": "a", "album": "al"}
        song = SongRating.from_dict(d)
        assert song.rating == 0
        assert song.starred == ""


class TestGetAllRatings:
    def _make_client(self) -> NavidromeClient:
        return NavidromeClient(url="http://localhost:4533", user="admin", password="secret")

    def _mock_login(self) -> httpx.Response:
        request = httpx.Request("POST", "http://localhost:4533/auth/login")
        return httpx.Response(200, json={"token": "fake-jwt"}, request=request)

    def _mock_song_page(self, songs: list[dict]) -> httpx.Response:  # type: ignore[type-arg]
        request = httpx.Request("GET", "http://localhost:4533/api/song")
        return httpx.Response(200, json=songs, request=request)

    def test_returns_rated_and_starred(self) -> None:
        client = self._make_client()
        songs = [
            {
                "id": "s1",
                "path": "a.flac",
                "title": "A",
                "artist": "X",
                "album": "Y",
                "rating": 5,
                "starredAt": "2024-01-01",
            },
            {"id": "s2", "path": "b.flac", "title": "B", "artist": "X", "album": "Y", "rating": 0},
            {"id": "s3", "path": "c.flac", "title": "C", "artist": "X", "album": "Y", "rating": 3},
        ]
        with (
            patch.object(client._client, "post", return_value=self._mock_login()),
            patch.object(client._client, "get", return_value=self._mock_song_page(songs)),
        ):
            result = client.get_all_ratings(page_size=500)

        assert len(result) == 2
        assert result[0].id == "s1"
        assert result[0].rating == 5
        assert result[0].starred == "2024-01-01"
        assert result[1].id == "s3"

    def test_paginates(self) -> None:
        client = self._make_client()
        s1 = {"id": "s1", "path": "a.flac", "title": "A", "artist": "X", "album": "Y", "rating": 4}
        s2 = {"id": "s2", "path": "b.flac", "title": "B", "artist": "X", "album": "Y", "rating": 2}
        page1 = [s1]
        page2 = [s2]
        responses = [
            self._mock_song_page(page1),
            self._mock_song_page(page2),
            self._mock_song_page([]),
        ]
        with (
            patch.object(client._client, "post", return_value=self._mock_login()),
            patch.object(client._client, "get", side_effect=responses),
        ):
            result = client.get_all_ratings(page_size=1)

        assert len(result) == 2

    def test_stops_on_short_page(self) -> None:
        client = self._make_client()
        page = [
            {"id": "s1", "path": "a.flac", "title": "A", "artist": "X", "album": "Y", "rating": 3},
            {"id": "s2", "path": "b.flac", "title": "B", "artist": "X", "album": "Y", "rating": 0},
        ]
        with (
            patch.object(client._client, "post", return_value=self._mock_login()),
            patch.object(client._client, "get", return_value=self._mock_song_page(page)),
        ):
            result = client.get_all_ratings(page_size=500)

        assert len(result) == 1

    def test_empty_library(self) -> None:
        client = self._make_client()
        with (
            patch.object(client._client, "post", return_value=self._mock_login()),
            patch.object(client._client, "get", return_value=self._mock_song_page([])),
        ):
            result = client.get_all_ratings()

        assert result == []


class TestSetRating:
    def test_set_rating(self) -> None:
        client = NavidromeClient(url="http://localhost:4533", user="admin", password="secret")
        response_data = {"subsonic-response": {"status": "ok"}}
        request = httpx.Request("GET", "http://localhost:4533/rest/setRating")
        mock_resp = httpx.Response(200, json=response_data, request=request)

        with patch.object(client._client, "get", return_value=mock_resp) as mock_get:
            client.set_rating("song1", 4)

        args, kwargs = mock_get.call_args
        assert "setRating" in args[0]
        assert kwargs["params"]["id"] == "song1"
        assert kwargs["params"]["rating"] == "4"

    def test_star(self) -> None:
        client = NavidromeClient(url="http://localhost:4533", user="admin", password="secret")
        response_data = {"subsonic-response": {"status": "ok"}}
        request = httpx.Request("GET", "http://localhost:4533/rest/star")
        mock_resp = httpx.Response(200, json=response_data, request=request)

        with patch.object(client._client, "get", return_value=mock_resp) as mock_get:
            client.star("song1")

        args, _ = mock_get.call_args
        assert "star" in args[0]

    def test_unstar(self) -> None:
        client = NavidromeClient(url="http://localhost:4533", user="admin", password="secret")
        response_data = {"subsonic-response": {"status": "ok"}}
        request = httpx.Request("GET", "http://localhost:4533/rest/unstar")
        mock_resp = httpx.Response(200, json=response_data, request=request)

        with patch.object(client._client, "get", return_value=mock_resp) as mock_get:
            client.unstar("song1")

        args, _ = mock_get.call_args
        assert "unstar" in args[0]


class TestNdRescanCommand:
    def test_scan_started(self, capsys: object) -> None:
        from music_tagger.cli import _nd_rescan

        with patch("music_tagger.cli.NavidromeClient") as mock_cls:
            mock_cls.return_value.start_scan.return_value = {"scanning": True, "count": 0}
            _nd_rescan([])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Scan started" in captured.out

    def test_scan_complete(self, capsys: object) -> None:
        from music_tagger.cli import _nd_rescan

        with patch("music_tagger.cli.NavidromeClient") as mock_cls:
            mock_cls.return_value.start_scan.return_value = {"scanning": False, "count": 5000}
            _nd_rescan([])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Scan complete" in captured.out
        assert "5000" in captured.out


class TestNdRatingsCommand:
    def _make_songs(self) -> list[SongRating]:
        return [
            SongRating(
                id="s1",
                path="A/B/01.flac",
                title="Song1",
                artist="Art1",
                album="Alb1",
                rating=5,
                starred="2024-01-01T00:00:00Z",
            ),
            SongRating(
                id="s2",
                path="A/B/02.flac",
                title="Song2",
                artist="Art1",
                album="Alb1",
                rating=0,
                starred="2024-02-01T00:00:00Z",
            ),
        ]

    def test_out_file(self, tmp_path: object, capsys: object) -> None:
        from pathlib import Path

        from music_tagger.cli import _nd_ratings

        out = Path(str(tmp_path)) / "ratings.json"
        songs = self._make_songs()
        with patch("music_tagger.cli.NavidromeClient") as mock_cls:
            mock_cls.return_value.get_all_ratings.return_value = songs
            _nd_ratings(["-o", str(out)])

        data = json.loads(out.read_text())
        assert len(data) == 2
        assert data[0]["id"] == "s1"
        assert data[0]["rating"] == 5

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "2 song(s)" in captured.out
        assert "1 rated" in captured.out
        assert "2 starred" in captured.out

    def test_empty(self, tmp_path: object, capsys: object) -> None:
        from pathlib import Path

        from music_tagger.cli import _nd_ratings

        out = Path(str(tmp_path)) / "ratings.json"
        with patch("music_tagger.cli.NavidromeClient") as mock_cls:
            mock_cls.return_value.get_all_ratings.return_value = []
            _nd_ratings(["-o", str(out)])

        data = json.loads(out.read_text())
        assert data == []


class TestNdSetRatingCommand:
    def test_dry_run(self, tmp_path: object, capsys: object) -> None:
        from pathlib import Path

        from music_tagger.cli import _nd_set_rating

        ratings = [
            {"id": "s1", "path": "p", "title": "T1", "artist": "A", "album": "Al", "rating": 4},
            {
                "id": "s2",
                "path": "p",
                "title": "T2",
                "artist": "A",
                "album": "Al",
                "starred": "2024-01-01",
            },
        ]
        f = Path(str(tmp_path)) / "ratings.json"
        f.write_text(json.dumps(ratings))

        _nd_set_rating(["--from", str(f), "--dry-run"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "rating=4" in captured.out
        assert "starred" in captured.out
        assert "dry run" in captured.out

    def test_applies_ratings(self, tmp_path: object, capsys: object) -> None:
        from pathlib import Path

        from music_tagger.cli import _nd_set_rating

        ratings = [
            {
                "id": "s1",
                "path": "p",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 3,
                "starred": "2024-01-01",
            },
        ]
        f = Path(str(tmp_path)) / "ratings.json"
        f.write_text(json.dumps(ratings))

        with patch("music_tagger.cli.NavidromeClient") as mock_cls:
            mock_client = mock_cls.return_value
            _nd_set_rating(["--from", str(f)])

        mock_client.set_rating.assert_called_once_with("s1", 3)
        mock_client.star.assert_called_once_with("s1")

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 rating" in captured.out
        assert "1 star" in captured.out

    def test_skips_missing_id(self, tmp_path: object, capsys: object) -> None:
        from pathlib import Path

        from music_tagger.cli import _nd_set_rating

        ratings = [
            {"id": "", "path": "p", "title": "T1", "artist": "A", "album": "Al", "rating": 3},
        ]
        f = Path(str(tmp_path)) / "ratings.json"
        f.write_text(json.dumps(ratings))

        with patch("music_tagger.cli.NavidromeClient") as mock_cls:
            mock_client = mock_cls.return_value
            _nd_set_rating(["--from", str(f)])

        mock_client.set_rating.assert_not_called()

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "skipped" in captured.err
