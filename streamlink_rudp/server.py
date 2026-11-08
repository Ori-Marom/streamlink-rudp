"""Media server: serves a prepared media directory over RUDP and over TCP.

RUDP: clients send ``{"op": "GET", "path": ..., "conn": <id>}`` to the control port.
Each accepted request gets its own *ephemeral UDP socket* and sender thread (the same
idea TFTP uses), so any number of clients and transfers run concurrently without
their packets ever sharing a socket. Retransmitted requests for a transfer already in
progress are recognised by ``(client address, conn id)`` and ignored.

TCP: the same JSON request on one line; the reply is a JSON header line followed by
the object bytes. TCP is the reference transport for comparisons.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path

from .netem import NetemConfig, NetworkEmulator
from .transport import PathState, Sender, SenderConfig, TransferError

log = logging.getLogger(__name__)

DEFAULT_UDP_PORT = 8888
DEFAULT_TCP_PORT = 9999
MAX_REQUEST = 2048


class RequestError(Exception):
    pass


class MediaServer:
    def __init__(self, root: Path, host: str = "127.0.0.1", udp_port: int = DEFAULT_UDP_PORT,
                 tcp_port: int | None = DEFAULT_TCP_PORT, netem: NetemConfig | None = None,
                 sender_config: SenderConfig | None = None, max_sessions: int = 64,
                 telemetry=None):
        self.root = Path(root).resolve()
        self.host = host
        self.sender_config = sender_config or SenderConfig()
        self.emulator = NetworkEmulator(netem)
        self.max_sessions = max_sessions
        self.telemetry = telemetry
        self._sessions: dict[tuple, threading.Thread] = {}
        self._paths: dict[str, PathState] = {}  # per-client RTT/ssthresh cache (RFC 2140)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.transfers_completed = 0
        self.transfers_failed = 0

        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((host, udp_port))
        self.tcp = None
        if tcp_port is not None:
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind((host, tcp_port))
            self.tcp.listen(32)
        self._threads: list[threading.Thread] = []

    @property
    def udp_port(self) -> int:
        return self.udp.getsockname()[1]

    @property
    def tcp_port(self) -> int | None:
        return self.tcp.getsockname()[1] if self.tcp else None

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> "MediaServer":
        self._threads = [threading.Thread(target=self._udp_loop, name="rudp-control", daemon=True)]
        if self.tcp:
            self._threads.append(threading.Thread(target=self._tcp_loop, name="tcp-accept", daemon=True))
        for t in self._threads:
            t.start()
        log.info("serving %s  (RUDP udp/%d%s)", self.root, self.udp_port,
                 f", TCP tcp/{self.tcp_port}" if self.tcp else "")
        return self

    def serve_forever(self) -> None:
        self.start()
        try:
            self._stop.wait()  # sleeps; no busy loop
        except KeyboardInterrupt:
            log.info("shutting down")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for s in (self.udp, self.tcp):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self.emulator.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- request handling --------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        """Map a request path to a file inside the media root, rejecting traversal."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise RequestError("invalid path")
        target = (self.root / path).resolve()
        if target != self.root and self.root not in target.parents:
            raise RequestError("path outside media root")
        if not target.is_file():
            raise RequestError("not found")
        return target

    @staticmethod
    def parse_request(raw: bytes) -> dict:
        try:
            req = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise RequestError("malformed request") from None
        if not isinstance(req, dict) or req.get("op") != "GET":
            raise RequestError("unsupported operation")
        return req

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, addr = self.udp.recvfrom(MAX_REQUEST)
            except OSError:
                return
            conn = None
            try:
                req = self.parse_request(raw)
                conn = req.get("conn")
                if not isinstance(conn, int) or not 0 <= conn < 2 ** 32:
                    raise RequestError("invalid connection id")
                key = (addr, conn)
                with self._lock:
                    if key in self._sessions:
                        continue  # retransmitted request; the session's SYN retries will answer
                    if len(self._sessions) >= self.max_sessions:
                        raise RequestError("server busy")
                    data = self.resolve(req.get("path")).read_bytes()
                    t = threading.Thread(target=self._session, args=(key, req["path"], data),
                                         name=f"rudp-{conn:08x}", daemon=True)
                    self._sessions[key] = t
                t.start()
            except (RequestError, OSError) as exc:
                log.warning("rejected request from %s:%d: %s", *addr, exc)
                reply = json.dumps({"conn": conn, "error": str(exc)}).encode()
                try:
                    self.udp.sendto(reply, addr)
                except OSError:
                    pass

    def _session(self, key, path: str, data: bytes) -> None:
        addr, conn = key
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, 0))
            sender = Sender(self.emulator.wrap(sock), addr, conn, data, self.sender_config,
                            path=self._paths.get(addr[0]), telemetry=self.telemetry)
            stats = sender.run()
            state = sender.path_state()
            if state is not None:
                self._paths[addr[0]] = state
            self.transfers_completed += 1
            log.info("sent %s to %s:%d  %.1f KB in %.2fs  retx=%d timeouts=%d", path, *addr,
                     len(data) / 1024, stats.duration, stats.retransmissions, stats.timeouts)
        except TransferError as exc:
            self.transfers_failed += 1
            log.warning("transfer of %s to %s:%d failed: %s", path, *addr, exc)
        finally:
            sock.close()
            with self._lock:
                self._sessions.pop(key, None)

    def _tcp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, addr = self.tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._tcp_client, args=(client, addr), daemon=True).start()

    def _tcp_client(self, client: socket.socket, addr) -> None:
        with client:
            client.settimeout(30)
            try:
                buf = b""
                while b"\n" not in buf and len(buf) < MAX_REQUEST:
                    chunk = client.recv(MAX_REQUEST)
                    if not chunk:
                        return
                    buf += chunk
                try:
                    req = self.parse_request(buf.split(b"\n", 1)[0])
                    data = self.resolve(req.get("path")).read_bytes()
                except RequestError as exc:
                    client.sendall(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
                    return
                client.sendall(json.dumps({"ok": True, "size": len(data)}).encode() + b"\n")
                client.sendall(data)
            except OSError as exc:
                log.debug("TCP client %s:%d: %s", *addr, exc)
