"""ฟังก์ชันช่วย: เวลา พ.ศ. รหัสนักศึกษา การตั้งค่า"""
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import repo

TZ = ZoneInfo("Asia/Bangkok")
THAI_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


# ---------- เวลา ----------
def now():
    return datetime.now(TZ).replace(microsecond=0)


def now_iso():
    return now().isoformat()


def to_iso(dt):
    return dt.astimezone(TZ).replace(microsecond=0).isoformat() if dt else None


def parse_dt(value):
    """รับ ISO string หรือค่าจาก <input type=datetime-local> คืน datetime (Asia/Bangkok)"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def form_dt(value):
    """แปลงค่าจากฟอร์มเป็น ISO string มาตรฐาน"""
    dt = parse_dt(value)
    return to_iso(dt) if dt else None


def plus_days(days):
    return to_iso(now() + timedelta(days=days))


def thai_date(value, with_time=True):
    dt = parse_dt(value)
    if not dt:
        return "-"
    dt = dt.astimezone(TZ)
    s = f"{dt.day} {THAI_MONTHS[dt.month]} {dt.year + 543}"
    if with_time:
        s += f" {dt:%H:%M} น."
    return s


def input_dt(value):
    """ISO -> ค่าใส่ <input type=datetime-local>"""
    dt = parse_dt(value)
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if dt else ""


def overlaps(a_start, a_end, b_start, b_end):
    a1, a2, b1, b2 = map(parse_dt, (a_start, a_end, b_start, b_end))
    if not all((a1, a2, b1, b2)):
        return False
    return a1 < b2 and b1 < a2


# ---------- การตั้งค่าระบบ (system_settings) ----------
DEFAULT_SETTINGS = {
    "current_academic_year": 2569,
    "allow_external_borrowers": False,
    "require_sci_borrower_doc": False,
    "ticket_sla_days": 3,
    "ticket_reopen_days": 7,
    "borrower_doc_retention_days": 30,
    "ticket_stats_min_group": 5,
    "ticket_anon_daily_limit": 3,
    "max_member_choices": 3,
    "max_concurrent_loans": 3,
    "qr_rotate_seconds": 30,
    "consent_valid_days": 730,
}


def settings():
    doc = repo().get("system_settings", "main") or {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in doc.items() if k != "id"})
    return merged


def setting(key):
    return settings().get(key, DEFAULT_SETTINGS.get(key))


# ---------- รหัสนักศึกษา (หัวข้อ 3) ----------
STUDENT_CODE_RE = re.compile(r"^\d{9}$")


def parse_student_code(code):
    """แยกรหัสนักศึกษา 9 หลักตามตำแหน่ง คืน dict หรือ None ถ้ารูปแบบผิด"""
    code = (code or "").strip()
    if not STUDENT_CODE_RE.match(code):
        return None
    level_code, yy, program_code, seq = code[0:2], code[2:4], code[4:6], code[6:9]
    admit_year = 2500 + int(yy)
    level = repo().get("student_level_codes", level_code)
    program = repo().get("programs", program_code)
    current_year = int(setting("current_academic_year"))
    year_level = None
    if level:
        year_level = current_year - admit_year + 1 + int(level.get("start_year_offset", 0))
    sci = bool(level and level.get("is_sci_eligible") and program and program.get("is_science_faculty"))
    if sci:
        account_type = "SCI"
        review = False
    elif program is None and level and level.get("is_sci_eligible"):
        # รหัสสาขาไม่พบในตาราง -> เข้าคิวตรวจ ไม่จัดเป็นต่างคณะอัตโนมัติ (AUTH-03)
        account_type = "OTHER_MRU"
        review = True
    else:
        account_type = "OTHER_MRU"
        review = False
    over_duration = bool(level and year_level and year_level > int(level.get("duration_years", 4)) + int(level.get("start_year_offset", 0)))
    return {
        "student_code": code,
        "level_code": level_code,
        "admit_year": admit_year,
        "program_code": program_code,
        "seq": seq,
        "year_level": year_level,
        "account_type": account_type,
        "needs_review": review,
        "over_duration": over_duration,
        "program_name": program.get("name_th") if program else None,
    }


def gen_code(prefix="", n=8):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(n))


def slugify(text):
    s = re.sub(r"[^\w฀-๿]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:60] or "post") + "-" + secrets.token_hex(3)


def sort_thai(items, key):
    """เรียงตามตัวอักษรไทยแบบง่าย: ข้ามสระนำ เ แ โ ใ ไ เมื่อเทียบ"""
    leading = "เแโใไ"

    def k(item):
        s = str(key(item) or "")
        out = []
        i = 0
        while i < len(s):
            if s[i] in leading and i + 1 < len(s):
                out.append(s[i + 1])
                out.append(s[i])
                i += 2
            else:
                out.append(s[i])
                i += 1
        return "".join(out)

    return sorted(items, key=k)
