"""Live dashboard: watch the stream in a browser while changing the network under it.

``python -m streamlink_rudp dashboard`` starts, in one process:

* a :class:`~streamlink_rudp.server.MediaServer` whose network emulator can be
  reconfigured at any moment, with live telemetry from its senders;
* a streaming client that downloads segments over RUDP forever (the video loops),
  choosing quality with the same ABR rule as the CLI client;
* a small HTTP server for the page, the video segments (remuxed to fragmented MP4
  for Media Source Extensions) and a Server-Sent Events feed of metrics.

The browser only talks HTTP to localhost; every media byte it plays first crossed
the emulated network over RUDP.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import media
from .abr import HybridABR, PlaybackModel
from .client import StreamClient
from .netem import NetemConfig
from .server import MediaServer
from .telemetry import Telemetry
from .transport import TransferError

log = logging.getLogger(__name__)

# Network presets: bottleneck rate (kbps, None = unlimited), one-way delay (ms), loss.
PRESETS = {
    "fiber":     {"label": "Fiber",       "rate_kbps": 20000, "delay_ms": 5,   "loss": 0.0},
    "wifi":      {"label": "Home Wi-Fi",  "rate_kbps": 8000,  "delay_ms": 15,  "loss": 0.002},
    "4g":        {"label": "4G",          "rate_kbps": 4000,  "delay_ms": 35,  "loss": 0.005},
    "3g":        {"label": "3G",          "rate_kbps": 1200,  "delay_ms": 90,  "loss": 0.01},
    "congested": {"label": "Congested",   "rate_kbps": 2500,  "delay_ms": 60,  "loss": 0.03},
}
SCENARIO = [("wifi", 18), ("3g", 14), ("4g", 14), ("fiber", 14), ("congested", 14)]
KEEP_SEGMENTS = 40
CACHE_DIR = Path.home() / ".cache" / "streamlink_rudp" / "dashboard-media-v2"


def ensure_media(media_dir: Path | None, duration: float = 30.0, progress=print) -> Path:
    """Use ``media_dir`` if given, otherwise a cached synthetic stream (built once)."""
    if media_dir is not None:
        if not (Path(media_dir) / media.MANIFEST_NAME).exists():
            raise media.MediaError(f"{media_dir} has no {media.MANIFEST_NAME}; run `prepare` first")
        return Path(media_dir)
    if (CACHE_DIR / media.MANIFEST_NAME).exists():
        return CACHE_DIR
    progress(f"Preparing a {duration:g}s test video in 3 qualities (one-time, cached in {CACHE_DIR})...")
    src = media.synthesize_source(CACHE_DIR / "source.mp4", duration)
    media.prepare(src, CACHE_DIR)
    (CACHE_DIR / "source.mp4").unlink(missing_ok=True)
    return CACHE_DIR


class LiveStream:
    """Endless adaptive download loop feeding the dashboard."""

    def __init__(self, client: StreamClient, manifest: dict, telemetry: Telemetry,
                 max_buffer: float = 10.0):
        self.client = client
        self.manifest = manifest
        self.telemetry = telemetry
        self.renditions = manifest["renditions"]
        self.max_buffer = max_buffer
        self.abr = HybridABR([r["bandwidth"] for r in self.renditions])
        self.model = PlaybackModel(startup_seconds=min(2.0, manifest["segment_duration"]))
        self.pinned: str | None = None
        self.segments: "collections.OrderedDict[int, tuple[bytes, dict]]" = collections.OrderedDict()
        self.seq = 0
        self.current: dict | None = None
        self.failures = 0
        self.started = threading.Event()
        self.stop = threading.Event()
        self._lock = threading.Lock()

    def run(self) -> None:
        self.started.wait()
        n = len(self.renditions[0]["segments"])
        last_choice = None
        while not self.stop.is_set():
            excess = self.model.level() - self.max_buffer
            if excess > 0:
                self.stop.wait(min(excess, 0.25))
                continue
            buffer_before = self.model.level()
            if self.pinned:
                choice = [r["id"] for r in self.renditions].index(self.pinned)
            else:
                choice = self.abr.choose(buffer_before)
            r = self.renditions[choice]
            seg = r["segments"][self.seq % n]
            started = time.monotonic()
            try:
                data = self.client._fetch_verified(seg)
            except TransferError as exc:
                self.failures += 1
                self.telemetry.event("transfer-failed", str(exc))
                self.stop.wait(0.5)
                continue
            elapsed = max(time.monotonic() - started, 1e-6)
            throughput = len(data) * 8 / elapsed
            if not self.pinned:
                self.abr.observe(throughput)
            stalls_before = self.model.stalls
            self.model.segment_arrived(seg["duration"])
            if self.model.stalls > stalls_before:
                self.telemetry.event("stall", "buffer ran dry")
            try:
                fmp4 = media.ts_to_fmp4(data)
            except media.MediaError as exc:
                self.telemetry.event("remux-failed", str(exc))
                continue
            if last_choice is not None and choice != last_choice:
                direction = "up" if choice > last_choice else "down"
                self.telemetry.event(f"switch-{direction}",
                                     f"{self.renditions[last_choice]['id']} → {r['id']}")
            last_choice = choice
            meta = {"seq": self.seq, "loop": self.seq // n, "rendition": r["id"],
                    "bitrate_kbps": round(r["bandwidth"] / 1000),
                    "throughput_kbps": round(throughput / 1000), "duration": seg["duration"],
                    "bytes": len(data), "download_ms": round(elapsed * 1000)}
            with self._lock:
                self.segments[self.seq] = (fmp4, meta)
                while len(self.segments) > KEEP_SEGMENTS:
                    self.segments.popitem(last=False)
                self.current = meta
            self.seq += 1

    def segment(self, seq: int):
        with self._lock:
            return self.segments.get(seq)

    def available(self) -> tuple[int, int]:
        with self._lock:
            if not self.segments:
                return 0, -1
            keys = list(self.segments)
            return keys[0], keys[-1]


class DashboardApp:
    def __init__(self, media_dir: Path, host: str = "127.0.0.1", port: int = 8080,
                 preset: str = "wifi"):
        self.telemetry = Telemetry()
        p = PRESETS[preset]
        self.netem = NetemConfig(rate_kbps=p["rate_kbps"], delay_ms=p["delay_ms"], loss=p["loss"],
                                 jitter_ms=1.0, queue_ms=80)
        self.preset = preset
        self.server = MediaServer(media_dir, udp_port=0, tcp_port=None, netem=self.netem,
                                  telemetry=self.telemetry)
        self.client = StreamClient(udp_port=self.server.udp_port, timeout=15)
        self.manifest = media.load_manifest((Path(media_dir) / media.MANIFEST_NAME).read_bytes())
        self.live = LiveStream(self.client, self.manifest, self.telemetry)
        self.scenario_on = False
        self._scenario_gen = 0
        self.t0 = time.monotonic()
        try:
            self.http = ThreadingHTTPServer((host, port), self._handler())
        except OSError:
            if port == 0:
                raise
            log.warning("port %d is busy; using a free port instead", port)
            self.http = ThreadingHTTPServer((host, 0), self._handler())
        self.http.daemon_threads = True
        self._page = resources.files("streamlink_rudp").joinpath("web/dashboard.html").read_bytes()

    @property
    def url(self) -> str:
        host, port = self.http.server_address[:2]
        return f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"

    # -- control ----------------------------------------------------------------------------
    def set_network(self, rate_kbps=None, delay_ms=None, loss=None, preset=None) -> None:
        if preset is not None:
            p = PRESETS[preset]
            rate_kbps, delay_ms, loss = p["rate_kbps"], p["delay_ms"], p["loss"]
            self.preset = preset
        else:
            self.preset = None
        cfg = self.netem
        if rate_kbps is not None:
            cfg.rate_kbps = float(rate_kbps) if float(rate_kbps) > 0 else None
        if delay_ms is not None:
            cfg.delay_ms = max(0.0, min(float(delay_ms), 1000.0))
        if loss is not None:
            cfg.loss = max(0.0, min(float(loss), 0.5))
        label = PRESETS[preset]["label"] if preset else "custom"
        rate = f"{cfg.rate_kbps / 1000:.1f} Mbps" if cfg.rate_kbps else "unlimited"
        self.telemetry.event("network", f"{label}: {rate}, {cfg.delay_ms:.0f} ms, {cfg.loss:.1%} loss")

    def set_scenario(self, on: bool) -> None:
        self.scenario_on = on
        self._scenario_gen += 1
        if on:
            threading.Thread(target=self._scenario, args=(self._scenario_gen,), daemon=True).start()

    def _scenario(self, gen: int) -> None:
        while self.scenario_on and gen == self._scenario_gen:
            for name, seconds in SCENARIO:
                if not self.scenario_on or gen != self._scenario_gen:
                    return
                self.set_network(preset=name)
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if not self.scenario_on or gen != self._scenario_gen:
                        return
                    time.sleep(0.2)

    def state(self) -> dict:
        tel = self.telemetry.snapshot()
        cfg = self.netem
        first, last = self.live.available()
        model = self.live.model
        sent = tel["segments_sent"] + tel["retransmissions"]
        return {
            "t": round(time.monotonic() - self.t0, 3),
            "transport": tel,
            "retransmit_pct": round(100 * tel["retransmissions"] / sent, 2) if sent else 0.0,
            "network": {"rate_kbps": cfg.rate_kbps, "delay_ms": cfg.delay_ms, "loss": cfg.loss,
                        "preset": self.preset, "scenario": self.scenario_on,
                        "queue_drops": self.server.emulator.stats.dropped_queue,
                        "random_drops": self.server.emulator.stats.dropped_random},
            "playback": {"buffer": round(model.level(), 2), "stalls": model.stalls,
                         "stall_seconds": round(model.stall_time, 2),
                         "startup_delay": model.startup_delay and round(model.startup_delay, 3),
                         "pinned": self.live.pinned, "current": self.live.current,
                         "estimate_kbps": round((self.live.abr.estimate or 0) / 1000)},
            "segments": {"first": first, "last": last},
        }

    def info(self) -> dict:
        return {"title": self.manifest["title"], "duration": self.manifest["duration"],
                "segment_duration": self.manifest["segment_duration"],
                "renditions": [{"id": r["id"], "width": r["width"], "height": r["height"],
                                "bitrate_kbps": round(r["bandwidth"] / 1000)} for r in self.manifest["renditions"]],
                "presets": {k: v for k, v in PRESETS.items()}}

    # -- lifecycle ----------------------------------------------------------------------------
    def start(self) -> "DashboardApp":
        self.server.start()
        self.server.emulator.reset_clock()
        threading.Thread(target=self.live.run, name="live-stream", daemon=True).start()
        threading.Thread(target=self.http.serve_forever, name="dashboard-http", daemon=True).start()
        return self

    def stop(self) -> None:
        self.live.stop.set()
        self.live.started.set()
        self.scenario_on = False
        self.http.shutdown()
        self.http.server_close()
        self.server.stop()

    # -- HTTP -----------------------------------------------------------------------------------
    def _handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, status: int = 200):
                self._send(status, json.dumps(obj).encode(), "application/json")

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(200, app._page, "text/html; charset=utf-8")
                elif path == "/api/info":
                    self._json(app.info())
                elif path == "/api/state":
                    self._json(app.state())
                elif path == "/api/events":
                    self._events()
                elif path.startswith("/seg/") and path.endswith(".mp4"):
                    try:
                        seq = int(path[5:-4])
                    except ValueError:
                        return self._json({"error": "bad segment"}, 400)
                    item = app.live.segment(seq)
                    if item is None:
                        return self._json({"error": "segment not available"}, 404)
                    fmp4, meta = item
                    self._send(200, fmp4, "video/mp4", {"X-Segment": json.dumps(meta)})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                length = min(int(self.headers.get("Content-Length") or 0), 10_000)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json({"error": "invalid JSON"}, 400)
                path = self.path.split("?", 1)[0]
                try:
                    if path == "/api/network":
                        preset = body.get("preset")
                        if preset is not None and preset not in PRESETS:
                            return self._json({"error": "unknown preset"}, 400)
                        if preset is not None or body.get("manual"):
                            app.set_scenario(False)
                        app.set_network(body.get("rate_kbps"), body.get("delay_ms"), body.get("loss"), preset)
                    elif path == "/api/abr":
                        pin = body.get("pin")
                        if pin is not None and pin not in [r["id"] for r in app.manifest["renditions"]]:
                            return self._json({"error": "unknown rendition"}, 400)
                        app.live.pinned = pin
                        app.telemetry.event("abr", f"quality pinned to {pin}" if pin else "adaptive quality")
                    elif path == "/api/scenario":
                        app.set_scenario(bool(body.get("on")))
                    else:
                        return self._json({"error": "not found"}, 404)
                except (TypeError, ValueError) as exc:
                    return self._json({"error": str(exc)}, 400)
                self._json(app.state())

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                app.live.started.set()  # the stream starts when the first viewer connects
                last_event = 0
                first, last = app.live.available()
                last_seq = max(first, last - 2) - 1  # a viewer joining late starts near the live edge
                try:
                    while not app.live.stop.is_set():
                        state = app.state()
                        events = app.telemetry.events_since(last_event)
                        if events:
                            last_event = events[-1][0]
                        state["events"] = [{"id": i, "t": round(t - app.t0, 3), "kind": k, "detail": d}
                                           for i, t, k, d in events]
                        first, last = app.live.available()
                        state["new_segments"] = []
                        for seq in range(max(last_seq + 1, first), last + 1):
                            item = app.live.segment(seq)
                            if item:
                                state["new_segments"].append(item[1])
                        last_seq = max(last_seq, last)
                        self.wfile.write(f"data: {json.dumps(state)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.25)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        return Handler


def run(media_dir: Path | None = None, host: str = "127.0.0.1", port: int = 8080,
        open_browser: bool = True, preset: str = "wifi") -> None:
    import webbrowser

    directory = ensure_media(media_dir)
    app = DashboardApp(directory, host, port, preset).start()
    print(f"StreamLink dashboard running at {app.url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(app.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        app.stop()
