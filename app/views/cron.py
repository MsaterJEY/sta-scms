"""งานรายวันสำหรับ Vercel Cron (ดู vercel.json) ป้องกันด้วย CRON_SECRET"""
from flask import Blueprint, abort, current_app, jsonify, request

from ..db import repo
from ..services import notify
from ..utils import now

bp = Blueprint("cron", __name__, url_prefix="/cron")


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
    result["at"] = now().isoformat()
    return jsonify(result)
