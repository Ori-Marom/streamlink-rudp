# Validation

## Local results

Validated on Windows with Python **3.13.5**:

- **7 regression tests passed** with `python -m unittest discover -s tests -v`.
- **Local end-to-end smoke check passed**: DHCP discovery and acknowledgement, UDP DNS
  lookup, three TCP downloads, and three RUDP downloads.
- Each downloaded file matched its **5,888-byte source payload exactly**. This spans
  five RUDP data packets per file with the current 1,400-byte maximum payload.

## Reproduce

From the repository root:

```sh
python -m unittest discover -s tests -v
python tests/smoke.py
```

The smoke harness starts isolated child processes, selects temporary unprivileged ports,
uses a temporary asset/download directory, and terminates its own child services afterward.
It requires local socket access. It uses binary fixture content rather than actual JPEG
images because its assertion concerns transport integrity.

Simulated packet loss and delay are disabled in the smoke server. This confirms a stable
baseline, not resilience under arbitrary loss, latency, concurrency, or malicious input.
The optional HTTP resolver is covered at the handler level by the unit tests; live HTTP/TLS
behavior is not part of this smoke run. No throughput benchmark or video-playback claim is made.

## Preparation fixes

The supplied architecture and filenames were preserved. Three focused corrections were
made before publishing:

1. `RUDPHeader.unpack()` now maps checksum and payload-length fields explicitly. The
   original positional constructor call swapped them, breaking checksum verification.
2. The alternate `rudp_packet.py` wrapper now includes the payload when packing a checksum.
3. Its string representation now uses the existing `header.seq` attribute.

The regression suite covers these corrections. The original uploaded source files on the
author's computer were not changed; fixes apply to the prepared repository copy.
