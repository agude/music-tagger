"""Inspect lyrics for an album: embedded tags and same-basename sidecars.

A track has up to three lyric sources: embedded tags, `<audio-stem>.lrc`
(synchronized), and `<audio-stem>.txt` (plain). Sidecars match the audio file's
basename exactly, including disc prefixes and Unicode. Everything here is
read-only: no network, no writes.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from mutagen import MutagenError

from .tags import UnsupportedFileError, audio_files, read_embedded_lyrics

LRC_SUFFIX = ".lrc"
TXT_SUFFIX = ".txt"

# Status used when a track has no lyric from any source. Not a LyricsKind,
# because a missing source has no content to classify.
MISSING = "missing"
UNREADABLE = "unreadable"


class LyricsKind(StrEnum):
    SYNCHRONIZED = "synchronized"
    PLAIN = "plain"
    EMPTY = "empty"
    MALFORMED = "malformed"


class Source(StrEnum):
    EMBEDDED = "embedded"
    LRC = "lrc"
    TXT = "txt"


# Best first. A malformed lyric outranks an empty one because it is a defect the
# operator must see; an empty one is simply no lyric.
_RANKING = (
    LyricsKind.SYNCHRONIZED,
    LyricsKind.PLAIN,
    LyricsKind.MALFORMED,
    LyricsKind.EMPTY,
)

# A complete LRC timestamp: [mm:ss], [mm:ss.xx], or [mm:ss:xx]. Seconds are
# 00-59. Parsing is strict so that an out-of-range value is flagged, not ignored.
_TIMESTAMP = re.compile(r"\[\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?\]")
# An LRC header such as [ar:Artist], [ti:Title], or [offset:+500].
_METADATA_TAG = re.compile(r"\[[A-Za-z]+:[^\]]*\]")


@dataclass(frozen=True)
class LyricsEntry:
    """One lyric value found for a track. path is set for sidecars only."""

    source: Source
    kind: LyricsKind
    language: str = ""
    description: str = ""
    path: Path | None = None


@dataclass
class TrackLyrics:
    path: Path
    entries: list[LyricsEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """The best outcome across sources: a LyricsKind value, missing, or unreadable.

        A track with unreadable audio or sidecars is reported as unreadable, even
        when other sources were read, so the failure is not hidden by them.
        """
        if self.errors:
            return UNREADABLE
        kinds = {entry.kind for entry in self.entries}
        for kind in _RANKING:
            if kind in kinds:
                return kind.value
        return MISSING


@dataclass
class AlbumLyrics:
    directory: Path
    tracks: list[TrackLyrics]

    def coverage(self) -> Counter[str]:
        """Count tracks by status."""
        return Counter(track.status for track in self.tracks)


def _scan_line(line: str) -> tuple[bool, bool, bool]:
    """Return (timed, invalid, has_text) for one stripped line.

    timed: starts with at least one valid timestamp.
    invalid: starts with a bracket that begins with a digit but is not a valid
    timestamp, such as [00:75.00].
    has_text: lyric words follow the leading tags.
    """
    timed = invalid = metadata = False
    rest = line
    while rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            break
        token = rest[: end + 1]
        if _TIMESTAMP.fullmatch(token):
            timed = True
        elif _METADATA_TAG.fullmatch(token):
            metadata = True
        elif token[1:2].isdigit():
            invalid = True
        else:
            # Bracketed lyric text, such as [Chorus], is lyric words.
            break
        rest = rest[end + 1 :]
    return timed, invalid, bool(rest.strip()) and not metadata


def classify(text: str, *, expect_timed: bool | None = None) -> LyricsKind:
    """Classify lyric text as synchronized, plain, empty, or malformed.

    expect_timed is set for sidecars: True for `.lrc`, False for `.txt`. A
    sidecar whose content disagrees with its extension is malformed. None
    accepts either form, as embedded tags do.
    """
    any_timed = any_invalid = any_text = False
    for raw in text.splitlines():
        timed, invalid, has_text = _scan_line(raw.strip())
        any_timed |= timed
        any_invalid |= invalid
        any_text |= has_text
    if any_invalid:
        return LyricsKind.MALFORMED
    if not any_timed and not any_text:
        return LyricsKind.EMPTY
    if expect_timed is True and not any_timed:
        return LyricsKind.MALFORMED
    if expect_timed is False and any_timed:
        return LyricsKind.MALFORMED
    return LyricsKind.SYNCHRONIZED if any_timed else LyricsKind.PLAIN


def _read_sidecar(path: Path, source: Source) -> LyricsEntry:
    """Read a sidecar as UTF-8, with or without a byte-order mark.

    Any other encoding, such as UTF-16 or Latin-1, is malformed.
    """
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        return LyricsEntry(source=source, kind=LyricsKind.MALFORMED, path=path)
    expect_timed = source is Source.LRC
    return LyricsEntry(source=source, kind=classify(text, expect_timed=expect_timed), path=path)


def lyric_sidecars(audio: Path) -> list[Path]:
    """Existing `<audio-stem>.lrc` and `<audio-stem>.txt` beside an audio file, LRC first.

    The match is by exact basename. Other `.txt` files in the album are not
    lyric sidecars and are never returned.
    """
    candidates = (audio.with_suffix(LRC_SUFFIX), audio.with_suffix(TXT_SUFFIX))
    return [sidecar for sidecar in candidates if sidecar.is_file()]


def inspect_track(path: Path) -> TrackLyrics:
    """Collect every lyric source for one audio file. Read-only.

    An unreadable audio file or sidecar is recorded in `errors`, and the other
    sources are still read.
    """
    track = TrackLyrics(path=path)
    try:
        embedded = read_embedded_lyrics(path)
    except (MutagenError, OSError, UnsupportedFileError) as exc:
        track.errors.append(f"embedded: {exc}")
    else:
        for lyric in embedded:
            track.entries.append(
                LyricsEntry(
                    source=Source.EMBEDDED,
                    kind=classify(lyric.text),
                    language=lyric.language,
                    description=lyric.description,
                )
            )
    for suffix, source in ((LRC_SUFFIX, Source.LRC), (TXT_SUFFIX, Source.TXT)):
        sidecar = path.with_suffix(suffix)
        if not sidecar.is_file():
            continue
        try:
            track.entries.append(_read_sidecar(sidecar, source))
        except OSError as exc:
            track.errors.append(f"{source.value}: {exc}")
    return track


def inspect_album(directory: Path) -> AlbumLyrics:
    """Inspect every audio file in a directory, in name order. Read-only."""
    return AlbumLyrics(
        directory=directory,
        tracks=[inspect_track(path) for path in audio_files(directory)],
    )
