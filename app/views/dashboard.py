"""หน้าหลักหลังล็อกอิน แจ้งเตือน และศูนย์รวมรายการรออนุมัติ"""
from flask import Blueprint, current_app, redirect, render_template, request, url_for

from ..db import repo
from ..rbac import can, current_user, login_required, user_holdings
from ..services import approval_state, approver_role_for

bp = Blueprint("dashboard", __name__, url_prefix="/app")


def pending_approvals_for(user):
    out = []
    for req in repo().all("approval_requests"):
        if req.get("status") not in ("PENDING",):
            continue
        if not approver_role_for(user, req):
            continue
        st = approval_state(req)
        if st["approved"]:
            continue
        mine_done = bool(set(approver_role_for(user, req)) <= st["approved_roles"])
        out.append({"req": req, "state": st, "mine_done": mine_done})
    out.sort(key=lambda x: x["req"].get("created_at", ""), reverse=True)
    return out


@bp.route("/")
@login_required
def index():
    u = current_user()
    activities_on = bool(current_app.config.get("ACTIVITIES_ENABLED"))
    tasks = {}
    if can("rec.approve") or can("eqp.self_approve") or (activities_on and can("act.approve")):
        tasks["approvals"] = len([x for x in pending_approvals_for(u) if not x["mine_done"]])
    if can("eqp.manage"):
        tasks["eqp_pending"] = len(repo().where("equipment_requests", status="PENDING"))
    if can("tkt.handle"):
        tasks["tickets_open"] = len(repo().where("tickets", status="SUBMITTED"))
    my_loans = [r for r in repo().where("equipment_requests", requester_id=u["id"])
                if r.get("status") not in ("CLOSED", "REJECTED", "CANCELLED")]
    my_hours = sum(float(h.get("hours") or 0) for h in repo().where("activity_hours", user_id=u["id"])) if activities_on else 0
    my_apps = repo().where("member_applications", user_id=u["id"])
    my_staff_apps = repo().where("activity_applications", user_id=u["id"]) if activities_on else []
    my_team_apps = repo().where("team_applications", user_id=u["id"])
    notes = sorted(repo().where("notifications", user_id=u["id"]), key=lambda n: n.get("created_at", ""), reverse=True)[:5]
    return render_template("dashboard/index.html", tasks=tasks, my_loans=my_loans, my_hours=my_hours,
                           my_apps=my_apps, my_staff_apps=my_staff_apps, my_team_apps=my_team_apps, notes=notes, holdings=user_holdings(u["id"]))


@bp.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    u = current_user()
    items = sorted(repo().where("notifications", user_id=u["id"]), key=lambda n: n.get("created_at", ""), reverse=True)
    if request.method == "POST":
        for n in items:
            if not n.get("is_read"):
                repo().update("notifications", n["id"], {"is_read": True})
        return redirect(url_for("dashboard.notifications"))
    return render_template("dashboard/notifications.html", items=items[:200])


@bp.route("/notifications/<nid>")
@login_required
def open_notification(nid):
    n = repo().get("notifications", nid)
    if not n or n["user_id"] != current_user()["id"]:
        return redirect(url_for("dashboard.notifications"))
    repo().update("notifications", nid, {"is_read": True})
    link = n.get("link") or url_for("dashboard.notifications")
    return redirect(link if link.startswith("/") else url_for("dashboard.notifications"))


@bp.route("/approvals")
@login_required
def approvals():
    u = current_user()
    items = pending_approvals_for(u)
    return render_template("dashboard/approvals.html", items=items)
