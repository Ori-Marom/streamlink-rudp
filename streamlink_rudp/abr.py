"""Adaptive bitrate (ABR) selection and a playback-buffer model.

The client picks a rendition before each segment with a hybrid rule used by many real
players:

* **Throughput:** estimate bandwidth as the harmonic mean of the last few segment
  downloads (robust to one lucky fast download) and choose the best rendition whose
  bitrate fits under a safety margin of that estimate.
* **Buffer:** when the playback buffer is low, never step up, and drop straight to
  the lowest rendition if a stall is imminent. Step up at most one rendition at a time
  so a single fast download does not cause a jump straight to the top.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


class HybridABR:
    def __init__(self, bitrates: list[int], safety: float = 0.8, window: int = 4,
                 low_buffer: float = 6.0, panic_buffer: float = 2.0, start_index: int = 0):
        if not bitrates:
            raise ValueError("bitrate ladder is empty")
        self.bitrates = list(bitrates)  # bits per second, ascending
        self.safety = safety
        self.window = window
        self.low_buffer = low_buffer
        self.panic_buffer = panic_buffer
        self.samples: list[float] = []
        self.current = min(start_index, len(bitrates) - 1)

    def observe(self, throughput_bps: float) -> None:
        if throughput_bps > 0:
            self.samples = (self.samples + [throughput_bps])[-self.window:]

    @property
    def estimate(self) -> float | None:
        if not self.samples:
            return None
        return len(self.samples) / sum(1 / s for s in self.samples)

    def choose(self, buffer_seconds: float) -> int:
        estimate = self.estimate
        if estimate is None:
            return self.current
        budget = estimate * self.safety
        target = 0
        for i, rate in enumerate(self.bitrates):
            if rate <= budget:
                target = i
        if target > self.current:
            target = self.current + 1 if buffer_seconds >= self.low_buffer else self.current
        if buffer_seconds < self.panic_buffer:
            target = 0
        self.current = target
        return target


@dataclass
class PlaybackModel:
    """Wall-clock model of a player's buffer, used for ABR decisions and stall statistics.

    Playback starts once ``startup_seconds`` of media are buffered. When the buffer runs
    dry the player stalls until the next segment arrives.
    """

    startup_seconds: float = 2.0
    buffered: float = 0.0           # seconds of media downloaded
    played_offset: float = 0.0      # media position when the clock was last anchored
    started_at: float | None = None
    stalls: int = 0
    stall_time: float = 0.0
    startup_delay: float | None = None
    _created: float = field(default_factory=time.monotonic)

    def position(self, now: float | None = None) -> float:
        if self.started_at is None:
            return 0.0
        now = time.monotonic() if now is None else now
        return min(self.buffered, self.played_offset + (now - self.started_at))

    def level(self, now: float | None = None) -> float:
        """Seconds of media buffered ahead of the playhead."""
        return self.buffered - self.position(now)

    def segment_arrived(self, duration: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.started_at is not None:
            # Did the buffer run dry before this segment landed?
            if self.played_offset + (now - self.started_at) > self.buffered:
                dry_at = self.started_at + (self.buffered - self.played_offset)
                self.stalls += 1
                self.stall_time += now - dry_at
                self.played_offset = self.buffered
                self.started_at = now
        self.buffered += duration
        if self.started_at is None and self.buffered >= self.startup_seconds:
            self.started_at = now
            self.startup_delay = now - self._created

    def finish(self) -> None:
        """Account for any pending stall at the end of a session."""
        if self.started_at is None:
            self.startup_delay = time.monotonic() - self._created
