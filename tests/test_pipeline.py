"""Tests for the apply, place, and rip pipeline commands."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC

from music_tagger.cli import EXIT_REVIEW, _apply, _place, _rip
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.replaygain import ReplayGainResult
from music_tagger.ripper import RipResult
from music_tagger.tags import read_album, read_genres

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _release(first_title: str = "Desperado") -> MBRelease:
    return MBRelease(
        id="r-1",
        title="Desperado",
        artist="Eagles",
        discs={
            1: [
                MBTrack(number=1, title=first_title),
                MBTrack(number=2, title="Twenty-One"),
                MBTrack(number=3, title="Out of Control"),
            ]
        },
    )


def _run_apply(album_dir: Path, *args: str, release: MBRelease | None = None) -> None:
    with (
        patch("music_tagger.cli.MusicBrainzClient") as client,
        patch("music_tagger.replaygain.scan_replaygain") as scan,
    ):
        client.return_value.fetch_release.return_value = release or _release()
        scan.return_value = ReplayGainResult(album_dir, 3, dry_run=False)
        _apply([str(album_dir), "--release-id", "r-1", *args])


@pytest.fixture()
def cover(tmp_path: Path) -> Path:
    path = tmp_path / "front.jpg"
    path.write_bytes(JPEG)
    return path


class TestApplyCommand:
    def test_runs_every_step(self, flac_album: Path, cover: Path, default_log: Path) -> None:
        _run_apply(flac_album, "--cover-file", str(cover), "--genre", "Retro Rock")

        names = sorted(path.name for path in flac_album.glob("*.flac"))
        assert names == ["01. Desperado.flac", "02. Twenty-One.flac", "03. Out of Control.flac"]
        first = flac_album / names[0]
        assert read_album(flac_album).tracks[0].tags["musicbrainz_albumid"] == "r-1"
        assert (flac_album / "cover.jpg").read_bytes() == JPEG
        assert FLAC(first).pictures[0].data == JPEG
        assert read_genres(first) == ["Retro Rock"]
        log = default_log.read_text()
        for heading in ("## Cover art", "## Genre", "## Rename", "## ReplayGain"):
            assert heading in log

    def test_marks_checklist_entry_done(
        self, flac_album: Path, cover: Path, default_checklist: Path
    ) -> None:
        default_checklist.write_text(
            "# music-tagger checklist\n\n1 albums to process.\n\n"
            f"- [ ] `{flac_album}` — Eagles — Desperado (3 tracks) [NO MB ID]\n"
        )

        _run_apply(flac_album, "--cover-file", str(cover), "--genre", "Retro Rock")

        assert f"- [x] `{flac_album}`" in default_checklist.read_text()

    def test_album_absent_from_checklist_is_fine(
        self, flac_album: Path, cover: Path, default_checklist: Path
    ) -> None:
        _run_apply(flac_album, "--cover-file", str(cover), "--genre", "Retro Rock")

        assert not default_checklist.exists()

    def test_sets_several_genres(self, flac_album: Path, cover: Path) -> None:
        _run_apply(
            flac_album, "--cover-file", str(cover), "--genre", "Retro Rock", "--genre", "Folk Rock"
        )

        first = sorted(flac_album.glob("*.flac"))[0]
        assert read_genres(first) == ["Retro Rock", "Folk Rock"]

    def test_keep_cover_embeds_existing_image(self, flac_album: Path) -> None:
        (flac_album / "cover.jpg").write_bytes(JPEG)

        _run_apply(flac_album, "--keep-cover", "--genre", "Retro Rock")

        first = sorted(flac_album.glob("*.flac"))[0]
        assert FLAC(first).pictures[0].data == JPEG

    def test_keep_cover_without_image_fails_before_writing(self, flac_album: Path) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _run_apply(flac_album, "--keep-cover", "--genre", "Retro Rock")

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before

    def test_missing_cover_file_fails_before_writing(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _run_apply(flac_album, "--cover-file", str(tmp_path / "nope.jpg"), "--genre", "Jazz")

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before

    def test_requires_a_cover_decision(self, flac_album: Path) -> None:
        with pytest.raises(SystemExit) as error:
            _run_apply(flac_album, "--genre", "Retro Rock")

        assert error.value.code == 2

    def test_requires_a_genre(self, flac_album: Path, cover: Path) -> None:
        with pytest.raises(SystemExit) as error:
            _run_apply(flac_album, "--cover-file", str(cover))

        assert error.value.code == 2

    def test_unknown_genre_fails_before_writing(self, flac_album: Path, cover: Path) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _run_apply(flac_album, "--cover-file", str(cover), "--genre", "Not A Genre")

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before

    def test_force_genre_allows_unlisted_genre(self, flac_album: Path, cover: Path) -> None:
        _run_apply(
            flac_album, "--cover-file", str(cover), "--genre", "Not A Genre", "--force-genre"
        )

        assert read_genres(sorted(flac_album.glob("*.flac"))[0]) == ["Not A Genre"]

    def test_gate_stops_before_any_step_writes(
        self, flac_album: Path, cover: Path, capsys: object
    ) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _run_apply(
                flac_album,
                "--cover-file",
                str(cover),
                "--genre",
                "Retro Rock",
                release=_release("Doolin-Dalton"),
            )

        assert error.value.code == EXIT_REVIEW
        assert {path: path.read_bytes() for path in before} == before
        assert not (flac_album / "cover.jpg").exists()
        assert "stopped at tag" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_passes_tag_flags_through(self, flac_album: Path, cover: Path) -> None:
        _run_apply(
            flac_album,
            "--cover-file",
            str(cover),
            "--genre",
            "Retro Rock",
            "--approve-overwrites",
            release=_release("Doolin-Dalton"),
        )

        assert read_album(flac_album).tracks[0].tags["title"] == "Doolin-Dalton"


@pytest.fixture()
def staging(tmp_path: Path, flac_album: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The FLAC fixture album inside a directory that counts as the temp dir."""
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    staged = temp_root / "rip-abc"
    flac_album.rename(staged)
    (staged / "Eagles - Desperado.log").write_text("whipper log")
    (staged / "Eagles - Desperado.m3u").write_text("playlist")
    return staged


class TestPlaceCommand:
    def test_copies_and_removes_staging(self, staging: Path, tmp_path: Path) -> None:
        root = tmp_path / "library"

        _place([str(staging), "--root", str(root)])

        album_dir = root / "Eagles" / "Desperado (1973)"
        assert sorted(path.name for path in album_dir.iterdir()) == [
            "01. Desperado.flac",
            "02. Twenty-One.flac",
            "03. Out of Control.flac",
            "Eagles - Desperado.log",
        ]
        assert not staging.exists()

    def test_logs_copy(self, staging: Path, tmp_path: Path, default_log: Path) -> None:
        _place([str(staging), "--root", str(tmp_path / "library")])

        assert "## Copy to" in default_log.read_text()

    def test_dry_run_changes_nothing(self, staging: Path, tmp_path: Path, capsys: object) -> None:
        root = tmp_path / "library"

        _place([str(staging), "--root", str(root), "--dry-run"])

        assert not root.exists()
        assert staging.exists()
        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert str(root / "Eagles" / "Desperado (1973)") in out

    def test_refuses_existing_album_directory(self, staging: Path, tmp_path: Path) -> None:
        album_dir = tmp_path / "library" / "Eagles" / "Desperado (1973)"
        album_dir.mkdir(parents=True)

        with pytest.raises(SystemExit) as error:
            _place([str(staging), "--root", str(tmp_path / "library")])

        assert error.value.code == 1
        assert list(album_dir.iterdir()) == []
        assert staging.exists()

    def test_merge_copies_into_existing_directory_and_keeps_staging(
        self, staging: Path, tmp_path: Path
    ) -> None:
        album_dir = tmp_path / "library" / "Eagles" / "Desperado (1973)"
        album_dir.mkdir(parents=True)
        (album_dir / "01. Desperado.flac").write_bytes(b"existing")

        _place([str(staging), "--root", str(tmp_path / "library"), "--merge"])

        assert (album_dir / "01. Desperado.flac").read_bytes() == b"existing"
        assert (album_dir / "02. Twenty-One.flac").exists()
        # A skipped file was not copied, so the staging copy is the only one.
        assert staging.exists()

    def test_overrides_directory_names(self, staging: Path, tmp_path: Path) -> None:
        root = tmp_path / "library"

        _place([str(staging), "--root", str(root), "--album-dir", "Desperado (Remaster)"])

        assert (root / "Eagles" / "Desperado (Remaster)" / "01. Desperado.flac").exists()

    def test_keeps_source_outside_temp_dir(
        self, flac_album: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "tmp-elsewhere"
        elsewhere.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(elsewhere))

        _place([str(flac_album), "--root", str(tmp_path / "library")])

        assert flac_album.exists()

    def test_copy_failure_keeps_staging(self, staging: Path, tmp_path: Path) -> None:
        with (
            patch("music_tagger.placement.shutil.copy2", side_effect=OSError("disk full")),
            pytest.raises(SystemExit) as error,
        ):
            _place([str(staging), "--root", str(tmp_path / "library")])

        assert error.value.code == 1
        assert staging.exists()


class TestRipStaging:
    def test_creates_staging_directory_when_path_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
    ) -> None:
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

        with patch("music_tagger.ripper.rip_cd") as rip_cd:
            rip_cd.return_value = RipResult(tracks=[], track_count=0)
            _rip([])

        target = rip_cd.call_args.args[0]
        assert target.parent == tmp_path
        assert target.name.startswith("rip-")
        assert f"Staging: {target}" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_uses_given_path(self, tmp_path: Path) -> None:
        with patch("music_tagger.ripper.rip_cd") as rip_cd:
            rip_cd.return_value = RipResult(tracks=[], track_count=0)
            _rip([str(tmp_path / "out")])

        assert rip_cd.call_args.args[0] == tmp_path / "out"
