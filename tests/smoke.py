import contextlib
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
from client import Client

def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

udp_port = free_port(socket.SOCK_DGRAM)
tcp_port = free_port(socket.SOCK_STREAM)
dhcp_port = free_port(socket.SOCK_DGRAM)
dns_port = free_port(socket.SOCK_DGRAM)
children = []
env = dict(os.environ, PYTHONPATH=str(project), PYTHONIOENCODING='utf-8')
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    payload = bytes(range(256)) * 23
    for quality in ('low', 'medium', 'high'):
        folder = base / 'assets' / quality
        folder.mkdir(parents=True)
        for name in ('frame1.jpg', 'frame2.jpg', 'frame3.jpg'):
            (folder / name).write_bytes(payload)
    commands = [
        f'from dhcp_server import DHCPServer; DHCPServer(port={dhcp_port}).start()',
        f'from dns_server import DNSServer; DNSServer(port={dns_port}, enable_doh=False).start()',
        f'import app_server as a; import RUDP_connection as r; r.SIMULATE_LATENCY=False; r.random.random=lambda:1.0; a.UDP_PORT={udp_port}; a.TCP_PORT={tcp_port}; a.start_server()',
    ]
    try:
        for command in commands:
            children.append(subprocess.Popen([sys.executable, '-u', '-c', command], cwd=temp,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(('127.0.0.1', tcp_port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError('Application server did not start')
                time.sleep(.05)
        client = Client()
        client.download_dir = str(base / 'downloads')
        Path(client.download_dir).mkdir()
        client.dhcp_server_address = ('127.0.0.1', dhcp_port)
        client.dns_server_address = ('127.0.0.1', dns_port)
        client.app_tcp_port, client.app_udp_port = tcp_port, udp_port
        assert client.request_ip(), 'DHCP DORA failed'
        assert client.request_dns('app.server.local'), 'DNS failed'
        client.download_dash_tcp('low')
        client.download_dash_rudp()
        for transport in ('tcp', 'rudp'):
            for name in ('frame1.jpg', 'frame2.jpg', 'frame3.jpg'):
                actual = (Path(client.download_dir) / f'{transport}_{name}').read_bytes()
                assert actual == payload, f'{transport}/{name}: content mismatch ({len(actual)} bytes)'
        print('PASS: DHCP + DNS + TCP/RUDP; six files match 5888-byte source payloads.')
    finally:
        for child in children:
            child.terminate()
        for child in children:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5)
