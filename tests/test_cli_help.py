"""Snapshot tests for every command's and subcommand's --help output.

The snapshots pin the help text so the CLI can be restructured without
changing what operators see. Regenerate them, only after a deliberate help
change, with::

    MUSIC_TAGGER_UPDATE_HELP=1 uv run pytest tests/test_cli_help.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from music_tagger.cli import main

HELP_DIR = Path(__file__).parent / "fixtures" / "help"

# (snapshot name, argv). Every entry is run with --help in-process.
# Keep this list in step with main()'s dispatch table and the mb/nd sub-dispatchers.
HELP_CASES: list[tuple[str, list[str]]] = [
    ("music-tagger", ["--help"]),
    ("read", ["read", "--help"]),
    ("toc", ["toc", "--help"]),
    ("mb", ["mb", "--help"]),
    ("mb-search", ["mb", "search", "--help"]),
    ("mb-release", ["mb", "release", "--help"]),
    ("mb-discid", ["mb", "discid", "--help"]),
    ("match", ["match", "--help"]),
    ("diff", ["diff", "--help"]),
    ("write-tags", ["write-tags", "--help"]),
    ("write-ratings", ["write-ratings", "--help"]),
    ("write-stats", ["write-stats", "--help"]),
    ("path-for", ["path-for", "--help"]),
    ("copy", ["copy", "--help"]),
    ("art", ["art", "--help"]),
    ("nd", ["nd", "--help"]),
    ("nd-rescan", ["nd", "rescan", "--help"]),
    ("nd-ratings", ["nd", "ratings", "--help"]),
    ("nd-stats", ["nd", "stats", "--help"]),
    ("nd-set-rating", ["nd", "set-rating", "--help"]),
    ("scan", ["scan", "--help"]),
    ("checklist", ["checklist", "--help"]),
    ("candidates", ["candidates", "--help"]),
    ("tag", ["tag", "--help"]),
    ("apply", ["apply", "--help"]),
    ("place", ["place", "--help"]),
    ("genre", ["genre", "--help"]),
    ("lyrics", ["lyrics", "--help"]),
    ("lyrics-batch", ["lyrics-batch", "--help"]),
    ("lyrics-decide", ["lyrics-decide", "--help"]),
    ("lock", ["lock", "--help"]),
    ("unlock", ["unlock", "--help"]),
    ("rename", ["rename", "--help"]),
    ("replaygain", ["replaygain", "--help"]),
    ("rip", ["rip", "--help"]),
]


# The snapshots hold Python 3.13+ argparse output. Python 3.12 indents and
# lists option metavars differently, so the text cannot match there.
@pytest.mark.skipif(sys.version_info < (3, 13), reason="argparse help format changed in 3.13")
@pytest.mark.parametrize(("name", "argv"), HELP_CASES, ids=[name for name, _ in HELP_CASES])
def test_help_matches_snapshot(
    name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # argparse wraps help text to the terminal width; pin it so snapshots are stable.
    monkeypatch.setenv("COLUMNS", "80")

    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    assert exc.value.code == 0

    out = capsys.readouterr().out
    path = HELP_DIR / f"{name}.txt"
    if os.environ.get("MUSIC_TAGGER_UPDATE_HELP") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8")
        return
    assert out == path.read_text(encoding="utf-8")
