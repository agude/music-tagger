"""LRCLIB lyrics client and per-track candidate policy. Fetch only.

The client makes GET requests and nothing else: it never publishes lyrics.
Candidates come from /api/search, never /api/get, because /api/get returns one
record that LRCLIB picked, which hides the ambiguity the policy must see.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from .lyrics import LyricsKind, classify

BASE_URL = "https://lrclib.net/api"
# LRCLIB asks clients to name the app, its version, and a homepage. The project
# URL is used rather than the operator's email, which is not sent to LRCLIB.
USER_AGENT = "music-tagger/0.1 (https://github.com/agude/music-tagger)"
# The LRCLIB developer-portal Application ID for this client-side app. LRCLIB
# documents it as public and self-asserted, not a credential, so it lives in
# source. It is sent with every request as the Lrclib-App-Id header.
APP_ID = "W02Uia6AAH-CdVxXklrftY9hwTZ3lHIj42dAgjK2Ghk"
CONNECT_TIMEOUT_SECS = 5.0
READ_TIMEOUT_SECS = 10.0
# The registered client-side allowance is about 180 requests/min per IP, and
# LRCLIB says the figure is not a contract. 0.35 s between requests is about
# 170/min, which keeps a margin under it. It is also within the 200-500 ms
# range LRCLIB suggests for batch work.
MIN_INTERVAL_SECS = 0.35
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_SECS = 1.0
# A Retry-After longer than this fails the request now, so one album cannot
# stall a batch for many minutes.
MAX_RETRY_WAIT_SECS = 60.0
# /api/search returns at most this many records and has no pagination.
SEARCH_RESULT_CAP = 20
# A candidate's duration must lie within this many seconds of the audio file.
DURATION_TOLERANCE_SECS = 2.0


class ProviderError(Exception):
    """LRCLIB could not answer after bounded retries, or sent an unusable reply.

    rate_limited is True when the last failure was HTTP 429, so the operator can
    tell throttling from an outage. It is False for everything else.
    """

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class LyricsRecord:
    """One LRCLIB record. Missing text is "", and a missing duration is None."""

    id: int
    track_name: str
    artist_name: str
    album_name: str
    duration_secs: float | None
    instrumental: bool
    plain_lyrics: str
    synced_lyrics: str


@dataclass(frozen=True)
class SearchResult:
    records: list[LyricsRecord]
    truncated: bool


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProviderError(f"LRCLIB field {key} is not text")
    return value


def _parse_record(data: object) -> LyricsRecord:
    if not isinstance(data, dict):
        raise ProviderError("LRCLIB record is not an object")
    record_id = data.get("id")
    if not isinstance(record_id, int) or isinstance(record_id, bool):
        raise ProviderError("LRCLIB record has no integer id")
    duration = data.get("duration")
    if duration is not None and (
        not isinstance(duration, int | float) or isinstance(duration, bool)
    ):
        raise ProviderError("LRCLIB duration is not a number")
    track_name = _text(data, "trackName") or _text(data, "name")
    return LyricsRecord(
        id=record_id,
        track_name=track_name,
        artist_name=_text(data, "artistName"),
        album_name=_text(data, "albumName"),
        duration_secs=float(duration) if duration is not None else None,
        instrumental=data.get("instrumental") is True,
        plain_lyrics=_text(data, "plainLyrics"),
        synced_lyrics=_text(data, "syncedLyrics"),
    )


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After header, or None for a missing or dated value."""
    raw = resp.headers.get("Retry-After", "").strip()
    if raw.isdigit():
        return float(raw)
    return None


class LrclibClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_INTERVAL_SECS,
    ) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Lrclib-App-Id": APP_ID,
            },
            timeout=httpx.Timeout(READ_TIMEOUT_SECS, connect=CONNECT_TIMEOUT_SECS),
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last_request: float | None = None

    def _space_requests(self) -> None:
        if self._last_request is not None and self._min_interval > 0:
            elapsed = self._clock() - self._last_request
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request = self._clock()

    def _request(self, path: str, params: dict[str, str]) -> httpx.Response:
        """GET with spacing and bounded retries.

        Returns the first response whose status is not retryable, so callers
        handle 200 and 404 themselves. Raises ProviderError when every attempt
        fails, or when the server asks for a wait longer than MAX_RETRY_WAIT_SECS.
        """
        reason = ""
        rate_limited = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._space_requests()
            wait = BACKOFF_SECS * 2 ** (attempt - 1)
            try:
                resp = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                reason = f"network error: {exc}"
                rate_limited = False
            else:
                if resp.status_code not in RETRY_STATUS_CODES:
                    return resp
                reason = f"HTTP {resp.status_code}"
                rate_limited = resp.status_code == 429
                wait = _retry_after(resp) or wait
                if wait > MAX_RETRY_WAIT_SECS:
                    raise ProviderError(
                        f"LRCLIB asked to wait {wait:.0f}s ({reason})",
                        rate_limited=rate_limited,
                    )
            if attempt < MAX_ATTEMPTS:
                self._sleep(wait)
        raise ProviderError(
            f"LRCLIB {path} failed after {MAX_ATTEMPTS} attempts ({reason})",
            rate_limited=rate_limited,
        )

    def search(self, *, title: str, artist: str, album: str = "") -> SearchResult:
        params = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        resp = self._request("/search", params)
        if resp.status_code != 200:
            raise ProviderError(f"LRCLIB search returned HTTP {resp.status_code}")
        data = self._json(resp)
        if not isinstance(data, list):
            raise ProviderError("LRCLIB search reply is not a list")
        records = [_parse_record(item) for item in data]
        return SearchResult(records=records, truncated=len(records) >= SEARCH_RESULT_CAP)

    def get(self, record_id: int) -> LyricsRecord | None:
        """Fetch one record by id. None when LRCLIB has no such record (404)."""
        resp = self._request(f"/get/{record_id}", {})
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ProviderError(f"LRCLIB get {record_id} returned HTTP {resp.status_code}")
        return _parse_record(self._json(resp))

    def get_exact(
        self, *, title: str, artist: str, album: str, duration_secs: float
    ) -> LyricsRecord | None:
        """Fetch the record whose title, artist, album, and duration all match (404: None).

        Used only to recover a record that a capped search hid. The caller adds it as a
        candidate; it is never chosen on this lookup alone.
        """
        params = {
            "track_name": title,
            "artist_name": artist,
            "album_name": album,
            "duration": str(round(duration_secs)),
        }
        resp = self._request("/get", params)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ProviderError(f"LRCLIB get by metadata returned HTTP {resp.status_code}")
        return _parse_record(self._json(resp))

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError("LRCLIB reply is not JSON") from exc


# Version terms are material: "Song (Live)" never matches "Song". Matching uses
# word tokens with case and diacritics folded, so only punctuation and spacing
# differ. Unicode letters are kept, so non-Latin titles do not all collapse to "".
_WORD = re.compile(r"\w+")


def _key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " ".join(_WORD.findall(folded))


@dataclass(frozen=True)
class TrackEvidence:
    """What the audio file says about itself. Duration comes from the audio, not tags."""

    title: str
    artist: str
    album: str
    duration_secs: float | None


@dataclass(frozen=True)
class Verdict:
    """How one candidate compares with the track. notes is the preview evidence."""

    record: LyricsRecord
    plausible: bool
    album_matches: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class LyricsChoice:
    kind: LyricsKind
    text: str


class Outcome(StrEnum):
    SELECTED = "selected"
    INSTRUMENTAL = "instrumental"
    NO_TEXT = "no_text"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    REVIEW = "review"
    MISSING_METADATA = "missing_metadata"
    RATE_LIMITED = "rate_limited"
    PROVIDER_FAILURE = "provider_failure"


@dataclass(frozen=True)
class Selection:
    outcome: Outcome
    reason: str
    record: LyricsRecord | None = None
    lyrics: LyricsChoice | None = None
    verdicts: tuple[Verdict, ...] = ()


def _merge_records(first: list[LyricsRecord], second: list[LyricsRecord]) -> list[LyricsRecord]:
    """Union of two result lists by record id, keeping the first occurrence."""
    seen: set[int] = set()
    merged: list[LyricsRecord] = []
    for record in [*first, *second]:
        if record.id not in seen:
            seen.add(record.id)
            merged.append(record)
    return merged


def _verdict(record: LyricsRecord, evidence: TrackEvidence) -> Verdict:
    notes: list[str] = []
    artist_ok = bool(_key(evidence.artist)) and _key(record.artist_name) == _key(evidence.artist)
    if not artist_ok:
        notes.append(f"artist differs: {record.artist_name!r}")
    title_ok = bool(_key(evidence.title)) and _key(record.track_name) == _key(evidence.title)
    if not title_ok:
        notes.append(f"title differs: {record.track_name!r}")

    duration_ok = False
    if evidence.duration_secs is None:
        notes.append("track duration unknown")
    elif record.duration_secs is None:
        notes.append("record duration unknown")
    else:
        diff = record.duration_secs - evidence.duration_secs
        if abs(diff) <= DURATION_TOLERANCE_SECS:
            duration_ok = True
            notes.append(f"duration {diff:+.1f}s")
        else:
            notes.append(f"duration off by {diff:+.1f}s")

    album_matches = bool(_key(record.album_name)) and _key(record.album_name) == _key(
        evidence.album
    )
    if album_matches:
        notes.append("album matches")
    else:
        notes.append(f"album differs: {record.album_name!r}")

    # What the record offers, so the caller can judge without fetching it.
    if record.instrumental:
        notes.append("instrumental")
    else:
        offered = [
            label
            for label, text in (
                ("synced text", record.synced_lyrics),
                ("plain text", record.plain_lyrics),
            )
            if text
        ]
        notes.append("has " + " and ".join(offered) if offered else "no lyric text")

    return Verdict(
        record=record,
        plausible=artist_ok and title_ok and duration_ok,
        album_matches=album_matches,
        notes=tuple(notes),
    )


def pick_lyrics(record: LyricsRecord, *, plain_only: bool = False) -> LyricsChoice | None:
    """Return the record's synchronized text, else its plain text. Never mixes sources.

    Each text is validated before use, so malformed synchronized text falls back
    to plain text from the same record. With plain_only, the synchronized text is
    never returned, for a track whose audio no longer matches the record's timing.
    """
    if (
        not plain_only
        and record.synced_lyrics
        and classify(record.synced_lyrics, expect_timed=True) == LyricsKind.SYNCHRONIZED
    ):
        return LyricsChoice(LyricsKind.SYNCHRONIZED, record.synced_lyrics)
    if record.plain_lyrics and classify(record.plain_lyrics) == LyricsKind.PLAIN:
        return LyricsChoice(LyricsKind.PLAIN, record.plain_lyrics)
    return None


def _failure(exc: ProviderError) -> Selection:
    outcome = Outcome.RATE_LIMITED if exc.rate_limited else Outcome.PROVIDER_FAILURE
    return Selection(outcome, str(exc))


def _finish(
    record: LyricsRecord,
    reason: str,
    verdicts: tuple[Verdict, ...],
    *,
    plain_only: bool = False,
) -> Selection:
    if record.instrumental:
        return Selection(
            Outcome.INSTRUMENTAL,
            f"{reason}; record is marked instrumental",
            record=record,
            verdicts=verdicts,
        )
    choice = pick_lyrics(record, plain_only=plain_only)
    if choice is None:
        return Selection(
            Outcome.NO_TEXT,
            f"{reason}; record has no usable lyrics text",
            record=record,
            verdicts=verdicts,
        )
    return Selection(Outcome.SELECTED, reason, record=record, lyrics=choice, verdicts=verdicts)


def lookup(client: LrclibClient, evidence: TrackEvidence) -> Selection:
    """Find lyrics for one track by search. Read-only; never writes.

    Selects a record only when exactly one plausible candidate matches artist,
    title, duration within DURATION_TOLERANCE_SECS, and album. Anything less
    certain is returned for review with the evidence, not chosen. When the search
    is still capped, the exact record for the track's title, artist, album, and
    duration is added as a candidate; the outcome stays review.
    """
    missing = [
        name
        for name, value in (
            ("title", evidence.title),
            ("artist", evidence.artist),
            ("duration", evidence.duration_secs),
        )
        if not value
    ]
    if missing:
        return Selection(
            Outcome.MISSING_METADATA,
            f"track lacks {', '.join(missing)}; no search run",
        )

    try:
        result = client.search(title=evidence.title, artist=evidence.artist, album=evidence.album)
        if result.truncated and evidence.album:
            # Dropping the album filter only broadens the search. If the broader
            # search is not capped, it is complete for this artist and title, so
            # its results can settle the choice that the capped search could not.
            broader = client.search(title=evidence.title, artist=evidence.artist)
            result = SearchResult(
                records=_merge_records(broader.records, result.records),
                truncated=broader.truncated,
            )
        if result.truncated and evidence.album and evidence.duration_secs is not None:
            exact = client.get_exact(
                title=evidence.title,
                artist=evidence.artist,
                album=evidence.album,
                duration_secs=evidence.duration_secs,
            )
            if exact is not None and all(r.id != exact.id for r in result.records):
                result = SearchResult(records=[*result.records, exact], truncated=True)
    except ProviderError as exc:
        return _failure(exc)

    verdicts = tuple(_verdict(record, evidence) for record in result.records)
    if result.truncated:
        return Selection(
            Outcome.REVIEW,
            f"search returned the {SEARCH_RESULT_CAP}-result cap; candidates may be incomplete",
            verdicts=verdicts,
        )
    plausible = [v for v in verdicts if v.plausible]
    if not plausible:
        return Selection(
            Outcome.NO_MATCH,
            f"no record matches artist, title, and duration within {DURATION_TOLERANCE_SECS:g}s",
            verdicts=verdicts,
        )
    if not evidence.album:
        return Selection(
            Outcome.REVIEW,
            "track has no album tag; cannot tell versions apart",
            verdicts=verdicts,
        )

    album_matches = [v for v in plausible if v.album_matches]
    if len(album_matches) == 1:
        chosen = album_matches[0].record
        choice = pick_lyrics(chosen)
        if choice is not None and choice.kind is LyricsKind.PLAIN and not chosen.instrumental:
            # Plain text cannot be checked against the audio's timing, so it is judged.
            return Selection(
                Outcome.REVIEW,
                "unique artist, title, duration, and album match, but the record has plain "
                "text only; its timing cannot be checked",
                verdicts=verdicts,
            )
        return _finish(chosen, "unique artist, title, duration, and album match", verdicts)
    if len(plausible) == 1:
        only = plausible[0].record
        return Selection(
            Outcome.REVIEW,
            f"only plausible record is on album {only.album_name!r}, not {evidence.album!r}",
            verdicts=verdicts,
        )
    return Selection(
        Outcome.AMBIGUOUS,
        f"{len(plausible)} plausible records; judge them from the evidence and pass the chosen id",
        verdicts=verdicts,
    )


def fetch_explicit(
    client: LrclibClient,
    record_id: int,
    evidence: TrackEvidence,
    *,
    plain_only: bool = False,
) -> Selection:
    """Fetch the record the operator named, and show how it compares with the track.

    The record is not checked against the track before it is returned, so the
    operator sees each mismatch in the verdict and decides whether to write.
    plain_only takes the plain text even when the record has synchronized text.
    """
    try:
        record = client.get(record_id)
    except ProviderError as exc:
        return _failure(exc)
    if record is None:
        return Selection(Outcome.NO_MATCH, f"LRCLIB has no record with id {record_id}")
    verdict = _verdict(record, evidence)
    return _finish(record, f"explicit record id {record_id}", (verdict,), plain_only=plain_only)
