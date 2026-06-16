import threading
import time
import unittest


class WebSearchResultTests(unittest.TestCase):
    def test_flattens_search_payload_into_clickable_results(self):
        from zotify.web import flatten_search_results

        payload = {
            "tracks": [
                {
                    "name": "A Song",
                    "uri": "spotify:track:abc",
                    "artists": [{"name": "The Artist"}],
                    "explicit": True,
                    "album": {"images": [{"url": "https://example.test/cover.jpg"}]},
                }
            ],
            "albums": [
                {
                    "name": "A Record",
                    "uri": "spotify:album:def",
                    "artists": [{"name": "The Artist"}],
                    "images": [{"url": "https://example.test/album.jpg"}],
                }
            ],
            "artists": [
                {
                    "name": "The Artist",
                    "uri": "spotify:artist:ghi",
                    "followers": {"total": 42},
                    "images": [{"url": "https://example.test/artist.jpg"}],
                }
            ],
            "playlists": [
                {
                    "name": "My Playlist",
                    "uri": "spotify:playlist:jkl",
                    "owner": {"display_name": "Me"},
                    "images": [{"url": "https://example.test/playlist.jpg"}],
                }
            ],
        }

        results = flatten_search_results(payload)

        self.assertEqual(
            [
                {
                    "type": "track",
                    "name": "A Song",
                    "subtitle": "The Artist",
                    "uri": "spotify:track:abc",
                    "image_url": "https://example.test/cover.jpg",
                    "metadata": {"explicit": True},
                },
                {
                    "type": "album",
                    "name": "A Record",
                    "subtitle": "The Artist",
                    "uri": "spotify:album:def",
                    "image_url": "https://example.test/album.jpg",
                    "metadata": {},
                },
                {
                    "type": "artist",
                    "name": "The Artist",
                    "subtitle": "42 followers",
                    "uri": "spotify:artist:ghi",
                    "image_url": "https://example.test/artist.jpg",
                    "metadata": {},
                },
                {
                    "type": "playlist",
                    "name": "My Playlist",
                    "subtitle": "Me",
                    "uri": "spotify:playlist:jkl",
                    "image_url": "https://example.test/playlist.jpg",
                    "metadata": {},
                },
            ],
            results,
        )


class WebLibraryItemTests(unittest.TestCase):
    def test_flattens_library_payload_with_inner_items(self):
        from zotify.web import flatten_library_items

        items = [
            {"track": {"name": "Saved Song", "uri": "spotify:track:abc", "artists": [{"name": "Artist"}]}},
            {"album": {"name": "Saved Album", "uri": "spotify:album:def", "artists": [{"name": "Artist"}]}},
            {"name": "Playlist", "uri": "spotify:playlist:ghi", "owner": {"display_name": "Me"}},
        ]

        self.assertEqual(
            [
                {"type": "track", "name": "Saved Song", "subtitle": "Artist", "uri": "spotify:track:abc", "image_url": None, "metadata": {}},
                {"type": "album", "name": "Saved Album", "subtitle": "Artist", "uri": "spotify:album:def", "image_url": None, "metadata": {}},
                {"type": "playlist", "name": "Playlist", "subtitle": "Me", "uri": "spotify:playlist:ghi", "image_url": None, "metadata": {}},
            ],
            flatten_library_items(items),
        )


class WebJobManagerTests(unittest.TestCase):
    def test_rejects_second_active_job_and_cancels_cooperatively(self):
        from zotify.web import JobCancelled, JobManager, JobRejected

        manager = JobManager()
        job_started = threading.Event()

        def job(ctx):
            ctx.log("started")
            job_started.set()
            while not ctx.cancel_requested:
                time.sleep(0.01)
            ctx.raise_if_cancelled()

        first_job = manager.start("slow", {"kind": "test"}, job)
        self.assertTrue(job_started.wait(1))
        self.assertEqual("running", first_job.status)

        with self.assertRaises(JobRejected):
            manager.start("second", {}, lambda ctx: None)

        manager.cancel_current()
        self.assertEqual("cancel_requested", first_job.status)
        first_job.thread.join(1)

        self.assertEqual("cancelled", first_job.status)
        self.assertIsInstance(first_job.error, JobCancelled)
        self.assertIn("started", [event["message"] for event in first_job.drain_events()])


class WebSettingsTests(unittest.TestCase):
    def test_settings_update_until_locked(self):
        from zotify.web import SettingsLocked, WebSettings

        settings = WebSettings()
        settings.update({"root_path": "/tmp/music", "download_format": "mp3"})

        self.assertEqual("/tmp/music", settings.values["root_path"])
        self.assertEqual("mp3", settings.values["download_format"])

        settings.lock()

        with self.assertRaises(SettingsLocked):
            settings.update({"root_path": "/tmp/other"})


class WebCliParserTests(unittest.TestCase):
    def test_web_flag_has_local_defaults_without_invoking_client(self):
        from zotify.__main__ import build_parser

        parser = build_parser()
        args = parser.parse_args(["--web"])

        self.assertTrue(args.web)
        self.assertEqual("127.0.0.1", args.web_host)
        self.assertEqual(4382, args.web_port)


if __name__ == "__main__":
    unittest.main()
