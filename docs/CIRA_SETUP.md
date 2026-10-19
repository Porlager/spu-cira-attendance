# เชื่อมต่อ CiRA CORE

Repo นี้เป็น Python web app พร้อม HTTP bridge **ยังไม่ใช่ไฟล์ flow ของ CiRA CORE ที่ import แล้วใช้งานได้ทันที** เพราะแต่ละรุ่นมีวิธีเข้าถึง payload และภาพจาก Python node ต่างกัน ต้องต่อ flow และตรวจสอบบนเครื่อง CiRA จริง

## แหล่งอ้างอิง
- [CiRA CORE JavaScript Update 2025](https://archive.cira-ai.com/other/CiRA_CORE_AYC_2025_Share/0x02%20CiRA%20CORE%20JavaScript%20Update%202025.pdf): DeepDetect มี `count`, `counts`, `objects`; แต่ละ object มี `name`, `confidence`, `x`, `y`, `w`, `h`
- [CiRA CORE Intro 2025](https://archive.cira-ai.com/other/CiRA_CORE_AYC_2025_Share/0x00%20CiRA%20CORE%20Intro%20AYC%202025_presetation.pdf): มี Python+ และ JavaScript สำหรับสร้าง application

## Flow ที่ต้องจัดใน CiRA

```text
Camera → DeepDetect (โมเดลพฤติกรรม เช่น awake / sleep / phone) → Debug ตรวจโครงสร้าง
                                                          ↓
                                     Python node → send_detections() → localhost:5050
```

1. เปิด `python app.py` บนเครื่องเดียวกับ CiRA ลงทะเบียนใบหน้า แล้วเปิดคาบในหน้า **ห้องเรียน**
2. ดู Debug ของ DeepDetect แล้วจดสามอย่าง:
   - ชื่อคลาสที่โมเดลส่งออกมา
   - `x, y` เป็นมุมซ้ายบนหรือจุดกึ่งกลาง และเป็นพิกเซลหรือสัดส่วน 0–1
   - `confidence` เป็น 0–1 หรือ 0–100
3. ไปหน้า **ตั้งค่า CiRA** จับคู่ชื่อคลาสกับพฤติกรรม และตั้งรูปแบบกรอบ/สเกลให้ตรงกับข้อ 2 แล้วกดบันทึก
4. ใน Python node เรียก:

```python
import sys
sys.path.append("/absolute/path/spu-cira-attendance/integrations")
from cira_bridge import send_detections

# payload = dict จาก DeepDetect ของรุ่นที่ติดตั้งจริง เช่น {"DeepDetect": {"objects": [...]}}
# frame   = numpy BGR เฟรมเดียวกับที่ใช้ตรวจ (ไม่บังคับ แต่ถ้าไม่ส่งจะเช็คชื่อรายคนไม่ได้)
send_detections(payload,
                token_file="/absolute/path/spu-cira-attendance/data/api-token.txt",
                frame_bgr=frame)
```

- ไม่ต้องใส่ `session_id` ระบบจะใช้คาบที่เปิดอยู่ ถ้าใส่ต้องตรงกับคาบที่เปิด
- ถ้าไม่ส่งภาพ ต้องใส่ `width=` และ `height=` ของภาพที่ใช้ตรวจ (เมื่อพิกัดเป็นพิกเซล)
- ฟังก์ชันส่งไม่เกิน 1 ครั้งต่อวินาทีต่อ function โดยอัตโนมัติ เรียกทุกเฟรมได้
- ภาพที่แนบต้องเป็นเฟรมเดียวกับผลตรวจ ย่อได้แต่ห้าม crop ไม่อย่างนั้นกรอบจะไม่ตรง

ผลของ function อื่นใน CiRA (เช่น DeepClassify) ส่งมาดูในหน้าตั้งค่าได้ด้วย `send_output("DeepClassify", result, token_file=...)` ข้อมูลนี้อยู่ในหน่วยความจำเท่านั้น ใช้ตรวจโครงสร้าง ไม่ถูกนำไปคำนวณ

## สัญญา API (กรณีไม่ใช้ Python bridge)

ส่ง HTTP POST พร้อม header `X-API-Key` ที่อ่านจากไฟล์ในเครื่อง อย่าฝังคีย์ลงใน flow ที่จะส่งขึ้น GitHub:

```http
POST http://127.0.0.1:5050/api/cira/frame
Content-Type: application/json
X-API-Key: <read from local file>

{
  "function": "DeepDetect",
  "width": 1280, "height": 720,
  "detections": [
    {"name": "sleep", "confidence": 0.82, "x": 100, "y": 120, "w": 80, "h": 90}
  ],
  "image": "<base64 JPEG ไม่บังคับ>"
}
```

ตอบกลับ:

```json
{"saved": true, "session_id": 3, "seq": 41, "people": 1,
 "counts": {"attentive": 0, "inattentive": 0, "sleeping": 1, "phone": 0},
 "attentive_pct": 0.0, "faces": [], "ignored_labels": []}
```

ตัวเลขข้างบนเป็นตัวอย่างสัญญา API ไม่ใช่ผลทดลองจริง

- `ignored_labels` คือคลาสที่ยังไม่ได้จับคู่ จะขึ้นในหน้าตั้งค่าให้กดเพิ่ม
- ถ้าถูกปฏิเสธ (เช่น ยังไม่เปิดคาบ, confidence เกินสเกล) จะได้ HTTP 400 พร้อม `error` ภาษาไทย และแถบบนของเว็บจะแสดงเหตุผล
- รับได้ไม่เกิน 300 กรอบต่อเฟรม ภาพไม่เกิน 3 MB

### เส้นทางเดิม (ยังใช้ได้)

- `POST /api/cira/observations` `{"session_id": 1, "people_count": 12}` บันทึกจำนวนคนอย่างเดียว ไม่สร้างรายชื่อ ใช้ผ่าน `send_deepdetect(...)` หรือ `python integrations/cira_bridge.py payload.json --mode count --session 1`
- `POST /api/recognize` ผ่าน `send_frame(...)` เช็คชื่อจากภาพอย่างเดียว ไม่ตรวจพฤติกรรม

## ทดสอบจากบรรทัดคำสั่ง

```bash
# ส่ง payload ที่ export จาก Debug (ไม่ commit ไฟล์จากห้องจริง)
python integrations/cira_bridge.py payload.json --image frame.jpg
python integrations/cira_bridge.py payload.json --width 1280 --height 720
# ทดสอบว่าต่อถึงกัน
python integrations/cira_bridge.py --ping
```

หาก CiRA อยู่คนละเครื่อง: ค่าเริ่มต้นไม่รองรับเครือข่าย ให้ย้าย app ไปเครื่อง CiRA ไม่ควรเปลี่ยน bind เป็น 0.0.0.0 โดยไม่มีระบบผู้ใช้/TLS

## เกณฑ์ตรวจรับบน CiRA จริง

- ตรวจ class label, รูปแบบกรอบ และ confidence scale จาก Debug แล้วตั้งค่าให้ตรง
- เทียบกรอบบนหน้าห้องเรียนกับภาพจริง ถ้าเลื่อนครึ่งกรอบ ให้สลับ "มุมซ้ายบน / จุดกึ่งกลาง"
- ส่งภาพ 0 / 1 / หลายคน และเทียบจำนวนกับภาพด้วยตา
- นักศึกษาที่ลงทะเบียน: หน้าตรง ก้มหน้า (คาดว่าระบุตัวไม่ได้) และคนที่ไม่ลงทะเบียน
- ท่าที่โมเดลสับสนง่าย: ก้มเขียนหนังสือ ฟุบโต๊ะ หันคุยกัน
- ส่งซ้ำ 5 ครั้ง ตรวจว่ามี attendance เพียงรายการเดียวต่อคนต่อคาบ
- ส่งตอนยังไม่เปิดคาบ หรือหลังปิดคาบ ต้องถูกปฏิเสธและแถบบนแสดงเหตุผล
- เก็บผลจริงในรายงาน ไม่อ้างว่า integration ผ่านจาก API test หรือข้อมูลจำลองเพียงอย่างเดียว
