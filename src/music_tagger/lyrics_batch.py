"""Lyric pending options and operator decisions, stored as JSON.

`lyrics-batch` saves every track it cannot settle, with each candidate LRCLIB
record's ID and evidence, to the pending file. `lyrics-decide` shows those
options and records a choice or a skip in the decisions file. Both files are
written only by commands, never by hand.
"""

from __future__ import annotations

import json
import math
from collections import Counter, UserDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text
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
    # The artist and the title match the track. Entries saved before this field read as False.
    identity_matches: bool = False


@dataclass(frozen=True)
class PendingEntry:
    album: str  # absolute album directory
    file: str  # audio file name
    track: int | None
    disc: int | None
    outcome: str  # a lookup outcome, or INSPECTION_LABEL
    reason: str
    candidates: tuple[Candidate, ...] = ()
    # The file's musicbrainz_releasetrackid, or "". Entries written before IDs read as "".
    release_track_id: str = ""


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
    Otherwise an album match with matching artist and title and synchronized text is
    taken when its length is within AUTO_LENGTH_SECS of the audio and its last synced
    line falls inside the audio. Plain text is never taken: its timing cannot be checked
    against the audio.
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
        if c.identity_matches
        and c.last_line_delta is not None
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
    # The file's musicbrainz_releasetrackid, or "". Decisions written before IDs read as "".
    release_track_id: str = ""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_entries(path: Path) -> list[dict[str, Any]] | None:
    """Return the entry objects of a schema-1 file, or None when the file does not exist.

    Raises ValueError, naming the file and entry, for any shape the loaders cannot read.
    """
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    version = data.get("version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ValueError(f"{path} is not a version {SCHEMA_VERSION} lyric file")
    raw = data.get("entries")
    if not isinstance(raw, list):
        raise ValueError(f"{path}: entries: expected a list")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: entry {index}: expected an object")
        entries.append(item)
    return entries


def _value(item: dict[str, Any], key: str, where: str) -> Any:
    if key not in item:
        raise ValueError(f"{where}: {key}: missing")
    return item[key]


def _text(item: dict[str, Any], key: str, where: str) -> str:
    value = _value(item, key, where)
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key}: expected a string, got {value!r}")
    return value


def _int(item: dict[str, Any], key: str, where: str) -> int:
    value = _value(item, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key}: expected an integer, got {value!r}")
    return value


def _optional_int(item: dict[str, Any], key: str, where: str) -> int | None:
    if _value(item, key, where) is None:
        return None
    return _int(item, key, where)


def _flag(item: dict[str, Any], key: str, where: str) -> bool:
    value = _value(item, key, where)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key}: expected true or false, got {value!r}")
    return value


def _seconds(item: dict[str, Any], key: str, where: str) -> float:
    value = _value(item, key, where)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{where}: {key}: expected a finite number, got {value!r}")
    return float(value)


def _optional_seconds(item: dict[str, Any], key: str, where: str) -> float | None:
    if _value(item, key, where) is None:
        return None
    return _seconds(item, key, where)


def _texts(item: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = _value(item, key, where)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where}: {key}: expected a list of strings")
    return tuple(value)


def _objects(item: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    value = _value(item, key, where)
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"{where}: {key}: expected a list of objects")
    return value


def _candidate_from_json(item: dict[str, Any], where: str) -> Candidate:
    # Absent in files written before these fields existed: read as unknown or False.
    return Candidate(
        id=_int(item, "id", where),
        title=_text(item, "title", where),
        artist=_text(item, "artist", where),
        album=_text(item, "album", where),
        duration_delta=_optional_seconds(item, "duration_delta", where),
        plausible=_flag(item, "plausible", where),
        album_matches=_flag(item, "album_matches", where),
        offers=_texts(item, "offers", where),
        notes=_texts(item, "notes", where),
        last_line_delta=(
            _optional_seconds(item, "last_line_delta", where) if "last_line_delta" in item else None
        ),
        identity_matches=(
            _flag(item, "identity_matches", where) if "identity_matches" in item else False
        ),
    )


def _optional_text(item: dict[str, Any], key: str, where: str) -> str:
    """A string, or "" when the key is absent."""
    return _text(item, key, where) if key in item else ""


def _entry_from_json(item: dict[str, Any], where: str) -> PendingEntry:
    candidates = _objects(item, "candidates", where)
    return PendingEntry(
        album=_text(item, "album", where),
        file=_text(item, "file", where),
        track=_optional_int(item, "track", where),
        disc=_optional_int(item, "disc", where),
        outcome=_text(item, "outcome", where),
        reason=_text(item, "reason", where),
        release_track_id=_optional_text(item, "release_track_id", where),
        candidates=tuple(
            _candidate_from_json(c, f"{where}: candidates[{n}]") for n, c in enumerate(candidates)
        ),
    )


def load_pending(path: Path) -> list[PendingEntry]:
    entries = _read_entries(path)
    if entries is None:
        return []
    return [_entry_from_json(item, f"{path}: entry {n}") for n, item in enumerate(entries)]


def save_pending(path: Path, entries: list[PendingEntry], stamp: str) -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "generated": stamp,
        "entries": [_entry_to_json(entry) for entry in entries],
    }
    _write_json(path, payload)


def _entry_to_json(entry: PendingEntry) -> dict[str, Any]:
    data: dict[str, Any] = {
        "album": entry.album,
        "file": entry.file,
        "track": entry.track,
        "disc": entry.disc,
        "outcome": entry.outcome,
        "reason": entry.reason,
    }
    if entry.release_track_id:
        data["release_track_id"] = entry.release_track_id
    data["candidates"] = [
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
            "identity_matches": c.identity_matches,
        }
        for c in entry.candidates
    ]
    return data


DecisionKey = tuple[str, str]


def decision_key(album: str, file: str, release_track_id: str) -> DecisionKey:
    """The key a decision is stored under: its release track ID, else its album and file."""
    return ("release-track", release_track_id) if release_track_id else (album, file)


def same_track(
    stored_album: str,
    stored_file: str,
    stored_release_track_id: str,
    album: str,
    file: str,
    release_track_id: str,
) -> bool:
    """Whether a stored decision or pending entry is the track now at album/file with this ID.

    When both have a release track ID, the IDs alone decide, so a renamed or moved file
    keeps its entry and two files with different IDs never share one. Otherwise the
    album and file name decide, so entries written before IDs keep matching.
    """
    if release_track_id and stored_release_track_id:
        return stored_release_track_id == release_track_id
    return stored_album == album and stored_file == file


class Decisions(UserDict[DecisionKey, Decision]):
    """Stored decisions by key, indexed by release track ID and by album and file.

    `find` answers in constant time what `same_track` would answer by scanning every
    decision, so a library-wide batch does not slow down as decisions accumulate.
    Every mutation goes through __setitem__ and __delitem__, which keep the indexes.
    """

    def __init__(self, initial: Mapping[DecisionKey, Decision] | None = None) -> None:
        self._seq: dict[DecisionKey, int] = {}
        self._next = 0
        self._by_id: dict[str, list[DecisionKey]] = {}
        self._by_path: dict[tuple[str, str], list[DecisionKey]] = {}
        super().__init__(initial)

    def __setitem__(self, key: DecisionKey, decision: Decision) -> None:
        if key in self.data:
            self._unindex(key, self.data[key])
        else:
            self._seq[key] = self._next
            self._next += 1
        self.data[key] = decision
        if decision.release_track_id:
            self._by_id.setdefault(decision.release_track_id, []).append(key)
        self._by_path.setdefault((decision.album, decision.file), []).append(key)

    def __delitem__(self, key: DecisionKey) -> None:
        self._unindex(key, self.data[key])
        del self.data[key]
        del self._seq[key]

    def _unindex(self, key: DecisionKey, decision: Decision) -> None:
        _remove_key(self._by_id, decision.release_track_id, key)
        _remove_key(self._by_path, (decision.album, decision.file), key)

    def find(
        self, album: str, file: str, release_track_id: str
    ) -> tuple[DecisionKey, Decision] | None:
        """The first stored decision, in insertion order, that `same_track` matches."""
        path_keys = self._by_path.get((album, file), [])
        if release_track_id:
            candidates = [k for k in path_keys if not self.data[k].release_track_id]
            candidates += self._by_id.get(release_track_id, [])
        else:
            candidates = list(path_keys)
        if not candidates:
            return None
        key = min(candidates, key=self._seq.__getitem__)
        return key, self.data[key]


def _remove_key[K](index: dict[K, list[DecisionKey]], name: K, key: DecisionKey) -> None:
    keys = index.get(name)
    if keys is None or key not in keys:
        return
    keys.remove(key)
    if not keys:
        del index[name]


def find_decision(
    decisions: Decisions, album: str, file: str, release_track_id: str
) -> tuple[DecisionKey, Decision] | None:
    """The stored decision for this track, with the key it is stored under, or None."""
    return decisions.find(album, file, release_track_id)


def find_pending(
    entries: list[PendingEntry], album: str, file: str, release_track_id: str
) -> PendingEntry | None:
    """The pending entry for this track, or None."""
    return next(
        (
            entry
            for entry in entries
            if same_track(
                entry.album, entry.file, entry.release_track_id, album, file, release_track_id
            )
        ),
        None,
    )


def load_decisions(path: Path) -> Decisions:
    entries = _read_entries(path) or []
    decisions = Decisions()
    for n, item in enumerate(entries):
        where = f"{path}: entry {n}"
        decision = Decision(
            album=_text(item, "album", where),
            file=_text(item, "file", where),
            track=_optional_int(item, "track", where),
            choice=_optional_int(item, "choice", where),
            reason=_text(item, "reason", where),
            decided=_text(item, "decided", where),
            release_track_id=_optional_text(item, "release_track_id", where),
        )
        key = decision_key(decision.album, decision.file, decision.release_track_id)
        decisions[key] = decision
    return decisions


def save_decisions(path: Path, decisions: Mapping[DecisionKey, Decision]) -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "entries": [
            _decision_to_json(d)
            for d in sorted(decisions.values(), key=lambda d: (d.album, d.file))
        ],
    }
    _write_json(path, payload)


def _decision_to_json(decision: Decision) -> dict[str, Any]:
    data: dict[str, Any] = {
        "album": decision.album,
        "file": decision.file,
        "track": decision.track,
        "choice": decision.choice,
        "reason": decision.reason,
        "decided": decision.decided,
    }
    if decision.release_track_id:
        data["release_track_id"] = decision.release_track_id
    return data


def merge_pending(
    previous: list[PendingEntry], fresh: list[PendingEntry], processed: set[str]
) -> list[PendingEntry]:
    """Replace the entries of albums this run processed; keep the entries of the rest."""
    kept = [entry for entry in previous if entry.album not in processed]
    return kept + fresh


def undecided(entries: list[PendingEntry], decisions: Decisions) -> list[PendingEntry]:
    return [
        entry
        for entry in entries
        if find_decision(decisions, entry.album, entry.file, entry.release_track_id) is None
    ]


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
