"""สิทธิ์ผูกกับตำแหน่งตามวาระ (PERM-01)

- ผู้ใช้ได้ "บทบาท" (role code) จากตำแหน่งที่ถือในปีการศึกษาปัจจุบัน + system_roles (SYS_ADMIN)
- สิทธิ์ (permission) ผูกกับบทบาท
- สิทธิ์ที่ locked กำหนดในโค้ดนี้เท่านั้น หน้าจัดการสิทธิ์แก้ไม่ได้ แม้แต่ SYS_ADMIN
- สิทธิ์ที่ไม่ locked: SYS_ADMIN เพิ่มบทบาทได้ผ่าน system_settings/permission_extra
"""
from functools import wraps

from flask import abort, g, session

from .db import repo
from .utils import now, parse_dt, setting

# code: (ชื่อ, บทบาทตั้งต้น, locked)
PERMISSIONS = {
    # เมนู "ผู้ดูแลระบบ": locked ทั้งคู่ เพิ่มบทบาทผ่านหน้าจัดการสิทธิ์ไม่ได้
    "admin.manage": ("จัดการบัญชี ตำแหน่ง ตั้งค่าระบบ ตารางสาขา", ["SYS_ADMIN"], True),
    "admin.holders": ("จัดการผู้ถือตำแหน่งตามวาระ", ["SYS_ADMIN", "ADMIN_SEC"], True),
    "audit.view": ("ดู Audit Log (ประวัติของระบบ)", ["SYS_ADMIN"], True),
    "delegation.manage": ("มอบ/ถอนสิทธิ์คัดกรองรายกิจกรรม", ["ADMIN_VP", "ADMIN_SEC"], True),
    "rec.manage": ("เปิด/ปิดรอบรับสมัครกรรมการ ตรวจคุณสมบัติ จัดชุดผล", ["PERSONNEL_HEAD"], False),
    "rec.view_grade": ("เห็นเกรดผู้สมัคร", ["PERSONNEL_HEAD"], True),
    "rec.score": ("ให้คะแนนสัมภาษณ์", ["BOARD"], False),
    "rec.approve": ("อนุมัติผลคัดเลือกกรรมการ (ALL)", ["ADMIN_VP", "ADMIN_SEC"], True),
    "rec.view_scores": ("ดูคะแนนรายคน", ["ADMIN_VP", "ADMIN_SEC"], True),
    "act.manage": ("สร้าง/จัดการกิจกรรมของกองตน", ["DIV_VP", "DIV_SEC"], False),
    "act.screen": ("คัดกรองใบสมัครทีมงาน (ต้องได้รับมอบหมาย)", ["PERSONNEL_HEAD"], False),
    "act.approve": ("อนุมัติผลคัดเลือกทีมงาน (ANY)", ["ADMIN_VP", "ADMIN_SEC"], True),
    "team.manage": ("สร้าง/จัดการกิจกรรมรับสมัครทีมงาน และพิจารณาผู้สมัคร", ["PERSONNEL_HEAD", "DIV_VP", "DIV_SEC", "SYS_ADMIN"], False),
    "team.delete": ("ลบกิจกรรม/ใบสมัคร และปักหมุดกิจกรรมรับสมัครทีมงาน", ["PERSONNEL_HEAD", "ADMIN_VP", "SYS_ADMIN"], False),
    "eqp.manage": ("จัดการอุปกรณ์ อนุมัติ จ่าย รับคืน ค่าชดใช้ ซ่อม เปิดดูเอกสารผู้ยืม", ["EQUIP_HEAD"], True),
    "eqp.self_approve": ("อนุมัติ/ยืนยันรับคืน เมื่อผู้ขอคือหัวหน้าฝ่ายอุปกรณ์", ["ADMIN_VP"], True),
    "eqp.report": ("ดูรายงานอุปกรณ์ (อ่านอย่างเดียว)", ["PRES", "ADVISOR", "EQUIP_HEAD"], False),
    "tkt.handle": ("รับเรื่อง ตอบ ปิด ส่งต่อ คำร้อง", ["ADMIN_SEC", "PRES_SEC"], True),
    "tkt.view": ("ดูคำร้องอย่างเดียว", ["ADMIN_VP"], True),
    "tkt.escalated": ("รับเรื่องที่ส่งต่อ เปิดตัวตนนิรนาม", ["PRES", "ADVISOR"], True),
    "tkt.sla_view": ("ดูแจ้งเตือนเรื่องค้างเกิน SLA", ["PRES"], False),
    "ann.manage": ("สร้าง/แก้ไข/เผยแพร่ประกาศ", ["PR"], False),
    "faq.manage": ("จัดการ FAQ", ["DIV_SEC", "PRES_SEC"], False),
    "doc.upload": ("อัปโหลดเอกสารของกอง/กิจกรรม", ["DIV_VP", "DIV_SEC"], False),
    "report.view": ("ดูรายงานข้ามโมดูล", ["PRES", "ADVISOR"], False),
}

ROLE_LABELS = {
    "STUDENT": "นักศึกษา", "COMM": "กรรมการ", "SUBCOMM": "อนุกรรมการ", "BOARD": "บอร์ดบริหาร",
    "DIV_VP": "อุปนายกประจำกอง", "DIV_SEC": "เลขาประจำกอง", "ADMIN_VP": "อุปนายกฝ่ายบริหารและสัมพันธ์",
    "ADMIN_SEC": "เลขาฝ่ายบริหารและสัมพันธ์", "EQUIP_HEAD": "หัวหน้าฝ่ายอุปกรณ์และสถานที่",
    "PERSONNEL_HEAD": "หัวหน้าฝ่ายบุคลากร", "PRES": "นายกสโมสร", "PRES_SEC": "เลขานุการนายก",
    "PR": "ฝ่ายประชาสัมพันธ์", "ADVISOR": "อาจารย์ที่ปรึกษา", "SYS_ADMIN": "ผู้ดูแลระบบ",
}


def permission_roles(perm):
    name, roles, locked = PERMISSIONS[perm]
    if locked:
        return set(roles)
    extra = (repo().get("system_settings", "permission_extra") or {}).get(perm, [])
    return set(roles) | set(extra or [])


def current_year():
    return int(setting("current_academic_year"))


def user_holdings(user_id, year=None):
    """ตำแหน่งที่ผู้ใช้ถือในวาระ (ACTIVE) พร้อมข้อมูลตำแหน่ง"""
    year = year or current_year()
    out = []
    for h in repo().where("position_holders", user_id=user_id, academic_year=year, status="ACTIVE"):
        pos = repo().get("positions", h["position_id"])
        if pos:
            h["position"] = pos
            out.append(h)
    return out


def user_roles(user):
    if not user:
        return set()
    roles = set(user.get("system_roles") or [])
    if user.get("account_type") == "SCI":
        roles.add("STUDENT")
    for h in user_holdings(user["id"]):
        roles.update(h["position"].get("roles") or [])
    return roles


def user_divisions(user, role_filter=("DIV_VP", "DIV_SEC")):
    """กองที่ผู้ใช้เป็นอุปนายก/เลขาประจำกอง"""
    divs = set()
    for h in user_holdings(user["id"]):
        if set(h["position"].get("roles") or []) & set(role_filter):
            if h["position"].get("division_id"):
                divs.add(h["position"]["division_id"])
    return divs


def holders_of_role(role, year=None):
    """รายชื่อ user_id ที่ถือบทบาทนี้ในวาระ"""
    year = year or current_year()
    pos_ids = [p["id"] for p in repo().all("positions") if role in (p.get("roles") or [])]
    users = set()
    for pid in pos_ids:
        for h in repo().where("position_holders", position_id=pid, academic_year=year, status="ACTIVE"):
            users.add(h["user_id"])
    return users


# ---------- ผู้ใช้ปัจจุบัน ----------
def current_user():
    uid = session.get("uid")
    if "user" in g and g.get("user_uid") == uid:
        return g.user
    g.user_uid = uid
    user = repo().get("users", uid) if uid else None
    if user and user.get("status") != "ACTIVE":
        user = None
    g.user = user
    g.roles = user_roles(user) if user else set()
    return user


def roles():
    current_user()
    return g.roles


def can(perm, user=None):
    if user is None:
        current_user()
        r = g.roles
    else:
        r = user_roles(user)
    return bool(r & permission_roles(perm))


def has_role(*codes):
    return bool(roles() & set(codes))


def has_delegation(user, perm, scope_id):
    """PERM-02: การมอบสิทธิ์รายกิจกรรม มีวันหมดอายุ ถอนได้"""
    for d in repo().where("delegations", to_user_id=user["id"], permission=perm, scope_id=scope_id):
        if d.get("revoked_at"):
            continue
        exp = parse_dt(d.get("expires_at"))
        if exp and exp < now():
            continue
        return True
    return False


# ---------- decorators ----------
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            from flask import redirect, request, url_for
            return redirect(url_for("auth.login", next=request.full_path))
        return fn(*a, **kw)
    return wrapper


def permission_required(*perms):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_user():
                from flask import redirect, request, url_for
                return redirect(url_for("auth.login", next=request.full_path))
            if not any(can(p) for p in perms):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def account_types_required(*types):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                from flask import redirect, request, url_for
                return redirect(url_for("auth.login", next=request.full_path))
            if u.get("account_type") not in types:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def is_board(user):
    return any(h["position"].get("level") == "BOARD" for h in user_holdings(user["id"]))
