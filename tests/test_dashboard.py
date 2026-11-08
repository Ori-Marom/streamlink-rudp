"""Dashboard tests: HTTP API, live controls, and browser-compatible segment remuxing."""
import json
import logging
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from streamlink_rudp import media

logging.disable(logging.CRITICAL)
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def tfdt(fmp4: bytes) -> int:
    """baseMediaDecodeTime of the first fragment."""
    i = fmp4.find(b"tfdt")
    version = fmp4[i + 4]
    return struct.unpack(">Q", fmp4[i + 8:i + 16])[0] if version == 1 else struct.unpack(">I", fmp4[i + 8:i + 12])[0]


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlink_rudp.dashboard import DashboardApp
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        src = media.synthesize_source(base / "src.mp4", duration=6, width=640, height=360)
        media.prepare(src, base / "media", ladder=media.TINY_LADDER, segment_seconds=2)
        cls.media_dir = base / "media"
        cls.app = DashboardApp(cls.media_dir, port=0).start()
        cls.base_url = cls.app.url.rstrip("/")
        cls.app.live.started.set()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base_url + path, timeout=10) as r:
            return r.status, r.headers, r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_for_segments(self, count, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            first, last = self.app.live.available()
            if last + 1 >= count:
                return
            time.sleep(0.1)
        self.fail(f"fewer than {count} segments arrived")

    def test_page_and_info(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"StreamLink Live", body)
        info = json.loads(self.get("/api/info")[2])
        self.assertEqual([r["id"] for r in info["renditions"]], ["180p", "360p"])
        self.assertIn("3g", info["presets"])

    def test_segments_stream_and_are_browser_ready(self):
        self.wait_for_segments(3)
        status, headers, seg0 = self.get("/seg/0.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(seg0[4:8], b"ftyp")
        meta = json.loads(headers["X-Segment"])
        self.assertEqual((meta["seq"], meta["loop"]), (0, 0))
        seg2 = self.get("/seg/2.mp4")[2]
        self.assertGreater(tfdt(seg2), tfdt(seg0))  # real timeline, not rebased to zero

    def test_network_controls(self):
        status, state = self.post("/api/network", {"preset": "3g"})
        self.assertEqual(status, 200)
        self.assertEqual(state["network"]["rate_kbps"], 1200)
        self.assertEqual(self.app.netem.delay_ms, 90)
        status, state = self.post("/api/network", {"manual": True, "rate_kbps": 5000, "delay_ms": 10, "loss": 0.02})
        self.assertEqual((state["network"]["rate_kbps"], state["network"]["loss"]), (5000, 0.02))
        self.assertIsNone(state["network"]["preset"])
        self.assertEqual(self.post("/api/network", {"preset": "nope"})[0], 400)
        self.post("/api/network", {"preset": "fiber"})

    def test_quality_pin_and_scenario(self):
        status, state = self.post("/api/abr", {"pin": "360p"})
        self.assertEqual(state["playback"]["pinned"], "360p")
        self.assertEqual(self.post("/api/abr", {"pin": "8k"})[0], 400)
        self.post("/api/abr", {"pin": None})
        status, state = self.post("/api/scenario", {"on": True})
        self.assertTrue(state["network"]["scenario"])
        status, state = self.post("/api/scenario", {"on": False})
        self.assertFalse(state["network"]["scenario"])

    def test_event_stream(self):
        self.wait_for_segments(1)
        with urllib.request.urlopen(self.base_url + "/api/events", timeout=10) as r:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            line = r.readline()
            while not line.startswith(b"data:"):
                line = r.readline()
        state = json.loads(line[5:])
        self.assertIn("cwnd", state["transport"])
        self.assertIn("buffer", state["playback"])

    def test_unknown_routes(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/seg/99999.mp4")
        self.assertEqual(ctx.exception.code, 404)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class RemuxTests(unittest.TestCase):
    """The properties browsers need from each remuxed segment (found failing in Chrome)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        src = media.synthesize_source(base / "src.mp4", duration=6, width=640, height=360)
        cls.manifest = media.prepare(src, base / "media", ladder=media.TINY_LADDER, segment_seconds=2)
        cls.base = base / "media"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def remux(self, rendition, index):
        return media.ts_to_fmp4((self.base / rendition / f"seg_{index:04d}.ts").read_bytes())

    def test_audio_config_is_in_the_header(self):
        out = Path(self.tmp.name) / "s.mp4"
        out.write_bytes(self.remux("180p", 0))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,profile",
                                "-of", "json", str(out)], capture_output=True, text=True)
        profiles = {s["codec_name"]: s.get("profile") for s in json.loads(probe.stdout)["streams"]}
        self.assertEqual(profiles["aac"], "LC")

    def test_timestamps_are_continuous_across_renditions(self):
        a, b, c = self.remux("180p", 0), self.remux("360p", 1), self.remux("180p", 2)
        self.assertNotIn(b"elst", a)
        t0, t1, t2 = tfdt(a), tfdt(b), tfdt(c)
        self.assertAlmostEqual((t1 - t0) / 90000, 2.0, delta=0.05)
        self.assertAlmostEqual((t2 - t1) / 90000, 2.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
