"""Searching MusicBrainz by barcode, alone or with artist and album."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_tagger.musicbrainz import MBRelease, MusicBrainzClient, barcode_variants


class TestBarcodeVariants:
    def test_ean_with_leading_zero_adds_upc(self) -> None:
        assert barcode_variants("0603497837465") == ["0603497837465", "603497837465"]

    def test_upc_adds_ean(self) -> None:
        assert barcode_variants("603497837465") == ["603497837465", "0603497837465"]

    def test_other_codes_are_kept_as_given(self) -> None:
        assert barcode_variants("4988005123456") == ["4988005123456"]
        assert barcode_variants("ABC") == ["ABC"]


class TestBarcodeQuery:
    def _client(self, payload: dict[str, object]) -> MusicBrainzClient:
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = response
        client._rate_limit = MagicMock()  # type: ignore[method-assign]
        return client

    def test_barcode_alone_is_a_query(self) -> None:
        client = self._client({"releases": []})
        client.search_releases("", "", barcode="0603497837465")
        query = client._client.get.call_args.kwargs["params"]["query"]
        assert query == "barcode:(0603497837465 OR 603497837465) AND format:CD"

    def test_barcode_narrows_a_title_search(self) -> None:
        client = self._client({"releases": []})
        client.search_releases("Little Feat", "Dixie Chicken", barcode="603497837465")
        query = client._client.get.call_args.kwargs["params"]["query"]
        assert query == (
            '(artist:"Little Feat" release:"Dixie Chicken") '
            "AND barcode:(603497837465 OR 0603497837465) AND format:CD"
        )

    def test_empty_search_is_refused(self) -> None:
        client = self._client({"releases": []})
        with pytest.raises(ValueError, match="album title or a barcode"):
            client.search_releases("", "")


class TestMbSearchBarcode:
    def test_barcode_alone_searches(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from music_tagger.cli.services import _mb_search

        out = tmp_path / "found.json"
        with patch("music_tagger.cli.services.MusicBrainzClient") as client_class:
            client_class.return_value.search_releases.return_value = [
                MBRelease(id="rel-1", title="Dixie Chicken")
            ]
            _mb_search(["--barcode", "603497837465", "-o", str(out)])
        client_class.return_value.search_releases.assert_called_once_with(
            "", "", format="CD", limit=10, barcode="603497837465"
        )
        assert "1 candidates for barcode 603497837465:" in capsys.readouterr().out

    def test_no_album_and_no_barcode_is_a_usage_error(self) -> None:
        from music_tagger.cli.services import _mb_search

        with pytest.raises(SystemExit) as error:
            _mb_search(["--artist", "Little Feat"])
        assert error.value.code == 2
