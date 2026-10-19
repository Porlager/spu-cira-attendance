"""Section roster CSV import: decode, detect columns, validate. Pure functions, unit-tested."""
import csv
import io
import re

STUDENT_ID = re.compile(r"[A-Za-z0-9_-]{1,30}")
MAX_ROWS = 3000
HEADERS = {
    "id": ("รหัส", "student id", "student_id", "studentid", "id", "code"),
    "surname": ("นามสกุล", "surname", "last name", "lastname"),
    "name": ("ชื่อ", "name", "first name", "firstname", "full name", "fullname"),
}


def decode_csv(raw):
    """Excel "CSV UTF-8" or plain Thai Windows CSV (cp874 / TIS-620)."""
    for encoding in ("utf-8-sig", "cp874"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("อ่านไฟล์ไม่ได้ บันทึกจาก Excel เป็น CSV UTF-8 แล้วลองใหม่")


def _column(header, key):
    for index, cell in enumerate(header):
        text = cell.strip().lower()
        rest = text
        for word in HEADERS["surname"]:
            rest = rest.replace(word, "")
        has_name = any(word in rest for word in HEADERS["name"])
        if key == "name" and has_name:          # "ชื่อ", "ชื่อ-นามสกุล", "first name"
            return index
        if key == "surname" and rest != text and not has_name:   # "นามสกุล", "last name" alone
            return index
        if key == "id" and any(word in text for word in HEADERS["id"]):
            return index
    return None


def parse_roster(text):
    """Return (rows, errors). rows = [(student_id, name)], errors = [{"line", "error"}].

    Columns: first = student id, second = name. With a header row, columns are found by
    name instead (รหัส / ชื่อ / นามสกุล), and first name + surname are joined.
    """
    lines = [(n, [c.strip() for c in row]) for n, row in enumerate(csv.reader(io.StringIO(text)), 1) if any(c.strip() for c in row)]
    if not lines:
        raise ValueError("ไฟล์ว่าง")
    columns = {"id": 0, "name": 1, "surname": None}
    first = lines[0][1]
    if not STUDENT_ID.fullmatch(first[0]):
        found = {key: _column(first, key) for key in HEADERS}
        if found["id"] is None or found["name"] is None:
            raise ValueError("หัวตารางต้องมีคอลัมน์ รหัส และ ชื่อ หรือไม่มีหัวตารางเลย (คอลัมน์ 1 = รหัส, 2 = ชื่อ)")
        columns = found
        lines = lines[1:]
    if len(lines) > MAX_ROWS:
        raise ValueError("นำเข้าได้ไม่เกิน %d คนต่อไฟล์" % MAX_ROWS)
    rows, errors, seen = [], [], set()
    for line, cells in lines:
        def cell(key):
            index = columns[key]
            return cells[index] if index is not None and index < len(cells) else ""
        student_id = cell("id")
        name = " ".join(part for part in (cell("name"), cell("surname")) if part)
        if not STUDENT_ID.fullmatch(student_id):
            errors.append({"line": line, "error": "รหัส \"%s\" ใช้ได้เฉพาะ A-Z, 0-9, _ - ไม่เกิน 30 ตัว" % student_id[:40]})
        elif not 1 <= len(name) <= 100:
            errors.append({"line": line, "error": "ชื่อของ %s ว่างหรือยาวเกิน 100 ตัวอักษร" % student_id})
        elif student_id in seen:
            errors.append({"line": line, "error": "รหัส %s ซ้ำในไฟล์" % student_id})
        else:
            seen.add(student_id)
            rows.append((student_id, name))
    return rows, errors
