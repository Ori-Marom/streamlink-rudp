import struct
from RUDP_core import RUDPHeader

class RUDPPacket:
    """
    מחלקה העוטפת את ה-Header (מתוך RUDP_core) ואת ה-Payload (ה-Chunk).
    """
    def __init__(self, header, payload=b''):
        self.header = header
        self.payload = payload

    def to_bytes(self):
        """מכין את החבילה לשליחה: מחבר את ה-Header הארוז עם ה-Payload"""
        return self.header.pack(self.payload) + self.payload

    @classmethod
    def from_bytes(cls, raw_data):
        """מפרק בייטים שהתקבלו מהרשת לאובייקט RUDPPacket"""
        header_size = struct.calcsize(RUDPHeader.HEADER_FORMAT)
        
        if len(raw_data) < header_size:
            return None # או לזרוק שגיאה
            
        # פירוק ה-Header באמצעות הפונקציה הקיימת ב-RUDPHeader
        header = RUDPHeader.unpack(raw_data[:header_size])
        
        # שאר המידע הוא ה-Payload
        payload = raw_data[header_size:]
        
        return cls(header, payload)

    def __str__(self):
        return f"Packet[Seq={self.header.seq}, Flags={self.header.flags}, DataLen={len(self.payload)}]"
