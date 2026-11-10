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

    def test_failed_revert_reports_and_logs_the_stranded_file(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tracks = read_album(flac_album).tracks
        for track, title in zip(tracks, ["Desperado", "Twenty-One", "Out of Control"], strict=True):
            write_tags(track, [TagChange("title", track.tags["title"], title)])
        log = tmp_path / "changes.log"
        real_rename = Path.rename
        calls = {"revert": 0}

        def fail_second_and_its_revert(self: Path, target: str | Path) -> Path:
            name = Path(target).name
            if name == "02. Twenty-One.flac":
                raise OSError("disk full")
            # The first reverting move takes "01. Desperado.flac" back to a temporary name.
            if self.name == "01. Desperado.flac" and name.endswith(".tmp"):
                calls["revert"] += 1
                raise OSError("share went away")
            return real_rename(self, target)

        with (
            patch.object(Path, "rename", fail_second_and_its_revert),
            pytest.raises(SystemExit) as error,
        ):
            _rename([str(flac_album), "--log", str(log)])

        assert error.value.code == 1
        assert calls["revert"] == 1
        names = sorted(p.name for p in flac_album.glob("*.flac"))
        assert "01. Desperado.flac" in names
        err = capsys.readouterr().err
        assert "1 file(s) could not be reverted" in err
        assert "is now 01. Desperado.flac" in err
        text = _log_text(log)
        assert "## Rename" in text
        assert "01. Desperado.flac" in text
        assert "Twenty-One" not in text

    def test_stranded_file_is_reported_once_with_its_error(
        self, flac_album: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tracks = read_album(flac_album).tracks
        for track, title in zip(tracks, ["Desperado", "Twenty-One", "Out of Control"], strict=True):
            write_tags(track, [TagChange("title", track.tags["title"], title)])
        source_name = tracks[0].path.name
        real_rename = Path.rename

        def fail_second_and_its_revert(self: Path, target: str | Path) -> Path:
            name = Path(target).name
            if name == "02. Twenty-One.flac":
                raise OSError("disk full")
            if self.name == "01. Desperado.flac" and name.endswith(".tmp"):
                raise OSError("share went away")
            return real_rename(self, target)

        with (
            patch.object(Path, "rename", fail_second_and_its_revert),
            pytest.raises(SystemExit) as error,
        ):
            _rename([str(flac_album)])

        assert error.value.code == 1
        err = capsys.readouterr().err
        assert err.count("01. Desperado.flac") == 1
        assert f"  {source_name} is now 01. Desperado.flac (share went away)" in err


def _swap_two_files(flac_album: Path) -> None:
    """Two files whose targets are each other's names: "01. Alpha" becomes track 2 Beta."""
    (flac_album / "03 - Track 3.flac").unlink()
    first, second = sorted(flac_album.glob("*.flac"))
    alpha = flac_album / "01. Alpha.flac"
    beta = flac_album / "02. Beta.flac"
    first.rename(alpha)
    second.rename(beta)
    write_tags(
        read_album(flac_album).tracks[0],
        [TagChange("tracknumber", "", "2"), TagChange("title", "", "Beta")],
    )
    write_tags(
        read_album(flac_album).tracks[1],
        [TagChange("tracknumber", "", "1"), TagChange("title", "", "Alpha")],
    )


class TestRenameSwapRollbackKeepsEveryFile:
    def test_failed_swap_revert_does_not_replace_a_stranded_file(
        self, flac_album: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from music_tagger.cli.library import _rename

        _swap_two_files(flac_album)
        before = sorted(p.read_bytes() for p in flac_album.iterdir())
        real_rename = Path.rename
        beta_moves = {"count": 0}

        def fail_swap_and_its_revert(self: Path, target: str | Path) -> Path:
            name = Path(target).name
            if name == "01. Alpha.flac" and self.name.endswith(".tmp"):
                raise OSError("disk full")
            if self.name == "02. Beta.flac" and name.endswith(".tmp"):
                # The first such move is phase 1 of the swap; the second reverts Beta.
                beta_moves["count"] += 1
                if beta_moves["count"] == 2:
                    raise OSError("share went away")
            return real_rename(self, target)

        with (
            patch.object(Path, "rename", fail_swap_and_its_revert),
            pytest.raises(SystemExit) as error,
        ):
            _rename([str(flac_album)])

        assert error.value.code == 1
        after = sorted(p.read_bytes() for p in flac_album.iterdir())
        assert after == before
        err = capsys.readouterr().err
        assert "01. Alpha.flac is now 02. Beta.flac (share went away)" in err
        assert "02. Beta.flac is held by 01. Alpha.flac" in err


class TestRenameCaseVariantHeld:
    def test_source_matching_a_stranded_path_in_case_is_held(self, tmp_path: Path) -> None:
        from music_tagger.cli.library import RenameFailedError, _two_phase_rename

        alpha = tmp_path / "a.flac"
        beta = tmp_path / "B.flac"
        alpha.write_bytes(b"audio-A")
        beta.write_bytes(b"audio-B")
        real_rename = Path.rename

        def fail_onto_a_and_revert_onto_b(self: Path, target: str | Path) -> Path:
            name = Path(target).name
            if name == "a.flac" and self.name.endswith(".tmp"):
                raise OSError("disk full")
            if name.endswith(".tmp") and self.name == "b.flac":
                raise OSError("share went away")
            return real_rename(self, target)

        with (
            patch.object(Path, "rename", fail_onto_a_and_revert_onto_b),
            pytest.raises(RenameFailedError) as error,
        ):
            _two_phase_rename([(alpha, tmp_path / "b.flac"), (beta, alpha)])

        held = [entry for entry in error.value.stranded if entry[0] == beta]
        assert len(held) == 1
        assert held[0][2] == "B.flac is held by a.flac"
        assert held[0][1].exists()
        assert sorted(p.read_bytes() for p in tmp_path.iterdir()) == [b"audio-A", b"audio-B"]


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
