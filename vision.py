"""Local Haar detection + LBPH baseline. Not a liveness detector."""
import base64
import binascii
import threading
import cv2
import numpy as np


def encode_jpeg(image, quality=80):
    """Re-encode a decoded frame for the live view (memory only, never written to disk)."""
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("แปลงภาพเป็น JPEG ไม่สำเร็จ")
    return encoded.tobytes()


class Vision:
    def __init__(self):
        self.detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        self.lock = threading.RLock()
        self.model = None
        self.labels = {}

    def decode(self, data):
        if not isinstance(data, str) or len(data) > 4_000_000:
            raise ValueError("ภาพไม่ถูกต้องหรือใหญ่เกิน 3 MB")
        try:
            raw = base64.b64decode(data.split(",")[-1], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("รูปภาพ base64 ไม่ถูกต้อง")
        # Read dimensions before decoding untrusted compressed image data.
        if len(raw) > 3_000_000:
            raise ValueError("ภาพใหญ่เกินไป")
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
        if image is None or image.shape[0] > 2048 or image.shape[1] > 2048:
            raise ValueError("ใช้ภาพ JPEG/PNG ขนาดไม่เกิน 4096 × 4096")
        return image

    def faces(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        with self.lock:
            boxes = self.detector.detectMultiScale(gray, 1.15, 5, minSize=(45, 45))
        return [(tuple(int(v) for v in box), cv2.equalizeHist(cv2.resize(gray[y:y+h, x:x+w], (160, 160))))
                for box in boxes for x, y, w, h in [box]]

    def train(self, rows):
        with self.lock:
            images, labels, mapping = [], [], {}
            for label, row in enumerate(rows):
                mapping[label] = row["id"]
                for sample in row["samples"]:
                    images.append(np.frombuffer(sample, np.uint8).reshape(160, 160).copy())
                    labels.append(label)
            self.model = None
            self.labels = mapping
            if images:
                self.model = cv2.face.LBPHFaceRecognizer_create()
                self.model.train(images, np.array(labels, dtype=np.int32))

    def predict(self, face, threshold):
        with self.lock:
            if self.model is None:
                return None, None
            label, distance = self.model.predict(face)
            return (self.labels.get(label) if distance < threshold else None), round(distance, 2)
