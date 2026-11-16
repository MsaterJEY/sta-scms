"""บัญชีผู้ใช้: ลงทะเบียน เข้าสู่ระบบ ความยินยอม (AUTH-01..06)"""
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import repo
from ..rbac import current_user, login_required, user_holdings
from ..seed import run_seed
from ..services import audit, notify
from ..utils import now, now_iso, parse_dt, parse_student_code, plus_days, setting, to_iso

bp = Blueprint("auth", __name__)

CONSENT_PURPOSES = {
    "REGISTER": "ลงทะเบียนและใช้งานระบบสโมสรนักศึกษา",
}
MAX_FAILED = 5
LOCK_MINUTES = 15


def _find_user(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    key = "student_code" if identifier.isdigit() else "email"
    found = repo().where("users", **{key: identifier.lower() if key == "email" else identifier})
    return found[0] if found else None


def _safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard.index")


@bp.route("/register", methods=["GET", "POST"])
def register():
    allow_external = bool(setting("allow_external_borrowers"))
    form = request.form
    if request.method == "POST":
        mode = form.get("mode", "student")
        errors = []
        email = (form.get("email") or "").strip().lower()
        pw = form.get("password") or ""
        data = {
            "first_name": (form.get("first_name") or "").strip(),
            "last_name": (form.get("last_name") or "").strip(),
            "email": email,
            "phone": (form.get("phone") or "").strip(),
            "line_id": (form.get("line_id") or "").strip(),
        }
        if not data["first_name"] or not data["last_name"]:
            errors.append("กรุณากรอกชื่อและนามสกุล")
        if "@" not in email:
            errors.append("อีเมลไม่ถูกต้อง")
        elif repo().where("users", email=email):
            errors.append("อีเมลนี้ลงทะเบียนแล้ว")
        if len(pw) < 8:
            errors.append("รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร")
        if pw != form.get("password2"):
            errors.append("ยืนยันรหัสผ่านไม่ตรงกัน")
        if not form.get("consent"):
            errors.append("กรุณายินยอมให้เก็บและใช้ข้อมูลส่วนบุคคล")
        if mode == "external":
            if not allow_external:
                errors.append("ระบบยังไม่เปิดให้บุคคลภายนอกลงทะเบียน")
            data.update({"student_code": None, "account_type": "EXTERNAL"})
        else:
            parsed = parse_student_code(form.get("student_code"))
            if not parsed:
                errors.append("รหัสนักศึกษาต้องเป็นตัวเลข 9 หลัก")
            elif repo().where("users", student_code=parsed["student_code"]):
                errors.append("รหัสนักศึกษานี้ลงทะเบียนแล้ว")
            else:
                data.update({
                    "student_code": parsed["student_code"], "account_type": parsed["account_type"],
                    "level_code": parsed["level_code"], "admit_year": parsed["admit_year"],
                    "program_code": parsed["program_code"], "year_level": parsed["year_level"],
                    "program_source": "AUTO", "needs_review": parsed["needs_review"],
                    "over_duration": parsed["over_duration"],
                })
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/register.html", form=form, allow_external=allow_external)
        data.update({
            "password_hash": generate_password_hash(pw), "status": "ACTIVE", "student_status": "STUDYING",
            "gpa": None, "system_roles": [], "failed_logins": 0, "locked_until": None, "created_at": now_iso(),
        })
        uid = repo().add("users", data)
        repo().add("consents", {"user_id": uid, "purpose": "REGISTER", "text": CONSENT_PURPOSES["REGISTER"],
                                "granted_at": now_iso(), "expires_at": plus_days(int(setting("consent_valid_days")))})
        audit("user.register", "user", uid, new={"account_type": data["account_type"]}, actor_id=uid)
        if data.get("needs_review") or data.get("over_duration"):
            admins = [u["id"] for u in repo().all("users") if "SYS_ADMIN" in (u.get("system_roles") or [])]
            notify(admins, "มีบัญชีรอตรวจสาขา/ชั้นปี", f"{data.get('student_code')}", url_for("admin.users", review=1))
        session.clear()
        session["uid"] = uid
        session.permanent = True
        flash("ลงทะเบียนสำเร็จ", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("auth/register.html", form={}, allow_external=allow_external)


@bp.route("/api/parse-code")
def parse_code():
    p = parse_student_code(request.args.get("code"))
    if not p:
        return {"ok": False}
    return {"ok": True, "program": p["program_name"], "year_level": p["year_level"],
            "account_type": p["account_type"], "admit_year": p["admit_year"], "needs_review": p["needs_review"]}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = _find_user(request.form.get("identifier"))
        pw = request.form.get("password") or ""
        if u:
            locked = parse_dt(u.get("locked_until"))
            if locked and locked > now():
                flash("บัญชีถูกล็อกชั่วคราวเนื่องจากใส่รหัสผ่านผิดหลายครั้ง กรุณาลองใหม่ภายหลัง", "danger")
                return render_template("auth/login.html")
        if not u or not check_password_hash(u.get("password_hash", ""), pw):
            if u:
                n = int(u.get("failed_logins") or 0) + 1
                upd = {"failed_logins": n}
                if n >= MAX_FAILED:
                    upd.update({"failed_logins": 0, "locked_until": to_iso(now() + timedelta(minutes=LOCK_MINUTES))})
                    audit("user.locked", "user", u["id"], actor_id=u["id"])
                repo().update("users", u["id"], upd)
            flash("รหัสนักศึกษา/อีเมล หรือรหัสผ่านไม่ถูกต้อง", "danger")
            return render_template("auth/login.html")
        if u.get("status") != "ACTIVE":
            flash("บัญชีนี้ถูกระงับการใช้งาน", "danger")
            return render_template("auth/login.html")
        repo().update("users", u["id"], {"failed_logins": 0, "locked_until": None, "last_login_at": now_iso()})
        session.clear()
        session["uid"] = u["id"]
        session.permanent = True
        audit("user.login", "user", u["id"], actor_id=u["id"])
        return redirect(_safe_next(request.args.get("next")))
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("public.home"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    u = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "contact":
            upd = {"phone": request.form.get("phone", "").strip(), "line_id": request.form.get("line_id", "").strip()}
            if request.form.get("gender") in ("M", "F"):
                upd["gender"] = request.form["gender"]
            repo().update("users", u["id"], upd)
            audit("user.update_contact", "user", u["id"])
            flash("บันทึกแล้ว", "success")
        elif action == "password":
            if not check_password_hash(u["password_hash"], request.form.get("old_password", "")):
                flash("รหัสผ่านเดิมไม่ถูกต้อง", "danger")
            elif len(request.form.get("new_password", "")) < 8:
                flash("รหัสผ่านใหม่ต้องยาวอย่างน้อย 8 ตัวอักษร", "danger")
            else:
                repo().update("users", u["id"], {"password_hash": generate_password_hash(request.form["new_password"])})
                audit("user.change_password", "user", u["id"])
                flash("เปลี่ยนรหัสผ่านแล้ว", "success")
        return redirect(url_for("auth.profile"))
    program = repo().get("programs", u.get("program_code")) if u.get("program_code") else None
    consents = repo().where("consents", user_id=u["id"])
    holdings = repo().where("position_holders", user_id=u["id"])
    positions = {p["id"]: p for p in repo().all("positions")}
    return render_template("auth/profile.html", u=u, program=program, consents=consents,
                           holdings=holdings, positions=positions, current=user_holdings(u["id"]))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """ตั้งค่าครั้งแรก: seed ข้อมูลตั้งต้น และสร้างผู้ดูแลระบบคนแรก (ต้องมี SETUP_TOKEN)"""
    token = current_app.config.get("SETUP_TOKEN")
    if not token or request.values.get("token") != token:
        abort(404)
    has_admin = any("SYS_ADMIN" in (u.get("system_roles") or []) for u in repo().all("users"))
    if request.method == "POST":
        # สร้างผู้ดูแลระบบก่อน seed: ถ้า seed ช้าจนหมดเวลา บัญชียังอยู่ และรัน seed ซ้ำได้
        msg = "seed ข้อมูลตั้งต้นแล้ว"
        if not has_admin and request.form.get("email"):
            email = request.form["email"].strip().lower()
            if len(request.form.get("password", "")) < 8:
                flash("รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร", "danger")
                return redirect(url_for("auth.setup", token=token))
            uid = repo().add("users", {
                "email": email, "student_code": None, "first_name": request.form.get("first_name", "ผู้ดูแล"),
                "last_name": request.form.get("last_name", "ระบบ"), "account_type": "EXTERNAL", "status": "ACTIVE",
                "password_hash": generate_password_hash(request.form["password"]), "system_roles": ["SYS_ADMIN"],
                "failed_logins": 0, "created_at": now_iso(),
            })
            audit("setup.create_admin", "user", uid, actor_id=uid)
            msg += " และสร้างผู้ดูแลระบบแล้ว"
        run_seed(int(request.form.get("year") or 2569))
        flash(msg, "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/setup.html", token=token, has_admin=has_admin)
