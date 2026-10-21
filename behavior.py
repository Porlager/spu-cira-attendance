"""Pure logic for CiRA behavior detections: settings, parsing, face matching, smoothing.
No Flask or OpenCV here, so every rule can be unit-tested directly."""
import math
import time

# "phone" is optional: it only counts when settings["phone_enabled"] is True.
BEHAVIORS = ("attentive", "inattentive", "sleeping", "phone")
TARGETS = BEHAVIORS + ("person", "ignore")
DEFAULT_SETTINGS = {
    # Placeholder class names. Replace them with the labels your CiRA model really outputs.
    "label_map": {"person": "person", "awake": "attentive", "distracted": "inattentive",
                  "sleep": "sleeping", "phone": "phone"},
    # CiRA CORE 2025 docs: DeepDetect x, y are the box CENTER in pixels, confidence 0..1.
    "box_origin": "center",       # center | topleft
    "coord_units": "pixel",       # pixel | normalized (0..1)
    "confidence_scale": 1,        # 1 when CiRA sends 0..1, 100 when it sends 0..100
    "min_confidence": 0.5,
    "smoothing_frames": 3,
    "phone_enabled": False,       # optional: phone
    # Web face recognition (LBPH) distance cutoff: lower = stricter. Used only when CiRA sends no faces.
    "face_threshold": 70,
}
MAX_DETECTIONS = 300


def copy_settings(settings):
    return {**settings, "label_map": dict(settings["label_map"])}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_settings(data, base=None):
    """Return a full settings dict: `base` (or defaults) overlaid with validated `data`."""
    if not isinstance(data, dict):
        raise ValueError("การตั้งค่าต้องเป็น JSON object")
    unknown = sorted(set(data) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError("ไม่รู้จักการตั้งค่า: " + ", ".join(unknown))
    result = copy_settings(base or DEFAULT_SETTINGS)
    if "label_map" in data:
        label_map = data["label_map"]
        if not isinstance(label_map, dict) or len(label_map) > 50:
            raise ValueError("การจับคู่คลาสต้องเป็น object ไม่เกิน 50 รายการ")
        clean = {}
        for name, target in label_map.items():
            name = name.strip().lower() if isinstance(name, str) else ""
            if not 1 <= len(name) <= 64:
                raise ValueError("ชื่อคลาสต้องยาว 1–64 ตัวอักษร")
            if target not in TARGETS:
                raise ValueError("พฤติกรรมของคลาส " + name + " ไม่ถูกต้อง")
            clean[name] = target
        result["label_map"] = clean
    choices = {"box_origin": ("topleft", "center"), "coord_units": ("pixel", "normalized")}
    for key, allowed in choices.items():
        if key in data:
            if data[key] not in allowed:
                raise ValueError(key + " ต้องเป็น " + " หรือ ".join(allowed))
            result[key] = data[key]
    if "confidence_scale" in data:
        if type(data["confidence_scale"]) is not int or data["confidence_scale"] not in (1, 100):
            raise ValueError("confidence_scale ต้องเป็น 1 หรือ 100")
        result["confidence_scale"] = data["confidence_scale"]
    if "min_confidence" in data:
        if not _number(data["min_confidence"]) or not 0 <= data["min_confidence"] <= 1:
            raise ValueError("min_confidence ต้องอยู่ระหว่าง 0–1")
        result["min_confidence"] = float(data["min_confidence"])
    if "smoothing_frames" in data:
        if type(data["smoothing_frames"]) is not int or not 1 <= data["smoothing_frames"] <= 10:
            raise ValueError("smoothing_frames ต้องเป็นจำนวนเต็ม 1–10")
        result["smoothing_frames"] = data["smoothing_frames"]
    if "face_threshold" in data:
        if type(data["face_threshold"]) is not int or not 20 <= data["face_threshold"] <= 150:
            raise ValueError("face_threshold ต้องเป็นจำนวนเต็ม 20–150")
        result["face_threshold"] = data["face_threshold"]
    if "phone_enabled" in data:  # optional: phone
        if type(data["phone_enabled"]) is not bool:
            raise ValueError("phone_enabled ต้องเป็น true หรือ false")
        result["phone_enabled"] = data["phone_enabled"]
    return result


def effective_behavior(label, settings):
    """Map a CiRA class name to a behavior. None means the class is not mapped yet."""
    target = settings["label_map"].get(label.strip().lower())
    if target == "phone" and not settings["phone_enabled"]:  # optional: phone
        return "person"
    return target


def unwrap_cira_payload(data, function=None):
    """Find detector output in a raw CiRA payload.

    Accepts {"DeepDetect": {"objects": [...], "im_w": 1280, "im_h": 720}}, the same wrapped
    in {"payload": {...}}, or another detector key such as "DeepDetectCPU".
    Returns (function, objects, im_w, im_h), or None when no single detector output is found.
    """
    if isinstance(data.get("payload"), dict):
        data = data["payload"]
    if function in data:
        keys = [function]
    else:
        keys = [k for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("objects"), list)]
    if len(keys) != 1 or not isinstance(data[keys[0]], dict) or not isinstance(data[keys[0]].get("objects"), list):
        return None
    inner = data[keys[0]]
    return keys[0], inner["objects"], inner.get("im_w"), inner.get("im_h")


def _clamp(value):
    return min(1.0, max(0.0, value))


def parse_detections(items, settings, width=None, height=None, allow_student_id=False):
    """Validate CiRA objects and normalize boxes to [x0, y0, x1, y1] in 0..1.

    Returns (detections, unmapped_labels). Each detection has `kind`:
    "person" for person-class boxes, "state" for one-box-per-student behavior boxes.
    """
    if not isinstance(items, list) or len(items) > MAX_DETECTIONS:
        raise ValueError("detections ต้องเป็น list ไม่เกิน %d รายการ" % MAX_DETECTIONS)
    if settings["coord_units"] == "pixel":
        if type(width) is not int or type(height) is not int or not (1 <= width <= 10000 and 1 <= height <= 10000):
            raise ValueError("ต้องส่ง width และ height (พิกเซล) ของภาพที่ใช้ตรวจจับ")
        scale_x, scale_y = width, height
    else:
        scale_x = scale_y = 1
    scale = settings["confidence_scale"]
    detections, unmapped = [], []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("แต่ละ detection ต้องเป็น object")
        name = item.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError("ชื่อคลาส (name) ไม่ถูกต้อง")
        values = [item.get(k) for k in ("confidence", "x", "y", "w", "h")]
        if not all(_number(v) for v in values):
            raise ValueError("confidence, x, y, w, h ต้องเป็นตัวเลข")
        confidence, x, y, w, h = values
        if w <= 0 or h <= 0:
            raise ValueError("w และ h ต้องมากกว่า 0")
        if not 0 <= confidence <= scale:
            raise ValueError("confidence ต้องอยู่ระหว่าง 0–%d ตรวจการตั้งค่า confidence scale" % scale)
        label = name.strip().lower()
        behavior = effective_behavior(label, settings)
        if behavior is None:
            if label not in unmapped:
                unmapped.append(label)
            continue
        confidence = confidence / scale
        if behavior == "ignore" or confidence < settings["min_confidence"]:
            continue
        if settings["box_origin"] == "center":
            x, y = x - w / 2, y - h / 2
        box = [_clamp(x / scale_x), _clamp(y / scale_y), _clamp((x + w) / scale_x), _clamp((y + h) / scale_y)]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        kind = "person" if settings["label_map"].get(label) == "person" else "state"
        detection = {"label": label, "behavior": behavior, "kind": kind,
                     "confidence": round(confidence, 3), "box": [round(v, 4) for v in box]}
        student_id = item.get("student_id")
        if student_id is not None:
            if not allow_student_id:
                raise ValueError("student_id ใช้ได้เฉพาะข้อมูลจำลองที่ไม่มีภาพ")
            if not isinstance(student_id, str) or not 1 <= len(student_id) <= 30:
                raise ValueError("student_id ไม่ถูกต้อง")
            detection["student_id"] = student_id
        detections.append(detection)
    return detections, unmapped


def parse_faces(items, settings, width=None, height=None):
    """Face-recognition results from CiRA (e.g. the FaceRec node): [{name, x, y, w, h, distance?}].
    Boxes follow the same Settings (origin, units) as DeepDetect. Returns [{name, distance, box}]."""
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("faces ต้องเป็น list ไม่เกิน 100 รายการ")
    if settings["coord_units"] == "pixel":
        if type(width) is not int or type(height) is not int or width < 1 or height < 1:
            raise ValueError("ต้องส่ง width และ height (พิกเซล) ของภาพที่ใช้จำหน้า")
        scale_x, scale_y = width, height
    else:
        scale_x = scale_y = 1
    faces = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("แต่ละ face ต้องมี name")
        values = [item.get(k) for k in ("x", "y", "w", "h")]
        if not all(_number(v) for v in values) or values[2] <= 0 or values[3] <= 0:
            raise ValueError("face ต้องมี x, y, w, h เป็นตัวเลข")
        x, y, w, h = values
        if settings["box_origin"] == "center":
            x, y = x - w / 2, y - h / 2
        distance = item.get("distance")
        faces.append({"name": item["name"].strip()[:64],
                      "distance": round(distance, 2) if _number(distance) else None,
                      "box": [round(_clamp(v), 4) for v in (x / scale_x, y / scale_y, (x + w) / scale_x, (y + h) / scale_y)]})
    return faces


def student_for_name(name, student_ids):
    """CiRA FaceRec labels are the names used when training, e.g. "66060711" or "66060711_Lattaphon".
    Match the exact id first, then an id followed by a separator."""
    if name in student_ids:
        return name
    for sid in student_ids:
        if name.startswith(sid) and not name[len(sid)].isalnum():
            return sid
    return None


def summarize(detections):
    """Per-frame counts. people = max(person boxes, behavior boxes) so running a person
    model and a behavior model together does not count everybody twice."""
    counts = {b: 0 for b in BEHAVIORS}
    persons = states = 0
    for d in detections:
        if d["kind"] == "person":
            persons += 1
            continue
        states += 1
        if d["behavior"] in counts:
            counts[d["behavior"]] += 1
    counts["people"] = max(persons, states)
    return counts


def attentive_pct(counts):
    """attentive / (attentive + inattentive + sleeping + phone). None when nobody is classified."""
    total = sum(counts.get(b, 0) for b in BEHAVIORS)
    return round(counts.get("attentive", 0) * 100 / total, 1) if total else None


def _unique(pairs):
    """Keep student→box pairs only when the student has one box and the box has one student."""
    by_student, by_box = {}, {}
    for student, index in pairs:
        by_student.setdefault(student, set()).add(index)
        by_box.setdefault(index, set()).add(student)
    return {student: next(iter(boxes)) for student, boxes in by_student.items()
            if len(boxes) == 1 and len(by_box[next(iter(boxes))]) == 1}


def match_faces(faces, detections):
    """Attribute recognized faces to behavior boxes.

    Rule: a face belongs to a behavior box when the face center lies inside the box.
    If several boxes contain it, the smallest box wins. A box claimed by two students,
    or a student found in two boxes, is left unattributed. Returns {student_id: index}.
    """
    pairs = []
    for face in faces:
        student = face.get("student_id")
        if not student:
            continue
        x0, y0, x1, y1 = face["box"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best = None
        for index, d in enumerate(detections):
            if d["kind"] != "state" or d["behavior"] not in BEHAVIORS:
                continue
            bx0, by0, bx1, by1 = d["box"]
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                area = (bx1 - bx0) * (by1 - by0)
                if best is None or area < best[0]:
                    best = (area, index)
        if best:
            pairs.append((student, best[1]))
    return _unique(pairs)


def match_hints(detections):
    """Simulator only: detections already carry student_id, so apply the same uniqueness rule."""
    return _unique([(d["student_id"], i) for i, d in enumerate(detections)
                    if d.get("student_id") and d["kind"] == "state" and d["behavior"] in BEHAVIORS])


class Smoother:
    """A student's shown status changes only after `frames` consecutive matching observations."""

    def __init__(self, frames=3, stale_seconds=20, clock=time.monotonic):
        self.frames = frames
        self.stale_seconds = stale_seconds
        self.clock = clock
        self.state = {}

    def update(self, key, behavior):
        now = self.clock()
        s = self.state.setdefault(key, {"status": None, "since": now, "candidate": None, "streak": 0})
        s["last"] = now
        if behavior == s["status"]:
            s["candidate"], s["streak"] = None, 0
        else:
            if behavior == s["candidate"]:
                s["streak"] += 1
            else:
                s["candidate"], s["streak"] = behavior, 1
            if s["streak"] >= self.frames:
                s.update(status=behavior, since=now, candidate=None, streak=0)
        return s["status"]

    def status(self, key):
        """(status, seconds). status is a behavior, "unknown" or "not_seen"."""
        s = self.state.get(key)
        if not s or s["status"] is None:
            return "unknown", 0
        now = self.clock()
        if now - s["last"] > self.stale_seconds:
            return "not_seen", int(now - s["last"])
        return s["status"], int(now - s["since"])

    def forget_session(self, session_id):
        self.state = {k: v for k, v in self.state.items() if k[0] != session_id}

    def forget_student(self, student_id):
        self.state = {k: v for k, v in self.state.items() if k[1] != student_id}
