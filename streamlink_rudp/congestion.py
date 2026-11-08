"""Window-based congestion control in the style of TCP NewReno (RFC 5681 / RFC 6582).

The window is counted in segments. The transport tells the controller what happened
(new data acknowledged, a loss detected by duplicate ACKs / SACK, or a timeout) and the
controller answers with the current congestion window.
"""
from __future__ import annotations


class NewReno:
    def __init__(self, initial_cwnd: float = 2.0, initial_ssthresh: float = 64.0,
                 max_cwnd: float = 4096.0):
        self.cwnd = float(initial_cwnd)
        self.ssthresh = float(initial_ssthresh)
        self.max_cwnd = max_cwnd
        self.in_recovery = False
        self.recovery_point = 0  # segment index that ends fast recovery once acknowledged

    @property
    def phase(self) -> str:
        if self.in_recovery:
            return "fast-recovery"
        return "slow-start" if self.cwnd < self.ssthresh else "congestion-avoidance"

    def on_ack(self, newly_acked: int, cumulative_index: int) -> None:
        """New data acknowledged; ``cumulative_index`` is the first segment not yet acked."""
        if newly_acked <= 0:
            return
        if self.in_recovery:
            if cumulative_index >= self.recovery_point:
                # Full acknowledgement: leave recovery and deflate the window.
                self.in_recovery = False
                self.cwnd = self.ssthresh
            return
        if self.cwnd < self.ssthresh:
            self.cwnd += newly_acked  # slow start: exponential growth per RTT
        else:
            self.cwnd += newly_acked / self.cwnd  # congestion avoidance: +1 segment per RTT
        self.cwnd = min(self.cwnd, self.max_cwnd)

    def on_loss(self, flight_size: int, highest_sent: int) -> bool:
        """Loss detected by duplicate ACKs or SACK. Returns True if the window was reduced.

        At most one reduction happens per window of data, as in NewReno.
        """
        if self.in_recovery:
            return False
        self.ssthresh = max(flight_size / 2, 2.0)
        self.cwnd = self.ssthresh
        self.in_recovery = True
        self.recovery_point = highest_sent
        return True

    def on_timeout(self, flight_size: int) -> None:
        self.ssthresh = max(flight_size / 2, 2.0)
        self.cwnd = 1.0
        self.in_recovery = False
