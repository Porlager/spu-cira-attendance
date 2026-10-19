"""Call from a CiRA CORE Python node, or run from the command line.
The adapter accepts the documented DeepDetect payload; no CiRA SDK is assumed.
"""
import argparse
import base64
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:5050"
_last_sent = {}


def post(path, payload, token_file, base_url=BASE_URL):
    token = Path(token_file).read_text().strip()
    req = Request(base_url + path, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "X-API-Key": token}, method="POST")
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def get(path, token_file, base_url=BASE_URL):
    token = Path(token_file).read_text().strip()
    with urlopen(Request(base_url + path, headers={"X-API-Key": token}), timeout=10) as response:
        return json.load(response)


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
    return post("/api/recognize", {"session_id": session_id, "image": base64.b64encode(encoded).decode()}, token_file, base_url)


def extract_objects(payload, function="DeepDetect"):
    """Accept {"DeepDetect": {"objects": [...]}}, {"objects": [...]} or a bare list.
    Checks structure only. Confidence and boxes are NOT rescaled here; the web app's
    Settings page decides the confidence scale and box format in one place."""
    if isinstance(payload, dict):
        inner = payload.get(function, payload)
        objects = inner.get("objects") if isinstance(inner, dict) else None
    else:
        objects = payload
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


def encode_frame(frame_bgr, max_width=1280):
    import cv2
    h, w = frame_bgr.shape[:2]
    if w > max_width:
        frame_bgr = cv2.resize(frame_bgr, (max_width, max(1, round(max_width*h/w))))
    ok, encoded = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("Cannot encode frame")
    return base64.b64encode(encoded).decode()


def send_detections(objects, token_file, session_id=None, width=None, height=None, frame_bgr=None,
                    function="DeepDetect", source="cira", base_url=BASE_URL, min_interval=1.0):
    """Send one frame of CiRA results to /api/cira/frame.

    objects: a DeepDetect payload or a list of {name, confidence, x, y, w, h}.
    width/height: size of the frame the boxes refer to. Taken from frame_bgr when given.
    frame_bgr: optional same, uncropped frame; enables face check-in and per-student behavior.
    Returns the server response, or None when skipped by the min_interval throttle.
    """
    now = time.monotonic()
    if min_interval and now - _last_sent.get(function, float("-inf")) < min_interval:
        return None
    payload = {"function": function, "source": source, "detections": extract_objects(objects, function)}
    if session_id is not None:
        payload["session_id"] = session_id
    if frame_bgr is not None:
        height, width = frame_bgr.shape[:2]
        payload["image"] = encode_frame(frame_bgr)
    if width is not None and height is not None:
        payload["width"], payload["height"] = int(width), int(height)
    _last_sent[function] = now
    return post("/api/cira/frame", payload, token_file, base_url)


def send_output(function, output, token_file, base_url=BASE_URL):
    """Show any other CiRA function result (DeepClassify, OCR, ...) on the Settings page. Memory only."""
    return post("/api/cira/output", {"function": function, "output": output}, token_file, base_url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send one CiRA result JSON file to SPU Attendance")
    parser.add_argument("payload", nargs="?", help="JSON file exported from CiRA Debug; do not commit real classroom payloads")
    parser.add_argument("--mode", choices=["detections", "count"], default="detections")
    parser.add_argument("--session", type=int, help="optional for detections (defaults to the open session), required for count")
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
        print(send_deepdetect(json.loads(Path(args.payload).read_text()), args.session, args.token_file,
                              base_url=args.base_url, person_label=args.person_label))
    else:
        frame = None
        if args.image:
            import cv2
            frame = cv2.imread(args.image)
            if frame is None:
                parser.error("cannot read image " + args.image)
        print(send_detections(json.loads(Path(args.payload).read_text()), args.token_file, session_id=args.session,
                              width=args.width, height=args.height, frame_bgr=frame, function=args.function,
                              base_url=args.base_url, min_interval=0))
