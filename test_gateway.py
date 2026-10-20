import os
import gzip
import json
import threading
import unittest
from unittest.mock import Mock, patch
import freeview
from datetime import datetime
import xml.etree.ElementTree as ET

os.environ.setdefault("PUBLIC_BASE_URL", "http://gateway.test:8787")

import gateway


CHANNELS = {
    "binge": [
        {
            "AssetId": "binge-1",
            "Title": "LifeStyle Channel",
            "LinearSchedule": {
                "Now": {"Title": "Now", "Start": "2026-10-20T00:00:00Z", "End": "2026-10-20T01:00:00Z"},
                "Next": {"Title": "Next", "Start": "2026-10-20T01:00:00Z", "End": "2026-10-20T02:00:00Z"},
                "Later": [
                    {"Title": "Later", "Start": "2026-10-20T02:00:00Z", "End": "2026-10-20T03:00:00Z"}
                ],
            },
        }
    ],
    "kayo": [{"AssetId": "kayo-1", "Title": "FOX SPORTS 503", "LinearSchedule": {}}],
}


class GatewayTests(unittest.TestCase):
    def test_freeview_xmltv_keeps_programme_description(self):
        xml = b'<tv><programme channel="abc" start="20261020040000 +0000" stop="20261020050000 +0000"><title>Example</title><desc>Full programme synopsis.</desc><sub-title>Episode title</sub-title></programme></tv>'
        client = freeview.FreeviewClient()
        with patch("freeview.fetch", return_value=(gzip.compress(xml), "")):
            guide = client.full_guide()
        self.assertEqual(guide["abc"][0]["Description"], "Full programme synopsis.")
        self.assertEqual(guide["abc"][0]["EpisodeTitle"], "Episode title")

    def test_abc_video_ignores_epg_and_logo_follows_active_feed(self):
        client = freeview.FreeviewClient()
        now = datetime.fromisoformat("2026-10-20T19:35:00+11:00")
        programme = {"Start": "2026-10-20T19:30:00+11:00", "End": "2026-10-20T20:00:00+11:00",
                     "Title": "Bluey"}
        client.abc_schedules = {"kids": [programme], "family": [dict(programme, Title="")]}
        self.assertEqual(client.abc_active_service(now), "family")
        client.abc_schedules["family"][0]["Title"] = "ABC Kids programs resume at 4am"
        self.assertEqual(client.abc_active_service(now), "family")
        client.abc_schedules["family"][0]["Title"] = "Family programme"
        self.assertEqual(client.abc_active_service(now), "family")
        client.abc_logos = {"kids": "https://example.test/kids.png", "family": "https://example.test/family.png"}
        with patch.object(client, "abc_active_service", return_value="family"), patch("freeview.fetch", return_value=(bytes.fromhex("89504e47"), "")) as request:
            body, mime = client.abc_logo()
        request.assert_called_once_with("https://example.test/family.png")
        self.assertEqual(mime, "image/png")

    def test_abc_shared_channel_and_melbourne_boundaries(self):
        client = freeview.FreeviewClient()
        data = {
            "mjh-abc-kids": {"name": "ABC Kids", "chno": 22, "mjh_master": "https://example.test/kids.m3u8",
                            "programs": [[1792485000, "Evening show in Kids feed"]]},
            "mjh-abc-tv-plus": {"name": "ABC Family", "chno": 22, "mjh_master": "https://example.test/family.m3u8",
                               "programs": [[1792440000, "Morning show in Family feed"]]},
        }
        with patch("freeview.fetch", return_value=(gzip.compress(json.dumps(data).encode()), "")):
            channels = client.channels()
        self.assertEqual([(tile["Title"], tile["Number"]) for tile in channels], [("ABC Kids/Family", 22)])
        self.assertEqual({row["Title"] for row in channels[0]["LinearSchedule"]},
                         {"Evening show in Kids feed", "Morning show in Family feed"})
        for timestamp, expected in [
            ("2026-10-20T03:59:00+11:00", "family"),
            ("2026-10-20T04:00:00+11:00", "kids"),
            ("2026-10-20T19:29:00+11:00", "kids"),
            ("2026-10-20T19:30:00+11:00", "family"),
            ("2026-07-20T19:30:00+10:00", "family"),
        ]:
            self.assertEqual(client.abc_service(datetime.fromisoformat(timestamp)), expected)

    def test_freeview_excludes_fast_and_radio_and_inherits_schedule(self):
        data = {
            "abc": {"name": "ABC TV", "chno": 21, "mjh_master": "https://example.test/live.m3u8",
                    "epg_id": "schedule"},
            "schedule": {"programs": [[1792428960, "News"], [1792430460, "Next"]]},
            "fast": {"name": "FAST", "mjh_master": "https://example.test/fast.m3u8"},
            "radio": {"name": "Radio", "chno": 301, "mjh_master": "https://example.test/radio.m3u8"},
        }
        with patch("freeview.fetch", return_value=(gzip.compress(json.dumps(data).encode()), "")):
            channels = freeview.FreeviewClient().channels()
        self.assertEqual([tile["Title"] for tile in channels], ["ABC TV"])
        self.assertEqual(channels[0]["LinearSchedule"][0]["Title"], "News")

    def test_freeview_rewrites_hls_uris_and_preserves_request_headers(self):
        client = freeview.FreeviewClient()
        headers = {"User-Agent": "required-agent"}
        route = client.register("https://example.test/start.m3u8", headers)
        manifest = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nsegment.ts\n'
        with patch("freeview.fetch", return_value=(manifest, "https://cdn.test/live/index.m3u8")) as request:
            body, mime = client.proxy(route.rsplit("/", 1)[-1])
        request.assert_called_once_with("https://example.test/start.m3u8", headers)
        self.assertEqual(mime, "application/vnd.apple.mpegurl")
        self.assertIn(b'URI="/freeview/media/', body)
        self.assertIn(("https://cdn.test/live/segment.ts", headers), client.urls.values())

    def test_catalogue_failure_keeps_cached_playlist_channels(self):
        service = object.__new__(gateway.Gateway)
        service.channel_cache_lock = threading.Lock()
        service.channel_cache = {"binge": CHANNELS["binge"]}
        service.cache_time = {"binge": 0}
        service.clients = {"binge": Mock()}
        service.clients["binge"].channels.side_effect = OSError("Network unavailable")
        self.assertEqual(service.channels("binge"), CHANNELS["binge"])

    def setUp(self):
        self.original_channels = gateway.GATEWAY.channels
        gateway.GATEWAY.channels = lambda provider: CHANNELS[provider]
        self.handler = object.__new__(gateway.Handler)
        self.handler.headers = {"Host": "gateway.test:8787"}

    def tearDown(self):
        gateway.GATEWAY.channels = self.original_channels

    def test_combined_playlist_has_stream_and_license_routes(self):
        playlist = self.handler.playlist(("binge", "kayo"))
        self.assertEqual(playlist.count("#EXTINF:"), 2)
        self.assertIn("/stream/binge/binge-1.mpd", playlist)
        self.assertIn("/license/binge/binge-1", playlist)
        self.assertIn("#KODIPROP:mimetype=application/dash+xml", playlist)
        self.assertIn("#KODIPROP:inputstream.adaptive.drm_legacy=com.widevine.alpha|", playlist)
        self.assertIn("#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha", playlist)
        self.assertEqual(playlist.count('group-title="Foxtel Live"'), 2)

    def test_epg_flattens_later_rows(self):
        root = ET.fromstring(self.handler.epg(("binge",)))
        self.assertEqual(len(root.findall("channel")), 1)
        self.assertEqual(len(root.findall("programme")), 3)
        self.assertEqual([x.findtext("title") for x in root.findall("programme")], ["Now", "Next", "Later"])

    def test_tivimate_playlist_uses_legacy_widevine_properties_only(self):
        playlist = self.handler.playlist(("binge",), client="tivimate")
        self.assertIn("inputstream.adaptive.license_type=com.widevine.alpha", playlist)
        self.assertIn("inputstream.adaptive.license_key=http://gateway.test:8787/license/binge/binge-1", playlist)
        self.assertNotIn("inputstream.adaptive.drm_legacy", playlist)
        self.assertNotIn("#KODIPROP:mimetype", playlist)


if __name__ == "__main__":
    unittest.main()
