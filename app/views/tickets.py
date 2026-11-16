"""7.0 คำร้องและร้องเรียน (TKT-01..22) — ฉบับ SRS v3

- ไม่รับเรื่องร้องเรียนนายกผ่านระบบ (TKT-04, TKT-16, TKT-17) ส่งต่อได้ถึงนายกเท่านั้น (TKT-11)
- เรื่องนิรนามไม่เก็บตัวตนผู้ส่งเลย (TKT-02, TKT-03, TKT-18..22): submitter_id ว่าง
  ผู้ส่งเข้าถึงเรื่องด้วยรหัสติดตาม + PIN (ระบบเก็บเฉพาะค่าแฮช) ไม่มีการเปิดตัวตน (ยกเลิก TKT-12)
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, current_year, holders_of_role, in_year, login_required, user_roles
from ..services import audit, notify, notify_role
from ..storage import UploadError, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import gen_code, now, now_iso, parse_dt, setting, to_iso

bp = Blueprint("tickets", __name__, url_prefix="/tickets")
NORMAL_HANDLERS = {"ADMIN_SEC", "PRES_SEC"}
KEY_ROLES = ("PRES_SEC", "ADMIN_VP", "ADMIN_SEC")
OUT_OF_SCOPE = "นอกขอบเขต: เรื่องของนายก — ระบบนี้ไม่รับเรื่องร้องเรียนนายกสโมสร ให้เสนอที่ประชุมบอร์ดบริหาร"
PIN_MAX_FAILS, PIN_LOCK_MINUTES = 5, 15


def complaint_positions():
    """ตำแหน่งบอร์ดที่เลือกเป็นผู้ถูกร้องเรียนได้ (ไม่รวมนายก — TKT-04)"""
    year = current_year()
    return sorted([p for p in repo().all("positions") if p.get("level") == "BOARD" and in_year(p, year)
                   and "PRES" not in (p.get("roles") or [])], key=lambda p: (p.get("division_id") or "", p.get("name") or ""))


def route_ticket(target_position_ids):
    """หัวข้อ 5.4 (v3) กำหนดผู้รับเรื่องและซ่อนเรื่องจากผู้ถูกร้องเรียน"""
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
    if {"ADMIN_SEC", "PRES_SEC"} <= hidden_roles:
        handlers, level = {"PRES"}, "PRESIDENT"
    else:
        handlers, level = NORMAL_HANDLERS - hidden_roles, "NORMAL"
        inform_pres = bool(hidden_roles & {"ADMIN_SEC", "ADMIN_VP"})
    viewers = {"ADMIN_VP"} - hidden_roles
    return {"hidden_roles": sorted(hidden_roles), "hidden_user_ids": sorted(hidden_users), "handler_roles": sorted(handlers),
            "viewer_roles": sorted(viewers), "routing_level": level, "inform_pres": inform_pres}


def is_hidden_from(t, user, roles=None):
    roles = roles if roles is not None else user_roles(user)
    return user["id"] in (t.get("hidden_user_ids") or []) or bool(roles & set(t.get("hidden_roles") or []))


def anon_unlocked(t):
    """เรื่องนิรนามที่เบราว์เซอร์นี้ยืนยันรหัสติดตาม + PIN แล้ว"""
    return not t.get("submitter_id") and t["id"] in (session.get("anon_tickets") or [])


def access(t, user):
    """คืนระดับสิทธิ์ต่อเรื่อง: owner / handle / view / none"""
    if anon_unlocked(t) or (user and t.get("submitter_id") and t["submitter_id"] == user["id"]):
        return "owner"
    if not user:
        return "none"
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


def _status_log(t, to_status, reason="", anonymous=False):
    u = current_user()
    repo().add("ticket_status_logs", {"ticket_id": t["id"], "from_status": t.get("status"), "to_status": to_status,
                                      "actor_id": None if anonymous or not u else u["id"], "reason": reason, "at": now_iso()})


# ---------- โควตาเรื่องนิรนามต่อวันแบบไม่ผูกตัวตน (TKT-19) ----------
def _today():
    return now().strftime("%Y-%m-%d")


def purge_anon_quota():
    """ทำลายกุญแจและลบแถวโควตาของวันที่ผ่านไปแล้ว"""
    day = _today()
    n = 0
    for col in ("ticket_anon_keys", "ticket_anon_quota"):
        for d in repo().all(col):
            if d.get("day") != day:
                repo().delete(col, d["id"])
                n += 1
    return n


def anon_quota_take(user_id):
    """นับโควตาด้วยค่า HMAC ของรหัสผู้ใช้กับกุญแจรายวัน คืน False ถ้าเกินโควตา"""
    day = _today()
    key = repo().get("ticket_anon_keys", day)
    if not key:
        purge_anon_quota()
        repo().set("ticket_anon_keys", day, {"day": day, "key": secrets.token_hex(32)})
        key = repo().get("ticket_anon_keys", day)
    token = hmac.new(key["key"].encode(), user_id.encode(), hashlib.sha256).hexdigest()
    row = repo().get("ticket_anon_quota", token) or {"day": day, "count": 0}
    if int(row.get("count") or 0) >= int(setting("ticket_anon_daily_limit") or 3):
        return False
    repo().set("ticket_anon_quota", token, {"day": day, "count": int(row.get("count") or 0) + 1})
    return True


# ---------- รายการ ----------
@bp.route("/")
@login_required
def index():
    u = current_user()
    mine = sorted(repo().where("tickets", submitter_id=u["id"]), key=lambda t: t.get("created_at", ""), reverse=True)
    queue = []
    if can("tkt.handle") or can("tkt.view") or can("tkt.escalated"):
        for t in repo().all("tickets"):
            if t.get("submitter_id") == u["id"]:
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
    board_positions = complaint_positions()
    if request.method == "POST":
        f = request.form
        cat = repo().get("ticket_categories", f.get("category_id"))
        if not cat or not f.get("title", "").strip() or not f.get("detail", "").strip():
            flash("กรุณาเลือกประเภท และกรอกหัวข้อ/รายละเอียด", "danger")
            return redirect(url_for("tickets.new"))
        allowed = {p["id"] for p in board_positions}
        targets = [p for p in f.getlist("target_position_ids") if p in allowed]
        anonymous = bool(f.get("is_anonymous"))
        if anonymous and not anon_quota_take(u["id"]):
            flash("วันนี้ส่งเรื่องนิรนามครบจำนวนที่กำหนดแล้ว กรุณาส่งใหม่ในวันถัดไป หรือส่งแบบระบุตัวตน", "danger")
            return redirect(url_for("tickets.new"))
        routing = route_ticket(targets)
        files = []
        if not anonymous:  # TKT-21 เรื่องนิรนามไม่เปิดให้แนบไฟล์ (กัน metadata ระบุตัวตน)
            for fs in request.files.getlist("files")[:3]:
                if fs and fs.filename:
                    try:
                        b, ext, ctype = validate_upload(fs)
                        files.append({"name": fs.filename, "path": save_bytes("tickets", b, ext, ctype, encrypt=True)})
                    except UploadError as e:
                        flash(f"{fs.filename}: {e}", "warning")
        sla = int(cat.get("sla_days") or setting("ticket_sla_days") or 3)
        # TKT-20 เรื่องนิรนามปัดเวลาสร้างเป็นรายวัน
        created = now().replace(hour=0, minute=0, second=0) if anonymous else now()
        pin = f"{secrets.randbelow(10 ** 6):06d}" if anonymous else None
        data = {
            "tracking_code": gen_code("T"), "category_id": cat["id"], "submitter_id": None if anonymous else u["id"],
            "is_anonymous": anonymous, "access_pin_hash": generate_password_hash(pin) if pin else None,
            "pin_failed": 0, "pin_locked_until": None, "target_position_ids": targets,
            "title": f["title"].strip()[:200], "detail": f["detail"].strip()[:10000],
            "sensitivity": "RESTRICTED" if (targets or cat.get("is_sensitive")) else "NORMAL",
            "status": "SUBMITTED", "handler_id": None,
            "due_at": to_iso((created + timedelta(days=sla + (1 if anonymous else 0)))),
            "closed_at": None, "reopen_until": None, "reopen_count": 0, "created_at": to_iso(created), "files": files,
            **{k: v for k, v in routing.items() if k != "inform_pres"},
        }
        tid = repo().add("tickets", data)
        if not anonymous:
            repo().add("consents", {"user_id": u["id"], "purpose": "TICKET", "ref": tid, "granted_at": now_iso(), "expires_at": None})
        for role in routing["handler_roles"]:
            notify(list(holders_of_role(role) - set(routing["hidden_user_ids"])), "มีคำร้องใหม่", data["title"],
                   url_for("tickets.detail", tid=tid))
        if routing["inform_pres"]:
            notify_role("PRES", "รับทราบ: มีเรื่องร้องเรียนตำแหน่งในฝ่ายบริหารฯ", data["tracking_code"],
                        url_for("tickets.index"), exclude=routing["hidden_user_ids"])
        audit("ticket.create", "ticket", tid, new={"routing": routing["routing_level"]}, anonymous=anonymous)
        if anonymous:
            session["anon_tickets"] = list(set((session.get("anon_tickets") or []) + [tid]))[-20:]
            return render_template("tkt/anon_created.html", code=data["tracking_code"], pin=pin, tid=tid)
        flash(f"ส่งคำร้องแล้ว รหัสติดตาม {data['tracking_code']}", "success")
        return redirect(url_for("tickets.detail", tid=tid))
    return render_template("tkt/form.html", cats=cats, board_positions=board_positions)


@bp.route("/track")
@login_required
def track():
    code = (request.args.get("code") or "").strip().upper()
    found = repo().where("tickets", tracking_code=code)
    if found and not found[0].get("submitter_id") and access(found[0], current_user()) == "none":
        return redirect(url_for("tickets.anon", code=code))
    if not found or access(found[0], current_user()) == "none":
        flash("ไม่พบรหัสติดตามนี้", "warning")
        return redirect(url_for("tickets.index"))
    return redirect(url_for("tickets.detail", tid=found[0]["id"]))


@bp.route("/anon", methods=["GET", "POST"])
def anon():
    """TKT-02 เข้าถึงเรื่องนิรนามด้วยรหัสติดตาม + PIN (ไม่ต้องล็อกอิน ไม่บันทึกว่าใครเปิด)"""
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        found = [t for t in repo().where("tickets", tracking_code=code) if not t.get("submitter_id") and t.get("access_pin_hash")]
        t = found[0] if found else None
        locked = t and parse_dt(t.get("pin_locked_until")) and parse_dt(t["pin_locked_until"]) > now()
        if locked:
            flash(f"ใส่ PIN ผิดหลายครั้ง รหัสติดตามนี้ถูกล็อกชั่วคราว {PIN_LOCK_MINUTES} นาที", "danger")
        elif t and check_password_hash(t["access_pin_hash"], (request.form.get("pin") or "").strip()):
            repo().update("tickets", t["id"], {"pin_failed": 0, "pin_locked_until": None})
            session["anon_tickets"] = list(set((session.get("anon_tickets") or []) + [t["id"]]))[-20:]
            return redirect(url_for("tickets.detail", tid=t["id"]))
        else:
            if t:  # TKT-18 ล็อกชั่วคราวเมื่อใส่ PIN ผิดซ้ำต่อรหัสติดตาม
                fails = int(t.get("pin_failed") or 0) + 1
                upd = {"pin_failed": fails}
                if fails >= PIN_MAX_FAILS:
                    upd = {"pin_failed": 0, "pin_locked_until": to_iso(now() + timedelta(minutes=PIN_LOCK_MINUTES))}
                repo().update("tickets", t["id"], upd)
            flash("รหัสติดตามหรือ PIN ไม่ถูกต้อง", "danger")
        return redirect(url_for("tickets.anon", code=code))
    return render_template("tkt/anon.html", code=(request.args.get("code") or "").strip().upper())


@bp.route("/<tid>")
def detail(tid):
    t = _ticket(tid)
    u = current_user()
    lvl = access(t, u)
    if lvl == "none":
        if not u:
            return redirect(url_for("auth.login", next=request.full_path))
        abort(403)
    if lvl != "owner":
        _log_access(t, "VIEW")
        if t.get("sensitivity") == "RESTRICTED" or lvl == "view":
            audit("ticket.view", "ticket", tid)
    msgs = sorted(repo().where("ticket_messages", ticket_id=tid), key=lambda m: m.get("sent_at", ""))
    if lvl == "owner":
        msgs = [m for m in msgs if not m.get("is_internal")]
    # เรื่องนิรนามไม่มีตัวตนผู้ส่งในระบบ จึงไม่มีอะไรให้เปิดดู
    submitter = repo().get("users", t["submitter_id"]) if t.get("submitter_id") and not t.get("is_anonymous") else None
    rating = repo().get("ticket_ratings", tid)
    logs = sorted(repo().where("ticket_status_logs", ticket_id=tid), key=lambda l: l.get("at", ""))
    targets = [repo().get("positions", p) for p in t.get("target_position_ids") or []]
    reopen_ok = t.get("status") == "CLOSED" and parse_dt(t.get("reopen_until")) and parse_dt(t["reopen_until"]) > now()
    senders = {m["sender_id"]: repo().get("users", m["sender_id"]) for m in msgs if m.get("sender_id")}
    return render_template("tkt/detail.html", t=t, lvl=lvl, msgs=msgs, submitter=submitter, rating=rating, logs=logs,
                           cat=repo().get("ticket_categories", t["category_id"]) or {}, targets=[x for x in targets if x],
                           is_claimer=bool(u) and t.get("handler_id") == u["id"], reopen_ok=reopen_ok, senders=senders,
                           overdue=sla_overdue(t), handler=repo().get("users", t.get("handler_id")))


@bp.route("/<tid>/action", methods=["POST"])
def action(tid):
    t = _ticket(tid)
    u = current_user()
    lvl = access(t, u)
    if lvl == "none" and not u:
        return redirect(url_for("auth.login"))
    act = request.form.get("action")
    reason = request.form.get("reason", "").strip()
    link = url_for("tickets.detail", tid=tid)
    anon_owner = lvl == "owner" and not t.get("submitter_id")  # ไม่บันทึกตัวตนในทุกขั้นตอน (TKT-20)
    uid = None if anon_owner or not u else u["id"]

    if act == "message":
        body = request.form.get("body", "").strip()
        internal = bool(request.form.get("is_internal"))
        if not body:
            return redirect(link)
        if lvl == "owner":
            if t["status"] == "CLOSED":
                abort(400)
            repo().add("ticket_messages", {"ticket_id": tid, "sender_id": uid, "sender_type": "STUDENT", "is_internal": False,
                                           "body": body[:5000], "sent_at": now_iso()})
            if t.get("status") == "WAITING_INFO":
                _status_log(t, "IN_PROGRESS", anonymous=anon_owner)
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
                notify(t.get("submitter_id"), "คำร้องของคุณมีการตอบกลับ", t["title"], link)
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
        notify(t.get("submitter_id"), "คำร้องของคุณได้รับการรับเรื่องแล้ว", t["title"], link)

    elif act == "release":
        if not u or t.get("handler_id") != u["id"]:
            abort(403)
        for c in repo().where("ticket_claims", ticket_id=tid, handler_id=u["id"]):
            if not c.get("released_at"):
                repo().update("ticket_claims", c["id"], {"released_at": now_iso()})
        repo().update("tickets", tid, {"handler_id": None})
        _status_log(t, t["status"], "ปล่อยเรื่อง")

    elif act in ("close", "close_scope"):
        # TKT-17 ปิดเรื่อง "นอกขอบเขต: เรื่องของนายก" ได้โดยผู้รับเรื่องทุกคน ไม่ต้องกดรับเรื่องก่อน
        scope = act == "close_scope"
        if lvl != "handle" or (not scope and t.get("handler_id") != u["id"]) or t.get("status") == "CLOSED":
            abort(403)
        reason = OUT_OF_SCOPE if scope else reason
        if not reason:
            flash("การปิดเรื่องต้องระบุเหตุผล", "danger")
            return redirect(link)
        _status_log(t, "CLOSED", reason)
        reopen = None if scope else to_iso(now() + timedelta(days=int(setting("ticket_reopen_days") or 7)))
        repo().update("tickets", tid, {"status": "CLOSED", "closed_at": now_iso(), "close_reason": reason,
                                       "out_of_scope": scope, "reopen_until": reopen})
        notify(t.get("submitter_id"), "คำร้องของคุณปิดเรื่องแล้ว", reason, link)

    elif act == "escalate":
        # TKT-11 ส่งต่อไปนายกเท่านั้น
        if lvl != "handle" or t.get("routing_level") != "NORMAL" or t.get("status") == "CLOSED":
            abort(403)
        _status_log(t, t["status"], f"ส่งต่อนายกสโมสร: {reason}")
        repo().update("tickets", tid, {"routing_level": "PRESIDENT", "handler_roles": ["PRES"], "handler_id": None})
        notify(list(holders_of_role("PRES") - set(t.get("hidden_user_ids") or [])), "มีคำร้องที่ส่งต่อถึงคุณ", t["title"], link)

    elif act == "reopen":
        if lvl != "owner" or t["status"] != "CLOSED" or not parse_dt(t.get("reopen_until")) or parse_dt(t["reopen_until"]) < now():
            abort(400)
        _status_log(t, "IN_PROGRESS", "ยังไม่ได้รับการแก้ไข", anonymous=anon_owner)
        repo().update("tickets", tid, {"status": "IN_PROGRESS", "closed_at": None, "reopen_count": int(t.get("reopen_count") or 0) + 1})
        notify(t.get("handler_id"), "คำร้องถูกเปิดใหม่", t["title"], link)

    elif act == "rate":
        if lvl != "owner" or t["status"] != "CLOSED" or repo().get("ticket_ratings", tid):
            abort(400)
        score = int(request.form.get("score") or 0)
        if 1 <= score <= 5:
            repo().set("ticket_ratings", tid, {"ticket_id": tid, "score": score, "comment": request.form.get("comment", "")[:500],
                                               "rated_at": now_iso()})
    else:
        abort(400)
    audit(f"ticket.{act}", "ticket", tid, anonymous=anon_owner)
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


def advisor_ticket_summary():
    """ตัวเลขสรุปสำหรับ Dashboard อาจารย์ที่ปรึกษา: เสนอมา / กำลังดำเนินการ / เสร็จสิ้น แยกประเภท เวลาเฉลี่ย ค้างเกิน SLA"""
    min_group = int(setting("ticket_stats_min_group") or 5)
    tickets = repo().all("tickets")
    rows, total = [], {"all": len(tickets), "open": 0, "closed": 0, "overdue": 0, "avg_days": None}
    durations = []
    for c in repo().all("ticket_categories"):
        ts = [t for t in tickets if t.get("category_id") == c["id"]]
        closed = [t for t in ts if t.get("status") == "CLOSED"]
        days = [(parse_dt(t["closed_at"]) - parse_dt(t["created_at"])).total_seconds() / 86400
                for t in closed if parse_dt(t.get("closed_at")) and parse_dt(t.get("created_at"))]
        durations += days
        overdue = len([t for t in ts if sla_overdue(t)])
        total["open"] += len(ts) - len(closed)
        total["closed"] += len(closed)
        total["overdue"] += overdue
        if len(ts) < min_group:
            rows.append({"name": c["name"], "hidden": True, "min": min_group})
        else:
            rows.append({"name": c["name"], "hidden": False, "all": len(ts), "open": len(ts) - len(closed),
                         "closed": len(closed), "overdue": overdue,
                         "avg_days": round(sum(days) / len(days), 1) if days else None})
    total["avg_days"] = round(sum(durations) / len(durations), 1) if durations else None
    if total["all"] < min_group:
        total = {"hidden": True, "min": min_group}
    return rows, total


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
    """TKT-10 เรื่องเลย SLA และยังไม่มีผู้รับ แจ้ง PRES และผู้รับเรื่อง + ล้างโควตานิรนามของวันก่อน (TKT-19)"""
    purge_anon_quota()
    n = 0
    for t in repo().all("tickets"):
        if sla_overdue(t):
            n += 1
            link = url_for("tickets.detail", tid=t["id"])
            hidden = set(t.get("hidden_user_ids") or [])
            for r in t.get("handler_roles") or []:
                notify(list(holders_of_role(r) - hidden), "คำร้องค้างเกิน SLA", t.get("tracking_code"), link)
            if "PRES" not in (t.get("handler_roles") or []):
                notify(list(holders_of_role("PRES") - hidden), "คำร้องค้างเกิน SLA (ดูอย่างเดียว)", t.get("tracking_code"),
                       url_for("tickets.stats"))
    return n
