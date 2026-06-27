from __future__ import annotations

from pathlib import Path

from music_tagger.tags import TagChange, compute_diff, read_album, write_tags


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
        changes = [TagChange(field="musicbrainz_albumid", old_value="test-album-id-1", new_value=new_id)]
        write_tags(track, changes)

        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["musicbrainz_albumid"] == new_id

    def test_write_no_changes(self, flac_album: Path) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        write_tags(track, [])
        album2 = read_album(flac_album)
        assert album2.tracks[0].tags["title"] == "Desperado"


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
        changes = [TagChange(field="musicbrainz_albumid", old_value="test-album-id-1", new_value=new_id)]
        write_tags(track, changes)

        album2 = read_album(mp3_album)
        assert album2.tracks[0].tags["musicbrainz_albumid"] == new_id
