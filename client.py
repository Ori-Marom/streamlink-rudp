import socket
import json
import os
import struct
import time
from RUDP_core import RUDPHeader, RUDPPacket

# גודל חוצץ ל־Selective Repeat (חבילות שהגיעו מחוץ לסדר)
OUT_OF_ORDER_BUFFER_SIZE = 64


class Client:
    def __init__(self):
        # מניעת מספרי קסם
        self.dhcp_server_address = ('127.0.0.1', 67)
        self.dns_server_address = ('127.0.0.1', 8053)
        self.app_tcp_port = 9999
        self.app_udp_port = 8888
        self.buffer_size = 2048

        self.my_ip = None
        self.app_server_ip = None
        self.download_dir = "downloads"
        # חלון קבלה לשליחה ב-ACK (Flow control – דרישת המטלה)
        self.receive_window = 65535
        # DASH דינמי: throughput אחרון (bytes/sec) לבחירת איכות לפני כל chunk
        self._last_throughput_bps = None

        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

    def request_ip(self):
        """פנייה ל-DHCP לקבלת כתובת IP – מחזור DORA מלא (Discover, Offer, Request, Ack)."""
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(5.0)
        try:
            print("[*] Sending 'DHCP_DISCOVER' to DHCP Server...")
            client_socket.sendto("DHCP_DISCOVER".encode('utf-8'), self.dhcp_server_address)
            data, _ = client_socket.recvfrom(self.buffer_size)
            response = data.decode('utf-8')
            if not response.startswith("DHCP_OFFER:"):
                return False
            offered = response.split(":", 1)[1]
            if offered == "NONE":
                print("[-] DHCP server has no IPs available (OFFER:NONE).")
                return False
            self.my_ip = offered
            print(f"[*] Received OFFER: {self.my_ip}. Sending DHCP_REQUEST...")
            client_socket.sendto(f"DHCP_REQUEST:{self.my_ip}".encode('utf-8'), self.dhcp_server_address)
            data, _ = client_socket.recvfrom(self.buffer_size)
            ack_response = data.decode('utf-8')
            if ack_response.startswith("DHCP_ACK:") and self.my_ip in ack_response:
                print(f"[V] Success! My new IP is: {self.my_ip}\n")
                return True
            if ack_response.startswith("DHCP_NAK:"):
                print("[-] Server sent NAK.")
        except socket.timeout:
            print("[-] DHCP Request timed out.")
        finally:
            client_socket.close()
        return False

    def request_dns(self, domain_name):
        """פנייה לשרת ה-DNS"""
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(5.0)
        try:
            print(f"[*] Asking DNS Server for IP of: '{domain_name}'")
            client_socket.sendto(domain_name.encode('utf-8'), self.dns_server_address)
            data, _ = client_socket.recvfrom(self.buffer_size)
            response = data.decode('utf-8')
            if response.startswith("DNS_RESPONSE:") and "NOT_FOUND" not in response:
                self.app_server_ip = response.split(":")[1]
                print(f"[V] Success! Application Server IP is: {self.app_server_ip}\n")
                return True
        except socket.timeout:
            print("[-] DNS Request timed out.")
        finally:
            client_socket.close()
        return False

    def request_dns_doh(self, domain_name):
        """בונוס: פנייה ל-DNS דרך DoH (DNS over HTTP) – שרת מאזין על פורט 8054."""
        try:
            from urllib.request import urlopen
            from urllib.error import URLError, HTTPError
        except ImportError:
            print("[-] DoH requires urllib.")
            return False
        doh_url = f"http://127.0.0.1:8054/dns-query?name={domain_name}"
        try:
            print(f"[*] Asking DoH Server for IP of: '{domain_name}'")
            with urlopen(doh_url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("Status") == 0 and data.get("Answer"):
                self.app_server_ip = data["Answer"][0].get("data")
                if self.app_server_ip:
                    print(f"[V] Success (DoH)! Application Server IP is: {self.app_server_ip}\n")
                    return True
            print("[-] DoH: NOT_FOUND or empty answer.")
        except (URLError, HTTPError, ValueError) as e:
            print(f"[-] DoH Request failed: {e}")
        return False

    def download_dash_tcp(self, quality="medium"):
        """הורדת סרטון DASH בעזרת TCP"""
        print(f"\n--- Starting TCP Download (Quality: {quality}) ---")
        tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            tcp_sock.connect((self.app_server_ip, self.app_tcp_port))

            # בקשת ה-Manifest – קוראים אורך (4 בייטים) ואז את כל ה-JSON (מניעת חיתוך)
            tcp_sock.sendall(b"GET_MANIFEST")
            len_buf = b""
            while len(len_buf) < 4:
                chunk = tcp_sock.recv(4 - len(len_buf))
                if not chunk:
                    raise ConnectionError("Connection closed before manifest length")
                len_buf += chunk
            manifest_len = struct.unpack("!I", len_buf)[0]
            manifest_data = b""
            while len(manifest_data) < manifest_len:
                chunk = tcp_sock.recv(min(self.buffer_size, manifest_len - len(manifest_data)))
                if not chunk:
                    break
                manifest_data += chunk
            manifest = json.loads(manifest_data.decode('utf-8'))

            for file_name in manifest["files"]:
                # עבור TCP אנחנו פותחים חיבור חדש לכל קובץ, או משתמשים בקיים
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect((self.app_server_ip, self.app_tcp_port))

                request = f"REQUEST_CHUNK:{quality}:{file_name}"
                sock.sendall(request.encode('utf-8'))

                file_path = os.path.join(self.download_dir, f"tcp_{file_name}")
                chunks = []
                while True:
                    data = sock.recv(self.buffer_size)
                    if not data:
                        break
                    chunks.append(data)
                full_data = b"".join(chunks)
                # מקרה קצה: שרת החזיר הודעת שגיאה – לא כותבים לקובץ
                if full_data.startswith(b"ERROR:"):
                    print(f"[-] TCP Server error for {file_name}: {full_data.decode('utf-8', errors='replace')}")
                else:
                    with open(file_path, 'wb') as f:
                        f.write(full_data)
                    print(f"[TCP] Downloaded {file_name}")
                sock.close()

        except Exception as e:
            print(f"[-] TCP Error: {e}")

    def _adaptive_quality(self):
        """DASH דינמי: בוחר איכות לפי throughput מדוד (לפני כל chunk)."""
        if self._last_throughput_bps is None:
            return "medium"  # chunk ראשון – אין היסטוריה
        # סף בייט/שנייה (ניתן לכוון לפי גודל chunk טיפוסי)
        if self._last_throughput_bps < 8_000:
            return "low"
        if self._last_throughput_bps < 25_000:
            return "medium"
        return "high"

    def download_dash_rudp(self, initial_quality="medium"):
        """הורדת סרטון DASH בעזרת RUDP: handshake, Selective Repeat, DASH דינמי, סיום FIN."""
        print("\n--- Starting RUDP Download (Adaptive quality) ---")
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_addr = (self.app_server_ip, self.app_udp_port)

        try:
            udp_sock.sendto(b"GET_MANIFEST", server_addr)
            data, _ = udp_sock.recvfrom(self.buffer_size)
            manifest = json.loads(data.decode('utf-8'))
            file_sizes = manifest.get("file_sizes", {})
            qualities = manifest.get("qualities", ["low", "medium", "high"])

            for file_name in manifest["files"]:
                # DASH דינמי: איכות לפי throughput מדוד (לפני כל chunk)
                quality = self._adaptive_quality()
                if quality not in qualities:
                    quality = initial_quality if initial_quality in qualities else "medium"
                print(f"\n[*] Requesting {file_name} via RUDP (quality={quality})...")
                request = f"REQUEST_CHUNK:{quality}:{file_name}"
                udp_sock.sendto(request.encode('utf-8'), server_addr)

                # המתנה ל-SYN ולחיצת יד (השרת שולח SYN תחילה)
                udp_sock.settimeout(10.0)
                handshake_done = False
                while not handshake_done:
                    raw_data, _ = udp_sock.recvfrom(4096)
                    if not RUDPPacket.verify_checksum(raw_data):
                        continue
                    packet = RUDPPacket.from_bytes(raw_data)
                    if not packet:
                        continue
                    if packet.header.flags & RUDPHeader.SYN:
                        synack = RUDPHeader(seq=0, ack=1, flags=RUDPHeader.SYN | RUDPHeader.ACK, payload_len=0)
                        udp_sock.sendto(synack.pack(), server_addr)
                        print("[RUDP] Sent SYN-ACK (handshake).")
                    elif packet.header.flags & RUDPHeader.ACK and not (packet.header.flags & RUDPHeader.SYN):
                        handshake_done = True
                    elif packet.header.flags & RUDPHeader.DATA:
                        handshake_done = True
                        # נשאיר את החבילה הראשונה לעיבוד למטה
                        break

                expected_size = file_sizes.get(quality, {}).get(file_name, 0)
                if expected_size <= 0:
                    expected_size = None
                expected_seq = 0
                received_bytes = 0
                # Selective Repeat: חוצץ לחבילות שהגיעו מחוץ לסדר
                ooo_buffer = {}
                file_path = os.path.join(self.download_dir, f"rudp_{file_name}")
                t0 = time.perf_counter()

                udp_sock.settimeout(3.0)
                with open(file_path, 'wb') as f:
                    # עיבוד החבילה הראשונה אם התקבלה כבר ב-handshake
                    if packet is not None and (packet.header.flags & RUDPHeader.DATA):
                        p = packet
                        packet = None
                        seq, payload = p.header.seq, p.payload
                        if seq == expected_seq:
                            f.write(payload)
                            expected_seq += 1
                            received_bytes += len(payload)
                            while expected_seq in ooo_buffer:
                                pld = ooo_buffer.pop(expected_seq)
                                f.write(pld)
                                received_bytes += len(pld)
                                expected_seq += 1
                        elif expected_seq < seq < expected_seq + OUT_OF_ORDER_BUFFER_SIZE:
                            ooo_buffer[seq] = payload
                        ack_header = RUDPHeader(ack=expected_seq, flags=RUDPHeader.ACK, window=self.receive_window)
                        udp_sock.sendto(ack_header.pack(), server_addr)

                    while True:
                        try:
                            if packet is None:
                                raw_data, _ = udp_sock.recvfrom(4096)
                                if not RUDPPacket.verify_checksum(raw_data):
                                    continue
                                packet = RUDPPacket.from_bytes(raw_data)
                            if not packet:
                                packet = None
                                continue

                            # סיום חיבור: FIN מהשרת
                            if packet.header.flags & RUDPHeader.FIN:
                                ack_header = RUDPHeader(ack=expected_seq, flags=RUDPHeader.ACK, window=self.receive_window)
                                udp_sock.sendto(ack_header.pack(), server_addr)
                                print(f"[RUDP] FIN received, connection closed.")
                                packet = None
                                break

                            if not (packet.header.flags & RUDPHeader.DATA):
                                packet = None
                                continue

                            seq, payload = packet.header.seq, packet.payload
                            packet = None

                            if seq == expected_seq:
                                f.write(payload)
                                received_bytes += len(payload)
                                expected_seq += 1
                                while expected_seq in ooo_buffer:
                                    pld = ooo_buffer.pop(expected_seq)
                                    f.write(pld)
                                    received_bytes += len(pld)
                                    expected_seq += 1
                            elif expected_seq < seq < expected_seq + OUT_OF_ORDER_BUFFER_SIZE:
                                ooo_buffer[seq] = payload

                            ack_header = RUDPHeader(ack=expected_seq, flags=RUDPHeader.ACK, window=self.receive_window)
                            udp_sock.sendto(ack_header.pack(), server_addr)

                            if expected_size is not None and received_bytes >= expected_size:
                                break
                        except socket.timeout:
                            break

                    # המתנה קצרה ל-FIN מהשרת (סיום חיבור מסודר)
                    if expected_size is not None and received_bytes >= expected_size:
                        udp_sock.settimeout(2.0)
                        try:
                            raw_data, _ = udp_sock.recvfrom(4096)
                            pkt = RUDPPacket.from_bytes(raw_data)
                            if pkt and (pkt.header.flags & RUDPHeader.FIN):
                                ack_header = RUDPHeader(ack=expected_seq, flags=RUDPHeader.ACK, window=self.receive_window)
                                udp_sock.sendto(ack_header.pack(), server_addr)
                                print(f"[RUDP] FIN received, ACK sent.")
                        except socket.timeout:
                            pass
                        udp_sock.settimeout(3.0)

                elapsed = time.perf_counter() - t0
                if elapsed > 0 and received_bytes > 0:
                    self._last_throughput_bps = received_bytes / elapsed
                print(f"[RUDP] Finished receiving {file_name} ({received_bytes} bytes).")

        except Exception as e:
            print(f"[-] RUDP Error: {e}")
        finally:
            udp_sock.close()


if __name__ == "__main__":
    my_client = Client()

    # 1. השגת IP
    if my_client.request_ip():
        # 2. מציאת שרת האפליקציה ב-DNS
        if my_client.request_dns("app.server.local"):
            # 3. הפעלת הורדת וידאו!

            # בדיקת ערוץ TCP
            my_client.download_dash_tcp("low")

            # בדיקת ערוץ RUDP (DASH דינמי – איכות לפי throughput)
            my_client.download_dash_rudp(initial_quality="medium")
