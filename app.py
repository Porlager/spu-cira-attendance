"""SPU classroom prototype; binds to loopback, stores sensitive data locally."""
import base64
import binascii
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
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote
from flask import Flask, abort, g, jsonify, redirect, render_template, request, Response, session as cookie
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
import behavior as bh
import roster
from vision import Vision, encode_jpeg, pick_enrollment_face

FUNCTION_NAME = re.compile(r"[A-Za-z0-9 _.\-]{1,40}")
USERNAME = re.compile(r"[A-Za-z0-9_.\-]{3,30}")
CODE = re.compile(r"[A-Za-z0-9 _.\-]{1,20}")
KPI_WINDOW_SECONDS = 10
# hashlib.scrypt is missing on some macOS Pythons, so pin PBKDF2.
PASSWORD_METHOD = "pbkdf2:sha256:600000"
MACHINE = {"id": None, "username": "api-key", "name": "CiRA / API key", "role": "machine"}
# path -> (template, nav key, roles that may open it); the API-key holder may open every page
PAGES = {
    "/": ("start.html", "start", ("admin", "teacher")),
    "/live": ("live.html", "live", ("admin", "teacher")),
    "/reports": ("reports.html", "reports", ("admin", "teacher")),
    "/enroll": ("enroll.html", "enroll", ("admin",)),
    "/students": ("students.html", "students", ("admin",)),
    "/admin/sections": ("sections.html", "sections", ("admin",)),
    "/admin/users": ("users.html", "users", ("admin",)),
    "/settings": ("settings.html", "settings", ("admin",)),
    "/account": ("account.html", "account", ("admin", "teacher")),
}
SESSION_SELECT = """SELECT s.*, sec.course_code, sec.course_name, sec.section AS section_name,
    sec.teacher_id AS section_teacher_id, r.code AS room_code, r.name AS room_name, u.name AS teacher_name
    FROM sessions s LEFT JOIN sections sec ON sec.id=s.section_id LEFT JOIN rooms r ON r.id=s.room_id
    LEFT JOIN users u ON u.id=COALESCE(s.teacher_id, sec.teacher_id)"""


def secret_file(path):
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return path.read_text().strip()


def create_app(data_dir=None, vision=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 18 * 1024 * 1024
    root = Path(data_dir or os.environ.get("SPU_DATA_DIR", Path(__file__).parent / "data"))
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    token_path = root / "api-token.txt"
    token = secret_file(token_path)
    app.config.update(SECRET_KEY=secret_file(root / "secret-key.txt"), SESSION_COOKIE_NAME="spu_session",
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12), PASSWORD_METHOD=PASSWORD_METHOD)
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
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','teacher')), password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS sections(id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
        course_code TEXT NOT NULL, course_name TEXT NOT NULL DEFAULT '', section TEXT NOT NULL,
        teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL,
        UNIQUE(course_code, section));
    CREATE TABLE IF NOT EXISTS section_students(section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
        student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE, PRIMARY KEY(section_id, student_id));
    """)
    existing = [r["name"] for r in con.execute("PRAGMA table_info(sessions)")]
    for column, ddl in [("simulated", "INTEGER NOT NULL DEFAULT 0"), ("section_id", "INTEGER REFERENCES sections(id)"),
                        ("room_id", "INTEGER REFERENCES rooms(id)"), ("teacher_id", "INTEGER")]:
        if column not in existing:
            con.execute("ALTER TABLE sessions ADD COLUMN %s %s" % (column, ddl))
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
    live = {"seq": 0, "source": None, "last_contact": None, "last_ok": None, "last_error": None, "last_error_at": None,
            "outputs": {}, "unmapped": {}}
    streams = {}   # session id -> {"frame", "jpeg", "window"}; one per open room
    login_failures = {}

    def now():
        return datetime.now(timezone.utc).isoformat()

    def retrain():
        con = db()
        try:
            rows = [{"id": r["id"], "samples": [s[0] for s in con.execute("SELECT image FROM samples WHERE student_id=?", (r["id"],))]}
                    for r in con.execute("SELECT id FROM students WHERE id IN (SELECT student_id FROM samples) ORDER BY id")]
            cv.train(rows)
        finally:
            con.close()

    retrain()

    # ------------------------------------------------------------------ auth
    def load_user(uid):
        con = db()
        try:
            row = con.execute("SELECT id, username, name, role FROM users WHERE id=?", (uid,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def has_users():
        con = db()
        try:
            return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
        finally:
            con.close()

    def csrf_token():
        if "csrf" not in cookie:
            cookie["csrf"] = secrets.token_urlsafe(32)
        return cookie["csrf"]

    def is_admin():
        return g.role in ("admin", "machine")

    def roles(*allowed):
        """API guard. The API-key holder (CiRA, scripts, tests) may call everything."""
        def wrap(view):
            @wraps(view)
            def inner(*args, **kwargs):
                if g.role != "machine" and g.role not in allowed:
                    abort(403, "ไม่มีสิทธิ์ใช้งานส่วนนี้")
                return view(*args, **kwargs)
            return inner
        return wrap

    @app.before_request
    def guard():
        # Reject DNS rebinding and cross-origin access, including pages which embed the CSRF token.
        if request.host.split(":")[0] not in {"localhost", "127.0.0.1"}:
            return jsonify(error="Localhost only"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin request rejected"), 403
        g.user = g.role = None
        key = request.headers.get("X-API-Key", "")
        if key and secrets.compare_digest(key, token):
            g.user, g.role = MACHINE, "machine"
        elif cookie.get("uid"):
            user = load_user(cookie["uid"])
            if user:
                g.user, g.role = user, user["role"]
            else:
                cookie.clear()
        if request.path.startswith("/api/"):
            if g.role is None:
                return jsonify(error="ต้องเข้าสู่ระบบ หรือส่ง X-API-Key"), 401
            if g.role != "machine" and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), cookie.get("csrf", "-")):
                return jsonify(error="CSRF token ไม่ถูกต้อง โหลดหน้าใหม่แล้วลองอีกครั้ง"), 403

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

    def safe_next(value):
        return value if isinstance(value, str) and value.startswith("/") and not value.startswith("//") else "/"

    def sign_in(user):
        cookie.clear()
        cookie.permanent = True
        cookie["uid"] = user["id"]
        cookie["csrf"] = secrets.token_urlsafe(32)

    def check_password(value):
        if not isinstance(value, str) or not 8 <= len(value) <= 128:
            raise ValueError("รหัสผ่านต้องยาว 8–128 ตัวอักษร")
        return value

    def hash_password(value):
        return generate_password_hash(check_password(value), method=app.config["PASSWORD_METHOD"])

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not has_users():
            return redirect("/setup")
        if request.method == "GET":
            return render_template("login.html", error=None, next=safe_next(request.args.get("next")), username="")
        username = request.form.get("username", "").strip()
        nxt = safe_next(request.form.get("next"))
        count, until = login_failures.get(username.lower(), (0, 0))
        if until > time.time():
            return render_template("login.html", error="ลองผิดหลายครั้ง รอ 1 นาทีแล้วลองใหม่", next=nxt, username=username), 429
        con = db()
        try:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        finally:
            con.close()
        if not row or not check_password_hash(row["password_hash"], request.form.get("password", "")):
            count += 1
            login_failures[username.lower()] = (count, time.time() + 60 if count >= 5 else 0)
            return render_template("login.html", error="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", next=nxt, username=username), 401
        login_failures.pop(username.lower(), None)
        sign_in(row)
        return redirect(nxt)

    @app.post("/logout")
    def logout():
        if secrets.compare_digest(request.form.get("csrf", ""), cookie.get("csrf", "-")):
            cookie.clear()
        return redirect("/login")

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """First run only: create the first admin account."""
        if has_users():
            return redirect("/login")
        if request.method == "GET":
            return render_template("setup.html", error=None, form={})
        form = request.form
        try:
            username = form.get("username", "").strip()
            if not USERNAME.fullmatch(username):
                raise ValueError("ชื่อผู้ใช้ใช้ได้ A-Z, 0-9, _ . - ยาว 3–30 ตัว")
            name = required_text(form.get("name"), "ชื่อที่แสดง")
            if form.get("password") != form.get("confirm"):
                raise ValueError("รหัสผ่านสองช่องไม่ตรงกัน")
            password_hash = hash_password(form.get("password"))
        except ValueError as error:
            return render_template("setup.html", error=str(error), form=form), 400
        with lock:
            con = db()
            try:
                with con:
                    if con.execute("SELECT 1 FROM users").fetchone():
                        return redirect("/login")
                    uid = con.execute("INSERT INTO users(username,name,role,password_hash,created_at) VALUES(?,?,?,?,?)",
                                      (username, name, "admin", password_hash, now())).lastrowid
            finally:
                con.close()
        sign_in({"id": uid})
        return redirect("/admin/sections")

    def page_view(template, name, allowed):
        def view(**kwargs):
            if g.role is None:
                return redirect("/login?next=" + quote(request.full_path.rstrip("?")) if has_users() else "/setup")
            if g.role != "machine" and g.role not in allowed:
                return redirect("/")
            return render_template(template, page=name, user=g.user, csrf=csrf_token(), **kwargs)
        return view

    for path, (template, name, allowed) in PAGES.items():
        app.add_url_rule(path, "page_" + name, page_view(template, name, allowed))
    app.add_url_rule("/admin/sections/<int:section_id>", "page_section", page_view("section.html", "sections", ("admin",)))

    # ------------------------------------------------------------------ helpers
    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("ต้องส่ง JSON object")
        return value

    def required_text(value, name, limit=100):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(name + " ไม่ถูกต้อง")
        return value.strip()

    def optional_text(value, limit=100):
        return value.strip()[:limit] if isinstance(value, str) else ""

    def code_text(value, name, limit=20):
        value = required_text(value, name, limit)
        if not CODE.fullmatch(value):
            raise ValueError(name + " ใช้ได้เฉพาะ A-Z, 0-9, ช่องว่าง _ . -")
        return value

    def student_id(value):
        value = required_text(value, "รหัสนักศึกษา", 30)
        if not roster.STUDENT_ID.fullmatch(value):
            raise ValueError("รหัสใช้ได้เฉพาะ A-Z, 0-9, _ และ -")
        return value

    def function_name(value):
        if not isinstance(value, str) or not FUNCTION_NAME.fullmatch(value):
            raise ValueError("ชื่อ function ต้องเป็น A-Z, 0-9, ช่องว่าง _ . - ไม่เกิน 40 ตัว")
        return value

    def get_session(con, sid):
        row = con.execute(SESSION_SELECT + " WHERE s.id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def open_sessions(con):
        return [dict(r) for r in con.execute(SESSION_SELECT + " WHERE s.ended_at IS NULL ORDER BY s.id DESC")]

    def can_view(sess):
        if is_admin():
            return True
        return g.role == "teacher" and g.user["id"] in (sess["teacher_id"], sess["section_teacher_id"])

    def pick_session(con, requested=None):
        """The session a page or CiRA call is about: the one asked for, else the user's latest open one."""
        if requested is not None:
            sess = get_session(con, requested)
            if not sess or not can_view(sess):
                abort(404, "ไม่พบคาบเรียน")
            return sess
        mine = [s for s in open_sessions(con) if can_view(s)]
        return mine[0] if mine else None

    def session_arg():
        value = request.args.get("session")
        if value in (None, ""):
            return None
        if not value.isdigit():
            raise ValueError("session ต้องเป็นตัวเลข")
        return int(value)

    def roster_rows(con, sess):
        """Students who belong to this session: its section's roster, or everyone for ad-hoc sessions."""
        if sess and sess["section_id"]:
            return con.execute("""SELECT st.id, st.name FROM section_students ss JOIN students st ON st.id=ss.student_id
                WHERE ss.section_id=? ORDER BY st.id""", (sess["section_id"],)).fetchall()
        return con.execute("SELECT id, name FROM students ORDER BY id").fetchall()

    def in_roster(con, sess, sid):
        if sess["section_id"]:
            return con.execute("SELECT 1 FROM section_students WHERE section_id=? AND student_id=?", (sess["section_id"], sid)).fetchone()
        return con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone()

    def get_section(con, section_id):
        row = con.execute("""SELECT sec.*, r.code AS room_code, r.name AS room_name, u.name AS teacher_name,
            (SELECT COUNT(*) FROM section_students WHERE section_id=sec.id) AS students,
            (SELECT COUNT(DISTINCT ss.student_id) FROM section_students ss JOIN samples sa ON sa.student_id=ss.student_id
                WHERE ss.section_id=sec.id) AS with_faces
            FROM sections sec JOIN rooms r ON r.id=sec.room_id LEFT JOIN users u ON u.id=sec.teacher_id
            WHERE sec.id=?""", (section_id,)).fetchone()
        return dict(row) if row else None

    def section_for(con, section_id, write=False):
        sec = get_section(con, section_id) if type(section_id) is int else None
        if not sec or (not is_admin() and (write or sec["teacher_id"] != g.user["id"])):
            abort(404, "ไม่พบเซ็ค")
        return sec

    # ------------------------------------------------------------------ users
    @app.get("/api/me")
    def me():
        return jsonify(user=g.user)

    @app.post("/api/me/password")
    @roles("admin", "teacher")
    def change_password():
        if g.role == "machine":
            raise ValueError("API key ไม่มีรหัสผ่าน")
        data = body()
        con = db()
        try:
            row = con.execute("SELECT password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
            if not row or not check_password_hash(row["password_hash"], data.get("current") or ""):
                raise ValueError("รหัสผ่านปัจจุบันไม่ถูกต้อง")
            with con:
                con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(data.get("new")), g.user["id"]))
        finally:
            con.close()
        return jsonify(saved=True)

    @app.get("/api/users")
    @roles("admin")
    def list_users():
        con = db()
        try:
            rows = con.execute("""SELECT u.id, u.username, u.name, u.role, u.created_at,
                (SELECT COUNT(*) FROM sections WHERE teacher_id=u.id) AS sections FROM users u ORDER BY u.role, u.name""")
            return jsonify(users=[dict(r) for r in rows])
        finally:
            con.close()

    def user_fields(data, partial=False):
        fields = {}
        if not partial or "username" in data:
            username = data.get("username")
            if not isinstance(username, str) or not USERNAME.fullmatch(username.strip()):
                raise ValueError("ชื่อผู้ใช้ใช้ได้ A-Z, 0-9, _ . - ยาว 3–30 ตัว")
            fields["username"] = username.strip()
        if not partial or "name" in data:
            fields["name"] = required_text(data.get("name"), "ชื่อที่แสดง")
        if not partial or "role" in data:
            if data.get("role") not in ("admin", "teacher"):
                raise ValueError("role ต้องเป็น admin หรือ teacher")
            fields["role"] = data["role"]
        if not partial or data.get("password"):
            fields["password_hash"] = hash_password(data.get("password"))
        return fields

    @app.post("/api/users")
    @roles("admin")
    def create_user():
        fields = user_fields(body())
        with lock:
            con = db()
            try:
                with con:
                    if con.execute("SELECT 1 FROM users WHERE username=?", (fields["username"],)).fetchone():
                        return jsonify(error="ชื่อผู้ใช้นี้มีแล้ว"), 409
                    uid = con.execute("INSERT INTO users(username,name,role,password_hash,created_at) VALUES(?,?,?,?,?)",
                                      (fields["username"], fields["name"], fields["role"], fields["password_hash"], now())).lastrowid
            finally:
                con.close()
        return jsonify(id=uid), 201

    def admins_left(con, excluding):
        return con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND id<>?", (excluding,)).fetchone()[0]

    @app.patch("/api/users/<int:uid>")
    @roles("admin")
    def update_user(uid):
        fields = user_fields(body(), partial=True)
        fields.pop("username", None)
        with lock:
            con = db()
            try:
                row = con.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
                if not row:
                    abort(404, "ไม่พบผู้ใช้")
                if row["role"] == "admin" and fields.get("role") == "teacher" and not admins_left(con, uid):
                    raise ValueError("ต้องมีผู้ดูแลอย่างน้อย 1 คน")
                if fields:
                    with con:
                        con.execute("UPDATE users SET %s WHERE id=?" % ", ".join(k + "=?" for k in fields), (*fields.values(), uid))
            finally:
                con.close()
        return jsonify(saved=True)

    @app.delete("/api/users/<int:uid>")
    @roles("admin")
    def delete_user(uid):
        if g.user["id"] == uid:
            raise ValueError("ลบบัญชีของตัวเองไม่ได้")
        with lock:
            con = db()
            try:
                row = con.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
                if row and row["role"] == "admin" and not admins_left(con, uid):
                    raise ValueError("ต้องมีผู้ดูแลอย่างน้อย 1 คน")
                with con:
                    deleted = con.execute("DELETE FROM users WHERE id=?", (uid,)).rowcount
            finally:
                con.close()
        return jsonify(deleted=bool(deleted))

    # ------------------------------------------------------------------ rooms and sections
    @app.get("/api/rooms")
    @roles("admin", "teacher")
    def list_rooms():
        con = db()
        try:
            mine = None if is_admin() else g.user["id"]
            rows = con.execute("""SELECT r.id, r.code, r.name,
                (SELECT COUNT(*) FROM sections WHERE room_id=r.id AND (? IS NULL OR teacher_id=?)) AS sections
                FROM rooms r ORDER BY r.code""", (mine, mine)).fetchall()
            opened = {s["room_id"]: s for s in open_sessions(con)}
            rooms = []
            for r in rows:
                if mine is not None and not r["sections"]:
                    continue
                sess = opened.get(r["id"])
                rooms.append({**dict(r), "open_session": None if not sess else {
                    "id": sess["id"], "title": sess["title"], "teacher_name": sess["teacher_name"],
                    "started_at": sess["started_at"], "mine": can_view(sess)}})
            return jsonify(rooms=rooms)
        finally:
            con.close()

    @app.post("/api/rooms")
    @roles("admin")
    def create_room():
        data = body()
        code = code_text(data.get("code"), "รหัสห้อง")
        name = optional_text(data.get("name"))
        with lock:
            con = db()
            try:
                with con:
                    if con.execute("SELECT 1 FROM rooms WHERE code=?", (code,)).fetchone():
                        return jsonify(error="มีห้อง " + code + " แล้ว"), 409
                    rid = con.execute("INSERT INTO rooms(code,name) VALUES(?,?)", (code, name)).lastrowid
            finally:
                con.close()
        return jsonify(id=rid), 201

    @app.delete("/api/rooms/<int:rid>")
    @roles("admin")
    def delete_room(rid):
        with lock:
            con = db()
            try:
                if con.execute("SELECT 1 FROM sections WHERE room_id=?", (rid,)).fetchone():
                    return jsonify(error="ห้องนี้ยังมีเซ็คอยู่ ย้ายหรือลบเซ็คก่อน"), 409
                if con.execute("SELECT 1 FROM sessions WHERE room_id=?", (rid,)).fetchone():
                    return jsonify(error="ห้องนี้มีประวัติคาบเรียน ลบไม่ได้"), 409
                with con:
                    deleted = con.execute("DELETE FROM rooms WHERE id=?", (rid,)).rowcount
            finally:
                con.close()
        return jsonify(deleted=bool(deleted))

    @app.get("/api/sections")
    @roles("admin", "teacher")
    def list_sections():
        room = request.args.get("room") or None
        mine = None if is_admin() else g.user["id"]
        con = db()
        try:
            ids = [r[0] for r in con.execute("""SELECT id FROM sections WHERE (? IS NULL OR room_id=?) AND (? IS NULL OR teacher_id=?)
                ORDER BY course_code, section""", (room, room, mine, mine))]
            opened = {s["section_id"]: s for s in open_sessions(con) if s["section_id"]}
            sections = []
            for sid in ids:
                sec = get_section(con, sid)
                sess = opened.get(sid)
                sec["open_session"] = {"id": sess["id"], "started_at": sess["started_at"]} if sess else None
                sections.append(sec)
            return jsonify(sections=sections)
        finally:
            con.close()

    def teacher_ref(con, value):
        if value is None:
            return None
        if type(value) is not int or not con.execute("SELECT 1 FROM users WHERE id=?", (value,)).fetchone():
            raise ValueError("ไม่พบอาจารย์ที่เลือก")
        return value

    def room_ref(con, value):
        if type(value) is not int or not con.execute("SELECT 1 FROM rooms WHERE id=?", (value,)).fetchone():
            raise ValueError("เลือกห้องก่อน")
        return value

    @app.post("/api/sections")
    @roles("admin")
    def create_section():
        data = body()
        course_code = code_text(data.get("course_code"), "รหัสวิชา")
        section = code_text(data.get("section"), "เซ็ค", 10)
        course_name = optional_text(data.get("course_name"))
        with lock:
            con = db()
            try:
                room_id, teacher_id = room_ref(con, data.get("room_id")), teacher_ref(con, data.get("teacher_id"))
                with con:
                    if con.execute("SELECT 1 FROM sections WHERE course_code=? AND section=?", (course_code, section)).fetchone():
                        return jsonify(error=course_code + " เซ็ค " + section + " มีแล้ว"), 409
                    new_id = con.execute("""INSERT INTO sections(room_id,course_code,course_name,section,teacher_id,created_at)
                        VALUES(?,?,?,?,?,?)""", (room_id, course_code, course_name, section, teacher_id, now())).lastrowid
            finally:
                con.close()
        return jsonify(id=new_id), 201

    @app.get("/api/sections/<int:section_id>")
    @roles("admin", "teacher")
    def section_detail(section_id):
        con = db()
        try:
            sec = section_for(con, section_id)
            students = [dict(r) for r in con.execute("""SELECT st.id, st.name, (SELECT COUNT(*) FROM samples WHERE student_id=st.id) AS samples
                FROM section_students ss JOIN students st ON st.id=ss.student_id WHERE ss.section_id=? ORDER BY st.id""", (section_id,))]
            return jsonify(section=sec, students=students)
        finally:
            con.close()

    @app.patch("/api/sections/<int:section_id>")
    @roles("admin")
    def update_section(section_id):
        data = body()
        with lock:
            con = db()
            try:
                section_for(con, section_id, write=True)
                fields = {}
                if "room_id" in data:
                    fields["room_id"] = room_ref(con, data["room_id"])
                if "teacher_id" in data:
                    fields["teacher_id"] = teacher_ref(con, data["teacher_id"])
                if "course_name" in data:
                    fields["course_name"] = optional_text(data["course_name"])
                if fields:
                    with con:
                        con.execute("UPDATE sections SET %s WHERE id=?" % ", ".join(k + "=?" for k in fields), (*fields.values(), section_id))
            finally:
                con.close()
        return jsonify(saved=True)

    @app.delete("/api/sections/<int:section_id>")
    @roles("admin")
    def delete_section(section_id):
        with lock:
            con = db()
            try:
                section_for(con, section_id, write=True)
                if con.execute("SELECT 1 FROM sessions WHERE section_id=?", (section_id,)).fetchone():
                    return jsonify(error="เซ็คนี้มีประวัติคาบเรียน ลบไม่ได้"), 409
                with con:
                    con.execute("DELETE FROM sections WHERE id=?", (section_id,))
            finally:
                con.close()
        return jsonify(deleted=True)

    @app.post("/api/sections/<int:section_id>/students")
    @roles("admin")
    def add_section_students(section_id):
        ids = body().get("student_ids")
        if not isinstance(ids, list) or not ids or len(ids) > roster.MAX_ROWS:
            raise ValueError("เลือกนักศึกษาอย่างน้อย 1 คน")
        with lock:
            con = db()
            try:
                section_for(con, section_id, write=True)
                added = 0
                with con:
                    for sid in ids:
                        if isinstance(sid, str) and con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone():
                            added += con.execute("INSERT OR IGNORE INTO section_students VALUES(?,?)", (section_id, sid)).rowcount
            finally:
                con.close()
        return jsonify(added=added)

    @app.delete("/api/sections/<int:section_id>/students/<sid>")
    @roles("admin")
    def remove_section_student(section_id, sid):
        with lock:
            con = db()
            try:
                section_for(con, section_id, write=True)
                with con:
                    removed = con.execute("DELETE FROM section_students WHERE section_id=? AND student_id=?", (section_id, sid)).rowcount
            finally:
                con.close()
        return jsonify(removed=bool(removed))

    @app.post("/api/sections/<int:section_id>/import")
    @roles("admin")
    def import_roster(section_id):
        """CSV of student id + name. New students are created without faces; dry_run previews only."""
        data = body()
        if isinstance(data.get("file"), str):
            if len(data["file"]) > 2_800_000:
                raise ValueError("ไฟล์ใหญ่เกิน 2 MB")
            try:
                raw = base64.b64decode(data["file"], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("ไฟล์ไม่ถูกต้อง")
            text = roster.decode_csv(raw)
        elif isinstance(data.get("csv"), str) and len(data["csv"]) <= 2_000_000:
            text = data["csv"]
        else:
            raise ValueError("ต้องส่งไฟล์ CSV")
        rows, errors = roster.parse_roster(text)
        dry_run = data.get("dry_run") is True
        summary = {"rows": len(rows), "created": 0, "added": 0, "already_in_section": 0, "name_conflicts": [],
                   "errors": errors, "dry_run": dry_run}
        with lock:
            con = db()
            try:
                section_for(con, section_id, write=True)
                stamp = now()
                for sid, name in rows:
                    existing = con.execute("SELECT name FROM students WHERE id=?", (sid,)).fetchone()
                    if not existing:
                        summary["created"] += 1
                        if not dry_run:
                            con.execute("INSERT INTO students VALUES(?,?,?)", (sid, name, stamp))
                    elif existing["name"] != name:
                        summary["name_conflicts"].append({"id": sid, "current": existing["name"], "csv": name})
                    if existing and con.execute("SELECT 1 FROM section_students WHERE section_id=? AND student_id=?", (section_id, sid)).fetchone():
                        summary["already_in_section"] += 1
                    else:
                        summary["added"] += 1
                        if not dry_run:
                            con.execute("INSERT OR IGNORE INTO section_students VALUES(?,?)", (section_id, sid))
                con.commit()
            finally:
                con.close()
        return jsonify(summary)

    # ------------------------------------------------------------------ students and faces
    @app.get("/api/state")
    @roles("admin")
    def state():
        con = db()
        try:
            opened = open_sessions(con)
            current = opened[0] if opened else None
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
    @roles("admin")
    def list_students():
        con = db()
        try:
            rows = con.execute("""SELECT s.id, s.name, s.created_at,
                (SELECT COUNT(*) FROM samples WHERE student_id=s.id) AS samples,
                (SELECT COUNT(*) FROM attendance WHERE student_id=s.id) AS sessions_attended,
                (SELECT group_concat(sec.course_code || '/' || sec.section, ', ') FROM section_students ss
                    JOIN sections sec ON sec.id=ss.section_id WHERE ss.student_id=s.id) AS sections
                FROM students s ORDER BY s.id""")
            return jsonify(students=[dict(r) for r in rows])
        finally:
            con.close()

    def enrollment_face(image):
        """(box, face, width, height) of the one face to enroll in a photo, or ValueError with the reason."""
        decoded = cv.decode(image)
        finder = getattr(cv, "enroll_faces", cv.faces)
        box, face = pick_enrollment_face(finder(decoded))
        return box, face, decoded.shape[1], decoded.shape[0]

    def face_samples(data):
        """Usable samples from 3–8 photos. Photos without exactly one clear face are skipped;
        fewer than 3 usable photos is an error that names the photos to retake."""
        if data.get("consent") is not True:
            raise ValueError("ต้องยินยอมลงทะเบียนใบหน้าก่อน")
        images = data.get("images")
        if not isinstance(images, list) or not 3 <= len(images) <= 8:
            raise ValueError("ต้องใช้ 3–8 ภาพที่เห็นใบหน้าเพียงคนเดียว")
        samples, problems = [], []
        for number, image in enumerate(images, 1):
            try:
                samples.append(enrollment_face(image)[1].tobytes())
            except ValueError as error:
                problems.append((number, str(error)))
        if len(samples) < 3:
            detail = " · ".join("ภาพที่ %d: %s" % p for p in problems)
            raise ValueError("ใช้ได้ %d ภาพ ต้องมีอย่างน้อย 3 ภาพ ถ่ายใหม่แทน %s" % (len(samples), detail))
        return samples, [n for n, _ in problems]

    @app.post("/api/faces/check")
    @roles("admin")
    def check_face():
        """Instant feedback for the enrollment camera: is there exactly one clear face?"""
        try:
            (x, y, w, h), _, width, height = enrollment_face(body().get("image"))
        except ValueError as error:
            return jsonify(ok=False, reason=str(error))
        return jsonify(ok=True, box=[x / width, y / height, (x + w) / width, (y + h) / height])

    @app.post("/api/students")
    @roles("admin")
    def enroll():
        data = body()
        sid = student_id(data.get("id"))
        name = required_text(data.get("name"), "ชื่อ", 100)
        samples, skipped = face_samples(data)
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
        return jsonify(id=sid, samples=len(samples), skipped=skipped), 201

    @app.post("/api/students/<sid>/faces")
    @roles("admin")
    def replace_faces(sid):
        """Face photos for a student who came from a CSV roster (replaces any older photos)."""
        samples, skipped = face_samples(body())
        with lock:
            con = db()
            try:
                if not con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone():
                    abort(404, "ไม่พบนักศึกษา")
                with con:
                    con.execute("DELETE FROM samples WHERE student_id=?", (sid,))
                    con.executemany("INSERT INTO samples VALUES(?,?)", [(sid, s) for s in samples])
                retrain()
            finally:
                con.close()
        return jsonify(id=sid, samples=len(samples), skipped=skipped)

    @app.delete("/api/students/<sid>")
    @roles("admin")
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

    # ------------------------------------------------------------------ sessions
    @app.get("/api/sessions")
    @roles("admin", "teacher")
    def list_sessions():
        con = db()
        try:
            rows = [dict(r) for r in con.execute(SESSION_SELECT.replace("SELECT s.*,", """SELECT s.*,
                (SELECT COUNT(*) FROM observations WHERE session_id=s.id) AS frames,
                (SELECT COUNT(*) FROM attendance WHERE session_id=s.id) AS present,""", 1) + " ORDER BY s.id DESC")]
            return jsonify(sessions=[r for r in rows if can_view(r)])
        finally:
            con.close()

    @app.post("/api/sessions")
    @roles("admin", "teacher")
    def start_session():
        data = body()
        simulated = data.get("simulated", False)
        if type(simulated) is not bool:
            raise ValueError("simulated ต้องเป็น true หรือ false")
        con = db()
        try:
            section_id = data.get("section_id")
            if section_id is not None:
                sec = section_for(con, section_id)
                title = sec["course_code"] + " · sec " + sec["section"]
                room_id = sec["room_id"]
                teacher_id = sec["teacher_id"] if g.role == "machine" else g.user["id"]
            else:
                if g.role == "teacher":
                    raise ValueError("เลือกห้องและเซ็คก่อนเริ่มคาบ")
                title = required_text(data.get("title"), "ชื่อคาบเรียน")
                room_id = None if data.get("room_id") is None else room_ref(con, data["room_id"])
                teacher_id = g.user["id"]
            with lock, con:
                busy = con.execute("SELECT title FROM sessions WHERE ended_at IS NULL AND room_id IS ?", (room_id,)).fetchone()
                if busy:
                    return jsonify(error="ห้องนี้มีคาบเปิดอยู่ (" + busy["title"] + ") ปิดคาบเดิมก่อนเริ่มคาบใหม่"), 409
                cursor = con.execute("""INSERT INTO sessions(title,started_at,simulated,section_id,room_id,teacher_id)
                    VALUES(?,?,?,?,?,?)""", (title, now(), int(simulated), section_id, room_id, teacher_id))
            return jsonify(id=cursor.lastrowid), 201
        finally:
            con.close()

    @app.post("/api/sessions/<int:sid>/close")
    @roles("admin", "teacher")
    def close_session(sid):
        con = db()
        try:
            sess = get_session(con, sid)
            if not sess or not can_view(sess):
                abort(404, "ไม่พบคาบเรียน")
            with lock, con:
                changed = con.execute("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL", (now(), sid)).rowcount
                streams.pop(sid, None)
                smoother.forget_session(sid)
            return jsonify(closed=bool(changed))
        finally:
            con.close()

    def record(session_id, identities, count, source, count_source=None, extra=None):
        con = db()
        try:
            with lock, con:
                sess = get_session(con, session_id)
                if not sess or sess["ended_at"]:
                    raise ValueError("คาบเรียนไม่ตรงกับคาบที่กำลังเปิดอยู่")
                stamp = now()
                for sid in set(identities):
                    if in_roster(con, sess, sid):
                        con.execute("INSERT OR IGNORE INTO attendance VALUES(?,?,?,?)", (session_id, sid, stamp, source))
                con.execute("INSERT INTO counts(session_id,observed_at,count,source) VALUES(?,?,?,?)", (session_id, stamp, count, count_source or source))
                if extra:
                    extra(con, stamp, sess)
        finally:
            con.close()

    @app.post("/api/recognize")
    @roles("admin", "teacher")
    def recognize():
        data = body()
        session_id = data.get("session_id")
        if type(session_id) is not int:
            raise ValueError("เลือกคาบเรียนก่อน")
        con = db()
        try:
            pick_session(con, session_id)
        finally:
            con.close()
        with lock:
            image = cv.decode(data.get("image"))
            results = []
            for box, face in cv.faces(image):
                sid, distance = cv.predict(face, threshold)
                results.append(dict(box=box, student_id=sid, distance=distance))
            record(session_id, [r["student_id"] for r in results if r["student_id"]], len(results), "local-face")
        return jsonify(faces=results, count=len(results), width=image.shape[1], height=image.shape[0])

    # ------------------------------------------------------------------ CiRA input
    @app.post("/api/cira/observations")
    @roles("admin")
    def cira_observation():
        data = body()
        count, sid = data.get("people_count"), data.get("session_id")
        if type(count) is not int or not 0 <= count <= 1000 or type(sid) is not int:
            raise ValueError("session_id และ people_count ต้องเป็นจำนวนเต็ม (0–1000 คน)")
        # Legacy count-only path. Names must be recognized by the local face pipeline.
        record(sid, [], count, "cira-person")
        return jsonify(saved=True)

    def frame_session(con, data):
        """Which open session a CiRA frame belongs to: session_id, else room code, else the only open one."""
        opened = open_sessions(con)
        requested, room = data.get("session_id"), data.get("room")
        if requested is not None:
            found = [s for s in opened if type(requested) is int and s["id"] == requested]
            if not found:
                raise ValueError("คาบเรียนไม่ตรงกับคาบที่กำลังเปิดอยู่")
            return found[0]
        if room not in (None, ""):
            if not isinstance(room, str):
                raise ValueError("room ต้องเป็นรหัสห้อง เช่น 11-501")
            found = [s for s in opened if (s["room_code"] or "").lower() == room.strip().lower()]
            if not found:
                raise ValueError("ห้อง " + room[:20] + " ยังไม่มีคาบที่เปิดอยู่")
            return found[0]
        if not opened:
            raise ValueError("ยังไม่มีคาบเรียนที่เปิดอยู่ เปิดคาบในหน้าเว็บก่อน")
        if len(opened) > 1:
            raise ValueError("มีหลายห้องเปิดคาบอยู่ ต้องส่ง room (รหัสห้อง) มาด้วย")
        return opened[0]

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
            session = frame_session(con, data)
        finally:
            con.close()
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

        def save(con, stamp, sess):
            stored_dets = [{k: d[k] for k in ("label", "behavior", "kind", "confidence", "box")} for d in detections]
            con.execute("""INSERT INTO observations(session_id,observed_at,source,function,people,attentive,inattentive,
                sleeping,phone,faces,matched,detections) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sess["id"], stamp, source, function, counts["people"], counts["attentive"], counts["inattentive"],
                         counts["sleeping"], counts["phone"], len(faces), len(matches), json.dumps(stored_dets)))
            for student, index in matches.items():
                if in_roster(con, sess, student):
                    known.append(student)
                    con.execute("""INSERT INTO student_behavior VALUES(?,?,?,1,?) ON CONFLICT(session_id,student_id,behavior)
                        DO UPDATE SET frames=frames+1, last_seen=excluded.last_seen""",
                                (sess["id"], student, detections[index]["behavior"], stamp))

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
            live.update(source=source, last_ok=time.time(), last_error=None, last_error_at=None)
            stream = streams.setdefault(session["id"], {"window": deque(maxlen=300)})
            stream.update(jpeg=encode_jpeg(decoded) if decoded is not None else None,
                          frame={"seq": live["seq"], "received_at": now(), "source": source, "function": function,
                                 "has_image": decoded is not None, "aspect": round(aspect, 4),
                                 "detections": frame_dets, "faces": faces})
            stream["window"].append((time.time(), counts))
            for student in known:
                smoother.update((session["id"], student), detections[matches[student]]["behavior"])
            for label in unmapped:
                live["unmapped"][label] = now()
            while len(live["unmapped"]) > 50:
                live["unmapped"].pop(next(iter(live["unmapped"])))
            store_output(function, {k: v for k, v in data.items() if k != "image"})
        return {"saved": True, "session_id": session["id"], "room": session["room_code"], "seq": live["seq"],
                "people": counts["people"], "counts": {b: counts[b] for b in bh.BEHAVIORS},
                "attentive_pct": bh.attentive_pct(counts),
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
    @roles("admin")
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
    @roles("admin")
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
    @roles("admin")
    def cira_outputs():
        with lock:
            outputs = list(live["outputs"].values())
        return jsonify(outputs=sorted(outputs, key=lambda o: o["received_at"], reverse=True))

    # ------------------------------------------------------------------ live output
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
            sess = pick_session(con, session_arg())
            visible = [s for s in open_sessions(con) if can_view(s)]
        finally:
            con.close()
        with lock:
            return jsonify(session=sess, open_sessions=len(visible), cira=cira_status(), user=g.user)

    @app.get("/api/live")
    @roles("admin", "teacher")
    def live_state():
        settings = config["settings"]
        con = db()
        try:
            sess = pick_session(con, session_arg())
            sid = sess["id"] if sess else None
            students = roster_rows(con, sess) if sess else []
            seen = {r["student_id"]: r for r in con.execute("SELECT student_id, seen_at, source FROM attendance WHERE session_id=?", (sid,))}
            faces = {r[0] for r in con.execute("SELECT DISTINCT student_id FROM samples")}
            tallies = {}
            for r in con.execute("SELECT student_id, behavior, frames FROM student_behavior WHERE session_id=?", (sid,)):
                tallies.setdefault(r["student_id"], {})[r["behavior"]] = r["frames"]
        finally:
            con.close()
        with lock:
            cira = cira_status()
            stream = streams.get(sid) if sess and not sess["ended_at"] else None
            frame = stream["frame"] if stream else None
            t = time.time()
            window = [c for at, c in stream["window"] if t - at <= KPI_WINDOW_SECONDS] if stream else []
            roster_out = []
            for r in students:
                frames = tallies.get(r["id"], {})
                total = sum(frames.values())
                att = seen.get(r["id"])
                state, seconds = smoother.status((sid, r["id"]))
                roster_out.append({"id": r["id"], "name": r["name"], "present": bool(att), "seen_at": att["seen_at"] if att else None,
                                   "source": att["source"] if att else None, "has_face": r["id"] in faces,
                                   "status": state, "status_seconds": seconds, "frames": total,
                                   "shares": {b: round(frames.get(b, 0) / total, 3) if total else 0 for b in bh.BEHAVIORS}})

        def mean(key):
            return round(sum(c[key] for c in window) / len(window), 1) if window else None

        pooled = {b: sum(c[b] for c in window) for b in bh.BEHAVIORS}
        kpi = {"registered": len(students), "present": sum(1 for r in roster_out if r["present"]),
               "with_faces": sum(1 for r in roster_out if r["has_face"]),
               "people": mean("people"), "attentive_pct": bh.attentive_pct(pooled) if window else None,
               "sleeping": mean("sleeping"), "inattentive": mean("inattentive"),
               "phone": mean("phone") if settings["phone_enabled"] else None,  # optional: phone
               "window_seconds": KPI_WINDOW_SECONDS, "frames_in_window": len(window)}
        return jsonify(session=sess, cira=cira, frame=frame, kpi=kpi, roster=roster_out,
                       settings={"phone_enabled": settings["phone_enabled"]})

    @app.get("/api/live/frame")
    @roles("admin", "teacher")
    def live_frame():
        con = db()
        try:
            sess = pick_session(con, session_arg())
        finally:
            con.close()
        with lock:
            stream = streams.get(sess["id"]) if sess else None
            jpeg = stream.get("jpeg") if stream else None
        if not jpeg:
            return jsonify(error="ยังไม่มีภาพจาก CiRA"), 404
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/api/settings")
    @roles("admin")
    def get_settings():
        settings = config["settings"]
        with lock:
            unmapped = [{"label": k, "last_seen": v} for k, v in live["unmapped"].items() if k not in settings["label_map"]]
        con = db()
        try:
            rooms = [r[0] for r in con.execute("SELECT code FROM rooms ORDER BY code")]
        finally:
            con.close()
        return jsonify(settings=settings, defaults=bh.DEFAULT_SETTINGS, behaviors=list(bh.TARGETS),
                       unmapped_labels=unmapped, endpoint=request.host_url + "api/cira/frame", rooms=rooms,
                       token_file=str(token_path.resolve()),
                       integrations_dir=str((Path(__file__).parent / "integrations").resolve()))

    @app.put("/api/settings")
    @roles("admin")
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

    # ------------------------------------------------------------------ reports
    def session_report(con, sid):
        sess = get_session(con, sid)
        if not sess or not can_view(sess):
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
        seen = {r["student_id"]: r for r in con.execute("SELECT student_id, seen_at, source FROM attendance WHERE session_id=?", (sid,))}
        students = []
        for r in roster_rows(con, sess):
            frames = tallies.get(r["id"], {})
            total = sum(frames.values())
            att = seen.get(r["id"])
            students.append({"id": r["id"], "name": r["name"], "present": bool(att), "seen_at": att["seen_at"] if att else None,
                             "source": att["source"] if att else None, "frames": total,
                             "shares": {b: round(frames.get(b, 0) / total, 3) if total else 0 for b in bh.BEHAVIORS}})
        phone_used = bool(totals["p"]) or any(t.get("phone") for t in tallies.values())
        summary = {"registered": len(students), "present": sum(1 for s in students if s["present"]),
                   "frames": totals["frames"], "max_sleeping": totals["max_sleeping"],
                   "avg_people": round(totals["avg_people"], 1) if totals["frames"] else None,
                   "avg_attentive_pct": bh.attentive_pct({"attentive": totals["a"] or 0, "inattentive": totals["i"] or 0,
                                                          "sleeping": totals["s"] or 0, "phone": totals["p"] or 0}),
                   "first_frame": totals["first_frame"], "last_frame": totals["last_frame"], "sources": sources}
        return {"session": sess, "summary": summary, "timeline": timeline, "students": students,
                "phone_enabled": config["settings"]["phone_enabled"] or phone_used}  # optional: phone

    @app.get("/api/sessions/<int:sid>/report")
    @roles("admin", "teacher")
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
    @roles("admin", "teacher")
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
