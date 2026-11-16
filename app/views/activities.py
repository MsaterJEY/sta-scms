"""2.0 กิจกรรมและทีมงาน (ACT-01..13)"""
import hashlib
import hmac
import time

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import (can, current_user, current_year, has_delegation, is_board, login_required, permission_required,
                    user_divisions, user_roles)
from ..services import approval_state, audit, create_approval, decide, display_name, notify, notify_role, resubmit
from ..storage import UploadError, delete_file, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import form_dt, now, now_iso, overlaps, parse_dt, setting, slugify, sort_thai, thai_date

bp = Blueprint("activities", __name__, url_prefix="/activities")

GROUP_LABELS = {"COMM": "กรรมการ", "SUBCOMM": "อนุกรรมการ", "STUDENT": "นักศึกษาคณะวิทยาศาสตร์ฯ"}


def _act(aid):
    a = repo().get("activities", aid)
    if not a:
        abort(404)
    return a


def can_manage(act, user=None):
    user = user or current_user()
    return can("act.manage") and act.get("division_id") in user_divisions(user)


def can_screen(act, user=None):
    user = user or current_user()
    return can("act.screen") and has_delegation(user, "act.screen", act["id"])


def position_open(p):
    if p.get("status") != "OPEN":
        return False
    o, c = parse_dt(p.get("open_at")), parse_dt(p.get("close_at"))
    t = now()
    return (not o or o <= t) and (not c or t <= c)


def eligible_for(p, user):
    """ACT-04 กลุ่มที่สมัครได้; ผู้ถือ BOARD และผู้ใช้ OTHER_MRU/EXTERNAL สมัครไม่ได้"""
    if user.get("account_type") != "SCI":
        return False, "เฉพาะนักศึกษาคณะวิทยาศาสตร์ฯ"
    if is_board(user):
        return False, "ผู้ถือตำแหน่งบอร์ดบริหารสมัครทีมงานไม่ได้"
    groups = set(p.get("eligible_groups") or ["STUDENT"])
    r = user_roles(user)
    if "STUDENT" in groups or (groups & r):
        return True, ""
    return False, "ตำแหน่งนี้เปิดให้เฉพาะ " + ", ".join(GROUP_LABELS[g] for g in groups)


def conflicts_for(user_id, act):
    """ACT-05 เทียบเวลากับกิจกรรมที่ยืนยันเป็นทีมงานแล้ว"""
    out = []
    for s in repo().where("activity_staff", user_id=user_id, confirm_status="CONFIRMED"):
        if s["activity_id"] == act["id"]:
            continue
        other = repo().get("activities", s["activity_id"])
        if other and overlaps(act.get("start_at"), act.get("end_at"), other.get("start_at"), other.get("end_at")):
            out.append(other)
    return out


# ---------- รายการกิจกรรม ----------
@bp.route("/")
@login_required
def index():
    u = current_user()
    year = int(request.args.get("year") or current_year())
    acts = repo().where("activities", academic_year=year)
    divs = {d["id"]: d for d in repo().all("divisions")}
    my_divs = user_divisions(u) if can("act.manage") else set()
    positions = repo().all("activity_positions")
    open_pos = [p for p in positions if position_open(p)]
    act_map = {a["id"]: a for a in acts}
    acts.sort(key=lambda a: a.get("start_at") or "", reverse=True)
    return render_template("act/index.html", acts=acts, divs=divs, my_divs=my_divs, open_pos=open_pos,
                           act_map=act_map, year=year, GROUP_LABELS=GROUP_LABELS)


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<aid>/edit", methods=["GET", "POST"])
@permission_required("act.manage")
def form(aid=None):
    u = current_user()
    act = _act(aid) if aid else None
    if act and not can_manage(act):
        abort(403)
    my_divs = user_divisions(u)
    divs = [d for d in repo().all("divisions") if d["id"] in my_divs]
    if request.method == "POST":
        f = request.form
        if f.get("division_id") not in my_divs:
            abort(403)
        data = {"title": f.get("title", "").strip(), "description": f.get("description", ""),
                "division_id": f.get("division_id"), "location": f.get("location", "").strip(),
                "start_at": form_dt(f.get("start_at")), "end_at": form_dt(f.get("end_at"))}
        if not data["title"] or not data["start_at"] or not data["end_at"]:
            flash("กรุณากรอกชื่อ วันเริ่ม และวันสิ้นสุด", "danger")
            return redirect(request.url)
        if act:
            repo().update("activities", aid, data)
            audit("activity.update", "activity", aid, new=data)
        else:
            data.update({"academic_year": current_year(), "owner_id": u["id"], "status": "PLANNED",
                         "created_at": now_iso()})
            aid = repo().add("activities", data)
            audit("activity.create", "activity", aid, new=data)
        flash("บันทึกกิจกรรมแล้ว", "success")
        return redirect(url_for("activities.detail", aid=aid))
    return render_template("act/form.html", act=act, divs=divs)


@bp.route("/<aid>")
@login_required
def detail(aid):
    act = _act(aid)
    u = current_user()
    manage, screen = can_manage(act), can_screen(act)
    positions = sorted(repo().where("activity_positions", activity_id=aid), key=lambda p: p.get("name", ""))
    my_apps = {a["activity_position_id"]: a for a in repo().where("activity_applications", activity_id=aid, user_id=u["id"])}
    my_staff = next(iter(repo().where("activity_staff", activity_id=aid, user_id=u["id"])), None)
    ctx = {"act": act, "manage": manage, "screen": screen, "positions": positions, "my_apps": my_apps,
           "my_staff": my_staff, "division": repo().get("divisions", act.get("division_id")) or {},
           "owner": repo().get("users", act.get("owner_id")), "GROUP_LABELS": GROUP_LABELS,
           "elig": {p["id"]: eligible_for(p, u) for p in positions}, "open": {p["id"]: position_open(p) for p in positions},
           "conflicts": conflicts_for(u["id"], act),
           "team_events": [e for e in repo().where("team_events", activity_id=aid)
                           if manage or e.get("status") != "WAITING"]}
    if manage or screen or can("act.approve") or can("report.view"):
        apps = repo().where("activity_applications", activity_id=aid)
        users = {a["user_id"]: repo().get("users", a["user_id"]) or {} for a in apps}
        staff = repo().where("activity_staff", activity_id=aid)
        for s in staff:
            users.setdefault(s["user_id"], repo().get("users", s["user_id"]) or {})
        batches = sorted(repo().where("staff_selection_batches", activity_id=aid), key=lambda b: b.get("created_at", ""))
        for b in batches:
            b["state"] = approval_state(repo().get("approval_requests", b.get("approval_request_id")))
        sessions = sorted(repo().where("activity_sessions", activity_id=aid), key=lambda s: s.get("start_at") or "")
        att = repo().where("attendance", activity_id=aid)
        hours = {h["user_id"]: h for h in repo().where("activity_hours", activity_id=aid)}
        docs = repo().where("documents", activity_id=aid)
        evidence = sorted(repo().where("activity_evidence", activity_id=aid), key=lambda e: e.get("created_at", ""))
        pos_map = {p["id"]: p for p in positions}
        ctx.update({"apps": sort_thai(apps, lambda a: users[a["user_id"]].get("first_name")), "users": users,
                    "staff": staff, "batches": batches, "sessions": sessions, "att": att, "hours": hours,
                    "docs": docs, "evidence": evidence, "max_evidence": int(current_app.config["MAX_EVIDENCE_IMAGES"]),
                    "pos_map": pos_map})
    return render_template("act/detail.html", **ctx)


# ---------- ตำแหน่งทีมงาน (ACT-02, ACT-03) ----------
@bp.route("/<aid>/positions", methods=["POST"])
@login_required
def save_position(aid):
    act = _act(aid)
    if not can_manage(act):
        abort(403)
    f = request.form
    groups = [g for g in f.getlist("groups") if g in GROUP_LABELS] or ["STUDENT"]
    data = {"activity_id": aid, "name": f.get("name", "").strip(), "quota": int(f.get("quota") or 1),
            "requirements": f.get("requirements", ""), "eligible_groups": groups,
            "open_at": form_dt(f.get("open_at")), "close_at": form_dt(f.get("close_at"))}
    if not data["name"]:
        flash("กรุณาระบุชื่อตำแหน่ง", "danger")
    elif f.get("position_id"):
        repo().update("activity_positions", f["position_id"], data)
        flash("แก้ไขตำแหน่งแล้ว", "success")
    else:
        data.update({"proposed_by": current_user()["id"], "status": "DRAFT", "created_at": now_iso()})
        pid = repo().add("activity_positions", data)
        audit("activity.position_add", "activity_position", pid, new=data)
        flash("เพิ่มตำแหน่งแล้ว (เปิดรับได้ทันทีโดยไม่ต้องอนุมัติ)", "success")
    return redirect(url_for("activities.detail", aid=aid, _anchor="positions"))


@bp.route("/position/<pid>/status", methods=["POST"])
@login_required
def position_status(pid):
    p = repo().get("activity_positions", pid) or abort(404)
    act = _act(p["activity_id"])
    if not can_manage(act):
        abort(403)
    st = request.form.get("status")
    if st not in ("OPEN", "CLOSED", "DRAFT"):
        abort(400)
    repo().update("activity_positions", pid, {"status": st})
    audit("activity.position_status", "activity_position", pid, new=st)
    if st == "OPEN" and not repo().where("announcements", source_type="activity", source_id=act["id"]):
        _draft_activity_announcement(act)
        flash("สร้างร่างประกาศรับสมัครทีมงานส่งให้ฝ่ายประชาสัมพันธ์แล้ว", "info")
    return redirect(url_for("activities.detail", aid=act["id"], _anchor="positions"))


def _draft_activity_announcement(act):
    """ACT-13 ร่างประกาศรับสมัครทีมงานอัตโนมัติ"""
    lines = [f"ชื่อกิจกรรม: {act.get('title')}", f"วันเริ่ม: {thai_date(act.get('start_at'))}",
             f"วันสิ้นสุด: {thai_date(act.get('end_at'))}", f"สถานที่: {act.get('location') or '-'}", "ตำแหน่งที่เปิดรับ:"]
    for p in repo().where("activity_positions", activity_id=act["id"]):
        g = ", ".join(GROUP_LABELS.get(x, x) for x in p.get("eligible_groups") or [])
        lines.append(f"- {p.get('name')} จำนวน {p.get('quota')} คน (สมัครได้: {g}) ปิดรับ {thai_date(p.get('close_at'))}")
    title = f"รับสมัครทีมงาน {act.get('title')}"
    repo().add("announcements", {
        "category_id": "ACTIVITY", "slug": slugify(title), "title": title, "body": "\n".join(lines), "status": "DRAFT",
        "is_pinned": False, "publish_at": None, "author_id": current_user()["id"], "source_type": "activity",
        "source_id": act["id"], "approval_request_id": None, "created_at": now_iso(),
    })
    notify_role("PR", "มีร่างประกาศรับสมัครทีมงาน", title, url_for("announcements.manage"))


# ---------- สมัครทีมงาน (ACT-04, ACT-05) ----------
@bp.route("/position/<pid>/apply", methods=["POST"])
@login_required
def apply(pid):
    p = repo().get("activity_positions", pid) or abort(404)
    act = _act(p["activity_id"])
    u = current_user()
    ok, why = eligible_for(p, u)
    if not ok:
        flash(why, "danger")
    elif not position_open(p):
        flash("ตำแหน่งนี้ไม่ได้เปิดรับในขณะนี้", "danger")
    elif repo().get("activity_applications", f"{pid}_{u['id']}"):
        flash("คุณสมัครตำแหน่งนี้แล้ว", "warning")
    else:
        conf = conflicts_for(u["id"], act)
        repo().set("activity_applications", f"{pid}_{u['id']}", {
            "activity_id": act["id"], "activity_position_id": pid, "user_id": u["id"], "status": "SUBMITTED",
            "note": request.form.get("note", "")[:1000], "submitted_at": now_iso(),
            "conflict_warning": [c["title"] for c in conf], "screened_by": None, "screen_result": None,
        })
        audit("activity.apply", "activity_application", f"{pid}_{u['id']}")
        msg = "ส่งใบสมัครทีมงานแล้ว"
        if conf:
            msg += " — เตือน: เวลาชนกับ " + ", ".join(c["title"] for c in conf)
        flash(msg, "warning" if conf else "success")
    return redirect(url_for("activities.detail", aid=act["id"]))


@bp.route("/application/<app_id>/screen", methods=["POST"])
@login_required
def screen(app_id):
    a = repo().get("activity_applications", app_id) or abort(404)
    act = _act(a["activity_id"])
    if not can_screen(act):
        abort(403)
    res = request.form.get("result")
    if res not in ("SCREEN_PASS", "SCREEN_FAIL"):
        abort(400)
    repo().update("activity_applications", app_id, {"status": res, "screen_result": res,
                                                    "screen_reason": request.form.get("reason", ""),
                                                    "screened_by": current_user()["id"]})
    audit("activity.screen", "activity_application", app_id, new=res)
    return redirect(url_for("activities.detail", aid=act["id"], _anchor="apps"))


# ---------- ชุดผลคัดเลือกทีมงาน (ACT-06, ACT-07) ----------
@bp.route("/<aid>/batch", methods=["POST"])
@login_required
def create_batch(aid):
    act = _act(aid)
    if not can_screen(act):
        abort(403)
    bid = request.form.get("batch_id")
    batch = repo().get("staff_selection_batches", bid) if bid else None
    picks = []
    for a in repo().where("activity_applications", activity_id=aid):
        res = request.form.get(f"res_{a['id']}")
        if res in ("APPROVED", "RESERVE", "REJECTED") and a["status"] in ("SCREEN_PASS", "SCREEN_FAIL", "IN_BATCH"):
            picks.append((a, res))
    if not picks:
        flash("ยังไม่ได้เลือกผล", "danger")
        return redirect(url_for("activities.detail", aid=aid, _anchor="apps"))
    if batch:
        for s in repo().where("activity_staff", batch_id=batch["id"]):
            repo().delete("activity_staff", s["id"])
    else:
        bid = repo().add("staff_selection_batches", {"activity_id": aid, "status": "PENDING", "approval_request_id": None,
                                                     "created_by": current_user()["id"], "created_at": now_iso()})
    for a, res in picks:
        repo().set("activity_staff", f"{bid}_{a['id']}", {
            "batch_id": bid, "activity_id": aid, "application_id": a["id"], "user_id": a["user_id"],
            "activity_position_id": a["activity_position_id"], "result": res, "confirm_status": None, "work_status": None,
        })
        repo().update("activity_applications", a["id"], {"status": "IN_BATCH"})
    batch = repo().get("staff_selection_batches", bid)
    if batch.get("approval_request_id"):
        resubmit(repo().get("approval_requests", batch["approval_request_id"]))
    else:
        req_id = create_approval("STAFF_FINAL", "staff_selection_batch", bid, f"ผลคัดเลือกทีมงาน: {act.get('title')}",
                                 url_for("activities.batch_view", bid=bid))
        repo().update("staff_selection_batches", bid, {"approval_request_id": req_id})
    repo().update("staff_selection_batches", bid, {"status": "PENDING"})
    flash("ส่งชุดผลคัดเลือกทีมงานเพื่ออนุมัติแล้ว", "success")
    return redirect(url_for("activities.batch_view", bid=bid))


def finalize_staff_batch(batch):
    if batch.get("status") == "APPROVED":
        return
    repo().update("staff_selection_batches", batch["id"], {"status": "APPROVED", "approved_at": now_iso()})
    act = repo().get("activities", batch["activity_id"]) or {}
    for s in repo().where("activity_staff", batch_id=batch["id"]):
        if s.get("removed"):
            continue
        repo().update("activity_applications", s["application_id"], {"status": s["result"]})
        msg = {"APPROVED": "ผ่านการคัดเลือก กรุณายืนยันสิทธิ์", "RESERVE": "อยู่ในรายชื่อสำรอง",
               "REJECTED": "ไม่ผ่านการคัดเลือก"}[s["result"]]
        notify(s["user_id"], f"ผลคัดเลือกทีมงาน {act.get('title')}", msg, url_for("activities.detail", aid=act.get("id")))
    audit("activity.batch_approved", "staff_selection_batch", batch["id"])


@bp.route("/batch/<bid>", methods=["GET", "POST"])
@login_required
def batch_view(bid):
    batch = repo().get("staff_selection_batches", bid) or abort(404)
    act = _act(batch["activity_id"])
    if not (can_manage(act) or can_screen(act) or can("act.approve") or can("report.view")):
        abort(403)
    req = repo().get("approval_requests", batch.get("approval_request_id"))
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("approve", "reject"):
            if not can("act.approve"):
                abort(403)
            ok, msg = decide(req, "APPROVE" if action == "approve" else "REJECT", request.form.get("reason", ""))
            flash(msg, "success" if ok else "danger")
            if ok and action == "reject":
                repo().update("staff_selection_batches", bid, {"status": "RECONSIDER"})
            elif ok and approval_state(repo().get("approval_requests", req["id"]))["approved"]:
                finalize_staff_batch(repo().get("staff_selection_batches", bid))
        elif action == "remove" and can("act.approve"):
            s = repo().get("activity_staff", request.form.get("staff_id"))
            if s and s["batch_id"] == bid and batch.get("status") != "APPROVED":
                repo().update("activity_staff", s["id"], {"removed": not s.get("removed")})
        return redirect(url_for("activities.batch_view", bid=bid))
    rows = repo().where("activity_staff", batch_id=bid)
    users = {s["user_id"]: repo().get("users", s["user_id"]) or {} for s in rows}
    pos_map = {p["id"]: p for p in repo().where("activity_positions", activity_id=act["id"])}
    rows.sort(key=lambda s: (s["result"] != "APPROVED", pos_map.get(s["activity_position_id"], {}).get("name", "")))
    return render_template("act/batch_view.html", batch=batch, act=act, rows=rows, users=users, pos_map=pos_map,
                           state=approval_state(req), req=req)


@bp.route("/staff/<sid>/confirm", methods=["POST"])
@login_required
def confirm(sid):
    """ACT-08 ยืนยันหรือสละสิทธิ์"""
    s = repo().get("activity_staff", sid) or abort(404)
    u = current_user()
    batch = repo().get("staff_selection_batches", s["batch_id"]) or {}
    if s["user_id"] != u["id"] or s["result"] != "APPROVED" or s.get("confirm_status") or batch.get("status") != "APPROVED":
        abort(400)
    act = _act(s["activity_id"])
    if request.form.get("action") == "confirm":
        conf = conflicts_for(u["id"], act)
        repo().update("activity_staff", sid, {"confirm_status": "CONFIRMED", "work_status": "WORKING"})
        repo().update("activity_applications", s["application_id"], {"status": "CONFIRMED"})
        flash("ยืนยันเป็นทีมงานแล้ว" + (" — เตือน: เวลาชนกับ " + ", ".join(c["title"] for c in conf) if conf else ""),
              "warning" if conf else "success")
    else:
        repo().update("activity_staff", sid, {"confirm_status": "DECLINED"})
        repo().update("activity_applications", s["application_id"], {"status": "DECLINED"})
        notify(act.get("owner_id"), "มีทีมงานสละสิทธิ์", f"{display_name(u)} - {act.get('title')}",
               url_for("activities.detail", aid=act["id"]))
        flash("สละสิทธิ์แล้ว", "info")
    audit("activity.staff_" + request.form.get("action", "decline"), "activity_staff", sid)
    return redirect(url_for("activities.detail", aid=act["id"]))


# ---------- รอบกิจกรรม QR เช็กชื่อ ชั่วโมง (ACT-09, ACT-10) ----------
def _qr_token(session_id, offset=0):
    step = int(setting("qr_rotate_seconds") or 30)
    window = int(time.time() // step) + offset
    msg = f"{session_id}:{window}".encode()
    return hmac.new(current_app.config["SECRET_KEY"].encode(), msg, hashlib.sha256).hexdigest()[:16]


@bp.route("/<aid>/sessions", methods=["POST"])
@login_required
def add_session(aid):
    act = _act(aid)
    if not can_manage(act):
        abort(403)
    start, end = form_dt(request.form.get("start_at")), form_dt(request.form.get("end_at"))
    if not start or not end or parse_dt(end) <= parse_dt(start):
        flash("เวลาเริ่ม/สิ้นสุดไม่ถูกต้อง", "danger")
    else:
        repo().add("activity_sessions", {"activity_id": aid, "name": request.form.get("name", "").strip() or "รอบกิจกรรม",
                                         "start_at": start, "end_at": end})
        flash("เพิ่มรอบกิจกรรมแล้ว", "success")
    return redirect(url_for("activities.detail", aid=aid, _anchor="sessions"))


@bp.route("/session/<sid>/qr")
@login_required
def session_qr(sid):
    s = repo().get("activity_sessions", sid) or abort(404)
    act = _act(s["activity_id"])
    if not can_manage(act):
        abort(403)
    return render_template("act/qr.html", s=s, act=act, rotate=int(setting("qr_rotate_seconds") or 30))


@bp.route("/session/<sid>/token")
@login_required
def session_token(sid):
    s = repo().get("activity_sessions", sid) or abort(404)
    if not can_manage(_act(s["activity_id"])):
        abort(403)
    url = url_for("activities.checkin", sid=sid, t=_qr_token(sid), _external=True)
    count = len(repo().where("attendance", session_id=sid))
    return jsonify({"url": url, "count": count})


def recompute_hours(user_id, act_id):
    existing = repo().get("activity_hours", f"{act_id}_{user_id}")
    if existing and existing.get("source") == "MANUAL":
        return
    sessions = {s["id"]: s for s in repo().where("activity_sessions", activity_id=act_id)}
    total = 0.0
    for a in repo().where("attendance", activity_id=act_id, user_id=user_id):
        s = sessions.get(a["session_id"])
        if s:
            total += (parse_dt(s["end_at"]) - parse_dt(s["start_at"])).total_seconds() / 3600
    repo().set("activity_hours", f"{act_id}_{user_id}", {"user_id": user_id, "activity_id": act_id,
                                                          "hours": round(total, 2), "source": "AUTO", "updated_at": now_iso()})


@bp.route("/checkin/<sid>")
@login_required
def checkin(sid):
    s = repo().get("activity_sessions", sid) or abort(404)
    act = _act(s["activity_id"])
    u = current_user()
    t = request.args.get("t", "")
    ok_token = any(hmac.compare_digest(t, _qr_token(sid, off)) for off in (0, -1))
    staff = [x for x in repo().where("activity_staff", activity_id=act["id"], user_id=u["id"])
             if x.get("confirm_status") == "CONFIRMED"]
    if not ok_token:
        msg, cls = "QR หมดอายุแล้ว กรุณาสแกนใหม่", "danger"
    elif not staff:
        msg, cls = "เช็กชื่อได้เฉพาะทีมงานที่ยืนยันแล้วของกิจกรรมนี้", "danger"
    elif repo().get("attendance", f"{sid}_{u['id']}"):
        msg, cls = "คุณเช็กชื่อรอบนี้ไปแล้ว", "info"
    else:
        repo().set("attendance", f"{sid}_{u['id']}", {"session_id": sid, "activity_id": act["id"], "user_id": u["id"],
                                                      "checked_in_at": now_iso(), "method": "QR"})
        recompute_hours(u["id"], act["id"])
        msg, cls = f"เช็กชื่อสำเร็จ: {act.get('title')} ({s.get('name')})", "success"
    return render_template("act/checkin.html", msg=msg, cls=cls, act=act, s=s)


@bp.route("/<aid>/attendance", methods=["POST"])
@login_required
def manual_attendance(aid):
    act = _act(aid)
    if not can_manage(act):
        abort(403)
    action = request.form.get("action")
    uid = request.form.get("user_id")
    if action == "checkin":
        sid = request.form.get("session_id")
        if repo().get("activity_sessions", sid) and not repo().get("attendance", f"{sid}_{uid}"):
            repo().set("attendance", f"{sid}_{uid}", {"session_id": sid, "activity_id": aid, "user_id": uid,
                                                      "checked_in_at": now_iso(), "method": "MANUAL",
                                                      "by": current_user()["id"]})
            recompute_hours(uid, aid)
    elif action == "hours":
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("บันทึกชั่วโมงด้วยมือต้องระบุเหตุผล", "danger")
        else:
            repo().set("activity_hours", f"{aid}_{uid}", {"user_id": uid, "activity_id": aid,
                                                          "hours": float(request.form.get("hours") or 0),
                                                          "source": "MANUAL", "reason": reason,
                                                          "recorded_by": current_user()["id"], "updated_at": now_iso()})
            audit("activity.manual_hours", "activity_hours", f"{aid}_{uid}", new={"reason": reason})
            flash("บันทึกชั่วโมงแล้ว", "success")
    return redirect(url_for("activities.detail", aid=aid, _anchor="sessions"))


# ---------- ผลดำเนินงาน (ACT-11) ----------
@bp.route("/<aid>/report", methods=["POST"])
@login_required
def report(aid):
    act = _act(aid)
    if not can_manage(act):
        abort(403)
    repo().update("activities", aid, {"result_summary": request.form.get("result_summary", ""),
                                      "participants": int(request.form.get("participants") or 0),
                                      "status": "DONE" if request.form.get("done") else act.get("status"),
                                      "result_at": now_iso()})
    if request.form.get("done"):
        attended = {a["user_id"] for a in repo().where("attendance", activity_id=aid)}
        for s in repo().where("activity_staff", activity_id=aid, confirm_status="CONFIRMED"):
            repo().update("activity_staff", s["id"], {"work_status": "DONE" if s["user_id"] in attended else "ABSENT"})
    # ภาพหลักฐาน: รูปภาพเท่านั้น จำกัดจำนวนต่อกิจกรรม
    limit = int(current_app.config["MAX_EVIDENCE_IMAGES"])
    have = len(repo().where("activity_evidence", activity_id=aid))
    files = [fs for fs in request.files.getlist("evidence") if fs and fs.filename]
    if have + len(files) > limit:
        flash(f"ภาพหลักฐานเก็บได้ไม่เกิน {limit} รูปต่อกิจกรรม (มีแล้ว {have} รูป) — ไม่ได้บันทึกรูปที่เลือก", "danger")
        files = []
    for fs in files:
        try:
            data, ext, ctype = validate_upload(fs, images_only=True, allow_pdf=False)
        except UploadError as e:
            flash(f"{fs.filename}: {e}", "danger")
            continue
        path = save_bytes("evidence", data, ext, ctype)
        repo().add("activity_evidence", {"activity_id": aid, "name": fs.filename, "file_path": path,
                                         "created_by": current_user()["id"], "created_at": now_iso()})
    audit("activity.report", "activity", aid)
    flash("บันทึกผลดำเนินงานแล้ว", "success")
    return redirect(url_for("activities.detail", aid=aid, _anchor="report"))


def _can_view_evidence(act):
    return can_manage(act) or can_screen(act) or can("act.approve") or can("report.view")


@bp.route("/evidence/<eid>")
@login_required
def evidence_image(eid):
    e = repo().get("activity_evidence", eid) or abort(404)
    if not _can_view_evidence(_act(e["activity_id"])):
        abort(403)
    return Response(load_bytes(e["file_path"]), mimetype=mimetype_for(e["file_path"]),
                    headers={"Cache-Control": "private, max-age=3600"})


@bp.route("/evidence/<eid>/delete", methods=["POST"])
@login_required
def evidence_delete(eid):
    e = repo().get("activity_evidence", eid) or abort(404)
    act = _act(e["activity_id"])
    if not can_manage(act):
        abort(403)
    delete_file(e["file_path"])
    repo().delete("activity_evidence", eid)
    audit("activity.evidence_delete", "activity_evidence", eid)
    flash("ลบภาพแล้ว", "info")
    return redirect(url_for("activities.detail", aid=act["id"], _anchor="report"))


@bp.route("/my")
@login_required
def my():
    u = current_user()
    apps = repo().where("activity_applications", user_id=u["id"])
    staff = repo().where("activity_staff", user_id=u["id"])
    hours = repo().where("activity_hours", user_id=u["id"])
    ids = {a["activity_id"] for a in apps} | {h["activity_id"] for h in hours}
    acts = {i: repo().get("activities", i) or {} for i in ids}
    pos = {a["activity_position_id"]: repo().get("activity_positions", a["activity_position_id"]) or {} for a in apps}
    staff_by_app = {s["application_id"]: s for s in staff}
    return render_template("act/my.html", apps=apps, acts=acts, pos=pos, staff_by_app=staff_by_app, hours=hours,
                           total=sum(float(h.get("hours") or 0) for h in hours))


# ---------- ส่งออก (ACT-12) ----------
@dataset("activity_staff")
def ds_staff(args):
    act = _act(args.get("activity_id"))
    if not (can_manage(act) or can("act.approve") or can("report.view")):
        abort(403)
    pos_map = {p["id"]: p for p in repo().where("activity_positions", activity_id=act["id"])}
    rows = []
    for b in repo().where("staff_selection_batches", activity_id=act["id"]):
        if not approval_state(repo().get("approval_requests", b.get("approval_request_id")))["approved"]:
            continue  # เฉพาะชุดที่อนุมัติครบ
        for s in repo().where("activity_staff", batch_id=b["id"]):
            if s.get("removed") or s["result"] != "APPROVED":
                continue
            u = repo().get("users", s["user_id"]) or {}
            rows.append([pos_map.get(s["activity_position_id"], {}).get("name"), u.get("student_code"),
                         u.get("first_name"), u.get("last_name"),
                         {"CONFIRMED": "ยืนยัน", "DECLINED": "สละสิทธิ์"}.get(s.get("confirm_status"), "รอยืนยัน"),
                         u.get("phone") if can_manage(act) else ""])
    for s in repo().where("activity_staff", activity_id=act["id"], source="TEAM"):  # จากโมดูลรับสมัครทีมงาน
        u = repo().get("users", s["user_id"]) or {}
        rows.append([s.get("position_name"), u.get("student_code"), u.get("first_name"), u.get("last_name"), "ยืนยัน",
                     u.get("phone") if can_manage(act) else ""])
    rows.sort(key=lambda r: (r[0] or "", r[2] or ""))
    return "activity-staff", ["ตำแหน่ง", "รหัสนักศึกษา", "ชื่อ", "นามสกุล", "การยืนยัน", "เบอร์โทร"], rows


@dataset("activity_hours")
def ds_hours(args):
    act = _act(args.get("activity_id"))
    if not (can_manage(act) or can("report.view")):
        abort(403)
    rows = []
    for h in repo().where("activity_hours", activity_id=act["id"]):
        u = repo().get("users", h["user_id"]) or {}
        rows.append([act.get("title"), u.get("student_code"), u.get("first_name"), u.get("last_name"), h.get("hours"),
                     "อัตโนมัติ" if h.get("source") == "AUTO" else "บันทึกด้วยมือ", h.get("reason")])
    return "activity-hours", ["กิจกรรม", "รหัสนักศึกษา", "ชื่อ", "นามสกุล", "ชั่วโมง", "ที่มา", "เหตุผล"], sort_thai(rows, lambda r: r[2])


@dataset("activity_applicants")
def ds_act_apps(args):
    act = _act(args.get("activity_id"))
    if not (can_manage(act) or can_screen(act)):
        abort(403)
    pos_map = {p["id"]: p for p in repo().where("activity_positions", activity_id=act["id"])}
    rows = []
    for a in repo().where("activity_applications", activity_id=act["id"]):
        u = repo().get("users", a["user_id"]) or {}
        rows.append([pos_map.get(a["activity_position_id"], {}).get("name"), u.get("student_code"), u.get("first_name"),
                     u.get("last_name"), u.get("year_level"), a.get("status"), ", ".join(a.get("conflict_warning") or [])])
    return "activity-applicants", ["ตำแหน่ง", "รหัสนักศึกษา", "ชื่อ", "นามสกุล", "ชั้นปี", "สถานะ", "เวลาชน"], rows
