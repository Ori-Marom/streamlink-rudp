"""Service discovery: a DHCP-style address allocator and a DNS resolver (UDP + DNS-over-HTTP).

These are deliberately small text protocols that model the *flow* of DHCP (DORA:
Discover, Offer, Request, Ack) and DNS; they do not configure real interfaces.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

log = logging.getLogger(__name__)

DEFAULT_DHCP_PORT = 6767   # unprivileged stand-in for UDP 67
DEFAULT_DNS_PORT = 8053
DEFAULT_DOH_PORT = 8054
APP_DOMAIN = "app.server.local"


class _UDPService:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def _bind(self) -> socket.socket:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.port = self.sock.getsockname()[1]
        return self.sock

    def start(self):
        """Serve in a background thread (used by the demo and the tests)."""
        self._bind()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._bind()
        try:
            self._loop()
        except KeyboardInterrupt:
            pass

    def stop(self) -> None:
        if self.sock:
            self.sock.close()

    def _loop(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                return
            try:
                self.handle(self.sock, addr, data.decode("utf-8", errors="replace").strip())
            except Exception:  # never let one bad datagram kill the service
                log.exception("error handling %r from %s", data[:64], addr)

    def handle(self, sock, addr, message: str) -> None:
        raise NotImplementedError


class DHCPServer(_UDPService):
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_DHCP_PORT,
                 pool: list[str] | None = None):
        super().__init__(host, port)
        self.ip_pool = list(pool or [f"192.168.1.{i}" for i in range(100, 150)])
        self.allocated_ips: dict = {}

    def handle(self, sock, addr, message: str) -> None:
        if message == "DHCP_DISCOVER":
            self.handle_discover(sock, addr)
        elif message.startswith("DHCP_REQUEST:"):
            self.handle_request(sock, addr, message.split(":", 1)[1])
        elif message.startswith("DHCP_RELEASE:"):
            ip = message.split(":", 1)[1]
            if self.allocated_ips.get(addr) == ip:
                del self.allocated_ips[addr]
                self.ip_pool.append(ip)

    def handle_discover(self, sock, addr) -> None:
        offered = self.allocated_ips.get(addr) or (self.ip_pool.pop(0) if self.ip_pool else None)
        if offered is None:
            sock.sendto(b"DHCP_OFFER:NONE", addr)
            log.warning("address pool exhausted")
            return
        self.allocated_ips[addr] = offered
        sock.sendto(f"DHCP_OFFER:{offered}".encode(), addr)
        log.info("offered %s to %s:%d", offered, *addr)

    def handle_request(self, sock, addr, requested: str) -> None:
        if self.allocated_ips.get(addr) == requested:
            sock.sendto(f"DHCP_ACK:{requested}".encode(), addr)
        else:
            sock.sendto(f"DHCP_NAK:{requested}".encode(), addr)


class DNSServer(_UDPService):
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_DNS_PORT,
                 records: dict[str, str] | None = None, enable_doh: bool = True,
                 doh_port: int = DEFAULT_DOH_PORT):
        super().__init__(host, port)
        self.dns_records = dict(records or {APP_DOMAIN: "127.0.0.1"})
        self.enable_doh = enable_doh
        self.doh_port = doh_port
        self._http: ThreadingHTTPServer | None = None

    def _resolve(self, name: str):
        ip = self.dns_records.get(name.strip().lower().rstrip("."))
        return (ip, f"DNS_RESPONSE:{ip}") if ip else (None, "DNS_RESPONSE:NOT_FOUND")

    def handle(self, sock, addr, message: str) -> None:
        sock.sendto(self._resolve(message)[1].encode(), addr)

    def _doh_handler(self, path: str, query: str):
        if path != "/dns-query":
            return 404, json.dumps({"Status": 2, "Comment": "unknown path"})
        name = parse_qs(query).get("name", [""])[0].strip()
        if not name:
            return 400, json.dumps({"Status": 2, "Comment": "missing name"})
        ip, _ = self._resolve(name)
        if ip:
            return 200, json.dumps({"Status": 0, "Answer": [{"name": name, "type": 1, "data": ip}]})
        return 200, json.dumps({"Status": 3, "Comment": "NXDOMAIN"})

    def _start_doh(self) -> None:
        resolver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                status, body = resolver._doh_handler(parsed.path, parsed.query)
                self.send_response(status)
                self.send_header("Content-Type", "application/dns-json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self._http = ThreadingHTTPServer((self.host, self.doh_port), Handler)
        self.doh_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def start(self):
        if self.enable_doh:
            self._start_doh()
        return super().start()

    def serve_forever(self) -> None:
        if self.enable_doh:
            self._start_doh()
        super().serve_forever()

    def stop(self) -> None:
        super().stop()
        if self._http:
            self._http.shutdown()


# -- client side --------------------------------------------------------------------------------
def _exchange(addr, message: str, timeout: float, sock=None) -> str:
    own = sock is None
    sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        for _ in range(3):  # UDP: retry a lost request
            sock.sendto(message.encode(), addr)
            try:
                return sock.recvfrom(1024)[0].decode()
            except socket.timeout:
                continue
        raise TimeoutError(f"no reply from {addr[0]}:{addr[1]}")
    finally:
        if own:
            sock.close()


def dhcp_lease(server=("127.0.0.1", DEFAULT_DHCP_PORT), timeout: float = 1.0) -> str:
    """Run the DORA exchange and return the leased address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        offer = _exchange(server, "DHCP_DISCOVER", timeout, sock)
        if not offer.startswith("DHCP_OFFER:") or offer.endswith(":NONE"):
            raise RuntimeError(f"no address offered ({offer})")
        ip = offer.split(":", 1)[1]
        ack = _exchange(server, f"DHCP_REQUEST:{ip}", timeout, sock)
        if ack != f"DHCP_ACK:{ip}":
            raise RuntimeError(f"lease refused ({ack})")
        return ip


def dns_lookup(name: str, server=("127.0.0.1", DEFAULT_DNS_PORT), timeout: float = 1.0) -> str:
    reply = _exchange(server, name, timeout)
    if not reply.startswith("DNS_RESPONSE:") or reply.endswith("NOT_FOUND"):
        raise LookupError(f"{name}: not found")
    return reply.split(":", 1)[1]


def doh_lookup(name: str, url: str = f"http://127.0.0.1:{DEFAULT_DOH_PORT}/dns-query",
               timeout: float = 2.0) -> str:
    try:
        with urlopen(f"{url}?name={name}", timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (URLError, ValueError) as exc:
        raise LookupError(f"DoH lookup failed: {exc}") from None
    if data.get("Status") != 0 or not data.get("Answer"):
        raise LookupError(f"{name}: not found")
    return data["Answer"][0]["data"]
