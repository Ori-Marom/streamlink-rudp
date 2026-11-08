"""User-space network emulator, in the spirit of Linux ``tc netem``.

Wrap a UDP socket with :meth:`NetworkEmulator.wrap` and every datagram it sends passes
through an emulated link that can:

* drop packets at random (``loss``), duplicate them or corrupt a byte;
* add a one-way propagation delay with jitter (packets stay in order, as on a real path);
* reorder packets explicitly (``reorder``: a packet is held back so later ones overtake it);
* limit bandwidth through a bottleneck with a drop-tail queue, so that sending too fast
  causes real *congestion* loss, which is what congestion control reacts to;
* change the bottleneck bandwidth over time (``bandwidth_profile``), to exercise
  adaptive bitrate switching.

All sockets wrapped by one emulator share the same bottleneck, so concurrent clients
compete for bandwidth like flows on a real link.
"""
from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
from dataclasses import dataclass, field


@dataclass
class NetemConfig:
    loss: float = 0.0                 # probability of dropping a packet
    delay_ms: float = 0.0             # one-way propagation delay
    jitter_ms: float = 0.0            # uniform +/- jitter added to the delay (order-preserving)
    reorder: float = 0.0              # probability a packet is held back and overtaken
    duplicate: float = 0.0            # probability of delivering a packet twice
    corrupt: float = 0.0              # probability of flipping one byte
    rate_kbps: float | None = None    # bottleneck bandwidth; None = unlimited
    queue_ms: float = 60.0            # bottleneck buffer, expressed as queueing delay
    bandwidth_profile: list[tuple[float, float]] = field(default_factory=list)
    seed: int | None = None

    @property
    def is_noop(self) -> bool:
        return not (self.loss or self.delay_ms or self.jitter_ms or self.reorder or self.duplicate
                    or self.corrupt or self.rate_kbps or self.bandwidth_profile)

    @staticmethod
    def parse_profile(text: str) -> list[tuple[float, float]]:
        """Parse ``"0:6000,10:1500,20:5000"`` (seconds:kbps) into a sorted profile."""
        steps = []
        for part in filter(None, (p.strip() for p in text.split(","))):
            at, kbps = part.split(":")
            steps.append((float(at), float(kbps)))
        return sorted(steps)


@dataclass
class LinkStats:
    sent: int = 0
    dropped_random: int = 0
    dropped_queue: int = 0
    reordered: int = 0
    duplicated: int = 0
    corrupted: int = 0


class NetworkEmulator:
    def __init__(self, config: NetemConfig | None = None):
        self.config = config or NetemConfig()
        self.stats = LinkStats()
        self._rng = random.Random(self.config.seed)
        self._lock = threading.Condition()
        self._heap: list[tuple[float, int, object, bytes, object]] = []
        self._counter = itertools.count()
        self._link_free_at = 0.0
        self._last_delivery = 0.0
        self._start = time.monotonic()
        self._thread: threading.Thread | None = None
        self._closed = False

    def wrap(self, sock) -> "EmulatedSocket":
        return EmulatedSocket(sock, self)

    def rate_kbps(self, now: float | None = None) -> float | None:
        cfg = self.config
        rate = cfg.rate_kbps
        if cfg.bandwidth_profile:
            elapsed = (now or time.monotonic()) - self._start
            for at, kbps in cfg.bandwidth_profile:
                if elapsed >= at:
                    rate = kbps
        return rate

    def reset_clock(self) -> None:
        """Restart the bandwidth profile from t=0."""
        self._start = time.monotonic()

    # -- datapath -------------------------------------------------------------------------
    def send(self, sock, data: bytes, addr) -> None:
        cfg = self.config
        if cfg.is_noop:
            sock.sendto(data, addr)
            return
        with self._lock:
            self.stats.sent += 1
            rng = self._rng
            if cfg.loss and rng.random() < cfg.loss:
                self.stats.dropped_random += 1
                return
            now = time.monotonic()
            depart = now
            rate = self.rate_kbps(now)
            if rate:
                start = max(now, self._link_free_at)
                if (start - now) * 1000 > cfg.queue_ms:
                    self.stats.dropped_queue += 1  # bottleneck buffer full: tail drop
                    return
                self._link_free_at = start + len(data) * 8 / (rate * 1000)
                depart = self._link_free_at
            copies = 2 if cfg.duplicate and rng.random() < cfg.duplicate else 1
            if copies == 2:
                self.stats.duplicated += 1
            for _ in range(copies):
                payload = data
                if cfg.corrupt and rng.random() < cfg.corrupt and data:
                    self.stats.corrupted += 1
                    buf = bytearray(data)
                    buf[rng.randrange(len(buf))] ^= 1 << rng.randrange(8)
                    payload = bytes(buf)
                delay = cfg.delay_ms
                if cfg.jitter_ms:
                    delay = max(0.0, delay + rng.uniform(-cfg.jitter_ms, cfg.jitter_ms))
                # FIFO: jitter never lets a packet overtake one sent before it ...
                deliver_at = max(depart + delay / 1000, self._last_delivery)
                if cfg.reorder and rng.random() < cfg.reorder:
                    # ... unless it is explicitly reordered: held back so successors pass it.
                    self.stats.reordered += 1
                    deliver_at += rng.uniform(1, 2 + cfg.jitter_ms) / 1000
                else:
                    self._last_delivery = deliver_at
                if deliver_at <= now:
                    sock.sendto(payload, addr)
                    continue
                heapq.heappush(self._heap, (deliver_at, next(self._counter), sock, payload, addr))
            if self._heap:
                self._ensure_thread()
                self._lock.notify()

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._deliver_loop, name="netem", daemon=True)
            self._thread.start()

    def _deliver_loop(self) -> None:
        while True:
            with self._lock:
                while not self._heap and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                deliver_at = self._heap[0][0]
                wait = deliver_at - time.monotonic()
                if wait > 0:
                    self._lock.wait(wait)
                    continue
                _, _, sock, payload, addr = heapq.heappop(self._heap)
            try:
                sock.sendto(payload, addr)
            except OSError:
                pass  # socket closed while the packet was "in flight"

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lock.notify_all()


class EmulatedSocket:
    """Socket proxy whose outgoing datagrams traverse an emulated link."""

    def __init__(self, sock, emulator: NetworkEmulator):
        self._sock = sock
        self._emulator = emulator

    def sendto(self, data: bytes, addr) -> int:
        self._emulator.send(self._sock, data, addr)
        return len(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)
