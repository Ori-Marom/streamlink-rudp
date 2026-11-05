"""Regression tests for packet encoding, corruption checks, and service behavior."""
import unittest

from RUDP_core import RUDPHeader, RUDPPacket
from rudp_packet import RUDPPacket as LegacyPacket
from dhcp_server import DHCPServer
from dns_server import DNSServer
from nca import NCA


class CaptureSocket:
    def __init__(self):
        self.messages = []

    def sendto(self, data, addr):
        self.messages.append((data.decode(), addr))


class ProtocolTests(unittest.TestCase):
    def test_header_round_trip_keeps_checksum_and_payload_length_distinct(self):
        payload = b"streamlink\x00\xff"
        original = RUDPHeader(seq=7, ack=3, flags=RUDPHeader.DATA,
                              window=4200, payload_len=len(payload))
        wire = RUDPPacket(original, payload).to_bytes()
        packet = RUDPPacket.from_bytes(wire)
        self.assertEqual((packet.header.seq, packet.header.ack,
                          packet.header.window, packet.header.payload_len),
                         (7, 3, 4200, len(payload)))
        self.assertEqual(packet.header.checksum, original.checksum)
        self.assertEqual(packet.payload, payload)
        self.assertTrue(RUDPPacket.verify_checksum(wire))

    def test_corruption_is_rejected(self):
        wire = bytearray(RUDPPacket(RUDPHeader(flags=RUDPHeader.DATA,
                                    payload_len=3), b"abc").to_bytes())
        wire[-1] ^= 1
        self.assertFalse(RUDPPacket.verify_checksum(bytes(wire)))

    def test_truncated_packet_is_rejected(self):
        self.assertIsNone(RUDPPacket.from_bytes(b"short"))
        self.assertFalse(RUDPPacket.verify_checksum(b"short"))

    def test_legacy_wrapper_uses_payload_checksum(self):
        packet = LegacyPacket(RUDPHeader(seq=4, flags=RUDPHeader.DATA,
                                         payload_len=3), b"abc")
        self.assertTrue(RUDPPacket.verify_checksum(packet.to_bytes()))
        self.assertIn("Seq=4", str(packet))

    def test_dhcp_offer_ack_nak_and_exhaustion(self):
        server, sock = DHCPServer(port=0), CaptureSocket()
        addr = ("127.0.0.1", 10001)
        server.handle_discover(sock, addr)
        self.assertEqual(sock.messages[-1][0], "DHCP_OFFER:192.168.1.100")
        server.handle_request(sock, addr, "192.168.1.100")
        self.assertEqual(sock.messages[-1][0], "DHCP_ACK:192.168.1.100")
        server.handle_request(sock, addr, "192.168.1.200")
        self.assertEqual(sock.messages[-1][0], "DHCP_NAK:192.168.1.200")
        server.ip_pool.clear()
        server.handle_discover(sock, ("127.0.0.1", 10002))
        self.assertEqual(sock.messages[-1][0], "DHCP_OFFER:NONE")

    def test_dns_known_and_unknown_records(self):
        server = DNSServer(enable_doh=False)
        self.assertEqual(server._resolve("app.server.local")[0], "127.0.0.1")
        self.assertEqual(server._resolve("unknown.local"),
                         (None, "DNS_RESPONSE:NOT_FOUND"))
        self.assertEqual(server._doh_handler("/dns-query", "")[0], 400)

    def test_congestion_window_respects_receive_window_and_timeout(self):
        nca = NCA()
        nca.on_ack_received(1, 700)
        self.assertEqual(nca.get_allowed_window(), 700)
        self.assertFalse(nca.on_ack_received(1, 700))
        self.assertFalse(nca.on_ack_received(1, 700))
        self.assertTrue(nca.on_ack_received(1, 700))
        nca.on_timeout()
        self.assertEqual(nca.cwnd, 1.0)


if __name__ == "__main__":
    unittest.main()
