from __future__ import annotations

from pathlib import Path

import pytest

from music_tagger.checklist import Checklist, ChecklistEntry
from music_tagger.cli import _checklist, _scan

SAMPLE = (
    "# music-tagger checklist\n"
    "\n"
    "3 albums to process.\n"
    "\n"
    "- [x] `/music/Air Supply/Forever Love (2003)` — Air Supply — Forever Love (36 tracks) "
    "[f7890790-ff4b-4524-9298-5c3bc66af976]\n"
    "- [x] `/music/Boston/Boston (1976)` — Boston — Boston (1 tracks) [NO MB ID] "
    "**SKIPPED: partial album — tagger maps files sequentially, would assign wrong track**\n"
    "- [ ] `/music/Eagles/Desperado (1973)` — Eagles — Desperado (3 tracks) [NO MB ID]\n"
)


@pytest.fixture()
def checklist_file(default_checklist: Path) -> Path:
    default_checklist.write_text(SAMPLE)
    return default_checklist


class TestChecklistFormat:
    def test_round_trips_existing_file(self) -> None:
        assert Checklist.parse(SAMPLE).format() == SAMPLE

    def test_parses_status_and_reason(self) -> None:
        entries = Checklist.parse(SAMPLE).entries

        assert [entry.status for entry in entries] == ["done", "skipped", "todo"]
        assert entries[1].reason.startswith("partial album")
        assert entries[2].path == "/music/Eagles/Desperado (1973)"

    def test_rejects_unrecognized_line(self) -> None:
        with pytest.raises(ValueError, match="line 7"):
            Checklist.parse(SAMPLE.replace("- [ ] `/music/Eagles", "* [ ] `/music/Eagles"))

    def test_header_counts_entries(self) -> None:
        checklist = Checklist.parse(SAMPLE)
        checklist.entries.append(ChecklistEntry("/music/X", "X — Y (1 tracks) [NO MB ID]"))

        assert "4 albums to process." in checklist.format()


class TestChecklistCommand:
    def test_next_prints_first_unchecked(self, checklist_file: Path, capsys: object) -> None:
        _checklist(["next"])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert out.splitlines() == [
            "Next: /music/Eagles/Desperado (1973)",
            "Album: Eagles — Desperado (3 tracks) [NO MB ID]",
            "Remaining: 1",
        ]

    def test_next_reports_completion(self, checklist_file: Path, capsys: object) -> None:
        _checklist(["done", "/music/Eagles/Desperado (1973)"])
        capsys.readouterr()  # type: ignore[attr-defined]

        _checklist(["next"])

        assert capsys.readouterr().out == "Checklist complete.\n"  # type: ignore[attr-defined]

    def test_done_marks_entry(self, checklist_file: Path) -> None:
        _checklist(["done", "/music/Eagles/Desperado (1973)"])

        assert "- [x] `/music/Eagles/Desperado (1973)`" in checklist_file.read_text()

    def test_done_accepts_trailing_slash(self, checklist_file: Path) -> None:
        _checklist(["done", "/music/Eagles/Desperado (1973)/"])

        assert "- [x] `/music/Eagles/Desperado (1973)`" in checklist_file.read_text()

    def test_unknown_album_exits_without_writing(
        self, checklist_file: Path, capsys: object
    ) -> None:
        with pytest.raises(SystemExit) as error:
            _checklist(["done", "/music/Nobody/Nothing"])

        assert error.value.code == 1
        assert checklist_file.read_text() == SAMPLE
        assert "/music/Nobody/Nothing" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_skip_records_reason(self, checklist_file: Path) -> None:
        _checklist(["skip", "/music/Eagles/Desperado (1973)", "--reason", "not in MusicBrainz"])

        assert (
            "- [x] `/music/Eagles/Desperado (1973)` — Eagles — Desperado (3 tracks) "
            "[NO MB ID] **SKIPPED: not in MusicBrainz**"
        ) in checklist_file.read_text()

    def test_skip_requires_reason(self, checklist_file: Path) -> None:
        with pytest.raises(SystemExit) as error:
            _checklist(["skip", "/music/Eagles/Desperado (1973)"])

        assert error.value.code == 2

    def test_reopen_clears_status_and_reason(self, checklist_file: Path) -> None:
        _checklist(["reopen", "/music/Boston/Boston (1976)"])

        assert (
            "- [ ] `/music/Boston/Boston (1976)` — Boston — Boston (1 tracks) [NO MB ID]\n"
        ) in checklist_file.read_text()

    def test_reopen_matching_reason(self, checklist_file: Path, capsys: object) -> None:
        _checklist(["reopen", "--matching", "maps files sequentially"])

        text = checklist_file.read_text()
        assert "- [ ] `/music/Boston/Boston (1976)`" in text
        assert "- [x] `/music/Air Supply/Forever Love (2003)`" in text
        assert "Reopened 1 entry" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_reopen_requires_path_or_matching(self, checklist_file: Path) -> None:
        with pytest.raises(SystemExit) as error:
            _checklist(["reopen"])

        assert error.value.code == 2

    def test_explicit_file(self, tmp_path: Path, default_checklist: Path) -> None:
        other = tmp_path / "other.md"
        other.write_text(SAMPLE)

        _checklist(["done", "/music/Eagles/Desperado (1973)", "--file", str(other)])

        assert "- [x] `/music/Eagles/Desperado (1973)`" in other.read_text()
        assert not default_checklist.exists()


class TestScanMarkdown:
    def test_writes_markdown_checklist(self, flac_album: Path, tmp_path: Path) -> None:
        out = tmp_path / "checklist.md"

        _scan([str(flac_album.parent), "--all", "-o", str(out)])

        assert out.read_text() == (
            "# music-tagger checklist\n\n1 albums to process.\n\n"
            f"- [ ] `{flac_album}` — Eagles — Desperado (3 tracks) [test-album-id-1]\n"
        )

    def test_rescan_keeps_existing_status(self, flac_album: Path, tmp_path: Path) -> None:
        out = tmp_path / "checklist.md"
        existing = (
            "# music-tagger checklist\n\n1 albums to process.\n\n"
            "- [x] `/music/Old/Album` — Old — Album (2 tracks) [NO MB ID] **SKIPPED: gone**\n"
        )
        out.write_text(existing)

        _scan([str(flac_album.parent), "--all", "-o", str(out)])

        text = out.read_text()
        assert "2 albums to process." in text
        assert "**SKIPPED: gone**" in text
        assert f"- [ ] `{flac_album}`" in text

    def test_rescan_does_not_duplicate(self, flac_album: Path, tmp_path: Path) -> None:
        out = tmp_path / "checklist.md"
        _scan([str(flac_album.parent), "--all", "-o", str(out)])
        _checklist(["done", str(flac_album), "--file", str(out)])

        _scan([str(flac_album.parent), "--all", "-o", str(out)])

        assert out.read_text().count(str(flac_album)) == 1
        assert f"- [x] `{flac_album}`" in out.read_text()
