import socket
import json
import threading
from urllib.parse import urlparse, parse_qs

# בונוס: DNS over HTTPS (DoH) – פורט נפרד להאזנה ל-HTTPS/HTTP
DOH_PORT = 8054


class DNSServer:
    def __init__(self, host='127.0.0.1', port=8053, enable_doh=True):
        self.host = host
        self.port = port
        self.doh_port = DOH_PORT
        self.enable_doh = enable_doh
        self.buffer_size = 1024

        # זהו "ספר הטלפונים" של השרת - מילון הממפה שם דומיין לכתובת IP
        self.dns_records = {
            "app.server.local": "127.0.0.1"
        }

    def _resolve(self, domain_name):
        """מחזיר (ip או None, תגובה לטקסט)."""
        if domain_name in self.dns_records:
            return self.dns_records[domain_name], f"DNS_RESPONSE:{self.dns_records[domain_name]}"
        return None, "DNS_RESPONSE:NOT_FOUND"

    def _doh_handler(self, path, query_string):
        """בונוס DoH: תגובה בסגנון JSON (GET ?name=domain)."""
        params = parse_qs(query_string) if query_string else {}
        name = params.get("name", [""])[0].strip()
        if not name:
            return 400, json.dumps({"Status": 2, "Comment": "missing name"})
        ip, _ = self._resolve(name)
        if ip:
            body = {"Status": 0, "Answer": [{"type": 1, "data": ip}]}
        else:
            body = {"Status": 3, "Comment": "NOT_FOUND"}
        return 200, json.dumps(body)

    def _run_doh_server(self):
        """האזנה ל-DoH על HTTP (localhost – ב-production משתמשים ב-HTTPS)."""
        from http.server import HTTPServer, BaseHTTPRequestHandler

        dns_server_ref = self

        class DoHHandler(BaseHTTPRequestHandler):
            def do_GET(req_self):
                parsed = urlparse(req_self.path)
                status, body = dns_server_ref._doh_handler(parsed.path, parsed.query)
                req_self.send_response(status)
                req_self.send_header("Content-Type", "application/json")
                req_self.end_headers()
                req_self.wfile.write(body.encode("utf-8"))
                domain = parse_qs(parsed.query).get("name", [""])[0]
                print(f"[DoH] GET ?name={domain} -> {body[:80]}")

            def log_message(self, format, *args):
                pass

        server = HTTPServer((self.host, self.doh_port), DoHHandler)
        print(f"[*] DoH (DNS over HTTP) listening on {self.host}:{self.doh_port} (bonus)")
        server.serve_forever()

    def start(self):
        if self.enable_doh:
            threading.Thread(target=self._run_doh_server, daemon=True).start()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_socket.bind((self.host, self.port))

        print(f"[*] DNS Server is up and listening on {self.host}:{self.port}...")

        try:
            while True:
                data, client_address = server_socket.recvfrom(self.buffer_size)
                domain_name = data.decode('utf-8')

                print(f"\n[+] Received DNS query for: '{domain_name}' from {client_address}")

                ip_address, response = self._resolve(domain_name)
                if ip_address:
                    print(f"[>] Resolved '{domain_name}' to IP: {ip_address}")
                else:
                    print(f"[-] Domain '{domain_name}' not found in records.")

                server_socket.sendto(response.encode('utf-8'), client_address)

        except KeyboardInterrupt:
            print("\n[*] Shutting down DNS Server.")
        finally:
            server_socket.close()


if __name__ == "__main__":
    dns_server = DNSServer(enable_doh=True)
    dns_server.start()
