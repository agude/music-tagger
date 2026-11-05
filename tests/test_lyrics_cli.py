"""The `lyrics` command: mode rules, inspection, lookup, fetch, and logging."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from mutagen.flac import FLAC

from music_tagger import cli
from music_tagger.cli import _lyrics
from tests.lyrics_fakes import catalog as _catalog
from tests.lyrics_fakes import no_network as _no_network
from tests.lyrics_fakes import record as _record
from tests.lyrics_fakes import responder as _responder
from tests.lyrics_fakes import use_client as _use_client


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


USAGE_ERRORS = [
    ["--lookup", "--fetch"],
    ["--fetch", "--embed"],
    ["--lookup", "--embed"],
    ["--dry-run"],
    ["--lookup", "--dry-run"],
    ["--replace"],
    ["--lookup", "--replace"],
    ["--lyrics-id", "5"],
    ["--lyrics-id", "5", "--fetch"],
    ["--lyrics-id", "5", "--lookup"],
    ["--lyrics-id", "5", "--track", "1", "--embed"],
    ["--disc", "2"],
    ["--embed", "--lookup"],
]


class TestModeRules:
    @pytest.mark.parametrize("flags", USAGE_ERRORS, ids=lambda f: " ".join(f))
    def test_conflicting_flags_are_rejected_before_any_work(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str]
    ) -> None:
        _no_network(monkeypatch)
        before = _digest_tree(flac_album)
        with pytest.raises(SystemExit) as info:
            _lyrics([str(flac_album), *flags])
        assert info.value.code == 2
        assert _digest_tree(flac_album) == before

    def test_lookup_with_one_track_and_explicit_record_is_accepted(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        _lyrics([str(flac_album), "--lookup", "--track", "1", "--lyrics-id", "101"])

    def test_dry_run_replace_fetch_is_accepted(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        before = _digest_tree(flac_album)
        _lyrics([str(flac_album), "--fetch", "--dry-run", "--replace"])
        assert _digest_tree(flac_album) == before


class TestInspect:
    def test_prints_status_per_track_and_coverage(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _no_network(monkeypatch)
        (flac_album / "01 - Track 1.lrc").write_text("[00:01.00]Timed", encoding="utf-8")
        (flac_album / "02 - Track 2.txt").write_text("Plain", encoding="utf-8")

        _lyrics([str(flac_album)])

        out = capsys.readouterr().out
        assert "01 - Track 1.flac: synchronized" in out
        assert "    lrc (01 - Track 1.lrc): synchronized" in out
        assert "02 - Track 2.flac: plain" in out
        assert "03 - Track 3.flac: missing" in out
        assert "Coverage: missing 1, plain 1, synchronized 1" in out

    def test_reports_embedded_language(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _no_network(monkeypatch)
        audio = FLAC(flac_album / "01 - Track 1.flac")
        audio["LYRICS"] = ["Words"]
        audio.save()

        _lyrics([str(flac_album), "--track", "1"])

        out = capsys.readouterr().out
        assert "01 - Track 1.flac: plain" in out
        assert "    embedded: plain" in out
        assert "02 - Track 2.flac" not in out

    def test_writes_nothing(self, flac_album: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_network(monkeypatch)
        before = _digest_tree(flac_album)
        _lyrics([str(flac_album)])
        assert _digest_tree(flac_album) == before

    def test_track_without_match_exits_1(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_network(monkeypatch)
        with pytest.raises(SystemExit) as info:
            _lyrics([str(flac_album), "--track", "9"])
        assert info.value.code == 1


class TestLookup:
    def test_shows_selection_and_writes_nothing(
        self,
        flac_album: Path,
        monkeypatch: pytest.MonkeyPatch,
        default_log: Path,
        capsys: Any,
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        before = _digest_tree(flac_album)

        _lyrics([str(flac_album), "--lookup"])

        out = capsys.readouterr().out
        assert "01 - Track 1.flac: selected" in out
        assert "unique artist, title, duration, and album match" in out
        assert "record 101 (plausible): duration +0.0s; album matches" in out
        assert _digest_tree(flac_album) == before
        assert not default_log.exists()

    def test_ambiguous_is_reported_without_failing(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        catalog = _catalog(flac_album)
        catalog["Desperado"].append(_record(900, "Desperado", "Desperado"))
        _use_client(monkeypatch, _responder(catalog))

        _lyrics([str(flac_album), "--lookup", "--track", "1"])

        assert "01 - Track 1.flac: ambiguous" in capsys.readouterr().out

    def test_track_selector_limits_lookup(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        _lyrics([str(flac_album), "--lookup", "--track", "3"])
        out = capsys.readouterr().out
        assert "03 - Track 3.flac: selected" in out
        assert "01 - Track 1.flac" not in out

    def test_explicit_record_shows_evidence(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        catalog = _catalog(flac_album)
        catalog["Other"] = [_record(555, "Other", "Other", artistName="Someone Else")]
        _use_client(monkeypatch, _responder(catalog))
        before = _digest_tree(flac_album)

        _lyrics([str(flac_album), "--lookup", "--track", "2", "--lyrics-id", "555"])

        out = capsys.readouterr().out
        assert "02 - Track 2.flac: selected" in out
        assert "explicit record id 555" in out
        assert "artist differs" in out
        assert _digest_tree(flac_album) == before


class TestFetch:
    def test_writes_synchronized_sidecars_and_logs_each_change(
        self,
        flac_album: Path,
        monkeypatch: pytest.MonkeyPatch,
        default_log: Path,
        capsys: Any,
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))

        _lyrics([str(flac_album), "--fetch"])

        for name in ("01 - Track 1", "02 - Track 2", "03 - Track 3"):
            assert (flac_album / f"{name}.lrc").read_bytes() == b"[00:01.00]Timed line\n"
        assert "Summary: 3 wrote" in capsys.readouterr().out
        log = default_log.read_text(encoding="utf-8")
        assert (
            "- `01 - Track 1.flac`: wrote `01 - Track 1.lrc` from LRCLIB record 101 "
            "(synchronized, new file)" in log
        )
        assert "record 103" in log

    def test_log_never_contains_lyric_text(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, default_log: Path
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        _lyrics([str(flac_album), "--fetch"])
        log = default_log.read_text(encoding="utf-8")
        assert "Timed line" not in log
        assert "Plain line" not in log

    def test_dry_run_writes_and_logs_nothing(
        self,
        flac_album: Path,
        monkeypatch: pytest.MonkeyPatch,
        default_log: Path,
        capsys: Any,
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        before = _digest_tree(flac_album)

        _lyrics([str(flac_album), "--fetch", "--dry-run"])

        out = capsys.readouterr().out
        assert "Summary: 3 would write" in out
        assert "(dry run — nothing written)" in out
        assert _digest_tree(flac_album) == before
        assert not default_log.exists()

    def test_existing_lyrics_are_preserved_by_default(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Old words\n", encoding="utf-8")
        _use_client(monkeypatch, _responder(_catalog(flac_album)))

        _lyrics([str(flac_album), "--fetch"])

        assert (flac_album / "01 - Track 1.txt").read_text(encoding="utf-8") == "Old words\n"
        assert not (flac_album / "01 - Track 1.lrc").exists()
        assert (flac_album / "02 - Track 2.lrc").exists()
        out = capsys.readouterr().out
        assert "preserved" in out
        assert "Summary: 1 preserved, 2 wrote" in out

    def test_replace_removes_stale_sidecar_and_logs_it(
        self,
        flac_album: Path,
        monkeypatch: pytest.MonkeyPatch,
        default_log: Path,
    ) -> None:
        (flac_album / "01 - Track 1.txt").write_text("Old words\n", encoding="utf-8")
        _use_client(monkeypatch, _responder(_catalog(flac_album)))

        _lyrics([str(flac_album), "--fetch", "--replace", "--track", "1"])

        assert (flac_album / "01 - Track 1.lrc").exists()
        assert not (flac_album / "01 - Track 1.txt").exists()
        log = default_log.read_text(encoding="utf-8")
        assert "(synchronized, replaced existing lyrics)" in log
        assert "- `01 - Track 1.flac`: removed stale `01 - Track 1.txt`" in log

    def test_instrumental_writes_no_file(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        catalog = _catalog(flac_album, instrumental=True, plainLyrics=None, syncedLyrics=None)
        _use_client(monkeypatch, _responder(catalog))

        _lyrics([str(flac_album), "--fetch"])

        assert not list(flac_album.glob("*.lrc"))
        assert not list(flac_album.glob("*.txt"))
        assert "Summary: 3 instrumental" in capsys.readouterr().out

    def test_unresolved_selection_exits_3_and_others_still_write(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(flac_album)
        catalog["Twenty-One"].append(_record(902, "Twenty-One", "Desperado"))
        _use_client(monkeypatch, _responder(catalog))

        with pytest.raises(SystemExit) as info:
            _lyrics([str(flac_album), "--fetch"])

        assert info.value.code == cli.EXIT_LYRICS_UNRESOLVED
        assert (flac_album / "01 - Track 1.lrc").exists()
        assert not (flac_album / "02 - Track 2.lrc").exists()
        assert (flac_album / "03 - Track 3.lrc").exists()

    def test_missing_title_is_unresolved(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(flac_album)
        audio = FLAC(flac_album / "01 - Track 1.flac")
        del audio["TITLE"]
        audio.save()
        _use_client(monkeypatch, _responder(catalog))

        with pytest.raises(SystemExit) as info:
            _lyrics([str(flac_album), "--fetch", "--track", "1"])

        assert info.value.code == cli.EXIT_LYRICS_UNRESOLVED
        assert not (flac_album / "01 - Track 1.lrc").exists()

    def test_outage_exits_1_and_writes_nothing(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _use_client(monkeypatch, lambda request: httpx.Response(503))
        before = _digest_tree(flac_album)

        with pytest.raises(SystemExit) as info:
            _lyrics([str(flac_album), "--fetch"])

        assert info.value.code == 1
        assert _digest_tree(flac_album) == before
        assert "provider_failure" in capsys.readouterr().out

    def test_rate_limit_exits_1_and_is_labelled(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _use_client(
            monkeypatch,
            lambda request: httpx.Response(429, headers={"Retry-After": "600"}),
        )

        with pytest.raises(SystemExit) as info:
            _lyrics([str(flac_album), "--fetch", "--track", "1"])

        assert info.value.code == 1
        assert "rate_limited" in capsys.readouterr().out

    def test_explicit_record_writes_only_its_track(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(flac_album)
        catalog["Other"] = [_record(555, "Other", "Other")]
        _use_client(monkeypatch, _responder(catalog))

        _lyrics([str(flac_album), "--fetch", "--track", "2", "--lyrics-id", "555"])

        assert (flac_album / "02 - Track 2.lrc").exists()
        assert not list(flac_album.glob("01*.lrc"))
        assert not list(flac_album.glob("03*.lrc"))

    def test_no_log_flag_writes_sidecars_without_log(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, default_log: Path
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))

        _lyrics([str(flac_album), "--fetch", "--no-log"])

        assert (flac_album / "01 - Track 1.lrc").exists()
        assert not default_log.exists()

    def test_second_run_changes_nothing(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _use_client(monkeypatch, _responder(_catalog(flac_album)))
        _lyrics([str(flac_album), "--fetch"])
        before = _digest_tree(flac_album)

        _lyrics([str(flac_album), "--fetch"])

        assert _digest_tree(flac_album) == before
        assert "Summary: 3 unchanged" in capsys.readouterr().out
