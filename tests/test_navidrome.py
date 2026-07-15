from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from music_tagger.navidrome import NavidromeClient, _auth_params


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
