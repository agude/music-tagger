"""Smoke and structural tests for every command's and subcommand's --help.

The tests check that help is printed and that each parser is well formed.
They do not pin the help text, so the CLI can change its wording or layout
without a test edit.
"""

from __future__ import annotations

import argparse
import re
from typing import NoReturn

import pytest

from music_tagger.cli import _register_commands, main

# argv for every command and subcommand, each ending in --help.
HELP_CASES: list[list[str]] = [
    ["--help"],
    ["read", "--help"],
    ["toc", "--help"],
    ["mb", "--help"],
    ["mb", "search", "--help"],
    ["mb", "release", "--help"],
    ["mb", "discid", "--help"],
    ["match", "--help"],
    ["diff", "--help"],
    ["write-tags", "--help"],
    ["edit-tags", "--help"],
    ["write-ratings", "--help"],
    ["write-stats", "--help"],
    ["path-for", "--help"],
    ["copy", "--help"],
    ["art", "--help"],
    ["nd", "--help"],
    ["nd", "rescan", "--help"],
    ["nd", "ratings", "--help"],
    ["nd", "stats", "--help"],
    ["nd", "set-rating", "--help"],
    ["scan", "--help"],
    ["checklist", "--help"],
    ["candidates", "--help"],
    ["tag", "--help"],
    ["apply", "--help"],
    ["place", "--help"],
    ["genre", "--help"],
    ["lyrics", "--help"],
    ["lyrics-batch", "--help"],
    ["lyrics-decide", "--help"],
    ["batch-dry-run", "--help"],
    ["lock", "--help"],
    ["unlock", "--help"],
    ["rename", "--help"],
    ["replaygain", "--help"],
    ["rip", "--help"],
    ["identify", "--help"],
]

# Dispatchers route on a sub-argument and build no parser of their own, so
# the parser capture below cannot reach them. Their subcommands are captured.
DISPATCHER_ARGV: list[list[str]] = [["mb"], ["nd"]]

# The same commands with --help removed. Calling each with [] reaches its parser.
PARSER_CASES: list[list[str]] = [
    argv[:-1] for argv in HELP_CASES if argv[:-1] and argv[:-1] not in DISPATCHER_ARGV
]


class _ParserCaptured(Exception):
    """Raised in place of parse_args so the test receives the parser."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__()
        self.parser = parser


def _capture_parser(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> argparse.ArgumentParser:
    def fake_parse_args(
        self: argparse.ArgumentParser,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> NoReturn:
        raise _ParserCaptured(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)
    with pytest.raises(_ParserCaptured) as exc:
        main(list(argv))
    return exc.value.parser


def _real_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Actions other than the automatic -h/--help."""
    return [a for a in parser._actions if not isinstance(a, argparse._HelpAction)]


@pytest.mark.parametrize("argv", HELP_CASES, ids=[" ".join(argv) for argv in HELP_CASES])
def test_help_prints_usage_and_exits_zero(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_top_level_help_names_every_dispatched_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for name in _register_commands():
        assert re.search(rf"^  {re.escape(name)}\s", out, re.MULTILINE), name


def test_help_cases_cover_every_dispatched_command() -> None:
    covered = {argv[0] for argv in HELP_CASES}
    assert set(_register_commands()) <= covered


@pytest.mark.parametrize("argv", PARSER_CASES, ids=[" ".join(argv) for argv in PARSER_CASES])
def test_parser_is_a_described_music_tagger_parser(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _capture_parser(argv, monkeypatch)
    assert parser.prog.startswith("music-tagger")
    assert parser.description


@pytest.mark.parametrize("argv", PARSER_CASES, ids=[" ".join(argv) for argv in PARSER_CASES])
def test_option_strings_are_not_repeated(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    parser = _capture_parser(argv, monkeypatch)
    option_strings = [opt for action in _real_actions(parser) for opt in action.option_strings]
    assert len(option_strings) == len(set(option_strings))


@pytest.mark.parametrize("argv", PARSER_CASES, ids=[" ".join(argv) for argv in PARSER_CASES])
def test_option_strings_are_dashed_and_kebab_case(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _capture_parser(argv, monkeypatch)
    for action in _real_actions(parser):
        for opt in action.option_strings:
            if opt.startswith("--"):
                assert re.fullmatch(r"--[a-z][a-z0-9]*(-[a-z0-9]+)*", opt), opt
            else:
                assert re.fullmatch(r"-[A-Za-z]", opt), opt


@pytest.mark.parametrize("argv", PARSER_CASES, ids=[" ".join(argv) for argv in PARSER_CASES])
def test_every_positional_has_a_dest(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    parser = _capture_parser(argv, monkeypatch)
    for action in _real_actions(parser):
        if not action.option_strings:
            assert action.dest
            assert action.dest != argparse.SUPPRESS
