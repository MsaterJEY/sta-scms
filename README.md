# STA-SCMS — ระบบบริหารจัดการสโมสรนักศึกษา คณะวิทยาศาสตร์เทคโนโลยีและการเกษตร

พัฒนาตาม `เอกสารข้อกำหนดความต้องการของระบบ (SRS).md` และ `DFDandERD.md`

| ส่วน | เทคโนโลยี |
|---|---|
| Frontend | Bootstrap 5.3 + Bootstrap Icons + ฟอนต์ Sarabun (Jinja2 templates, mobile-first) |
| Backend | Python 3.12 + Flask 3 (Modular Monolith แยก blueprint ตามโมดูล) |
| Database | Cloud Firestore (firebase-admin) |
| ไฟล์ | เก็บใน Firestore แบบแบ่งชิ้น (ไม่ใช้ Firebase Storage — ใช้แผนฟรี Spark ได้) เอกสารผู้ยืม/ไฟล์คำร้องเข้ารหัสด้วย Fernet |
| Deploy | Vercel (Python Serverless) + Vercel Cron |

## โมดูล

| # | โมดูล | ไฟล์ |
|---|---|---|
| 0/6 | บัญชี รหัสนักศึกษา→สาขา/ชั้นปี ตำแหน่งตามวาระ สิทธิ์ (locked) การอนุมัติ ALL/ANY แจ้งเตือน Audit | `app/rbac.py`, `app/services.py`, `app/views/auth.py`, `app/views/admin.py` |
| 1 | รับสมัครกรรมการ/อนุกรรมการ ตรวจคุณสมบัติ สัมภาษณ์ ให้คะแนน (ล็อก) ชุดผล อนุมัติ ALL แต่งตั้ง | `app/views/recruitment.py` |
| 2 | กิจกรรม ตำแหน่งทีมงาน สมัคร ตรวจเวลาชน มอบสิทธิ์คัดกรอง อนุมัติ ANY QR เช็กชื่อ ชั่วโมง | `app/views/activities.py` |
| 3 | อุปกรณ์ (รายชิ้น/นับรวม) คำขอยืม คงเหลือตามช่วงเวลา จ่าย รับคืนบางส่วน ค่าชดใช้ ซ่อม ยืมเอง→ADMIN_VP | `app/views/equipment.py` |
| 4 | ทะเบียนลิงก์เอกสาร (Google Drive) ตามระดับการเข้าถึง — ระบบไม่เก็บไฟล์เอกสาร | `app/views/documents.py` |
| 5 | รายงานข้ามโมดูล + Chart.js | `app/views/reports.py` |
| 7 | คำร้อง นิรนาม เส้นทาง/ซ่อนผู้ถูกร้องเรียน รับเรื่อง ปิด เปิดใหม่ ส่งต่อ เปิดตัวตน SLA | `app/views/tickets.py` |
| 8 | ประกาศ (ร่างอัตโนมัติ ตั้งเวลา ปักหมุด ห้ามเผยแพร่ผลก่อนอนุมัติ) และ FAQ | `app/views/announcements.py`, `app/views/public.py` |
| — | ปุ่ม **คัดลอกข้อมูล** (TSV) + **Excel / CSV (UTF-8 BOM)** ทุกตาราง บันทึก `export_jobs` | `app/exports.py`, `app/static/js/app.js` |

## รันในเครื่อง

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scripts/seed.py --demo --admin-email admin@demo.local --admin-password demo12345
python run.py                    # http://localhost:5000
```

ถ้าไม่ตั้ง `FIREBASE_CREDENTIALS` ระบบจะใช้ไฟล์ `instance/localdb.json` แทน (เฉพาะพัฒนา)   
บัญชีตัวอย่าง (`--demo`, รหัสผ่าน `demo12345`): `406765001` นายก · `406765002` เลขานุการนายก · `406765003` อุปนายก บห. · `406765004` เลขา บห. · `406765005` หัวหน้าฝ่ายอุปกรณ์ · `406765006` หัวหน้าฝ่ายบุคลากร · `406765008` ประชาสัมพันธ์ · `406765009` อุปนายกกองวิชาการ · `406865010` นักศึกษา · `406111011` ต่างคณะ

ทดสอบอัตโนมัติ (ครอบคลุมทุกโมดูลและกฎสิทธิ์หลัก):

```bash
python tests/smoke_test.py
```

## ตั้งค่า Firebase

1. สร้างโปรเจกต์ที่ <https://console.firebase.google.com> (แผนฟรี Spark) → เปิด **Firestore Database** เลือก **Production mode** region `asia-southeast1` — **ไม่ต้องเปิด Storage**
2. ไม่จำเป็นต้องแก้ rules: Production mode ปิด client ไว้แล้ว และแอปใช้ Admin SDK ฝั่งเซิร์ฟเวอร์ซึ่งไม่ถูก rules บังคับ (ไฟล์ `firestore.rules` มีไว้อ้างอิง)
3. Project settings → Service accounts → **Generate new private key** ได้ไฟล์ JSON (ห้าม commit)
4. แปลงเป็น base64 เพื่อใส่ env:
   ```bash
   python -c "import base64;print(base64.b64encode(open('serviceAccount.json','rb').read()).decode())"
   ```

## Deploy บน GitHub + Vercel

```bash
git init
git add .
git commit -m "feat: ระบบบริหารจัดการสโมสรนักศึกษา STA-SCMS"
git branch -M main
git remote add origin https://github.com/<user>/sta-scms.git
git push -u origin main
```

1. Vercel → **Add New Project** → Import repo นี้ (Framework Preset: Other)
2. ใส่ Environment Variables (สร้างค่าด้วย `python scripts/gen_keys.py`):

| ตัวแปร | ค่า |
|---|---|
| `SECRET_KEY` | ค่าสุ่มยาว |
| `FIREBASE_CREDENTIALS` | base64 ของ service account JSON |
| `FILE_ENCRYPTION_KEY` | Fernet key |
| `SETUP_TOKEN` | ใช้เปิดหน้าตั้งค่าครั้งแรก |
| `CRON_SECRET` | Vercel Cron ส่ง `Authorization: Bearer <CRON_SECRET>` ให้อัตโนมัติ |
| `SITE_URL` | `https://<your-app>.vercel.app` |
| `SMTP_*` | (ไม่บังคับ) ส่งอีเมลแจ้งเตือน |

3. Deploy แล้วเปิด `https://<your-app>.vercel.app/setup?token=<SETUP_TOKEN>` เพื่อ seed ข้อมูลตั้งต้นและสร้างผู้ดูแลระบบคนแรก
4. ผู้ดูแลระบบลงผู้ถือตำแหน่งบอร์ดที่ **ผู้ดูแลระบบ > ผู้ถือตำแหน่งตามวาระ** (ตำแหน่ง locked ต้องมีผู้ถือครบก่อนเปิดวาระ)

Cron รายวัน (`/cron/daily`, 08:00 น. เวลาไทย) ทำงาน: เผยแพร่ประกาศที่ตั้งเวลา, แจ้งเตือนอุปกรณ์ใกล้ครบ/เกินกำหนด, ลบเอกสารผู้ยืมที่ครบอายุ, แจ้งคำร้องเกิน SLA, แจ้งชั้นปีเกินหลักสูตร

## การออกแบบข้อมูลบน Firestore

- ใช้ชื่อ collection ตาม ERD (`users`, `position_holders`, `approval_requests`, `equipment_requests`, `tickets` ฯลฯ)
- รวม `USERS` + `STUDENTS` เป็นเอกสารเดียวใน `users` (Firestore ไม่มี join) — ฟิลด์ phone/line_id/gpa ซ่อนในชั้นแอป
- ความไม่ซ้ำ (unique) ใช้ document id แบบกำหนดเอง เช่น `attendance/{session}_{user}`, `interview_scores/{app}_{criterion}_{scorer}`, `activity_applications/{position}_{user}`
- ใช้ query แบบเท่ากับเท่านั้น จึงไม่ต้องสร้าง composite index
- สถานะอนุมัติคำนวณจาก `approval_logs` รายคน, "เกินกำหนด" คำนวณจากวันคืน, คงเหลืออุปกรณ์คำนวณสด (ไม่เก็บซ้ำ)
- สิทธิ์ locked กำหนดในโค้ด (`app/rbac.py`) แก้จากหน้าระบบไม่ได้; Firestore rules ปิด client ทั้งหมดแทน DB trigger
- ไฟล์อัปโหลดเก็บใน `files/{id}` (ข้อมูลไฟล์) + `file_chunks/{id}_{n}` (ชิ้นละ ≤ 900 KB เพราะเอกสาร Firestore จำกัด 1 MiB) — ดู `app/storage.py`

## ไฟล์ที่ระบบเก็บ และโควตาแผนฟรี

| ไฟล์ | ข้อจำกัด |
|---|---|
| รูปบัตรผู้ยืม (เข้ารหัส) | ลบอัตโนมัติ 30 วันหลังปิดคำขอ |
| ไฟล์แนบคำร้อง (เข้ารหัส) | ≤ 2 MB/ไฟล์ |
| รูปอุปกรณ์ / ภาพปกประกาศ | รูปภาพเท่านั้น |
| ภาพหลักฐานกิจกรรม | รูปภาพ ไม่เกิน 5 รูปต่อกิจกรรม (`MAX_EVIDENCE_IMAGES`) |

- รูปภาพถูกย่อที่เบราว์เซอร์ (ด้านยาว ≤ 1600 px, JPEG) ก่อนอัปโหลด ขนาดสูงสุดต่อไฟล์ 2 MB (`MAX_FILE_MB`)
- เอกสารโครงการ/รายงานไม่เก็บในระบบ ให้วางลิงก์ Google Drive ในเมนูเอกสาร
- โควตา Firestore แผน Spark: เก็บได้ 1 GiB, อ่าน 50,000 ครั้ง/วัน, เขียน 20,000 ครั้ง/วัน — เปิดไฟล์ 1 ไฟล์ใช้การอ่าน 1 + จำนวนชิ้น
- แผน Spark ไม่มี backup อัตโนมัติ ควรส่งออกข้อมูลเป็นระยะ (ปุ่มส่งออก Excel/CSV ในแต่ละหน้า)

## ข้อสมมติที่ตั้งไว้ (ปรับได้)

- โครงสร้างกอง (SRS 17.2 ข้อ 3): บริหารและสัมพันธ์ / วิชาการ / กีฬา / ศิลปวัฒนธรรม / กิจกรรมเพื่อสังคม — แก้ที่ **ผู้ดูแลระบบ > กองและตำแหน่ง**
- ยืนยันตัวตนด้วยรหัสผ่าน (hash ด้วย werkzeug scrypt) ล็อกบัญชี 15 นาทีเมื่อผิด 5 ครั้ง; ลืมรหัสผ่านติดต่อผู้ดูแล (ยังไม่มี OTP/SSO)
- ระดับการเข้าถึงเอกสาร: เฉพาะผู้สร้าง / กองเจ้าของ / บอร์ด / สมาชิกทุกคน (อาจารย์ที่ปรึกษาเห็นทั้งหมด) — ควบคุมการเห็นรายการ/ลิงก์ในระบบ ส่วนสิทธิ์เปิดไฟล์จริงตั้งที่ Google Drive
- Vercel จำกัด request 4.5 MB จึงจำกัดทั้งคำขอไว้ 4 MB และต่อไฟล์ 2 MB
