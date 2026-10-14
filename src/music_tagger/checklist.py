"""The album checklist: a markdown file of library albums to process.

Each entry is one line:

    - [ ] `<album path>` — <description>
    - [x] `<album path>` — <description>
    - [x] `<album path>` — <description> **SKIPPED: <reason>**
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_TITLE = "# music-tagger checklist"
_COUNT_LINE = re.compile(r"^\d+ albums to process\.$")
_ENTRY_LINE = re.compile(
    r"^- \[(?P<mark>[ x])\] `(?P<path>[^`]+)` — (?P<description>.*?)"
    r"(?: \*\*SKIPPED: (?P<reason>.*)\*\*)?$"
)


def normalize_path(path: str | Path) -> str:
    """Absolute path without a trailing slash, so lookups match scan's entries."""
    return os.path.normpath(os.path.abspath(path))


@dataclass
class ChecklistEntry:
    path: str
    description: str
    status: str = "todo"  # "todo", "done", or "skipped"
    reason: str = ""

    def format(self) -> str:
        mark = " " if self.status == "todo" else "x"
        line = f"- [{mark}] `{self.path}` — {self.description}"
        if self.status == "skipped":
            line += f" **SKIPPED: {self.reason}**"
        return line


@dataclass
class Checklist:
    entries: list[ChecklistEntry] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> Checklist:
        entries = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip() or line == _TITLE or _COUNT_LINE.match(line):
                continue
            match = _ENTRY_LINE.match(line)
            if match is None:
                raise ValueError(f"unrecognized checklist line {number}: {line}")
            reason = match["reason"] or ""
            if match["mark"] == " ":
                status = "todo"
            else:
                status = "skipped" if match["reason"] is not None else "done"
            entries.append(ChecklistEntry(match["path"], match["description"], status, reason))
        return cls(entries)

    @classmethod
    def load(cls, path: Path) -> Checklist:
        return cls.parse(path.read_text()) if path.exists() else cls()

    def format(self) -> str:
        lines = [_TITLE, "", f"{len(self.entries)} albums to process.", ""]
        lines += [entry.format() for entry in self.entries]
        return "\n".join(lines) + "\n"

    def save(self, path: Path) -> None:
        path.write_text(self.format())

    def find(self, album_path: str | Path) -> ChecklistEntry | None:
        wanted = normalize_path(album_path)
        for entry in self.entries:
            if normalize_path(entry.path) == wanted:
                return entry
        return None

    def next_todo(self) -> ChecklistEntry | None:
        return next((entry for entry in self.entries if entry.status == "todo"), None)

    def add_new(self, entries: list[ChecklistEntry]) -> int:
        """Append entries whose path is not listed yet. Returns how many were added."""
        new_entries = [entry for entry in entries if self.find(entry.path) is None]
        self.entries.extend(new_entries)
        return len(new_entries)
