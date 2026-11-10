"""Embed sidecar lyrics into audio tags, as a backup of the sidecar. No network.

Each track has one source: its `.lrc` when that holds lyrics, else its `.txt`.
Never both. The text goes into the container's lyric tag:

- FLAC `LYRICS` and M4A `©lyr`: the text as-is, with LRC timestamps kept.
- MP3 synchronized (`.lrc`): `SYLT`, language `eng`, empty descriptor,
  millisecond timestamps, content type lyrics.
- MP3 plain (`.txt`): `USLT`, language `eng`, empty descriptor.

The managed slot is the whole FLAC or M4A lyric field, and the `eng`,
empty-descriptor SYLT and USLT frames of an MP3. An MP3 keeps one of the two.
Other languages and descriptors, and every other tag, are left alone. A
different value in the slot needs replace. Planning reads and writes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import SYLT, USLT, Encoding
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from .lyrics import LyricsKind, Source, TrackLyrics
from .lyrics_write import WriteStatus
from .tags import LYRICS_MP4_ATOM, LYRICS_VORBIS_KEY, UnsupportedFileError

EMBED_LANGUAGE = "eng"
EMBED_DESCRIPTION = ""
# ID3 SYLT: content type 1 is lyrics; time stamp format 2 is milliseconds.
_SYLT_CONTENT_TYPE_LYRICS = 1
_SYLT_FORMAT_MILLISECONDS = 2

_LRC_TIMESTAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_LEADING_TAGS = re.compile(r"(?:\[[^\]]*\])+")


@dataclass(frozen=True)
class EmbedSource:
    path: Path
    kind: LyricsKind  # SYNCHRONIZED or PLAIN
    text: str  # LF line endings, no trailing newline


@dataclass(frozen=True)
class EmbedPlan:
    status: WriteStatus
    reason: str
    path: Path
    source: EmbedSource | None = None
    tag: str = ""  # the tag written, for the dry run and the log
    replaces: bool = False


@dataclass(frozen=True)
class _Slot:
    """The managed lyric content of one file. Empty means no managed lyrics."""

    fields: tuple[str, ...] = ()  # FLAC and M4A: every value of the lyric field
    uslt: tuple[str, ...] = ()  # MP3: managed USLT frames
    sylt: tuple[tuple[tuple[str, int], ...], ...] = ()  # MP3: managed SYLT frames

    def is_empty(self) -> bool:
        return not (self.fields or self.uslt or self.sylt)


def parse_lrc(text: str) -> list[tuple[str, int]]:
    """Return (line, milliseconds) pairs in time order from LRC text.

    A line may carry several leading timestamps, and each one becomes a pair.
    A line without a leading timestamp is skipped. A fraction is read by its
    digits: one digit is tenths, two are hundredths, and three are milliseconds.
    """
    pairs: list[tuple[str, int]] = []
    for line in text.split("\n"):
        lead = _LEADING_TAGS.match(line)
        if lead is None:
            continue
        body = line[lead.end() :]
        for token in _LRC_TIMESTAMP.finditer(lead.group(0)):
            minutes, seconds, fraction = token.groups()
            millis = (int(minutes) * 60 + int(seconds)) * 1000
            if fraction:
                millis += int(fraction.ljust(3, "0"))
            pairs.append((body, millis))
    pairs.sort(key=lambda pair: pair[1])
    return pairs


def _read_text(path: Path) -> str:
    text = path.read_bytes().decode("utf-8-sig")
    return text.replace("\r\n", "\n").rstrip("\n")


def choose_source(track: TrackLyrics) -> EmbedSource | None:
    """Pick the one sidecar to embed: the .lrc if it holds lyrics, else the .txt.

    A malformed candidate raises ValueError, so the operator decides. An empty
    candidate is skipped. Returns None when neither sidecar holds lyrics.
    """
    for source, kind in ((Source.LRC, LyricsKind.SYNCHRONIZED), (Source.TXT, LyricsKind.PLAIN)):
        for entry in track.entries:
            if entry.source is not source or entry.path is None:
                continue
            if entry.kind is LyricsKind.MALFORMED:
                raise ValueError(f"{entry.path.name} is malformed")
            if entry.kind is kind:
                return EmbedSource(path=entry.path, kind=kind, text=_read_text(entry.path))
    return None


def _read_slot(audio: object) -> _Slot:
    if isinstance(audio, FLAC | MP4):
        key = LYRICS_VORBIS_KEY if isinstance(audio, FLAC) else LYRICS_MP4_ATOM
        return _Slot(fields=tuple(str(v) for v in audio.get(key, [])))  # type: ignore[no-untyped-call]
    if isinstance(audio, MP3):
        if audio.tags is None:
            return _Slot()
        uslt = tuple(
            str(frame.text)
            for frame in audio.tags.getall("USLT")
            if frame.lang == EMBED_LANGUAGE and frame.desc == EMBED_DESCRIPTION
        )
        sylt = tuple(
            tuple((str(text), int(millis)) for text, millis in frame.text)
            for frame in audio.tags.getall("SYLT")
            if frame.lang == EMBED_LANGUAGE and frame.desc == EMBED_DESCRIPTION
        )
        return _Slot(uslt=uslt, sylt=sylt)
    raise UnsupportedFileError(f"not a FLAC, MP3, or M4A file: {audio}")


def _target(audio: object, source: EmbedSource) -> _Slot:
    if isinstance(audio, MP3):
        if source.kind is LyricsKind.SYNCHRONIZED:
            return _Slot(sylt=(tuple(parse_lrc(source.text)),))
        return _Slot(uslt=(source.text,))
    return _Slot(fields=(source.text,))


def _tag_name(audio: object, source: EmbedSource) -> str:
    if isinstance(audio, MP3):
        frame = "SYLT" if source.kind is LyricsKind.SYNCHRONIZED else "USLT"
        return f"{frame} {EMBED_LANGUAGE}"
    return "LYRICS" if isinstance(audio, FLAC) else "©lyr"


def plan_embed(track: TrackLyrics, *, replace: bool) -> EmbedPlan:
    """Decide what to embed for one track. Read-only.

    replace authorizes overwriting a different value in the managed slot.
    """
    path = track.path
    if track.errors:
        return EmbedPlan(
            WriteStatus.REFUSED,
            "could not read every lyric source: " + "; ".join(track.errors),
            path,
        )
    try:
        source = choose_source(track)
    except (ValueError, UnicodeDecodeError) as exc:
        return EmbedPlan(WriteStatus.REFUSED, str(exc), path)
    if source is None:
        return EmbedPlan(WriteStatus.NO_TEXT, "no lyric sidecar holds text", path)

    try:
        audio = MutagenFile(path)
        if audio is None:
            raise UnsupportedFileError(f"not a supported audio file: {path}")
        current = _read_slot(audio)
    except (MutagenError, OSError, UnsupportedFileError) as exc:
        return EmbedPlan(WriteStatus.REFUSED, f"cannot read tags: {exc}", path)

    tag = _tag_name(audio, source)
    if current == _target(audio, source):
        return EmbedPlan(WriteStatus.UNCHANGED, f"{tag} already holds this text", path, source, tag)
    has_content = not current.is_empty()
    if has_content and not replace:
        return EmbedPlan(
            WriteStatus.PRESERVED,
            "existing lyrics are kept; pass --replace to replace them",
            path,
            source,
            tag,
        )
    reason = f"write {tag} from {source.path.name}"
    if has_content:
        reason = f"replace existing lyrics with {tag} from {source.path.name}"
    return EmbedPlan(WriteStatus.WRITE, reason, path, source, tag, replaces=has_content)


def _write_mp3(audio: MP3, source: EmbedSource) -> int:
    """Set the managed frame, keep the file's ID3 version, and return that version."""
    if audio.tags is None:
        audio.add_tags()  # type: ignore[no-untyped-call]
    tags = audio.tags
    if tags is None:
        raise UnsupportedFileError("could not create ID3 tags")
    version = tags.version[1]
    # ID3v2.3 has no UTF-8 encoding; v2.4 does.
    encoding = Encoding.UTF8 if version == 4 else Encoding.UTF16
    if source.kind is LyricsKind.SYNCHRONIZED:
        tags.delall(f"USLT:{EMBED_DESCRIPTION}:{EMBED_LANGUAGE}")
        tags.add(
            SYLT(
                encoding=encoding,
                lang=EMBED_LANGUAGE,
                format=_SYLT_FORMAT_MILLISECONDS,
                type=_SYLT_CONTENT_TYPE_LYRICS,
                desc=EMBED_DESCRIPTION,
                text=parse_lrc(source.text),
            )
        )
    else:
        tags.delall(f"SYLT:{EMBED_DESCRIPTION}:{EMBED_LANGUAGE}")
        tags.add(
            USLT(
                encoding=encoding,
                lang=EMBED_LANGUAGE,
                desc=EMBED_DESCRIPTION,
                text=source.text,
            )
        )
    return version


def apply_embed(plan: EmbedPlan) -> None:
    """Write a WRITE plan's tag and save the file.

    Any other status raises ValueError, so nothing is written from a plan that
    was refused, preserved, or unchanged.
    """
    if plan.status is not WriteStatus.WRITE or plan.source is None:
        raise ValueError(f"cannot apply a {plan.status.value} plan")
    audio = MutagenFile(plan.path)
    if isinstance(audio, FLAC):
        audio[LYRICS_VORBIS_KEY] = [plan.source.text]
        audio.save()
    elif isinstance(audio, MP4):
        audio[LYRICS_MP4_ATOM] = [plan.source.text]
        audio.save()  # type: ignore[no-untyped-call]
    elif isinstance(audio, MP3):
        # mutagen saves only ID3v2.3 and v2.4, and converting v2.2 would rewrite
        # every frame, which the embed rules forbid. The file is left unchanged.
        if audio.tags is not None and audio.tags.version[1] == 2:
            raise UnsupportedFileError(
                f"{plan.path.name} has an ID3v2.2 tag, which cannot be written; "
                "the file is unchanged"
            )
        version = _write_mp3(audio, plan.source)
        audio.save(v2_version=version)
    else:
        raise UnsupportedFileError(f"not a FLAC, MP3, or M4A file: {plan.path}")
