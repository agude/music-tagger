"""A command that fails partway still logs every file it changed, and no file it did not."""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC
from PIL import Image

from music_tagger.cli.library import _art, _copy, _genre, _rename
from music_tagger.cli.tagging import _edit_locks, _tag, _write_tags
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.placement import compute_placement
from music_tagger.tagger import AlbumResult, TrackDiff
from music_tagger.tags import TagChange, read_album, write_locks, write_tags

FIRST = "01 - Track 1.flac"
SECOND = "02 - Track 2.flac"


def _fail_on_second(real: Callable[..., Any]) -> Callable[..., Any]:
    """A stand-in for a per-file writer: the first call works, the second raises."""
    calls = {"n": 0}

    def writer(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(*args, **kwargs)

    return writer


def _log_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _label_release() -> MBRelease:
    """Fills an empty label on every track: a change that needs no review."""
    return MBRelease(
        id="r-1",
        title="Desperado",
        artist="Eagles",
        label="Asylum Records",
        discs={
            1: [
                MBTrack(number=1, title="Desperado"),
                MBTrack(number=2, title="Twenty-One"),
                MBTrack(number=3, title="Out of Control"),
            ]
        },
    )


class TestTagLogsPartialFailure:
    def test_tag_logs_the_first_file_and_not_the_second(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        log = tmp_path / "changes.log"
        release = _label_release()

        with (
            patch("music_tagger.cli.tagging.MusicBrainzClient") as client,
            patch("music_tagger.tagger.write_tags", side_effect=_fail_on_second(write_tags)),
            pytest.raises(SystemExit) as error,
        ):
            client.return_value.fetch_release.return_value = release
            _tag([str(flac_album), "--release-id", "r-1", "--log", str(log)])

        assert error.value.code == 1
        text = _log_text(log)
        assert FIRST in text
        assert SECOND not in text


class TestWriteTagsLogsPartialFailure:
    def test_write_tags_logs_the_first_file_and_not_the_second(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        album = read_album(flac_album)
        diff = AlbumResult(
            album,
            MBRelease("r-1", "Desperado"),
            [TrackDiff(t, [TagChange("label", "", "Asylum Records")]) for t in album.tracks],
        )
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(diff.to_dict()))
        log = tmp_path / "changes.log"

        with (
            patch("music_tagger.tagger.write_tags", side_effect=_fail_on_second(write_tags)),
            pytest.raises(SystemExit) as error,
        ):
            _write_tags(["--diff", str(diff_path), "--log", str(log)])

        assert error.value.code == 1
        text = _log_text(log)
        assert FIRST in text
        assert SECOND not in text


class TestGenreLogsPartialFailure:
    def test_genre_logs_the_count_actually_written(self, flac_album: Path, tmp_path: Path) -> None:
        from music_tagger import tags as tags_module

        log = tmp_path / "changes.log"

        with (
            patch.object(
                tags_module, "write_genres", side_effect=_fail_on_second(tags_module.write_genres)
            ),
            pytest.raises(SystemExit) as error,
        ):
            _genre([str(flac_album), "Retro Rock", "--log", str(log)])

        assert error.value.code == 1
        text = _log_text(log)
        assert "## Genre: " in text
        assert "- **Tracks:** 1" in text
        assert "- **Tracks:** 3" not in text

    def test_genre_write_error_prints_file_and_exits_1(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from music_tagger import tags as tags_module

        with (
            patch.object(
                tags_module, "write_genres", side_effect=_fail_on_second(tags_module.write_genres)
            ),
            pytest.raises(SystemExit) as error,
        ):
            _genre([str(flac_album), "Retro Rock", "--log", str(tmp_path / "changes.log")])

        assert error.value.code == 1
        err = capsys.readouterr().err
        assert "Error: " in err
        assert SECOND in err
        assert "disk full" in err
        assert "Traceback" not in err


class TestRenameLogsPartialFailure:
    def test_failed_rename_is_reverted_and_not_logged(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        tracks = read_album(flac_album).tracks
        for track, title in zip(tracks, ["Desperado", "Twenty-One", "Out of Control"], strict=True):
            write_tags(track, [TagChange("title", track.tags["title"], title)])
        before = sorted(p.name for p in flac_album.glob("*.flac"))
        log = tmp_path / "changes.log"
        real_rename = Path.rename

        def fail_onto_second_track(self: Path, target: str | Path) -> Path:
            if Path(target).name == "02. Twenty-One.flac":
                raise OSError("disk full")
            return real_rename(self, target)

        with (
            patch.object(Path, "rename", fail_onto_second_track),
            pytest.raises(SystemExit) as error,
        ):
            _rename([str(flac_album), "--log", str(log)])

        assert error.value.code == 1
        assert sorted(p.name for p in flac_album.glob("*.flac")) == before
        assert "## Rename" not in _log_text(log)


class TestArtLogsPartialFailure:
    def test_embed_failure_logs_the_files_embedded_so_far(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16)).save(buffer, "JPEG")
        cover = tmp_path / "front.jpg"
        cover.write_bytes(buffer.getvalue())
        log = tmp_path / "changes.log"

        with (
            patch.object(FLAC, "save", side_effect=[None, OSError("disk full")]),
            pytest.raises(SystemExit) as error,
        ):
            _art([str(flac_album), "--cover-file", str(cover), "--embed", "--log", str(log)])

        assert error.value.code == 1
        text = _log_text(log)
        assert "## Cover art" in text
        assert "- **Embedded:** 1 file(s)" in text
        assert "- **Embedded:** 3 file(s)" not in text


class TestLockLogsPartialFailure:
    @pytest.mark.parametrize("command", ["lock", "unlock"])
    def test_lock_logs_the_first_track_and_not_the_second(
        self, flac_album: Path, tmp_path: Path, command: str
    ) -> None:
        tracks = read_album(flac_album).tracks
        if command == "unlock":
            for track in tracks:
                write_locks(track.path, ["label"])
        log = tmp_path / "changes.log"
        from music_tagger.cli import tagging as tagging_module

        with (
            patch.object(tagging_module, "write_locks", side_effect=_fail_on_second(write_locks)),
            pytest.raises(SystemExit) as error,
        ):
            _edit_locks([str(flac_album), "label", "--log", str(log)], locking=command == "lock")

        assert error.value.code == 1
        text = _log_text(log)
        assert f"- `{FIRST}`" in text
        assert f"- `{SECOND}`" not in text


class TestCopyLogsPartialFailure:
    def test_copy_logs_copied_files_before_exiting_1(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        album = read_album(flac_album)
        plan = compute_placement(album, root=tmp_path / "library")
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan.to_dict()))
        log = tmp_path / "changes.log"

        with (
            patch(
                "music_tagger.placement.shutil.copy2",
                side_effect=_fail_on_second(shutil.copy2),
            ),
            pytest.raises(SystemExit) as error,
        ):
            _copy(["--plan", str(plan_path), "--log", str(log)])

        assert error.value.code == 1
        text = _log_text(log)
        assert f"  {FIRST} → " in text
        assert "02. Twenty-One.flac" not in text
