"""สิทธิ์ผูกกับตำแหน่งตามวาระ (PERM-01) — ฉบับ SRS v3

- ตำแหน่งในผัง (เปลี่ยนได้รายปี) แยกจาก "หน้าที่ของระบบ" (function_code คงที่)
  บทบาท (role) ของตำแหน่ง = บทบาทตามระดับ (BOARD/COMM/SUBCOMM) + บทบาทของหน้าที่ที่ตำแหน่งนั้นถือ
- ผู้ใช้ได้บทบาทจากตำแหน่งที่ถือในปีการศึกษาปัจจุบัน + system_roles (SYS_ADMIN)
- สิทธิ์ (permission) ผูกกับบทบาท
- สิทธิ์ที่ locked กำหนดในโค้ดนี้เท่านั้น หน้าจัดการสิทธิ์แก้ไม่ได้ แม้แต่ SYS_ADMIN
- สิทธิ์ที่ไม่ locked: SYS_ADMIN เพิ่มบทบาทได้ผ่าน system_settings/permission_extra
- สิทธิ์สำรอง (ORG-11..13): ตำแหน่งที่ตั้ง fallback_functions ไว้และไม่มีผู้ถือในวาระ
  ผู้ถือหน้าที่สำรองใช้สิทธิ์ที่ไม่ locked ของตำแหน่งนั้นได้ และ Audit Log ระบุว่าใช้สิทธิ์สำรอง
"""
from functools import wraps

from flask import abort, has_request_context, request, session

from .db import repo
from .utils import now, parse_dt, setting

# code: (ชื่อ, บทบาทตั้งต้น, locked)
PERMISSIONS = {
    # เมนู "ผู้ดูแลระบบ": locked เพิ่มบทบาทผ่านหน้าจัดการสิทธิ์ไม่ได้
    "admin.manage": ("จัดการบัญชี ผังบอร์ด/ตำแหน่ง ตั้งค่าระบบ ตารางสาขา", ["SYS_ADMIN"], True),
    "admin.holders": ("จัดการผู้ถือตำแหน่งตามวาระ", ["SYS_ADMIN", "ADMIN_SEC"], True),
    "audit.view": ("ดู Audit Log (ประวัติของระบบ)", ["SYS_ADMIN"], True),
    "delegation.manage": ("มอบ/ถอนสิทธิ์คัดกรอง", ["ADMIN_VP", "ADMIN_SEC"], True),
    "rec.manage": ("เปิด/ปิดรอบรับสมัครกรรมการ ตรวจคุณสมบัติ จัดชุดผล", ["PERSONNEL_HEAD"], False),
    "rec.view_grade": ("เห็นเกรดผู้สมัคร", ["PERSONNEL_HEAD"], False),
    "rec.score": ("ให้คะแนนสัมภาษณ์", ["BOARD"], False),
    "rec.approve": ("อนุมัติผลคัดเลือกกรรมการ (ALL)", ["ADMIN_VP", "ADMIN_SEC"], True),
    "rec.view_scores": ("ดูคะแนนรายคน", ["ADMIN_VP", "ADMIN_SEC"], True),
    # โมดูลกิจกรรม (ปิดด้วย ACTIVITIES_ENABLED ตาม SRS v3 หัวข้อ 7)
    "act.manage": ("สร้าง/จัดการกิจกรรมของกองตน", ["DIV_VP", "DIV_SEC"], False),
    "act.screen": ("คัดกรองใบสมัครทีมงาน (ต้องได้รับมอบหมาย)", ["PERSONNEL_HEAD"], False),
    "act.approve": ("อนุมัติผลคัดเลือกทีมงาน (ANY)", ["ADMIN_VP", "ADMIN_SEC"], True),
    "team.manage": ("สร้าง/จัดการกิจกรรมรับสมัครทีมงาน และพิจารณาผู้สมัคร",
                    ["PERSONNEL_HEAD", "DIV_VP", "DIV_SEC", "UNIT_HEAD", "SYS_ADMIN"], False),
    "team.delete": ("ลบกิจกรรม/ใบสมัคร และปักหมุดกิจกรรมรับสมัครทีมงาน", ["PERSONNEL_HEAD", "ADMIN_VP", "SYS_ADMIN"], False),
    "eqp.manage": ("จัดการอุปกรณ์ อนุมัติ จ่าย รับคืน ค่าชดใช้ ซ่อม เปิดดูเอกสารผู้ยืม", ["EQUIP_HEAD"], True),
    "eqp.self_approve": ("อนุมัติ/ยืนยันรับคืน เมื่อผู้ขอคือหัวหน้าฝ่ายอุปกรณ์", ["ADMIN_VP"], True),
    "eqp.report": ("ดูรายงานอุปกรณ์ (อ่านอย่างเดียว)", ["PRES", "EQUIP_HEAD"], False),
    "tkt.handle": ("รับเรื่อง ตอบ ปิด ส่งต่อ คำร้อง", ["ADMIN_SEC", "PRES_SEC"], True),
    "tkt.view": ("ดูคำร้องอย่างเดียว", ["ADMIN_VP"], True),
    "tkt.escalated": ("รับเรื่องคำร้องที่ส่งต่อ", ["PRES"], True),
    "tkt.sla_view": ("ดูแจ้งเตือนเรื่องค้างเกิน SLA", ["PRES"], False),
    "ann.manage": ("สร้าง/แก้ไข/เผยแพร่/ตั้งเวลาประกาศ", ["PR"], False),
    "ann.draft": ("ร่างประกาศ (เผยแพร่ไม่ได้)", ["PR_DRAFT"], False),
    "member.view": ("ดูสรุปรายชื่อกรรมการ/อนุกรรมการ (อ่านอย่างเดียว)", ["BOARD", "SYS_ADMIN"], False),
    "org.manage": ("ใส่ข้อมูลโครงสร้างองค์กร (เผยแพร่ต่อสาธารณะ)", ["PRES_SEC", "ADMIN_SEC"], False),
    "faq.manage": ("จัดการ FAQ", ["DIV_SEC", "PRES_SEC"], False),
    "doc.upload": ("บันทึก/ดูเอกสารของกองตน", ["BOARD"], False),
    "report.view": ("ดูรายงานข้ามโมดูล", ["PRES"], False),
    "dashboard.advisor": ("ดู Dashboard สรุป (ตัวเลขรวมเท่านั้น)", ["ADVISOR"], False),
}

ROLE_LABELS = {
    "STUDENT": "นักศึกษา", "COMM": "กรรมการ", "SUBCOMM": "อนุกรรมการ", "BOARD": "บอร์ดบริหาร",
    "DIV_VP": "อุปนายกประจำกอง", "DIV_SEC": "เลขาประจำกอง", "ADMIN_VP": "อุปนายกฝ่ายบริหารและสัมพันธ์",
    "ADMIN_SEC": "เลขาฝ่ายบริหารและสัมพันธ์", "EQUIP_HEAD": "หัวหน้าฝ่ายอุปกรณ์และสถานที่",
    "PERSONNEL_HEAD": "หัวหน้าฝ่ายบุคลากร", "PRES": "นายกสโมสร", "PRES_SEC": "เลขานุการนายก",
    "PR": "ประชาสัมพันธ์ (เผยแพร่ประกาศ)", "PR_DRAFT": "ทีมสื่อ (ร่างประกาศ)", "UNIT_HEAD": "หัวหน้าฝ่ายในกองกิจกรรม",
    "ADVISOR": "อาจารย์ที่ปรึกษา", "SYS_ADMIN": "ผู้ดูแลระบบ",
}

# หน้าที่ของระบบ (function_code): (ชื่อ, บทบาทที่ได้) — คงที่ ไม่ขึ้นกับชื่อตำแหน่งในผังของแต่ละปี
FUNCTIONS = {
    "": ("ไม่มีหน้าที่พิเศษในระบบ", []),
    "PRES": ("นายกสโมสร", ["PRES"]),
    "PRES_SEC": ("เลขานุการนายก", ["PRES_SEC"]),
    "ADMIN_VP": ("อุปนายกฝ่ายบริหารและสัมพันธ์", ["ADMIN_VP", "DIV_VP"]),
    "ADMIN_SEC": ("เลขาฝ่ายบริหารและสัมพันธ์", ["ADMIN_SEC", "DIV_SEC"]),
    "PERSONNEL_HEAD": ("หัวหน้าฝ่ายบุคลากร", ["PERSONNEL_HEAD"]),
    "EQUIP_HEAD": ("หัวหน้าฝ่ายอุปกรณ์และสถานที่", ["EQUIP_HEAD"]),
    "DIV_VP": ("อุปนายกประจำกอง", ["DIV_VP"]),
    "DIV_SEC": ("เลขาประจำกอง", ["DIV_SEC"]),
    "PR_VP": ("อุปนายกฝ่ายประชาสัมพันธ์ (เผยแพร่ประกาศ)", ["DIV_VP", "PR"]),
    "PR_SEC": ("เลขาฝ่ายประชาสัมพันธ์ (เผยแพร่ประกาศ)", ["DIV_SEC", "PR"]),
    "MEDIA_HEAD": ("หัวหน้าฝ่ายสื่อ (เผยแพร่ประกาศ)", ["PR"]),
    "PR_DRAFT": ("ทีมสื่อ (ร่างประกาศ)", ["PR_DRAFT"]),
    "UNIT_HEAD": ("หัวหน้าฝ่ายในกองกิจกรรม (จัดการรับสมัครทีมงาน)", ["UNIT_HEAD"]),
    "ADVISOR": ("อาจารย์ที่ปรึกษา", ["ADVISOR"]),
}
LEVEL_ROLES = {"BOARD": "BOARD", "COMMITTEE": "COMM", "SUBCOMMITTEE": "SUBCOMM"}


def roles_for(level, function_code):
    """บทบาทของตำแหน่ง = บทบาทตามระดับ + บทบาทของหน้าที่"""
    base = [LEVEL_ROLES[level]] if level in LEVEL_ROLES else []
    return base + [r for r in FUNCTIONS.get(function_code or "", ("", []))[1] if r not in base]


def permission_roles(perm):
    name, roles, locked = PERMISSIONS[perm]
    if locked:
        return set(roles)
    extra = (repo().get("system_settings", "permission_extra") or {}).get(perm, [])
    return set(roles) | set(extra or [])


def current_year():
    return int(setting("current_academic_year"))


def in_year(pos, year):
    """ตำแหน่งอยู่ในผังของปีนั้นหรือไม่ (academic_year ว่าง = ใช้ทุกปี)"""
    return pos.get("academic_year") in (None, "", year)


def positions_of_year(year=None):
    year = year or current_year()
    return [p for p in repo().all("positions") if in_year(p, year)]


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


# ---------- สิทธิ์สำรองเมื่อตำแหน่งว่าง (ORG-11, ORG-12) ----------
def vacant_fallback_positions(year=None):
    """ตำแหน่งที่ตั้งหน้าที่สำรองไว้ และไม่มีผู้ถือในวาระ"""
    year = year or current_year()
    out = []
    for p in repo().where("positions", has_fallback=True):
        if in_year(p, year) and not repo().where("position_holders", position_id=p["id"], academic_year=year,
                                                 status="ACTIVE"):
            out.append(p)
    return out


def _roles_from(user, holdings):
    roles = set(user.get("system_roles") or [])
    if user.get("account_type") == "SCI":
        roles.add("STUDENT")
    for h in holdings:
        roles.update(h["position"].get("roles") or [])
    return roles


def _fallback_roles(holdings, year=None):
    mine = {h["position"].get("function_code") or h["position"].get("code") for h in holdings}
    out = set()
    if not mine:
        return out
    for p in vacant_fallback_positions(year):
        if mine & set(p.get("fallback_functions") or []):
            out.update(FUNCTIONS.get(p.get("function_code") or "", ("", []))[1] or [])
    return out


def user_roles(user):
    """บทบาทจริงของผู้ใช้ (ไม่รวมสิทธิ์สำรอง)"""
    if not user:
        return set()
    return _roles_from(user, user_holdings(user["id"]))


def user_fallback_roles(user):
    return _fallback_roles(user_holdings(user["id"])) if user else set()


def user_divisions(user, role_filter=("DIV_VP", "DIV_SEC")):
    """กองที่ผู้ใช้เป็นอุปนายก/เลขาประจำกอง"""
    divs = set()
    for h in user_holdings(user["id"]):
        if set(h["position"].get("roles") or []) & set(role_filter):
            if h["position"].get("division_id"):
                divs.add(h["position"]["division_id"])
    return divs


def board_divisions(user):
    """กองที่ผู้ใช้ถือตำแหน่งระดับบอร์ด (ใช้กับเอกสารของกองตน)"""
    return user_divisions(user, role_filter=("BOARD",))


def holders_of_role(role, year=None):
    """รายชื่อ user_id ที่ถือบทบาทนี้ในวาระ (รวมผู้สำรองเมื่อตำแหน่งเจ้าของบทบาทว่าง)"""
    year = year or current_year()
    positions = [p for p in repo().all("positions") if in_year(p, year)]
    users = set()
    for p in positions:
        if role in (p.get("roles") or []):
            for h in repo().where("position_holders", position_id=p["id"], academic_year=year, status="ACTIVE"):
                users.add(h["user_id"])
    for p in positions:
        if not p.get("has_fallback") or role not in FUNCTIONS.get(p.get("function_code") or "", ("", []))[1]:
            continue
        if repo().where("position_holders", position_id=p["id"], academic_year=year, status="ACTIVE"):
            continue
        for q in positions:
            if (q.get("function_code") or q.get("code")) in (p.get("fallback_functions") or []):
                for h in repo().where("position_holders", position_id=q["id"], academic_year=year, status="ACTIVE"):
                    users.add(h["user_id"])
    return users


# ---------- ผู้ใช้ปัจจุบัน (แคชต่อ 1 request) ----------
def _ctx():
    if not has_request_context():
        return {"user": None, "roles": set(), "fallback": set(), "used": set()}
    uid = session.get("uid")
    ctx = getattr(request, "_sta_ctx", None)
    if ctx is None or ctx["uid"] != uid:
        user = repo().get("users", uid) if uid else None
        if user and user.get("status") != "ACTIVE":
            user = None
        holdings = user_holdings(user["id"]) if user else []
        ctx = {"uid": uid, "user": user, "roles": _roles_from(user, holdings) if user else set(),
               "fallback": _fallback_roles(holdings) if user else set(), "used": set()}
        request._sta_ctx = ctx
    return ctx


def current_user():
    return _ctx()["user"]


def roles():
    return _ctx()["roles"]


def fallback_used():
    """สิทธิ์ที่ request นี้ได้มาจากสิทธิ์สำรอง (ใช้ระบุใน Audit Log — ORG-13)"""
    return sorted(_ctx()["used"])


def can(perm, user=None):
    if user is None:
        ctx = _ctx()
        r, fb = ctx["roles"], ctx["fallback"]
    else:
        ctx = None
        r, fb = user_roles(user), user_fallback_roles(user)
    need = permission_roles(perm)
    if r & need:
        return True
    if fb & need and not PERMISSIONS[perm][2]:  # สิทธิ์ locked ต้องมีผู้ถือจริง
        if ctx is not None:
            ctx["used"].add(perm)
        return True
    return False


def has_role(*codes):
    return bool(roles() & set(codes))


def has_delegation(user, perm, scope_id):
    """PERM-02: การมอบสิทธิ์ มีวันหมดอายุ ถอนได้"""
    for d in repo().where("delegations", to_user_id=user["id"], permission=perm, scope_id=scope_id):
        if d.get("revoked_at"):
            continue
        exp = parse_dt(d.get("expires_at"))
        if exp and exp < now():
            continue
        return True
    return False


# ---------- decorators ----------
def _to_login():
    from flask import redirect, url_for
    return redirect(url_for("auth.login", next=request.full_path))


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return _to_login()
        return fn(*a, **kw)
    return wrapper


def permission_required(*perms):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_user():
                return _to_login()
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
                return _to_login()
            if u.get("account_type") not in types:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def is_board(user):
    return any(h["position"].get("level") == "BOARD" for h in user_holdings(user["id"]))
