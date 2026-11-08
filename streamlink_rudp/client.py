"""Streaming client: adaptive segment download, integrity checks, live playback, MP4 output."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import random
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .abr import HybridABR, PlaybackModel
from .media import MANIFEST_NAME, MediaError, ffmpeg_path, load_manifest, to_mp4
from .netem import NetemConfig, NetworkEmulator
from .server import DEFAULT_TCP_PORT, DEFAULT_UDP_PORT
from .transport import TransferError, receive

log = logging.getLogger(__name__)


class IntegrityError(TransferError):
    pass


@dataclass
class SegmentRecord:
    index: int
    rendition: str
    bitrate_kbps: int
    bytes: int
    download_seconds: float
    throughput_kbps: float
    buffer_before: float
    at: float  # seconds since session start


@dataclass
class SessionReport:
    title: str
    transport: str
    segments: list[SegmentRecord] = field(default_factory=list)
    duration_media: float = 0.0
    wall_seconds: float = 0.0
    bytes: int = 0
    startup_delay: float | None = None
    stalls: int = 0
    stall_seconds: float = 0.0
    output_ts: str | None = None
    output_mp4: str | None = None

    @property
    def switches(self) -> int:
        return sum(1 for a, b in zip(self.segments, self.segments[1:]) if a.rendition != b.rendition)

    @property
    def average_bitrate_kbps(self) -> float:
        total = sum(s.bitrate_kbps for s in self.segments)
        return total / len(self.segments) if self.segments else 0.0

    def to_json(self) -> str:
        data = asdict(self)
        data.update(switches=self.switches, average_bitrate_kbps=round(self.average_bitrate_kbps, 1))
        return json.dumps(data, indent=2)


class Player:
    """Plays an MPEG-TS byte stream fed through the player's standard input."""

    CANDIDATES = (
        ("ffplay", ["-hide_banner", "-loglevel", "error", "-autoexit",
                    "-window_title", "StreamLink RUDP", "-i", "pipe:0"]),
        ("mpv", ["--force-window=yes", "--title=StreamLink RUDP", "--really-quiet", "-"]),
        ("vlc", ["--play-and-exit", "--quiet", "-"]),
    )
    WINDOWS_VLC = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VideoLAN" / "VLC" / "vlc.exe"

    def __init__(self, preferred: str | None = None):
        self.command = self.find(preferred)
        if self.command is None:
            raise MediaError("no video player found: install ffplay (part of ffmpeg), mpv or VLC")
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self.alive = True
        self._thread = threading.Thread(target=self._writer, name="player-feed", daemon=True)
        self._thread.start()

    @classmethod
    def find(cls, preferred: str | None = None) -> list[str] | None:
        for name, args in cls.CANDIDATES:
            if preferred and preferred != name:
                continue
            exe = shutil.which(name)
            if not exe and name == "vlc" and cls.WINDOWS_VLC.exists():
                exe = str(cls.WINDOWS_VLC)
            if exe:
                return [exe, *args]
        return None

    def feed(self, data: bytes) -> None:
        if self.alive:
            self._queue.put(data)

    def _writer(self) -> None:
        while True:
            data = self._queue.get()
            if data is None:
                break
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.alive = False  # the viewer closed the window; keep downloading
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def close(self, wait: bool = True) -> None:
        self._queue.put(None)
        if wait:
            self._thread.join()
            self.proc.wait()


class StreamClient:
    def __init__(self, host: str = "127.0.0.1", udp_port: int = DEFAULT_UDP_PORT,
                 tcp_port: int = DEFAULT_TCP_PORT, transport: str = "rudp",
                 netem: NetemConfig | None = None, timeout: float = 10.0):
        if transport not in ("rudp", "tcp"):
            raise ValueError("transport must be 'rudp' or 'tcp'")
        # Resolve once: the transport compares reply addresses with the server's IP.
        self.host = socket.gethostbyname(host)
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.transport = transport
        self.timeout = timeout
        self.emulator = NetworkEmulator(netem)
        self._rng = random.SystemRandom()

    # -- object transfer ------------------------------------------------------------------
    def fetch(self, path: str) -> bytes:
        return self._fetch_rudp(path) if self.transport == "rudp" else self._fetch_tcp(path)

    def _fetch_rudp(self, path: str) -> bytes:
        conn = self._rng.getrandbits(32)
        request = json.dumps({"op": "GET", "path": path, "conn": conn}).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", 0))
            data, _ = receive(self.emulator.wrap(sock), (self.host, self.udp_port), request, conn,
                              idle_timeout=self.timeout)
        return data

    def _fetch_tcp(self, path: str) -> bytes:
        with socket.create_connection((self.host, self.tcp_port), timeout=self.timeout) as sock:
            sock.sendall(json.dumps({"op": "GET", "path": path}).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise TransferError("server closed the connection")
                buf += chunk
            header_line, body = buf.split(b"\n", 1)
            header = json.loads(header_line)
            if not header.get("ok"):
                raise TransferError(header.get("error", "request failed"))
            size = header["size"]
            parts, received = [body], len(body)
            while received < size:
                chunk = sock.recv(min(1 << 16, size - received))
                if not chunk:
                    raise TransferError(f"connection closed after {received} of {size} bytes")
                parts.append(chunk)
                received += len(chunk)
            return b"".join(parts)

    def get_manifest(self) -> dict:
        return load_manifest(self.fetch(MANIFEST_NAME))

    # -- streaming ------------------------------------------------------------------------------
    def stream(self, out_dir: Path, *, play: bool = False, player: str | None = None,
               realtime: bool | None = None, max_buffer: float = 15.0, rendition: str | None = None,
               make_mp4: bool = True, on_segment=None) -> SessionReport:
        """Download the whole stream adaptively, optionally playing it as it arrives.

        ``realtime`` paces downloads like a real player (never more than ``max_buffer``
        seconds ahead). It defaults to on while playing, off otherwise.
        ``rendition`` pins one rendition instead of adapting.
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        realtime = play if realtime is None else realtime
        manifest = self.get_manifest()
        renditions = manifest["renditions"]
        if rendition is not None:
            ids = [r["id"] for r in renditions]
            if rendition not in ids:
                raise MediaError(f"unknown rendition {rendition!r}; available: {', '.join(ids)}")
            fixed = ids.index(rendition)
        abr = HybridABR([r["bandwidth"] for r in renditions])
        model = PlaybackModel(startup_seconds=min(2.0, manifest["segment_duration"]))
        report = SessionReport(title=manifest["title"], transport=self.transport,
                               duration_media=manifest["duration"])
        ts_path = out_dir / "stream.ts"
        viewer = Player(player) if play else None
        t0 = time.monotonic()
        used: set[str] = set()
        try:
            with open(ts_path, "wb") as ts:
                for index in range(len(renditions[0]["segments"])):
                    if realtime:
                        excess = model.level() - max_buffer
                        if excess > 0:
                            time.sleep(excess)
                    buffer_before = model.level()
                    choice = fixed if rendition is not None else abr.choose(buffer_before)
                    r = renditions[choice]
                    seg = r["segments"][index]
                    started = time.monotonic()
                    data = self._fetch_verified(seg)
                    elapsed = max(time.monotonic() - started, 1e-6)
                    abr.observe(len(data) * 8 / elapsed)
                    model.segment_arrived(seg["duration"])
                    ts.write(data)
                    if viewer:
                        viewer.feed(data)
                    used.add(r["id"])
                    record = SegmentRecord(index, r["id"], round(r["bandwidth"] / 1000), len(data),
                                           round(elapsed, 4), round(len(data) * 8 / elapsed / 1000, 1),
                                           round(buffer_before, 3), round(started - t0, 3))
                    report.segments.append(record)
                    report.bytes += len(data)
                    if on_segment:
                        on_segment(record, model)
        finally:
            if viewer:
                viewer.close(wait=False)
        model.finish()
        report.wall_seconds = round(time.monotonic() - t0, 3)
        report.startup_delay = model.startup_delay and round(model.startup_delay, 3)
        report.stalls = model.stalls
        report.stall_seconds = round(model.stall_time, 3)
        report.output_ts = str(ts_path)
        if make_mp4 and ffmpeg_path():
            height = max(r["height"] for r in renditions if r["id"] in used)
            report.output_mp4 = str(to_mp4(ts_path, out_dir / "stream.mp4", used, height))
        (out_dir / "session.json").write_text(report.to_json())
        if viewer:
            viewer.close(wait=True)  # let the viewer finish watching
        return report

    def _fetch_verified(self, segment: dict, attempts: int = 3) -> bytes:
        for attempt in range(attempts):
            data = self.fetch(segment["path"])
            if len(data) == segment["size"] and hashlib.sha256(data).hexdigest() == segment["sha256"]:
                return data
            log.warning("integrity check failed for %s (attempt %d)", segment["path"], attempt + 1)
        raise IntegrityError(f"{segment['path']}: content does not match the manifest")
