"""Live sender statistics, shared between transfer threads and a dashboard.

A :class:`Telemetry` object is handed to every :class:`~streamlink_rudp.transport.Sender`
started by a server. Senders write plain attributes as they run; readers take a
consistent copy with :meth:`snapshot`. Counters are cumulative across transfers.
"""
from __future__ import annotations

import collections
import threading
import time


class Telemetry:
    def __init__(self, max_events: int = 200):
        self._lock = threading.Lock()
        self.cwnd = 0.0
        self.ssthresh = 0.0
        self.pipe = 0
        self.srtt: float | None = None
        self.rto = 0.0
        self.phase = "idle"
        self.segments_sent = 0
        self.retransmissions = 0
        self.timeouts = 0
        self.fast_recoveries = 0
        self.acked_bytes = 0
        self.transfers = 0
        self._events: collections.deque = collections.deque(maxlen=max_events)
        self._event_id = 0

    def event(self, kind: str, detail: str = "") -> None:
        with self._lock:
            self._event_id += 1
            self._events.append((self._event_id, time.monotonic(), kind, detail))

    def events_since(self, last_id: int) -> list[tuple[int, float, str, str]]:
        with self._lock:
            return [e for e in self._events if e[0] > last_id]

    def snapshot(self) -> dict:
        return {
            "cwnd": round(self.cwnd, 2), "ssthresh": round(min(self.ssthresh, 1e6), 2),
            "pipe": self.pipe, "phase": self.phase,
            "srtt_ms": round(self.srtt * 1000, 1) if self.srtt else None,
            "rto_ms": round(self.rto * 1000, 1),
            "segments_sent": self.segments_sent, "retransmissions": self.retransmissions,
            "timeouts": self.timeouts, "fast_recoveries": self.fast_recoveries,
            "acked_bytes": self.acked_bytes, "transfers": self.transfers,
        }
