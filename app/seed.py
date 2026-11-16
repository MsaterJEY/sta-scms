"""ข้อมูลตั้งต้น (seed) — รันซ้ำได้ ไม่ทับข้อมูลที่แก้แล้ว ยกเว้นตำแหน่ง/ลำดับอนุมัติที่เป็นค่าระบบ

โครงสร้างกอง/ตำแหน่งใช้ผังบอร์ด 69 ตาม SRS v3 แก้ได้ที่หน้า ผู้ดูแลระบบ > กองและตำแหน่ง
ระบบที่ติดตั้งก่อน v3 ให้กด "ย้ายเป็นผังบอร์ด 69 (v3)" ที่หน้าเดียวกัน (migrate_v3)
"""
from .db import repo
from .rbac import roles_for
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

# ผังบอร์ด 69 (SRS v3 หัวข้อ 2) — ปรับได้รายปีที่หน้า ผู้ดูแลระบบ > กองและตำแหน่ง
DIVISIONS = [
    ("ADMIN", "กองบริหารและสัมพันธ์"),
    ("ACTIVITY", "กองกิจกรรม"),
    ("PR", "กองประชาสัมพันธ์"),
    ("POLICY", "กองแผนและนโยบาย"),
    # กองเลขา: กรรมการ/อนุกรรมการฝ่ายเอกสาร (ไม่มีตำแหน่งระดับบอร์ดของกองเอง)
    ("SECRETARY", "กองเลขา"),
]

# (code, name, division, function_code, parent, single_holder, locked_required) — ระดับ BOARD ทั้งหมด 23 ตำแหน่ง
BOARD_CHART = [
    ("PRES", "นายกสโมสร", None, "PRES", None, True, True),
    ("PRES_SEC", "เลขานุการนายก", None, "PRES_SEC", "PRES", True, True),
    ("TREASURER", "เหรัญญิก", None, "", "PRES", True, False),
    ("ADMIN_VP", "อุปนายกฝ่ายบริหารและสัมพันธ์", "ADMIN", "ADMIN_VP", "PRES", True, True),
    ("ADMIN_SEC", "เลขาฝ่ายบริหารและสัมพันธ์", "ADMIN", "ADMIN_SEC", "ADMIN_VP", True, True),
    ("PERSONNEL_HEAD", "หัวหน้าฝ่ายบุคลากร", "ADMIN", "PERSONNEL_HEAD", "ADMIN_VP", True, False),
    ("FUNDRAISING_HEAD", "หัวหน้าฝ่ายจัดหาทุน", "ADMIN", "", "ADMIN_VP", True, False),
    ("EQUIP_HEAD", "หัวหน้าฝ่ายอุปกรณ์และสถานที่", "ADMIN", "EQUIP_HEAD", "ADMIN_VP", True, True),
    ("WELFARE_HEAD", "หัวหน้าฝ่ายสวัสดิการและพยาบาล", "ADMIN", "", "ADMIN_VP", True, False),
    ("ACT_VP", "อุปนายกฝ่ายกิจกรรม", "ACTIVITY", "DIV_VP", "PRES", True, False),
    ("ACT_SEC", "เลขาฝ่ายกิจกรรม", "ACTIVITY", "DIV_SEC", "ACT_VP", True, False),
    ("ACADEMIC_HEAD", "หัวหน้าฝ่ายวิชาการและพิธีกร", "ACTIVITY", "UNIT_HEAD", "ACT_VP", True, False),
    ("SPORT_HEAD", "หัวหน้าฝ่ายกีฬาและนันทนาการ", "ACTIVITY", "UNIT_HEAD", "ACT_VP", True, False),
    ("VOLUNTEER_HEAD", "หัวหน้าฝ่ายจิตอาสา", "ACTIVITY", "", "ACT_VP", True, False),
    ("CULTURE_HEAD", "หัวหน้าฝ่ายวัฒนธรรม", "ACTIVITY", "UNIT_HEAD", "ACT_VP", True, False),
    ("PR_VP", "อุปนายกฝ่ายประชาสัมพันธ์", "PR", "PR_VP", "PRES", True, False),
    ("PR_SEC", "เลขาฝ่ายประชาสัมพันธ์", "PR", "PR_SEC", "PR_VP", True, False),
    ("MEDIA_HEAD", "หัวหน้าฝ่ายสื่อ", "PR", "MEDIA_HEAD", "PR_VP", True, False),
    ("CONTENT_TEAM", "ทีม Reels/คอนเทนต์", "PR", "PR_DRAFT", "MEDIA_HEAD", False, False),
    ("PHOTO_HEAD", "หัวหน้าฝ่ายรูปภาพ", "PR", "PR_DRAFT", "MEDIA_HEAD", True, False),
    ("VIDEO_HEAD", "หัวหน้าฝ่ายวิดีโอ", "PR", "PR_DRAFT", "MEDIA_HEAD", True, False),
    ("BANNER_HEAD", "หัวหน้าฝ่ายแบนเนอร์", "PR", "PR_DRAFT", "MEDIA_HEAD", True, False),
    ("POLICY_VP", "อุปนายกฝ่ายแผนและนโยบาย", "POLICY", "DIV_VP", "PRES", True, False),
]
# ตำแหน่งว่างที่ใช้ผู้สำรอง (SRS v3 หัวข้อ 3)
FALLBACKS = {"PERSONNEL_HEAD": ["ADMIN_VP", "ADMIN_SEC"]}


def chart_positions(year):
    """ตำแหน่งทั้งหมดของผัง: บอร์ด (ผูกปีการศึกษา) + อาจารย์ที่ปรึกษา/กรรมการ/อนุกรรมการ (ใช้ทุกปี)"""
    out = []

    def add(code, name, level, div, fn, parent, single, locked, ay):
        fb = FALLBACKS.get(code, [])
        out.append((code, {"code": code, "name": name, "level": level, "division_id": div, "function_code": fn,
                           "roles": roles_for(level, fn), "parent_id": parent, "single_holder": single,
                           "locked_required": locked, "academic_year": ay, "fallback_functions": fb,
                           "has_fallback": bool(fb)}))

    for code, name, div, fn, parent, single, locked in BOARD_CHART:
        add(code, name, "BOARD", div, fn, parent, single, locked, year)
    add("ADVISOR", "อาจารย์ที่ปรึกษา", "STAFF", None, "ADVISOR", None, False, False, None)
    for code, name in DIVISIONS:
        add(f"COMM_{code}", "กรรมการ" + name, "COMMITTEE", code, "", None, False, False, None)
        add(f"SUB_{code}", "อนุกรรมการ" + name, "SUBCOMMITTEE", code, "", None, False, False, None)
    return out


FLOWS = [
    ("REC_FINAL", "ผลคัดเลือกกรรมการ/อนุกรรมการ", "ALL", ["ADMIN_VP", "ADMIN_SEC"]),
    ("STAFF_FINAL", "ผลคัดเลือกทีมงานกิจกรรม", "ANY", ["ADMIN_VP", "ADMIN_SEC"]),  # ใช้เมื่อเปิดโมดูลกิจกรรมเท่านั้น
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
    """อ่านรายการที่มีอยู่ครั้งเดียวต่อคอลเลกชัน แล้วเขียนทั้งหมดใน batch เดียว (กัน timeout บน serverless)"""
    r = repo()
    have = {}
    items = []

    def put(col, doc_id, data, overwrite=False):
        if col not in have:
            have[col] = {d["id"] for d in r.all(col)}
        if overwrite or str(doc_id) not in have[col]:
            items.append((col, str(doc_id), data))

    for code, name, former in PROGRAMS:
        put("programs", code, {"code": code, "name_th": name, "former_names": former, "faculty_code": "SCI",
                               "is_science_faculty": True, "admitting_until": None})
    for code, name, dur, offset, sci in LEVELS:
        put("student_level_codes", code, {"code": code, "name": name, "duration_years": dur,
                                          "start_year_offset": offset, "is_sci_eligible": sci})
    for code, name in DIVISIONS:
        put("divisions", code, {"code": code, "name": name})
    for code, data in chart_positions(year):
        put("positions", code, data)
    for code, name, mode, roles in FLOWS:
        put("approval_flows", code, {"code": code, "name": name, "mode": mode, "approver_roles": roles}, overwrite=True)
    for code, name, sla, sens in TICKET_CATEGORIES:
        put("ticket_categories", code, {"name": name, "sla_days": sla, "is_sensitive": sens, "is_active": True})
    for code, name in ANN_CATEGORIES:
        put("announcement_categories", code, {"code": code, "name": name}, overwrite=True)
    for code, name in FAQ_CATEGORIES:
        put("faq_categories", code, {"name": name})
    for code, name in EQP_CATEGORIES:
        put("equipment_categories", code, {"name": name, "is_active": True})
    put("academic_years", str(year), {"name": str(year), "start_date": f"{year - 543}-06-01",
                                      "end_date": f"{year - 542}-05-31", "is_current": True})
    s = dict(DEFAULT_SETTINGS)
    s["current_academic_year"] = year
    put("system_settings", "main", s)
    put("system_settings", "seed_info", {"seeded_at": now_iso()}, overwrite=True)
    r.set_many(items)


# ---------- ย้ายระบบที่สร้างไว้ก่อน SRS v3 (หัวข้อ 9) ----------
OLD_DIVISIONS = {"ACADEMIC": "วิชาการ", "SPORT": "กีฬา", "CULTURE": "ศิลปวัฒนธรรม", "VOLUNTEER": "กิจกรรมเพื่อสังคม"}
# ผู้ถือตำแหน่งของกองเดิม -> ฝ่ายชื่อเดียวกันในกองกิจกรรม (เฉพาะที่ชื่อตรงกัน)
HOLDER_MAP = {"ACADEMIC_VP": "ACADEMIC_HEAD", "SPORT_VP": "SPORT_HEAD", "CULTURE_VP": "CULTURE_HEAD"}
HOLDER_MAP.update({f"{k}_{d}": f"{k}_ACTIVITY" for k in ("COMM", "SUB") for d in OLD_DIVISIONS})
OLD_POSITIONS = (["PR_HEAD"] + [f"{d}_{k}" for d in OLD_DIVISIONS for k in ("VP", "SEC")]
                 + [f"{k}_{d}" for k in ("COMM", "SUB") for d in OLD_DIVISIONS])


def migrate_v3(year):
    """ย้ายข้อมูลเดิมเป็นโครงสร้าง SRS v3 รันซ้ำได้ คืนสรุปสิ่งที่เปลี่ยน"""
    r = repo()
    done = {"positions": 0, "holders_moved": 0, "holders_ended": [], "division_refs": 0, "anon_tickets": 0,
            "equipment_requests": 0}
    # 1) กองและผังบอร์ด 69 (เขียนทับตำแหน่งรหัสเดียวกัน เพื่อเติม function_code/parent/fallback)
    r.set_many([("divisions", code, {"code": code, "name": name}) for code, name in DIVISIONS]
               + [("positions", code, data) for code, data in chart_positions(year)])
    done["positions"] = len(chart_positions(year))
    positions = {p["id"]: p for p in r.all("positions")}
    users = {}

    def uname(uid):
        if uid not in users:
            u = r.get("users", uid) or {}
            users[uid] = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or uid
        return users[uid]

    # 2) ผู้ถือตำแหน่งของกองเดิม
    for old in OLD_POSITIONS:
        for h in r.where("position_holders", position_id=old):
            new = HOLDER_MAP.get(old)
            target = positions.get(new) if new else None
            busy = target and target.get("single_holder") and r.where(
                "position_holders", position_id=new, academic_year=h.get("academic_year"), status="ACTIVE")
            if target and not busy:
                r.update("position_holders", h["id"], {"position_id": new, "position_code": new, "migrated_from": old})
                done["holders_moved"] += 1
            elif h.get("status") == "ACTIVE":
                r.update("position_holders", h["id"], {"status": "ENDED", "end_date": now_iso()[:10],
                                                       "end_note": "ตำแหน่งถูกยกเลิกเมื่อย้ายเป็นผังบอร์ด 69"})
                done["holders_ended"].append(f"{uname(h['user_id'])} ({(positions.get(old) or {}).get('name', old)})")
    # 3) ข้อมูลที่อ้างกองเดิม -> กองกิจกรรม (เก็บชื่อฝ่ายเดิมไว้ใน unit_name)
    for p in r.all("team_positions"):
        upd = {}
        if p.get("division_id") in OLD_DIVISIONS:
            upd.update({"division_id": "ACTIVITY", "unit_name": OLD_DIVISIONS[p["division_id"]]})
        limit = p.get("restrict_divisions") or []
        if set(limit) & set(OLD_DIVISIONS):
            upd["restrict_divisions"] = sorted({"ACTIVITY" if d in OLD_DIVISIONS else d for d in limit})
        if upd:
            r.update("team_positions", p["id"], upd)
            done["division_refs"] += 1
    for col, field in (("team_applications", "home_division_id"), ("documents", "division_id"),
                       ("activities", "division_id")):
        for d in r.all(col):
            if d.get(field) in OLD_DIVISIONS:
                r.update(col, d["id"], {field: "ACTIVITY"})
                done["division_refs"] += 1
    for old in OLD_POSITIONS:
        if old in positions:
            r.delete("positions", old)
    for d in OLD_DIVISIONS:
        r.delete("divisions", d)
    # 4) เรื่องนิรนามเดิม: ล้างตัวตนผู้ส่ง และลบบันทึกการเปิดตัวตน (หัวข้อ 5.2, 9 ข้อ 6-7)
    for t in r.where("tickets", is_anonymous=True):
        if t.get("submitter_id"):
            r.update("tickets", t["id"], {"submitter_id": None})
            for m in r.where("ticket_messages", ticket_id=t["id"], sender_type="STUDENT"):
                r.update("ticket_messages", m["id"], {"sender_id": None})
            for c in r.where("consents", purpose="TICKET", ref=t["id"]):
                r.delete("consents", c["id"])
            done["anon_tickets"] += 1
    for x in r.all("ticket_identity_reveals"):
        r.delete("ticket_identity_reveals", x["id"])
    # 5) คำขอยืม: activity_id -> event_name (ข้อความ)
    for q in r.all("equipment_requests"):
        if q.get("activity_id") and not q.get("event_name"):
            act = r.get("activities", q["activity_id"]) or {}
            r.update("equipment_requests", q["id"], {"event_name": act.get("title") or "", "activity_id": None})
            done["equipment_requests"] += 1
    r.set("system_settings", "migrations", {"v3": now_iso()}, merge=True)
    return done
