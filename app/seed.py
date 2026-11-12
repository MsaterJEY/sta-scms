"""ข้อมูลตั้งต้น (seed) — รันซ้ำได้ ไม่ทับข้อมูลที่แก้แล้ว ยกเว้นตำแหน่ง/ลำดับอนุมัติที่เป็นค่าระบบ

โครงสร้างกอง/ฝ่ายยังเป็นประเด็นเปิด (SRS 17.2 ข้อ 3) จึงตั้งเป็นข้อสมมติ แก้ได้ที่หน้า ผู้ดูแลระบบ > ตำแหน่ง
"""
from .db import repo
from .utils import DEFAULT_SETTINGS, now_iso

PROGRAMS = [
    ("04", "คณิตศาสตร์", []),
    ("09", "คอมพิวเตอร์ศึกษา", []),
    ("13", "วิทยาศาสตร์ทั่วไป", []),
    ("40", "นวัตกรรมและธุรกิจอาหาร", []),
    ("50", "เคมีประยุกต์", []),
    ("52", "ชีววิทยาประยุกต์", []),
    ("53", "เทคโนโลยีการผลิตพืชและธุรกิจเกษตร", []),
    ("59", "วิทยาการคอมพิวเตอร์และเทคโนโลยีดิจิทัล", []),
    ("63", "จุลชีววิทยาทางการแพทย์และอุตสาหกรรม", []),
    ("64", "ฟิสิกส์ประยุกต์", ["ฟิสิกส์อุตสาหกรรม"]),
    ("65", "เทคโนโลยีสารสนเทศ", []),
    ("67", "การประกอบอาหารฮาลาลและโภชนาการ", []),
    ("76", "สัตวศาสตร์และธุรกิจปศุสัตว์", []),
    ("98", "พลังงานทดแทน", []),
]

LEVELS = [
    ("40", "ป.ตรี 4 ปี ภาคปกติ", 4, 0, True),
    ("10", "ป.ตรี เทียบโอน 2 ปีหลัง ภาคปกติ", 2, 2, True),
]

# (code, name) — ข้อสมมติ
DIVISIONS = [
    ("ADMIN", "กองบริหารและสัมพันธ์"),
    ("ACADEMIC", "กองวิชาการ"),
    ("SPORT", "กองกีฬา"),
    ("CULTURE", "กองศิลปวัฒนธรรม"),
    ("VOLUNTEER", "กองกิจกรรมเพื่อสังคม"),
]

# (code, name, level, division, roles, single_holder, locked_required)
POSITIONS = [
    ("PRES", "นายกสโมสร", "BOARD", None, ["BOARD", "PRES"], True, True),
    ("PRES_SEC", "เลขานุการนายก", "BOARD", None, ["BOARD", "PRES_SEC"], True, True),
    ("ADMIN_VP", "อุปนายกฝ่ายบริหารและสัมพันธ์", "BOARD", "ADMIN", ["BOARD", "DIV_VP", "ADMIN_VP"], True, True),
    ("ADMIN_SEC", "เลขาฝ่ายบริหารและสัมพันธ์", "BOARD", "ADMIN", ["BOARD", "DIV_SEC", "ADMIN_SEC"], True, True),
    ("EQUIP_HEAD", "หัวหน้าฝ่ายอุปกรณ์และสถานที่", "BOARD", "ADMIN", ["BOARD", "EQUIP_HEAD"], True, True),
    ("PERSONNEL_HEAD", "หัวหน้าฝ่ายบุคลากร", "BOARD", "ADMIN", ["BOARD", "PERSONNEL_HEAD"], True, False),
    ("PR_HEAD", "หัวหน้าฝ่ายประชาสัมพันธ์", "BOARD", "ADMIN", ["BOARD", "PR"], False, False),
    ("ACADEMIC_VP", "อุปนายกกองวิชาการ", "BOARD", "ACADEMIC", ["BOARD", "DIV_VP"], True, False),
    ("ACADEMIC_SEC", "เลขากองวิชาการ", "BOARD", "ACADEMIC", ["BOARD", "DIV_SEC"], True, False),
    ("SPORT_VP", "อุปนายกกองกีฬา", "BOARD", "SPORT", ["BOARD", "DIV_VP"], True, False),
    ("SPORT_SEC", "เลขากองกีฬา", "BOARD", "SPORT", ["BOARD", "DIV_SEC"], True, False),
    ("CULTURE_VP", "อุปนายกกองศิลปวัฒนธรรม", "BOARD", "CULTURE", ["BOARD", "DIV_VP"], True, False),
    ("CULTURE_SEC", "เลขากองศิลปวัฒนธรรม", "BOARD", "CULTURE", ["BOARD", "DIV_SEC"], True, False),
    ("VOLUNTEER_VP", "อุปนายกกองกิจกรรมเพื่อสังคม", "BOARD", "VOLUNTEER", ["BOARD", "DIV_VP"], True, False),
    ("VOLUNTEER_SEC", "เลขากองกิจกรรมเพื่อสังคม", "BOARD", "VOLUNTEER", ["BOARD", "DIV_SEC"], True, False),
    ("ADVISOR", "อาจารย์ที่ปรึกษา", "STAFF", None, ["ADVISOR"], False, False),
    # ตำแหน่งที่เปิดรับสมัคร
    ("COMM_ADMIN", "กรรมการกองบริหารและสัมพันธ์", "COMMITTEE", "ADMIN", ["COMM"], False, False),
    ("COMM_ACADEMIC", "กรรมการกองวิชาการ", "COMMITTEE", "ACADEMIC", ["COMM"], False, False),
    ("COMM_SPORT", "กรรมการกองกีฬา", "COMMITTEE", "SPORT", ["COMM"], False, False),
    ("COMM_CULTURE", "กรรมการกองศิลปวัฒนธรรม", "COMMITTEE", "CULTURE", ["COMM"], False, False),
    ("COMM_VOLUNTEER", "กรรมการกองกิจกรรมเพื่อสังคม", "COMMITTEE", "VOLUNTEER", ["COMM"], False, False),
    ("SUB_ADMIN", "อนุกรรมการกองบริหารและสัมพันธ์", "SUBCOMMITTEE", "ADMIN", ["SUBCOMM"], False, False),
    ("SUB_ACADEMIC", "อนุกรรมการกองวิชาการ", "SUBCOMMITTEE", "ACADEMIC", ["SUBCOMM"], False, False),
    ("SUB_SPORT", "อนุกรรมการกองกีฬา", "SUBCOMMITTEE", "SPORT", ["SUBCOMM"], False, False),
    ("SUB_CULTURE", "อนุกรรมการกองศิลปวัฒนธรรม", "SUBCOMMITTEE", "CULTURE", ["SUBCOMM"], False, False),
    ("SUB_VOLUNTEER", "อนุกรรมการกองกิจกรรมเพื่อสังคม", "SUBCOMMITTEE", "VOLUNTEER", ["SUBCOMM"], False, False),
]

FLOWS = [
    ("REC_FINAL", "ผลคัดเลือกกรรมการ/อนุกรรมการ", "ALL", ["ADMIN_VP", "ADMIN_SEC"]),
    ("STAFF_FINAL", "ผลคัดเลือกทีมงานกิจกรรม", "ANY", ["ADMIN_VP", "ADMIN_SEC"]),
    ("EQP_SELF", "คำขอยืมของหัวหน้าฝ่ายอุปกรณ์", "ANY", ["ADMIN_VP"]),
]

TICKET_CATEGORIES = [
    ("GENERAL", "คำร้องทั่วไป", 3, False),
    ("COMPLAINT", "ร้องเรียน", 3, True),
    ("SUGGESTION", "ข้อเสนอแนะ", 7, False),
    ("ISSUE_EQUIPMENT", "แจ้งปัญหาอุปกรณ์", 3, False),
    ("ISSUE_PLACE", "แจ้งปัญหาสถานที่", 3, False),
    ("ISSUE_ACTIVITY", "แจ้งปัญหากิจกรรม", 3, False),
]

ANN_CATEGORIES = [("NEWS", "ข่าว"), ("NOTICE", "ประกาศ"), ("RECRUIT", "รับสมัคร"), ("ACTIVITY", "กิจกรรม")]
FAQ_CATEGORIES = [("GENERAL", "ทั่วไป"), ("EQUIPMENT", "ยืม-คืนอุปกรณ์"), ("RECRUIT", "การรับสมัคร"), ("TICKET", "คำร้อง")]
EQP_CATEGORIES = [("AV", "เครื่องเสียง/ภาพ"), ("TENT", "เต็นท์/โต๊ะ/เก้าอี้"), ("SPORT", "อุปกรณ์กีฬา"), ("OTHER", "อื่น ๆ")]


def run_seed(year=2569):
    r = repo()
    for code, name, former in PROGRAMS:
        if not r.get("programs", code):
            r.set("programs", code, {"code": code, "name_th": name, "former_names": former, "faculty_code": "SCI",
                                     "is_science_faculty": True, "admitting_until": None})
    for code, name, dur, offset, sci in LEVELS:
        if not r.get("student_level_codes", code):
            r.set("student_level_codes", code, {"code": code, "name": name, "duration_years": dur,
                                                "start_year_offset": offset, "is_sci_eligible": sci})
    for code, name in DIVISIONS:
        if not r.get("divisions", code):
            r.set("divisions", code, {"code": code, "name": name})
    for code, name, level, div, roles, single, locked in POSITIONS:
        if not r.get("positions", code):
            r.set("positions", code, {"code": code, "name": name, "level": level, "division_id": div,
                                      "roles": roles, "single_holder": single, "locked_required": locked})
    for code, name, mode, roles in FLOWS:
        r.set("approval_flows", code, {"code": code, "name": name, "mode": mode, "approver_roles": roles})
    for code, name, sla, sens in TICKET_CATEGORIES:
        if not r.get("ticket_categories", code):
            r.set("ticket_categories", code, {"name": name, "sla_days": sla, "is_sensitive": sens, "is_active": True})
    for code, name in ANN_CATEGORIES:
        r.set("announcement_categories", code, {"code": code, "name": name})
    for code, name in FAQ_CATEGORIES:
        if not r.get("faq_categories", code):
            r.set("faq_categories", code, {"name": name})
    for code, name in EQP_CATEGORIES:
        if not r.get("equipment_categories", code):
            r.set("equipment_categories", code, {"name": name, "is_active": True})
    if not r.get("academic_years", str(year)):
        r.set("academic_years", str(year), {"name": str(year), "start_date": f"{year - 543}-06-01",
                                            "end_date": f"{year - 542}-05-31", "is_current": True})
    if not r.get("system_settings", "main"):
        s = dict(DEFAULT_SETTINGS)
        s["current_academic_year"] = year
        r.set("system_settings", "main", s)
    r.set("system_settings", "seed_info", {"seeded_at": now_iso()})
