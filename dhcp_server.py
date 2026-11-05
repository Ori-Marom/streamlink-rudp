import socket


class DHCPServer:
    def __init__(self, host='127.0.0.1', port=67):
        """
        אתחול שרת ה-DHCP.
        פורט 67 הוא הפורט הסטנדרטי להאזנה של שרת DHCP.
        """
        self.host = host
        self.port = port
        self.buffer_size = 1024
        # מאגר כתובות IP פשוט לחלוקה ללקוחות
        self.ip_pool = ["192.168.1.100", "192.168.1.101", "192.168.1.102"]
        self.allocated_ips = {}

    def start(self):
        """
        הפעלת השרת והאזנה לבקשות נכנסות מלקוחות.
        """
        # הגדרת שקע UDP
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_socket.bind((self.host, self.port))

        print(f"[*] DHCP Server is up and listening on {self.host}:{self.port}...")

        try:
            while True:
                # קבלת נתונים מהלקוח
                data, client_address = server_socket.recvfrom(self.buffer_size)
                message = data.decode('utf-8')

                print(f"\n[+] Received message from {client_address}: {message}")

                # ניתוב הבקשה לפי סוג ההודעה (מחזור DORA מלא)
                if message == "DHCP_DISCOVER":
                    self.handle_discover(server_socket, client_address)
                elif message.startswith("DHCP_REQUEST:"):
                    requested_ip = message.split(":", 1)[1]
                    self.handle_request(server_socket, client_address, requested_ip)
                else:
                    print("[-] Unknown message type received.")

        except KeyboardInterrupt:
            print("\n[*] Shutting down DHCP Server.")
        finally:
            server_socket.close()

    def handle_discover(self, server_socket, client_address):
        """
        טיפול בבקשת גילוי (Discover) ושליחת הצעת IP (Offer).
        מקרה קצה: כשאין IP במאגר – שולחים DHCP_OFFER:NONE כדי שהלקוח לא יישאר על timeout.
        """
        if self.ip_pool:
            # שליפת הכתובת הבאה הפנויה מהמאגר
            offered_ip = self.ip_pool.pop(0)
            self.allocated_ips[client_address] = offered_ip

            # יצירת הודעת ההצעה
            response = f"DHCP_OFFER:{offered_ip}"
            server_socket.sendto(response.encode('utf-8'), client_address)

            print(f"[>] Offered IP {offered_ip} to {client_address}")
        else:
            # מקרה קצה: מאגר ריק – הלקוח מקבל תשובה מפורשת
            response = "DHCP_OFFER:NONE"
            server_socket.sendto(response.encode('utf-8'), client_address)
            print("[!] No available IPs in the pool – sent DHCP_OFFER:NONE to client.")

    def handle_request(self, server_socket, client_address, requested_ip):
        """
        טיפול בבקשת הקצאה (Request) ושליחת אישור (Ack) – השלמת DORA.
        """
        if self.allocated_ips.get(client_address) == requested_ip:
            response = f"DHCP_ACK:{requested_ip}"
            server_socket.sendto(response.encode('utf-8'), client_address)
            print(f"[>] Acknowledged IP {requested_ip} to {client_address}")
        else:
            # הלקוח ביקש IP שלא הוצע לו – אפשר לשלוח NAK או להתעלם
            response = f"DHCP_NAK:{requested_ip}"
            server_socket.sendto(response.encode('utf-8'), client_address)
            print(f"[-] NAK: {client_address} requested {requested_ip} which was not offered.")


if __name__ == "__main__":
    # יצירת מופע של השרת והפעלתו
    dhcp_server = DHCPServer()
    dhcp_server.start()
