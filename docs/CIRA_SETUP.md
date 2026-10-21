# เชื่อมต่อ CiRA CORE

คู่มือฉบับเต็มพร้อมภาพ: [CIRA_GUIDE.pdf](CIRA_GUIDE.pdf) · ภาพตัวอย่างหน้าเว็บ: [WEB_PREVIEW.pdf](WEB_PREVIEW.pdf)

Repo นี้เป็น Python web app พร้อม HTTP bridge **ยังไม่ใช่ไฟล์ flow ของ CiRA CORE ที่ import แล้วใช้งานได้ทันที** โค้ดสำหรับ Python+ node ทดสอบกับตัวจำลองเท่านั้น ต้องต่อ flow และตรวจสอบบนเครื่อง CiRA จริง

## แหล่งอ้างอิง
- [CiRA CORE JavaScript Update 2025](https://archive.cira-ai.com/other/CiRA_CORE_AYC_2025_Share/0x02%20CiRA%20CORE%20JavaScript%20Update%202025.pdf) หน้า 27–28: DeepDetect มี `count`, `counts`, `objects`, `im_w`, `im_h`; แต่ละ object มี `name`, `confidence` (0–1), `x`, `y` (**จุดกึ่งกลาง**, พิกเซล), `w`, `h`
- [CiRA CORE Intro 2025](https://archive.cira-ai.com/other/CiRA_CORE_AYC_2025_Share/0x00%20CiRA%20CORE%20Intro%20AYC%202025_presetation.pdf) หน้า 14: Python+ node ใช้ `handle_request(req)` กับ `getReqData` / `makeRespData`
- Python+ และ REST node มีใน CiRA CORE รุ่น desktop ไม่มีในรุ่น browser (ciracore.cira-ai.com)

## Flow ที่ต้องจัดใน CiRA

```text
Camera → DeepDetect (โมเดลพฤติกรรม) → Python+ (cira_python_plus.py) → Debug / JavaScript / ...
                                            │  ▲
                              INPUT ────────┘  └──────── OUTPUT payload["spu"]
                                     localhost:5050
```

1. เปิด `python app.py` บนเครื่องเดียวกับ CiRA ผู้ดูแลสร้างห้อง (เช่น `11-501`) เซ็ค รายชื่อ และภาพใบหน้า แล้วอาจารย์เข้าสู่ระบบ → เลือกห้อง → เลือกเซ็ค → เริ่มคาบ
2. ดู Debug ของ DeepDetect: ชื่อคลาส, x,y เป็นจุดกึ่งกลางหรือมุม, หน่วยพิกัด, สเกล confidence
3. หน้า **ตั้งค่า CiRA**: จับคู่ชื่อคลาสกับพฤติกรรม ค่าเริ่มต้นตั้งตามเอกสาร CiRA แล้ว (จุดกึ่งกลาง, พิกเซล, 0–1)
4. วางโค้ด [`integrations/cira_python_plus.py`](../integrations/cira_python_plus.py) ใน Python+ node แก้ `REPO` ให้เป็น path ของ repo ในเครื่องนั้น และ `ROOM` เป็นรหัสห้องของกล้องตัวนั้น (เว้นว่างได้ถ้าเปิดคาบทีละห้อง)

```python
def handle_request(req):
    payload, img = getReqData(req)
    result = send_detections(payload, TOKEN_FILE, frame_bgr=img, room=ROOM, timeout=2)   # INPUT
    if result is not None:
        payload["spu"] = flow_output(result)                                  # OUTPUT
    return makeRespData(payload, img)
```

ไฟล์จริงจัดการกรณีเว็บปิดหรือส่งไม่ผ่านให้แล้ว flow จะเดินต่อเสมอ และใส่ `payload["spu"]["error"]` บอกเหตุผล

- ไม่ต้องใส่ `session_id` ระบบเลือกคาบที่เปิดอยู่ในห้อง `room` ให้ ถ้าเปิดคาบห้องเดียวไม่ต้องใส่ `room` ก็ได้
- ส่งจริงไม่เกิน 1 ครั้งต่อวินาที เฟรมที่ถูกเว้นจะได้ผลล่าสุดซ้ำใน `payload["spu"]`
- ภาพที่แนบต้องเป็นเฟรมเดียวกับผลตรวจ ย่อได้แต่ห้าม crop ภาพเก็บในหน่วยความจำเฉพาะเฟรมล่าสุด

## ใช้ FaceRec ของ CiRA เช็คชื่อ

ถ้า flow มี node **FaceRec** (เทรนหน้าด้วย FaceTrainer) วาง Python+ ต่อจาก FaceRec:

```text
WebCam → DeepDetect → FaceRec → Python+ (cira_python_plus.py) → Debug
```

- bridge ส่งผล FaceRec ไปด้วยเป็น `faces` (ชื่อ + กรอบ) เว็บจะ **ใช้ชื่อจาก FaceRec เช็คชื่อแทนการจำหน้าของเว็บ**
- ชื่อที่เทรนใน FaceTrainer ต้องเป็นรหัสนักศึกษา เช่น `66060711` หรือ `66060711_ชื่อ` และนักศึกษาต้องอยู่ในรายชื่อเซ็คที่เปิดคาบ ชื่ออื่น เช่น `unknown` ไม่นับ
- แหล่งที่มาในรายงานจะเป็น `cira-facerec` ไม่ต้องถ่ายภาพลงทะเบียนในเว็บซ้ำ
- รูปแบบ payload ของ FaceRec ยังไม่มีเอกสาร bridge จึงหาเองจากรายการที่มีชื่อ + กรอบ ถ้าเช็คชื่อไม่ขึ้น ดูหน้า "ตั้งค่า CiRA" ส่วน "ผลลัพธ์ล่าสุดจาก CiRA" ซึ่งแสดง `cira_payload` ทั้งก้อน แล้วส่งให้ทีมดูโครงสร้างจริง

ภาพลื่นขึ้น: ผลเต็มส่งวินาทีละครั้ง ระหว่างนั้นส่งเฉพาะภาพ + กรอบทุก 0.2 วินาที (`PREVIEW_INTERVAL`) ซึ่งไม่ถูกบันทึกลงรายงาน

ถ้า Debug ขึ้น `"spu": {"status": "error", ...}` ให้อ่าน `error` และ `where` เพื่อดูสาเหตุ (เช่น `REPO` ผิด, ยังไม่เปิดคาบ, เว็บไม่ได้รัน) โค้ดใน Python+ จะไม่ทำให้ service พังอีก

## สัญญา API

`POST` (หรือ `PUT`) `http://127.0.0.1:5050/api/cira/frame` พร้อม header `X-API-Key` ที่อ่านจากไฟล์ในเครื่อง อย่าฝังคีย์ลงใน flow ที่จะส่งขึ้น GitHub

รับได้สองแบบ:

```json
{"room": "11-501",
 "DeepDetect": {"im_w": 1280, "im_h": 720,
  "objects": [{"name": "sleep", "confidence": 0.82, "x": 140, "y": 165, "w": 80, "h": 90}]},
 "image": "<base64 JPEG ไม่บังคับ>"}
```

```json
{"function": "DeepDetect", "width": 1280, "height": 720,
 "detections": [{"name": "sleep", "confidence": 0.82, "x": 140, "y": 165, "w": 80, "h": 90}]}
```

ตอบกลับ (OUTPUT):

```json
{"saved": true, "session_id": 3, "seq": 41, "people": 1,
 "counts": {"attentive": 0, "inattentive": 0, "sleeping": 1, "phone": 0},
 "attentive_pct": 0.0, "faces": [], "ignored_labels": []}
```

ตัวเลขข้างบนเป็นตัวอย่างสัญญา API ไม่ใช่ผลทดลองจริง

- payload ห่อใน `{"payload": {...}}` ได้ และ key อื่นอย่าง `DeepDetectCPU` ใช้ได้ถ้ามี detector เดียว
- ถ้าถูกปฏิเสธ จะได้ HTTP 400 พร้อม `error` ภาษาไทย และแถบบนของเว็บจะแสดงเหตุผล เช่น `มีหลายห้องเปิดคาบอยู่ ต้องส่ง room (รหัสห้อง) มาด้วย`
- สรุปทั้งห้องเฉลี่ย 10 วินาทีและรายชื่อในเซ็คพร้อมสถานะ: `GET /api/live?session=<id>` (ค่า `session_id` อยู่ในคำตอบของ `/api/cira/frame`)

### เส้นทางเดิม (ยังใช้ได้)

- `POST /api/cira/observations` `{"session_id": 1, "people_count": 12}` บันทึกจำนวนคนอย่างเดียว
- `POST /api/recognize` เช็คชื่อจากภาพอย่างเดียว ไม่ตรวจพฤติกรรม

## ทดสอบจากบรรทัดคำสั่ง

```bash
python integrations/cira_bridge.py --ping
python integrations/cira_bridge.py payload.json                 # ใช้ im_w/im_h ใน payload
python integrations/cira_bridge.py payload.json --image frame.jpg
```

ไม่ commit payload หรือภาพจากห้องเรียนจริง หาก CiRA อยู่คนละเครื่อง ให้ย้าย app ไปเครื่อง CiRA ไม่ควรเปลี่ยน bind เป็น 0.0.0.0 โดยไม่มีระบบผู้ใช้/TLS

## เกณฑ์ตรวจรับบน CiRA จริง

- ตรวจ class label, รูปแบบกรอบ และ confidence scale จาก Debug แล้วตั้งค่าให้ตรง
- เทียบกรอบบนหน้าห้องเรียนกับภาพจริง
- ใน Python+ ตรวจ `type(img)` และ `img.shape` ว่าเป็นภาพ BGR และ `payload["spu"]["status"] == "ok"`
- ส่งภาพ 0 / 1 / หลายคน และเทียบจำนวนกับภาพด้วยตา
- นักศึกษาที่ลงทะเบียน: หน้าตรง ก้มหน้า (คาดว่าระบุตัวไม่ได้) และคนที่ไม่ลงทะเบียน
- ท่าที่โมเดลสับสนง่าย: ก้มเขียนหนังสือ ฟุบโต๊ะ หันคุยกัน
- ส่งซ้ำ 5 ครั้ง ตรวจว่ามี attendance เพียงรายการเดียวต่อคนต่อคาบ
- ส่งตอนยังไม่เปิดคาบ หรือหลังปิดคาบ ต้องถูกปฏิเสธและแถบบนแสดงเหตุผล
- เปิดคาบ 2 ห้องพร้อมกัน แล้วส่ง `room` ต่างกัน ข้อมูลต้องไปถูกห้อง
- เก็บผลจริงในรายงาน ไม่อ้างว่า integration ผ่านจาก API test หรือข้อมูลจำลองเพียงอย่างเดียว
