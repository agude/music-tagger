"""Read and write audio tags via mutagen.

Provides a unified interface over FLAC (Vorbis Comments) and MP3 (ID3v2).
Field names follow Navidrome's mappings.yaml conventions.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, POPM, TCON, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a"}

# FMPS spellings. MP3 TXXX descriptions and MP4 freeform names are
# case-sensitive, so each output spelling is fixed here.
#
# Output is "FMPS_Rating" and "FMPS_Playcount". That is the FMPS spelling, and
# the one Clementine's MP4 reader matches. Clementine's ID3 writer emits
# "FMPS_PlayCount" and its readers match exact keys, so the other casings are
# read as aliases and never written. FLAC Vorbis keys are case-insensitive and
# keep the uppercase FMPS_RATING.
#
# Readers accept the uppercase names earlier versions of this tool wrote. Two
# aliases holding different values are a conflict; neither is chosen.
FMPS_RATING_OUTPUT = "FMPS_Rating"
FMPS_RATING_ALIASES = ("FMPS_Rating", "FMPS_RATING")
FMPS_PLAYCOUNT_OUTPUT = "FMPS_Playcount"
FMPS_PLAYCOUNT_ALIASES = ("FMPS_Playcount", "FMPS_PlayCount", "FMPS_PLAYCOUNT")

# MP4 atoms, for reading only. Genre is the one MP4 field this tool writes;
# see write_tags, which refuses the rest rather than silently dropping them.
MP4_MAP: dict[str, str] = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "albumartist": "aART",
    "album": "\xa9alb",
    "date": "\xa9day",
    "genre": "\xa9gen",
    "composer": "\xa9wrt",
    "musicbrainz_albumid": "----:com.apple.iTunes:MusicBrainz Album Id",
    "fmps_rating": f"----:com.apple.iTunes:{FMPS_RATING_OUTPUT}",
    "starred": "----:com.apple.iTunes:STARRED",
    "starred_at": "----:com.apple.iTunes:STARRED_AT",
}

# MP4 has no standard star-rating field, and the conventions collide: the
# freeform RATING atom is 0-100 in MusicBee/EZ CD, while this tool writes
# RATING as 1-5 for FLAC and MP3. Writing 1-5 into an M4A would read back as
# unrated or one star under the 0-100 reading, so `rating` is deliberately
# absent from MP4_MAP. FMPS_RATING (0.0-1.0) is scale-free and carries the
# value; the native `rate` atom carries 0-100 for third-party readers.
MP4_RATE_ATOM = "rate"

_MB_DISAMBIG_TERMS = {
    "acoustic",
    "demo",
    "live",
    "radio edit",
    "remaster",
    "remastered",
    "remix",
    "single edit",
}

# Canonical field name -> (FLAC Vorbis tag, MP3 ID3 frame)
# For MP3 TXXX frames, the value is the description string.
FIELD_MAP: dict[str, tuple[str, str]] = {
    "title": ("TITLE", "TIT2"),
    "artist": ("ARTIST", "TPE1"),
    "album": ("ALBUM", "TALB"),
    "albumartist": ("ALBUMARTIST", "TPE2"),
    "tracknumber": ("TRACKNUMBER", "TRCK"),
    "discnumber": ("DISCNUMBER", "TPOS"),
    "totaltracks": ("TOTALTRACKS", "TXXX:TOTALTRACKS"),
    "totaldiscs": ("TOTALDISCS", "TXXX:TOTALDISCS"),
    "date": ("DATE", "TDRC"),
    "originaldate": ("ORIGINALDATE", "TDOR"),
    "releasedate": ("RELEASEDATE", "TDRL"),
    "genre": ("GENRE", "TCON"),
    "composer": ("COMPOSER", "TCOM"),
    "catalognumber": ("CATALOGNUMBER", "TXXX:CATALOGNUMBER"),
    "label": ("LABEL", "TXXX:LABEL"),
    "country": ("RELEASECOUNTRY", "TXXX:MusicBrainz Album Release Country"),
    "media": ("MEDIA", "TXXX:MEDIA"),
    "barcode": ("BARCODE", "TXXX:BARCODE"),
    "musicbrainz_albumid": ("MUSICBRAINZ_ALBUMID", "TXXX:MusicBrainz Album Id"),
    "musicbrainz_artistid": ("MUSICBRAINZ_ARTISTID", "TXXX:MusicBrainz Artist Id"),
    "musicbrainz_albumartistid": (
        "MUSICBRAINZ_ALBUMARTISTID",
        "TXXX:MusicBrainz Album Artist Id",
    ),
    "musicbrainz_releasegroupid": (
        "MUSICBRAINZ_RELEASEGROUPID",
        "TXXX:MusicBrainz Release Group Id",
    ),
    "musicbrainz_releasetrackid": (
        "MUSICBRAINZ_RELEASETRACKID",
        "TXXX:MusicBrainz Release Track Id",
    ),
    "musicbrainz_recordingid": (
        "MUSICBRAINZ_TRACKID",
        "TXXX:MusicBrainz Recording Id",
    ),
    "releasetype": ("RELEASETYPE", "TXXX:MusicBrainz Album Type"),
    "releasestatus": ("RELEASESTATUS", "TXXX:MusicBrainz Album Status"),
    "isrc": ("ISRC", "TSRC"),
    "compilation": ("COMPILATION", "TXXX:COMPILATION"),
    "asin": ("ASIN", "TXXX:ASIN"),
    "script": ("SCRIPT", "TXXX:SCRIPT"),
    "lyricist": ("LYRICIST", "TEXT"),
    "conductor": ("CONDUCTOR", "TPE3"),
    "producer": ("PRODUCER", "TXXX:PRODUCER"),
    "engineer": ("ENGINEER", "TXXX:ENGINEER"),
    "mixer": ("MIXER", "TXXX:MIXER"),
    "remixer": ("REMIXER", "TPE4"),
    "performer": ("PERFORMER", "TXXX:PERFORMER"),
    "musicbrainz_workid": ("MUSICBRAINZ_WORKID", "TXXX:MusicBrainz Work Id"),
    "musicbrainz_composerid": (
        "MUSICBRAINZ_COMPOSERID",
        "TXXX:MusicBrainz Composer Id",
    ),
    "musicbrainz_lyricistid": (
        "MUSICBRAINZ_LYRICISTID",
        "TXXX:MusicBrainz Lyricist Id",
    ),
    "musicbrainz_producerid": (
        "MUSICBRAINZ_PRODUCERID",
        "TXXX:MusicBrainz Producer Id",
    ),
    "musicbrainz_engineerid": (
        "MUSICBRAINZ_ENGINEERID",
        "TXXX:MusicBrainz Engineer Id",
    ),
    "musicbrainz_mixerid": (
        "MUSICBRAINZ_MIXERID",
        "TXXX:MusicBrainz Mixer Id",
    ),
    "musicbrainz_conductorid": (
        "MUSICBRAINZ_CONDUCTORID",
        "TXXX:MusicBrainz Conductor Id",
    ),
    "musicbrainz_remixerid": (
        "MUSICBRAINZ_REMIXERID",
        "TXXX:MusicBrainz Remixer Id",
    ),
    "musicbrainz_performerid": (
        "MUSICBRAINZ_PERFORMERID",
        "TXXX:MusicBrainz Performer Id",
    ),
    "music_tagger_updated": (
        "MUSIC_TAGGER_UPDATED",
        "TXXX:MUSIC_TAGGER_UPDATED",
    ),
    "fmps_rating": ("FMPS_RATING", f"TXXX:{FMPS_RATING_OUTPUT}"),
    "rating": ("RATING", "TXXX:RATING"),
    "starred": ("STARRED", "TXXX:STARRED"),
    "starred_at": ("STARRED_AT", "TXXX:STARRED_AT"),
}

# Fields `tag` must leave at their current value, including an empty one.
# Stored outside FIELD_MAP because it is multi-valued and FIELD_MAP reads
# only the first value of each field.
LOCK_VORBIS_KEY = "MUSIC_TAGGER_LOCKED"
LOCK_MP4_ATOM = "----:com.apple.iTunes:MUSIC_TAGGER_LOCKED"
LOCKABLE_FIELDS = frozenset(FIELD_MAP) - {"music_tagger_updated"}


@dataclass
class TrackTags:
    path: Path
    tags: dict[str, str] = field(default_factory=dict)
    duration_secs: float = 0.0
    format: str = ""
    locked_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": self.format,
            "duration_secs": self.duration_secs,
            "tags": dict(self.tags),
            "locked_fields": list(self.locked_fields),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackTags:
        return cls(
            path=Path(data["path"]),
            tags=data.get("tags", {}),
            duration_secs=data.get("duration_secs", 0.0),
            format=data.get("format", ""),
            locked_fields=list(data.get("locked_fields", [])),
        )


@dataclass
class AlbumTags:
    directory: Path
    tracks: list[TrackTags] = field(default_factory=list)

    @property
    def artist(self) -> str:
        return _most_common(
            t.tags.get("albumartist") or t.tags.get("artist", "") for t in self.tracks
        )

    @property
    def album(self) -> str:
        return _most_common(t.tags.get("album", "") for t in self.tracks)

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "artist": self.artist,
            "album": self.album,
            "track_count": self.track_count,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlbumTags:
        return cls(
            directory=Path(data["directory"]),
            tracks=[TrackTags.from_dict(t) for t in data.get("tracks", [])],
        )


def _most_common(values: Any) -> str:
    from collections import Counter

    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


_FLAC_MAGIC = b"fLaC"
_VORBIS_COMMENT_BLOCK = 4


def _flac_tags_from_lookup(lookup: Callable[[str], Sequence[str] | None]) -> dict[str, str]:
    """Map Vorbis comments onto FIELD_MAP names; lookup(key) returns values, first wins."""
    tags: dict[str, str] = {}
    for canonical, (vorbis_key, _) in FIELD_MAP.items():
        values = lookup(vorbis_key)
        if values:
            tags[canonical] = values[0]
    return tags


def _read_flac_tags(audio: FLAC) -> dict[str, str]:
    return _flac_tags_from_lookup(audio.get)


def _parse_vorbis_comments(block: bytes) -> dict[str, list[str]]:
    """Parse a VORBIS_COMMENT block into upper-case key -> values, in file order."""
    (vendor_length,) = struct.unpack_from("<I", block, 0)
    pos = 4 + vendor_length
    (count,) = struct.unpack_from("<I", block, pos)
    pos += 4
    comments: dict[str, list[str]] = {}
    for _ in range(count):
        (length,) = struct.unpack_from("<I", block, pos)
        pos += 4
        text = block[pos : pos + length].decode("utf-8", errors="replace")
        pos += length
        key, separator, value = text.partition("=")
        if separator:
            comments.setdefault(key.upper(), []).append(value)
    return comments


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ValueError("FLAC metadata ends early")
        data += chunk
    return data


def _flac_comments_from_stream(stream: BinaryIO, size: int) -> dict[str, list[str]] | None:
    """Return a FLAC stream's Vorbis comments, or None if its layout is unexpected.

    Reads each 4-byte block header and the VORBIS_COMMENT block, then seeks past
    every other block, so embedded pictures are never read. size is the file
    length; a last block that runs past it means the file is truncated.
    """
    if stream.read(4) != _FLAC_MAGIC:
        return None
    comments: dict[str, list[str]] | None = None
    while True:
        header = _read_exact(stream, 4)
        is_last = header[0] & 0x80
        block_type = header[0] & 0x7F
        length = int.from_bytes(header[1:4], "big")
        if block_type == _VORBIS_COMMENT_BLOCK:
            if comments is not None:
                return None
            comments = _parse_vorbis_comments(_read_exact(stream, length))
        else:
            stream.seek(length, 1)
        if is_last:
            if stream.tell() > size:
                return None
            return comments if comments is not None else {}


def read_flac_comments(path: Path) -> dict[str, list[str]] | None:
    """Read a FLAC file's Vorbis comments without loading its embedded pictures.

    Returns None when the file is not a plain FLAC stream or its metadata is
    malformed; callers then fall back to mutagen. Reads are unbuffered, so an
    open does not fill Python's default buffer, which is sized from st_blksize.
    """
    with open(path, "rb", buffering=0) as stream:
        size = os.fstat(stream.fileno()).st_size
        try:
            return _flac_comments_from_stream(stream, size)
        except (ValueError, struct.error):
            return None


def _read_mp3_tags(audio: MP3) -> dict[str, str]:
    if audio.tags is None:
        return {}
    tags: dict[str, str] = {}
    for canonical, (_, id3_key) in FIELD_MAP.items():
        if id3_key.startswith("TXXX:"):
            desc = id3_key[5:]
            for frame in audio.tags.getall("TXXX"):
                if frame.desc.lower() == desc.lower():
                    tags[canonical] = frame.text[0] if frame.text else ""
                    break
        else:
            frame = audio.tags.get(id3_key)
            if frame:
                tags[canonical] = str(frame)
    return tags


def _read_mp4_tags(audio: MP4) -> dict[str, str]:
    """Read the string-valued MP4 atoms.

    Skips trkn/disk (tuples, not strings) and anything else whose atom type
    would not survive the dict[str, str] contract.
    """
    tags: dict[str, str] = {}
    for canonical, atom in MP4_MAP.items():
        values = audio.get(atom)  # type: ignore[no-untyped-call]
        if not values:
            continue
        first = values[0]
        tags[canonical] = (
            bytes(first).decode("utf-8", "replace") if isinstance(first, bytes) else str(first)
        )
    return tags


@dataclass(frozen=True)
class FmpsField:
    """One FMPS value. `value` is "" when absent, or when `conflict` is set."""

    value: str = ""
    conflict: bool = False


@dataclass(frozen=True)
class FmpsStats:
    rating: FmpsField
    play_count: FmpsField


def _resolve_fmps(texts: Mapping[str, str], aliases: tuple[str, ...]) -> FmpsField:
    present = {texts[name] for name in aliases if texts.get(name)}
    if len(present) > 1:
        return FmpsField(conflict=True)
    return FmpsField(value=present.pop() if present else "")


def read_fmps(audio: MP3 | MP4) -> FmpsStats:
    """Read rating and play count from every accepted spelling. Read-only.

    Lookups use exact keys, as the spellings are case-sensitive. An empty
    value counts as absent. Aliases that agree are one value; aliases that
    differ are a conflict.
    """
    texts: dict[str, str] = {}
    if isinstance(audio, MP3):
        id3 = audio.tags
        if id3 is not None:
            for name in FMPS_RATING_ALIASES + FMPS_PLAYCOUNT_ALIASES:
                frame = id3.get(f"TXXX:{name}")
                if frame is not None and frame.text:
                    texts[name] = str(frame.text[0])
    elif isinstance(audio, MP4):
        atoms = audio.tags
        if atoms is not None:
            for name in FMPS_RATING_ALIASES + FMPS_PLAYCOUNT_ALIASES:
                values = atoms.get(f"----:com.apple.iTunes:{name}")
                if values:
                    texts[name] = bytes(values[0]).decode("utf-8", "replace")
    else:
        raise TypeError(f"read_fmps reads MP3 and MP4 only, not {type(audio).__name__}")
    return FmpsStats(
        rating=_resolve_fmps(texts, FMPS_RATING_ALIASES),
        play_count=_resolve_fmps(texts, FMPS_PLAYCOUNT_ALIASES),
    )


def read_album(directory: Path) -> AlbumTags:
    """Read tags from all audio files in a directory."""
    album = AlbumTags(directory=directory)
    files = sorted(f for f in directory.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS)
    for path in files:
        audio = MutagenFile(path)
        if audio is None:
            continue
        if isinstance(audio, FLAC):
            tags = _read_flac_tags(audio)
            fmt = "flac"
        elif isinstance(audio, MP3):
            tags = _read_mp3_tags(audio)
            fmt = "mp3"
        elif isinstance(audio, MP4):
            tags = _read_mp4_tags(audio)
            fmt = "m4a"
        else:
            continue
        duration = audio.info.length if audio.info else 0.0
        album.tracks.append(
            TrackTags(
                path=path,
                tags=tags,
                duration_secs=duration,
                format=fmt,
                locked_fields=_read_locks(audio),
            )
        )
    return album


def _write_flac_tag(audio: FLAC, canonical: str, value: str) -> None:
    vorbis_key = FIELD_MAP[canonical][0]
    audio[vorbis_key] = [value]


def _write_mp3_tag(audio: MP3, canonical: str, value: str) -> None:
    if audio.tags is None:
        audio.add_tags()  # type: ignore[no-untyped-call]
    assert audio.tags is not None
    id3_key = FIELD_MAP[canonical][1]
    if id3_key.startswith("TXXX:"):
        desc = id3_key[5:]
        for frame in audio.tags.getall("TXXX"):
            if frame.desc.lower() == desc.lower():
                frame.text = [value]
                return
        audio.tags.add(TXXX(desc=desc, text=[value]))
    else:
        from mutagen.id3 import Frames

        frame_cls = Frames.get(id3_key)
        if frame_cls:
            audio.tags.setall(id3_key, [frame_cls(text=[value])])


@dataclass
class TagChange:
    field: str
    old_value: str
    new_value: str

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> TagChange:
        return cls(
            field=data["field"],
            old_value=data.get("old_value", ""),
            new_value=data["new_value"],
        )


def compute_diff(track: TrackTags, new_tags: dict[str, str]) -> list[TagChange]:
    """Compute the tag changes needed for a single track."""
    # Fields where comparison should be case-insensitive
    case_insensitive = {"releasestatus", "releasetype"}
    # Fields excluded from diff — always written when other changes exist
    skip_diff = {"music_tagger_updated"}

    changes = []
    for field_name, new_value in sorted(new_tags.items()):
        if field_name in skip_diff:
            continue
        old_value = track.tags.get(field_name, "")
        if field_name in case_insensitive:
            if old_value.lower() == new_value.lower():
                continue
        elif old_value == new_value:
            continue
        if field_name == "title" and old_value and new_value.startswith(old_value):
            suffix = new_value[len(old_value) :].strip()
            if suffix.startswith("(") and suffix.endswith(")"):
                inner = suffix[1:-1].lower()
                if any(t in inner for t in _MB_DISAMBIG_TERMS):
                    continue
        changes.append(TagChange(field=field_name, old_value=old_value, new_value=new_value))
    return changes


def write_tags(track: TrackTags, changes: list[TagChange]) -> None:
    """Write tag changes to a single audio file."""
    if not changes:
        return
    audio = MutagenFile(track.path)
    if audio is None:
        return
    if not isinstance(audio, FLAC | MP3):
        # M4A is readable and its genre is writable via write_genres, but the
        # general field map has no MP4 write path. Fail loudly rather than
        # reporting success after writing nothing.
        raise NotImplementedError(
            f"writing tags to {track.path.suffix} is not supported: {track.path}"
        )
    for change in changes:
        if change.field not in FIELD_MAP:
            continue
        if isinstance(audio, FLAC):
            _write_flac_tag(audio, change.field, change.new_value)
        else:
            _write_mp3_tag(audio, change.field, change.new_value)
    audio.save()


def read_genres(path: Path) -> list[str]:
    """Read every genre value from a file.

    Genre is the only multi-valued field in the schema. `_read_flac_tags` keeps
    just the first value, which is enough for matching but would silently drop
    the rest on a write-back, so genre edits go through this instead.
    """
    audio = MutagenFile(path)
    if audio is None:
        return []
    if isinstance(audio, FLAC):
        return [str(v) for v in audio.get("GENRE", [])]  # type: ignore[no-untyped-call]
    if isinstance(audio, MP3) and audio.tags is not None:
        return [str(t) for frame in audio.tags.getall("TCON") for t in frame.text]
    if isinstance(audio, MP4):
        return [str(v) for v in audio.get("\xa9gen", [])]  # type: ignore[no-untyped-call]
    return []


def write_genres(path: Path, genres: list[str], *, timestamp: str = "") -> bool:
    """Replace the genre tag with `genres`, one tag value each. True if written.

    FLAC gets repeated GENRE comments; MP3 gets a single multi-value TCON frame.
    Both are what Navidrome reads natively, so no delimiter is involved.
    """
    audio = MutagenFile(path)
    if audio is None:
        return False
    if isinstance(audio, FLAC):
        if genres:
            audio["GENRE"] = genres
        elif "GENRE" in audio:
            del audio["GENRE"]  # type: ignore[no-untyped-call]
    elif isinstance(audio, MP3):
        if audio.tags is None:
            audio.add_tags()  # type: ignore[no-untyped-call]
        assert audio.tags is not None
        audio.tags.delall("TCON")
        if genres:
            audio.tags.add(TCON(text=genres))
    elif isinstance(audio, MP4):
        if genres:
            audio["\xa9gen"] = genres
        elif "\xa9gen" in audio:
            del audio["\xa9gen"]  # type: ignore[no-untyped-call]
    else:
        return False
    if timestamp:
        if isinstance(audio, FLAC):
            _write_flac_tag(audio, "music_tagger_updated", timestamp)
        elif isinstance(audio, MP3):
            _write_mp3_tag(audio, "music_tagger_updated", timestamp)
        else:
            audio["----:com.apple.iTunes:MUSIC_TAGGER_UPDATED"] = [timestamp.encode()]
    audio.save()
    return True


def read_locks(path: Path) -> list[str]:
    """Read the fields locked against `tag` changes."""
    audio = MutagenFile(path)
    if audio is None:
        return []
    return _read_locks(audio)


def _read_locks(audio: Any) -> list[str]:
    if isinstance(audio, FLAC):
        return [str(v) for v in audio.get(LOCK_VORBIS_KEY, [])]  # type: ignore[no-untyped-call]
    if isinstance(audio, MP3) and audio.tags is not None:
        for frame in audio.tags.getall("TXXX"):
            if frame.desc.lower() == LOCK_VORBIS_KEY.lower():
                return [str(t) for t in frame.text]
        return []
    if isinstance(audio, MP4):
        values = audio.get(LOCK_MP4_ATOM, [])  # type: ignore[no-untyped-call]
        return [bytes(v).decode("utf-8", "replace") for v in values]
    return []


def write_locks(path: Path, fields: list[str], *, timestamp: str = "") -> None:
    """Replace the locked-field list with `fields`, one tag value each.

    Raises ValueError for a field `tag` does not write, and NotImplementedError
    for formats other than FLAC and MP3, before touching the file.
    """
    unknown = sorted(set(fields) - LOCKABLE_FIELDS)
    if unknown:
        raise ValueError(f"cannot lock unknown field(s): {', '.join(unknown)}")
    locked = sorted(set(fields))

    audio = MutagenFile(path)
    if not isinstance(audio, FLAC | MP3):
        # `tag` cannot write M4A either, so a lock there would protect nothing.
        raise NotImplementedError(f"locking {path.suffix} files is not supported: {path}")

    if isinstance(audio, FLAC):
        if locked:
            audio[LOCK_VORBIS_KEY] = locked
        elif LOCK_VORBIS_KEY in audio:
            del audio[LOCK_VORBIS_KEY]  # type: ignore[no-untyped-call]
        if timestamp:
            _write_flac_tag(audio, "music_tagger_updated", timestamp)
    else:
        if audio.tags is None:
            audio.add_tags()  # type: ignore[no-untyped-call]
        assert audio.tags is not None
        audio.tags.delall(f"TXXX:{LOCK_VORBIS_KEY}")
        if locked:
            audio.tags.add(TXXX(desc=LOCK_VORBIS_KEY, text=locked))
        if timestamp:
            _write_mp3_tag(audio, "music_tagger_updated", timestamp)
    audio.save()


# Embedded lyric fields: FLAC Vorbis LYRICS, MP3 ID3 USLT frames (one per
# language/description), and M4A ©lyr. The M4A key is an ordinary atom, not a
# freeform one, so it stays out of the write_tags path that refuses M4A.
LYRICS_VORBIS_KEY = "LYRICS"
LYRICS_MP3_FRAME = "USLT"
LYRICS_MP4_ATOM = "\xa9lyr"


@dataclass(frozen=True)
class EmbeddedLyrics:
    """One lyric value stored in an audio file. language and description are MP3 only."""

    text: str
    language: str = ""
    description: str = ""


def read_embedded_lyrics(path: Path) -> list[EmbeddedLyrics]:
    """Read every embedded lyric value in a file. Read-only.

    Returns [] when the file holds no lyrics. Raises UnsupportedFileError when
    mutagen cannot identify the file, and MutagenError or OSError when it cannot
    read it. Callers decide how to report those.
    """
    audio = MutagenFile(path)
    if isinstance(audio, FLAC):
        return [
            EmbeddedLyrics(text=str(v))
            for v in audio.get(LYRICS_VORBIS_KEY, [])  # type: ignore[no-untyped-call]
        ]
    if isinstance(audio, MP3):
        if audio.tags is None:
            return []
        return [
            EmbeddedLyrics(text=str(frame.text), language=frame.lang, description=frame.desc)
            for frame in audio.tags.getall(LYRICS_MP3_FRAME)
        ]
    if isinstance(audio, MP4):
        return [
            EmbeddedLyrics(text=str(v))
            for v in audio.get(LYRICS_MP4_ATOM, [])  # type: ignore[no-untyped-call]
        ]
    raise UnsupportedFileError(f"not a FLAC, MP3, or M4A file: {path}")


RATING_TO_POPM = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
# The writer owns only the POPM frame with an empty owner. Frames from other
# owners belong to other players: this module never changes them, or their counts.
POPM_OWNER = ""
# Diff keys for the writer's POPM frame. They are not Navidrome fields.
POPM_RATING_FIELD = "popm"
POPM_COUNT_FIELD = "popm_count"
# Diff key for the MP4 rate atom (0-100).
RATE_FIELD = "rate"
# Diff keys for each container's rating fields. MP4 has no bare RATING.
_RATING_FIELDS: dict[str, tuple[str, ...]] = {
    "flac": ("fmps_rating", "rating"),
    "mp3": ("fmps_rating", "rating"),
    "mp4": ("fmps_rating", RATE_FIELD),
}
# Vorbis keys are case-insensitive, so FLAC keeps the uppercase names.
_FLAC_KEYS = {"fmps_rating": "FMPS_RATING", "rating": "RATING", "fmps_playcount": "FMPS_PLAYCOUNT"}
_MP3_ALIASES: dict[str, tuple[str, ...]] = {
    "fmps_rating": FMPS_RATING_ALIASES,
    "fmps_playcount": FMPS_PLAYCOUNT_ALIASES,
}
_MP4_ALIASES: dict[str, tuple[str, ...]] = {
    "fmps_rating": FMPS_RATING_ALIASES,
    "fmps_playcount": FMPS_PLAYCOUNT_ALIASES,
}


def _rating_to_fmps(rating: int) -> str:
    if not rating:
        return ""
    return f"{rating / 5:.1f}"


class UnsupportedFileError(ValueError):
    """A file mutagen cannot read, or whose format has no rating tags.

    Raised instead of returning no changes, so an unsupported file is never
    reported the same way as one that is already correct.
    """


class MalformedStatsError(ValueError):
    """A stored rating or play count cannot be trusted, so nothing is written.

    Raised when stored values (two spellings, or a repeated tag) disagree, or a
    stored count is not a number.
    """


@dataclass(frozen=True)
class StatsWrite:
    """The changes one statistics write made, or would make on a dry run.

    clear_withheld and decrease_withheld report snapshot values that the policy refused.
    """

    changes: list[TagChange]
    clear_withheld: bool = False
    decrease_withheld: bool = False


def _diff_targets(targets: Mapping[str, str], current: Mapping[str, str]) -> list[TagChange]:
    changes: list[TagChange] = []
    for tag_name, new_value in sorted(targets.items()):
        old_value = current.get(tag_name, "")
        if old_value != new_value:
            changes.append(TagChange(field=tag_name, old_value=old_value, new_value=new_value))
    return changes


def _own_popm(tags: ID3) -> POPM | None:
    frame = tags.get(f"POPM:{POPM_OWNER}")  # type: ignore[no-untyped-call]
    return frame if isinstance(frame, POPM) else None


def _mp3_tags(audio: MP3) -> ID3:
    if audio.tags is None:
        audio.add_tags()  # type: ignore[no-untyped-call]
    assert audio.tags is not None
    return audio.tags


def _set_own_popm(tags: ID3, rating: int | None = None, count: int | None = None) -> None:
    """Set the writer's POPM rating or count and leave the other one alone.

    A new frame starts at 0 for the value that was not given.
    """
    frame = _own_popm(tags)
    if frame is None:
        tags.add(  # type: ignore[no-untyped-call]
            POPM(email=POPM_OWNER, rating=rating or 0, count=count or 0)  # type: ignore[no-untyped-call]
        )
        return
    if rating is not None:
        frame.rating = rating
    if count is not None:
        frame.count = count


def _txxx_frames(tags: ID3, desc: str) -> list[Any]:
    """Every TXXX frame whose description matches desc, ignoring case."""
    frames: list[Any] = tags.getall("TXXX")  # type: ignore[no-untyped-call]
    return [frame for frame in frames if frame.desc.lower() == desc.lower()]


def _txxx_values(tags: ID3, desc: str) -> set[str]:
    """The distinct non-empty first text values of the TXXX frames matching desc."""
    return {frame.text[0] for frame in _txxx_frames(tags, desc) if frame.text and frame.text[0]}


def _delete_txxx(tags: ID3, desc: str) -> None:
    """Delete every case variant of a TXXX frame."""
    for frame in _txxx_frames(tags, desc):
        if frame.HashKey in tags:
            del tags[frame.HashKey]  # type: ignore[no-untyped-call]


def _set_mp3_field(audio: MP3, aliases: tuple[str, ...], value: str) -> None:
    """Replace every accepted spelling of a TXXX field. An empty value removes the field."""
    if audio.tags is None and not value:
        return
    tags = _mp3_tags(audio)
    for name in aliases:
        tags.delall(f"TXXX:{name}")  # type: ignore[no-untyped-call]
    if value:
        tags.add(TXXX(encoding=3, desc=aliases[0], text=[value]))  # type: ignore[no-untyped-call]


def _set_mp3_rating(audio: MP3, value: str) -> None:
    """Replace every case variant of TXXX:RATING. An empty value removes the field."""
    if audio.tags is None and not value:
        return
    tags = _mp3_tags(audio)
    _delete_txxx(tags, "RATING")
    if value:
        tags.add(TXXX(encoding=3, desc="RATING", text=[value]))  # type: ignore[no-untyped-call]


def _set_mp4_field(audio: MP4, aliases: tuple[str, ...], value: str) -> None:
    """Replace every accepted spelling of a freeform field. An empty value removes the field."""
    for name in aliases:
        key = f"----:com.apple.iTunes:{name}"
        if key in audio:
            del audio[key]  # type: ignore[no-untyped-call]
    if value:
        audio[f"----:com.apple.iTunes:{aliases[0]}"] = [value.encode("utf-8")]


def _set_flac_field(audio: FLAC, key: str, value: str) -> None:
    if key in audio:
        del audio[key]  # type: ignore[no-untyped-call]
    if value:
        audio[key] = [value]


def _apply_stats_changes(audio: FLAC | MP3 | MP4, changes: list[TagChange]) -> None:
    """Apply planned changes in memory. The caller saves the file."""
    for change in changes:
        field, value = change.field, change.new_value
        if isinstance(audio, FLAC):
            _set_flac_field(audio, _FLAC_KEYS[field], value)
        elif isinstance(audio, MP3):
            if field == POPM_RATING_FIELD:
                _set_own_popm(_mp3_tags(audio), rating=int(value))
            elif field == POPM_COUNT_FIELD:
                _set_own_popm(_mp3_tags(audio), count=int(value))
            elif field == "rating":
                _set_mp3_rating(audio, value)
            else:
                _set_mp3_field(audio, _MP3_ALIASES[field], value)
        elif field == RATE_FIELD:
            # mutagen renders `rate` as a text atom, so the 0-100 value is a string.
            if value:
                audio[MP4_RATE_ATOM] = [value]
            elif MP4_RATE_ATOM in audio:
                del audio[MP4_RATE_ATOM]  # type: ignore[no-untyped-call]
        else:
            _set_mp4_field(audio, _MP4_ALIASES[field], value)


def _flac_stats(
    lookup: Callable[[str], Sequence[str] | None],
) -> tuple[dict[str, str], frozenset[str]]:
    """Each stats field's single value, and the fields whose stored values disagree."""
    current: dict[str, str] = {}
    conflicts: set[str] = set()
    for name, key in _FLAC_KEYS.items():
        values = {value for value in lookup(key) or () if value}
        if len(values) > 1:
            conflicts.add(name)
        elif values:
            current[name] = values.pop()
    return current, frozenset(conflicts)


def _conflicts(fmps: FmpsStats) -> frozenset[str]:
    names = (("fmps_rating", fmps.rating), ("fmps_playcount", fmps.play_count))
    return frozenset(name for name, field in names if field.conflict)


def _mp3_stats(audio: MP3) -> tuple[dict[str, str], frozenset[str]]:
    fmps = read_fmps(audio)
    current = {"fmps_rating": fmps.rating.value, "fmps_playcount": fmps.play_count.value}
    conflicts = set(_conflicts(fmps))
    if audio.tags is not None:
        ratings = _txxx_values(audio.tags, "RATING")
        if len(ratings) > 1:
            conflicts.add("rating")
        elif ratings:
            current["rating"] = ratings.pop()
        own = _own_popm(audio.tags)
        if own is not None:
            current[POPM_RATING_FIELD] = str(own.rating)
            current[POPM_COUNT_FIELD] = str(own.count)
    return current, frozenset(conflicts)


def _mp4_stats(audio: MP4) -> tuple[dict[str, str], frozenset[str]]:
    fmps = read_fmps(audio)
    current = {"fmps_rating": fmps.rating.value, "fmps_playcount": fmps.play_count.value}
    if audio.tags is not None:
        rate = audio.tags.get(MP4_RATE_ATOM)
        if rate:
            current[RATE_FIELD] = str(rate[0])
    return current, _conflicts(fmps)


_COUNT_TEXT = re.compile(r"[0-9]+(\.[0-9]+)?")


def _file_count(current: Mapping[str, str]) -> Decimal | None:
    """The file's play count: the larger of its FMPS and POPM counts, or None if neither is set.

    A stored count may carry a fraction, such as "3.0"; it is compared as a Decimal.
    """
    counts: list[Decimal] = []
    for name in ("fmps_playcount", POPM_COUNT_FIELD):
        text = current.get(name, "")
        if not text:
            continue
        if not _COUNT_TEXT.fullmatch(text):
            raise MalformedStatsError(f"{name} holds {text!r}, which is not a count")
        counts.append(Decimal(text))
    return max(counts) if counts else None


def _plan_stats(
    kind: str,
    current: Mapping[str, str],
    conflicts: frozenset[str],
    *,
    rating: int | None,
    play_count: int | None,
    clear: bool,
    allow_decrease: bool,
) -> StatsWrite:
    """Diff the snapshot's values against a file's current values. Writes nothing."""
    if rating is not None and "fmps_rating" in conflicts:
        raise MalformedStatsError("FMPS rating stored values disagree; nothing written")
    if rating is not None and "rating" in conflicts:
        raise MalformedStatsError("RATING stored values disagree; nothing written")
    if play_count is not None and "fmps_playcount" in conflicts:
        raise MalformedStatsError("FMPS play count stored values disagree; nothing written")

    targets: dict[str, str] = {}
    clear_withheld = False
    decrease_withheld = False
    rating_fields = _RATING_FIELDS[kind]

    if rating:
        targets["fmps_rating"] = _rating_to_fmps(rating)
        if kind == "mp4":
            targets[RATE_FIELD] = str(rating * 20)
        else:
            targets["rating"] = str(rating)
        if kind == "mp3":
            # Diffed on its own, so a missing or stale frame is repaired even when
            # the TXXX tags already match.
            targets[POPM_RATING_FIELD] = str(RATING_TO_POPM.get(rating, 0))
    elif rating == 0:
        # "0" in the POPM frame is an unrated frame, not a rating to clear.
        popm_is_rated = current.get(POPM_RATING_FIELD, "0") not in ("", "0")
        has_rating = popm_is_rated or any(current.get(name) for name in rating_fields)
        if has_rating and clear:
            for name in rating_fields:
                targets[name] = ""
            if kind == "mp3" and POPM_RATING_FIELD in current:
                # The count stays; only the rating goes to zero.
                targets[POPM_RATING_FIELD] = "0"
        elif has_rating:
            clear_withheld = True

    if play_count is not None:
        file_count = _file_count(current)
        if file_count is None or play_count >= file_count or allow_decrease:
            targets["fmps_playcount"] = str(play_count)
            if kind == "mp3":
                targets[POPM_COUNT_FIELD] = str(play_count)
            # A stored count with the same value, such as "3.0", is left as it is.
            for name in ("fmps_playcount", POPM_COUNT_FIELD):
                if name in targets and current.get(name) and Decimal(current[name]) == play_count:
                    del targets[name]
        else:
            decrease_withheld = True

    return StatsWrite(_diff_targets(targets, current), clear_withheld, decrease_withheld)


def write_stats_to_file(
    path: Path,
    *,
    rating: int | None = None,
    play_count: int | None = None,
    clear: bool = False,
    allow_decrease: bool = False,
    dry_run: bool = False,
) -> StatsWrite:
    """Write a snapshot's rating and play count to one audio file.

    None gives no instruction for that value. A rating of 0 means unrated, and
    it clears the rating only with clear; otherwise the file is left alone and
    clear_withheld is set. A play count below the file's count is written only
    with allow_decrease; otherwise decrease_withheld is set. Favorites are never
    written. Nothing is written on a dry run.

    A plain FLAC is diffed from its Vorbis comments, so mutagen opens it only to
    write a change. Raises UnsupportedFileError for a file that cannot be read or
    has no rating support, and MalformedStatsError for conflicting or malformed
    stored values.
    """
    comments = read_flac_comments(path)
    if comments is not None:
        current, conflicts = _flac_stats(comments.get)
        plan = _plan_stats(
            "flac",
            current,
            conflicts,
            rating=rating,
            play_count=play_count,
            clear=clear,
            allow_decrease=allow_decrease,
        )
        if not plan.changes or dry_run:
            return plan
        flac = MutagenFile(path)
        if not isinstance(flac, FLAC):
            raise ValueError(f"{path}: FLAC header, but mutagen reads {type(flac).__name__}")
        _apply_stats_changes(flac, plan.changes)
        flac.save()
        return plan

    try:
        audio = MutagenFile(path)
    except MutagenError as exc:
        raise UnsupportedFileError(f"mutagen cannot read it: {exc}") from exc
    if audio is None:
        raise UnsupportedFileError("mutagen does not recognize the format")
    if isinstance(audio, FLAC):
        kind = "flac"
        current, conflicts = _flac_stats(audio.get)
    elif isinstance(audio, MP3):
        kind = "mp3"
        current, conflicts = _mp3_stats(audio)
    elif isinstance(audio, MP4):
        kind = "mp4"
        current, conflicts = _mp4_stats(audio)
    else:
        raise UnsupportedFileError(f"{type(audio).__name__} has no rating support")

    plan = _plan_stats(
        kind,
        current,
        conflicts,
        rating=rating,
        play_count=play_count,
        clear=clear,
        allow_decrease=allow_decrease,
    )
    if plan.changes and not dry_run:
        _apply_stats_changes(audio, plan.changes)
        audio.save()
    return plan


def write_rating_to_file(path: Path, rating: int, dry_run: bool = False) -> list[TagChange]:
    """Write the rating tags only; play counts are left alone. Returns changes made.

    Favorites (STARRED, STARRED_AT) are neither written nor cleared. See
    write_stats_to_file for the errors raised.
    """
    return write_stats_to_file(path, rating=rating, dry_run=dry_run).changes


def embed_cover_art(album_dir: Path, image_data: bytes, mime: str) -> int:
    """Embed cover art into all audio files in album_dir. Returns count of files updated."""

    files = sorted(f for f in album_dir.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS)
    count = 0
    for path in files:
        audio = MutagenFile(path)
        if audio is None:
            continue
        if isinstance(audio, FLAC):
            audio.clear_pictures()  # type: ignore[no-untyped-call]
            pic = Picture()  # type: ignore[no-untyped-call]
            pic.type = 3  # Front Cover
            pic.mime = mime
            pic.data = image_data
            audio.add_picture(pic)  # type: ignore[no-untyped-call]
            audio.save()
            count += 1
        elif isinstance(audio, MP3):
            if audio.tags is None:
                audio.add_tags()  # type: ignore[no-untyped-call]
            assert audio.tags is not None
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_data))
            audio.save()
            count += 1
    return count
