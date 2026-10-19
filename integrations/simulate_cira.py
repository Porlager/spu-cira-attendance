"""Send SIMULATED CiRA DeepDetect results so the dashboard can be demoed without a model.

Every frame is tagged source="simulator" and only goes into a session marked simulated,
which the web app labels "ข้อมูลจำลอง". Never present these numbers as classroom results.

    python integrations/simulate_cira.py --seconds 120 --with-identities
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError

try:
    from integrations import cira_bridge
except ImportError:
    import cira_bridge

WIDTH, HEIGHT = 1280, 720
# Per-frame chance of switching state. Sleep is sticky, distraction is short.
TRANSITIONS = {
    "attentive": {"inattentive": 0.04, "sleeping": 0.004, "phone": 0.01},
    "inattentive": {"attentive": 0.35, "sleeping": 0.01, "phone": 0.03},
    "sleeping": {"attentive": 0.05},
    "phone": {"attentive": 0.25},  # optional: phone
}


def seat_boxes(count, columns=8):
    """Seat grid as (x, y, w, h) pixels. Back rows sit higher and look smaller."""
    columns = max(1, min(columns, count))
    rows = -(-count // columns)
    boxes = []
    for i in range(count):
        row, col = divmod(i, columns)
        t = row / (rows - 1) if rows > 1 else 1.0
        w = int(70 + 60 * t)
        h = int(w * 1.25)
        cx = 60 + (col + 0.5) * (WIDTH - 120) / columns
        cy = 170 + t * 380
        boxes.append((int(cx - w / 2), int(cy - h / 2), w, h))
    return boxes


def labels_from_settings(settings):
    """First CiRA class name mapped to each behavior, e.g. {"sleeping": "sleep"}."""
    labels = {}
    for name, target in settings["label_map"].items():
        if target in TRANSITIONS and (target != "phone" or settings["phone_enabled"]):
            labels.setdefault(target, name)
    missing = [b for b in ("attentive", "sleeping") if b not in labels]
    if missing:
        raise ValueError("Map a CiRA class to %s on the Settings page first" % ", ".join(missing))
    return labels


def step(rng, state, labels):
    for target, chance in TRANSITIONS[state].items():
        if target in labels and rng.random() < chance:
            return target
    return state


def make_frame(rng, people, labels, face_visible=0.8, settings=None):
    """Advance every simulated student one frame and build a /api/cira/frame payload
    in the box format the web app is configured for (like a real CiRA model would)."""
    settings = settings or {"box_origin": "topleft", "coord_units": "pixel", "confidence_scale": 1}
    detections = []
    for person in people:
        person["state"] = step(rng, person["state"], labels)
        if person["state"] not in labels:
            person["state"] = "attentive"
        x, y, w, h = person["box"]
        x = min(max(0, x + rng.randint(-3, 3)), WIDTH - w)
        y = min(max(0, y + rng.randint(-3, 3)), HEIGHT - h)
        if settings["box_origin"] == "center":
            x, y = x + w / 2, y + h / 2
        if settings["coord_units"] == "normalized":
            x, y, w, h = x / WIDTH, y / HEIGHT, w / WIDTH, h / HEIGHT
        confidence = round(rng.uniform(0.6, 0.97), 2) * settings["confidence_scale"]
        obj = {"name": labels[person["state"]], "confidence": round(confidence, 2), "x": x, "y": y, "w": w, "h": h}
        # Sleepers usually have their face down, so the simulator never identifies them.
        if person.get("student_id") and person["state"] != "sleeping" and rng.random() < face_visible:
            obj["student_id"] = person["student_id"]
        detections.append(obj)
    return {"function": "DeepDetect", "source": "simulator", "width": WIDTH, "height": HEIGHT, "detections": detections}


def main():
    parser = argparse.ArgumentParser(description="Send simulated CiRA detections to SPU Attendance")
    parser.add_argument("--token-file", default="data/api-token.txt")
    parser.add_argument("--base-url", default=cira_bridge.BASE_URL)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--fps", type=float, default=1)
    parser.add_argument("--people", type=int, default=24)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--with-identities", action="store_true", help="attach enrolled student ids to some seats")
    parser.add_argument("--keep-open", action="store_true", help="leave the simulated session open at the end")
    parser.add_argument("--title", help="session title for an ad-hoc simulated class (the web labels it ข้อมูลจำลอง)")
    parser.add_argument("--section", type=int, help="section id: simulate that section's room and roster")
    args = parser.parse_args()
    call = {"token_file": args.token_file, "base_url": args.base_url}

    settings = cira_bridge.get("/api/settings", **call)["settings"]
    labels = labels_from_settings(settings)
    session = cira_bridge.get("/api/status", **call)["session"]
    opened = False
    if args.section:
        data = cira_bridge.get("/api/sections/%d" % args.section, **call)
        busy = [s for s in cira_bridge.get("/api/sessions", **call)["sessions"]
                if not s["ended_at"] and s["room_id"] == data["section"]["room_id"]]
        if busy and not busy[0]["simulated"]:
            sys.exit("ห้อง %s มีคาบจริงเปิดอยู่ ตัวจำลองจะไม่ส่งข้อมูลปนกับคาบจริง" % data["section"]["room_code"])
        if busy:
            session_id = busy[0]["id"]
        else:
            session_id = cira_bridge.post("/api/sessions", {"section_id": args.section, "simulated": True}, **call)["id"]
            opened = True
        roster = [s["id"] for s in data["students"] if s["samples"]]  # no face photos = never recognized
    else:
        if session and not session["simulated"]:
            sys.exit("มีคาบจริงเปิดอยู่ ปิดคาบนั้นก่อน ตัวจำลองจะไม่ส่งข้อมูลปนกับคาบจริง")
        if session:
            session_id = session["id"]
        else:
            title = args.title or "คาบจำลอง " + datetime.now().strftime("%H:%M")
            session_id = cira_bridge.post("/api/sessions", {"title": title, "simulated": True}, **call)["id"]
            opened = True
        roster = None
    identities = []
    if args.with_identities:
        identities = roster if roster is not None else [s["id"] for s in cira_bridge.get("/api/students", **call)["students"] if s["samples"]]
    rng = random.Random(args.seed)
    people = [{"box": box, "state": "attentive", "student_id": identities[i] if i < len(identities) else None}
              for i, box in enumerate(seat_boxes(args.people))]
    print("ส่งข้อมูลจำลองเข้าคาบ #%d (Ctrl+C เพื่อหยุด)" % session_id)
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            payload = make_frame(rng, people, labels, settings=settings)
            payload["session_id"] = session_id
            result = cira_bridge.post("/api/cira/frame", payload, **call)
            print("เฟรม %d: คน %d · ตั้งใจ %s%% · หลับ %d" % (result["seq"], result["people"],
                  result["attentive_pct"], result["counts"]["sleeping"]))
            time.sleep(1 / args.fps)
    except KeyboardInterrupt:
        pass
    finally:
        if opened and not args.keep_open:
            cira_bridge.post("/api/sessions/%d/close" % session_id, {}, **call)
            print("ปิดคาบจำลองแล้ว")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        sys.exit("เซิร์ฟเวอร์ตอบ %d: %s" % (error.code, json.loads(error.read() or b"{}").get("error", "")))
    except URLError as error:
        sys.exit("ต่อ %s ไม่ได้: เปิด python app.py ก่อน (%s)" % (cira_bridge.BASE_URL, error.reason))
