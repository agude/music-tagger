from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from music_tagger.cli.tagging import _edit_tags
from music_tagger.tags import TagChange, compute_diff, read_album, write_locks, write_tags


def test_edit_tags_sets_and_clears_fields_then_second_run_is_noop(
    flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    track = read_album(flac_album).tracks[0]
    write_tags(track, [TagChange("isrc", "", "USIR19902220")])
    log_path = tmp_path / "pilot.log"
    arguments = [
        str(flac_album),
        "--file",
        track.path.name,
        "--set",
        "title=Corrected title",
        "--clear",
        "isrc",
        "--approve-overwrites",
        "--log",
        str(log_path),
    ]

    _edit_tags(arguments)

    corrected = read_album(flac_album).tracks[0]
    assert corrected.tags["title"] == "Corrected title"
    assert corrected.tags.get("isrc", "") == ""
    assert "Manual tag edit" in log_path.read_text()
    assert "Corrected title" in log_path.read_text()

    after_first_write = track.path.read_bytes()
    log_after_first_write = log_path.read_text()
    _edit_tags(arguments)
    capsys.readouterr()

    assert track.path.read_bytes() == after_first_write
    assert log_path.read_text() == log_after_first_write


def test_edit_tags_applies_explicit_version_titles_and_case(flac_album: Path) -> None:
    track = read_album(flac_album).tracks[0]
    title = f"{track.tags['title']} (Live)"
    assert compute_diff(track, {"title": title}) == []

    _edit_tags(
        [
            str(flac_album),
            "--file",
            track.path.name,
            "--set",
            f"title={title}",
            "--set",
            "releasetype=album",
            "--set",
            "releasestatus=OFFICIAL",
            "--approve-overwrites",
        ]
    )

    updated = read_album(flac_album).tracks[0]
    assert updated.tags["title"] == title
    assert updated.multi["releasetype"] == ("album",)
    assert updated.tags["releasestatus"] == "OFFICIAL"


def test_edit_tags_preserves_multi_value_fields_as_separate_values(
    flac_album: Path,
) -> None:
    track = read_album(flac_album).tracks[0]

    _edit_tags(
        [
            str(flac_album),
            "--file",
            track.path.name,
            "--set",
            "artists=First Artist",
            "--set",
            "artists=Second Artist",
        ]
    )

    updated = read_album(flac_album).tracks[0]
    assert updated.multi["artists"] == ("First Artist", "Second Artist")


def test_edit_tags_dry_run_requires_overwrite_approval_and_writes_nothing(
    flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    track = read_album(flac_album).tracks[0]
    before = track.path.read_bytes()
    log_path = tmp_path / "pilot.log"

    with pytest.raises(SystemExit) as error:
        _edit_tags(
            [
                str(flac_album),
                "--file",
                track.path.name,
                "--set",
                "title=Corrected title",
                "--dry-run",
                "--log",
                str(log_path),
            ]
        )

    assert error.value.code == 3
    assert track.path.read_bytes() == before
    assert not log_path.exists()
    assert "Gate: review" in capsys.readouterr().out


@pytest.mark.parametrize("dry_run", [False, True])
def test_edit_tags_refuses_locked_fields(flac_album: Path, tmp_path: Path, dry_run: bool) -> None:
    track = read_album(flac_album).tracks[0]
    write_locks(track.path, ["title"])
    locked_bytes = track.path.read_bytes()
    arguments = [
        str(flac_album),
        "--file",
        track.path.name,
        "--set",
        "title=Corrected title",
        "--approve-overwrites",
        "--log",
        str(tmp_path / "pilot.log"),
    ]
    if dry_run:
        arguments.append("--dry-run")

    with pytest.raises(SystemExit) as error:
        _edit_tags(arguments)

    assert error.value.code == 1
    assert read_album(flac_album).tracks[0].tags["title"] == "Desperado"
    assert track.path.read_bytes() == locked_bytes
    assert not (tmp_path / "pilot.log").exists()


def test_edit_tags_all_updates_album_and_is_idempotent(flac_album: Path, tmp_path: Path) -> None:
    log = tmp_path / "album.log"
    arguments = [
        str(flac_album),
        "--all",
        "--set",
        "date=1997",
        "--set",
        "totaldiscs=3",
        "--set",
        "artists=First Artist",
        "--set",
        "artists=Second Artist",
        "--approve-overwrites",
        "--log",
        str(log),
    ]
    _edit_tags(arguments)
    album = read_album(flac_album)
    assert len(album.tracks) == 3
    for track in album.tracks:
        assert track.tags["date"] == "1997"
        assert track.tags["totaldiscs"] == "3"
        assert track.multi["artists"] == ("First Artist", "Second Artist")
    files = {track.path: track.path.read_bytes() for track in album.tracks}
    first_log = log.read_bytes()
    _edit_tags(arguments)
    assert all(path.read_bytes() == content for path, content in files.items())
    assert log.read_bytes() == first_log


@pytest.mark.parametrize("approve", [False, True])
def test_edit_tags_all_dry_run_preserves_every_file(
    flac_album: Path, tmp_path: Path, approve: bool
) -> None:
    files = {track.path: track.path.read_bytes() for track in read_album(flac_album).tracks}
    log = tmp_path / "dry-run.log"
    arguments = [
        str(flac_album),
        "--all",
        "--set",
        "title=Shared title",
        "--dry-run",
        "--log",
        str(log),
    ]
    if approve:
        arguments.append("--approve-overwrites")
    with pytest.raises(SystemExit) as error:
        _edit_tags(arguments)
    assert error.value.code == 3
    assert all(path.read_bytes() == content for path, content in files.items())
    assert not log.exists()


@pytest.mark.parametrize("problem", ["locked", "unreadable", "m4a"])
def test_edit_tags_all_refuses_entire_batch_before_writing(
    flac_album: Path, m4a_album: Path, tmp_path: Path, problem: str
) -> None:
    if problem == "locked":
        write_locks(read_album(flac_album).tracks[-1].path, ["title"])
    elif problem == "unreadable":
        (flac_album / "99. Unreadable.flac").write_bytes(b"not audio")
    else:
        shutil.copyfile(next(m4a_album.glob("*.m4a")), flac_album / "99. Unsupported.m4a")
    files = {path: path.read_bytes() for path in flac_album.iterdir() if path.is_file()}
    log = tmp_path / "refused.log"
    with pytest.raises(SystemExit) as error:
        _edit_tags(
            [
                str(flac_album),
                "--all",
                "--set",
                "title=Shared title",
                "--approve-overwrites",
                "--log",
                str(log),
            ]
        )
    assert error.value.code == 1
    assert all(path.read_bytes() == content for path, content in files.items())
    assert not log.exists()
