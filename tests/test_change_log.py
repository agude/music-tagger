"""Every mutating album command logs to the change log unless told not to."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC

from music_tagger.cli import _art, _default_log_path, _genre, _lock, _rename, _replaygain


def _rename_fixture_files(album_dir: Path) -> None:
    for path in sorted(album_dir.glob("*.flac")):
        path.rename(path.parent / f"track{path.stem[:2]}.flac")


class TestDefaultLogPath:
    def test_environment_override(self, default_log: Path) -> None:
        assert _default_log_path() == default_log

    def test_project_log_without_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MUSIC_TAGGER_LOG")

        assert _default_log_path() == Path.home() / "Projects/music-tagger/changes.log"


class TestDefaultLogging:
    def test_rename_logs_by_default(self, flac_album: Path, default_log: Path) -> None:
        _rename_fixture_files(flac_album)

        _rename([str(flac_album)])

        assert "## Rename" in default_log.read_text()

    def test_genre_logs_by_default(self, flac_album: Path, default_log: Path) -> None:
        _genre([str(flac_album), "Retro Rock"])

        assert "## Genre" in default_log.read_text()

    def test_lock_logs_by_default(self, flac_album: Path, default_log: Path) -> None:
        _lock([str(flac_album), "isrc"])

        assert "## Lock: isrc" in default_log.read_text()

    def test_no_log_writes_nothing(self, flac_album: Path, default_log: Path) -> None:
        _rename_fixture_files(flac_album)

        _rename([str(flac_album), "--no-log"])

        assert not default_log.exists()

    def test_explicit_log_replaces_default(
        self, flac_album: Path, default_log: Path, tmp_path: Path
    ) -> None:
        explicit = tmp_path / "explicit.log"
        _rename_fixture_files(flac_album)

        _rename([str(flac_album), "--log", str(explicit)])

        assert "## Rename" in explicit.read_text()
        assert not default_log.exists()

    def test_log_and_no_log_conflict(self, flac_album: Path, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as error:
            _rename([str(flac_album), "--log", str(tmp_path / "x.log"), "--no-log"])

        assert error.value.code == 2


class TestArtLog:
    def test_logs_local_cover_and_embed(
        self, flac_album: Path, default_log: Path, tmp_path: Path
    ) -> None:
        cover = tmp_path / "front.jpg"
        cover.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        _art([str(flac_album), "--cover-file", str(cover), "--embed"])

        log = default_log.read_text()
        assert "## Cover art" in log
        assert f"- **Source:** {cover}" in log
        assert "- **Embedded:** 3 file(s)" in log

    def test_logs_release_source(self, flac_album: Path, default_log: Path) -> None:
        from music_tagger.coverart import ArtResult

        saved = flac_album / "cover.jpg"
        saved.write_bytes(b"\xff\xd8\xff\xe0")
        with patch("music_tagger.cli.fetch_cover_art") as fetch:
            fetch.return_value = ArtResult(saved=True, path=saved, size_bytes=4)
            _art([str(flac_album), "--release-id", "r-1"])

        assert "- **Source:** Cover Art Archive release r-1" in default_log.read_text()

    def test_skipped_cover_without_embed_is_not_logged(
        self, flac_album: Path, default_log: Path, tmp_path: Path
    ) -> None:
        (flac_album / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        cover = tmp_path / "front.jpg"
        cover.write_bytes(b"\xff\xd8\xff\xe0")

        _art([str(flac_album), "--cover-file", str(cover)])

        assert not default_log.exists()


class TestReplayGainLog:
    def test_logs_album_gain(self, flac_album: Path, default_log: Path) -> None:
        from music_tagger.replaygain import ReplayGainResult

        def fake_rsgain(directory: Path, **_: object) -> ReplayGainResult:
            for path in sorted(directory.glob("*.flac")):
                audio = FLAC(path)
                audio["REPLAYGAIN_ALBUM_GAIN"] = ["-7.25 dB"]
                audio.save()
            return ReplayGainResult(directory, 3, dry_run=False)

        with patch("music_tagger.replaygain.scan_replaygain", side_effect=fake_rsgain):
            _replaygain([str(flac_album)])

        log = default_log.read_text()
        assert "## ReplayGain" in log
        assert "- **Tracks:** 3" in log
        assert "- **Album gain:** -7.25 dB" in log

    def test_dry_run_is_not_logged(self, flac_album: Path, default_log: Path) -> None:
        from music_tagger.replaygain import ReplayGainResult

        with patch("music_tagger.replaygain.scan_replaygain") as scan:
            scan.return_value = ReplayGainResult(flac_album, 3, dry_run=True)
            _replaygain([str(flac_album), "--dry-run"])

        assert not default_log.exists()
