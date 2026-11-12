"""7.0 คำร้องและร้องเรียน (TKT-01..15)"""
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, current_year, holders_of_role, login_required, user_roles
from ..services import audit, display_name, notify, notify_role
from ..storage import UploadError, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import gen_code, now, now_iso, parse_dt, setting, to_iso

bp = Blueprint("tickets", __name__, url_prefix="/tickets")
NORMAL_HANDLERS = {"ADMIN_SEC", "PRES_SEC"}
KEY_ROLES = ("PRES", "PRES_SEC", "ADMIN_VP", "ADMIN_SEC")


def route_ticket(target_position_ids):
    """หัวข้อ 5.4 กำหนดผู้รับเรื่องและซ่อนเรื่องจากผู้ถูกร้องเรียน"""
    hidden_roles, hidden_users = set(), set()
    year = current_year()
    for pid in target_position_ids:
        pos = repo().get("positions", pid)
        if not pos:
            continue
        hidden_roles |= set(pos.get("roles") or []) & set(KEY_ROLES)
        for h in repo().where("position_holders", position_id=pid, academic_year=year, status="ACTIVE"):
            hidden_users.add(h["user_id"])
    inform_pres = False
    if "PRES" in hidden_roles:
        hidden_roles.add("PRES_SEC")  # ข้อสมมติ 17.1 ข้อ 2
        handlers, level = {"ADVISOR"}, "ADVISOR"
    elif {"ADMIN_SEC", "PRES_SEC"} <= hidden_roles:
        handlers, level = {"PRES", "ADVISOR"}, "PRESIDENT"
    else:
        handlers, level = NORMAL_HANDLERS - hidden_roles, "NORMAL"
        inform_pres = bool(hidden_roles & {"ADMIN_SEC", "ADMIN_VP"})
    viewers = {"ADMIN_VP"} - hidden_roles
    return {"hidden_roles": sorted(hidden_roles), "hidden_user_ids": sorted(hidden_users), "handler_roles": sorted(handlers),
            "viewer_roles": sorted(viewers), "routing_level": level, "inform_pres": inform_pres}


def is_hidden_from(t, user, roles=None):
    roles = roles if roles is not None else user_roles(user)
    return user["id"] in (t.get("hidden_user_ids") or []) or bool(roles & set(t.get("hidden_roles") or []))


def access(t, user):
    """คืนระดับสิทธิ์ต่อเรื่อง: owner / handle / view / none"""
    if t["submitter_id"] == user["id"]:
        return "owner"
    roles = user_roles(user)
    if is_hidden_from(t, user, roles):
        return "none"
    if roles & set(t.get("handler_roles") or []):
        return "handle"
    if roles & set(t.get("viewer_roles") or []):
        return "view"
    return "none"


def sla_overdue(t):
    due = parse_dt(t.get("due_at"))
    return t.get("status") == "SUBMITTED" and due is not None and due < now()


def _ticket(tid):
    t = repo().get("tickets", tid)
    if not t:
        abort(404)
    return t


def _log_access(t, action):
    repo().add("ticket_access_logs", {"ticket_id": t["id"], "user_id": current_user()["id"], "action": action, "at": now_iso()})


def _status_log(t, to_status, reason=""):
    repo().add("ticket_status_logs", {"ticket_id": t["id"], "from_status": t.get("status"), "to_status": to_status,
                                      "actor_id": current_user()["id"], "reason": reason, "at": now_iso()})


@bp.route("/")
@login_required
def index():
    u = current_user()
    mine = sorted(repo().where("tickets", submitter_id=u["id"]), key=lambda t: t.get("created_at", ""), reverse=True)
    queue = []
    if can("tkt.handle") or can("tkt.view") or can("tkt.escalated"):
        for t in repo().all("tickets"):
            if t["submitter_id"] == u["id"]:
                continue
            lvl = access(t, u)
            if lvl in ("handle", "view"):
                t["level"] = lvl
                t["overdue"] = sla_overdue(t)
                queue.append(t)
        f = request.args.get("status")
        if f:
            queue = [t for t in queue if t.get("status") == f]
        queue.sort(key=lambda t: (t.get("status") == "CLOSED", t.get("created_at", "")))
    cats = {c["id"]: c for c in repo().all("ticket_categories")}
    return render_template("tkt/index.html", mine=mine, queue=queue, cats=cats, status=request.args.get("status"))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    u = current_user()
    if u.get("account_type") != "SCI":
        flash("ส่งคำร้องได้เฉพาะนักศึกษาคณะวิทยาศาสตร์ฯ", "danger")
        return redirect(url_for("tickets.index"))
    cats = [c for c in repo().all("ticket_categories") if c.get("is_active", True)]
    board_positions = [p for p in repo().all("positions") if p.get("level") == "BOARD"]
    if request.method == "POST":
        f = request.form
        cat = repo().get("ticket_categories", f.get("category_id"))
        if not cat or not f.get("title", "").strip() or not f.get("detail", "").strip():
            flash("กรุณาเลือกประเภท และกรอกหัวข้อ/รายละเอียด", "danger")
            return redirect(url_for("tickets.new"))
        targets = [p for p in f.getlist("target_position_ids") if repo().get("positions", p)]
        routing = route_ticket(targets)
        files = []
        for fs in request.files.getlist("files")[:3]:
            if fs and fs.filename:
                try:
                    b, ext, ctype = validate_upload(fs)
                    files.append({"name": fs.filename, "path": save_bytes("tickets", b, ext, ctype, encrypt=True)})
                except UploadError as e:
                    flash(f"{fs.filename}: {e}", "warning")
        sla = int(cat.get("sla_days") or setting("ticket_sla_days") or 3)
        data = {
            "tracking_code": gen_code("T"), "category_id": cat["id"], "submitter_id": u["id"],
            "is_anonymous": bool(f.get("is_anonymous")), "target_position_ids": targets,
            "title": f["title"].strip()[:200], "detail": f["detail"].strip()[:10000],
            "sensitivity": "RESTRICTED" if (targets or cat.get("is_sensitive")) else "NORMAL",
            "status": "SUBMITTED", "handler_id": None, "due_at": to_iso(now() + timedelta(days=sla)),
            "closed_at": None, "reopen_until": None, "reopen_count": 0, "created_at": now_iso(), "files": files,
            **{k: v for k, v in routing.items() if k != "inform_pres"},
        }
        tid = repo().add("tickets", data)
        repo().add("consents", {"user_id": u["id"], "purpose": "TICKET", "ref": tid, "granted_at": now_iso(), "expires_at": None})
        for role in routing["handler_roles"]:
            notify(list(holders_of_role(role) - set(routing["hidden_user_ids"])), "มีคำร้องใหม่", data["title"],
                   url_for("tickets.detail", tid=tid))
        if routing["inform_pres"]:
            notify_role("PRES", "รับทราบ: มีเรื่องร้องเรียนตำแหน่งในฝ่ายบริหารฯ", data["tracking_code"],
                        url_for("tickets.index"), exclude=routing["hidden_user_ids"])
        audit("ticket.create", "ticket", tid, new={"routing": routing["routing_level"]})
        flash(f"ส่งคำร้องแล้ว รหัสติดตาม {data['tracking_code']}", "success")
        return redirect(url_for("tickets.detail", tid=tid))
    return render_template("tkt/form.html", cats=cats, board_positions=board_positions)


@bp.route("/track")
@login_required
def track():
    code = (request.args.get("code") or "").strip().upper()
    found = repo().where("tickets", tracking_code=code)
    if not found or access(found[0], current_user()) == "none":
        flash("ไม่พบรหัสติดตามนี้", "warning")
        return redirect(url_for("tickets.index"))
    return redirect(url_for("tickets.detail", tid=found[0]["id"]))


@bp.route("/<tid>")
@login_required
def detail(tid):
    t = _ticket(tid)
    u = current_user()
    lvl = access(t, u)
    if lvl == "none":
        abort(403)
    if lvl != "owner":
        _log_access(t, "VIEW")
        if t.get("sensitivity") == "RESTRICTED" or lvl == "view":
            audit("ticket.view", "ticket", tid)
    msgs = sorted(repo().where("ticket_messages", ticket_id=tid), key=lambda m: m.get("sent_at", ""))
    if lvl == "owner":
        msgs = [m for m in msgs if not m.get("is_internal")]
    reveal = next(iter(repo().where("ticket_identity_reveals", ticket_id=tid, revealed_by=u["id"])), None)
    show_identity = lvl == "owner" or not t.get("is_anonymous") or reveal
    submitter = repo().get("users", t["submitter_id"]) if show_identity else None
    rating = repo().get("ticket_ratings", tid)
    logs = sorted(repo().where("ticket_status_logs", ticket_id=tid), key=lambda l: l.get("at", ""))
    targets = [repo().get("positions", p) for p in t.get("target_position_ids") or []]
    reopen_ok = t.get("status") == "CLOSED" and parse_dt(t.get("reopen_until")) and parse_dt(t["reopen_until"]) > now()
    senders = {m["sender_id"]: repo().get("users", m["sender_id"]) for m in msgs}
    return render_template("tkt/detail.html", t=t, lvl=lvl, msgs=msgs, submitter=submitter, rating=rating, logs=logs,
                           cat=repo().get("ticket_categories", t["category_id"]) or {}, targets=[x for x in targets if x],
                           is_claimer=t.get("handler_id") == u["id"], reopen_ok=reopen_ok, senders=senders,
                           can_reveal=can("tkt.escalated") and t.get("is_anonymous") and lvl in ("handle", "view") and not reveal,
                           overdue=sla_overdue(t), handler=repo().get("users", t.get("handler_id")))


@bp.route("/<tid>/action", methods=["POST"])
@login_required
def action(tid):
    t = _ticket(tid)
    u = current_user()
    lvl = access(t, u)
    act = request.form.get("action")
    reason = request.form.get("reason", "").strip()
    link = url_for("tickets.detail", tid=tid)

    if act == "message":
        body = request.form.get("body", "").strip()
        internal = bool(request.form.get("is_internal"))
        if not body:
            return redirect(link)
        if lvl == "owner":
            if t["status"] == "CLOSED":
                abort(400)
            repo().add("ticket_messages", {"ticket_id": tid, "sender_id": u["id"], "sender_type": "STUDENT", "is_internal": False,
                                           "body": body[:5000], "sent_at": now_iso()})
            if t.get("status") == "WAITING_INFO":
                _status_log(t, "IN_PROGRESS")
                repo().update("tickets", tid, {"status": "IN_PROGRESS"})
            notify(t.get("handler_id"), "ผู้ส่งตอบกลับคำร้อง", t["title"], link)
        elif lvl == "handle" and (t.get("handler_id") == u["id"] or internal):
            # ตอบกลับได้เฉพาะเจ้าของเรื่อง; บันทึกภายในผู้รับเรื่องเขียนได้
            repo().add("ticket_messages", {"ticket_id": tid, "sender_id": u["id"], "sender_type": "HANDLER",
                                           "is_internal": internal, "body": body[:5000], "sent_at": now_iso()})
            if not internal:
                new_status = request.form.get("new_status") if request.form.get("new_status") in ("IN_PROGRESS", "WAITING_INFO", "ANSWERED") else "ANSWERED"
                _status_log(t, new_status)
                repo().update("tickets", tid, {"status": new_status})
                notify(t["submitter_id"], "คำร้องของคุณมีการตอบกลับ", t["title"], link)
                _log_access(t, "REPLY")
        else:
            abort(403)

    elif act == "claim":
        if lvl != "handle" or t.get("handler_id"):
            flash("รับเรื่องไม่ได้ (มีผู้รับแล้ว หรือไม่มีสิทธิ์)", "danger")
            return redirect(link)
        repo().add("ticket_claims", {"ticket_id": tid, "handler_id": u["id"], "claimed_at": now_iso(), "released_at": None})
        _status_log(t, "CLAIMED")
        repo().update("tickets", tid, {"handler_id": u["id"], "status": "CLAIMED" if t["status"] == "SUBMITTED" else t["status"]})
        notify(t["submitter_id"], "คำร้องของคุณได้รับการรับเรื่องแล้ว", t["title"], link)

    elif act == "release":
        if t.get("handler_id") != u["id"]:
            abort(403)
        for c in repo().where("ticket_claims", ticket_id=tid, handler_id=u["id"]):
            if not c.get("released_at"):
                repo().update("ticket_claims", c["id"], {"released_at": now_iso()})
        repo().update("tickets", tid, {"handler_id": None})
        _status_log(t, t["status"], "ปล่อยเรื่อง")

    elif act == "close":
        if lvl != "handle" or t.get("handler_id") != u["id"]:
            abort(403)
        if not reason:
            flash("การปิดเรื่องต้องระบุเหตุผล", "danger")
            return redirect(link)
        _status_log(t, "CLOSED", reason)
        repo().update("tickets", tid, {"status": "CLOSED", "closed_at": now_iso(), "close_reason": reason,
                                       "reopen_until": to_iso(now() + timedelta(days=int(setting("ticket_reopen_days") or 7)))})
        notify(t["submitter_id"], "คำร้องของคุณปิดเรื่องแล้ว", reason, link)

    elif act == "escalate":
        if lvl != "handle":
            abort(403)
        target = request.form.get("to") if request.form.get("to") in ("PRESIDENT", "ADVISOR") else "PRESIDENT"
        roles = ["PRES", "ADVISOR"] if target == "PRESIDENT" else ["ADVISOR"]
        roles = [r for r in roles if r not in (t.get("hidden_roles") or [])] or ["ADVISOR"]
        _status_log(t, t["status"], f"ส่งต่อ {target}: {reason}")
        repo().update("tickets", tid, {"routing_level": target, "handler_roles": roles, "handler_id": None})
        for r in roles:
            notify(list(holders_of_role(r) - set(t.get("hidden_user_ids") or [])), "มีคำร้องที่ส่งต่อถึงคุณ", t["title"], link)

    elif act == "reopen":
        if lvl != "owner" or t["status"] != "CLOSED" or not parse_dt(t.get("reopen_until")) or parse_dt(t["reopen_until"]) < now():
            abort(400)
        _status_log(t, "IN_PROGRESS", "ยังไม่ได้รับการแก้ไข")
        repo().update("tickets", tid, {"status": "IN_PROGRESS", "closed_at": None, "reopen_count": int(t.get("reopen_count") or 0) + 1})
        notify(t.get("handler_id"), "คำร้องถูกเปิดใหม่", t["title"], link)

    elif act == "rate":
        if lvl != "owner" or t["status"] != "CLOSED" or repo().get("ticket_ratings", tid):
            abort(400)
        score = int(request.form.get("score") or 0)
        if 1 <= score <= 5:
            repo().set("ticket_ratings", tid, {"ticket_id": tid, "score": score, "comment": request.form.get("comment", "")[:500],
                                               "rated_at": now_iso()})

    elif act == "reveal":
        if not can("tkt.escalated") or lvl not in ("handle", "view") or not t.get("is_anonymous"):
            abort(403)
        if not reason:
            flash("การเปิดตัวตนต้องระบุเหตุผล", "danger")
            return redirect(link)
        repo().add("ticket_identity_reveals", {"ticket_id": tid, "revealed_by": u["id"], "reason": reason, "at": now_iso()})
        audit("ticket.reveal_identity", "ticket", tid, note=reason)
    else:
        abort(400)
    audit(f"ticket.{act}", "ticket", tid)
    return redirect(link)


@bp.route("/<tid>/file/<int:idx>")
@login_required
def file(tid, idx):
    t = _ticket(tid)
    if access(t, current_user()) == "none":
        abort(403)
    files = t.get("files") or []
    if idx >= len(files):
        abort(404)
    _log_access(t, "VIEW")
    return Response(load_bytes(files[idx]["path"]), mimetype=mimetype_for(files[idx]["path"]),
                    headers={"Cache-Control": "no-store"})


def ticket_stats():
    """TKT-14 สถิติรวมเท่านั้น ซ่อนกลุ่มที่เล็กกว่าเกณฑ์"""
    min_group = int(setting("ticket_stats_min_group") or 5)
    cats = {c["id"]: c for c in repo().all("ticket_categories")}
    rows = []
    tickets = repo().all("tickets")
    for cid, c in cats.items():
        ts = [t for t in tickets if t.get("category_id") == cid]
        n = len(ts)
        closed = [t for t in ts if t.get("status") == "CLOSED"]
        ratings = [repo().get("ticket_ratings", t["id"]) for t in closed]
        ratings = [r["score"] for r in ratings if r]
        if n < min_group:
            rows.append([c["name"], f"< {min_group}", "-", "-", "-"])
        else:
            rows.append([c["name"], n, len(closed), len([t for t in ts if sla_overdue(t)]),
                         round(sum(ratings) / len(ratings), 2) if ratings else "-"])
    return rows


@bp.route("/stats")
@login_required
def stats():
    if not (can("tkt.handle") or can("tkt.view") or can("tkt.escalated") or can("report.view")):
        abort(403)
    overdue = []
    if can("tkt.sla_view"):
        u = current_user()
        overdue = [t for t in repo().all("tickets") if sla_overdue(t) and not is_hidden_from(t, u)]
    return render_template("tkt/stats.html", rows=ticket_stats(), overdue=overdue)


@dataset("ticket_stats")
def ds_stats(args):
    if not (can("tkt.handle") or can("tkt.view") or can("tkt.escalated") or can("report.view")):
        abort(403)
    return "ticket-stats", ["ประเภท", "จำนวนเรื่อง", "ปิดแล้ว", "ค้างเกิน SLA", "คะแนนพึงพอใจเฉลี่ย"], ticket_stats()


def daily_ticket_jobs():
    """TKT-10 เรื่องเลย SLA และยังไม่มีผู้รับ แจ้ง PRES และผู้รับเรื่อง"""
    n = 0
    for t in repo().all("tickets"):
        if sla_overdue(t):
            n += 1
            link = url_for("tickets.detail", tid=t["id"])
            hidden = set(t.get("hidden_user_ids") or [])
            for r in t.get("handler_roles") or []:
                notify(list(holders_of_role(r) - hidden), "คำร้องค้างเกิน SLA", t.get("tracking_code"), link)
            if "PRES" not in (t.get("hidden_roles") or []):
                notify(list(holders_of_role("PRES") - hidden), "คำร้องค้างเกิน SLA (ดูอย่างเดียว)", t.get("tracking_code"),
                       url_for("tickets.stats"))
    return n
