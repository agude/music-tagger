from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from music_tagger.coverart import ArtResult, fetch_cover_art


def _mock_response(status_code: int = 200, content: bytes = b"JPEGDATA") -> httpx.Response:
    request = httpx.Request("GET", "https://example.com")
    return httpx.Response(status_code=status_code, content=content, request=request)


class TestFetchCoverArt:
    def test_saves_image(self, tmp_path: Path) -> None:
        with patch("music_tagger.coverart.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _mock_response(content=b"FAKEJPEG" * 100)

            result = fetch_cover_art("release-123", tmp_path)

        assert result.saved is True
        assert result.path == tmp_path / "cover.jpg"
        assert result.path.exists()
        assert result.size_bytes == 800
        mock_client.get.assert_called_once()
        assert "front-500" in mock_client.get.call_args[0][0]

    def test_full_size(self, tmp_path: Path) -> None:
        with patch("music_tagger.coverart.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _mock_response()

            fetch_cover_art("release-123", tmp_path, full_size=True)

        url = mock_client.get.call_args[0][0]
        assert url.endswith("/front")
        assert "front-500" not in url

    def test_not_found(self, tmp_path: Path) -> None:
        resp = _mock_response(status_code=404)
        with patch("music_tagger.coverart.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = resp

            result = fetch_cover_art("release-123", tmp_path)

        assert result.saved is False
        assert result.not_found is True
        assert not (tmp_path / "cover.jpg").exists()

    def test_skips_existing(self, tmp_path: Path) -> None:
        existing = tmp_path / "cover.jpg"
        existing.write_bytes(b"existing")

        result = fetch_cover_art("release-123", tmp_path)

        assert result.saved is False
        assert result.skipped is True
        assert existing.read_bytes() == b"existing"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        existing = tmp_path / "cover.jpg"
        existing.write_bytes(b"old")

        with patch("music_tagger.coverart.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _mock_response(content=b"new image")

            result = fetch_cover_art("release-123", tmp_path, force=True)

        assert result.saved is True
        assert existing.read_bytes() == b"new image"

    def test_creates_directory(self, tmp_path: Path) -> None:
        dest = tmp_path / "nested" / "album"

        with patch("music_tagger.coverart.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _mock_response()

            result = fetch_cover_art("release-123", dest)

        assert result.saved is True
        assert dest.exists()


class TestArtCommand:
    def test_saves_art(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        with patch("music_tagger.cli.fetch_cover_art") as mock_fetch:
            mock_fetch.return_value = ArtResult(
                saved=True, path=tmp_path / "cover.jpg", size_bytes=50000,
            )
            _art([str(tmp_path), "--release-id", "abc-123"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Saved cover.jpg" in captured.out
        assert "49 KB" in captured.out

    def test_not_found(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        with patch("music_tagger.cli.fetch_cover_art") as mock_fetch:
            mock_fetch.return_value = ArtResult(saved=False, not_found=True)
            _art([str(tmp_path), "--release-id", "abc-123"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No cover art available" in captured.out

    def test_skipped(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        with patch("music_tagger.cli.fetch_cover_art") as mock_fetch:
            mock_fetch.return_value = ArtResult(
                saved=False, path=tmp_path / "cover.jpg", skipped=True,
            )
            _art([str(tmp_path), "--release-id", "abc-123"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "already exists" in captured.out
