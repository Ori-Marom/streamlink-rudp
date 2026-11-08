"""Retransmission-timeout estimation (RFC 6298)."""
from __future__ import annotations


class RTOEstimator:
    """Smoothed RTT / RTT variance estimator with exponential backoff.

    Follows RFC 6298 with one documented deviation: the lower bound defaults to 200 ms
    (as in Linux) instead of 1 s, which suits a lab that runs on low-latency links.
    """

    ALPHA = 1 / 8
    BETA = 1 / 4
    K = 4

    def __init__(self, initial: float = 1.0, min_rto: float = 0.2, max_rto: float = 60.0,
                 granularity: float = 0.001):
        self.min_rto = min_rto
        self.max_rto = max_rto
        self.granularity = granularity
        self.srtt: float | None = None
        self.rttvar: float | None = None
        self._base = initial
        self._backoff = 1

    @property
    def rto(self) -> float:
        return min(self.max_rto, max(self.min_rto, self._base) * self._backoff)

    def sample(self, rtt: float) -> None:
        """Feed one RTT measurement (only from segments that were never retransmitted)."""
        if self.srtt is None:
            self.srtt = rtt
            self.rttvar = rtt / 2
        else:
            self.rttvar = (1 - self.BETA) * self.rttvar + self.BETA * abs(self.srtt - rtt)
            self.srtt = (1 - self.ALPHA) * self.srtt + self.ALPHA * rtt
        self._base = self.srtt + max(self.granularity, self.K * self.rttvar)
        self._backoff = 1

    def seed(self, srtt: float, rttvar: float) -> None:
        """Start from RTT statistics cached from an earlier connection (RFC 2140)."""
        self.srtt, self.rttvar = srtt, rttvar
        self._base = srtt + max(self.granularity, self.K * rttvar)
        self._backoff = 1

    def backoff(self) -> None:
        """Double the timeout after an expiry (RFC 6298 section 5.5)."""
        if self.rto < self.max_rto:
            self._backoff *= 2

    def reset_backoff(self) -> None:
        self._backoff = 1


class FixedRTO:
    """Constant timeout with no backoff; the baseline the adaptive estimator is measured against."""

    def __init__(self, timeout: float = 1.0):
        self.rto = timeout
        self.srtt = None

    def sample(self, rtt: float) -> None:
        pass

    def backoff(self) -> None:
        pass

    def reset_backoff(self) -> None:
        pass
