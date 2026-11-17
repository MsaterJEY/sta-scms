"""รับสมัครคณะกรรมการ/อนุกรรมการ (ย้ายมาจากระบบ Google Apps Script + Sheets เดิม)

แนวคิดเดิมคงไว้: ฟอร์มหน้าเดียว กรอกคำนำหน้า ชื่อ รหัสนักศึกษา ชั้นปี สาขา LINE ID ฝ่ายที่สมัคร ประสบการณ์
ได้รหัสการสมัคร (APP-ปี-ลำดับ) และสมัครซ้ำด้วยรหัสนักศึกษาเดิมไม่ได้
สิ่งที่เปลี่ยนเมื่อย้ายมาเว็บนี้:
- เก็บใน Firestore (join_rounds, join_applications) ไม่ใช้ Google Sheet
- ตรวจคณะและชั้นปีจากรหัสนักศึกษา: กรรมการรับชั้นปี 2-4, อนุกรรมการรับเฉพาะชั้นปี 1 (ตั้งได้รายรอบ)
- ทีมงานกรอกคะแนนสัมภาษณ์ สรุปผล ประกาศผล และส่งผู้ผ่านเข้าทะเบียนกรรมการ/อนุกรรมการ
- เลขาฝ่ายบริหารและสัมพันธ์แก้ไขข้อมูลผู้สมัครได้เมื่อกรอกผิด (สิทธิ์ rec.edit)
"""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, current_year, in_year
from ..services import audit, notify
from ..utils import form_dt, now, now_iso, parse_dt, parse_student_code, sort_thai

bp = Blueprint("join", __name__, url_prefix="/join")

KINDS = {"COMM": {"label": "คณะกรรมการ", "level": "COMMITTEE", "min_year": 2, "max_year": 4},
         "SUBCOMM": {"label": "อนุกรรมการ", "level": "SUBCOMMITTEE", "min_year": 1, "max_year": 1}}
ROUND_STATUS = {"WAITING": ("รอเปิดรับสมัคร", "warning"), "OPEN": ("เปิดรับสมัคร", "success"), "CLOSED": ("ปิดรับสมัครแล้ว", "secondary")}
RESULTS = {"PENDING": ("รอผล", "warning"), "PASS": ("ผ่านการคัดเลือก", "success"), "RESERVE": ("สำรอง", "info"),
           "FAIL": ("ไม่ผ่าน", "danger")}
PREFIXES = ("นาย", "นางสาว", "นาง")
# ฝ่ายที่เปิดรับตามฟอร์มเดิม (แก้ได้รายรอบ)
DEFAULT_UNITS = ["ฝ่ายบุคลากร", "ฝ่ายจัดหาทุน", "ฝ่ายเอกสาร", "ฝ่ายอุปกรณ์และสถานที่", "ฝ่ายสวัสดิการและพยาบาล", "ฝ่ายวิชาการ",
                 "ฝ่ายพิธีกร", "ฝ่ายกีฬาและนันทนาการ", "ฝ่ายจิตอาสา", "ฝ่ายวัฒนธรรม", "ฝ่ายสื่อ", "ฝ่ายแผนและนโยบาย"]
NAME_RE = re.compile(r"^[A-Za-zก-๙.\s]+$")
MAX_SCORE = 100


@bp.app_context_processor
def _labels():
    return {"JOIN_KINDS": KINDS, "JOIN_ROUND_STATUS": ROUND_STATUS, "JOIN_RESULTS": RESULTS}


# ---------- ตัวช่วย ----------
def is_staff():
    """ทีมงานที่เห็นรายชื่อผู้สมัคร"""
    return bool(current_user()) and any(can(p) for p in ("rec.manage", "rec.score", "rec.approve", "rec.edit"))


def staff_required():
    if not current_user():
        return redirect(url_for("auth.login", next=request.full_path))
    if not is_staff():
        abort(403)
    return None


def _round(rid):
    r = repo().get("join_rounds", rid)
    if not r:
        abort(404)
    return r


def is_open(r):
    if r.get("status") != "OPEN":
        return False
    o, c = parse_dt(r.get("open_at")), parse_dt(r.get("close_at"))
    t = now()
    return (not o or o <= t) and (not c or t <= c)


def year_text(r):
    lo, hi = int(r.get("min_year") or 1), int(r.get("max_year") or 8)
    return f"ชั้นปีที่ {lo}" if lo == hi else f"ชั้นปีที่ {lo}-{hi}"


def check_student(r, code):
    """ตรวจรหัสนักศึกษา: ต้องเป็นนักศึกษาคณะวิทยาศาสตร์ฯ และชั้นปีอยู่ในช่วงที่รอบเปิดรับ คืน (ข้อมูล, ข้อความผิดพลาด)"""
    p = parse_student_code(code)
    if not p:
        return None, "รหัสนักศึกษาต้องเป็นตัวเลข 9 หลัก"
    if p["account_type"] != "SCI":
        return None, "เปิดรับเฉพาะนักศึกษาคณะวิทยาศาสตร์เทคโนโลยีและการเกษตร (ระบบตรวจจากรหัสนักศึกษา)"
    yl = p.get("year_level") or 0
    if not int(r.get("min_year") or 1) <= yl <= int(r.get("max_year") or 8):
        return None, f"รอบนี้เปิดรับเฉพาะ{year_text(r)} (รหัสนี้เป็นชั้นปีที่ {yl})"
    return p, None


def next_app_code(r):
    """รหัสการสมัครแบบเดิม APP-ปี-ลำดับ ต่อรอบ"""
    used = {a.get("app_code") for a in repo().where("join_applications", round_id=r["id"])}
    n = len(used) + 1
    while True:
        code = f"APP-{r.get('academic_year')}-{'K' if r.get('kind') == 'COMM' else 'S'}{n:04d}"
        if code not in used:
            return code
        n += 1


def full_name(a):
    return f"{a.get('prefix', '')}{a.get('first_name', '')} {a.get('last_name', '')}".strip()


def linked_user(student_code):
    rows = repo().where("users", student_code=student_code) if student_code else []
    return rows[0] if rows else None


# ---------- หน้าสาธารณะ ----------
@bp.route("/")
def index():
    rounds = sorted(repo().all("join_rounds"), key=lambda r: r.get("created_at") or "", reverse=True)
    shown = [r for r in rounds if r.get("status") != "WAITING"]
    for r in shown:
        r["open"] = is_open(r)
        r["count"] = len(repo().where("join_applications", round_id=r["id"]))
        r["year_text"] = year_text(r)
    shown.sort(key=lambda r: not r["open"])
    return render_template("join/index.html", rounds=shown, staff=is_staff())


@bp.route("/<rid>", methods=["GET", "POST"])
def apply(rid):
    r = _round(rid)
    if r.get("status") == "WAITING" and not is_staff():
        abort(404)
    u = current_user()
    programs = sorted([p for p in repo().all("programs") if p.get("is_science_faculty", True)], key=lambda p: p.get("name_th") or "")
    units = r.get("units") or DEFAULT_UNITS
    form = {}
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "").strip() for k in ("prefix", "first_name", "last_name", "student_code", "program_code",
                                                   "line_id", "unit", "experience")}
        error = None
        info = None
        if f.get("website"):  # ช่องดักบอต ผู้ใช้จริงมองไม่เห็น
            abort(400)
        if not is_open(r):
            error = "รอบนี้ไม่ได้เปิดรับสมัครในขณะนี้"
        elif form["prefix"] not in PREFIXES:
            error = "กรุณาเลือกคำนำหน้าชื่อ"
        elif not form["first_name"] or not form["last_name"] or not NAME_RE.match(form["first_name"] + form["last_name"]):
            error = "ชื่อและนามสกุลต้องเป็นตัวอักษรเท่านั้น ห้ามมีตัวเลขหรือสัญลักษณ์"
        elif form["unit"] not in units:
            error = "กรุณาเลือกฝ่ายที่ต้องการสมัคร"
        elif not f.get("consent"):
            error = "กรุณายินยอมให้เก็บข้อมูลเพื่อการรับสมัคร"
        else:
            info, error = check_student(r, form["student_code"])
        program = next((p for p in programs if p["id"] == form["program_code"]), None)
        if not error and not program:
            error = "กรุณาเลือกสาขาวิชา"
        doc_id = f"{rid}_{form['student_code']}"
        if not error and repo().get("join_applications", doc_id):
            error = f"รหัสนักศึกษา {form['student_code']} ได้สมัครรอบนี้ไปแล้ว ไม่สามารถสมัครซ้ำได้"
        if error:
            flash(error, "danger")
        else:
            code = next_app_code(r)
            data = {"round_id": rid, "kind": r.get("kind"), "academic_year": r.get("academic_year"), "app_code": code,
                    "prefix": form["prefix"], "first_name": form["first_name"][:80], "last_name": form["last_name"][:80],
                    "student_code": form["student_code"], "year_level": info["year_level"],
                    "program_code": program["id"], "program": program.get("name_th"), "unit": form["unit"],
                    "line_id": form["line_id"][:60], "experience": form["experience"][:2000] or "-",
                    "user_id": u["id"] if u and u.get("student_code") == form["student_code"] else None,
                    "result": "PENDING", "score": None, "score_note": "", "submitted_at": now_iso()}
            repo().set("join_applications", doc_id, data)
            audit("join.apply", "join_application", doc_id, new={"app_code": code}, anonymous=not u)
            target = linked_user(form["student_code"])
            if target:
                title = f"สมัคร{KINDS[r['kind']]['label']}สำเร็จ: {r.get('title')}"
                notify(target["id"], title, f"รหัสการสมัคร {code} · {form['unit']}", url_for("join.status"), event="RECRUIT",
                       line_text=f"{title} · รหัสการสมัคร {code}")
            session["join_done"] = doc_id
            return redirect(url_for("join.done", rid=rid))
    elif u and u.get("student_code"):
        form = {"first_name": u.get("first_name") or "", "last_name": u.get("last_name") or "",
                "student_code": u.get("student_code"), "program_code": u.get("program_code") or ""}
    return render_template("join/form.html", r=r, form=form, programs=programs, units=units, prefixes=PREFIXES,
                           open=is_open(r), year_text=year_text(r))


@bp.route("/<rid>/done")
def done(rid):
    """หน้ายืนยันหลังสมัคร แสดงเฉพาะเบราว์เซอร์ที่เพิ่งส่งใบสมัคร"""
    a = repo().get("join_applications", session.get("join_done") or "")
    if not a or a.get("round_id") != rid:
        return redirect(url_for("join.apply", rid=rid))
    return render_template("join/done.html", r=_round(rid), a=a, name=full_name(a))


@bp.route("/status", methods=["GET", "POST"])
def status():
    """ตรวจสอบสถานะ/ผลด้วยรหัสนักศึกษา + รหัสการสมัคร (ไม่ต้องล็อกอิน)"""
    found, searched = [], False
    u = current_user()
    if request.method == "POST":
        searched = True
        sid = request.form.get("student_code", "").strip()
        code = request.form.get("app_code", "").strip().upper()
        found = [a for a in repo().where("join_applications", student_code=sid) if a.get("app_code") == code]
    elif u and u.get("student_code"):
        found = repo().where("join_applications", student_code=u["student_code"])
    rounds = {a["round_id"]: repo().get("join_rounds", a["round_id"]) or {} for a in found}
    found.sort(key=lambda a: a.get("submitted_at") or "", reverse=True)
    return render_template("join/status.html", found=found, rounds=rounds, searched=searched, name=full_name)


# ---------- จัดการรอบ ----------
@bp.route("/manage")
def manage():
    denied = staff_required()
    if denied:
        return denied
    rounds = sorted(repo().all("join_rounds"), key=lambda r: r.get("created_at") or "", reverse=True)
    apps = repo().all("join_applications")
    for r in rounds:
        mine = [a for a in apps if a.get("round_id") == r["id"]]
        r["count"] = len(mine)
        r["scored"] = sum(1 for a in mine if a.get("score") is not None)
        r["passed"] = sum(1 for a in mine if a.get("result") == "PASS")
        r["year_text"] = year_text(r)
    return render_template("join/manage.html", rounds=rounds)


@bp.route("/manage/new", methods=["GET", "POST"])
@bp.route("/manage/<rid>/edit", methods=["GET", "POST"])
def round_form(rid=None):
    if not current_user():
        return redirect(url_for("auth.login", next=request.full_path))
    if not can("rec.manage"):
        abort(403)
    r = _round(rid) if rid else None
    if request.method == "POST":
        f = request.form
        kind = f.get("kind") if f.get("kind") in KINDS else "COMM"
        units = [x.strip()[:80] for x in f.get("units", "").splitlines() if x.strip()]
        try:
            lo, hi = int(f.get("min_year") or KINDS[kind]["min_year"]), int(f.get("max_year") or KINDS[kind]["max_year"])
        except ValueError:
            lo, hi = KINDS[kind]["min_year"], KINDS[kind]["max_year"]
        data = {"title": f.get("title", "").strip()[:150], "kind": kind, "description": f.get("description", "").strip()[:2000],
                "academic_year": int(f.get("academic_year") or current_year()), "min_year": lo, "max_year": hi,
                "open_at": form_dt(f.get("open_at")), "close_at": form_dt(f.get("close_at")),
                "units": units or DEFAULT_UNITS, "interview_info": f.get("interview_info", "").strip()[:500]}
        if not data["title"]:
            flash("กรุณาระบุชื่อรอบรับสมัคร", "danger")
        elif not 1 <= lo <= hi <= 8:
            flash("ช่วงชั้นปีไม่ถูกต้อง", "danger")
        elif r and r.get("kind") != kind and repo().where("join_applications", round_id=rid):
            flash("รอบนี้มีผู้สมัครแล้ว เปลี่ยนประเภทไม่ได้", "danger")
        else:
            if r:
                repo().update("join_rounds", rid, data)
            else:
                data.update({"status": "WAITING", "results_published": False, "created_by": current_user()["id"],
                             "created_at": now_iso()})
                rid = repo().add("join_rounds", data)
            audit("join.round_save", "join_round", rid, new={"title": data["title"], "kind": kind})
            flash("บันทึกรอบรับสมัครแล้ว", "success")
            return redirect(url_for("join.applicants", rid=rid))
    return render_template("join/round_form.html", r=r, year=current_year(), default_units=DEFAULT_UNITS)


@bp.route("/manage/<rid>/status", methods=["POST"])
def round_status(rid):
    if not current_user() or not can("rec.manage"):
        abort(403)
    r = _round(rid)
    action = request.form.get("action")
    if action in ("OPEN", "CLOSED", "WAITING"):
        repo().update("join_rounds", rid, {"status": action})
        audit("join.round_status", "join_round", rid, old=r.get("status"), new=action)
        flash(f"{ROUND_STATUS[action][0]}: {r.get('title')}", "success")
    elif action in ("publish", "unpublish"):
        publish = action == "publish"
        apps = repo().where("join_applications", round_id=rid)
        if publish and any(a.get("result") == "PENDING" for a in apps):
            flash("ยังมีผู้สมัครที่ยังไม่ได้สรุปผล กรุณาสรุปผลให้ครบก่อนประกาศ", "danger")
        else:
            repo().update("join_rounds", rid, {"results_published": publish, "published_at": now_iso() if publish else None})
            audit("join.results_" + action, "join_round", rid)
            if publish:
                for a in apps:
                    target = linked_user(a.get("student_code"))
                    if target:
                        title = f"ประกาศผล{KINDS[r['kind']]['label']}: {r.get('title')}"
                        notify(target["id"], title, RESULTS[a.get("result", "PENDING")][0], url_for("join.status"),
                               event="RECRUIT", line_text=f"{title} · {RESULTS[a.get('result', 'PENDING')][0]}")
            flash("ประกาศผลแล้ว ผู้สมัครตรวจผลได้ที่หน้าตรวจสอบสถานะ" if publish else "ยกเลิกการประกาศผลแล้ว", "success")
    elif action == "delete":
        if repo().where("join_applications", round_id=rid):
            flash("รอบนี้มีผู้สมัครแล้ว ลบไม่ได้ (ปิดรับสมัครแทน)", "danger")
        else:
            repo().delete("join_rounds", rid)
            audit("join.round_delete", "join_round", rid, old={"title": r.get("title")})
            flash("ลบรอบรับสมัครแล้ว", "info")
            return redirect(url_for("join.manage"))
    else:
        abort(400)
    return redirect(url_for("join.applicants", rid=rid))


# ---------- ผู้สมัคร คะแนนสัมภาษณ์ และสรุปผล ----------
def _rows(rid, args):
    rows = repo().where("join_applications", round_id=rid)
    everyone = list(rows)
    q = (args.get("q") or "").strip()
    if q:
        rows = [a for a in rows if q in a.get("student_code", "") or q in full_name(a) or q.upper() in (a.get("app_code") or "")]
    if args.get("unit"):
        rows = [a for a in rows if a.get("unit") == args["unit"]]
    if args.get("result") in RESULTS:
        rows = [a for a in rows if a.get("result") == args["result"]]
    elif args.get("result") == "unscored":
        rows = [a for a in rows if a.get("score") is None]
    sort = args.get("sort") or "score"
    if sort == "name":
        rows = sort_thai(rows, lambda a: a.get("first_name"))
    elif sort == "time":
        rows.sort(key=lambda a: a.get("submitted_at") or "")
    else:  # คะแนนสูงสุดก่อน ผู้ที่ยังไม่มีคะแนนอยู่ท้าย
        rows = sort_thai(rows, lambda a: a.get("first_name"))
        rows.sort(key=lambda a: (a.get("score") is None, -(a.get("score") or 0)))
    return rows, everyone


@bp.route("/manage/<rid>")
def applicants(rid):
    denied = staff_required()
    if denied:
        return denied
    r = _round(rid)
    rows, everyone = _rows(rid, request.args)
    stats = {"all": len(everyone), "scored": sum(1 for a in everyone if a.get("score") is not None),
             **{k: sum(1 for a in everyone if a.get("result") == k) for k in RESULTS}}
    by_unit = [{"unit": un, "all": sum(1 for a in everyone if a.get("unit") == un),
                "pass": sum(1 for a in everyone if a.get("unit") == un and a.get("result") == "PASS")}
               for un in sorted({a.get("unit") for a in everyone if a.get("unit")})]
    edit = repo().get("join_applications", request.args.get("edit")) if request.args.get("edit") else None
    return render_template("join/round.html", r=r, rows=rows, stats=stats, by_unit=by_unit, args=request.args, name=full_name,
                           units=r.get("units") or DEFAULT_UNITS, year_text=year_text(r), open=is_open(r),
                           edit=edit if edit and edit.get("round_id") == rid and can("rec.edit") else None,
                           programs=sorted(repo().all("programs"), key=lambda p: p.get("name_th") or ""), prefixes=PREFIXES,
                           can_manage=can("rec.manage"), can_score=can("rec.score") or can("rec.manage"), can_edit=can("rec.edit"),
                           max_score=MAX_SCORE)


@bp.route("/manage/<rid>/scores", methods=["POST"])
def save_scores(rid):
    """กรอก/แก้คะแนนสัมภาษณ์หลายคนในครั้งเดียว และสรุปผลรายคน (ผู้มีสิทธิ์จัดการรอบ)"""
    if not current_user() or not (can("rec.score") or can("rec.manage")):
        abort(403)
    r = _round(rid)
    f = request.form
    changed = 0
    for a in repo().where("join_applications", round_id=rid):
        upd = {}
        raw = f.get(f"score_{a['id']}")
        if raw is not None:
            raw = raw.strip()
            try:
                val = None if raw == "" else max(0.0, min(float(raw), float(MAX_SCORE)))
            except ValueError:
                flash(f"คะแนนของ {full_name(a)} ไม่ใช่ตัวเลข — ข้ามรายการนี้", "warning")
                continue
            if val != a.get("score"):
                upd.update({"score": val, "scored_by": current_user()["id"], "scored_at": now_iso()})
        note = f.get(f"note_{a['id']}")
        if note is not None and note.strip()[:300] != (a.get("score_note") or ""):
            upd["score_note"] = note.strip()[:300]
        res = f.get(f"result_{a['id']}")
        if res in RESULTS and res != a.get("result") and can("rec.manage"):
            upd.update({"result": res, "decided_by": current_user()["id"], "decided_at": now_iso()})
        if upd:
            repo().update("join_applications", a["id"], upd)
            audit("join.score_save", "join_application", a["id"], old={k: a.get(k) for k in ("score", "result")},
                  new={k: upd.get(k, a.get(k)) for k in ("score", "result")})
            changed += 1
    if f.get("cutoff") not in (None, "") and can("rec.manage"):
        # สรุปผลจากเกณฑ์คะแนน: ถึงเกณฑ์ = ผ่าน ต่ำกว่า = ไม่ผ่าน (เฉพาะคนที่มีคะแนนและยังรอผล)
        try:
            cutoff = float(f["cutoff"])
        except ValueError:
            cutoff = None
        if cutoff is not None:
            for a in repo().where("join_applications", round_id=rid):
                if a.get("score") is not None and a.get("result") == "PENDING":
                    repo().update("join_applications", a["id"], {"result": "PASS" if a["score"] >= cutoff else "FAIL",
                                                                 "decided_by": current_user()["id"], "decided_at": now_iso()})
                    changed += 1
            audit("join.cutoff", "join_round", rid, new={"cutoff": cutoff})
    flash(f"บันทึกแล้ว {changed} รายการ" if changed else "ไม่มีรายการที่เปลี่ยนแปลง", "success" if changed else "info")
    if r.get("results_published") and changed:
        flash("รอบนี้ประกาศผลแล้ว การแก้ไขมีผลต่อหน้าตรวจผลของผู้สมัครทันที", "warning")
    return redirect(url_for("join.applicants", rid=rid, **{k: v for k, v in request.args.items()}))


@bp.route("/application/<aid>/edit", methods=["POST"])
def edit_application(aid):
    """แก้ไขข้อมูลผู้สมัครเมื่อเจ้าตัวกรอกผิด (เลขาฝ่ายบริหารและสัมพันธ์)"""
    if not current_user() or not can("rec.edit"):
        abort(403)
    a = repo().get("join_applications", aid) or abort(404)
    r = _round(a["round_id"])
    f = request.form
    back = redirect(url_for("join.applicants", rid=r["id"]))
    if f.get("action") == "delete":
        repo().delete("join_applications", aid)
        audit("join.application_delete", "join_application", aid, old={"app_code": a.get("app_code"), "student_code": a.get("student_code")})
        flash("ลบใบสมัครแล้ว", "info")
        return back
    new = {"prefix": f.get("prefix") if f.get("prefix") in PREFIXES else a.get("prefix"),
           "first_name": f.get("first_name", "").strip()[:80], "last_name": f.get("last_name", "").strip()[:80],
           "student_code": f.get("student_code", "").strip(), "unit": f.get("unit", "").strip()[:80],
           "line_id": f.get("line_id", "").strip()[:60], "experience": f.get("experience", "").strip()[:2000] or "-"}
    program = repo().get("programs", f.get("program_code"))
    info, error = check_student(r, new["student_code"])
    if not new["first_name"] or not new["last_name"]:
        error = "กรุณากรอกชื่อและนามสกุล"
    elif not program:
        error = "กรุณาเลือกสาขาวิชา"
    new_id = f"{r['id']}_{new['student_code']}"
    if not error and new_id != aid and repo().get("join_applications", new_id):
        error = f"รหัสนักศึกษา {new['student_code']} มีใบสมัครในรอบนี้อยู่แล้ว"
    if error:
        flash(error, "danger")
        return redirect(url_for("join.applicants", rid=r["id"], edit=aid))
    new.update({"program_code": program["id"], "program": program.get("name_th"), "year_level": info["year_level"],
                "edited_by": current_user()["id"], "edited_at": now_iso()})
    old = {k: a.get(k) for k in ("prefix", "first_name", "last_name", "student_code", "unit", "program")}
    merged = {k: v for k, v in dict(a, **new).items() if k != "id"}
    if new_id != aid:  # รหัสนักศึกษาเปลี่ยน: ย้ายเอกสารเพื่อคงกติกาห้ามสมัครซ้ำ
        merged["user_id"] = None
        repo().set("join_applications", new_id, merged)
        repo().delete("join_applications", aid)
    else:
        repo().update("join_applications", aid, new)
    audit("join.application_edit", "join_application", new_id, old=old,
          new={k: new.get(k) for k in old}, note=f.get("reason", "").strip()[:200] or None)
    flash("แก้ไขข้อมูลผู้สมัครแล้ว", "success")
    return back


@bp.route("/manage/<rid>/to-roster", methods=["POST"])
def to_roster(rid):
    """ส่งผู้ผ่านการคัดเลือกเข้าทะเบียนกรรมการ/อนุกรรมการ (ได้ตำแหน่งทันทีถ้ามีบัญชี หรือเมื่อสมัครสมาชิก)"""
    if not current_user() or not can("rec.manage"):
        abort(403)
    from .roster import _ensure_holder, guess_division
    r = _round(rid)
    kind, year = r["kind"], int(r.get("academic_year") or current_year())
    level = KINDS[kind]["level"]
    div_pos = {p.get("division_id"): p["id"] for p in repo().all("positions")
               if in_year(p, year) and p.get("level") == level and p.get("division_id")}
    users = {u.get("student_code"): u for u in repo().all("users") if u.get("student_code")}
    done, skipped = 0, []
    for a in repo().where("join_applications", round_id=rid):
        if a.get("result") != "PASS":
            continue
        div = guess_division(a.get("unit"), div_pos)
        if not div:
            skipped.append(f"{full_name(a)} ({a.get('unit')})")
            continue
        doc_id = f"{year}_{kind}_{a['student_code']}"
        old = repo().get("member_roster", doc_id) or {}
        entry = {"academic_year": year, "member_type": kind, "student_code": a["student_code"],
                 "first_name": a.get("first_name"), "last_name": a.get("last_name"), "program": a.get("program") or "",
                 "unit": a.get("unit") or "", "year_level": a.get("year_level"), "status_text": "ผ่านการคัดเลือก",
                 "division_id": div, "position_id": div_pos[div], "user_id": old.get("user_id"), "holder_id": old.get("holder_id"),
                 "imported_by": current_user()["id"], "imported_at": now_iso(), "source": "JOIN", "join_application_id": a["id"]}
        if old and old.get("position_id") != div_pos[div]:
            entry.update({"user_id": None, "holder_id": None})
        repo().set("member_roster", doc_id, entry)
        entry["id"] = doc_id
        if users.get(a["student_code"]):
            _ensure_holder(users[a["student_code"]], entry)
        done += 1
    audit("join.to_roster", "join_round", rid, new={"count": done, "skipped": len(skipped)})
    flash(f"เพิ่มผู้ผ่านการคัดเลือกเข้าทะเบียน{KINDS[kind]['label']} {done} คน", "success" if done else "info")
    if skipped:
        flash("จับคู่ฝ่ายกับกองไม่ได้ ให้เพิ่มเองที่หน้าทะเบียน: " + ", ".join(skipped), "warning")
    return redirect(url_for("join.applicants", rid=rid, result="PASS"))


@dataset("join_applicants")
def ds_join(args):
    if not is_staff():
        abort(403)
    r = _round(args.get("round_id"))
    rows, _ = _rows(r["id"], args)
    out = [[a.get("app_code"), a.get("submitted_at"), full_name(a), a.get("student_code"), a.get("program"), a.get("year_level"),
            a.get("unit"), a.get("line_id"), a.get("experience"), a.get("score"), a.get("score_note"),
            RESULTS.get(a.get("result"), ("",))[0]] for a in rows]
    return "join-applicants", ["ApplicationID", "Timestamp", "FullName", "StudentID", "Major", "Year", "Department", "LineID",
                                "Experience", "คะแนนสัมภาษณ์", "หมายเหตุ", "ผล"], out
