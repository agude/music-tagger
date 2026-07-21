"""Read and write audio tags via mutagen.

Provides a unified interface over FLAC (Vorbis Comments) and MP3 (ID3v2).
Field names follow Navidrome's mappings.yaml conventions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, POPM, TXXX
from mutagen.mp3 import MP3

AUDIO_EXTENSIONS = {".flac", ".mp3"}

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


@dataclass
class TrackTags:
    path: Path
    tags: dict[str, str] = field(default_factory=dict)
    duration_secs: float = 0.0
    format: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": self.format,
            "duration_secs": self.duration_secs,
            "tags": dict(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackTags:
        return cls(
            path=Path(data["path"]),
            tags=data.get("tags", {}),
            duration_secs=data.get("duration_secs", 0.0),
            format=data.get("format", ""),
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


def _read_flac_tags(audio: FLAC) -> dict[str, str]:
    tags: dict[str, str] = {}
    for canonical, (vorbis_key, _) in FIELD_MAP.items():
        values = audio.get(vorbis_key)  # type: ignore[no-untyped-call]
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
        else:
            continue
        duration = audio.info.length if audio.info else 0.0
        album.tracks.append(TrackTags(path=path, tags=tags, duration_secs=duration, format=fmt))
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
    for change in changes:
        if change.field not in FIELD_MAP:
            continue
        if isinstance(audio, FLAC):
            _write_flac_tag(audio, change.field, change.new_value)
        elif isinstance(audio, MP3):
            _write_mp3_tag(audio, change.field, change.new_value)
    audio.save()


RATING_TO_POPM = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}


def _rating_to_fmps(rating: int) -> str:
    if not rating:
        return ""
    return f"{rating / 5:.1f}"


def write_rating_to_file(
    path: Path, rating: int, starred: str = "", dry_run: bool = False
) -> list[TagChange]:
    """Write rating and starred tags to an audio file. Returns changes made."""
    audio = MutagenFile(path)
    if audio is None:
        return []

    targets: dict[str, str] = {}
    if rating:
        targets["fmps_rating"] = _rating_to_fmps(rating)
        targets["rating"] = str(rating)
    if starred:
        targets["starred"] = "true"
        targets["starred_at"] = starred

    if isinstance(audio, FLAC):
        current = _read_flac_tags(audio)
    elif isinstance(audio, MP3):
        current = _read_mp3_tags(audio)
    else:
        return []

    changes: list[TagChange] = []
    for tag_name, new_value in sorted(targets.items()):
        old_value = current.get(tag_name, "")
        if old_value != new_value:
            changes.append(TagChange(field=tag_name, old_value=old_value, new_value=new_value))

    if not changes or dry_run:
        return changes

    if isinstance(audio, FLAC):
        for change in changes:
            _write_flac_tag(audio, change.field, change.new_value)
    elif isinstance(audio, MP3):
        for change in changes:
            _write_mp3_tag(audio, change.field, change.new_value)
        if rating:
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("POPM")
            audio.tags.add(POPM(email="", rating=RATING_TO_POPM.get(rating, 0), count=0))

    audio.save()
    return changes


def embed_cover_art(album_dir: Path, image_path: Path) -> int:
    """Embed cover art into all audio files in album_dir. Returns count of files updated."""
    image_data = image_path.read_bytes()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    files = sorted(f for f in album_dir.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS)
    count = 0
    for path in files:
        audio = MutagenFile(path)
        if audio is None:
            continue
        if isinstance(audio, FLAC):
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3  # Front Cover
            pic.mime = mime
            pic.data = image_data
            audio.add_picture(pic)
            audio.save()
            count += 1
        elif isinstance(audio, MP3):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_data))
            audio.save()
            count += 1
    return count
