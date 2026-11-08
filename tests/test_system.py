"""System tests: media server, concurrent clients, discovery and real video end to end."""
import hashlib
import logging
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from streamlink_rudp import discovery, media
from streamlink_rudp.client import StreamClient
from streamlink_rudp.netem import NetemConfig
from streamlink_rudp.server import MediaServer, RequestError
from streamlink_rudp.transport import TransferError

logging.disable(logging.CRITICAL)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "media"
        self.root.mkdir()
        self.files = {f"obj{i}.bin": bytes([i]) * (40_000 + i * 7_000) for i in range(6)}
        for name, blob in self.files.items():
            (self.root / name).write_bytes(blob)
        (Path(self.tmp.name) / "secret.txt").write_text("outside the root")
        self.server = MediaServer(self.root, udp_port=0, tcp_port=0,
                                  netem=NetemConfig(loss=0.03, delay_ms=5, jitter_ms=3, seed=1)).start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def client(self, transport="rudp"):
        return StreamClient(udp_port=self.server.udp_port, tcp_port=self.server.tcp_port,
                            transport=transport, timeout=5)

    def test_fetch_over_both_transports(self):
        for transport in ("rudp", "tcp"):
            with self.subTest(transport=transport):
                self.assertEqual(self.client(transport).fetch("obj3.bin"), self.files["obj3.bin"])

    def test_server_given_by_hostname(self):
        client = StreamClient("localhost", self.server.udp_port, self.server.tcp_port, timeout=5)
        self.assertEqual(client.fetch("obj1.bin"), self.files["obj1.bin"])

    def test_concurrent_clients(self):
        results, errors = {}, []

        def worker(name):
            try:
                results[name] = self.client().fetch(name)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in self.files]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(results, self.files)

    def test_path_traversal_and_missing_files_are_rejected(self):
        for bad in ("../secret.txt", "/etc/passwd", "missing.bin", ""):
            with self.subTest(path=bad):
                with self.assertRaises(RequestError):
                    self.server.resolve(bad)
                with self.assertRaises(TransferError):
                    self.client().fetch(bad)
                with self.assertRaises(TransferError):
                    self.client("tcp").fetch(bad)


class DiscoveryTests(unittest.TestCase):
    class Capture:
        def __init__(self):
            self.messages = []

        def sendto(self, data, addr):
            self.messages.append(data.decode())

    def test_dhcp_dora_nak_and_exhaustion(self):
        server, sock, addr = discovery.DHCPServer(port=0, pool=["10.0.0.5"]), self.Capture(), ("c", 1)
        server.handle_discover(sock, addr)
        self.assertEqual(sock.messages[-1], "DHCP_OFFER:10.0.0.5")
        server.handle_request(sock, addr, "10.0.0.5")
        self.assertEqual(sock.messages[-1], "DHCP_ACK:10.0.0.5")
        server.handle_request(sock, addr, "10.0.0.9")
        self.assertEqual(sock.messages[-1], "DHCP_NAK:10.0.0.9")
        server.handle_discover(sock, ("other", 2))
        self.assertEqual(sock.messages[-1], "DHCP_OFFER:NONE")
        server.handle(sock, addr, "DHCP_RELEASE:10.0.0.5")
        server.handle_discover(sock, ("other", 2))
        self.assertEqual(sock.messages[-1], "DHCP_OFFER:10.0.0.5")

    def test_live_dhcp_dns_and_doh(self):
        dhcp = discovery.DHCPServer(port=0).start()
        dns = discovery.DNSServer(port=0, doh_port=0, records={"app.server.local": "10.1.2.3"}).start()
        try:
            self.assertEqual(discovery.dhcp_lease(("127.0.0.1", dhcp.port)), "192.168.1.100")
            self.assertEqual(discovery.dns_lookup("APP.server.local.", ("127.0.0.1", dns.port)), "10.1.2.3")
            self.assertEqual(discovery.doh_lookup(
                "app.server.local", f"http://127.0.0.1:{dns.doh_port}/dns-query"), "10.1.2.3")
            with self.assertRaises(LookupError):
                discovery.dns_lookup("nope.local", ("127.0.0.1", dns.port))
        finally:
            dhcp.stop()
            dns.stop()


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class VideoEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        src = media.synthesize_source(base / "src.mp4", duration=8, width=640, height=360)
        cls.manifest = media.prepare(src, base / "media", ladder=media.TINY_LADDER, segment_seconds=2)
        cls.base = base

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_manifest_is_consistent(self):
        m = self.manifest
        self.assertEqual([r["id"] for r in m["renditions"]], ["180p", "360p"])
        self.assertEqual(len({len(r["segments"]) for r in m["renditions"]}), 1)
        self.assertAlmostEqual(m["duration"], 8, delta=0.2)
        for r in m["renditions"]:
            for s in r["segments"]:
                blob = (self.base / "media" / s["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(blob).hexdigest(), s["sha256"])

    def test_stream_over_lossy_link_produces_playable_video(self):
        netem = NetemConfig(loss=0.05, delay_ms=10, jitter_ms=4, seed=3)
        with MediaServer(self.base / "media", udp_port=0, tcp_port=None, netem=netem) as server:
            out = self.base / "out"
            report = StreamClient(udp_port=server.udp_port).stream(out)
        self.assertEqual(len(report.segments), len(self.manifest["renditions"][0]["segments"]))
        self.assertTrue(Path(report.output_mp4).exists())
        decode = subprocess.run(["ffmpeg", "-v", "error", "-i", report.output_ts, "-f", "null", "-"],
                                capture_output=True, text=True)
        self.assertEqual(decode.returncode, 0)
        self.assertEqual(decode.stderr.strip(), "")
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", report.output_mp4], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(float(probe["format"]["duration"]), 8, delta=0.5)
        self.assertEqual(sorted(s["codec_type"] for s in probe["streams"]), ["audio", "video"])
        session = json.loads((out / "session.json").read_text())
        self.assertEqual(session["transport"], "rudp")

    def test_mixed_renditions_decode_cleanly(self):
        with MediaServer(self.base / "media", udp_port=0, tcp_port=0) as server:
            client = StreamClient(udp_port=server.udp_port, tcp_port=server.tcp_port, transport="tcp")
            manifest = client.get_manifest()
            parts = [client.fetch(manifest["renditions"][i % 2]["segments"][i]["path"])
                     for i in range(len(manifest["renditions"][0]["segments"]))]
        mixed = self.base / "mixed.ts"
        mixed.write_bytes(b"".join(parts))
        decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mixed), "-f", "null", "-"],
                                capture_output=True, text=True)
        self.assertEqual((decode.returncode, decode.stderr.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
