"""Transport tests over real loopback sockets through the network emulator."""
import json
import os
import socket
import threading
import unittest

from streamlink_rudp import packet as pk
from streamlink_rudp.bench import loopback_transfer
from streamlink_rudp.netem import NetemConfig, NetworkEmulator
from streamlink_rudp.transport import (RemoteError, Sender, SenderConfig, TransferError,
                                       receive)


class TransferTests(unittest.TestCase):
    def test_clean_transfer_sizes(self):
        for size in (0, 1, 1199, 1200, 1201, 250_000):
            with self.subTest(size=size):
                data = os.urandom(size)
                loopback_transfer(data)  # raises if the bytes differ

    def test_loss_in_both_directions(self):
        data = os.urandom(300_000)
        r = loopback_transfer(data, forward=NetemConfig(loss=0.1, delay_ms=5, seed=1),
                              reverse=NetemConfig(loss=0.1, delay_ms=5, seed=2))
        self.assertGreater(r.sender.retransmissions, 0)

    def test_reordering_duplication_and_corruption(self):
        chaos = dict(loss=0.05, delay_ms=8, jitter_ms=8, reorder=0.1, duplicate=0.1, corrupt=0.05)
        r = loopback_transfer(os.urandom(300_000),
                              forward=NetemConfig(seed=3, **chaos), reverse=NetemConfig(seed=4, **chaos))
        self.assertGreater(r.receiver.duplicates + r.receiver.out_of_order, 0)
        self.assertGreater(r.link.corrupted, 0)
        self.assertGreater(r.link.reordered, 0)

    def test_handshake_survives_lost_syn(self):
        # A 60% loss rate makes losing the first SYN (and SYN-ACKs) near certain.
        r = loopback_transfer(os.urandom(20_000), forward=NetemConfig(loss=0.6, seed=5),
                              reverse=NetemConfig(loss=0.3, seed=6))
        self.assertEqual(r.receiver.bytes, 20_000)

    def test_go_back_n_baseline_still_correct(self):
        loopback_transfer(os.urandom(150_000), SenderConfig(sack=False, adaptive_rto=False),
                          forward=NetemConfig(loss=0.05, delay_ms=5, seed=7))

    def test_bottleneck_causes_queue_drops_that_are_recovered(self):
        r = loopback_transfer(os.urandom(800_000), SenderConfig(trace=True),
                              forward=NetemConfig(rate_kbps=4000, delay_ms=10, queue_ms=20, seed=8))
        self.assertGreater(r.link.dropped_queue, 0)
        self.assertGreater(r.sender.fast_recoveries, 0)
        self.assertTrue(r.sender.trace)
        self.assertLess(r.receiver.throughput_bps, 4_000_000)

    def test_sack_beats_go_back_n_under_loss(self):
        data = os.urandom(300_000)
        net = dict(forward=NetemConfig(loss=0.05, delay_ms=10, seed=9),
                   reverse=NetemConfig(delay_ms=10, seed=10))
        sack = loopback_transfer(data, SenderConfig(), **net)
        gbn = loopback_transfer(data, SenderConfig(sack=False, adaptive_rto=False), **net)
        self.assertLess(sack.sender.retransmissions, gbn.sender.retransmissions)


class ProtocolEdgeTests(unittest.TestCase):
    def setUp(self):
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()

    def _sock(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        self.socks.append(s)
        return s

    def test_packets_with_another_connection_id_are_ignored(self):
        server, client, control = self._sock(), self._sock(), self._sock()
        data = os.urandom(50_000)

        def serve():
            _, addr = control.recvfrom(100)
            # Inject a valid-looking packet for a different transfer first.
            server.sendto(pk.Packet(999, seq=0, flags=pk.SYN, payload=pk.encode_syn(5, 1200)).encode(), addr)
            Sender(server, addr, 42, data).run()

        t = threading.Thread(target=serve)
        t.start()
        received, _ = receive(client, control.getsockname(), b"GET", 42)
        t.join()
        self.assertEqual(received, data)

    def test_remote_error_reply(self):
        client, control = self._sock(), self._sock()

        def serve():
            _, addr = control.recvfrom(100)
            control.sendto(json.dumps({"conn": 7, "error": "not found"}).encode(), addr)

        threading.Thread(target=serve).start()
        with self.assertRaisesRegex(RemoteError, "not found"):
            receive(client, control.getsockname(), b"GET", 7)

    def test_unreachable_server_times_out(self):
        client, silent = self._sock(), self._sock()
        with self.assertRaises(TransferError):
            receive(client, silent.getsockname(), b"GET", 1, request_timeout=0.05, max_requests=3)

    def test_sender_gives_up_on_silent_receiver(self):
        server, hole = self._sock(), self._sock()
        cfg = SenderConfig(control_retries=2)
        with self.assertRaises(TransferError):
            Sender(server, hole.getsockname(), 1, b"x" * 5000, cfg).run()

    def test_emulator_is_deterministic_with_seed(self):
        def drops(seed):
            em = NetworkEmulator(NetemConfig(loss=0.5, seed=seed))
            sink = self._sock()
            for _ in range(200):
                em.send(sink, b"x", sink.getsockname())
            return em.stats.dropped_random
        self.assertEqual(drops(1), drops(1))


if __name__ == "__main__":
    unittest.main()
