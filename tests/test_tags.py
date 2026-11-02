from __future__ import annotations

import io
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, POPM, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from music_tagger.tags import (
    RATING_TO_POPM,
    AlbumTags,
    FmpsField,
    FmpsStats,
    MalformedStatsError,
    StatsWrite,
    TagChange,
    TrackTags,
    UnsupportedFileError,
    _flac_comments_from_stream,
    _rating_to_fmps,
    compute_diff,
    embed_cover_art,
    read_album,
    read_flac_comments,
    read_fmps,
    read_genres,
    read_locks,
    write_genres,
    write_locks,
    write_rating_to_file,
    write_stats_to_file,
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
    IMAGE = b"\xff\xd8\xff" + b"\x00" * 100

    def test_embed_flac(self, flac_album: Path) -> None:
        from mutagen.flac import FLAC

        count = embed_cover_art(flac_album, self.IMAGE, "image/jpeg")

        assert count == 3
        for path in sorted(flac_album.glob("*.flac")):
            pics = FLAC(path).pictures
            assert len(pics) == 1
            assert pics[0].type == 3
            assert pics[0].mime == "image/jpeg"
            assert pics[0].data == self.IMAGE

    def test_embed_mp3(self, mp3_album: Path) -> None:
        from mutagen.mp3 import MP3

        count = embed_cover_art(mp3_album, self.IMAGE, "image/jpeg")

        assert count == 3
        for path in sorted(mp3_album.glob("*.mp3")):
            audio = MP3(path)
            assert audio.tags is not None
            apic_frames = audio.tags.getall("APIC")
            assert len(apic_frames) == 1
            assert apic_frames[0].type == 3
            assert apic_frames[0].mime == "image/jpeg"
            assert apic_frames[0].data == self.IMAGE

    def test_writes_given_mime(self, flac_album: Path) -> None:
        from mutagen.flac import FLAC

        embed_cover_art(flac_album, b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/png")

        audio = FLAC(sorted(flac_album.glob("*.flac"))[0])
        assert audio.pictures[0].mime == "image/png"

    def test_replaces_existing(self, flac_album: Path) -> None:
        from mutagen.flac import FLAC

        embed_cover_art(flac_album, b"old image data", "image/jpeg")
        embed_cover_art(flac_album, b"new image data", "image/jpeg")

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
        changes = write_rating_to_file(track_path, rating=4)

        assert len(changes) == 2
        fields = {c.field for c in changes}
        assert fields == {"fmps_rating", "rating"}

        album = read_album(flac_album)
        tags = album.tracks[0].tags
        assert tags["fmps_rating"] == "0.8"
        assert tags["rating"] == "4"
        assert "starred" not in tags
        assert "starred_at" not in tags

    def test_existing_favorite_is_preserved(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        audio = FLAC(track_path)
        audio["STARRED"] = "2020-01-01T00:00:00Z"
        audio.save()

        changes = write_rating_to_file(track_path, rating=2)

        assert {c.field for c in changes} == {"fmps_rating", "rating"}
        album = read_album(flac_album)
        assert album.tracks[0].tags["starred"] == "2020-01-01T00:00:00Z"

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
        changes = write_rating_to_file(track_path, rating=5)

        fields = {c.field for c in changes}
        assert fields == {"fmps_rating", "rating", "popm"}

        album = read_album(mp3_album)
        tags = album.tracks[0].tags
        assert tags["fmps_rating"] == "1.0"
        assert tags["rating"] == "5"
        assert "starred" not in tags

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

    def test_zero_rating_writes_nothing(self, mp3_album: Path) -> None:
        from mutagen.mp3 import MP3

        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        changes = write_rating_to_file(track_path, rating=0)

        assert changes == []

        album = read_album(mp3_album)
        tags = album.tracks[0].tags
        assert "rating" not in tags
        assert "starred" not in tags

        audio = MP3(track_path)
        popm_frames = audio.tags.getall("POPM")
        assert len(popm_frames) == 0

    def test_dry_run(self, mp3_album: Path) -> None:
        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        changes = write_rating_to_file(track_path, rating=3, dry_run=True)

        assert len(changes) > 0
        album = read_album(mp3_album)
        assert "rating" not in album.tracks[0].tags


class TestGenresFlac:
    def test_writes_separate_tag_values(self, flac_album: Path) -> None:
        from mutagen.flac import FLAC

        track_path = sorted(flac_album.glob("*.flac"))[0]
        assert write_genres(track_path, ["Pop Rock", "Y2K Rock"]) is True

        # Two distinct GENRE comments, not one delimited string.
        assert FLAC(track_path)["GENRE"] == ["Pop Rock", "Y2K Rock"]
        assert read_genres(track_path) == ["Pop Rock", "Y2K Rock"]

    def test_replaces_existing(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_genres(track_path, ["Retro Rock", "Folk Rock"])
        write_genres(track_path, ["Jazz"])

        assert read_genres(track_path) == ["Jazz"]

    def test_empty_list_clears(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_genres(track_path, ["Jazz"])
        write_genres(track_path, [])

        assert read_genres(track_path) == []

    def test_stamps_timestamp(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_genres(track_path, ["Jazz"], timestamp="2026-08-03T00:00:00Z")

        album = read_album(flac_album)
        assert album.tracks[0].tags["music_tagger_updated"] == "2026-08-03T00:00:00Z"

    def test_read_album_keeps_first_value_only(self, flac_album: Path) -> None:
        """read_album flattens genre; read_genres is why edits must not use it."""
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_genres(track_path, ["Retro Rock", "Folk Rock"])

        assert read_album(flac_album).tracks[0].tags["genre"] == "Retro Rock"
        assert read_genres(track_path) == ["Retro Rock", "Folk Rock"]


class TestGenresMp3:
    def test_writes_single_multivalue_frame(self, mp3_album: Path) -> None:
        from mutagen.mp3 import MP3

        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        assert write_genres(track_path, ["Pop Rock", "Y2K Rock"]) is True

        frames = MP3(track_path).tags.getall("TCON")
        assert len(frames) == 1
        assert frames[0].text == ["Pop Rock", "Y2K Rock"]
        assert read_genres(track_path) == ["Pop Rock", "Y2K Rock"]

    def test_replaces_existing(self, mp3_album: Path) -> None:
        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        write_genres(track_path, ["Retro Rock", "Folk Rock"])
        write_genres(track_path, ["Jazz"])

        assert read_genres(track_path) == ["Jazz"]

    def test_empty_list_clears(self, mp3_album: Path) -> None:
        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        write_genres(track_path, ["Jazz"])
        write_genres(track_path, [])

        assert read_genres(track_path) == []


class TestReadGenresEdgeCases:
    def test_non_audio_returns_false(self, tmp_path: Path) -> None:
        junk = tmp_path / "notes.txt"
        junk.write_text("not audio")
        assert write_genres(junk, ["Jazz"]) is False


class TestGenresM4a:
    def test_reads_native_multivalue(self, m4a_album: Path) -> None:
        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        assert read_genres(track_path) == ["Retro Rock", "Folk Rock"]

    def test_writes_separate_atom_values(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        assert write_genres(track_path, ["Pop Rock", "Y2K Rock"]) is True

        assert MP4(track_path)["\xa9gen"] == ["Pop Rock", "Y2K Rock"]
        assert read_genres(track_path) == ["Pop Rock", "Y2K Rock"]

    def test_empty_list_clears(self, m4a_album: Path) -> None:
        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        write_genres(track_path, [])
        assert read_genres(track_path) == []

    def test_stamps_timestamp(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        write_genres(track_path, ["Jazz"], timestamp="2026-08-03T00:00:00Z")

        stamp = MP4(track_path)["----:com.apple.iTunes:MUSIC_TAGGER_UPDATED"]
        assert bytes(stamp[0]).decode() == "2026-08-03T00:00:00Z"


class TestReadAlbumM4a:
    def test_track_count_and_format(self, m4a_album: Path) -> None:
        album = read_album(m4a_album)
        assert album.track_count == 3
        assert {t.format for t in album.tracks} == {"m4a"}

    def test_artist_and_album(self, m4a_album: Path) -> None:
        album = read_album(m4a_album)
        assert album.artist == "Eagles"
        assert album.album == "Desperado"

    def test_freeform_atom_decoded_to_str(self, m4a_album: Path) -> None:
        """---- atoms come back as MP4FreeForm bytes, not str."""
        tags = read_album(m4a_album).tracks[0].tags
        assert tags["musicbrainz_albumid"] == "test-album-id-1"

    def test_genre_flattened_to_first_value(self, m4a_album: Path) -> None:
        tags = read_album(m4a_album).tracks[0].tags
        assert tags["genre"] == "Retro Rock"


class TestWriteTagsUnsupportedFormat:
    def test_m4a_raises_rather_than_silently_skipping(self, m4a_album: Path) -> None:
        import pytest

        track = read_album(m4a_album).tracks[0]
        with pytest.raises(NotImplementedError, match="not supported"):
            write_tags(track, [TagChange(field="title", old_value="a", new_value="b")])


class TestRatingM4a:
    def test_writes_fmps_and_rate_atom(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        changes = write_rating_to_file(track_path, rating=4)

        fields = {c.field for c in changes}
        assert fields == {"fmps_rating", "rate"}

        a = MP4(track_path)
        assert bytes(a["----:com.apple.iTunes:FMPS_Rating"][0]).decode() == "0.8"
        assert "----:com.apple.iTunes:STARRED" not in a
        assert a["rate"] == ["80"]

    def test_omits_bare_rating_atom(self, m4a_album: Path) -> None:
        """RATING is 1-5 here but 0-100 in MusicBee; writing it would misread."""
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        write_rating_to_file(track_path, rating=5)

        assert "----:com.apple.iTunes:RATING" not in MP4(track_path)

    def test_rate_atom_scale(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        for stars, expected in ((1, 20), (3, 60), (5, 100)):
            track_path = sorted(m4a_album.glob("*.m4a"))[0]
            write_rating_to_file(track_path, rating=stars)
            assert MP4(track_path)["rate"] == [str(expected)]

    def test_idempotent(self, m4a_album: Path) -> None:
        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        write_rating_to_file(track_path, rating=4)
        again = write_rating_to_file(track_path, rating=4)

        assert again == []

    def test_zero_rating_writes_nothing(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        assert write_rating_to_file(track_path, rating=0) == []

        a = MP4(track_path)
        assert "rate" not in a
        assert "----:com.apple.iTunes:STARRED" not in a

    def test_existing_favorite_is_preserved(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        audio = MP4(track_path)
        audio["----:com.apple.iTunes:STARRED"] = [b"true"]
        audio["----:com.apple.iTunes:STARRED_AT"] = [b"2020-01-01T00:00:00Z"]
        audio.save()

        write_rating_to_file(track_path, rating=3)

        a = MP4(track_path)
        assert bytes(a["----:com.apple.iTunes:STARRED"][0]).decode() == "true"
        assert bytes(a["----:com.apple.iTunes:STARRED_AT"][0]).decode() == "2020-01-01T00:00:00Z"

    def test_dry_run(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        changes = write_rating_to_file(track_path, rating=3, dry_run=True)

        assert changes
        assert "----:com.apple.iTunes:FMPS_Rating" not in MP4(track_path)


def _set_mp3_txxx(path: Path, frames: dict[str, str]) -> None:
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()  # type: ignore[no-untyped-call]
    assert audio.tags is not None
    for desc, text in frames.items():
        audio.tags.add(TXXX(encoding=3, desc=desc, text=[text]))
    audio.save()


def _rating_frames(tags: ID3) -> dict[str, list[str]]:
    """Each TXXX frame whose description is a case variant of RATING, keyed by its description."""
    return {
        frame.desc: list(frame.text)
        for frame in tags.getall("TXXX")
        if frame.desc.lower() == "rating"
    }


def _set_m4a_freeform(path: Path, atoms: dict[str, str]) -> None:
    audio = MP4(path)
    for name, text in atoms.items():
        audio[f"----:com.apple.iTunes:{name}"] = [text.encode("utf-8")]
    audio.save()


class TestWriteStats:
    """Rating and play-count writes in every container, with the clear and decrease policies."""

    @staticmethod
    def _track(album: Path, ext: str) -> Path:
        return sorted(album.glob(f"*.{ext}"))[0]

    def test_round_trips_in_every_container(
        self, flac_album: Path, mp3_album: Path, m4a_album: Path
    ) -> None:
        flac = self._track(flac_album, "flac")
        write_stats_to_file(flac, rating=4, play_count=9)
        comments = read_flac_comments(flac)
        assert comments is not None
        assert comments["RATING"] == ["4"]
        assert comments["FMPS_RATING"] == ["0.8"]
        assert comments["FMPS_PLAYCOUNT"] == ["9"]

        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, rating=4, play_count=9)
        id3 = MP3(mp3).tags
        assert id3 is not None
        assert id3["TXXX:RATING"].text == ["4"]
        assert id3["TXXX:FMPS_Rating"].text == ["0.8"]
        assert id3["TXXX:FMPS_Playcount"].text == ["9"]
        popm = id3.getall("POPM")[0]
        assert (popm.rating, popm.count) == (RATING_TO_POPM[4], 9)

        m4a = self._track(m4a_album, "m4a")
        write_stats_to_file(m4a, rating=4, play_count=9)
        atoms = MP4(m4a)
        assert bytes(atoms["----:com.apple.iTunes:FMPS_Rating"][0]).decode() == "0.8"
        assert bytes(atoms["----:com.apple.iTunes:FMPS_Playcount"][0]).decode() == "9"
        assert atoms["rate"] == ["80"]
        assert "----:com.apple.iTunes:RATING" not in atoms

    def test_explicit_zero_without_file_count_writes_zero(
        self, flac_album: Path, mp3_album: Path
    ) -> None:
        flac = self._track(flac_album, "flac")
        write_stats_to_file(flac, play_count=0)
        comments = read_flac_comments(flac)
        assert comments is not None
        assert comments["FMPS_PLAYCOUNT"] == ["0"]

        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, play_count=0)
        popm = MP3(mp3).tags.getall("POPM")[0]
        assert popm.count == 0

    def test_absent_values_give_no_instruction(self, flac_album: Path) -> None:
        flac = self._track(flac_album, "flac")

        plan = write_stats_to_file(flac)

        assert plan == StatsWrite(changes=[])

    def test_zero_rating_clears_only_with_clear(self, flac_album: Path) -> None:
        flac = self._track(flac_album, "flac")
        write_stats_to_file(flac, rating=4, play_count=2)

        withheld = write_stats_to_file(flac, rating=0)

        assert withheld.changes == []
        assert withheld.clear_withheld
        assert read_flac_comments(flac)["RATING"] == ["4"]  # type: ignore[index]

        cleared = write_stats_to_file(flac, rating=0, clear=True)

        assert {c.field for c in cleared.changes} == {"fmps_rating", "rating"}
        comments = read_flac_comments(flac)
        assert comments is not None
        assert "RATING" not in comments
        assert "FMPS_RATING" not in comments
        assert comments["FMPS_PLAYCOUNT"] == ["2"]

    def test_zero_rating_on_unrated_file_is_not_withheld(self, flac_album: Path) -> None:
        flac = self._track(flac_album, "flac")

        plan = write_stats_to_file(flac, rating=0)

        assert plan == StatsWrite(changes=[])

    def test_mp3_clear_zeroes_popm_and_keeps_count(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, rating=4, play_count=7)

        plan = write_stats_to_file(mp3, rating=0, clear=True)

        assert {c.field for c in plan.changes} == {"fmps_rating", "rating", "popm"}
        id3 = MP3(mp3).tags
        assert id3 is not None
        assert "TXXX:FMPS_Rating" not in id3
        assert "TXXX:RATING" not in id3
        assert id3["TXXX:FMPS_Playcount"].text == ["7"]
        popm = id3.getall("POPM")[0]
        assert (popm.rating, popm.count) == (0, 7)

    def test_m4a_clear_removes_fmps_and_rate(self, m4a_album: Path) -> None:
        m4a = self._track(m4a_album, "m4a")
        write_stats_to_file(m4a, rating=4, play_count=2)

        plan = write_stats_to_file(m4a, rating=0, clear=True)

        assert {c.field for c in plan.changes} == {"fmps_rating", "rate"}
        atoms = MP4(m4a)
        assert "----:com.apple.iTunes:FMPS_Rating" not in atoms
        assert "rate" not in atoms
        assert bytes(atoms["----:com.apple.iTunes:FMPS_Playcount"][0]).decode() == "2"

    def test_lower_play_count_is_withheld_without_allow_decrease(self, flac_album: Path) -> None:
        flac = self._track(flac_album, "flac")
        write_stats_to_file(flac, play_count=9)

        withheld = write_stats_to_file(flac, play_count=5)

        assert withheld.changes == []
        assert withheld.decrease_withheld
        assert read_flac_comments(flac)["FMPS_PLAYCOUNT"] == ["9"]  # type: ignore[index]

        allowed = write_stats_to_file(flac, play_count=5, allow_decrease=True)

        assert [c.field for c in allowed.changes] == ["fmps_playcount"]
        assert read_flac_comments(flac)["FMPS_PLAYCOUNT"] == ["5"]  # type: ignore[index]

    def test_rating_only_preserves_play_count(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, play_count=7)

        write_stats_to_file(mp3, rating=3)

        id3 = MP3(mp3).tags
        assert id3 is not None
        popm = id3.getall("POPM")[0]
        assert (popm.rating, popm.count) == (RATING_TO_POPM[3], 7)
        assert id3["TXXX:FMPS_Playcount"].text == ["7"]

    def test_count_only_preserves_rating(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, rating=3)

        write_stats_to_file(mp3, play_count=5)

        id3 = MP3(mp3).tags
        assert id3 is not None
        popm = id3.getall("POPM")[0]
        assert (popm.rating, popm.count) == (RATING_TO_POPM[3], 5)
        assert id3["TXXX:RATING"].text == ["3"]

    def test_unchanged_flac_is_never_opened_by_mutagen(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flac = self._track(flac_album, "flac")
        write_stats_to_file(flac, rating=4, play_count=9)

        def refuse(path: Path) -> None:
            raise AssertionError("mutagen opened an unchanged FLAC")

        monkeypatch.setattr("music_tagger.tags.MutagenFile", refuse)
        plan = write_stats_to_file(flac, rating=4, play_count=9)

        assert plan.changes == []

    def test_second_run_is_idempotent(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, rating=4, play_count=9)

        assert write_stats_to_file(mp3, rating=4, play_count=9).changes == []

    def test_dry_run_leaves_bytes_unchanged(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        write_stats_to_file(mp3, rating=4, play_count=9)
        before = mp3.read_bytes()

        plan = write_stats_to_file(mp3, rating=2, play_count=12, dry_run=True)

        assert plan.changes
        assert mp3.read_bytes() == before

    def test_stale_uppercase_alias_is_replaced_by_canonical(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        _set_mp3_txxx(mp3, {"FMPS_RATING": "0.4"})

        plan = write_stats_to_file(mp3, rating=4)

        assert {c.field for c in plan.changes} == {"fmps_rating", "rating", "popm"}
        id3 = MP3(mp3).tags
        assert id3 is not None
        assert "TXXX:FMPS_RATING" not in id3
        assert id3["TXXX:FMPS_Rating"].text == ["0.8"]
        assert write_stats_to_file(mp3, rating=4).changes == []

    def test_rating_case_variant_is_replaced_by_canonical(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        _set_mp3_txxx(mp3, {"Rating": "3"})

        write_stats_to_file(mp3, rating=4)

        id3 = MP3(mp3).tags
        assert id3 is not None
        assert _rating_frames(id3) == {"RATING": ["4"]}
        assert write_stats_to_file(mp3, rating=4).changes == []

    def test_clear_removes_every_rating_case_variant(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        _set_mp3_txxx(mp3, {"Rating": "3"})

        plan = write_stats_to_file(mp3, rating=0, clear=True)

        assert [c.field for c in plan.changes] == ["rating"]
        id3 = MP3(mp3).tags
        assert id3 is not None
        assert _rating_frames(id3) == {}
        assert write_stats_to_file(mp3, rating=0, clear=True).changes == []

    def test_conflicting_rating_case_variants_refuse_the_write(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        _set_mp3_txxx(mp3, {"Rating": "3", "RATING": "4"})
        before = mp3.read_bytes()

        with pytest.raises(MalformedStatsError, match="RATING stored values disagree"):
            write_stats_to_file(mp3, rating=5)

        assert mp3.read_bytes() == before

    def test_conflicting_play_count_aliases_refuse_the_write(self, mp3_album: Path) -> None:
        mp3 = self._track(mp3_album, "mp3")
        _set_mp3_txxx(mp3, {"FMPS_Playcount": "5", "FMPS_PLAYCOUNT": "6"})
        before = mp3.read_bytes()

        with pytest.raises(MalformedStatsError, match="play count stored values disagree"):
            write_stats_to_file(mp3, play_count=7)

        assert mp3.read_bytes() == before

    @pytest.mark.parametrize("stored", ["many", "1e3", "3.5.1"])
    def test_non_count_play_count_refuses_the_write(self, flac_album: Path, stored: str) -> None:
        flac = self._track(flac_album, "flac")
        audio = FLAC(flac)
        audio["FMPS_PLAYCOUNT"] = stored
        audio.save()
        before = flac.read_bytes()

        with pytest.raises(MalformedStatsError, match="is not a count"):
            write_stats_to_file(flac, play_count=3)

        assert flac.read_bytes() == before

    def test_fractional_count_with_same_value_is_not_rewritten(self, flac_album: Path) -> None:
        flac = self._track(flac_album, "flac")
        audio = FLAC(flac)
        audio["FMPS_PLAYCOUNT"] = "3.0"
        audio.save()

        assert write_stats_to_file(flac, play_count=3) == StatsWrite(changes=[])

    def test_fractional_count_above_snapshot_is_withheld(self, flac_album: Path) -> None:
        flac = self._track(flac_album, "flac")
        audio = FLAC(flac)
        audio["FMPS_PLAYCOUNT"] = "3.5"
        audio.save()

        plan = write_stats_to_file(flac, play_count=3)

        assert plan == StatsWrite(changes=[], decrease_withheld=True)
        comments = read_flac_comments(flac)
        assert comments is not None
        assert comments["FMPS_PLAYCOUNT"] == ["3.5"]


class TestFlacMultiValueStats:
    """A FLAC stats field with several stored values is never silently collapsed."""

    @staticmethod
    def _set_comments(path: Path, values: dict[str, list[str]]) -> None:
        audio = FLAC(path)
        for key, items in values.items():
            audio[key] = items
        audio.save()

    def test_conflicting_play_counts_refuse_the_write(self, flac_album: Path) -> None:
        flac = sorted(flac_album.glob("*.flac"))[0]
        self._set_comments(flac, {"FMPS_PLAYCOUNT": ["5", "9"]})
        before = flac.read_bytes()

        with pytest.raises(MalformedStatsError, match="play count stored values disagree"):
            write_stats_to_file(flac, play_count=7)

        assert flac.read_bytes() == before

    def test_identical_duplicate_play_counts_count_as_one(self, flac_album: Path) -> None:
        flac = sorted(flac_album.glob("*.flac"))[0]
        self._set_comments(flac, {"FMPS_PLAYCOUNT": ["5", "5"]})

        plan = write_stats_to_file(flac, play_count=7)

        assert [c.field for c in plan.changes] == ["fmps_playcount"]
        comments = read_flac_comments(flac)
        assert comments is not None
        assert comments["FMPS_PLAYCOUNT"] == ["7"]

    def test_conflicting_rating_blocks_a_rating_write(self, flac_album: Path) -> None:
        flac = sorted(flac_album.glob("*.flac"))[0]
        self._set_comments(flac, {"RATING": ["3", "4"]})
        before = flac.read_bytes()

        with pytest.raises(MalformedStatsError, match="RATING stored values disagree"):
            write_stats_to_file(flac, rating=5)

        assert flac.read_bytes() == before


class TestUnsupportedRatingTarget:
    def test_unrecognized_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.flac"
        path.write_text("not audio")

        with pytest.raises(UnsupportedFileError, match="mutagen"):
            write_rating_to_file(path, rating=4)

    def test_dry_run_raises_too(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.flac"
        path.write_text("not audio")

        with pytest.raises(UnsupportedFileError, match="mutagen"):
            write_rating_to_file(path, rating=4, dry_run=True)


class TestMp3PopmFrame:
    """The writer owns only the empty-owner POPM frame (plan A4)."""

    @staticmethod
    def _track(album: Path) -> Path:
        return sorted(album.glob("*.mp3"))[0]

    @staticmethod
    def _add_popm(path: Path, email: str, rating: int, count: int) -> None:
        audio = MP3(path)
        assert audio.tags is not None
        audio.tags.add(POPM(email=email, rating=rating, count=count))
        audio.save()

    @staticmethod
    def _other_frames(path: Path) -> dict[str, str]:
        audio = MP3(path)
        assert audio.tags is not None
        return {
            key: str(frame)
            for key, frame in audio.tags.items()
            if not key.startswith(("TXXX:", "POPM:"))
        }

    def test_rating_update_keeps_play_count_and_other_owners(self, mp3_album: Path) -> None:
        path = self._track(mp3_album)
        self._add_popm(path, "", rating=1, count=42)
        self._add_popm(path, "other@example.com", rating=200, count=9)

        write_rating_to_file(path, rating=4)

        frames = {frame.email: frame for frame in MP3(path).tags.getall("POPM")}
        assert (frames[""].rating, frames[""].count) == (RATING_TO_POPM[4], 42)
        assert (frames["other@example.com"].rating, frames["other@example.com"].count) == (200, 9)

    def test_missing_popm_is_repaired_when_tags_match(self, mp3_album: Path) -> None:
        path = self._track(mp3_album)
        write_rating_to_file(path, rating=4)
        audio = MP3(path)
        assert audio.tags is not None
        audio.tags.delall("POPM")
        audio.save()

        changes = write_rating_to_file(path, rating=4)

        assert [c.field for c in changes] == ["popm"]
        assert MP3(path).tags.getall("POPM")[0].rating == RATING_TO_POPM[4]

    def test_stale_popm_is_repaired_and_keeps_its_count(self, mp3_album: Path) -> None:
        path = self._track(mp3_album)
        write_rating_to_file(path, rating=4)
        self._add_popm(path, "", rating=1, count=7)

        changes = write_rating_to_file(path, rating=4)

        assert [c.field for c in changes] == ["popm"]
        frame = MP3(path).tags.getall("POPM")[0]
        assert (frame.rating, frame.count) == (RATING_TO_POPM[4], 7)

    def test_dry_run_reports_missing_popm_without_writing(self, mp3_album: Path) -> None:
        path = self._track(mp3_album)
        write_rating_to_file(path, rating=4)
        audio = MP3(path)
        assert audio.tags is not None
        audio.tags.delall("POPM")
        audio.save()

        changes = write_rating_to_file(path, rating=4, dry_run=True)

        assert [c.field for c in changes] == ["popm"]
        assert MP3(path).tags.getall("POPM") == []

    def test_second_run_makes_no_changes(self, mp3_album: Path) -> None:
        path = self._track(mp3_album)
        write_rating_to_file(path, rating=4)

        assert write_rating_to_file(path, rating=4) == []

    def test_unrelated_frames_are_preserved(self, mp3_album: Path) -> None:
        path = self._track(mp3_album)
        before = self._other_frames(path)

        write_rating_to_file(path, rating=4)

        assert self._other_frames(path) == before


class TestReadFmps:
    """Accepted FMPS spellings, aliases, and conflicts (plan A3)."""

    def test_mp3_canonical_spelling(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"FMPS_Rating": "0.8", "FMPS_Playcount": "12"})

        stats = read_fmps(MP3(path))

        assert stats.rating == FmpsField(value="0.8")
        assert stats.play_count == FmpsField(value="12")

    def test_mp3_uppercase_spelling(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"FMPS_RATING": "0.4", "FMPS_PLAYCOUNT": "3"})

        stats = read_fmps(MP3(path))

        assert stats.rating == FmpsField(value="0.4")
        assert stats.play_count == FmpsField(value="3")

    def test_mp3_clementine_playcount_spelling(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"FMPS_PlayCount": "7"})

        assert read_fmps(MP3(path)).play_count == FmpsField(value="7")

    def test_mp3_conflicting_aliases_are_reported(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"FMPS_Rating": "0.8", "FMPS_RATING": "0.4"})

        rating = read_fmps(MP3(path)).rating

        assert rating == FmpsField(conflict=True)

    def test_mp3_agreeing_aliases_are_one_value(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"FMPS_Rating": "0.8", "FMPS_RATING": "0.8"})

        assert read_fmps(MP3(path)).rating == FmpsField(value="0.8")

    def test_mp3_unlisted_casing_is_not_read(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"fmps_rating": "0.9"})

        assert read_fmps(MP3(path)) == FmpsStats(rating=FmpsField(), play_count=FmpsField())

    def test_mp3_empty_value_is_absent(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]
        _set_mp3_txxx(path, {"FMPS_Rating": "", "FMPS_RATING": "0.6"})

        assert read_fmps(MP3(path)).rating == FmpsField(value="0.6")

    def test_m4a_canonical_spelling(self, m4a_album: Path) -> None:
        path = sorted(m4a_album.glob("*.m4a"))[0]
        _set_m4a_freeform(path, {"FMPS_Rating": "0.8", "FMPS_Playcount": "12"})

        stats = read_fmps(MP4(path))

        assert stats.rating == FmpsField(value="0.8")
        assert stats.play_count == FmpsField(value="12")

    def test_m4a_uppercase_spelling(self, m4a_album: Path) -> None:
        path = sorted(m4a_album.glob("*.m4a"))[0]
        _set_m4a_freeform(path, {"FMPS_RATING": "0.4", "FMPS_PLAYCOUNT": "3"})

        stats = read_fmps(MP4(path))

        assert stats.rating == FmpsField(value="0.4")
        assert stats.play_count == FmpsField(value="3")

    def test_m4a_conflicting_aliases_are_reported(self, m4a_album: Path) -> None:
        path = sorted(m4a_album.glob("*.m4a"))[0]
        _set_m4a_freeform(path, {"FMPS_Playcount": "5", "FMPS_PLAYCOUNT": "6"})

        assert read_fmps(MP4(path)).play_count == FmpsField(conflict=True)

    def test_m4a_absent_fields_are_empty(self, m4a_album: Path) -> None:
        path = sorted(m4a_album.glob("*.m4a"))[0]

        assert read_fmps(MP4(path)) == FmpsStats(rating=FmpsField(), play_count=FmpsField())

    def test_unsupported_container_is_refused(self, flac_album: Path) -> None:
        path = sorted(flac_album.glob("*.flac"))[0]

        with pytest.raises(TypeError, match="MP3 and MP4 only"):
            read_fmps(FLAC(path))  # type: ignore[arg-type]

    def test_mp3_rating_is_written_in_canonical_spelling(self, mp3_album: Path) -> None:
        path = sorted(mp3_album.glob("*.mp3"))[0]

        write_rating_to_file(path, rating=4)

        frames = MP3(path).tags
        assert frames is not None
        assert frames["TXXX:FMPS_Rating"].text == ["0.8"]
        assert "TXXX:FMPS_RATING" not in frames


class TestTrackTagsLockedFields:
    def test_round_trip(self) -> None:
        track = TrackTags(
            Path("/music/01.flac"), {}, format="flac", locked_fields=["isrc", "title"]
        )

        restored = TrackTags.from_dict(track.to_dict())

        assert restored.locked_fields == ["isrc", "title"]

    def test_missing_key_means_no_locks(self) -> None:
        restored = TrackTags.from_dict({"path": "/music/01.flac"})

        assert restored.locked_fields == []


class TestLocksFlac:
    def test_writes_separate_tag_values(self, flac_album: Path) -> None:
        from mutagen.flac import FLAC

        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_locks(track_path, ["title", "isrc"])

        assert FLAC(track_path)["MUSIC_TAGGER_LOCKED"] == ["isrc", "title"]
        assert read_locks(track_path) == ["isrc", "title"]

    def test_removes_duplicates(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_locks(track_path, ["title", "title"])

        assert read_locks(track_path) == ["title"]

    def test_empty_list_clears(self, flac_album: Path) -> None:
        from mutagen.flac import FLAC

        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_locks(track_path, ["title"])
        write_locks(track_path, [])

        assert "MUSIC_TAGGER_LOCKED" not in FLAC(track_path)
        assert read_locks(track_path) == []

    def test_stamps_timestamp(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_locks(track_path, ["title"], timestamp="2026-10-14T00:00:00Z")

        assert read_album(flac_album).tracks[0].tags["music_tagger_updated"] == (
            "2026-10-14T00:00:00Z"
        )

    def test_read_album_exposes_locks_outside_tags(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_locks(track_path, ["title", "isrc"])

        album = read_album(flac_album)

        assert album.tracks[0].locked_fields == ["isrc", "title"]
        assert album.tracks[1].locked_fields == []
        assert "music_tagger_locked" not in album.tracks[0].tags

    def test_rejects_unknown_field_without_writing(self, flac_album: Path) -> None:
        import pytest

        track_path = sorted(flac_album.glob("*.flac"))[0]
        before = track_path.read_bytes()

        with pytest.raises(ValueError, match="not-a-field"):
            write_locks(track_path, ["title", "not-a-field"])

        assert track_path.read_bytes() == before

    def test_rejects_timestamp_field(self, flac_album: Path) -> None:
        import pytest

        track_path = sorted(flac_album.glob("*.flac"))[0]

        with pytest.raises(ValueError, match="music_tagger_updated"):
            write_locks(track_path, ["music_tagger_updated"])


class TestLocksMp3:
    def test_writes_single_multivalue_frame(self, mp3_album: Path) -> None:
        from mutagen.mp3 import MP3

        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        write_locks(track_path, ["title", "isrc"])

        frames = [
            frame
            for frame in MP3(track_path).tags.getall("TXXX")
            if frame.desc == "MUSIC_TAGGER_LOCKED"
        ]
        assert len(frames) == 1
        assert frames[0].text == ["isrc", "title"]
        assert read_locks(track_path) == ["isrc", "title"]

    def test_empty_list_clears(self, mp3_album: Path) -> None:
        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        write_locks(track_path, ["title"])
        write_locks(track_path, [])

        assert read_locks(track_path) == []

    def test_read_album_exposes_locks(self, mp3_album: Path) -> None:
        track_path = sorted(mp3_album.glob("*.mp3"))[0]
        write_locks(track_path, ["title"])

        assert read_album(mp3_album).tracks[0].locked_fields == ["title"]


class TestLocksM4a:
    def test_write_raises_rather_than_silently_skipping(self, m4a_album: Path) -> None:
        import pytest

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        before = track_path.read_bytes()

        with pytest.raises(NotImplementedError, match="not supported"):
            write_locks(track_path, ["title"])

        assert track_path.read_bytes() == before

    def test_reads_freeform_atom(self, m4a_album: Path) -> None:
        from mutagen.mp4 import MP4

        track_path = sorted(m4a_album.glob("*.m4a"))[0]
        audio = MP4(track_path)
        audio["----:com.apple.iTunes:MUSIC_TAGGER_LOCKED"] = [b"isrc", b"title"]
        audio.save()

        assert read_locks(track_path) == ["isrc", "title"]
        assert read_album(m4a_album).tracks[0].locked_fields == ["isrc", "title"]


class _CountingStream(io.BytesIO):
    """BytesIO that counts the bytes handed to readers."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def _block(block_type: int, body: bytes, *, last: bool) -> bytes:
    header = (0x80 if last else 0) | block_type
    return bytes([header]) + len(body).to_bytes(3, "big") + body


def _vorbis_body(*comments: str) -> bytes:
    vendor = b"test"
    body = len(vendor).to_bytes(4, "little") + vendor + len(comments).to_bytes(4, "little")
    for comment in comments:
        raw = comment.encode()
        body += len(raw).to_bytes(4, "little") + raw
    return body


class TestReadFlacComments:
    def test_matches_mutagen_on_fixtures(self, flac_album: Path) -> None:
        for path in sorted(flac_album.glob("*.flac")):
            audio = FLAC(path)
            expected: dict[str, list[str]] = {}
            for key, value in audio.tags:
                expected.setdefault(key.upper(), []).append(value)
            assert read_flac_comments(path) == expected

    def test_skips_picture_block_contents(self, flac_album: Path) -> None:
        path = sorted(flac_album.glob("*.flac"))[0]
        audio = FLAC(path)
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.data = b"\x00" * 2_000_000
        audio.add_picture(picture)
        audio.save()

        data = path.read_bytes()
        stream = _CountingStream(data)
        comments = _flac_comments_from_stream(stream, len(data))

        assert comments == read_flac_comments(path)
        assert stream.bytes_read < 100_000

    def test_non_flac_returns_none(self, mp3_album: Path) -> None:
        assert read_flac_comments(sorted(mp3_album.glob("*.mp3"))[0]) is None

    def test_truncated_file_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "truncated.flac"
        path.write_bytes(b"fLaC\x04\x00\x00\x40")  # VORBIS_COMMENT claims 64 bytes; none follow

        assert read_flac_comments(path) is None

    def test_truncated_comment_body_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "short-body.flac"
        path.write_bytes(b"fLaC" + bytes([0x04]) + (64).to_bytes(3, "big") + b"abc")

        assert read_flac_comments(path) is None

    def test_truncated_last_block_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "short-padding.flac"
        path.write_bytes((b"fLaC" + _block(1, b"\x00" * 100, last=True))[:-50])

        assert read_flac_comments(path) is None

    def test_two_vorbis_blocks_return_none(self) -> None:
        data = (
            b"fLaC"
            + _block(4, _vorbis_body("TITLE=a"), last=False)
            + _block(4, _vorbis_body("TITLE=b"), last=True)
        )

        assert _flac_comments_from_stream(_CountingStream(data), len(data)) is None

    def test_no_vorbis_block_returns_empty(self) -> None:
        data = b"fLaC" + _block(1, b"\x00" * 8, last=True)

        assert _flac_comments_from_stream(_CountingStream(data), len(data)) == {}


class TestWriteRatingFastPath:
    @staticmethod
    def _refuse_mutagen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("mutagen opened the file")

    def test_unchanged_file_is_not_opened_by_mutagen(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        write_rating_to_file(track_path, rating=4)
        monkeypatch.setattr("music_tagger.tags.MutagenFile", self._refuse_mutagen)

        assert write_rating_to_file(track_path, rating=4) == []

    def test_dry_run_is_not_opened_by_mutagen(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        monkeypatch.setattr("music_tagger.tags.MutagenFile", self._refuse_mutagen)

        changes = write_rating_to_file(track_path, rating=3, dry_run=True)

        assert {c.field for c in changes} == {"fmps_rating", "rating"}

    def test_fallback_gives_same_changes(
        self, flac_album: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        fast = write_rating_to_file(track_path, rating=4, dry_run=True)
        monkeypatch.setattr("music_tagger.tags.read_flac_comments", lambda path: None)
        slow = write_rating_to_file(track_path, rating=4, dry_run=True)

        assert len(fast) == 2
        assert fast == slow

    def test_fast_path_write_reads_back_through_header_reader(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        assert read_flac_comments(track_path) is not None

        write_rating_to_file(track_path, rating=4)

        comments = read_flac_comments(track_path)
        assert comments is not None
        assert comments["RATING"] == ["4"]
        assert comments["FMPS_RATING"] == ["0.8"]
        assert "STARRED" not in comments

    def test_id3_prefixed_flac_falls_back_to_mutagen(self, flac_album: Path) -> None:
        track_path = sorted(flac_album.glob("*.flac"))[0]
        track_path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + track_path.read_bytes())
        assert read_flac_comments(track_path) is None

        changes = write_rating_to_file(track_path, rating=4)

        assert {c.field for c in changes} == {"fmps_rating", "rating"}
        assert track_path.read_bytes().startswith(b"ID3")
        assert FLAC(track_path)["rating"] == ["4"]
