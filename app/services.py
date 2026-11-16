"""บริการกลาง: Audit Log, แจ้งเตือน, การอนุมัติ, ข้อมูลผู้ใช้"""
import smtplib
from email.message import EmailMessage

from flask import current_app, request

from .db import repo
from .rbac import current_user, fallback_used, holders_of_role, user_roles
from .utils import now_iso


# ---------- Audit (AUD-01) เพิ่มอย่างเดียว ----------
def audit(action, entity_type, entity_id=None, old=None, new=None, note=None, actor_id=None, anonymous=False):
    """anonymous=True: ไม่บันทึกผู้ทำและ IP (เรื่องร้องเรียนนิรนาม TKT-20)"""
    u = None if anonymous else current_user()
    ip = None
    if not anonymous:
        try:
            ip = request.headers.get("x-forwarded-for", request.remote_addr)
        except RuntimeError:
            ip = None
    used = [] if anonymous else fallback_used()
    if used:  # ORG-13
        note = f"[ใช้สิทธิ์สำรอง: {', '.join(used)}] {note or ''}".strip()
    repo().add("audit_logs", {
        "actor_id": None if anonymous else (actor_id or (u["id"] if u else None)),
        "actor_name": "นิรนาม" if anonymous else (display_name(u) if u else "system"),
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "old": old,
        "new": new,
        "note": note,
        "via_fallback": used or None,
        "ip": ip,
        "at": now_iso(),
    })


# ---------- แจ้งเตือน (NTF-01) ----------
def notify(user_ids, title, body="", link=None, email=False, event=None):
    """event: ประเภทเหตุการณ์ที่ส่งทาง LINE ได้ (ดู line.EVENTS) — ข้อความ LINE ไม่มี title/body ของรายการ"""
    from . import line
    if not user_ids:
        return
    if isinstance(user_ids, str):
        user_ids = [user_ids]
    for uid in set(filter(None, user_ids)):
        repo().add("notifications", {
            "user_id": uid, "title": title, "body": body, "link": link,
            "is_read": False, "created_at": now_iso(),
        })
        fallback = line.deliver(uid, event, link) == "FAILED" if event else False
        if email or fallback:
            u = repo().get("users", uid)
            if u and u.get("email"):
                sent = send_email(u["email"], title, f"{body}\n\n{(current_app.config.get('SITE_URL') or '') + (link or '')}")
                if fallback:
                    line._log(uid, event, "EMAIL", "FALLBACK" if sent else "FAILED", None if sent else "smtp")


def notify_role(role, title, body="", link=None, exclude=None):
    ids = holders_of_role(role) - set(exclude or [])
    notify(list(ids), title, body, link)


def send_email(to, subject, body):
    cfg = current_app.config
    if not cfg.get("SMTP_HOST"):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER")
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as s:
            s.starttls()
            if cfg.get("SMTP_USER"):
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------- ผู้ใช้ ----------
def display_name(user):
    if not user:
        return "-"
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return name or user.get("email") or user.get("student_code") or "-"


def user_map(ids):
    out = {}
    for i in set(filter(None, ids)):
        u = repo().get("users", i)
        if u:
            out[i] = u
    return out


# ---------- การอนุมัติ (APR-01..03) ----------
def flow(code):
    return repo().get("approval_flows", code)


def create_approval(flow_code, subject_type, subject_id, title, link):
    u = current_user()
    req_id = repo().add("approval_requests", {
        "flow_code": flow_code, "subject_type": subject_type, "subject_id": subject_id,
        "title": title, "link": link, "status": "PENDING", "cycle": 1,
        "requested_by": u["id"] if u else None, "created_at": now_iso(),
    })
    f = flow(flow_code)
    for role in (f or {}).get("approver_roles", []):
        notify_role(role, "มีรายการรออนุมัติ", title, link)
    audit("approval.request", subject_type, subject_id, new={"flow": flow_code, "request_id": req_id})
    return req_id


def approval_state(req):
    """คำนวณสถานะจากบันทึกการอนุมัติรายคน ไม่ใช้สถานะตายตัว (APR-02)"""
    if not req:
        return {"status": "NONE", "label": "-", "approved": False, "logs": [], "approved_roles": set()}
    f = flow(req["flow_code"]) or {"mode": "ANY", "approver_roles": []}
    logs = sorted(repo().where("approval_logs", request_id=req["id"]), key=lambda x: x.get("decided_at", ""))
    cycle_logs = [l for l in logs if l.get("cycle") == req.get("cycle", 1)]
    approved_roles = {l["role"] for l in cycle_logs if l["decision"] == "APPROVE"}
    rejected = [l for l in cycle_logs if l["decision"] == "REJECT"]
    need = set(f.get("approver_roles", []))
    if f.get("mode") == "ALL":
        ok = need.issubset(approved_roles)
        label = "อนุมัติครบ" if ok else (f"อนุมัติ {len(approved_roles & need)}/{len(need)}" if approved_roles else "รออนุมัติ")
    else:
        ok = bool(approved_roles & need)
        who = next((l for l in cycle_logs if l["decision"] == "APPROVE"), None)
        label = f"อนุมัติแล้วโดย {who['approver_name']}" if ok and who else "รออนุมัติ"
    status = "APPROVED" if ok else "PENDING"
    if req.get("status") == "CANCELLED":
        status, label = "CANCELLED", "ยกเลิก"
    elif not ok and req.get("status") == "RECONSIDER":
        status, label = "RECONSIDER", "รอพิจารณาใหม่"
    return {"status": status, "label": label, "approved": ok, "logs": logs, "approved_roles": approved_roles,
            "mode": f.get("mode"), "need": need, "rejected": rejected}


def approver_role_for(user, req):
    f = flow(req["flow_code"]) or {}
    mine = user_roles(user) & set(f.get("approver_roles", []))
    return sorted(mine)


def decide(req, decision, reason=""):
    """บันทึกการตัดสินใจของผู้อนุมัติ คืน (ok, message)"""
    u = current_user()
    my_roles = approver_role_for(u, req)
    if not my_roles:
        return False, "คุณไม่มีสิทธิ์อนุมัติรายการนี้"
    state = approval_state(req)
    if state["approved"]:
        return False, "รายการนี้อนุมัติแล้ว"
    if req.get("status") in ("RECONSIDER", "CANCELLED"):
        return False, "รายการนี้รอผู้เสนอแก้ไขและส่งใหม่"
    if decision == "REJECT" and not reason.strip():
        return False, "การปฏิเสธต้องระบุเหตุผล"
    pending_roles = [r for r in my_roles if r not in state["approved_roles"]]
    if not pending_roles:
        return False, "คุณอนุมัติรายการนี้ไปแล้ว"
    role = pending_roles[0]
    repo().add("approval_logs", {
        "request_id": req["id"], "cycle": req.get("cycle", 1), "approver_id": u["id"],
        "approver_name": display_name(u), "role": role, "decision": decision,
        "reason": reason.strip(), "decided_at": now_iso(),
    })
    if decision == "REJECT":
        # ALL/ANY: ปฏิเสธ -> กลับเป็นรอพิจารณาใหม่ (เริ่มรอบใหม่)
        repo().update("approval_requests", req["id"], {
            "cycle": req.get("cycle", 1) + 1, "last_rejected_cycle": req.get("cycle", 1),
            "status": "RECONSIDER", "last_reject_reason": reason.strip(),
        })
        notify(req.get("requested_by"), "รายการถูกส่งกลับให้พิจารณาใหม่", f"{req.get('title')} : {reason}", req.get("link"))
    else:
        req = repo().get("approval_requests", req["id"])
        new_state = approval_state(req)
        repo().update("approval_requests", req["id"], {"status": new_state["status"]})
        if new_state["approved"]:
            notify(req.get("requested_by"), "รายการได้รับอนุมัติแล้ว", req.get("title"), req.get("link"))
    audit(f"approval.{decision.lower()}", "approval_request", req["id"], new={"role": role, "reason": reason})
    return True, "บันทึกผลแล้ว"


def resubmit(req):
    repo().update("approval_requests", req["id"], {"status": "PENDING"})
    f = flow(req["flow_code"]) or {}
    for role in f.get("approver_roles", []):
        notify_role(role, "มีรายการรออนุมัติ (ส่งใหม่)", req.get("title"), req.get("link"))
