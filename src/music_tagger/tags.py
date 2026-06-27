"""Read and write audio tags via mutagen.

Provides a unified interface over FLAC (Vorbis Comments) and MP3 (ID3v2).
Field names follow Navidrome's mappings.yaml conventions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import TXXX, ID3
from mutagen.mp3 import MP3

AUDIO_EXTENSIONS = {".flac", ".mp3"}

_MB_DISAMBIG_TERMS = {
    "acoustic", "demo", "live", "radio edit", "remaster",
    "remastered", "remix", "single edit",
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
}


@dataclass
class TrackTags:
    path: Path
    tags: dict[str, str] = field(default_factory=dict)
    duration_secs: float = 0.0
    format: str = ""


@dataclass
class AlbumTags:
    directory: Path
    tracks: list[TrackTags] = field(default_factory=list)

    @property
    def artist(self) -> str:
        return _most_common(t.tags.get("albumartist") or t.tags.get("artist", "") for t in self.tracks)

    @property
    def album(self) -> str:
        return _most_common(t.tags.get("album", "") for t in self.tracks)

    @property
    def track_count(self) -> int:
        return len(self.tracks)


def _most_common(values: Any) -> str:
    from collections import Counter

    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _read_flac_tags(audio: FLAC) -> dict[str, str]:
    tags: dict[str, str] = {}
    for canonical, (vorbis_key, _) in FIELD_MAP.items():
        values = audio.get(vorbis_key)
        if values:
            tags[canonical] = values[0]
    return tags


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


def read_album(directory: Path) -> AlbumTags:
    """Read tags from all audio files in a directory."""
    album = AlbumTags(directory=directory)
    files = sorted(
        f for f in directory.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS
    )
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
        else:
            continue
        duration = audio.info.length if audio.info else 0.0
        album.tracks.append(
            TrackTags(path=path, tags=tags, duration_secs=duration, format=fmt)
        )
    return album


def _write_flac_tag(audio: FLAC, canonical: str, value: str) -> None:
    vorbis_key = FIELD_MAP[canonical][0]
    audio[vorbis_key] = [value]


def _write_mp3_tag(audio: MP3, canonical: str, value: str) -> None:
    if audio.tags is None:
        audio.add_tags()
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


def compute_diff(
    track: TrackTags, new_tags: dict[str, str]
) -> list[TagChange]:
    """Compute the tag changes needed for a single track."""
    # Fields where comparison should be case-insensitive
    case_insensitive = {"releasestatus", "releasetype"}

    changes = []
    for field_name, new_value in sorted(new_tags.items()):
        old_value = track.tags.get(field_name, "")
        if field_name in case_insensitive:
            if old_value.lower() == new_value.lower():
                continue
        elif old_value == new_value:
            continue
        if field_name == "title" and old_value and new_value.startswith(old_value):
            suffix = new_value[len(old_value):].strip()
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
    for change in changes:
        if change.field not in FIELD_MAP:
            continue
        if isinstance(audio, FLAC):
            _write_flac_tag(audio, change.field, change.new_value)
        elif isinstance(audio, MP3):
            _write_mp3_tag(audio, change.field, change.new_value)
    audio.save()
