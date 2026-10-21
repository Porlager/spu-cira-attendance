"""Call from a CiRA CORE Python+ node (see cira_python_plus.py), or run from the command line.
The adapter accepts the documented DeepDetect payload; no CiRA SDK is assumed.
Standard library only (cv2 is needed only when a frame is attached); works on Python 3.8+.
"""
import argparse
import base64
import json
import time
try:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
except ImportError:  # Python 2.7 used by ROS Melodic
    from urllib2 import HTTPError, Request, urlopen

BASE_URL = "http://127.0.0.1:5050"
_last_sent = {}
_last_preview = {}
_FACE_LISTS = ("faces", "objects", "results", "persons", "people", "detections")
_NAME_KEYS = ("name", "label", "id", "class", "person")
_monotonic = getattr(time, "monotonic", time.time)

def _read_text(path):
    with open(path, "r") as stream:
        return stream.read()


def post(path, payload, token_file, base_url=BASE_URL, timeout=10):
    token = _read_text(token_file).strip()
    req = Request(base_url + path, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "X-API-Key": token}, method="POST")
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def get(path, token_file, base_url=BASE_URL, timeout=10):
    token = _read_text(token_file).strip()
    with urlopen(Request(base_url + path, headers={"X-API-Key": token}), timeout=timeout) as response:
        return json.load(response)


def error_message(error):
    """Readable reason for a failed call; the web app returns {"error": "..."} in Thai."""
    if isinstance(error, HTTPError):
        try:
            return "HTTP %d: %s" % (error.code, json.loads(error.read() or b"{}").get("error", ""))
        except ValueError:
            return "HTTP %d" % error.code
    return str(error)


def send_deepdetect(payload, session_id, token_file, base_url=BASE_URL, person_label="person", min_confidence=0.5):
    """Legacy count-only path. Confidence must be normalized to 0..1 before calling this adapter."""
    objects = payload.get("DeepDetect", {}).get("objects")
    if not isinstance(objects, list):
        raise ValueError("Expected payload.DeepDetect.objects; inspect your CiRA Debug output first")
    count = 0
    for obj in objects:
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise ValueError("Invalid DeepDetect object")
        if obj["name"] != person_label:
            continue
        confidence = obj.get("confidence")
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError("Normalize confidence to 0..1 explicitly for your CiRA version")
        count += confidence >= min_confidence
    return post("/api/cira/observations", {"session_id": session_id, "people_count": count}, token_file, base_url)


def send_frame(image_bgr, session_id, token_file, base_url=BASE_URL):
    """Optional face attendance path; caller supplies a real BGR ndarray frame."""
    import cv2
    h, w = image_bgr.shape[:2]
    frame = cv2.resize(image_bgr, (640, max(1, round(640*h/w))))
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        raise ValueError("Cannot encode frame")
    return post("/api/recognize", {"session_id": session_id, "image": base64.b64encode(encoded.tobytes()).decode()}, token_file, base_url)


def _detector(payload, function):
    """(objects, im_w, im_h) from {"DeepDetect": {...}}, {"objects": [...]} or a bare list.
    Falls back to the only key holding "objects" (e.g. DeepDetectCPU) when `function` is absent."""
    if not isinstance(payload, dict):
        return payload, None, None
    if function in payload:
        inner = payload[function]
    elif isinstance(payload.get("objects"), list):
        inner = payload
    else:
        keys = [k for k, v in payload.items() if isinstance(v, dict) and isinstance(v.get("objects"), list)]
        inner = payload[keys[0]] if len(keys) == 1 else None
    if not isinstance(inner, dict):
        return None, None, None
    return inner.get("objects"), inner.get("im_w"), inner.get("im_h")


def extract_objects(payload, function="DeepDetect"):
    """Accept {"DeepDetect": {"objects": [...]}}, {"objects": [...]} or a bare list.
    Checks structure only. Confidence and boxes are NOT rescaled here; the web app's
    Settings page decides the confidence scale and box format in one place."""
    objects = _detector(payload, function)[0]
    if not isinstance(objects, list):
        raise ValueError("Expected payload.%s.objects; inspect your CiRA Debug output first" % function)
    clean = []
    for obj in objects:
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise ValueError("Invalid %s object: needs a string name" % function)
        if any(type(obj.get(k)) not in (int, float) for k in ("confidence", "x", "y", "w", "h")):
            raise ValueError("Each object needs numeric confidence, x, y, w, h")
        clean.append({k: obj[k] for k in ("name", "confidence", "x", "y", "w", "h")})
    return clean


def _face(obj):
    """{name, x, y, w, h, distance} from one face-recognition result, or None if it is not one."""
    if not isinstance(obj, dict):
        return None
    name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), (str, int)) and not isinstance(obj.get(k), bool)), None)
    box = [obj.get(k) for k in ("x", "y", "w", "h")]
    if not all(type(v) in (int, float) for v in box):
        rect = next((obj[k] for k in ("rect", "box", "bbox") if isinstance(obj.get(k), (list, tuple))), None)
        box = list(rect) if rect and len(rect) == 4 and all(type(v) in (int, float) for v in rect) else None
    if name is None or not box or box[2] <= 0 or box[3] <= 0:
        return None
    face = {"name": str(name), "x": box[0], "y": box[1], "w": box[2], "h": box[3]}
    distance = next((obj[k] for k in ("distance", "dist", "score", "confidence") if type(obj.get(k)) in (int, float)), None)
    if distance is not None:
        face["distance"] = distance
    return face


def extract_faces(payload, detector="DeepDetect"):
    """Face-recognition results from any other node in the payload (e.g. FaceRec), or None if there are none.
    The exact FaceRec format is not documented, so every list of named boxes outside the detector counts."""
    if not isinstance(payload, dict):
        return None
    found, seen_node = [], False
    for key, value in payload.items():
        if key in (detector, "spu") or not isinstance(value, (dict, list)):
            continue
        lists = [value] if isinstance(value, list) else [value.get(k) for k in _FACE_LISTS]
        for items in lists:
            if isinstance(items, list):
                faces = [f for f in (_face(o) for o in items) if f]
                if faces or ("face" in key.lower() and not items):
                    seen_node = True
                found.extend(faces)
    return found if seen_node else None


def payload_preview(payload, limit=30000):
    """Small copy of the raw CiRA payload for the Settings page, so the real node formats can be inspected."""
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return {"note": "payload is not JSON"}
    return json.loads(text) if len(text) <= limit else {"note": "payload too large", "keys": sorted(payload)}


def encode_frame(frame_bgr, max_width=1280, quality=85):
    import cv2
    h, w = frame_bgr.shape[:2]
    if w > max_width:
        frame_bgr = cv2.resize(frame_bgr, (max_width, max(1, round(max_width*h/w))))
    ok, encoded = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("Cannot encode frame")
    return base64.b64encode(encoded.tobytes()).decode()


def send_detections(objects, token_file, session_id=None, width=None, height=None, frame_bgr=None,
                    function="DeepDetect", source="cira", base_url=BASE_URL, min_interval=1.0, timeout=10, room=None,
                    preview_interval=0.2, include_faces=True):
    """Send one frame of CiRA results to /api/cira/frame.

    objects: a DeepDetect payload or a list of {name, confidence, x, y, w, h}.
    width/height: size of the frame the boxes refer to. Taken from frame_bgr when given,
    otherwise from the payload's im_w/im_h when present.
    frame_bgr: optional same, uncropped frame; enables face check-in and per-student behavior.
    room: room code set by the admin (e.g. "11-501"); needed when several rooms have a class open.
    preview_interval: between full sends, frames with an image go out as light "preview" frames
    (picture + boxes only, nothing recorded) so the web view stays smooth. 0 disables previews.
    include_faces: forward face-recognition results from other nodes (e.g. FaceRec) for check-in.
    Returns the server response for full sends, or None for previews and throttled frames.
    """
    now = _monotonic()
    full = not min_interval or now - _last_sent.get(function, float("-inf")) >= min_interval
    if not full and not (frame_bgr is not None and preview_interval and
                         now - _last_preview.get(function, float("-inf")) >= preview_interval):
        return None
    payload = {"function": function, "source": source, "detections": extract_objects(objects, function)}
    faces = extract_faces(objects, function) if include_faces else None
    if faces is not None:
        payload["faces"] = faces
    _, im_w, im_h = _detector(objects, function)
    if width is None and height is None and type(im_w) is int and type(im_h) is int:
        width, height = im_w, im_h
    if session_id is not None:
        payload["session_id"] = session_id
    if room:
        payload["room"] = room
    if frame_bgr is not None:
        height, width = frame_bgr.shape[:2]
        payload["image"] = encode_frame(frame_bgr, quality=85 if full else 70)
    if width is not None and height is not None:
        payload["width"], payload["height"] = int(width), int(height)
    _last_preview[function] = now
    if not full:
        payload["preview"] = True
        post("/api/cira/frame", payload, token_file, base_url, timeout)
        return None
    if isinstance(objects, dict):
        payload["cira_payload"] = payload_preview(objects)
    _last_sent[function] = now
    return post("/api/cira/frame", payload, token_file, base_url, timeout)


def flow_output(result):
    """Compact web result to put back into the CiRA payload (e.g. payload["spu"]) for later nodes."""
    return {"seq": result["seq"], "room": result.get("room"), "people": result["people"], "attentive_pct": result["attentive_pct"],
            "sleeping": result["counts"]["sleeping"], "inattentive": result["counts"]["inattentive"],
            "phone": result["counts"]["phone"],
            "students": {face["student_id"]: face["behavior"] for face in result["faces"] if face["student_id"]},
            "ignored_labels": result["ignored_labels"]}


def send_output(function, output, token_file, base_url=BASE_URL, timeout=10):
    """Show any other CiRA function result (DeepClassif, OCR, ...) on the Settings page. Memory only."""
    return post("/api/cira/output", {"function": function, "output": output}, token_file, base_url, timeout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send one CiRA result JSON file to SPU Attendance")
    parser.add_argument("payload", nargs="?", help="JSON file exported from CiRA Debug; do not commit real classroom payloads")
    parser.add_argument("--mode", choices=["detections", "count"], default="detections")
    parser.add_argument("--session", type=int, help="optional for detections (defaults to the open session), required for count")
    parser.add_argument("--room", help="room code, needed when several rooms have a class open")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--image", help="optional same frame as the payload, for face check-in")
    parser.add_argument("--function", default="DeepDetect")
    parser.add_argument("--person-label", default="person")
    parser.add_argument("--token-file", default="data/api-token.txt")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--ping", action="store_true", help="send a test output and exit")
    args = parser.parse_args()
    if args.ping:
        print(send_output("ping", {"sent_at": time.strftime("%Y-%m-%d %H:%M:%S")}, args.token_file, args.base_url))
    elif not args.payload:
        parser.error("payload is required unless --ping is used")
    elif args.mode == "count":
        if args.session is None:
            parser.error("--session is required for --mode count")
        print(send_deepdetect(json.loads(_read_text(args.payload)), args.session, args.token_file,
                              base_url=args.base_url, person_label=args.person_label))
    else:
        frame = None
        if args.image:
            import cv2
            frame = cv2.imread(args.image)
            if frame is None:
                parser.error("cannot read image " + args.image)
        print(send_detections(json.loads(_read_text(args.payload)), args.token_file, session_id=args.session,
                              width=args.width, height=args.height, frame_bgr=frame, function=args.function,
                              base_url=args.base_url, min_interval=0, room=args.room))
