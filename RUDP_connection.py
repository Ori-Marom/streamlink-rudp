import socket
import random
import time
import threading
from RUDP_core import RUDPHeader, RUDPPacket

# סימולציית השהיה (latency) להדגמה בהקלטות – דרישת המטלה
SIMULATE_LATENCY = True
LATENCY_MAX_SEC = 0.05  # עד 50ms השהיה אקראית לפני כל שליחה


class RUDPConnection:
    MSS = 1400
    TIMEOUT = 2.0

    def __init__(self, sock):
        # במקום לפתוח סוקט חדש ולהתנגש בשרת, אנחנו מקבלים את הסוקט הקיים
        self.sock = sock
        self.sock.settimeout(self.TIMEOUT)

    def _do_handshake(self, dest_addr):
        """לחיצת יד משולשת: SYN → SYN-ACK → ACK (לפני שליחת DATA)."""
        syn_header = RUDPHeader(seq=0, ack=0, flags=RUDPHeader.SYN, payload_len=0)
        syn_pkt = RUDPPacket(syn_header, b'')
        self._send_packet_simulated(syn_pkt, dest_addr)
        print("   [RUDP] Sent SYN, waiting for SYN-ACK...")
        while True:
            raw_data, addr = self.sock.recvfrom(1024)
            pkt = RUDPPacket.from_bytes(raw_data)
            if not pkt:
                continue
            if (pkt.header.flags & RUDPHeader.SYN) and (pkt.header.flags & RUDPHeader.ACK):
                ack_header = RUDPHeader(seq=1, ack=1, flags=RUDPHeader.ACK, payload_len=0)
                self.sock.sendto(ack_header.pack(), dest_addr)
                print("   [RUDP] Handshake complete (SYN → SYN-ACK → ACK).")
                return True

    def _do_fin(self, dest_addr, total_packets):
        """סיום חיבור מסודר: שליחת FIN והמתנה ל-ACK."""
        fin_header = RUDPHeader(seq=total_packets, ack=0, flags=RUDPHeader.FIN, payload_len=0)
        fin_pkt = RUDPPacket(fin_header, b'')
        self._send_packet_simulated(fin_pkt, dest_addr)
        print("   [RUDP] Sent FIN, waiting for ACK...")
        self.sock.settimeout(5.0)
        try:
            raw_data, _ = self.sock.recvfrom(1024)
            pkt = RUDPPacket.from_bytes(raw_data)
            if pkt and (pkt.header.flags & RUDPHeader.ACK):
                print("   [RUDP] FIN acknowledged, connection closed.")
        except socket.timeout:
            print("   [RUDP] FIN ACK timeout (continuing anyway).")
        finally:
            self.sock.settimeout(self.TIMEOUT)

    def _receiver_thread(self, state, dest_addr, nca_instance, packets):
        """חוט שמאזין ל-ACKs ומקדם את החלון – לא חוסם את השליחה."""
        condition = state["condition"]
        total = state["total_packets"]
        while state["base"] < total:
            try:
                raw_data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                with condition:
                    state["next_seq"] = state["base"]
                    nca_instance.on_timeout()
                    print(f"   [!] Timeout (Slow). Retransmitting from {state['base']}...")
                    condition.notify_all()
                continue
            ack_pkt = RUDPPacket.from_bytes(raw_data)
            if not ack_pkt or not (ack_pkt.header.flags & RUDPHeader.ACK):
                continue
            ack_num = ack_pkt.header.ack
            client_rwnd = ack_pkt.header.window
            needs_fast_retx = nca_instance.on_ack_received(ack_num, client_rwnd)
            with condition:
                if needs_fast_retx:
                    print(f"   [!!!] FAST RETRANSMIT TRIGGERED for Packet {state['base']}")
                    state["next_seq"] = state["base"]
                elif ack_num > state["base"]:
                    state["base"] = ack_num
                    state["window_size"] = max(1, int(nca_instance.get_allowed_window() / self.MSS))
                condition.notify_all()

    def send_chunk_reliable(self, data, dest_addr, window_size_bytes, nca_instance):
        """
        שליחה אמינה: handshake (SYN/SYN-ACK/ACK), DATA עם חלון אסינכרוני (2 threads),
        סיום (FIN). תמיכה ב-Fast Retransmit ובקרת עומסים מול NCA.
        """
        self._do_handshake(dest_addr)

        packets = []
        for i in range(0, len(data), self.MSS):
            chunk_part = data[i:i + self.MSS]
            header = RUDPHeader(seq=len(packets), flags=RUDPHeader.DATA, payload_len=len(chunk_part))
            packets.append(RUDPPacket(header, chunk_part))

        total_packets = len(packets)
        window_size = max(1, int(window_size_bytes / self.MSS))
        condition = threading.Condition()
        state = {
            "base": 0,
            "next_seq": 0,
            "window_size": window_size,
            "total_packets": total_packets,
            "condition": condition,
        }

        print(f"   [Worker] Start Sending. Total: {total_packets} pkts (sliding window, 2 threads)")

        recv_thread = threading.Thread(
            target=self._receiver_thread,
            args=(state, dest_addr, nca_instance, packets),
            daemon=True,
        )
        recv_thread.start()

        while True:
            with condition:
                base = state["base"]
                next_seq = state["next_seq"]
                w = state["window_size"]
                if base >= total_packets:
                    break
                to_send = min(base + w, total_packets) - next_seq
                if to_send <= 0:
                    condition.wait(timeout=self.TIMEOUT)
                    continue
                indices = list(range(next_seq, next_seq + to_send))
                state["next_seq"] = next_seq + to_send

            for i in indices:
                self._send_packet_simulated(packets[i], dest_addr)

        recv_thread.join(timeout=1.0)
        self._do_fin(dest_addr, total_packets)
        return True

    def _send_packet_simulated(self, packet, addr):
        # סיכוי של 5% לאיבוד חבילה כדי לבדוק את המערכת (לפי דרישות המטלה)
        if random.random() < 0.05:
            print(f"      [~] DROP: Packet {packet.header.seq}")
            return
        # סימולציית השהיה (latency) להדגמה ב-PCAP – דרישת המטלה
        if SIMULATE_LATENCY and LATENCY_MAX_SEC > 0:
            time.sleep(random.uniform(0, LATENCY_MAX_SEC))
        self.sock.sendto(packet.to_bytes(), addr)
