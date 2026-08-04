from __future__ import annotations

from pathlib import Path

from music_tagger.genres import GENRE_LIST_PATH, load_canonical, unknown_genres

TABLE = """\
# Valid Genres

Some prose that should be ignored.

| Genre | Meaning |
|---|---|
| Folk Rock | Folk instrumentation with rock energy |
| Retro Rock | Classic rock era (60s-80s) |
| Y2K Rock | Late 90s / early 2000s rock |
"""


def _write_table(tmp_path: Path, body: str = TABLE) -> Path:
    target = tmp_path / "genre-list.md"
    target.write_text(body, encoding="utf-8")
    return target


class TestLoadCanonical:
    def test_parses_table_rows(self, tmp_path: Path) -> None:
        assert load_canonical(_write_table(tmp_path)) == {
            "Folk Rock",
            "Retro Rock",
            "Y2K Rock",
        }

    def test_skips_header_and_separator(self, tmp_path: Path) -> None:
        names = load_canonical(_write_table(tmp_path))
        assert "Genre" not in names
        assert not any(set(n) <= {"-"} for n in names)

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_canonical(tmp_path / "absent.md") == frozenset()

    def test_real_list_is_the_source_of_truth(self) -> None:
        """The shipped reference must parse, or validation silently no-ops."""
        names = load_canonical()
        assert GENRE_LIST_PATH.is_file()
        assert {"Retro Rock", "Folk Rock", "Y2K Rock", "DNI"} <= names


class TestUnknownGenres:
    def test_all_known(self, tmp_path: Path) -> None:
        assert unknown_genres(["Folk Rock", "Y2K Rock"], _write_table(tmp_path)) == []

    def test_reports_unknown(self, tmp_path: Path) -> None:
        unknown = unknown_genres(["Folk Rock", "Rock Retro Folk"], _write_table(tmp_path))
        assert unknown == ["Rock Retro Folk"]

    def test_is_case_sensitive(self, tmp_path: Path) -> None:
        assert unknown_genres(["folk rock"], _write_table(tmp_path)) == ["folk rock"]

    def test_missing_list_validates_everything(self, tmp_path: Path) -> None:
        assert unknown_genres(["Whatever"], tmp_path / "absent.md") == []
