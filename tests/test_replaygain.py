from __future__ import annotations

from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm

from music_tagger.replaygain import read_album_gain


class TestReadAlbumGain:
    """Tag names match what rsgain 3.x writes."""

    def test_flac(self, flac_album: Path) -> None:
        audio = FLAC(sorted(flac_album.glob("*.flac"))[0])
        audio["REPLAYGAIN_ALBUM_GAIN"] = ["3.61 dB"]
        audio.save()

        assert read_album_gain(flac_album) == "3.61 dB"

    def test_mp3(self, mp3_album: Path) -> None:
        audio = MP3(sorted(mp3_album.glob("*.mp3"))[0])
        audio.tags.add(TXXX(desc="REPLAYGAIN_ALBUM_GAIN", text=["4.05 dB"]))
        audio.save()

        assert read_album_gain(mp3_album) == "4.05 dB"

    def test_m4a_uppercase_atom(self, m4a_album: Path) -> None:
        audio = MP4(sorted(m4a_album.glob("*.m4a"))[0])
        audio["----:com.apple.iTunes:REPLAYGAIN_ALBUM_GAIN"] = [MP4FreeForm(b"-1.20 dB")]
        audio.save()

        assert read_album_gain(m4a_album) == "-1.20 dB"

    def test_missing(self, flac_album: Path) -> None:
        assert read_album_gain(flac_album) == ""
