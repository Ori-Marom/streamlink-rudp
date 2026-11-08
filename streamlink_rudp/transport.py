"""Reliable, congestion-controlled byte transfer over UDP.

One transfer moves one object (a manifest or a media segment) from a :class:`Sender`
to a receiver started with :func:`receive`:

    client                                   server (fresh UDP socket per transfer)
      | -- GET {path, conn} ---------------> | (control port; retried until SYN arrives)
      | <------------- SYN(isn, size, mss) -- | (retransmitted with RTO backoff)
      | -- SYN|ACK(window) ----------------> |
      | <-------------------- DATA seq=isn+1  |
      | -- ACK(cum, window, SACK blocks) --> |  ... sliding window, SACK, NewReno ...
      | <------------------------- FIN ----- |
      | -- ACK ----------------------------> |

Loss recovery combines a retransmission timer (RFC 6298) with SACK-based fast
retransmit (in the spirit of RFC 6675). A Go-Back-N mode without SACK and with a fixed
timeout is kept as a baseline for benchmarks.
"""
from __future__ import annotations

import heapq
import json
import logging
import math
import random
import socket
import time
from dataclasses import dataclass, field

from . import packet as pk
from .congestion import NewReno
from .rtt import FixedRTO, RTOEstimator

log = logging.getLogger(__name__)

DEFAULT_MSS = 1200  # payload bytes per segment; keeps datagrams under a 1500-byte MTU
DUP_THRESH = 3


class TransferError(RuntimeError):
    """The transfer could not be completed (peer unreachable, protocol violation...)."""


class RemoteError(TransferError):
    """The server rejected the request."""


@dataclass
class SenderConfig:
    mss: int = DEFAULT_MSS
    sack: bool = True            # False = Go-Back-N: on loss resend everything outstanding
    adaptive_rto: bool = True    # False = fixed timeout, no backoff
    fixed_rto: float = 1.0
    initial_cwnd: float = 2.0
    control_retries: int = 8     # SYN / FIN attempts
    max_timeouts: int = 12       # consecutive data timeouts before giving up
    trace: bool = False          # record cwnd over time (for charts)


@dataclass
class PathState:
    """What a sender learned about the path, reusable by the next transfer (RFC 2140)."""
    srtt: float
    rttvar: float
    ssthresh: float


@dataclass
class SenderStats:
    bytes: int = 0
    segments: int = 0
    packets_sent: int = 0
    retransmissions: int = 0
    timeouts: int = 0
    fast_recoveries: int = 0
    duration: float = 0.0
    srtt: float | None = None
    trace: list[tuple[float, float, float, int, float]] = field(default_factory=list)
    events: list[tuple[float, str]] = field(default_factory=list)

    @property
    def goodput_bps(self) -> float:
        return self.bytes * 8 / self.duration if self.duration > 0 else 0.0

    @property
    def retransmission_ratio(self) -> float:
        return self.retransmissions / self.segments if self.segments else 0.0


@dataclass
class ReceiveStats:
    bytes: int = 0
    duration: float = 0.0
    requests_sent: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    acks_sent: int = 0

    @property
    def throughput_bps(self) -> float:
        return self.bytes * 8 / self.duration if self.duration > 0 else 0.0


def _recv(sock, timeout: float):
    """recvfrom with a timeout; returns (None, None) on timeout or ICMP-induced resets."""
    sock.settimeout(max(timeout, 0.0005))
    try:
        return sock.recvfrom(65535)
    except (socket.timeout, ConnectionResetError):
        return None, None


def _decode(raw: bytes, conn_id: int):
    try:
        p = pk.Packet.decode(raw)
    except pk.PacketError:
        return None
    return p if p.conn_id == conn_id else None


class Sender:
    """Sends one byte string to a receiver that has requested it."""

    def __init__(self, sock, peer, conn_id: int, data: bytes, config: SenderConfig | None = None,
                 rng: random.Random | None = None, path: PathState | None = None):
        self.sock = sock
        self.peer = peer
        self.conn_id = conn_id
        self.data = data
        self.cfg = config or SenderConfig()
        self.isn = (rng or random.SystemRandom()).getrandbits(32)
        self.rto = RTOEstimator() if self.cfg.adaptive_rto else FixedRTO(self.cfg.fixed_rto)
        self.cc = NewReno(initial_cwnd=self.cfg.initial_cwnd)
        if path is not None:
            if self.cfg.adaptive_rto:
                self.rto.seed(path.srtt, path.rttvar)
            self.cc.ssthresh = max(path.ssthresh, 2.0)
        self.stats = SenderStats(bytes=len(data))
        self._t0 = 0.0

    def path_state(self) -> PathState | None:
        if self.rto.srtt is None:
            return None
        return PathState(self.rto.srtt, self.rto.rttvar or self.rto.srtt / 2,
                         max(self.cc.ssthresh if self.cc.ssthresh < 64 else self.cc.cwnd, 2.0))

    # -- helpers ---------------------------------------------------------------------
    def _send(self, flags: int, seq: int, payload: bytes = b"") -> None:
        p = pk.Packet(self.conn_id, seq=seq, flags=flags, payload=payload)
        self.sock.sendto(p.encode(), self.peer)
        self.stats.packets_sent += 1

    def _wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw, addr = _recv(self.sock, remaining)
            if raw is None:
                return None
            if addr != self.peer:
                continue
            p = _decode(raw, self.conn_id)
            if p is None:
                continue
            if p.has(pk.RST):
                raise TransferError("peer reset the transfer")
            if predicate(p):
                return p

    def _event(self, kind: str) -> None:
        self.stats.events.append((time.monotonic() - self._t0, kind))

    # -- phases ----------------------------------------------------------------------
    def run(self) -> SenderStats:
        self._t0 = time.monotonic()
        rwnd = self._handshake()
        self._transfer(rwnd)
        self._finish()
        self.stats.duration = time.monotonic() - self._t0
        self.stats.srtt = self.rto.srtt
        return self.stats

    def _handshake(self) -> int:
        syn = pk.encode_syn(len(self.data), self.cfg.mss)
        expected = pk.seq_add(self.isn, 1)
        for attempt in range(self.cfg.control_retries):
            sent = time.monotonic()
            self._send(pk.SYN, self.isn, syn)
            reply = self._wait_for(lambda p: p.has(pk.SYN) and p.has(pk.ACK) and p.ack == expected,
                                   self.rto.rto)
            if reply is not None:
                if attempt == 0:
                    self.rto.sample(time.monotonic() - sent)
                self.rto.reset_backoff()
                return max(1, reply.window)
            self.rto.backoff()
            log.debug("SYN timeout (attempt %d), RTO now %.3fs", attempt + 1, self.rto.rto)
        raise TransferError("handshake failed: no SYN-ACK from receiver")

    def _finish(self) -> None:
        n = self.stats.segments
        fin_seq = pk.seq_add(self.isn, 1 + n)
        expected = pk.seq_add(fin_seq, 1)
        for _ in range(self.cfg.control_retries // 2):
            self._send(pk.FIN, fin_seq)
            if self._wait_for(lambda p: p.has(pk.ACK) and p.ack == expected, self.rto.rto):
                return
            self.rto.backoff()
        # Every data segment was already acknowledged, so the transfer itself succeeded.
        log.debug("FIN not acknowledged; closing anyway")

    def _transfer(self, rwnd: int) -> None:
        cfg, cc, rto, stats = self.cfg, self.cc, self.rto, self.stats
        mss = cfg.mss
        n = math.ceil(len(self.data) / mss)
        stats.segments = n
        if n == 0:
            return

        sent_at = [0.0] * n
        retx = [0] * n
        sacked = bytearray(n)
        inflight = bytearray(n)
        lost = bytearray(n)
        lost_queue: list[int] = []
        una = nxt = pipe = 0
        top_sacked = 0          # one past the highest SACKed segment
        dupacks = 0
        timer = None            # start time of the retransmission timer
        consecutive_timeouts = 0
        base_seq = pk.seq_add(self.isn, 1)
        last_trace = -1.0

        def trace(now: float) -> None:
            nonlocal last_trace
            if cfg.trace and now - last_trace >= 0.002:
                last_trace = now
                stats.trace.append((now - self._t0, cc.cwnd, cc.ssthresh, pipe, rto.rto))

        def mark_lost(i: int) -> None:
            nonlocal pipe
            if inflight[i]:
                inflight[i] = 0
                pipe -= 1
            if not lost[i]:
                lost[i] = 1
                heapq.heappush(lost_queue, i)

        def send_segment(i: int, now: float) -> None:
            nonlocal pipe, timer
            self._send(pk.DATA, pk.seq_add(base_seq, i), self.data[i * mss:(i + 1) * mss])
            if sent_at[i]:
                retx[i] += 1
                stats.retransmissions += 1
            sent_at[i] = now
            lost[i] = 0
            inflight[i] = 1
            pipe += 1
            if timer is None:
                timer = now

        while una < n:
            now = time.monotonic()

            # 1. Transmit while the window allows: retransmissions first, then new data.
            window = max(1, int(min(cc.cwnd, rwnd)))
            while pipe < window:
                while lost_queue and (lost_queue[0] < una or sacked[lost_queue[0]]
                                      or not lost[lost_queue[0]]):
                    heapq.heappop(lost_queue)
                if lost_queue:
                    send_segment(heapq.heappop(lost_queue), now)
                elif nxt < n:
                    send_segment(nxt, now)
                    nxt += 1
                else:
                    break

            # 2. Wait for an ACK or for the retransmission timer to expire.
            deadline = (timer or now) + rto.rto
            raw, addr = _recv(self.sock, deadline - time.monotonic())
            now = time.monotonic()

            if raw is None or addr != self.peer:
                if timer is not None and now >= timer + rto.rto:
                    # Retransmission timeout: collapse the window and resend what is missing.
                    consecutive_timeouts += 1
                    stats.timeouts += 1
                    if consecutive_timeouts > cfg.max_timeouts:
                        raise TransferError("receiver stopped responding")
                    cc.on_timeout(nxt - una)
                    rto.backoff()
                    for i in range(una, nxt):
                        if not sacked[i]:
                            mark_lost(i)
                    timer = None
                    dupacks = 0
                    self._event("timeout")
                    trace(now)
                continue

            p = _decode(raw, self.conn_id)
            if p is None or not p.has(pk.ACK) or p.has(pk.SYN):
                continue
            if p.has(pk.RST):
                raise TransferError("peer reset the transfer")
            rwnd = max(1, p.window)
            cum = pk.seq_diff(p.ack, base_seq)
            if cum > n:
                continue  # acknowledges data never sent: ignore

            newly_sacked = 0
            rtt_candidate = None
            if cfg.sack:
                for start, end in pk.decode_sack(p.payload):
                    s, e = pk.seq_diff(start, base_seq), pk.seq_diff(end, base_seq)
                    if not (una <= s < e <= nxt):
                        continue
                    for i in range(s, e):
                        if not sacked[i]:
                            sacked[i] = 1
                            newly_sacked += 1
                            if inflight[i]:
                                inflight[i] = 0
                                pipe -= 1
                            if retx[i] == 0:
                                rtt_candidate = sent_at[i]
                    top_sacked = max(top_sacked, e)

            if cum > una:
                if retx[cum - 1] == 0:  # Karn's rule: never sample retransmitted segments
                    rtt_candidate = sent_at[cum - 1]
                for i in range(una, cum):
                    if inflight[i]:
                        inflight[i] = 0
                        pipe -= 1
                newly_acked = cum - una
                una = cum
                dupacks = 0
                consecutive_timeouts = 0
                if rtt_candidate:
                    rto.sample(now - rtt_candidate)
                rto.reset_backoff()
                timer = now if una < nxt else None
                cc.on_ack(newly_acked + newly_sacked, una)
            else:
                if rtt_candidate:
                    rto.sample(now - rtt_candidate)
                if newly_sacked and not cc.in_recovery:
                    cc.on_ack(newly_sacked, una)
                if pipe > 0 or nxt > una:
                    dupacks += 1

            # 3. Loss detection.
            newly_lost = False
            if cfg.sack:
                count = 0
                reorder_age = 1.25 * (rto.srtt or rto.rto)
                for i in range(min(top_sacked, nxt) - 1, una - 1, -1):
                    if sacked[i]:
                        count += 1
                    elif count >= DUP_THRESH and inflight[i] and (
                            retx[i] == 0 or now - sent_at[i] > reorder_age):
                        mark_lost(i)
                        newly_lost = True
                if dupacks >= DUP_THRESH and una < nxt and inflight[una] and retx[una] == 0:
                    mark_lost(una)
                    newly_lost = True
            elif dupacks == DUP_THRESH:
                for i in range(una, nxt):  # Go-Back-N: resend the whole outstanding window
                    mark_lost(i)
                newly_lost = True

            if newly_lost and cc.on_loss(nxt - una, nxt):
                stats.fast_recoveries += 1
                self._event("fast-retransmit")
            trace(now)


def receive(sock, server_addr, request: bytes, conn_id: int, *, request_timeout: float = 1.0,
            max_requests: int = 6, idle_timeout: float = 10.0, buffer_segments: int = 1024,
            linger: float = 2.0) -> tuple[bytes, ReceiveStats]:
    """Send ``request`` to the server's control address and receive the reply object."""
    stats = ReceiveStats()
    t0 = time.monotonic()

    # -- request + handshake -----------------------------------------------------------
    peer = syn = None
    wait = request_timeout
    for _ in range(max_requests):
        sock.sendto(request, server_addr)
        stats.requests_sent += 1
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            raw, addr = _recv(sock, deadline - time.monotonic())
            if raw is None:
                break
            if addr == server_addr and raw[:1] == b"{":
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("conn") == conn_id and "error" in message:
                    raise RemoteError(message["error"])
                continue
            if addr[0] != server_addr[0]:
                continue
            p = _decode(raw, conn_id)
            if p is not None and p.has(pk.SYN) and not p.has(pk.ACK):
                peer, syn = addr, p
                break
        if syn is not None:
            break
        wait = min(wait * 2, 8.0)
    if syn is None:
        raise TransferError(f"no response from server {server_addr[0]}:{server_addr[1]}")

    try:
        total, mss = pk.decode_syn(syn.payload)
    except pk.PacketError as exc:
        raise TransferError(str(exc)) from None
    if mss <= 0:
        raise TransferError("invalid MSS in SYN")
    isn = syn.seq
    base_seq = pk.seq_add(isn, 1)
    fin_seq = None

    def send_ack(ack: int, flags: int = pk.ACK, payload: bytes = b"", window: int = buffer_segments):
        sock.sendto(pk.Packet(conn_id, ack=ack, flags=flags, window=window,
                              payload=payload).encode(), peer)
        stats.acks_sent += 1

    send_ack(base_seq, pk.SYN | pk.ACK)

    # -- data ------------------------------------------------------------------------------
    expected_segments = math.ceil(total / mss)
    chunks: list[bytes | None] = [None] * expected_segments
    cum = 0
    stored_ahead = 0
    highest = 0
    complete_at = time.monotonic() if expected_segments == 0 else None
    last_heard = time.monotonic()

    while True:
        if complete_at is not None:
            # TIME_WAIT-like: keep answering retransmissions until the sender goes quiet.
            timeout = linger - (time.monotonic() - max(complete_at, last_heard))
            if timeout <= 0:
                break
        else:
            timeout = idle_timeout - (time.monotonic() - last_heard)
            if timeout <= 0:
                raise TransferError("transfer stalled: no data from server")
        raw, addr = _recv(sock, timeout)
        if raw is None or addr != peer:
            continue
        p = _decode(raw, conn_id)
        if p is None:
            continue
        last_heard = time.monotonic()

        if p.has(pk.SYN):
            send_ack(base_seq, pk.SYN | pk.ACK)  # our SYN-ACK was lost
            continue
        if p.has(pk.FIN):
            if cum == expected_segments:
                send_ack(pk.seq_add(p.seq, 1))
                break
            fin_seq = p.seq  # FIN overtook data: remember it, the sender will retry
            continue
        if not p.has(pk.DATA):
            continue

        idx = pk.seq_diff(p.seq, base_seq)
        if idx < expected_segments and idx - cum < buffer_segments:
            if chunks[idx] is None:
                chunks[idx] = p.payload
                if idx == cum:
                    while cum < expected_segments and chunks[cum] is not None:
                        cum += 1
                    stored_ahead = sum(1 for i in range(cum, highest) if chunks[i] is not None)
                else:
                    stats.out_of_order += 1
                    stored_ahead += 1
                highest = max(highest, idx + 1)
            else:
                stats.duplicates += 1

        # Build SACK blocks; the block holding the newest segment goes first (RFC 2018).
        blocks: list[tuple[int, int]] = []
        if highest > cum:
            i = cum
            while i < highest:
                if chunks[i] is not None:
                    j = i
                    while j < highest and chunks[j] is not None:
                        j += 1
                    blocks.append((i, j))
                    i = j
                else:
                    i += 1
            blocks.sort(key=lambda b: not (b[0] <= idx < b[1]))
        sack = pk.encode_sack([(pk.seq_add(base_seq, s), pk.seq_add(base_seq, e)) for s, e in blocks])
        send_ack(pk.seq_add(base_seq, cum), payload=sack, window=max(1, buffer_segments - stored_ahead))

        if cum == expected_segments and complete_at is None:
            complete_at = time.monotonic()
            stats.duration = complete_at - t0
            if fin_seq is not None:
                send_ack(pk.seq_add(fin_seq, 1))
                break

    data = b"".join(c for c in chunks if c is not None)
    if len(data) != total:
        raise TransferError(f"incomplete transfer: {len(data)} of {total} bytes")
    stats.bytes = total
    if not stats.duration:
        stats.duration = time.monotonic() - t0
    return data, stats
