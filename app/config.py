"""การตั้งค่าแอป อ่านจาก environment variables"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    # Firebase: ใส่ JSON ของ service account ทั้งก้อน หรือ base64 ของมัน
    FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")
    # ถ้าไม่ตั้ง Firebase จะใช้ไฟล์ JSON ในเครื่อง (สำหรับพัฒนาเท่านั้น)
    LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "instance", "localdb.json"))
    LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "instance", "uploads"))
    # กุญแจเข้ารหัสเอกสารผู้ยืม (Fernet key) สร้างด้วย scripts/gen_key.py
    FILE_ENCRYPTION_KEY = os.environ.get("FILE_ENCRYPTION_KEY", "")
    SETUP_TOKEN = os.environ.get("SETUP_TOKEN", "")
    CRON_SECRET = os.environ.get("CRON_SECRET", "")
    # ขนาดสูงสุดต่อไฟล์ (รูปภาพถูกย่อที่เบราว์เซอร์ก่อนส่ง)
    MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "2"))
    # ขนาดสูงสุดต่อ request — Vercel จำกัดที่ 4.5 MB
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024
    MAX_EVIDENCE_IMAGES = int(os.environ.get("MAX_EVIDENCE_IMAGES", "5"))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("VERCEL") == "1"
    WTF_CSRF_TIME_LIMIT = None
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")
    SITE_URL = os.environ.get("SITE_URL", "")
    # ข้อมูลติดต่อและช่องทางติดตามที่แสดงบนแถบบนสุดและ footer (ค่าเริ่มต้นเป็นตัวอย่าง)
    CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "smo-sta@example.com")
    CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "0-0000-0000")
    SOCIAL_FACEBOOK = os.environ.get("SOCIAL_FACEBOOK", "#")
    SOCIAL_LINE = os.environ.get("SOCIAL_LINE", "#")
    SOCIAL_INSTAGRAM = os.environ.get("SOCIAL_INSTAGRAM", "#")
