"""Unit tests: wire format, RTO estimation, congestion control, ABR and the buffer model."""
import unittest

from streamlink_rudp import packet as pk
from streamlink_rudp.abr import HybridABR, PlaybackModel
from streamlink_rudp.congestion import NewReno
from streamlink_rudp.netem import NetemConfig
from streamlink_rudp.rtt import RTOEstimator


class PacketTests(unittest.TestCase):
    def test_round_trip(self):
        p = pk.Packet(0xDEADBEEF, seq=2**32 - 1, ack=7, flags=pk.DATA | pk.ACK, window=300,
                      payload=b"stream\x00\xff")
        q = pk.Packet.decode(p.encode())
        self.assertEqual(q, p)
        self.assertEqual(pk.HEADER_SIZE, 20)

    def test_every_single_bit_flip_is_detected(self):
        wire = pk.Packet(1, seq=5, flags=pk.DATA, payload=b"abcdefgh").encode()
        for i in range(len(wire)):
            for bit in range(8):
                bad = bytearray(wire)
                bad[i] ^= 1 << bit
                with self.assertRaises(pk.PacketError):
                    pk.Packet.decode(bytes(bad))

    def test_truncated_and_length_mismatch_rejected(self):
        wire = pk.Packet(1, flags=pk.DATA, payload=b"abc").encode()
        for raw in (b"short", wire[:-1], wire + b"x"):
            with self.assertRaises(pk.PacketError):
                pk.Packet.decode(raw)

    def test_checksum_matches_rfc1071_example(self):
        # Example data from RFC 1071 section 3: sum 0xddf2 -> checksum 0x220d
        self.assertEqual(pk.internet_checksum(bytes.fromhex("0001f203f4f5f6f7")), 0x220D)

    def test_sack_and_syn_payloads(self):
        blocks = [(10, 20), (2**32 - 2, 3)]
        self.assertEqual(pk.decode_sack(pk.encode_sack(blocks)), blocks)
        self.assertEqual(len(pk.decode_sack(pk.encode_sack([(i, i + 1) for i in range(9)]))),
                         pk.MAX_SACK_BLOCKS)
        self.assertEqual(pk.decode_syn(pk.encode_syn(123456789, 1200)), (123456789, 1200))

    def test_sequence_arithmetic_wraps(self):
        self.assertEqual(pk.seq_add(2**32 - 1, 2), 1)
        self.assertEqual(pk.seq_diff(1, 2**32 - 1), 2)


class RTOTests(unittest.TestCase):
    def test_rfc6298_first_and_subsequent_samples(self):
        est = RTOEstimator(min_rto=0.0)
        self.assertEqual(est.rto, 1.0)
        est.sample(0.1)
        self.assertAlmostEqual(est.srtt, 0.1)
        self.assertAlmostEqual(est.rttvar, 0.05)
        self.assertAlmostEqual(est.rto, 0.1 + 4 * 0.05)
        est.sample(0.3)
        self.assertAlmostEqual(est.rttvar, 0.75 * 0.05 + 0.25 * 0.2)
        self.assertAlmostEqual(est.srtt, 0.875 * 0.1 + 0.125 * 0.3)

    def test_backoff_doubles_and_is_capped(self):
        est = RTOEstimator(initial=1.0, max_rto=5.0)
        est.backoff()
        self.assertEqual(est.rto, 2.0)
        for _ in range(10):
            est.backoff()
        self.assertEqual(est.rto, 5.0)
        est.reset_backoff()
        self.assertEqual(est.rto, 1.0)

    def test_minimum_rto(self):
        est = RTOEstimator(min_rto=0.2)
        est.sample(0.001)
        self.assertEqual(est.rto, 0.2)


class CongestionTests(unittest.TestCase):
    def test_slow_start_then_congestion_avoidance(self):
        cc = NewReno(initial_cwnd=2, initial_ssthresh=8)
        cc.on_ack(2, 2)
        self.assertEqual(cc.cwnd, 4)
        cc.on_ack(4, 6)
        self.assertEqual(cc.cwnd, 8)
        self.assertEqual(cc.phase, "congestion-avoidance")
        cc.on_ack(8, 14)  # one full window acknowledged -> about +1 segment
        self.assertAlmostEqual(cc.cwnd, 9.0)

    def test_fast_recovery_halves_once_per_window(self):
        cc = NewReno(initial_cwnd=20, initial_ssthresh=10)
        self.assertTrue(cc.on_loss(flight_size=20, highest_sent=100))
        self.assertEqual((cc.cwnd, cc.ssthresh), (10, 10))
        self.assertFalse(cc.on_loss(flight_size=20, highest_sent=100))  # same window: no 2nd cut
        cc.on_ack(5, 50)  # partial ACK: stay in recovery
        self.assertTrue(cc.in_recovery)
        cc.on_ack(50, 100)  # full ACK: leave recovery
        self.assertFalse(cc.in_recovery)

    def test_timeout_resets_to_one_segment(self):
        cc = NewReno(initial_cwnd=30)
        cc.on_timeout(flight_size=30)
        self.assertEqual((cc.cwnd, cc.ssthresh), (1.0, 15.0))


class ABRTests(unittest.TestCase):
    LADDER = [500_000, 1_500_000, 4_000_000]

    def test_starts_low_and_steps_up_one_level_at_a_time(self):
        abr = HybridABR(self.LADDER)
        self.assertEqual(abr.choose(0), 0)
        abr.observe(20_000_000)
        self.assertEqual(abr.choose(10), 1)
        self.assertEqual(abr.choose(10), 2)

    def test_no_step_up_with_low_buffer_and_panic_drop(self):
        abr = HybridABR(self.LADDER, start_index=1)
        abr.observe(20_000_000)
        self.assertEqual(abr.choose(3), 1)       # buffer low: hold
        self.assertEqual(abr.choose(1), 0)       # stall imminent: lowest

    def test_drops_immediately_when_throughput_falls(self):
        abr = HybridABR(self.LADDER, start_index=2)
        abr.observe(1_000_000)
        self.assertEqual(abr.choose(20), 0)

    def test_harmonic_mean_resists_one_fast_sample(self):
        abr = HybridABR(self.LADDER)
        for s in (1_000_000, 1_000_000, 1_000_000, 100_000_000):
            abr.observe(s)
        self.assertLess(abr.estimate, 1_400_000)

    def test_playback_model_counts_stalls(self):
        m = PlaybackModel(startup_seconds=2)
        m.segment_arrived(2, now=0.0)          # playback starts
        self.assertEqual(m.startup_delay is not None, True)
        m.segment_arrived(2, now=1.0)
        self.assertAlmostEqual(m.level(now=1.0), 3.0)
        m.segment_arrived(2, now=6.0)          # buffer ran dry at t=4 -> 2 s stall
        self.assertEqual(m.stalls, 1)
        self.assertAlmostEqual(m.stall_time, 2.0)
        self.assertAlmostEqual(m.level(now=6.0), 2.0)


class NetemConfigTests(unittest.TestCase):
    def test_profile_parsing(self):
        self.assertEqual(NetemConfig.parse_profile("10:1500, 0:6000"), [(0.0, 6000.0), (10.0, 1500.0)])
        self.assertTrue(NetemConfig().is_noop)


if __name__ == "__main__":
    unittest.main()
