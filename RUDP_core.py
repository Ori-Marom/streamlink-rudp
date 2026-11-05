import struct

def calculate_checksum(data):
    """
    מימוש Internet Checksum (16-bit).
    סוכם מילים של 16 ביט ומבצע Carry Fold.
    """
    if len(data) % 2 == 1:
        data += b'\x00'
    
    s = 0
    for i in range(0, len(data), 2):
        # חיבור מילים של 2 בייטים
        w = (data[i] << 8) + (data[i+1])
        s = s + w
        # ביצוע Carry (העברת הביט ה-17 חזרה להתחלה)
        s = (s & 0xffff) + (s >> 16)
        
    return ~s & 0xffff

class RUDPHeader:
    # Seq(L), Ack(L), Flags(H), Window(L), Checksum(H), PayloadLen(L)
    HEADER_FORMAT = "!LLHLHL" 
    SIZE = struct.calcsize(HEADER_FORMAT)

    # דגלים
    SYN = 0x01
    ACK = 0x02
    FIN = 0x04
    DATA = 0x08
    ERR = 0x10

    def __init__(self, seq=0, ack=0, flags=0, window=65535, payload_len=0, checksum=0):
        self.seq = seq
        self.ack = ack
        self.flags = flags
        self.window = window
        self.checksum = checksum
        self.payload_len = payload_len

    def pack(self, payload=b''):
        """
        אורז את ה-Header ומחשב Checksum אוטומטית על הכל.
        """
        # 1. יצירת Header זמני עם Checksum מאופס לצורך החישוב
        temp_header = struct.pack(self.HEADER_FORMAT, 
                                 self.seq, self.ack, self.flags, 
                                 self.window, 0, self.payload_len)
        
        # 2. חישוב ה-Checksum על ה-Header + ה-Payload
        self.checksum = calculate_checksum(temp_header + payload)
        
        # 3. החזרת ה-Header הסופי עם ה-Checksum המעודכן
        return struct.pack(self.HEADER_FORMAT, 
                           self.seq, self.ack, self.flags, 
                           self.window, self.checksum, self.payload_len)

    @classmethod
    def unpack(cls, data):
        unpacked = struct.unpack(cls.HEADER_FORMAT, data)
        seq, ack, flags, window, checksum, payload_len = unpacked
        return cls(seq=seq, ack=ack, flags=flags, window=window,
                   payload_len=payload_len, checksum=checksum)

class RUDPPacket:
    def __init__(self, header, payload=b''):
        self.header = header
        self.payload = payload

    def to_bytes(self):
        # כאן מתבצע הקסם: ה-Header מחשב את עצמו מול ה-Payload
        return self.header.pack(self.payload) + self.payload

    @classmethod
    def from_bytes(cls, raw_data):
        if len(raw_data) < RUDPHeader.SIZE:
            return None
        header = RUDPHeader.unpack(raw_data[:RUDPHeader.SIZE])
        payload = raw_data[RUDPHeader.SIZE:]
        return cls(header, payload)

    @classmethod
    def verify_checksum(cls, raw_data):
        """בודק שהחבילה תקינה (checksum) – אם פגומה מחזיר False."""
        if len(raw_data) < RUDPHeader.SIZE:
            return False
        header = RUDPHeader.unpack(raw_data[:RUDPHeader.SIZE])
        payload = raw_data[RUDPHeader.SIZE:]
        temp_header = struct.pack(RUDPHeader.HEADER_FORMAT,
                                  header.seq, header.ack, header.flags,
                                  header.window, 0, header.payload_len)
        computed = calculate_checksum(temp_header + payload)
        return computed == header.checksum
