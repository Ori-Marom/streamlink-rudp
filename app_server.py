import socket
import os
import json
import struct
import threading
from RUDP_connection import RUDPConnection
from nca import NCA

# Constants to avoid "magic numbers"
SERVER_IP = "127.0.0.1"
UDP_PORT = 8888
TCP_PORT = 9999
BUFFER_SIZE = 1024
ASSETS_DIR = "assets"
ENCODING = 'utf-8'
MANIFEST_FILES = ["frame1.jpg", "frame2.jpg", "frame3.jpg"]
QUALITIES = ["low", "medium", "high"]

# תמונה מינימלית תקינה (1x1 פיקסל JPEG) ליצירת נכסי DASH אם חסרים
MINIMAL_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c \x24.'
    b'\x27 \x22,\x23\x1c\x1c(7)\x2c01\x34\x34\x34\x1f\'\x39=\x382\x3c.\x334\x32\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00'
    b'\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7b'
    b'\x1f\x1f\xff\xd9'
)


def ensure_assets_exist():
    """יוצר תיקיות ונכסי DASH (תמונות דמה) אם חסרים – כדי שהמערכת תעמוד בדרישות."""
    for quality in QUALITIES:
        path = os.path.join(ASSETS_DIR, quality)
        if not os.path.exists(path):
            os.makedirs(path)
            print(f"[*] Created directory: {path}")
        for fname in MANIFEST_FILES:
            file_path = os.path.join(path, fname)
            if not os.path.exists(file_path):
                with open(file_path, 'wb') as f:
                    f.write(MINIMAL_JPEG)
                print(f"[*] Created placeholder: {file_path}")


def get_manifest():
    """
    Returns a dictionary of available video qualities (DASH Manifest).
    כולל גודל כל קובץ לכל איכות – כדי שהלקוח יידע מתי סיום קבלת קובץ ב-RUDP.
    """
    file_sizes = {}
    for quality in QUALITIES:
        file_sizes[quality] = {}
        for fname in MANIFEST_FILES:
            path = os.path.join(ASSETS_DIR, quality, fname)
            file_sizes[quality][fname] = os.path.getsize(path) if os.path.exists(path) else 0
    return {
        "video_name": "project_video",
        "qualities": QUALITIES.copy(),
        "files": MANIFEST_FILES.copy(),
        "file_sizes": file_sizes
    }


def send_file_rudp(udp_sock, addr, file_path):
    """
    שליחת הקובץ תוך שימוש במנגנון ה-RUDP המתקדם וה-NCA.
    """
    if not os.path.exists(file_path):
        print(f"[-] File not found: {file_path}")
        return False

    # אתחול בקרת עומסים (NCA) וחיבור RUDP
    client_nca = NCA()
    rudp_conn = RUDPConnection(udp_sock)

    print(f"[*] Reading {file_path} for RUDP transmission...")
    with open(file_path, 'rb') as f:
        data = f.read()

    # שליחה דרך המחלקה החכמה שמטפלת בפילוח (Segmentation) וחלונות
    success = rudp_conn.send_chunk_reliable(data, addr, client_nca.get_allowed_window(), client_nca)

    if success:
        print(f"[V] Successfully finished sending: {file_path}")
    return success


def udp_listener():
    """מאזין לבקשות UDP (RUDP) ומטפל בהן."""
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind((SERVER_IP, UDP_PORT))
    print(f"[*] Server listening on {SERVER_IP}:{UDP_PORT} (UDP/RUDP)")

    while True:
        try:
            udp_sock.settimeout(None)  # מחכים לבקשה חדשה ללא הגבלת זמן
            data, addr = udp_sock.recvfrom(BUFFER_SIZE)

            try:
                message = data.decode(ENCODING)
            except UnicodeDecodeError:
                continue  # מתעלמים ממידע בינארי שאינו הודעת טקסט ראשונית

            print(f"\n[UDP] Request received: '{message}' from {addr}")

            if message == "GET_MANIFEST":
                manifest = json.dumps(get_manifest())
                udp_sock.sendto(manifest.encode(ENCODING), addr)

            elif message.startswith("REQUEST_CHUNK"):
                parts = message.split(":")
                if len(parts) == 3:
                    _, quality, filename = parts
                    if quality not in QUALITIES or filename not in MANIFEST_FILES:
                        print(f"[-] Invalid quality or filename: {quality!r}, {filename!r}")
                        udp_sock.sendto(b"ERROR: Invalid quality or filename", addr)
                    else:
                        file_path = os.path.join(ASSETS_DIR, quality, filename)
                        send_file_rudp(udp_sock, addr, file_path)
                else:
                    print("[-] Invalid request format.")
        except Exception as e:
            print(f"[-] UDP Server Error: {e}")


def tcp_listener():
    """מאזין לבקשות TCP רגילות ומטפל בהן."""
    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.bind((SERVER_IP, TCP_PORT))
    tcp_sock.listen(5)
    print(f"[*] Server listening on {SERVER_IP}:{TCP_PORT} (TCP)")

    while True:
        client_sock, addr = tcp_sock.accept()
        print(f"\n[TCP] Connection established with {addr}")
        # פותחים תהליכון נפרד כדי לא לתקוע את השרת
        threading.Thread(target=handle_tcp_client, args=(client_sock, addr)).start()


def handle_tcp_client(client_sock, addr):
    try:
        data = client_sock.recv(BUFFER_SIZE)
        message = data.decode(ENCODING)
        print(f"[TCP] Request received: '{message}' from {addr}")

        if message == "GET_MANIFEST":
            manifest_str = json.dumps(get_manifest())
            manifest_bytes = manifest_str.encode(ENCODING)
            # שליחת אורך (4 בייטים big-endian) ואז ה-manifest – כדי שהלקוח יקרא manifest מלא
            client_sock.sendall(struct.pack("!I", len(manifest_bytes)) + manifest_bytes)

        elif message.startswith("REQUEST_CHUNK"):
            parts = message.split(":")
            if len(parts) == 3:
                _, quality, filename = parts
                if quality not in QUALITIES or filename not in MANIFEST_FILES:
                    client_sock.sendall(b"ERROR: Invalid quality or filename")
                    print(f"[-] Invalid quality or filename: {quality!r}, {filename!r}")
                else:
                    file_path = os.path.join(ASSETS_DIR, quality, filename)
                    if os.path.exists(file_path):
                        with open(file_path, 'rb') as f:
                            client_sock.sendall(f.read())
                        print(f"[V] Successfully finished sending {file_path} via TCP")
                    else:
                        client_sock.sendall(b"ERROR: File not found")
        client_sock.close()
    except Exception as e:
        print(f"[-] TCP Client Error: {e}")
        client_sock.close()


def start_server():
    print("--- Video Application Server (DASH) Starting ---")
    # הרצת שני המאזינים (TCP ו-UDP) במקביל
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=tcp_listener, daemon=True).start()

    # השארת התהליך הראשי באוויר
    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("\n[*] Shutting down server.")


if __name__ == "__main__":
    ensure_assets_exist()
    start_server()
