from pathlib import Path

import pytest
from mutagen.id3 import ID3, TXXX, UFID

from music_tagger.tags import AlbumTags, TagChange, compute_diff, read_album, write_tags

OWNER = "http://musicbrainz.org"
FIRST = "e1d41239-9571-4c2c-9dde-d86bb689f0cd"
SECOND = "7bd8a26a-951a-4a9d-ad8f-eaea79337416"


@pytest.mark.parametrize("description", ["MusicBrainz Recording Id", "MusicBrainz Track Id"])
def test_legacy_recording_id_migrates_once_through_saved_evidence(
    mp3_album: Path, description: str
) -> None:
    path = read_album(mp3_album).tracks[0].path
    tags = ID3(path)
    tags.delall(f"UFID:{OWNER}")
    for frame in tags.getall("TXXX"):
        if frame.desc.casefold() in {"musicbrainz recording id", "musicbrainz track id"}:
            tags.delall(frame.HashKey)
    tags.add(TXXX(desc=description, text=[FIRST]))
    tags.save(path)

    evidence = AlbumTags.from_dict(read_album(mp3_album).to_dict())
    track = evidence.tracks[0]
    assert track.tags["musicbrainz_recordingid"] == FIRST
    assert track.recording_id_needs_sync
    changes = compute_diff(track, {"musicbrainz_recordingid": FIRST})
    assert len(changes) == 1
    write_tags(track, changes)

    assert ID3(path)[f"UFID:{OWNER}"].data == FIRST.encode("ascii")
    updated = read_album(mp3_album).tracks[0]
    assert not updated.recording_id_needs_sync
    assert compute_diff(updated, {"musicbrainz_recordingid": FIRST}) == []


def test_recording_id_write_synchronizes_aliases_and_keeps_foreign_ufid(mp3_album: Path) -> None:
    path = read_album(mp3_album).tracks[0].path
    tags = ID3(path)
    tags.add(UFID(owner=OWNER, data=FIRST.encode("ascii")))
    tags.add(UFID(owner="https://example.org", data=b"foreign identifier"))
    tags.add(TXXX(desc="MusicBrainz Recording Id", text=[SECOND]))
    tags.add(TXXX(desc="MusicBrainz Track Id", text=[SECOND]))
    tags.save(path)

    track = read_album(mp3_album).tracks[0]
    assert track.tags["musicbrainz_recordingid"] == FIRST
    assert track.recording_id_needs_sync
    write_tags(track, [TagChange("musicbrainz_recordingid", FIRST, SECOND)])

    tags = ID3(path)
    assert tags[f"UFID:{OWNER}"].data == SECOND.encode("ascii")
    assert tags["TXXX:MusicBrainz Recording Id"].text == [SECOND]
    assert tags["TXXX:MusicBrainz Track Id"].text == [SECOND]
    assert tags["UFID:https://example.org"].data == b"foreign identifier"
    assert not read_album(mp3_album).tracks[0].recording_id_needs_sync


def test_standard_only_recording_id_needs_no_migration(mp3_album: Path) -> None:
    path = read_album(mp3_album).tracks[0].path
    tags = ID3(path)
    for frame in tags.getall("TXXX"):
        if frame.desc.casefold() in {"musicbrainz recording id", "musicbrainz track id"}:
            tags.delall(frame.HashKey)
    tags.add(UFID(owner=OWNER, data=FIRST.encode("ascii")))
    tags.save(path)
    track = read_album(mp3_album).tracks[0]
    assert track.tags["musicbrainz_recordingid"] == FIRST
    assert not track.recording_id_needs_sync
    assert compute_diff(track, {"musicbrainz_recordingid": FIRST}) == []


def test_recording_id_clear_removes_all_owned_representations(mp3_album: Path) -> None:
    path = read_album(mp3_album).tracks[0].path
    tags = ID3(path)
    tags.add(UFID(owner=OWNER, data=FIRST.encode("ascii")))
    tags.add(UFID(owner="https://example.org", data=b"keep"))
    tags.add(TXXX(desc="MusicBrainz Recording Id", text=[FIRST]))
    tags.add(TXXX(desc="MusicBrainz Track Id", text=[FIRST]))
    tags.save(path)

    track = read_album(mp3_album).tracks[0]
    write_tags(track, compute_diff(track, {"musicbrainz_recordingid": ""}))
    tags = ID3(path)
    assert f"UFID:{OWNER}" not in tags
    assert "TXXX:MusicBrainz Recording Id" not in tags
    assert "TXXX:MusicBrainz Track Id" not in tags
    assert tags["UFID:https://example.org"].data == b"keep"
