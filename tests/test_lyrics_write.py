from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from music_tagger.lyrics import LyricsKind, inspect_track
from music_tagger.lyrics_write import SidecarPlan, WriteStatus, apply_plan, plan_sidecar_write

TRACK = "01 - Track 1.flac"


def _plan(
    album: Path,
    kind: LyricsKind | None,
    text: str,
    *,
    replace: bool = False,
    name: str = TRACK,
) -> SidecarPlan:
    return plan_sidecar_write(inspect_track(album / name), kind, text, replace=replace)


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestPlanning:
    def test_synchronized_text_targets_lrc(self, flac_album: Path) -> None:
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Line")
        assert plan.status is WriteStatus.WRITE
        assert plan.target == flac_album / "01 - Track 1.lrc"
        assert plan.content == b"[00:01.00]Line\n"
        assert plan.remove == ()

    def test_plain_text_targets_txt(self, flac_album: Path) -> None:
        plan = _plan(flac_album, LyricsKind.PLAIN, "Plain line")
        assert plan.status is WriteStatus.WRITE
        assert plan.target == flac_album / "01 - Track 1.txt"
        assert plan.content == b"Plain line\n"

    def test_unicode_is_written_as_utf8_without_bom(self, flac_album: Path) -> None:
        text = "Señor 日本語\nSecond line"
        plan = _plan(flac_album, LyricsKind.PLAIN, text)
        assert plan.content == "Señor 日本語\nSecond line\n".encode()
        assert not plan.content.startswith(b"\xef\xbb\xbf")

    def test_no_text_creates_nothing(self, flac_album: Path) -> None:
        plan = _plan(flac_album, None, "")
        assert plan.status is WriteStatus.NO_TEXT
        assert plan.target is None
        assert not (flac_album / "01 - Track 1.lrc").exists()

    def test_preserves_plain_when_synchronized_becomes_available(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Existing plain\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed")
        assert plan.status is WriteStatus.PRESERVED
        assert plan.content is None
        assert not (flac_album / "01 - Track 1.lrc").exists()
        assert (flac_album / "01 - Track 1.txt").read_text(encoding="utf-8") == "Existing plain\n"

    def test_preserves_embedded_lyrics(self, flac_album: Path) -> None:
        audio = FLAC(flac_album / TRACK)
        audio["LYRICS"] = ["Embedded words"]
        audio.save()
        plan = _plan(flac_album, LyricsKind.PLAIN, "Other words")
        assert plan.status is WriteStatus.PRESERVED

    def test_empty_sidecar_is_overwritten_without_replace(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_bytes(b"")
        plan = _plan(flac_album, LyricsKind.PLAIN, "New words")
        assert plan.status is WriteStatus.WRITE
        assert plan.content == b"New words\n"

    def test_malformed_existing_sidecar_is_preserved(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_bytes("Señor".encode("latin-1"))
        plan = _plan(flac_album, LyricsKind.PLAIN, "New words")
        assert plan.status is WriteStatus.PRESERVED

    def test_identical_text_is_unchanged_even_with_replace(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.lrc").write_text("[00:01.00]Line\n", encoding="utf-8")
        assert _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Line").status is (
            WriteStatus.UNCHANGED
        )
        assert (
            _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Line", replace=True).status
            is WriteStatus.UNCHANGED
        )

    def test_replace_overwrites_same_kind(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Old\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.PLAIN, "New", replace=True)
        assert plan.status is WriteStatus.WRITE
        assert plan.content == b"New\n"
        assert plan.remove == ()
        assert "replace existing lyrics" in plan.reason

    def test_replace_removes_stale_other_type_sidecar(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed", replace=True)
        assert plan.status is WriteStatus.WRITE
        assert plan.target == flac_album / "01 - Track 1.lrc"
        assert plan.remove == (flac_album / "01 - Track 1.txt",)
        assert "remove stale 01 - Track 1.txt" in plan.reason

    def test_stale_sidecar_is_kept_without_replace(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed")
        assert plan.status is WriteStatus.PRESERVED
        assert plan.remove == ()

    def test_identical_target_with_stale_sidecar_only_removes(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.lrc").write_text("[00:01.00]Line\n", encoding="utf-8")
        (flac_album / "01 - Track 1.txt").write_text("Stale\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Line", replace=True)
        assert plan.status is WriteStatus.WRITE
        assert plan.content is None
        assert plan.remove == (flac_album / "01 - Track 1.txt",)

    def test_unrelated_txt_is_never_removed(self, flac_album: Path) -> None:
        (flac_album / "notes.txt").write_text("Recording notes", encoding="utf-8")
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed", replace=True)
        assert (flac_album / "notes.txt") not in plan.remove

    def test_refuses_when_a_source_cannot_be_read(self, flac_album: Path) -> None:
        (flac_album / "04 - Broken.flac").write_bytes(b"garbage, not audio")
        plan = _plan(flac_album, LyricsKind.PLAIN, "Words", name="04 - Broken.flac")
        assert plan.status is WriteStatus.REFUSED
        assert plan.content is None

    def test_refuses_synchronized_text_without_timestamps(self, flac_album: Path) -> None:
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "No timestamps here")
        assert plan.status is WriteStatus.REFUSED
        assert "synchronized" in plan.reason

    def test_refuses_plain_text_with_timestamps(self, flac_album: Path) -> None:
        plan = _plan(flac_album, LyricsKind.PLAIN, "[00:01.00]Timed")
        assert plan.status is WriteStatus.REFUSED

    def test_refuses_text_that_is_not_utf8_encodable(self, flac_album: Path) -> None:
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]\ud800")
        assert plan.status is WriteStatus.REFUSED
        assert "UTF-8" in plan.reason

    def test_refuses_kinds_that_cannot_be_written(self, flac_album: Path) -> None:
        plan = _plan(flac_album, LyricsKind.MALFORMED, "Words")
        assert plan.status is WriteStatus.REFUSED

    def test_planning_writes_nothing(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        before = _digest_tree(flac_album)
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed", replace=True)
        assert plan.status is WriteStatus.WRITE
        assert _digest_tree(flac_album) == before


class TestApply:
    def test_writes_target_and_removes_stale_sidecar(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        (flac_album / "notes.txt").write_text("Recording notes", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed", replace=True)

        apply_plan(plan)

        assert (flac_album / "01 - Track 1.lrc").read_bytes() == b"[00:01.00]Timed\n"
        assert not (flac_album / "01 - Track 1.txt").exists()
        assert (flac_album / "notes.txt").read_text(encoding="utf-8") == "Recording notes"

    def test_written_sidecar_has_standard_mode(self, flac_album: Path) -> None:
        apply_plan(_plan(flac_album, LyricsKind.PLAIN, "Words"))
        assert (flac_album / "01 - Track 1.txt").stat().st_mode & 0o777 == 0o644

    def test_audio_is_not_modified(self, flac_album: Path) -> None:
        audio = flac_album / TRACK
        before = audio.read_bytes()
        apply_plan(_plan(flac_album, LyricsKind.PLAIN, "Words"))
        assert audio.read_bytes() == before

    def test_refuses_to_apply_non_write_plans(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed")
        assert plan.status is WriteStatus.PRESERVED
        with pytest.raises(ValueError, match="cannot apply"):
            apply_plan(plan)
        assert not (flac_album / "01 - Track 1.lrc").exists()

    def test_interrupted_write_keeps_old_file_and_no_temp_files(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = flac_album / "01 - Track 1.txt"
        target.write_text("Old\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.PLAIN, "New", replace=True)

        def fail(src: str, dst: str) -> None:
            raise OSError("simulated interruption")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="simulated interruption"):
            apply_plan(plan)

        assert target.read_text(encoding="utf-8") == "Old\n"
        leftovers = [p.name for p in flac_album.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_missing_stale_file_is_tolerated(self, flac_album: Path) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Plain\n", encoding="utf-8")
        plan = _plan(flac_album, LyricsKind.SYNCHRONIZED, "[00:01.00]Timed", replace=True)
        (flac_album / "01 - Track 1.txt").unlink()
        apply_plan(plan)
        assert (flac_album / "01 - Track 1.lrc").exists()
