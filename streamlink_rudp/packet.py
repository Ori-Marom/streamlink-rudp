"""Wire format for the StreamLink reliable-UDP transport.

Every datagram starts with a fixed 20-byte header (network byte order):

    0               4               8               12  13  14      16      18      20
    +---------------+---------------+---------------+---+---+-------+-------+-------+
    |  connection   |   sequence    |  cumulative   |flg|rsv|window |checksm|paylen |
    |      id       |    number     |      ack      |   |   |(segs) |       |       |
    +---------------+---------------+---------------+---+---+-------+-------+-------+

* ``conn_id`` - random 32-bit identifier chosen by the client for each transfer.
  Packets that carry the wrong ID are ignored, so stray or delayed datagrams from an
  earlier transfer can never be mixed into a new one.
* ``seq`` / ``ack`` - 32-bit sequence space, counted in *segments* and starting at a
  random initial sequence number (ISN) chosen by the sender.
* ``window`` - receive window advertised by the receiver, in segments.
* ``checksum`` - 16-bit Internet checksum (RFC 1071) over header and payload.

An ACK may carry up to :data:`MAX_SACK_BLOCKS` selective-acknowledgement blocks in its
payload, each a pair of 32-bit sequence numbers ``[start, end)`` (RFC 2018 style).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

HEADER_FORMAT = "!IIIBBHHH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 20

SEQ_MOD = 1 << 32
MAX_SACK_BLOCKS = 4
_SACK_BLOCK = struct.Struct("!II")
_SYN_PAYLOAD = struct.Struct("!QH")  # total payload bytes, maximum segment size

SYN = 0x01
ACK = 0x02
FIN = 0x04
DATA = 0x08
RST = 0x10

FLAG_NAMES = {SYN: "SYN", ACK: "ACK", FIN: "FIN", DATA: "DATA", RST: "RST"}


class PacketError(ValueError):
    """Raised for datagrams that are truncated, malformed or fail the checksum."""


def internet_checksum(data: bytes) -> int:
    """RFC 1071 one's-complement checksum, folded to 16 bits."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def seq_add(seq: int, n: int) -> int:
    return (seq + n) % SEQ_MOD


def seq_diff(a: int, b: int) -> int:
    """Distance from ``b`` forward to ``a`` in the 32-bit sequence space."""
    return (a - b) % SEQ_MOD


@dataclass
class Packet:
    conn_id: int
    seq: int = 0
    ack: int = 0
    flags: int = 0
    window: int = 0
    payload: bytes = b""
    checksum: int = field(default=0, compare=False)

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)

    def encode(self) -> bytes:
        if len(self.payload) > 0xFFFF:
            raise PacketError("payload too large")
        header = struct.pack(HEADER_FORMAT, self.conn_id, self.seq, self.ack, self.flags,
                             0, min(self.window, 0xFFFF), 0, len(self.payload))
        self.checksum = internet_checksum(header + self.payload)
        return header[:16] + struct.pack("!HH", self.checksum, len(self.payload)) + self.payload

    @classmethod
    def decode(cls, raw: bytes) -> "Packet":
        if len(raw) < HEADER_SIZE:
            raise PacketError("truncated header")
        conn_id, seq, ack, flags, _rsv, window, checksum, length = struct.unpack(
            HEADER_FORMAT, raw[:HEADER_SIZE])
        payload = raw[HEADER_SIZE:]
        if len(payload) != length:
            raise PacketError("payload length mismatch")
        zeroed = raw[:16] + b"\x00\x00" + raw[18:]
        if internet_checksum(zeroed) != checksum:
            raise PacketError("bad checksum")
        return cls(conn_id, seq, ack, flags, window, payload, checksum)

    def describe(self) -> str:
        names = "|".join(name for bit, name in FLAG_NAMES.items() if self.flags & bit)
        return f"<{names or '-'} conn={self.conn_id:#010x} seq={self.seq} ack={self.ack} len={len(self.payload)}>"


def encode_sack(blocks: list[tuple[int, int]]) -> bytes:
    return b"".join(_SACK_BLOCK.pack(s, e) for s, e in blocks[:MAX_SACK_BLOCKS])


def decode_sack(payload: bytes) -> list[tuple[int, int]]:
    usable = len(payload) - len(payload) % _SACK_BLOCK.size
    return [_SACK_BLOCK.unpack_from(payload, i) for i in range(0, usable, _SACK_BLOCK.size)][:MAX_SACK_BLOCKS]


def encode_syn(total_bytes: int, mss: int) -> bytes:
    return _SYN_PAYLOAD.pack(total_bytes, mss)


def decode_syn(payload: bytes) -> tuple[int, int]:
    if len(payload) != _SYN_PAYLOAD.size:
        raise PacketError("bad SYN payload")
    return _SYN_PAYLOAD.unpack(payload)
