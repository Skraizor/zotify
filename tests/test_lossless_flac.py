import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import music_tag
from librespot.audio.decoders import AudioQuality, FormatOnlyAudioQuality, SuperAudioFormat

from zotify.config import CONFIG_VALUES, Zotify
from zotify.const import PREMIUM, TRACK_FILE_EXTENSIONS, TYPE


def config_default_value(cfg_setup):
    raw_value = cfg_setup["default"]
    value_type = cfg_setup["type"]
    if value_type is bool:
        return str(raw_value).lower() not in {"0", "no", "false"}
    return value_type(raw_value)


for config_key, config_setup in CONFIG_VALUES.items():
    Zotify.CONFIG.Values.setdefault(config_key, config_default_value(config_setup))

from zotify.api import Track


class DummySession:
    def __init__(self, subscription_type):
        self.subscription_type = subscription_type

    def get_user_attribute(self, attr):
        if attr == TYPE:
            return self.subscription_type
        return None


class LosslessQualityTests(unittest.TestCase):
    def setUp(self):
        self.original_session = Zotify.SESSION
        self.original_quality = Zotify.DOWNLOAD_QUALITY
        self.original_track_codec = Track._codec
        self.original_track_ext = Track._ext

    def tearDown(self):
        Zotify.SESSION = self.original_session
        Zotify.DOWNLOAD_QUALITY = self.original_quality
        Track._codec = self.original_track_codec
        Track._ext = self.original_track_ext

    def test_premium_lossless_requests_flac_source(self):
        Zotify.SESSION = DummySession(PREMIUM)

        premium, quality, bitrate = Zotify.parse_dl_quality("lossless")

        self.assertTrue(premium)
        self.assertEqual(AudioQuality.LOSSLESS, quality.preferred)
        self.assertEqual(SuperAudioFormat.FLAC, quality.format_filter)
        self.assertIsNone(bitrate)

    def test_free_lossless_downgrades_to_high_vorbis(self):
        Zotify.SESSION = DummySession("free")

        premium, quality, bitrate = Zotify.parse_dl_quality("lossless")

        self.assertFalse(premium)
        self.assertEqual(AudioQuality.HIGH, quality.preferred)
        self.assertEqual(SuperAudioFormat.VORBIS, quality.format_filter)
        self.assertEqual("160k", bitrate)

    def test_lossless_copy_uses_flac_expected_extension(self):
        Track._codec = "copy"
        Track._ext = "ogg"
        Zotify.DOWNLOAD_QUALITY = FormatOnlyAudioQuality(AudioQuality.LOSSLESS, SuperAudioFormat.FLAC)

        self.assertEqual("flac", Track.expected_output_extension())

    def test_non_lossless_copy_uses_ogg_expected_extension(self):
        Track._codec = "copy"
        Track._ext = "ogg"
        Zotify.DOWNLOAD_QUALITY = FormatOnlyAudioQuality(AudioQuality.VERY_HIGH, SuperAudioFormat.VORBIS)

        self.assertEqual("ogg", Track.expected_output_extension())

    def test_copy_source_codecs_map_to_file_extensions(self):
        self.assertEqual("flac", Track.source_copy_extension_for_codec("flac"))
        self.assertEqual("ogg", Track.source_copy_extension_for_codec("vorbis"))

    def test_flac_is_in_track_scan_extensions(self):
        self.assertIn("flac", TRACK_FILE_EXTENSIONS)


@unittest.skipIf(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                 "ffmpeg and ffprobe are required for FLAC smoke test")
class FlacSmokeTests(unittest.TestCase):
    def test_ffmpeg_flac_and_music_tag_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            flac_path = Path(tmpdir) / "fixture.flac"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "0.1", "-c:a", "flac", str(flac_path)
            ], check=True)
            probe = subprocess.run([
                "ffprobe", "-hide_banner", "-loglevel", "error",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(flac_path)
            ], check=True, text=True, capture_output=True)

            self.assertEqual("flac", probe.stdout.strip())

            tags = music_tag.load_file(flac_path)
            tags["artist"] = "Artist"
            tags["tracktitle"] = "Title"
            tags.save()

            written_tags = music_tag.load_file(flac_path)
            self.assertEqual(["Artist"], written_tags["artist"].values)
            self.assertEqual("Title", written_tags["tracktitle"].val)
