"""Read and write audio tags via mutagen.

Provides a unified interface over FLAC (Vorbis Comments) and MP3 (ID3v2).
Field names follow Navidrome's mappings.yaml conventions.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, POPM, TCON, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a"}

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
    "fmps_rating": "----:com.apple.iTunes:FMPS_RATING",
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
    "fmps_rating": ("FMPS_RATING", "TXXX:FMPS_RATING"),
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


RATING_TO_POPM = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}


def _rating_to_fmps(rating: int) -> str:
    if not rating:
        return ""
    return f"{rating / 5:.1f}"


def _rating_targets(rating: int, starred: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    if rating:
        targets["fmps_rating"] = _rating_to_fmps(rating)
        targets["rating"] = str(rating)
    if starred:
        targets["starred"] = "true"
        targets["starred_at"] = starred
    return targets


def _diff_targets(targets: Mapping[str, str], current: Mapping[str, str]) -> list[TagChange]:
    changes: list[TagChange] = []
    for tag_name, new_value in sorted(targets.items()):
        old_value = current.get(tag_name, "")
        if old_value != new_value:
            changes.append(TagChange(field=tag_name, old_value=old_value, new_value=new_value))
    return changes


def _write_rating_changes(audio: FLAC | MP3 | MP4, changes: list[TagChange], rating: int) -> None:
    if isinstance(audio, FLAC):
        for change in changes:
            _write_flac_tag(audio, change.field, change.new_value)
    elif isinstance(audio, MP3):
        for change in changes:
            _write_mp3_tag(audio, change.field, change.new_value)
        if rating:
            if audio.tags is None:
                audio.add_tags()  # type: ignore[no-untyped-call]
            assert audio.tags is not None
            audio.tags.delall("POPM")
            audio.tags.add(POPM(email="", rating=RATING_TO_POPM.get(rating, 0), count=0))
    elif isinstance(audio, MP4):
        for change in changes:
            audio[MP4_MAP[change.field]] = [change.new_value.encode("utf-8")]
        if rating:
            # mutagen renders `rate` as a text atom, so the 0-100 value is a string.
            audio[MP4_RATE_ATOM] = [str(rating * 20)]
    audio.save()


def write_rating_to_file(
    path: Path, rating: int, starred: str = "", dry_run: bool = False
) -> list[TagChange]:
    """Write rating and starred tags to an audio file. Returns changes made.

    A plain FLAC is checked from its Vorbis comments, so mutagen opens it only
    to write a change. A FLAC that changes is therefore read twice: once by the
    header reader, then by mutagen.
    """
    targets = _rating_targets(rating, starred)

    comments = read_flac_comments(path)
    if comments is not None:
        changes = _diff_targets(targets, _flac_tags_from_lookup(comments.get))
        if not changes or dry_run:
            return changes
        audio = MutagenFile(path)
        if not isinstance(audio, FLAC):
            raise ValueError(f"{path}: FLAC header, but mutagen reads {type(audio).__name__}")
        _write_rating_changes(audio, changes, rating)
        return changes

    audio = MutagenFile(path)
    if audio is None:
        return []
    if isinstance(audio, FLAC):
        current = _read_flac_tags(audio)
    elif isinstance(audio, MP3):
        current = _read_mp3_tags(audio)
    elif isinstance(audio, MP4):
        current = _read_mp4_tags(audio)
        targets.pop("rating", None)  # see MP4_RATE_ATOM: scales collide
    else:
        return []

    changes = _diff_targets(targets, current)
    if not changes or dry_run:
        return changes
    _write_rating_changes(audio, changes, rating)
    return changes


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
