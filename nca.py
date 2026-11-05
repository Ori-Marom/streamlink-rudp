# גודל מקטע מקסימלי – תואם ל-RUDP_connection.MSS (מניעת מספר קסם)
MSS = 1400


class NCA:
    """
    Network Client Abilities - עם תמיכה ב-Fast Retransmit
    """
    def __init__(self):
        # --- Congestion Control ---
        self.cwnd = 1.0          
        self.ssthresh = 64.0     
        self.dup_acks_count = 0  # מונה אישורים כפולים
        self.last_ack_received = -1 # מעקב אחרי ה-ACK האחרון
        
        # --- Flow Control ---
        self.rwnd = 65535        
        self.loss_history = []   

    def on_ack_received(self, ack_num, client_rwnd):
        """
        מעדכן את החלון. מחזיר True אם נדרש Fast Retransmit.
        """
        self.rwnd = client_rwnd
        
        # בדיקת Duplicate ACKs
        if ack_num == self.last_ack_received:
            self.dup_acks_count += 1
            print(f"[NCA] Duplicate ACK #{self.dup_acks_count} for Seq {ack_num}")
            
            if self.dup_acks_count == 3:
                # --- Fast Retransmit Trigger ---
                # זיהינו אובדן ודאי בלי לחכות ל-Timeout!
                self.ssthresh = max(self.cwnd / 2, 2)
                self.cwnd = self.ssthresh + 3 # ניפוח מלאכותי (Fast Recovery)
                return True # השרת צריך לשדר מיד!
                
        else:
            # ACK חדש ותקין - מאפסים את המונים
            self.dup_acks_count = 0
            self.last_ack_received = ack_num
            
            # הגדלת חלון רגילה (Slow Start / Congestion Avoidance)
            if self.cwnd < self.ssthresh:
                self.cwnd += 1
            else:
                self.cwnd += 1 / int(self.cwnd)
                
        return False # לא נדרש שידור חוזר מיידי

    def on_timeout(self):
        """טיפול ב-Timeout רגיל (כש-Fast Retransmit לא עבד)"""
        self.ssthresh = max(self.cwnd / 2, 2)
        self.cwnd = 1.0 
        self.dup_acks_count = 0
        self.loss_history.append(True)
        print(f"[NCA] Timeout! Resetting CWND to 1.")

    def get_allowed_window(self):
        cwnd_bytes = self.cwnd * MSS
        return min(cwnd_bytes, self.rwnd)

    def recommend_quality(self):
        """מחזיר איכות תואמת לשרת: low / medium / high (לשימוש ב-DASH דינמי)."""
        if self.cwnd < 4:
            return "low"
        if self.cwnd < 10:
            return "medium"
        return "high"
