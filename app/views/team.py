"""รับสมัครทีมงานดำเนินกิจกรรม (ย้ายมาจากระบบ Google Apps Script + Sheets เดิม)

แนวคิดเดิมคงไว้: กิจกรรม -> ตำแหน่งที่เปิดรับ (สังกัดกอง มีโควตา เงื่อนไขประเภท/กอง/เพศ) -> ใบสมัคร -> แอดมินพิจารณา
สิ่งที่เปลี่ยนเมื่อย้ายมาเว็บนี้:
- ผู้สมัครใช้บัญชีของเว็บ ข้อมูลกอง/ประเภท (กรรมการ/อนุกรรมการ) อ่านจากตำแหน่งที่ถือในวาระปัจจุบัน แทนชีตสมาชิก
- บัญชีแอดมิน/แอดมินขั้นสูง แทนด้วยสิทธิ์ team.manage / team.delete
- เพศอ่านจากข้อมูลบัญชี (users.gender) แทนการเดาจากคำนำหน้าชื่อ
- เชื่อมกับโมดูลกิจกรรมได้ (team_events.activity_id): ผู้ที่ผ่านการคัดเลือกจะเป็นทีมงานที่ยืนยันแล้วของกิจกรรมนั้น
  (activity_staff รหัส team_<ใบสมัคร>) จึงเช็กชื่อด้วย QR และได้ชั่วโมงกิจกรรม
"""
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, current_year, login_required, user_holdings
from ..services import audit, display_name, notify
from ..storage import UploadError, delete_file, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import now_iso, sort_thai, thai_date

bp = Blueprint("team", __name__, url_prefix="/team")

EVENT_STATUS = {"OPEN": ("เปิดรับสมัคร", "success"), "WAITING": ("รอเปิดรับสมัคร", "warning"),
                "CLOSED": ("ปิดรับสมัครแล้ว", "secondary")}
APP_STATUS = {"PENDING": ("รอพิจารณา", "warning"), "APPROVED": ("ผ่านการคัดเลือก", "success"),
              "REJECTED": ("ไม่ผ่าน", "danger")}
TYPE_LABELS = {"ALL": "คณะกรรมการและอนุกรรมการ", "COMM": "เฉพาะคณะกรรมการ", "SUBCOMM": "เฉพาะอนุกรรมการ"}
MEMBER_LABELS = {"COMM": "คณะกรรมการ", "SUBCOMM": "อนุกรรมการ"}
GENDER_LABELS = {"M": "ชาย", "F": "หญิง"}


@bp.app_context_processor
def _labels():
    return {"TEAM_EVENT_STATUS": EVENT_STATUS, "TEAM_APP_STATUS": APP_STATUS, "TEAM_TYPES": TYPE_LABELS,
            "TEAM_MEMBERS": MEMBER_LABELS, "TEAM_GENDERS": GENDER_LABELS}


# ---------- ตัวช่วย ----------
def is_manager():
    return bool(current_user()) and (can("team.manage") or can("team.delete"))


def manager_required():
    if not current_user():
        return redirect(url_for("auth.login", next=request.full_path))
    if not is_manager():
        abort(403)
    return None


def _event(eid):
    ev = repo().get("team_events", eid)
    if not ev:
        abort(404)
    return ev


def _int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def divisions():
    return sort_thai(repo().all("divisions"), lambda d: d.get("name"))


def sort_events(events):
    """ปักหมุดขึ้นก่อน ที่เหลือใหม่สุดขึ้นก่อน และติดป้าย "ล่าสุด" ให้กิจกรรมที่ไม่ปักหมุดตัวแรก"""
    events = sorted(events, key=lambda e: e.get("created_at") or "", reverse=True)
    events.sort(key=lambda e: not e.get("is_pinned"))
    marked = False
    for e in events:
        e["is_latest"] = not e.get("is_pinned") and not marked
        marked = marked or e["is_latest"]
    return events


def date_range(ev):
    a, b = ev.get("start_date"), ev.get("end_date")
    if not a:
        return "-"
    s = thai_date(a, with_time=False)
    return f"{s} - {thai_date(b, with_time=False)}" if b and b != a else s


def member_info(user):
    """กองและประเภทสมาชิกจากตำแหน่งที่ถือในวาระปัจจุบัน (บอร์ด/กรรมการ = คณะกรรมการ)"""
    info = {"type": None, "division_id": None}
    for h in user_holdings(user["id"]):
        level = h["position"].get("level")
        kind = {"BOARD": "COMM", "COMMITTEE": "COMM", "SUBCOMMITTEE": "SUBCOMM"}.get(level)
        if not kind:
            continue
        if info["type"] != "COMM":
            info["type"] = kind
        info["division_id"] = info["division_id"] or h["position"].get("division_id")
    return info


def tally(pos, apps):
    rows = [a for a in apps if a.get("position_id") == pos["id"] and a.get("status") != "REJECTED"]
    out = {"count": len(rows)}
    for key, field in (("M", "gender"), ("F", "gender"), ("COMM", "member_type"), ("SUBCOMM", "member_type")):
        out[key] = sum(1 for a in rows if a.get(field) == key)
    return out


def quota_block(pos, t, gender, member_type):
    """ลำดับตรวจ: จำนวนรวม > จำนวนตามเพศ > จำนวนตามประเภท คืนข้อความถ้าเต็ม"""
    if t["count"] >= _int(pos.get("quota")):
        return "ตำแหน่งนี้รับครบจำนวนแล้ว"
    if pos.get("gender") == "SPLIT" and gender in GENDER_LABELS:
        if t[gender] >= _int(pos.get("male_quota" if gender == "M" else "female_quota")):
            return f"ตำแหน่งนี้รับเพศ{GENDER_LABELS[gender]}ครบจำนวนแล้ว"
    if pos.get("applicant_type", "ALL") == "ALL" and pos.get("type_cap") and member_type in MEMBER_LABELS:
        if t[member_type] >= _int(pos.get("comm_quota" if member_type == "COMM" else "sub_quota")):
            return f"ตำแหน่งนี้รับ{MEMBER_LABELS[member_type]}ครบจำนวนแล้ว"
    return None


def eligibility(pos, user, info, div_names):
    if not info["type"]:
        return "เปิดรับเฉพาะคณะกรรมการและอนุกรรมการสโมสรในวาระปัจจุบัน"
    want = pos.get("applicant_type") or "ALL"
    if want != "ALL" and info["type"] != want:
        return "ตำแหน่งนี้เปิดรับ" + TYPE_LABELS[want]
    limit = pos.get("restrict_divisions") or []
    if limit and info["division_id"] not in limit:
        return "ตำแหน่งนี้เปิดรับเฉพาะผู้ที่อยู่ " + ", ".join(div_names.get(d, d) for d in limit)
    if pos.get("gender") in GENDER_LABELS and user.get("gender") and user["gender"] != pos["gender"]:
        return f"ตำแหน่งนี้เปิดรับเฉพาะเพศ{GENDER_LABELS[pos['gender']]}"
    return None


def gender_text(pos):
    g = pos.get("gender") or "ANY"
    if g == "SPLIT":
        return f"ชาย {_int(pos.get('male_quota'))} · หญิง {_int(pos.get('female_quota'))}"
    return f"{GENDER_LABELS[g]}เท่านั้น" if g in GENDER_LABELS else "ไม่จำกัดเพศ"


def positions_of(eid, apps=None, div_names=None):
    """ตำแหน่งของกิจกรรม พร้อมยอดผู้สมัคร เรียงตามกองแล้วตามลำดับที่สร้าง"""
    apps = repo().where("team_applications", event_id=eid) if apps is None else apps
    div_names = div_names or {d["id"]: d.get("name") for d in repo().all("divisions")}
    rows = sorted(repo().where("team_positions", event_id=eid), key=lambda p: p.get("created_at") or "")
    for p in rows:
        p["tally"] = tally(p, apps)
        p["full"] = p["tally"]["count"] >= _int(p.get("quota"))
        p["division_name"] = div_names.get(p.get("division_id"), "-")
        p["gender_text"] = gender_text(p)
        p["restrict_names"] = [div_names.get(d, d) for d in p.get("restrict_divisions") or []]
    rows.sort(key=lambda p: p["division_name"])
    return rows


# ---------- เชื่อมกับโมดูลกิจกรรม ----------
def linkable_activities():
    return sorted(repo().where("activities", academic_year=current_year()), key=lambda a: a.get("start_at") or "", reverse=True)


def sync_staff(ev, a, pos=None):
    """ให้รายชื่อทีมงานของกิจกรรมที่เชื่อมไว้ตรงกับผลใบสมัครนี้ (เฉพาะผู้สมัครที่มีบัญชี)"""
    sid = f"team_{a['id']}"
    act_id = ev.get("activity_id")
    if act_id and a.get("user_id") and a.get("status") == "APPROVED":
        others = repo().where("activity_staff", activity_id=act_id, user_id=a["user_id"])
        if not any(s["id"] != sid and s.get("confirm_status") == "CONFIRMED" for s in others):
            pos = pos or repo().get("team_positions", a.get("position_id")) or {}
            repo().set("activity_staff", sid, {
                "batch_id": None, "activity_id": act_id, "application_id": None, "team_application_id": a["id"],
                "user_id": a["user_id"], "activity_position_id": None, "position_name": pos.get("name"),
                "result": "APPROVED", "confirm_status": "CONFIRMED", "work_status": "WORKING", "source": "TEAM",
            })
            return
    if repo().get("activity_staff", sid):
        repo().delete("activity_staff", sid)


# ---------- หน้าสาธารณะ: กิจกรรมที่เปิดรับสมัคร ----------
def open_events():
    return sort_events(repo().where("team_events", status="OPEN"))


@bp.route("/")
def index():
    events = open_events()
    apps = repo().all("team_applications") if events else []
    div_names = {d["id"]: d.get("name") for d in repo().all("divisions")}
    for ev in events:
        ev_apps = [a for a in apps if a.get("event_id") == ev["id"]]
        pos = positions_of(ev["id"], ev_apps, div_names)
        ev["open_positions"] = [p for p in pos if not p["full"]]
        ev["full_count"] = len(pos) - len(ev["open_positions"])
        ev["position_count"] = len(pos)
        ev["applicant_count"] = sum(1 for a in ev_apps if a.get("status") != "REJECTED")
        ev["dates"] = date_range(ev)
    return render_template("team/index.html", events=events, manager=is_manager())


@bp.route("/<eid>")
def detail(eid):
    ev = _event(eid)
    u = current_user()
    manager = is_manager()
    apps = repo().where("team_applications", event_id=eid)
    mine = [a for a in apps if u and a.get("user_id") == u["id"]]
    if ev.get("status") == "WAITING" and not manager and not mine:
        abort(404)
    div_names = {d["id"]: d.get("name") for d in repo().all("divisions")}
    positions = positions_of(eid, apps, div_names)
    info = member_info(u) if u else None
    active = next((a for a in mine if a.get("status") != "REJECTED"), None)
    for p in positions:
        p["pct"] = min(100, round(p["tally"]["count"] * 100 / max(1, _int(p.get("quota")))))
        p["block"] = None
        if u:
            p["block"] = eligibility(p, u, info, div_names) or quota_block(p, p["tally"], u.get("gender"), info["type"])
        elif p["full"]:
            p["block"] = "ตำแหน่งนี้รับครบจำนวนแล้ว"
        p["need_gender"] = bool(u) and not u.get("gender") and (p.get("gender") or "ANY") != "ANY"
    groups = {}
    for p in positions:
        groups.setdefault(p["division_name"], []).append(p)
    return render_template("team/detail.html", ev=ev, groups=groups, dates=date_range(ev), manager=manager,
                           info=info, active=active, div_names=div_names,
                           can_apply=bool(u) and ev.get("status") == "OPEN" and not active)


@bp.route("/cover/<eid>")
def cover(eid):
    ev = _event(eid)
    if not ev.get("cover_path") or (ev.get("status") == "WAITING" and not is_manager()):
        abort(404)
    return Response(load_bytes(ev["cover_path"]), mimetype=mimetype_for(ev["cover_path"]),
                    headers={"Cache-Control": "public, max-age=86400"})


@bp.route("/position/<pid>/apply", methods=["POST"])
@login_required
def apply(pid):
    pos = repo().get("team_positions", pid) or abort(404)
    ev = _event(pos["event_id"])
    u = current_user()
    back = redirect(url_for("team.detail", eid=ev["id"]))
    if ev.get("status") != "OPEN":
        flash("กิจกรรมนี้ปิดรับสมัครแล้ว", "danger")
        return back
    apps = repo().where("team_applications", event_id=ev["id"])
    if any(a.get("user_id") == u["id"] and a.get("status") != "REJECTED" for a in apps):
        flash("คุณได้สมัครเข้าร่วมกิจกรรมนี้ไปแล้ว ไม่สามารถสมัครซ้ำได้", "warning")
        return back
    gender = u.get("gender")
    if not gender and (pos.get("gender") or "ANY") != "ANY":
        gender = request.form.get("gender")
        if gender not in GENDER_LABELS:
            flash("ตำแหน่งนี้กำหนดเงื่อนไขเรื่องเพศ กรุณาระบุเพศก่อนสมัคร", "danger")
            return back
        repo().update("users", u["id"], {"gender": gender})
        u["gender"] = gender
    info = member_info(u)
    div_names = {d["id"]: d.get("name") for d in repo().all("divisions")}
    why = eligibility(pos, u, info, div_names) or quota_block(pos, tally(pos, apps), gender, info["type"])
    if why:
        flash(why, "danger")
        return back
    aid = repo().add("team_applications", {
        "event_id": ev["id"], "position_id": pid, "user_id": u["id"], "full_name": display_name(u),
        "student_code": u.get("student_code") or "", "year_level": u.get("year_level"),
        "home_division_id": info["division_id"], "member_type": info["type"], "gender": gender,
        "status": "PENDING", "submitted_at": now_iso(), "added_by": None,
    })
    # ตรวจซ้ำหลังบันทึก: ถ้ามีคำขอที่มาก่อนทำให้ซ้ำหรือเกินโควตา (กดพร้อมกัน) ให้ถอนรายการนี้ออก
    rows = sorted(repo().where("team_applications", event_id=ev["id"]), key=lambda a: (a.get("submitted_at") or "", a["id"]))
    before = rows[:next((i for i, a in enumerate(rows) if a["id"] == aid), len(rows))]
    lost = "คุณได้สมัครเข้าร่วมกิจกรรมนี้ไปแล้ว" if any(
        a.get("user_id") == u["id"] and a.get("status") != "REJECTED" for a in before) else quota_block(
        pos, tally(pos, before), gender, info["type"])
    if lost:
        repo().delete("team_applications", aid)
        flash(lost, "danger")
        return back
    audit("team.apply", "team_application", aid)
    flash(f"สมัครสำเร็จ: {pos.get('name')} — รอผลการพิจารณา", "success")
    return redirect(url_for("team.my"))


@bp.route("/my")
@login_required
def my():
    u = current_user()
    apps = sorted(repo().where("team_applications", user_id=u["id"]), key=lambda a: a.get("submitted_at") or "", reverse=True)
    events = {a["event_id"]: repo().get("team_events", a["event_id"]) or {} for a in apps}
    positions = {a["position_id"]: repo().get("team_positions", a["position_id"]) or {} for a in apps}
    div_names = {d["id"]: d.get("name") for d in repo().all("divisions")}
    for a in apps:
        a["activity_id"] = events[a["event_id"]].get("activity_id") if a.get("status") == "APPROVED" else None
    return render_template("team/my.html", apps=apps, events=events, positions=positions, div_names=div_names,
                           info=member_info(u))


# ---------- จัดการกิจกรรม ----------
@bp.route("/manage")
def manage():
    denied = manager_required()
    if denied:
        return denied
    events = sort_events(repo().all("team_events"))
    apps = repo().all("team_applications")
    for ev in events:
        ev["applicant_count"] = sum(1 for a in apps if a.get("event_id") == ev["id"] and a.get("status") != "REJECTED")
        ev["pending_count"] = sum(1 for a in apps if a.get("event_id") == ev["id"] and a.get("status") == "PENDING")
        ev["dates"] = date_range(ev)
    stats = {k: sum(1 for e in events if e.get("status") == k) for k in EVENT_STATUS}
    q = (request.args.get("q") or "").strip().lower()
    status = request.args.get("status") or ""
    sort = request.args.get("sort") or "latest"
    shown = [e for e in events if (not q or q in (e.get("title") or "").lower()) and (not status or e.get("status") == status)]
    if sort == "name":
        shown = sort_thai(shown, lambda e: e.get("title"))
    elif sort == "applicants":
        shown.sort(key=lambda e: e["applicant_count"], reverse=True)
    elif sort == "oldest":
        shown.sort(key=lambda e: e.get("created_at") or "")
    groups = [(k, [e for e in shown if e.get("status") == k]) for k in ("OPEN", "WAITING", "CLOSED")]
    return render_template("team/manage.html", groups=groups, stats=stats, total=len(events), shown=len(shown),
                           q=request.args.get("q") or "", status=status, sort=sort)


@bp.route("/manage/new", methods=["GET", "POST"])
@bp.route("/manage/<eid>/edit", methods=["GET", "POST"])
def form(eid=None):
    denied = manager_required()
    if denied:
        return denied
    ev = _event(eid) if eid else None
    # wizard = ขั้นตอนสร้างใหม่ 3 ขั้น (กิจกรรมถูกบันทึกเป็น "รอเปิดรับสมัคร" ตั้งแต่ออกจากขั้นที่ 1)
    wizard = not ev or request.values.get("wizard") == "1"

    def page(event, step):
        positions = positions_of(eid) if ev else []
        return render_template("team/form.html", ev=event, positions=positions, divisions=divisions(), is_new=not ev,
                               activities=linkable_activities(),
                               wizard=wizard, step=step, dates=date_range(event))

    if request.method == "POST":
        f = request.form
        data = {"title": f.get("title", "").strip(), "start_date": f.get("start_date") or "",
                "end_date": f.get("end_date") or "", "location": f.get("location", "").strip(),
                "description": f.get("description", "").strip(), "note": f.get("note", "").strip()}
        data["activity_id"] = f.get("activity_id") or None
        error = None
        if data["activity_id"] and not repo().get("activities", data["activity_id"]):
            error = "ไม่พบกิจกรรมที่เลือกเชื่อม"
        elif not data["title"] or not data["start_date"]:
            error = "กรุณากรอกชื่อกิจกรรมและวันที่เริ่มงาน"
        elif data["end_date"] and data["end_date"] < data["start_date"]:
            error = "วันที่สิ้นสุดงานต้องไม่ก่อนวันที่เริ่มงาน"
        cover = request.files.get("cover")
        if not error and cover and cover.filename:
            try:
                blob, ext, ctype = validate_upload(cover, images_only=True, allow_pdf=False)
                data["cover_path"] = save_bytes("team", blob, ext, ctype)
            except UploadError as e:
                error = f"รูปปก: {e}"
        elif not error and ev and f.get("remove_cover"):
            data["cover_path"] = None
        if error:
            flash(error, "danger")
            return page(dict(ev or {}, **data), 1)
        if ev:
            if "cover_path" in data and ev.get("cover_path"):
                delete_file(ev["cover_path"])
            repo().update("team_events", eid, data)
            audit("team.event_update", "team_event", eid, new=data)
            if data["activity_id"] != ev.get("activity_id"):
                for a in repo().where("team_applications", event_id=eid):
                    sync_staff(data, a)
            if not wizard:
                flash("บันทึกข้อมูลกิจกรรมแล้ว", "success")
        else:
            data.update({"status": "WAITING", "is_pinned": False, "academic_year": current_year(),
                         "created_by": current_user()["id"], "created_at": now_iso()})
            data.setdefault("cover_path", None)
            eid = repo().add("team_events", data)
            audit("team.event_create", "team_event", eid, new=data)
        step = _int(f.get("next_step")) or 1
        return redirect(url_for("team.form", eid=eid, step=max(step, 2) if wizard else step, wizard=1 if wizard else None))
    step = _int(request.args.get("step")) or 1
    return page(ev or {}, step if ev else 1)


@bp.route("/manage/<eid>/finish", methods=["POST"])
def finish(eid):
    """ขั้นสุดท้ายของการสร้างกิจกรรม: เปิดรับสมัครทันที หรือเก็บไว้เป็นสถานะรอเปิดรับสมัคร"""
    denied = manager_required()
    if denied:
        return denied
    ev = _event(eid)
    if not repo().where("team_positions", event_id=eid):
        flash("กรุณาเพิ่มและบันทึกตำแหน่งอย่างน้อย 1 ตำแหน่ง", "danger")
        return redirect(url_for("team.form", eid=eid, step=2, wizard=1))
    if request.form.get("open_now") and ev.get("status") != "OPEN":
        repo().update("team_events", eid, {"status": "OPEN"})
        audit("team.event_status", "team_event", eid, new="OPEN")
    flash(f"สร้างกิจกรรมแล้ว: {ev.get('title')}", "success")
    return redirect(url_for("team.manage"))


@bp.route("/manage/<eid>/status", methods=["POST"])
def event_status(eid):
    denied = manager_required()
    if denied:
        return denied
    ev = _event(eid)
    action = request.form.get("action")
    if action in ("OPEN", "CLOSED"):
        if action == "OPEN" and not repo().where("team_positions", event_id=eid):
            flash("ยังเปิดรับสมัครไม่ได้ กรุณาเพิ่มตำแหน่งอย่างน้อย 1 ตำแหน่ง", "danger")
        else:
            repo().update("team_events", eid, {"status": action})
            audit("team.event_status", "team_event", eid, new=action)
            flash(f"{EVENT_STATUS[action][0]}: {ev.get('title')}", "success")
    elif action == "pin":
        if not can("team.delete"):
            abort(403)
        repo().update("team_events", eid, {"is_pinned": not ev.get("is_pinned")})
    elif action == "delete":
        if not can("team.delete"):
            abort(403)
        for a in repo().where("team_applications", event_id=eid):
            sync_staff({}, a)
            repo().delete("team_applications", a["id"])
        for p in repo().where("team_positions", event_id=eid):
            repo().delete("team_positions", p["id"])
        if ev.get("cover_path"):
            delete_file(ev["cover_path"])
        repo().delete("team_events", eid)
        audit("team.event_delete", "team_event", eid, old={"title": ev.get("title")})
        flash("ลบกิจกรรมแล้ว", "info")
        return redirect(url_for("team.manage"))
    else:
        abort(400)
    return redirect(request.referrer or url_for("team.manage"))


# ---------- ตำแหน่งที่เปิดรับ ----------
def _position_from_form(f):
    gender = f.get("gender") if f.get("gender") in ("ANY", "M", "F", "SPLIT") else "ANY"
    male, female = _int(f.get("male_quota")), _int(f.get("female_quota"))
    quota = male + female if gender == "SPLIT" else _int(f.get("quota"))
    if gender != "SPLIT":
        male = female = 0
    want = f.get("applicant_type") if f.get("applicant_type") in TYPE_LABELS else "ALL"
    cap = want == "ALL" and bool(f.get("type_cap"))
    comm, sub = (_int(f.get("comm_quota")), _int(f.get("sub_quota"))) if cap else (0, 0)
    valid = {d["id"] for d in repo().all("divisions")}
    scope = f.get("scope")  # all = ทุกกอง, limit = เฉพาะกองที่เลือก (ไม่ส่งมา = ดูจากรายการที่เลือก)
    restrict = [] if scope == "all" else [d for d in f.getlist("restrict") if d in valid]
    data = {"name": f.get("name", "").strip(), "division_id": f.get("division_id"),
            "description": f.get("description", "").strip()[:500], "quota": quota, "applicant_type": want,
            "type_cap": cap, "comm_quota": comm, "sub_quota": sub, "gender": gender, "male_quota": male,
            "female_quota": female, "restrict_divisions": restrict}
    error = None
    if not data["name"]:
        error = "กรุณาระบุชื่อตำแหน่งที่เปิดรับสมัคร"
    elif data["division_id"] not in valid:
        error = "กรุณาเลือกกองของตำแหน่ง"
    elif quota <= 0:
        error = "กรุณาระบุจำนวนชายหรือหญิงอย่างน้อย 1 คน" if gender == "SPLIT" else "กรุณาระบุจำนวนที่เปิดรับ"
    elif cap and comm + sub > quota:
        error = f"จำนวนกรรมการรวมอนุกรรมการต้องไม่เกินจำนวนรับรวม ({quota} คน)"
    elif scope == "limit" and not restrict:
        error = "กรุณาเลือกกองที่รับสมัครอย่างน้อย 1 กอง"
    return data, error


@bp.route("/manage/<eid>/positions", methods=["POST"])
def save_position(eid):
    denied = manager_required()
    if denied:
        return denied
    _event(eid)
    f = request.form
    pid = f.get("position_id")
    pos = repo().get("team_positions", pid) if pid else None
    if pid and (not pos or pos.get("event_id") != eid):
        abort(404)
    action = f.get("action", "save")
    if action == "delete":
        if repo().where("team_applications", position_id=pid):
            flash("ตำแหน่งนี้มีผู้สมัครแล้ว ลบไม่ได้ (ลบใบสมัครของตำแหน่งนี้ก่อน)", "danger")
        else:
            repo().delete("team_positions", pid)
            audit("team.position_delete", "team_position", pid, old={"name": pos.get("name")})
            flash("ลบตำแหน่งแล้ว", "info")
    elif action == "copy":
        clone = {k: v for k, v in pos.items() if k != "id"}
        clone.update({"name": f"{pos.get('name')} (สำเนา)", "created_at": now_iso()})
        repo().add("team_positions", clone)
        flash("คัดลอกตำแหน่งแล้ว", "success")
    else:
        data, error = _position_from_form(f)
        if error:
            flash(error, "danger")
        elif pos:
            repo().update("team_positions", pid, data)
            audit("team.position_update", "team_position", pid, new=data)
            flash("บันทึกตำแหน่งแล้ว", "success")
        else:
            data.update({"event_id": eid, "created_at": now_iso()})
            pid = repo().add("team_positions", data)
            audit("team.position_add", "team_position", pid, new=data)
            flash("เพิ่มตำแหน่งแล้ว", "success")
    return redirect(url_for("team.form", eid=eid, step=2, wizard=1 if f.get("wizard") == "1" else None))


# ---------- ผู้สมัคร ----------
def _applicant_rows(eid, args):
    """ผู้สมัครของกิจกรรมตามตัวกรอง (ค้นหา สถานะ ตำแหน่ง กอง) พร้อมชื่อตำแหน่ง/กองสำหรับแสดงผล"""
    div_names = {d["id"]: d.get("name") for d in repo().all("divisions")}
    pos_map = {p["id"]: p for p in repo().where("team_positions", event_id=eid)}
    rows = repo().where("team_applications", event_id=eid)
    for a in rows:
        p = pos_map.get(a.get("position_id"), {})
        a["position_name"] = p.get("name") or "-"
        a["division_name"] = div_names.get(p.get("division_id"), "-")
        a["home_division"] = a.get("home_division_text") or div_names.get(a.get("home_division_id"), "-")
    everyone = list(rows)
    q = (args.get("q") or "").strip().lower()
    status, pid, div = args.get("status") or "", args.get("pos") or "", args.get("div") or ""
    if q:
        rows = [a for a in rows if q in f"{a.get('full_name', '')} {a.get('student_code', '')}".lower()]
    if status == "active":
        rows = [a for a in rows if a.get("status") != "REJECTED"]
    elif status:
        rows = [a for a in rows if a.get("status") == status]
    if pid:
        rows = [a for a in rows if a.get("position_id") == pid]
    if div:
        rows = [a for a in rows if a["division_name"] == div]
    return rows, everyone, pos_map


def _grouped(rows):
    """เรียง กอง -> ตำแหน่ง -> ชื่อ (ตามอักษรไทย) สำหรับสรุป/พิมพ์/คัดลอกรายชื่อ"""
    rows = sort_thai(rows, lambda a: a.get("full_name"))
    rows = sort_thai(rows, lambda a: a["position_name"])
    return sort_thai(rows, lambda a: a["division_name"])


@bp.route("/manage/<eid>/applicants")
def applicants(eid):
    denied = manager_required()
    if denied:
        return denied
    ev = _event(eid)
    rows, everyone, pos_map = _applicant_rows(eid, request.args)
    sort = request.args.get("sort") or "latest"
    if sort == "name":
        rows = sort_thai(rows, lambda a: a.get("full_name"))
    else:
        rows.sort(key=lambda a: a.get("submitted_at") or "", reverse=sort != "oldest")
    stats = {k: sum(1 for a in everyone if a.get("status") == k) for k in APP_STATUS}
    filters = {k: request.args.get(k) or "" for k in ("q", "status", "pos", "div")}
    return render_template("team/applicants.html", ev=ev, rows=rows, stats=stats, total=len(everyone),
                           positions=positions_of(eid, everyone), filters=filters, sort=sort, dates=date_range(ev),
                           div_options=sorted({a["division_name"] for a in everyone}),
                           query={k: v for k, v in filters.items() if v})


@bp.route("/manage/<eid>/applicants/add", methods=["POST"])
def add_applicant(eid):
    """เพิ่มคนนอกที่ไม่มีบัญชี/ไม่ได้อยู่ในสโมสร แอดมินยืนยันคุณสมบัติเอง จึงข้ามการตรวจสิทธิ์และโควตา"""
    denied = manager_required()
    if denied:
        return denied
    _event(eid)
    f = request.form
    pos = repo().get("team_positions", f.get("position_id"))
    name = f.get("full_name", "").strip()
    if not name:
        flash("กรุณากรอกชื่อ-นามสกุล", "danger")
    elif not pos or pos.get("event_id") != eid:
        flash("กรุณาเลือกตำแหน่ง", "danger")
    else:
        aid = repo().add("team_applications", {
            "event_id": eid, "position_id": pos["id"], "user_id": None, "full_name": name,
            "student_code": f.get("student_code", "").strip(), "year_level": f.get("year_level", "").strip() or None,
            "home_division_id": None, "home_division_text": f.get("home_division", "").strip() or None,
            "member_type": f.get("member_type") if f.get("member_type") in MEMBER_LABELS else None,
            "gender": f.get("gender") if f.get("gender") in GENDER_LABELS else None,
            "status": "PENDING" if f.get("status") == "PENDING" else "APPROVED", "submitted_at": now_iso(),
            "added_by": current_user()["id"],
        })
        audit("team.applicant_add", "team_application", aid, new={"full_name": name})
        flash("เพิ่มผู้สมัครแล้ว", "success")
    return redirect(url_for("team.applicants", eid=eid))


@bp.route("/application/<aid>/status", methods=["POST"])
def application_status(aid):
    denied = manager_required()
    if denied:
        return denied
    a = repo().get("team_applications", aid) or abort(404)
    ev = _event(a["event_id"])
    action = request.form.get("action")
    if action == "delete":
        if not can("team.delete"):
            abort(403)
        sync_staff({}, a)
        repo().delete("team_applications", aid)
        audit("team.applicant_delete", "team_application", aid, old={"full_name": a.get("full_name")})
        flash("ลบประวัติการสมัครแล้ว", "info")
    elif action in APP_STATUS:
        repo().update("team_applications", aid, {"status": action, "decided_by": current_user()["id"],
                                                 "decided_at": now_iso()})
        audit("team.applicant_status", "team_application", aid, old=a.get("status"), new=action)
        sync_staff(ev, dict(a, status=action))
        if a.get("user_id") and action != "PENDING":
            linked = action == "APPROVED" and ev.get("activity_id")
            notify(a["user_id"], f"ผลการสมัครทีมงาน {ev.get('title')}",
                   APP_STATUS[action][0] + (" — เช็กชื่อและดูชั่วโมงได้ที่หน้ากิจกรรม" if linked else ""),
                   url_for("activities.detail", aid=ev["activity_id"]) if linked else url_for("team.my"))
    else:
        abort(400)
    return redirect(request.referrer or url_for("team.applicants", eid=ev["id"]))


@bp.route("/manage/<eid>/list.txt")
def applicants_text(eid):
    """รายชื่อเป็นข้อความ จัดกลุ่มตามกอง ใช้กับปุ่มคัดลอก (ตัวกรองเดียวกับหน้าผู้สมัคร)"""
    denied = manager_required()
    if denied:
        return denied
    ev = _event(eid)
    rows, _, _ = _applicant_rows(eid, request.args)
    rows = _grouped(rows)
    lines = [ev.get("title") or ""]
    current, n = None, 0
    for a in rows:
        if a["division_name"] != current:
            current, n = a["division_name"], 0
            size = sum(1 for x in rows if x["division_name"] == current)
            lines += ["", f"{current} ({size} คน)"]
        n += 1
        code = f" ({a['student_code']})" if a.get("student_code") else ""
        lines.append(f"{n}. {a.get('full_name')}{code} - {a['position_name']}")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8", headers={"X-Row-Count": str(len(rows))})


@bp.route("/manage/<eid>/print")
def applicants_print(eid):
    denied = manager_required()
    if denied:
        return denied
    ev = _event(eid)
    rows, _, _ = _applicant_rows(eid, request.args)
    groups = []
    for a in _grouped(rows):
        key = (a["division_name"], a["position_name"])
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(a)
    return render_template("team/print.html", ev=ev, groups=groups, total=len(rows))


@dataset("team_applicants")
def ds_team_applicants(args):
    if not is_manager():
        abort(403)
    ev = _event(args.get("event_id"))
    rows, _, _ = _applicant_rows(ev["id"], args)
    out = [[a.get("full_name"), a.get("student_code"), a.get("year_level"), a["division_name"], a["position_name"],
            a["home_division"], MEMBER_LABELS.get(a.get("member_type"), ""), GENDER_LABELS.get(a.get("gender"), ""),
            APP_STATUS.get(a.get("status"), (a.get("status"),))[0], thai_date(a.get("submitted_at"))]
           for a in _grouped(rows)]
    return "team-applicants", ["ชื่อ-นามสกุล", "รหัสนักศึกษา", "ชั้นปี", "กอง", "ตำแหน่ง", "กองต้นสังกัด", "ประเภท", "เพศ",
                               "สถานะ", "วันที่สมัคร"], out
