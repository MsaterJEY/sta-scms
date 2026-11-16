"""งานรายวันสำหรับ Vercel Cron (ดู vercel.json) ป้องกันด้วย CRON_SECRET"""
from flask import Blueprint, abort, current_app, jsonify, request

from ..db import repo
from ..services import notify
from ..utils import now

bp = Blueprint("cron", __name__, url_prefix="/cron")


def approval_digest():
    """ส่งสรุป "คุณมีรายการรออนุมัติ N รายการ" ทาง LINE ให้ผู้อนุมัติที่เลือกรับ (ไม่ส่งรายรายการ เพื่อประหยัดโควตา)"""
    from .. import line
    from ..rbac import holders_of_role
    from ..services import send_email
    from .dashboard import pending_approvals_for
    if not line.configured():
        return 0
    n = 0
    for uid in holders_of_role("ADMIN_VP") | holders_of_role("ADMIN_SEC"):
        if not line.wants(uid, "APPROVAL"):
            continue
        u = repo().get("users", uid)
        count = len([x for x in pending_approvals_for(u) if not x["mine_done"]]) if u else 0
        if not count:
            continue
        if line.deliver(uid, "APPROVAL", "/app/approvals", count=count) == "FAILED" and u.get("email"):
            sent = send_email(u["email"], f"คุณมีรายการรออนุมัติ {count} รายการ",
                              (current_app.config.get("SITE_URL") or "") + "/app/approvals")
            line._log(uid, "APPROVAL", "EMAIL", "FALLBACK" if sent else "FAILED", None if sent else "smtp")
        n += 1
    return n


@bp.route("/daily", methods=["GET", "POST"])
def daily():
    secret = current_app.config.get("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        abort(401)
    from .announcements import publish_scheduled
    from .equipment import daily_equipment_jobs
    from .tickets import daily_ticket_jobs

    result = {"announcements_published": publish_scheduled(), "borrower_docs_deleted": daily_equipment_jobs(),
              "tickets_over_sla": daily_ticket_jobs()}
    # AUTH-06 ชั้นปีเกินระยะเวลาหลักสูตร
    over = [u for u in repo().all("users") if u.get("over_duration") and not u.get("over_duration_notified")]
    if over:
        admins = [u["id"] for u in repo().all("users") if "SYS_ADMIN" in (u.get("system_roles") or [])]
        notify(admins, "มีนักศึกษาชั้นปีเกินระยะเวลาหลักสูตร", f"{len(over)} บัญชี", "/admin/users?review=1")
        for u in over:
            repo().update("users", u["id"], {"over_duration_notified": True})
    result["over_duration"] = len(over)
    result["line_approval_digests"] = approval_digest()
    result["at"] = now().isoformat()
    return jsonify(result)
