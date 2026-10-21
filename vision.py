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


def pick_enrollment_face(faces):
    """Choose the one face to enroll from a photo.

    A clearly dominant face (at least twice the area of the next one) wins, so a small
    false detection in the background does not reject the photo. Raises ValueError with
    a short Thai reason otherwise.
    """
    if not faces:
        raise ValueError("ไม่พบใบหน้า")
    ranked = sorted(faces, key=lambda f: f[0][2] * f[0][3], reverse=True)
    if len(ranked) > 1 and ranked[0][0][2] * ranked[0][0][3] < 2 * ranked[1][0][2] * ranked[1][0][3]:
        raise ValueError("พบใบหน้ามากกว่า 1 คน")
    return ranked[0]


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

    def enroll_faces(self, image):
        """More forgiving detection for enrollment photos (one person, close to the camera):
        small frames are upscaled, and a contrast-equalized pass runs when the first finds nothing."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        scale = 2 if gray.shape[1] < 600 else 1
        big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR) if scale > 1 else gray
        with self.lock:
            boxes = self.detector.detectMultiScale(big, 1.1, 4, minSize=(60, 60))
            if len(boxes) == 0:
                boxes = self.detector.detectMultiScale(cv2.equalizeHist(big), 1.1, 3, minSize=(60, 60))
        faces = []
        for x, y, w, h in boxes:
            crop = cv2.equalizeHist(cv2.resize(big[y:y+h, x:x+w], (160, 160)))
            faces.append(((int(x) // scale, int(y) // scale, int(w) // scale, int(h) // scale), crop))
        return faces

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
