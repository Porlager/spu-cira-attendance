"""SPU classroom prototype; binds to loopback, stores sensitive data locally."""
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request, Response
from werkzeug.exceptions import HTTPException
import behavior as bh
from vision import Vision, encode_jpeg

FUNCTION_NAME = re.compile(r"[A-Za-z0-9 _.\-]{1,40}")
KPI_WINDOW_SECONDS = 10
PAGES = {"/": ("live.html", "live"), "/enroll": ("enroll.html", "enroll"), "/students": ("students.html", "students"),
         "/reports": ("reports.html", "reports"), "/settings": ("settings.html", "settings")}


def create_app(data_dir=None, vision=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 18 * 1024 * 1024
    root = Path(data_dir or os.environ.get("SPU_DATA_DIR", Path(__file__).parent / "data"))
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    token_path = root / "api-token.txt"
    if not token_path.exists():
        fd = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    token = token_path.read_text().strip()
    db_path = root / "attendance.sqlite3"
    lock = threading.RLock()
    cv = vision or Vision()
    threshold = float(os.environ.get("LBPH_THRESHOLD", "55"))

    def db():
        con = sqlite3.connect(db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS students(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS samples(student_id TEXT REFERENCES students(id) ON DELETE CASCADE, image BLOB NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, title TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT);
    CREATE TABLE IF NOT EXISTS attendance(session_id INTEGER REFERENCES sessions(id), student_id TEXT REFERENCES students(id) ON DELETE CASCADE,
        seen_at TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(session_id, student_id));
    CREATE TABLE IF NOT EXISTS counts(id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES sessions(id), observed_at TEXT NOT NULL,
        count INTEGER NOT NULL, source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    -- One row per CiRA frame. Detections are normalized boxes only: no student ids, no image.
    CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
        observed_at TEXT NOT NULL, source TEXT NOT NULL, function TEXT NOT NULL, people INTEGER NOT NULL,
        attentive INTEGER NOT NULL, inattentive INTEGER NOT NULL, sleeping INTEGER NOT NULL, phone INTEGER NOT NULL DEFAULT 0,
        faces INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0, detections TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS observations_session ON observations(session_id, id);
    CREATE TABLE IF NOT EXISTS student_behavior(session_id INTEGER NOT NULL REFERENCES sessions(id),
        student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE, behavior TEXT NOT NULL,
        frames INTEGER NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(session_id, student_id, behavior));
    """)
    if "simulated" not in [r["name"] for r in con.execute("PRAGMA table_info(sessions)")]:
        con.execute("ALTER TABLE sessions ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0")
        con.commit()
    stored = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key,value FROM settings")}
    con.close()
    db_path.chmod(0o600)
    try:
        config = {"settings": bh.validate_settings(stored)}
    except ValueError:
        config = {"settings": bh.copy_settings(bh.DEFAULT_SETTINGS)}
    smoother = bh.Smoother(frames=config["settings"]["smoothing_frames"])
    # Latest CiRA state lives in memory only; images are never written to disk.
    live = {"seq": 0, "session_id": None, "frame": None, "jpeg": None, "source": None,
            "last_contact": None, "last_ok": None, "last_error": None, "last_error_at": None,
            "window": deque(maxlen=300), "outputs": {}, "unmapped": {}}

    def now():
        return datetime.now(timezone.utc).isoformat()

    def retrain():
        con = db()
        try:
            rows = [{"id": r["id"], "samples": [s[0] for s in con.execute("SELECT image FROM samples WHERE student_id=?", (r["id"],))]}
                    for r in con.execute("SELECT id FROM students ORDER BY id")]
            cv.train(rows)
        finally:
            con.close()

    retrain()

    @app.before_request
    def secure_local_api():
        # Reject DNS rebinding and cross-origin access, including pages which embed the token.
        if request.host.split(":")[0] not in {"localhost", "127.0.0.1"}:
            return jsonify(error="Localhost only"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin request rejected"), 403
        if request.path.startswith("/api/") and not secrets.compare_digest(request.headers.get("X-API-Key", ""), token):
            return jsonify(error="API key required"), 401

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("ต้องส่ง JSON object")
        return value

    def required_text(value, name, limit=100):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(name + " ไม่ถูกต้อง")
        return value.strip()

    def function_name(value):
        if not isinstance(value, str) or not FUNCTION_NAME.fullmatch(value):
            raise ValueError("ชื่อ function ต้องเป็น A-Z, 0-9, ช่องว่าง _ . - ไม่เกิน 40 ตัว")
        return value

    def active_session(con):
        row = con.execute("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def make_page(template, name):
        return lambda: render_template(template, api_token=token, page=name)

    for path, (template, name) in PAGES.items():
        app.add_url_rule(path, "page_" + name, make_page(template, name))

    @app.get("/api/state")
    def state():
        con = db()
        try:
            current = active_session(con)
            sessions = [dict(r) for r in con.execute("SELECT * FROM sessions ORDER BY id DESC")]
            students = [dict(r) for r in con.execute("SELECT id,name,created_at FROM students ORDER BY id")]
            attendance = []
            count = None
            if current:
                attendance = [dict(r) for r in con.execute("SELECT a.*,s.name FROM attendance a JOIN students s ON s.id=a.student_id WHERE session_id=? ORDER BY seen_at DESC", (current["id"],))]
                row = con.execute("SELECT * FROM counts WHERE session_id=? ORDER BY id DESC LIMIT 1", (current["id"],)).fetchone()
                count = dict(row) if row else None
            return jsonify(students=students, sessions=sessions, active=current, attendance=attendance, latest_count=count)
        finally:
            con.close()

    @app.get("/api/students")
    def list_students():
        con = db()
        try:
            rows = con.execute("""SELECT s.id, s.name, s.created_at,
                (SELECT COUNT(*) FROM samples WHERE student_id=s.id) AS samples,
                (SELECT COUNT(*) FROM attendance WHERE student_id=s.id) AS sessions_attended
                FROM students s ORDER BY s.id""")
            return jsonify(students=[dict(r) for r in rows])
        finally:
            con.close()

    @app.post("/api/students")
    def enroll():
        data = body()
        sid = required_text(data.get("id"), "รหัสนักศึกษา", 30)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            raise ValueError("รหัสใช้ได้เฉพาะ A-Z, 0-9, _ และ -")
        name = required_text(data.get("name"), "ชื่อ", 100)
        if data.get("consent") is not True:
            raise ValueError("ต้องยินยอมลงทะเบียนใบหน้าก่อน")
        images = data.get("images")
        if not isinstance(images, list) or not 3 <= len(images) <= 8:
            raise ValueError("ต้องใช้ 3–8 ภาพที่เห็นใบหน้าเพียงคนเดียว")
        samples = []
        for image in images:
            faces = cv.faces(cv.decode(image))
            if len(faces) != 1:
                raise ValueError("ทุกภาพต้องตรวจพบใบหน้าเพียง 1 คน กรุณาถ่ายใหม่")
            samples.append(faces[0][1].tobytes())
        with lock:
            con = db()
            try:
                with con:
                    if con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone():
                        return jsonify(error="รหัสนี้ลงทะเบียนแล้ว"), 409
                    con.execute("INSERT INTO students VALUES(?,?,?)", (sid, name, now()))
                    con.executemany("INSERT INTO samples VALUES(?,?)", [(sid, s) for s in samples])
                retrain()
            finally:
                con.close()
        return jsonify(id=sid, samples=len(samples)), 201

    @app.delete("/api/students/<sid>")
    def delete_student(sid):
        with lock:
            con = db()
            try:
                with con:
                    deleted = con.execute("DELETE FROM students WHERE id=?", (sid,)).rowcount
                retrain()
                smoother.forget_student(sid)
            finally:
                con.close()
        return jsonify(deleted=bool(deleted))

    @app.get("/api/sessions")
    def list_sessions():
        con = db()
        try:
            rows = con.execute("""SELECT s.*, (SELECT COUNT(*) FROM observations WHERE session_id=s.id) AS frames,
                (SELECT COUNT(*) FROM attendance WHERE session_id=s.id) AS present
                FROM sessions s ORDER BY s.id DESC""")
            return jsonify(sessions=[dict(r) for r in rows])
        finally:
            con.close()

    @app.post("/api/sessions")
    def start_session():
        data = body()
        title = required_text(data.get("title"), "ชื่อคาบเรียน")
        simulated = data.get("simulated", False)
        if type(simulated) is not bool:
            raise ValueError("simulated ต้องเป็น true หรือ false")
        con = db()
        try:
            with lock, con:
                if con.execute("SELECT 1 FROM sessions WHERE ended_at IS NULL").fetchone():
                    return jsonify(error="ปิดคาบเดิมก่อนเริ่มคาบใหม่"), 409
                cursor = con.execute("INSERT INTO sessions(title,started_at,simulated) VALUES(?,?,?)", (title, now(), int(simulated)))
            return jsonify(id=cursor.lastrowid), 201
        finally:
            con.close()

    @app.post("/api/sessions/<int:sid>/close")
    def close_session(sid):
        con = db()
        try:
            with lock, con:
                changed = con.execute("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL", (now(), sid)).rowcount
                if changed and live["session_id"] == sid:
                    live.update(session_id=None, frame=None, jpeg=None)
                    live["window"].clear()
                smoother.forget_session(sid)
            return jsonify(closed=bool(changed))
        finally:
            con.close()

    def record(session_id, identities, count, source, count_source=None, extra=None):
        con = db()
        try:
            with lock, con:
                active = con.execute("SELECT id FROM sessions WHERE ended_at IS NULL").fetchone()
                if not active or active["id"] != session_id:
                    raise ValueError("คาบเรียนไม่ตรงกับคาบที่กำลังเปิดอยู่")
                stamp = now()
                for sid in set(identities):
                    if con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone():
                        con.execute("INSERT OR IGNORE INTO attendance VALUES(?,?,?,?)", (session_id, sid, stamp, source))
                con.execute("INSERT INTO counts(session_id,observed_at,count,source) VALUES(?,?,?,?)", (session_id, stamp, count, count_source or source))
                if extra:
                    extra(con, stamp)
        finally:
            con.close()

    @app.post("/api/recognize")
    def recognize():
        data = body()
        session_id = data.get("session_id")
        if type(session_id) is not int:
            raise ValueError("เลือกคาบเรียนก่อน")
        with lock:
            image = cv.decode(data.get("image"))
            results = []
            for box, face in cv.faces(image):
                sid, distance = cv.predict(face, threshold)
                results.append(dict(box=box, student_id=sid, distance=distance))
            record(session_id, [r["student_id"] for r in results if r["student_id"]], len(results), "local-face")
        return jsonify(faces=results, count=len(results), width=image.shape[1], height=image.shape[0])

    @app.post("/api/cira/observations")
    def cira_observation():
        data = body()
        count, sid = data.get("people_count"), data.get("session_id")
        if type(count) is not int or not 0 <= count <= 1000 or type(sid) is not int:
            raise ValueError("session_id และ people_count ต้องเป็นจำนวนเต็ม (0–1000 คน)")
        # Legacy count-only path. Names must be recognized by the local face pipeline.
        record(sid, [], count, "cira-person")
        return jsonify(saved=True)

    def ingest_frame(data):
        settings = config["settings"]
        source = data.get("source", "cira")
        if source not in ("cira", "simulator"):
            raise ValueError("source ต้องเป็น cira หรือ simulator")
        width, height = data.get("width"), data.get("height")
        if "detections" in data:
            function, items = function_name(data.get("function", "DeepDetect")), data["detections"]
        else:
            # Raw CiRA payload, e.g. forwarded as-is from a Python+ node or a REST node.
            found = bh.unwrap_cira_payload(data, data.get("function"))
            if not found:
                raise ValueError("ไม่พบผลตรวจจับ ต้องมี detections หรือ payload ที่มี <ชื่อ node>.objects เช่น DeepDetect.objects")
            function, items, im_w, im_h = found
            function = function_name(function)
            width = width if width is not None else im_w
            height = height if height is not None else im_h
        simulated = source == "simulator"
        con = db()
        try:
            session = active_session(con)
        finally:
            con.close()
        if not session:
            raise ValueError("ยังไม่มีคาบเรียนที่เปิดอยู่ เปิดคาบในหน้าเว็บก่อน")
        requested = data.get("session_id")
        if requested is not None and (type(requested) is not int or requested != session["id"]):
            raise ValueError("คาบเรียนไม่ตรงกับคาบที่กำลังเปิดอยู่")
        if bool(session["simulated"]) != simulated:
            raise ValueError("คาบนี้เป็นคาบจำลอง รับเฉพาะข้อมูลจำลอง" if session["simulated"]
                             else "คาบจริงไม่รับข้อมูลจำลอง ให้ปิดคาบแล้วเปิดคาบจำลองแยก")
        image = data.get("image")
        detections, unmapped = bh.parse_detections(items, settings, width, height,
                                                   allow_student_id=simulated and image is None)
        faces, decoded = [], None
        if image is not None:
            decoded = cv.decode(image)
            ih, iw = decoded.shape[:2]
            for (x, y, w, h), face in cv.faces(decoded):
                student, distance = cv.predict(face, threshold)
                faces.append({"student_id": student, "distance": distance,
                              "box": [round(x / iw, 4), round(y / ih, 4), round((x + w) / iw, 4), round((y + h) / ih, 4)]})
            matches = bh.match_faces(faces, detections)
            identities = [f["student_id"] for f in faces if f["student_id"]]
        elif simulated:
            matches = bh.match_hints(detections)
            identities = [d["student_id"] for d in detections if d.get("student_id")]
        else:
            matches, identities = {}, []
        counts = bh.summarize(detections)
        known = []

        def save(con, stamp):
            stored_dets = [{k: d[k] for k in ("label", "behavior", "kind", "confidence", "box")} for d in detections]
            con.execute("""INSERT INTO observations(session_id,observed_at,source,function,people,attentive,inattentive,
                sleeping,phone,faces,matched,detections) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (session["id"], stamp, source, function, counts["people"], counts["attentive"], counts["inattentive"],
                         counts["sleeping"], counts["phone"], len(faces), len(matches), json.dumps(stored_dets)))
            for student, index in matches.items():
                if con.execute("SELECT 1 FROM students WHERE id=?", (student,)).fetchone():
                    known.append(student)
                    con.execute("""INSERT INTO student_behavior VALUES(?,?,?,1,?) ON CONFLICT(session_id,student_id,behavior)
                        DO UPDATE SET frames=frames+1, last_seen=excluded.last_seen""",
                                (session["id"], student, detections[index]["behavior"], stamp))

        with lock:
            record(session["id"], identities, counts["people"], "simulator" if simulated else "cira-face",
                   count_source=source, extra=save)
            frame_dets = [dict(d) for d in detections]
            for student, index in matches.items():
                frame_dets[index]["student_id"] = student
            if decoded is not None:
                aspect = decoded.shape[1] / decoded.shape[0]
            elif settings["coord_units"] == "pixel":
                aspect = width / height
            else:
                aspect = 16 / 9
            live["seq"] += 1
            live.update(session_id=session["id"], source=source, last_ok=time.time(), last_error=None, last_error_at=None,
                        jpeg=encode_jpeg(decoded) if decoded is not None else None,
                        frame={"seq": live["seq"], "received_at": now(), "source": source, "function": function,
                               "has_image": decoded is not None, "aspect": round(aspect, 4),
                               "detections": frame_dets, "faces": faces})
            live["window"].append((time.time(), session["id"], counts))
            for student in known:
                smoother.update((session["id"], student), detections[matches[student]]["behavior"])
            for label in unmapped:
                live["unmapped"][label] = now()
            while len(live["unmapped"]) > 50:
                live["unmapped"].pop(next(iter(live["unmapped"])))
            store_output(function, {k: v for k, v in data.items() if k != "image"})
        return {"saved": True, "session_id": session["id"], "seq": live["seq"], "people": counts["people"],
                "counts": {b: counts[b] for b in bh.BEHAVIORS}, "attentive_pct": bh.attentive_pct(counts),
                "faces": [{"student_id": f["student_id"], "distance": f["distance"],
                           "behavior": detections[matches[f["student_id"]]]["behavior"] if f["student_id"] in matches else None}
                          for f in faces],
                "ignored_labels": unmapped}

    def store_output(function, output):
        live["outputs"].pop(function, None)
        live["outputs"][function] = {"function": function, "received_at": now(), "output": output}
        while len(live["outputs"]) > 20:
            live["outputs"].pop(next(iter(live["outputs"])))

    @app.route("/api/cira/frame", methods=["POST", "PUT"])
    def cira_frame():
        with lock:
            live["last_contact"] = time.time()
        try:
            return jsonify(ingest_frame(body()))
        except ValueError as error:
            with lock:
                live.update(last_error=str(error), last_error_at=time.time())
            raise

    @app.post("/api/cira/output")
    def cira_output():
        data = body()
        function = function_name(data.get("function"))
        if "output" not in data or len(json.dumps(data["output"])) > 65536:
            raise ValueError("output ต้องมีและมีขนาดไม่เกิน 64 KB")
        with lock:
            live["last_contact"] = time.time()
            store_output(function, data["output"])
        return jsonify(saved=True)

    @app.get("/api/cira/outputs")
    def cira_outputs():
        with lock:
            outputs = list(live["outputs"].values())
        return jsonify(outputs=sorted(outputs, key=lambda o: o["received_at"], reverse=True))

    def cira_status():
        t = time.time()
        status = "offline"
        if live["last_error_at"] and t - live["last_error_at"] <= 10 and (not live["last_ok"] or live["last_error_at"] > live["last_ok"]):
            status = "error"
        elif live["last_ok"] and t - live["last_ok"] <= 5:
            status = "live"
        elif live["last_contact"] and t - live["last_contact"] <= 10:
            status = "connected"
        ago = (lambda v: round(t - v, 1) if v else None)
        return {"status": status, "source": live["source"], "last_error": live["last_error"],
                "seconds_since_contact": ago(live["last_contact"]), "seconds_since_frame": ago(live["last_ok"])}

    @app.get("/api/status")
    def status():
        con = db()
        try:
            session = active_session(con)
        finally:
            con.close()
        with lock:
            return jsonify(session=session, cira=cira_status())

    @app.get("/api/live")
    def live_state():
        settings = config["settings"]
        con = db()
        try:
            session = active_session(con)
            sid = session["id"] if session else None
            students = con.execute("""SELECT s.id, s.name, a.seen_at, a.source FROM students s
                LEFT JOIN attendance a ON a.student_id=s.id AND a.session_id=? ORDER BY s.id""", (sid,)).fetchall()
            tallies = {}
            for r in con.execute("SELECT student_id, behavior, frames FROM student_behavior WHERE session_id=?", (sid,)):
                tallies.setdefault(r["student_id"], {})[r["behavior"]] = r["frames"]
        finally:
            con.close()
        with lock:
            cira = cira_status()
            same = session is not None and live["session_id"] == sid
            frame = live["frame"] if same else None
            t = time.time()
            window = [c for at, s, c in live["window"] if s == sid and t - at <= KPI_WINDOW_SECONDS]
            roster = []
            for r in students:
                frames = tallies.get(r["id"], {})
                total = sum(frames.values())
                state, seconds = smoother.status((sid, r["id"])) if session else ("unknown", 0)
                roster.append({"id": r["id"], "name": r["name"], "present": bool(r["seen_at"]), "seen_at": r["seen_at"],
                               "source": r["source"], "status": state, "status_seconds": seconds, "frames": total,
                               "shares": {b: round(frames.get(b, 0) / total, 3) if total else 0 for b in bh.BEHAVIORS}})

        def mean(key):
            return round(sum(c[key] for c in window) / len(window), 1) if window else None

        pooled = {b: sum(c[b] for c in window) for b in bh.BEHAVIORS}
        kpi = {"registered": len(students), "present": sum(1 for r in roster if r["present"]),
               "people": mean("people"), "attentive_pct": bh.attentive_pct(pooled) if window else None,
               "sleeping": mean("sleeping"), "inattentive": mean("inattentive"),
               "phone": mean("phone") if settings["phone_enabled"] else None,  # optional: phone
               "window_seconds": KPI_WINDOW_SECONDS, "frames_in_window": len(window)}
        return jsonify(session=session, cira=cira, frame=frame, kpi=kpi, roster=roster,
                       settings={"phone_enabled": settings["phone_enabled"]})

    @app.get("/api/live/frame")
    def live_frame():
        with lock:
            jpeg = live["jpeg"]
        if not jpeg:
            return jsonify(error="ยังไม่มีภาพจาก CiRA"), 404
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/api/settings")
    def get_settings():
        settings = config["settings"]
        with lock:
            unmapped = [{"label": k, "last_seen": v} for k, v in live["unmapped"].items() if k not in settings["label_map"]]
        return jsonify(settings=settings, defaults=bh.DEFAULT_SETTINGS, behaviors=list(bh.TARGETS),
                       unmapped_labels=unmapped, endpoint=request.host_url + "api/cira/frame",
                       token_file=str(token_path.resolve()),
                       integrations_dir=str((Path(__file__).parent / "integrations").resolve()))

    @app.put("/api/settings")
    def put_settings():
        settings = bh.validate_settings(body(), base=config["settings"])
        con = db()
        try:
            with lock, con:
                con.executemany("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                [(k, json.dumps(v, ensure_ascii=False)) for k, v in settings.items()])
                config["settings"] = settings
                smoother.frames = settings["smoothing_frames"]
        finally:
            con.close()
        return jsonify(settings=settings)

    def session_report(con, sid):
        session = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not session:
            return None
        totals = con.execute("""SELECT COUNT(*) frames, SUM(attentive) a, SUM(inattentive) i, SUM(sleeping) s, SUM(phone) p,
            MAX(sleeping) max_sleeping, AVG(people) avg_people, MIN(observed_at) first_frame, MAX(observed_at) last_frame
            FROM observations WHERE session_id=?""", (sid,)).fetchone()
        sources = [r[0] for r in con.execute("SELECT DISTINCT source FROM observations WHERE session_id=?", (sid,))]
        timeline = []
        for r in con.execute("""SELECT substr(observed_at,1,16) minute, COUNT(*) frames, SUM(attentive) a, SUM(inattentive) i,
                SUM(sleeping) s, SUM(phone) p, AVG(sleeping) sleeping_avg, AVG(people) people_avg
                FROM observations WHERE session_id=? GROUP BY minute ORDER BY minute""", (sid,)):
            timeline.append({"minute": r["minute"], "frames": r["frames"],
                             "attentive_pct": bh.attentive_pct({"attentive": r["a"], "inattentive": r["i"], "sleeping": r["s"], "phone": r["p"]}),
                             "sleeping_avg": round(r["sleeping_avg"], 2), "people_avg": round(r["people_avg"], 1)})
        tallies = {}
        for r in con.execute("SELECT student_id, behavior, frames FROM student_behavior WHERE session_id=?", (sid,)):
            tallies.setdefault(r["student_id"], {})[r["behavior"]] = r["frames"]
        students = []
        for r in con.execute("""SELECT s.id, s.name, a.seen_at, a.source FROM students s
                LEFT JOIN attendance a ON a.student_id=s.id AND a.session_id=? ORDER BY s.id""", (sid,)):
            frames = tallies.get(r["id"], {})
            total = sum(frames.values())
            students.append({"id": r["id"], "name": r["name"], "present": bool(r["seen_at"]), "seen_at": r["seen_at"],
                             "source": r["source"], "frames": total,
                             "shares": {b: round(frames.get(b, 0) / total, 3) if total else 0 for b in bh.BEHAVIORS}})
        phone_used = bool(totals["p"]) or any(t.get("phone") for t in tallies.values())
        summary = {"registered": len(students), "present": sum(1 for s in students if s["present"]),
                   "frames": totals["frames"], "max_sleeping": totals["max_sleeping"],
                   "avg_people": round(totals["avg_people"], 1) if totals["frames"] else None,
                   "avg_attentive_pct": bh.attentive_pct({"attentive": totals["a"] or 0, "inattentive": totals["i"] or 0,
                                                          "sleeping": totals["s"] or 0, "phone": totals["p"] or 0}),
                   "first_frame": totals["first_frame"], "last_frame": totals["last_frame"], "sources": sources}
        return {"session": dict(session), "summary": summary, "timeline": timeline, "students": students,
                "phone_enabled": config["settings"]["phone_enabled"] or phone_used}  # optional: phone

    @app.get("/api/sessions/<int:sid>/report")
    def report(sid):
        con = db()
        try:
            result = session_report(con, sid)
        finally:
            con.close()
        if not result:
            return jsonify(error="ไม่พบคาบเรียน"), 404
        return jsonify(result)

    @app.get("/api/sessions/<int:sid>/export")
    def export(sid):
        con = db()
        try:
            result = session_report(con, sid)
        finally:
            con.close()
        if not result:
            return jsonify(error="ไม่พบคาบเรียน"), 404
        behaviors = ["attentive", "inattentive", "sleeping"] + (["phone"] if result["phone_enabled"] else [])
        labels = {"attentive": "ตั้งใจเรียน (%)", "inattentive": "ไม่ตั้งใจ (%)", "sleeping": "หลับ (%)", "phone": "ใช้โทรศัพท์ (%)"}
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["รหัสนักศึกษา", "ชื่อ", "สถานะ", "เวลา UTC", "แหล่งที่มา", "เฟรมที่ระบุพฤติกรรมได้"] + [labels[b] for b in behaviors])
        def safe(value):
            value = str(value or "")
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        for s in result["students"]:
            shares = ["%.1f" % (s["shares"][b] * 100) if s["frames"] else "" for b in behaviors]
            writer.writerow([safe(s["id"]), safe(s["name"]), "มาเรียน" if s["present"] else "ไม่พบการเช็คชื่อ",
                             s["seen_at"] or "", s["source"] or "", s["frames"]] + shares)
        name = f"attendance-{sid}-simulated.csv" if result["session"]["simulated"] else f"attendance-{sid}.csv"
        return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False, threaded=True)
