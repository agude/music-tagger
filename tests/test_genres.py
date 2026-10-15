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


FIXTURE_ALBUM = Path(__file__).parent / "fixtures" / "album_flac"


def _library(tmp_path: Path, albums: dict[str, list[list[str]]]) -> Path:
    """Build root/<artist>/<album>/ with one FLAC per genre list."""
    import shutil

    from music_tagger.tags import write_genres

    root = tmp_path / "library"
    for album_path, track_genres in albums.items():
        album_dir = root / album_path
        album_dir.mkdir(parents=True)
        for number, genres in enumerate(track_genres, 1):
            track = album_dir / f"{number:02d}. Track.flac"
            shutil.copy(FIXTURE_ALBUM / "01 - Track 1.flac", track)
            write_genres(track, genres)
    return root


class TestFindArtistDirs:
    def test_ignores_case_leading_the_and_punctuation(self, tmp_path: Path) -> None:
        from music_tagger.genres import find_artist_dirs

        for name in ["The Beatles", "Guns N' Roses", "Beatles Tribute"]:
            (tmp_path / name).mkdir()

        assert find_artist_dirs(tmp_path, "beatles") == [tmp_path / "The Beatles"]
        assert find_artist_dirs(tmp_path, "Guns N Roses") == [tmp_path / "Guns N' Roses"]

    def test_suggests_similar_names(self, tmp_path: Path) -> None:
        from music_tagger.genres import find_artist_dirs, similar_artist_dirs

        (tmp_path / "Matchbox Twenty").mkdir()

        assert find_artist_dirs(tmp_path, "Matchbox 20") == []
        assert similar_artist_dirs(tmp_path, "Matchbox 20") == ["Matchbox Twenty"]


class TestAlbumGenres:
    def test_unions_track_genres_per_album(self, tmp_path: Path) -> None:
        from music_tagger.genres import album_genres

        root = _library(
            tmp_path,
            {
                "Sugar Ray/14-59 (1999)": [["Pop Rock", "Y2K Rock"], ["Y2K Rock"]],
                "Sugar Ray/Lemonade (1997)": [[]],
            },
        )

        albums = album_genres(root / "Sugar Ray")

        assert [(a.album_dir.name, a.genres) for a in albums] == [
            ("14-59 (1999)", ["Pop Rock", "Y2K Rock"]),
            ("Lemonade (1997)", []),
        ]


class TestGenreArtistCommand:
    def test_prints_albums_and_counts(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _genre

        root = _library(
            tmp_path,
            {
                "Matchbox Twenty/Mad Season (2000)": [["Y2K Rock"]],
                "Matchbox Twenty/More Than You Think You Are (2002)": [["Y2K Rock", "Pop Rock"]],
                "Matchbox Twenty/Untitled": [[]],
            },
        )

        _genre(["--artist", "matchbox twenty", "--root", str(root)])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "  Mad Season (2000): Y2K Rock\n" in out
        assert "  More Than You Think You Are (2002): Y2K Rock, Pop Rock\n" in out
        assert "  Untitled: (no genre)\n" in out
        assert "Genres by album count: Y2K Rock (2 of 3), Pop Rock (1 of 3)" in out

    def test_no_match_suggests_names(self, tmp_path: Path, capsys: object) -> None:
        from music_tagger.cli import _genre

        root = _library(tmp_path, {"Matchbox Twenty/Mad Season (2000)": [["Y2K Rock"]]})

        _genre(["--artist", "Matchbox 20", "--root", str(root)])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "No artist directory matches 'Matchbox 20'" in out
        assert "Similar: Matchbox Twenty" in out
