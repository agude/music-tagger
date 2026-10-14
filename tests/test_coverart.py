from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from music_tagger.coverart import ArtResult, fetch_cover_art, image_dimensions, use_local_cover


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
                saved=True,
                path=tmp_path / "cover.jpg",
                size_bytes=50000,
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
                saved=False,
                path=tmp_path / "cover.jpg",
                skipped=True,
            )
            _art([str(tmp_path), "--release-id", "abc-123"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "already exists" in captured.out

    def test_embed_calls_embed_cover_art(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        cover = tmp_path / "cover.jpg"
        with (
            patch("music_tagger.cli.fetch_cover_art") as mock_fetch,
            patch("music_tagger.cli.embed_cover_art") as mock_embed,
        ):
            mock_fetch.return_value = ArtResult(saved=True, path=cover, size_bytes=1024)
            mock_embed.return_value = 3
            _art([str(tmp_path), "--release-id", "abc-123", "--embed"])

        mock_embed.assert_called_once_with(tmp_path, cover)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Embedded cover art into 3 file(s)" in captured.out

    def test_embed_skipped_still_embeds(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        cover = tmp_path / "cover.jpg"
        with (
            patch("music_tagger.cli.fetch_cover_art") as mock_fetch,
            patch("music_tagger.cli.embed_cover_art") as mock_embed,
        ):
            mock_fetch.return_value = ArtResult(saved=False, path=cover, skipped=True)
            mock_embed.return_value = 2
            _art([str(tmp_path), "--release-id", "abc-123", "--embed"])

        mock_embed.assert_called_once_with(tmp_path, cover)

    def test_embed_not_found_skips_embed(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        with (
            patch("music_tagger.cli.fetch_cover_art") as mock_fetch,
            patch("music_tagger.cli.embed_cover_art") as mock_embed,
        ):
            mock_fetch.return_value = ArtResult(saved=False, not_found=True)
            _art([str(tmp_path), "--release-id", "abc-123", "--embed"])

        mock_embed.assert_not_called()


class TestUseLocalCover:
    def test_copies_file(self, tmp_path: Path) -> None:
        source = tmp_path / "input.jpg"
        source.write_bytes(b"LOCALCOVER")
        dest_dir = tmp_path / "album"
        dest_dir.mkdir()

        result = use_local_cover(source, dest_dir)

        assert result.saved is True
        assert result.path == dest_dir / "cover.jpg"
        assert result.path.read_bytes() == b"LOCALCOVER"
        assert result.size_bytes == 10

    def test_skips_existing(self, tmp_path: Path) -> None:
        source = tmp_path / "input.jpg"
        source.write_bytes(b"NEW")
        dest_dir = tmp_path / "album"
        dest_dir.mkdir()
        existing = dest_dir / "cover.jpg"
        existing.write_bytes(b"OLD")

        result = use_local_cover(source, dest_dir)

        assert result.saved is False
        assert result.skipped is True
        assert existing.read_bytes() == b"OLD"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        source = tmp_path / "input.jpg"
        source.write_bytes(b"NEW")
        dest_dir = tmp_path / "album"
        dest_dir.mkdir()
        existing = dest_dir / "cover.jpg"
        existing.write_bytes(b"OLD")

        result = use_local_cover(source, dest_dir, force=True)

        assert result.saved is True
        assert existing.read_bytes() == b"NEW"

    def test_source_not_found(self, tmp_path: Path) -> None:
        source = tmp_path / "nonexistent.jpg"
        dest_dir = tmp_path / "album"
        dest_dir.mkdir()

        result = use_local_cover(source, dest_dir)

        assert result.saved is False
        assert result.not_found is True

    def test_creates_dest_directory(self, tmp_path: Path) -> None:
        source = tmp_path / "input.jpg"
        source.write_bytes(b"DATA")
        dest_dir = tmp_path / "nested" / "album"

        result = use_local_cover(source, dest_dir)

        assert result.saved is True
        assert dest_dir.exists()


class TestArtCommandCoverFile:
    def test_cover_file_uses_local(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        source = tmp_path / "my_cover.jpg"
        source.write_bytes(b"BIGIMAGE" * 1000)

        with patch("music_tagger.cli.use_local_cover") as mock_local:
            mock_local.return_value = ArtResult(
                saved=True,
                path=tmp_path / "cover.jpg",
                size_bytes=8000,
            )
            _art([str(tmp_path), "--cover-file", str(source)])

        mock_local.assert_called_once()
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Saved cover.jpg" in captured.out

    def test_cover_file_not_found(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        with patch("music_tagger.cli.use_local_cover") as mock_local:
            mock_local.return_value = ArtResult(saved=False, not_found=True)
            _art([str(tmp_path), "--cover-file", "/nonexistent.jpg"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Cover file not found" in captured.out

    def test_no_release_id_or_cover_file_errors(self, tmp_path: Path) -> None:
        import pytest

        from music_tagger.cli import _art

        with pytest.raises(SystemExit):
            _art([str(tmp_path)])

    def test_cover_file_with_embed(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _art

        source = tmp_path / "my_cover.jpg"
        source.write_bytes(b"DATA")
        cover = tmp_path / "cover.jpg"

        with (
            patch("music_tagger.cli.use_local_cover") as mock_local,
            patch("music_tagger.cli.embed_cover_art") as mock_embed,
        ):
            mock_local.return_value = ArtResult(saved=True, path=cover, size_bytes=4)
            mock_embed.return_value = 5
            _art([str(tmp_path), "--cover-file", str(source), "--embed"])

        mock_embed.assert_called_once_with(tmp_path, cover)


def _jpeg(width: int, height: int) -> bytes:
    """A JPEG header with an APP0 segment before the SOF0 frame header."""
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    sof0 = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\x00" * 9
    )
    return b"\xff\xd8" + app0 + sof0


def _png(width: int, height: int) -> bytes:
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr


class TestImageDimensions:
    def test_jpeg(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jpg"
        path.write_bytes(_jpeg(1400, 1200))

        assert image_dimensions(path) == (1400, 1200)

    def test_png(self, tmp_path: Path) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(_png(600, 500))

        assert image_dimensions(path) == (600, 500)

    def test_unknown_format(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jpg"
        path.write_bytes(b"not an image")

        assert image_dimensions(path) is None


class TestArtPreview:
    def _preview(self, album_dir: Path, content: bytes | None) -> None:
        from music_tagger.cli import _art

        def fake_fetch(release_id: str, dest_dir: Path, **_: object) -> ArtResult:
            if content is None:
                return ArtResult(saved=False, not_found=True)
            dest = dest_dir / "cover.jpg"
            dest.write_bytes(content)
            return ArtResult(saved=True, path=dest, size_bytes=len(content))

        with patch("music_tagger.cli.fetch_cover_art", side_effect=fake_fetch) as fetch:
            _art([str(album_dir), "--release-id", "r-1", "--preview"])
        assert fetch.call_args.kwargs["full_size"] is True

    def test_downloads_outside_album_and_reports_dimensions(
        self, flac_album: Path, default_log: Path, capsys: object
    ) -> None:
        before = sorted(path.name for path in flac_album.iterdir())

        self._preview(flac_album, _jpeg(1400, 1200))

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        candidate = Path(out.split("Candidate: ")[1].split(" (")[0])
        assert candidate.read_bytes() == _jpeg(1400, 1200)
        assert flac_album not in candidate.parents
        assert "1400x1200" in out
        assert "Current: none" in out
        assert sorted(path.name for path in flac_album.iterdir()) == before
        assert not default_log.exists()

    def test_reports_current_cover(self, flac_album: Path, capsys: object) -> None:
        (flac_album / "cover.jpg").write_bytes(_jpeg(500, 500))

        self._preview(flac_album, _jpeg(1400, 1200))

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert f"Current: {flac_album / 'cover.jpg'} (500x500" in out

    def test_not_found_exits_nonzero(self, flac_album: Path, capsys: object) -> None:
        import pytest

        with pytest.raises(SystemExit) as error:
            self._preview(flac_album, None)

        assert error.value.code == 1
        assert "No cover art available" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_rejects_cover_file(self, flac_album: Path, tmp_path: Path) -> None:
        import pytest

        from music_tagger.cli import _art

        with pytest.raises(SystemExit) as error:
            _art([str(flac_album), "--cover-file", str(tmp_path / "a.jpg"), "--preview"])

        assert error.value.code == 2
