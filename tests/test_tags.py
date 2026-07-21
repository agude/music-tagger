from __future__ import annotations

from pathlib import Path

from music_tagger.tags import (
    RATING_TO_POPM,
    AlbumTags,
    TagChange,
    TrackTags,
    _rating_to_fmps,
    compute_diff,
    embed_cover_art,
    read_album,
    write_rating_to_file,
    write_tags,
)


class TestTrackTagsSerialization:
    def test_round_trip(self) -> None:
        track = TrackTags(
            path=Path("/music/01.flac"),
            tags={"title": "Song", "artist": "Band"},
            duration_secs=234.5,
            format="flac",
        )
        data = track.to_dict()
        restored = TrackTags.from_dict(data)
        assert restored.path == track.path
        assert restored.tags == track.tags
        assert restored.duration_secs == track.duration_secs
        assert restored.format == track.format

    def test_dict_keys(self) -> None:
        track = TrackTags(
            path=Path("/a.flac"), tags={"title": "X"}, duration_secs=1.0, format="flac"
        )
        d = track.to_dict()
        assert d["path"] == "/a.flac"
        assert d["format"] == "flac"
        assert d["duration_secs"] == 1.0
        assert d["tags"] == {"title": "X"}


class TestAlbumTagsSerialization:
    def test_round_trip(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        data = album.to_dict()
        restored = AlbumTags.from_dict(data)
        assert restored.directory == album.directory
        assert restored.track_count == album.track_count
        assert restored.artist == album.artist
        assert restored.album == album.album
        for orig, rest in zip(album.tracks, restored.tracks, strict=True):
            assert rest.path == orig.path
            assert rest.tags == orig.tags

    def test_dict_includes_computed_fields(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        d = album.to_dict()
        assert d["artist"] == "Eagles"
        assert d["album"] == "Desperado"
        assert d["track_count"] == 3
        assert len(d["tracks"]) == 3


class TestTagChangeSerialization:
    def test_round_trip(self) -> None:
        change = TagChange(field="title", old_value="Old", new_value="New")
        restored = TagChange.from_dict(change.to_dict())
        assert restored.field == "title"
        assert restored.old_value == "Old"
        assert restored.new_value == "New"


class TestReadAlbumFlac:
    def test_track_count(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        assert album.track_count == 3

    def test_artist(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        assert album.artist == "Eagles"

    def test_album_name(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        assert album.album == "Desperado"

    def test_track_order(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        titles = [t.tags["title"] for t in album.tracks]
        assert titles == ["Desperado", "Twenty-One", "Out of Control"]

    def test_duration_positive(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        for track in album.tracks:
            assert track.duration_secs > 0

    def test_format(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        for track in album.tracks:
            assert track.format == "flac"

    def test_tags_present(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        assert track.tags["tracknumber"] == "1"
        assert track.tags["discnumber"] == "1"
        assert track.tags["date"] == "1973"
        assert track.tags["musicbrainz_albumid"] == "test-album-id-1"
        assert track.tags["barcode"] == "075596066624"


class TestReadAlbumMp3:
    def test_track_count(self, mp3_album: Path) -> None:
        album = read_album(mp3_album)
        assert album.track_count == 3

    def test_artist(self, mp3_album: Path) -> None:
        album = read_album(mp3_album)
        assert album.artist == "Eagles"

    def test_tags_present(self, mp3_album: Path) -> None:
        album = read_album(mp3_album)
        track = album.tracks[0]
        assert track.tags["title"] == "Desperado"
        assert track.tags["musicbrainz_albumid"] == "test-album-id-1"
        assert track.tags["barcode"] == "075596066624"

    def test_format(self, mp3_album: Path) -> None:
        album = read_album(mp3_album)
        for track in album.tracks:
            assert track.format == "mp3"


class TestReadAlbumEmpty:
    def test_empty_dir(self, tmp_path: Path) -> None:
        album = read_album(tmp_path)
        assert album.track_count == 0
        assert album.artist == ""
        assert album.album == ""


class TestComputeDiff:
    def test_no_changes(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"title": "Desperado", "date": "1973"})
        assert changes == []

    def test_changed_field(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"title": "New Title"})
        assert len(changes) == 1
        assert changes[0].field == "title"
        assert changes[0].old_value == "Desperado"
        assert changes[0].new_value == "New Title"

    def test_new_field(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"label": "Asylum Records"})
        assert len(changes) == 1
        assert changes[0].old_value == ""
        assert changes[0].new_value == "Asylum Records"

    def test_title_ignores_mb_disambig_suffix(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"title": "Desperado (single edit)"})
        assert changes == []

    def test_title_ignores_live_suffix(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"title": "Desperado (live & acoustic)"})
        assert changes == []

    def test_title_keeps_legit_parenthetical(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"title": "Desperado (reprise)"})
        assert len(changes) == 1

    def test_title_keeps_different_title_with_parens(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = compute_diff(track, {"title": "Heart (Broken)"})
        assert len(changes) == 1

    def test_releasestatus_case_insensitive(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        track.tags["releasestatus"] = "official"
        changes = compute_diff(track, {"releasestatus": "Official"})
        assert changes == []

    def test_releasestatus_different_value(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        track.tags["releasestatus"] = "official"
        changes = compute_diff(track, {"releasestatus": "Promotion"})
        assert len(changes) == 1


class TestWriteTagsFlac:
    def test_write_and_read_back(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        changes = [
            TagChange(field="title", old_value="Desperado", new_value="New Title"),
            TagChange(field="label", old_value="", new_value="Asylum Records"),
        ]
        write_tags(track, changes)

        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["title"] == "New Title"
        assert album2.tracks[0].tags["label"] == "Asylum Records"

    def test_write_musicbrainz_id(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        new_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        changes = [
            TagChange(field="musicbrainz_albumid", old_value="test-album-id-1", new_value=new_id)
        ]
        write_tags(track, changes)

        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["musicbrainz_albumid"] == new_id

    def test_write_no_changes(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        write_tags(track, [])
        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["title"] == "Desperado"


class TestEmbedCoverArt:
    def test_embed_flac(self, flac_album: Path, tmp_path: Path) -> None:
        from mutagen.flac import FLAC

        image = tmp_path / "cover.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        count = embed_cover_art(flac_album, image)

        assert count == 3
        for path in sorted(flac_album.iterdir()):
            if path.suffix.lower() != ".flac":
                continue
            audio = FLAC(path)
            pics = audio.pictures
            assert len(pics) == 1
            assert pics[0].type == 3
            assert pics[0].mime == "image/jpeg"
            assert pics[0].data == image.read_bytes()

    def test_embed_mp3(self, mp3_album: Path, tmp_path: Path) -> None:
        from mutagen.mp3 import MP3

        image = tmp_path / "cover.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        count = embed_cover_art(mp3_album, image)

        assert count == 3
        for path in sorted(mp3_album.iterdir()):
            if path.suffix.lower() != ".mp3":
                continue
            audio = MP3(path)
            assert audio.tags is not None
            apic_frames = audio.tags.getall("APIC")
            assert len(apic_frames) == 1
            assert apic_frames[0].type == 3
            assert apic_frames[0].mime == "image/jpeg"
            assert apic_frames[0].data == image.read_bytes()

    def test_png_mime(self, flac_album: Path, tmp_path: Path) -> None:
        from mutagen.flac import FLAC

        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        embed_cover_art(flac_album, image)

        audio = FLAC(sorted(flac_album.glob("*.flac"))[0])
        assert audio.pictures[0].mime == "image/png"

    def test_returns_count(self, flac_album: Path, tmp_path: Path) -> None:
        image = tmp_path / "cover.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        count = embed_cover_art(flac_album, image)
        assert count == 3

    def test_replaces_existing(self, flac_album: Path, tmp_path: Path) -> None:
        from mutagen.flac import FLAC

        image1 = tmp_path / "cover1.jpg"
        image1.write_bytes(b"old image data")
        embed_cover_art(flac_album, image1)

        image2 = tmp_path / "cover2.jpg"
        image2.write_bytes(b"new image data")
        embed_cover_art(flac_album, image2)

        audio = FLAC(sorted(flac_album.glob("*.flac"))[0])
        assert len(audio.pictures) == 1
        assert audio.pictures[0].data == b"new image data"


class TestWriteTagsMp3:
    def test_write_and_read_back(self, mp3_album: Path) -> None:
        album = read_album(mp3_album)
        track = album.tracks[0]
        changes = [
            TagChange(field="title", old_value="Desperado", new_value="New Title"),
            TagChange(field="label", old_value="", new_value="Asylum Records"),
        ]
        write_tags(track, changes)

        album2 = read_album(mp3_album)
        assert album2.tracks[0].tags["title"] == "New Title"
        assert album2.tracks[0].tags["label"] == "Asylum Records"

    def test_write_musicbrainz_id(self, mp3_album: Path) -> None:
        album = read_album(mp3_album)
        track = album.tracks[0]
        new_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        changes = [
            TagChange(field="musicbrainz_albumid", old_value="test-album-id-1", new_value=new_id)
        ]
        write_tags(track, changes)

        album2 = read_album(mp3_album)
        assert album2.tracks[0].tags["musicbrainz_albumid"] == new_id


class TestRatingToFmps:
    def test_values(self) -> None:
        assert _rating_to_fmps(1) == "0.2"
        assert _rating_to_fmps(2) == "0.4"
        assert _rating_to_fmps(3) == "0.6"
        assert _rating_to_fmps(4) == "0.8"
        assert _rating_to_fmps(5) == "1.0"

    def test_zero(self) -> None:
        assert _rating_to_fmps(0) == ""


class TestWriteRatingFlac:
    def test_writes_rating_tags(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        changes = write_rating_to_file(track_path, rating=4, starred="2024-01-15T10:00:00Z")

        assert len(changes) == 4
        fields = {c.field for c in changes}
        assert fields == {"fmps_rating", "rating", "starred", "starred_at"}

        album = read_album(flac_album)
        tags = album.tracks[0].tags
        assert tags["fmps_rating"] == "0.8"
        assert tags["rating"] == "4"
        assert tags["starred"] == "true"
        assert tags["starred_at"] == "2024-01-15T10:00:00Z"

    def test_rating_only(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        changes = write_rating_to_file(track_path, rating=3)

        fields = {c.field for c in changes}
        assert fields == {"fmps_rating", "rating"}

        album = read_album(flac_album)
        assert album.tracks[0].tags["rating"] == "3"
        assert "starred" not in album.tracks[0].tags

    def test_no_changes_when_same(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_rating_to_file(track_path, rating=5)
        changes = write_rating_to_file(track_path, rating=5)
        assert changes == []

    def test_dry_run(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        changes = write_rating_to_file(track_path, rating=4, dry_run=True)

        assert len(changes) > 0
        album = read_album(flac_album)
        assert "rating" not in album.tracks[0].tags


class TestWriteRatingMp3:
    def test_writes_rating_and_popm(self, mp3_album: Path) -> None:
        from mutagen.mp3 import MP3

        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        changes = write_rating_to_file(track_path, rating=5, starred="2024-06-01T00:00:00Z")

        fields = {c.field for c in changes}
        assert fields == {"fmps_rating", "rating", "starred", "starred_at"}

        album = read_album(mp3_album)
        tags = album.tracks[0].tags
        assert tags["fmps_rating"] == "1.0"
        assert tags["rating"] == "5"
        assert tags["starred"] == "true"

        audio = MP3(track_path)
        popm_frames = audio.tags.getall("POPM")
        assert len(popm_frames) == 1
        assert popm_frames[0].rating == RATING_TO_POPM[5]

    def test_popm_values(self, mp3_album: Path) -> None:
        from mutagen.mp3 import MP3

        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        for star_rating, popm_value in RATING_TO_POPM.items():
            write_rating_to_file(track_path, rating=star_rating)
            audio = MP3(track_path)
            popm = audio.tags.getall("POPM")
            assert len(popm) == 1
            assert popm[0].rating == popm_value, f"rating {star_rating} → POPM {popm_value}"

    def test_dry_run(self, mp3_album: Path) -> None:
        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        changes = write_rating_to_file(track_path, rating=3, dry_run=True)

        assert len(changes) > 0
        album = read_album(mp3_album)
        assert "rating" not in album.tracks[0].tags
