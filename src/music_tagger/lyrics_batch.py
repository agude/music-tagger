"""Lyric pending options and operator decisions, stored as JSON.

`lyrics-batch` saves every track it cannot settle, with each candidate LRCLIB
record's ID and evidence, to the pending file. `lyrics-decide` shows those
options and records a choice or a skip in the decisions file. Both files are
written only by commands, never by hand.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lyrics import MISSING, LyricsKind

# Track statuses that already hold lyrics. The batch leaves these alone.
HAS_LYRICS = frozenset({LyricsKind.SYNCHRONIZED.value, LyricsKind.PLAIN.value})
# Track statuses with no usable sidecar. The batch searches LRCLIB for these.
# Any other status (malformed, unreadable) is saved for inspection, never overwritten.
NEEDS_LOOKUP = frozenset({MISSING, LyricsKind.EMPTY.value})

# Lookup outcomes that need an operator judgment before the track can be written.
QUEUED_OUTCOMES = frozenset({"ambiguous", "review", "missing_metadata"})
INSPECTION_LABEL = "needs_inspection"

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Candidate:
    id: int
    title: str
    artist: str
    album: str
    duration_delta: float | None  # record minus audio, in seconds
    plausible: bool
    album_matches: bool
    offers: tuple[str, ...]  # any of: synced, plain, instrumental
    notes: tuple[str, ...]
    # Seconds the record's last synced line falls after the audio ends (negative: before).
    last_line_delta: float | None = None


@dataclass(frozen=True)
class PendingEntry:
    album: str  # absolute album directory
    file: str  # audio file name
    track: int | None
    disc: int | None
    outcome: str  # a lookup outcome, or INSPECTION_LABEL
    reason: str
    candidates: tuple[Candidate, ...] = ()


@dataclass
class AlbumReport:
    directory: Path
    counts: Counter[str] = field(default_factory=Counter)
    pending: list[PendingEntry] = field(default_factory=list)
    failed: bool = False
    error: str = ""


# An album match whose length differs from the audio by more than this is not auto-chosen.
AUTO_LENGTH_SECS = 5.0


def confident_choice(entry: PendingEntry) -> Candidate | None:
    """The candidate the rules accept without a judgment, or None.

    A plausible album match with synchronized text is taken, closest length first.
    Otherwise an album match with synchronized text is taken when its length is within
    AUTO_LENGTH_SECS of the audio and its last synced line falls inside the audio. Plain
    text is never taken: its timing cannot be checked against the audio.
    """

    def length(candidate: Candidate) -> float:
        if candidate.duration_delta is None:
            return math.inf
        return abs(candidate.duration_delta)

    synced = [c for c in entry.candidates if c.album_matches and "synced" in c.offers]
    plausible = [c for c in synced if c.plausible]
    if plausible:
        return min(plausible, key=length)
    fits = [
        c
        for c in synced
        if c.last_line_delta is not None
        and c.last_line_delta <= 0
        and length(c) <= AUTO_LENGTH_SECS
    ]
    if fits:
        return min(fits, key=length)
    return None


@dataclass(frozen=True)
class Decision:
    album: str
    file: str
    track: int | None
    choice: int | None  # the LRCLIB record ID written; None when skipped
    reason: str
    decided: str  # UTC timestamp


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> list[Any] | None:
    """Return the entry list of a schema-1 file, or None when the file does not exist."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        raise ValueError(f"{path} is not a version {SCHEMA_VERSION} lyric file")
    entries: list[Any] = data["entries"]
    return entries


def _candidate_from_json(item: dict[str, Any]) -> Candidate:
    return Candidate(
        id=int(item["id"]),
        title=str(item["title"]),
        artist=str(item["artist"]),
        album=str(item["album"]),
        duration_delta=None if item["duration_delta"] is None else float(item["duration_delta"]),
        plausible=bool(item["plausible"]),
        album_matches=bool(item["album_matches"]),
        offers=tuple(str(o) for o in item["offers"]),
        notes=tuple(str(n) for n in item["notes"]),
        last_line_delta=(
            None if item.get("last_line_delta") is None else float(item["last_line_delta"])
        ),
    )


def _entry_from_json(item: dict[str, Any]) -> PendingEntry:
    return PendingEntry(
        album=str(item["album"]),
        file=str(item["file"]),
        track=None if item["track"] is None else int(item["track"]),
        disc=None if item["disc"] is None else int(item["disc"]),
        outcome=str(item["outcome"]),
        reason=str(item["reason"]),
        candidates=tuple(_candidate_from_json(c) for c in item["candidates"]),
    )


def load_pending(path: Path) -> list[PendingEntry]:
    entries = _read_json(path)
    return [] if entries is None else [_entry_from_json(item) for item in entries]


def save_pending(path: Path, entries: list[PendingEntry], stamp: str) -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "generated": stamp,
        "entries": [_entry_to_json(entry) for entry in entries],
    }
    _write_json(path, payload)


def _entry_to_json(entry: PendingEntry) -> dict[str, Any]:
    return {
        "album": entry.album,
        "file": entry.file,
        "track": entry.track,
        "disc": entry.disc,
        "outcome": entry.outcome,
        "reason": entry.reason,
        "candidates": [
            {
                "id": c.id,
                "title": c.title,
                "artist": c.artist,
                "album": c.album,
                "duration_delta": c.duration_delta,
                "plausible": c.plausible,
                "album_matches": c.album_matches,
                "offers": list(c.offers),
                "notes": list(c.notes),
                "last_line_delta": c.last_line_delta,
            }
            for c in entry.candidates
        ],
    }


def load_decisions(path: Path) -> dict[tuple[str, str], Decision]:
    entries = _read_json(path)
    decisions: dict[tuple[str, str], Decision] = {}
    for item in entries or []:
        decision = Decision(
            album=str(item["album"]),
            file=str(item["file"]),
            track=None if item["track"] is None else int(item["track"]),
            choice=None if item["choice"] is None else int(item["choice"]),
            reason=str(item["reason"]),
            decided=str(item["decided"]),
        )
        decisions[(decision.album, decision.file)] = decision
    return decisions


def save_decisions(path: Path, decisions: dict[tuple[str, str], Decision]) -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "entries": [
            {
                "album": d.album,
                "file": d.file,
                "track": d.track,
                "choice": d.choice,
                "reason": d.reason,
                "decided": d.decided,
            }
            for d in sorted(decisions.values(), key=lambda d: (d.album, d.file))
        ],
    }
    _write_json(path, payload)


def merge_pending(
    previous: list[PendingEntry], fresh: list[PendingEntry], processed: set[str]
) -> list[PendingEntry]:
    """Replace the entries of albums this run processed; keep the entries of the rest."""
    kept = [entry for entry in previous if entry.album not in processed]
    return kept + fresh


def undecided(
    entries: list[PendingEntry], decisions: dict[tuple[str, str], Decision]
) -> list[PendingEntry]:
    return [entry for entry in entries if (entry.album, entry.file) not in decisions]


def _track_label(entry: PendingEntry) -> str:
    if entry.track is None:
        return entry.file
    disc = f" disc {entry.disc}" if entry.disc not in (None, 1) else ""
    return f"track {entry.track}{disc} ({entry.file})"


def _last_line_text(delta: float | None) -> str:
    if delta is None:
        return "no synced text"
    if delta > 0:
        return f"last line {delta:.1f}s past the audio end"
    return f"last line {-delta:.1f}s before the audio end"


def _candidate_line(candidate: Candidate) -> str:
    delta = (
        "duration unknown"
        if candidate.duration_delta is None
        else (f"{candidate.duration_delta:+.1f}s")
    )
    flags = [
        "plausible" if candidate.plausible else "not plausible",
        "album matches" if candidate.album_matches else "album differs",
    ]
    offers = ", ".join(candidate.offers) or "no text"
    line = (
        f'- id {candidate.id}: "{candidate.title}" / {candidate.artist} / '
        f"{candidate.album} | {delta} | {', '.join(flags)} | {offers} | "
        f"{_last_line_text(candidate.last_line_delta)}"
    )
    return line


def render_pending(entries: list[PendingEntry]) -> str:
    """Markdown view of pending entries for the operator or an LLM. Read-only."""
    if not entries:
        return "No undecided lyric tracks.\n"
    lines: list[str] = []
    current = ""
    for entry in sorted(entries, key=lambda e: (e.album, e.track or 0, e.file)):
        if entry.album != current:
            current = entry.album
            lines += [f"## {current}", ""]
        lines.append(f"### {_track_label(entry)}: {entry.outcome}")
        lines.append(entry.reason)
        for candidate in entry.candidates:
            lines.append(_candidate_line(candidate))
        if not entry.candidates:
            lines.append("(no candidates saved)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
