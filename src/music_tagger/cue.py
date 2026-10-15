"""Keep CUE sheet FILE references in step with renamed audio files.

Whipper's CUE sheets name each track's file. Renaming a FLAC without
rewriting its FILE line leaves a CUE that points at nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

_FILE_LINE = re.compile(r'^(\s*FILE\s+")([^"]+)(".*)$')
# CUE sheets are usually UTF-8 but older rippers wrote Latin-1;
# surrogateescape round-trips any bytes this module does not change.
_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def cue_sheets(album_dir: Path) -> list[Path]:
    return sorted(path for path in album_dir.iterdir() if path.suffix.lower() == ".cue")


def rewrite_cue_references(album_dir: Path, renames: Mapping[str, str]) -> list[Path]:
    """Point FILE lines at renamed files. `renames` maps old to new file names.

    Returns the CUE sheets that changed. Line endings are preserved.
    """
    changed: list[Path] = []
    for cue in cue_sheets(album_dir):
        with open(cue, encoding=_ENCODING, errors=_ERRORS, newline="") as f:
            lines = f.read().splitlines(keepends=True)

        rewritten = [_rewrite_line(line, renames) for line in lines]
        if rewritten == lines:
            continue
        with open(cue, "w", encoding=_ENCODING, errors=_ERRORS, newline="") as f:
            f.write("".join(rewritten))
        changed.append(cue)
    return changed


def missing_cue_references(album_dir: Path) -> list[tuple[Path, str]]:
    """(CUE sheet, file name) for each FILE line naming a file not in album_dir."""
    missing: list[tuple[Path, str]] = []
    for cue in cue_sheets(album_dir):
        with open(cue, encoding=_ENCODING, errors=_ERRORS) as f:
            for line in f:
                match = _FILE_LINE.match(line.rstrip("\r\n"))
                if match and not (album_dir / match[2]).exists():
                    missing.append((cue, match[2]))
    return missing


def _rewrite_line(line: str, renames: Mapping[str, str]) -> str:
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    match = _FILE_LINE.match(body)
    if not match or match[2] not in renames:
        return line
    return f"{match[1]}{renames[match[2]]}{match[3]}{ending}"
