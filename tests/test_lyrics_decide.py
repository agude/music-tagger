"""The `lyrics-decide` command: list saved options, choose one, or skip a track."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from music_tagger.cli import _lyrics_batch, _lyrics_decide
from music_tagger.lyrics_batch import load_decisions, load_pending
from music_tagger.tags import read_album
from tests.conftest import FIXTURES
from tests.lyrics_fakes import catalog, no_network, record, responder, use_client


def _add_album(root: Path, name: str) -> Path:
    album = root / "Eagles" / name
    shutil.copytree(FIXTURES / "album_flac", album)
    return album


def _sidecars(album: Path, suffix: str) -> list[str]:
    return sorted(p.name for p in album.glob(f"*{suffix}"))


def _exit_code(run: Callable[[], None]) -> int:
    try:
        run()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _batch(root: Path, pending: Path, decisions: Path) -> int:
    return _exit_code(
        lambda: _lyrics_batch(
            [str(root), "--pending", str(pending), "--decisions", str(decisions), "--no-log"]
        )
    )


def _decide(argv: list[str], pending: Path, decisions: Path) -> int:
    return _exit_code(
        lambda: _lyrics_decide([*argv, "--pending", str(pending), "--decisions", str(decisions)])
    )


def _ambiguous_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    """Two albums' worth of tracks: Desperado has two album matches, so it is pending."""
    root = tmp_path / "library"
    album = _add_album(root, "One")
    records = catalog(album)
    album_name = read_album(album).tracks[0].tags["album"]
    records["Desperado"].append(record(900, "Desperado", album_name))
    use_client(monkeypatch, responder(records))
    pending = tmp_path / "pending.json"
    decisions = tmp_path / "decisions.json"
    assert _batch(root, pending, decisions) == 3
    return root, album, pending, decisions


class TestList:
    def test_shows_undecided_options_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        no_network(monkeypatch)

        code = _decide(["list"], pending, decisions)

        assert code == 0
        out = capsys.readouterr().out
        assert "### track 1 (01 - Track 1.flac): ambiguous" in out
        assert "- id 101:" in out
        assert "- id 900:" in out
        assert "album matches" in out

    def test_hides_decided_tracks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        assert (
            _decide(
                [
                    "skip",
                    str(tmp_path / "library" / "Eagles" / "One"),
                    "--track",
                    "1",
                    "--reason",
                    "no lyric exists",
                ],
                pending,
                decisions,
            )
            == 0
        )
        capsys.readouterr()

        code = _decide(["list"], pending, decisions)

        assert code == 0
        assert "No undecided lyric tracks." in capsys.readouterr().out


class TestChoose:
    def test_writes_the_chosen_record_and_records_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        code = _decide(["choose", str(album), "--track", "1", "--id", "900"], pending, decisions)

        assert code == 0
        assert _sidecars(album, ".lrc").count("01 - Track 1.lrc") == 1
        [decision] = load_decisions(decisions).values()
        assert decision.choice == 900
        assert decision.file == "01 - Track 1.flac"

    def test_a_later_batch_does_not_search_a_chosen_track(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        _decide(["choose", str(album), "--track", "1", "--id", "900"], pending, decisions)

        assert _batch(root, pending, decisions) == 0
        assert load_pending(pending) == []

    def test_refuses_a_record_that_was_not_saved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        code = _decide(["choose", str(album), "--track", "1", "--id", "555"], pending, decisions)

        assert code == 2
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        assert not decisions.exists() or load_decisions(decisions) == {}

    def test_dry_run_writes_nothing_and_records_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        code = _decide(
            ["choose", str(album), "--track", "1", "--id", "900", "--dry-run"],
            pending,
            decisions,
        )

        assert code == 0
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        assert load_decisions(decisions) == {}

    def test_refuses_a_track_with_no_pending_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        # Track 2 already has a sidecar, so the batch saved no options for it.
        code = _decide(["choose", str(album), "--track", "2", "--id", "102"], pending, decisions)

        assert code == 2
        assert load_decisions(decisions) == {}

    def test_refuses_to_change_a_decided_track(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        _decide(["choose", str(album), "--track", "1", "--id", "900"], pending, decisions)

        code = _decide(["choose", str(album), "--track", "1", "--id", "101"], pending, decisions)

        assert code == 2
        [decision] = load_decisions(decisions).values()
        assert decision.choice == 900

    def test_plain_writes_txt_even_when_the_record_has_synced_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        code = _decide(
            ["choose", str(album), "--track", "1", "--id", "900", "--plain"], pending, decisions
        )

        assert code == 0
        assert "01 - Track 1.txt" in _sidecars(album, ".txt")
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        [decision] = load_decisions(decisions).values()
        assert decision.reason == "chose record 900 (plain text)"

    def test_replace_skip_replaces_an_earlier_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        _decide(
            ["skip", str(album), "--track", "1", "--reason", "no plausible record"],
            pending,
            decisions,
        )

        refused = _decide(["choose", str(album), "--track", "1", "--id", "900"], pending, decisions)
        code = _decide(
            ["choose", str(album), "--track", "1", "--id", "900", "--replace-skip"],
            pending,
            decisions,
        )

        assert refused == 2
        assert code == 0
        [decision] = load_decisions(decisions).values()
        assert decision.choice == 900
        assert decision.reason == "chose record 900; replaces skip: no plausible record"

    def test_replace_skip_never_replaces_a_choice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        _decide(["choose", str(album), "--track", "1", "--id", "900"], pending, decisions)

        code = _decide(
            ["choose", str(album), "--track", "1", "--id", "101", "--replace-skip"],
            pending,
            decisions,
        )

        assert code == 2
        [decision] = load_decisions(decisions).values()
        assert decision.choice == 900


class TestSkip:
    def test_records_the_reason_and_writes_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        code = _decide(
            ["skip", str(album), "--track", "1", "--reason", "no lyric exists"],
            pending,
            decisions,
        )

        assert code == 0
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        [decision] = load_decisions(decisions).values()
        assert decision.choice is None
        assert decision.reason == "no lyric exists"

    def test_refuses_an_empty_reason(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)

        code = _decide(["skip", str(album), "--track", "1", "--reason", "   "], pending, decisions)

        assert code == 2
        assert load_decisions(decisions) == {}

    def test_a_skipped_track_is_not_searched_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, album, pending, decisions = _ambiguous_setup(tmp_path, monkeypatch)
        _decide(
            ["skip", str(album), "--track", "1", "--reason", "no lyric exists"],
            pending,
            decisions,
        )
        seen: list[httpx.Request] = []
        inner = responder(catalog(album))

        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return inner(request)

        use_client(monkeypatch, recording)

        assert _batch(root, pending, decisions) == 0
        assert all(r.url.params.get("track_name") != "Desperado" for r in seen)


def _shared_number_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    """Two files numbered 1 and titled Desperado, so both are pending with the same options."""
    root = tmp_path / "library"
    album = _add_album(root, "One")
    shutil.copy(album / "01 - Track 1.flac", album / "01 - Track 1 copy.flac")
    records = catalog(album)
    album_name = read_album(album).tracks[0].tags["album"]
    records["Desperado"].append(record(900, "Desperado", album_name))
    use_client(monkeypatch, responder(records))
    pending = tmp_path / "pending.json"
    decisions = tmp_path / "decisions.json"
    assert _batch(root, pending, decisions) == 3
    return root, album, pending, decisions


class TestSelectByFile:
    def test_chooses_the_named_file_when_numbers_are_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _shared_number_setup(tmp_path, monkeypatch)

        code = _decide(
            ["choose", str(album), "--file", "01 - Track 1 copy.flac", "--id", "900"],
            pending,
            decisions,
        )

        assert code == 0
        [decision] = load_decisions(decisions).values()
        assert decision.file == "01 - Track 1 copy.flac"
        assert decision.choice == 900

    def test_a_shared_track_number_alone_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _shared_number_setup(tmp_path, monkeypatch)

        code = _decide(["choose", str(album), "--track", "1", "--id", "900"], pending, decisions)

        assert code == 2
        assert load_decisions(decisions) == {}

    def test_skips_the_named_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _, album, pending, decisions = _shared_number_setup(tmp_path, monkeypatch)

        code = _decide(
            [
                "skip",
                str(album),
                "--file",
                "01 - Track 1 copy.flac",
                "--reason",
                "merged file",
            ],
            pending,
            decisions,
        )

        assert code == 0
        [decision] = load_decisions(decisions).values()
        assert decision.file == "01 - Track 1 copy.flac"
        assert decision.choice is None
        assert decision.reason == "merged file"

    def test_a_file_number_that_disagrees_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _shared_number_setup(tmp_path, monkeypatch)

        code = _decide(
            [
                "choose",
                str(album),
                "--file",
                "01 - Track 1 copy.flac",
                "--track",
                "2",
                "--id",
                "900",
            ],
            pending,
            decisions,
        )

        assert code == 2
        assert load_decisions(decisions) == {}

    def test_a_file_not_in_the_album_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _shared_number_setup(tmp_path, monkeypatch)

        code = _decide(
            ["choose", str(album), "--file", "missing.flac", "--id", "900"], pending, decisions
        )

        assert code == 2
        assert load_decisions(decisions) == {}

    def test_a_track_or_a_file_is_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, album, pending, decisions = _shared_number_setup(tmp_path, monkeypatch)

        code = _decide(["choose", str(album), "--id", "900"], pending, decisions)

        assert code == 2
        assert load_decisions(decisions) == {}
