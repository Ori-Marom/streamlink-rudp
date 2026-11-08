# Validation

How the project is checked, and what each check proves.

## Reproduce

```sh
python -m unittest discover -s tests -v       # 47 tests, about 30 s
python -m streamlink_rudp demo --quick         # end-to-end smoke run
python -m streamlink_rudp bench                # experiments + charts (needs matplotlib)
```

Video tests need `ffmpeg` and `ffprobe` on the PATH; without them they are skipped
and everything else still runs. CI runs the full suite on Linux with Python
3.10 to 3.13 (with ffmpeg, plus the quick demo and an `ffprobe` check of its
output), and the transport and system tests on Windows and macOS.

## Test suite

| Area | File | What is checked |
|---|---|---|
| Wire format | `test_units.py` | Header round trip; **every single bit flip** in a packet is rejected; truncated and length-mismatched packets are rejected; checksum matches the RFC 1071 example; SACK/SYN payloads; 32-bit sequence wrap-around |
| RTO | `test_units.py` | RFC 6298 formulas for the first and later samples, backoff doubling and cap, minimum RTO |
| Congestion control | `test_units.py` | Slow start, congestion avoidance growth, one window reduction per recovery, partial vs full ACK, timeout |
| ABR + buffer | `test_units.py` | Starts low, steps up one level at a time, holds with a low buffer, panic drop, harmonic mean resists outliers, stall counting |
| Transport | `test_transport.py` | Object sizes 0 B to 250 KB including MSS boundaries; 10% loss in both directions; reordering + duplication + corruption together; 60% loss on the handshake; Go-Back-N correctness; recovery from bottleneck queue drops; SACK retransmits less than Go-Back-N; packets for another connection ID ignored; server error replies; unreachable server and silent receiver time out; emulator determinism |
| Server | `test_system.py` | RUDP and TCP fetches through a lossy link; six concurrent clients; hostname resolution; path traversal (`../`, absolute paths) and missing files rejected on both transports |
| Discovery | `test_system.py` | DHCP DORA, NAK, pool exhaustion and release; live DHCP, DNS and DNS-over-HTTP lookups |
| Dashboard | `test_dashboard.py` | Page, info and state API; segments stream and are browser-ready; network presets and manual values; quality pinning; auto scenario; Server-Sent Events feed; unknown routes. Remux regressions found in Chrome: the AAC configuration must be in the MP4 header, and fragments must keep a continuous timeline with no edit list, also across renditions |
| Video | `test_system.py` | `prepare` produces renditions with equal segment counts and correct hashes; a full adaptive stream over 5% loss decodes with **zero ffmpeg errors**, has audio and video, and the right duration; segments alternating between renditions decode cleanly |

The suite was run repeatedly (more than ten consecutive passes) to check for
timing-dependent flakiness; the one flaky case found, a sender left
retransmitting after the receiver had exited under 60% loss, was fixed with the
TIME_WAIT-style linger described in the protocol spec.

## Browser check

The dashboard was verified in headless Chrome (Playwright): with the auto scenario
cycling through all five network presets for 72 s, the video played continuously
(no 2-second interval without progress), the browser decoded all three
resolutions (640, 1280 and 1920 pixels wide), and the page logged no errors, in
light and dark mode and at phone width.

## Benchmark results

From `docs/images/results.json` (Linux, Python 3.13, loopback with the user-space emulator):

| Experiment | Result |
|---|---|
| 8 Mbps bottleneck, 40 ms RTT | 7.6 Mbps goodput (95% utilization), 3 fast recoveries, 0 timeouts |
| 1% loss each way, 40 ms RTT | SACK 4.40 Mbps vs Go-Back-N 1.79 Mbps; retransmissions 1.0% vs 31.3% |
| 10% loss each way, 40 ms RTT | SACK 0.64 Mbps vs Go-Back-N 0.16 Mbps |
| ABR, 7.6 → 1.3 → 7.6 Mbps | 6 switches, 0 stalls, 0.31 s startup |

## Bugs fixed relative to the first version

| Problem in v1 | Fix |
|---|---|
| A dropped SYN or FIN (the sender's own 5% artificial loss applied to them) aborted the transfer | SYN, FIN and GET are retransmitted with RTO backoff |
| Sequence numbers restarted at 0 for every file and there was no connection ID, so a late packet could corrupt the next file | Random ISN + 32-bit connection ID + one socket per transfer |
| All clients shared one UDP socket: a second client's request was read as an ACK | Each transfer gets its own socket and thread |
| `while True: pass` kept a CPU core at 100% | Server waits on an event |
| No timeout on the UDP manifest request: a lost reply hung the client forever | The manifest is a normal reliable transfer |
| A timed-out transfer silently left a truncated file | Size + SHA-256 verification per segment |
| ACK checksums were not verified by the server | Every packet is verified at decode time |
| Fixed 2 s timeout, Go-Back-N retransmission, no exit from fast recovery | RFC 6298 RTO, SACK, NewReno |
