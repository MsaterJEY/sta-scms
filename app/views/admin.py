"""ผู้ดูแลระบบ: บัญชี ปีการศึกษา ผู้ถือตำแหน่ง โครงสร้าง ตารางสาขา การตั้งค่า สิทธิ์ Audit (ORG, PERM)"""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import (FUNCTIONS, PERMISSIONS, ROLE_LABELS, can, current_user, current_year, holders_of_role, in_year,
                    login_required, permission_required, roles_for, vacant_fallback_positions)
from ..seed import OLD_DIVISIONS, OLD_POSITIONS
from ..services import audit, display_name, notify
from ..utils import DEFAULT_SETTINGS, form_dt, now_iso, parse_dt, now, settings, sort_thai

bp = Blueprint("admin", __name__, url_prefix="/admin")


def rename_division(code, name):
    """เปลี่ยนชื่อกอง (รหัสกองคงเดิม ข้อมูลที่อ้างถึงกองจึงไม่กระทบ) คืน True ถ้าเปลี่ยนสำเร็จ"""
    d = repo().get("divisions", code)
    name = (name or "").strip()[:80]
    if not d or not name:
        flash("กรุณาระบุชื่อกอง", "danger")
        return False
    if name != d.get("name"):
        repo().update("divisions", code, {"name": name})
        audit("division.rename", "division", code, old=d.get("name"), new=name)
    flash(f"เปลี่ยนชื่อกองเป็น \"{name}\" แล้ว", "success")
    return True


def division_usage(code):
    """จำนวนข้อมูลที่อ้างถึงกองนี้ ใช้ตัดสินว่าลบได้หรือไม่"""
    n = len(repo().where("positions", division_id=code)) + len(repo().where("team_positions", division_id=code))
    return n + len(repo().where("documents", division_id=code)) + len(repo().where("member_roster", division_id=code))


# ---------- บัญชีผู้ใช้ ----------
def _filter_users(args):
    users = repo().all("users")
    q = (args.get("q") or "").strip()
    if q:
        users = [u for u in users if q in (u.get("student_code") or "") or q in (u.get("email") or "")
                 or q in display_name(u)]
    if args.get("type"):
        users = [u for u in users if u.get("account_type") == args["type"]]
    if args.get("review"):
        users = [u for u in users if u.get("needs_review") or u.get("over_duration")]
    if args.get("program"):
        users = [u for u in users if u.get("program_code") == args["program"]]
    return sort_thai(users, lambda u: u.get("first_name"))


@bp.route("/users")
@permission_required("admin.manage")
def users():
    programs = {p["id"]: p for p in repo().all("programs")}
    return render_template("admin/users.html", users=_filter_users(request.args), programs=programs, args=request.args)


@dataset("users")
def ds_users(args):
    if not can("admin.manage"):
        abort(403)
    programs = {p["id"]: p for p in repo().all("programs")}
    rows = []
    for u in _filter_users(args):
        rows.append([u.get("student_code"), u.get("first_name"), u.get("last_name"), u.get("account_type"),
                     (programs.get(u.get("program_code")) or {}).get("name_th"), u.get("program_code"),
                     u.get("year_level"), u.get("email"), u.get("status")])
    return "users", ["รหัสนักศึกษา", "ชื่อ", "นามสกุล", "ประเภท", "สาขา", "รหัสสาขา", "ชั้นปี", "อีเมล", "สถานะ"], rows


@bp.route("/users/<uid>", methods=["GET", "POST"])
@permission_required("admin.manage")
def user_edit(uid):
    u = repo().get("users", uid)
    if not u:
        abort(404)
    if request.method == "POST":
        f = request.form
        upd = {
            "first_name": f.get("first_name", "").strip(), "last_name": f.get("last_name", "").strip(),
            "account_type": f.get("account_type"), "status": f.get("status"),
            "student_status": f.get("student_status"), "needs_review": False, "over_duration": False,
        }
        if f.get("program_code") != (u.get("program_code") or "") or str(f.get("year_level") or "") != str(u.get("year_level") or ""):
            upd.update({"program_code": f.get("program_code") or None,
                        "year_level": int(f["year_level"]) if f.get("year_level") else None,
                        "program_source": "MANUAL"})
        sys_roles = ["SYS_ADMIN"] if f.get("sys_admin") else []
        if uid == current_user()["id"] and not sys_roles:
            flash("ไม่สามารถถอนสิทธิ์ผู้ดูแลระบบของตัวเองได้", "warning")
            sys_roles = ["SYS_ADMIN"]
        upd["system_roles"] = sys_roles
        old = {k: u.get(k) for k in upd}
        repo().update("users", uid, upd)
        audit("user.admin_update", "user", uid, old=old, new=upd)
        flash("บันทึกแล้ว", "success")
        return redirect(url_for("admin.user_edit", uid=uid))
    programs = sorted(repo().all("programs"), key=lambda p: p["id"])
    holds = repo().where("position_holders", user_id=uid)
    positions = {p["id"]: p for p in repo().all("positions")}
    return render_template("admin/user_edit.html", u=u, programs=programs, holds=holds, positions=positions)


# ---------- ปีการศึกษา ----------
@bp.route("/years", methods=["GET", "POST"])
@permission_required("admin.manage")
def years():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            y = int(request.form["year"])
            repo().set("academic_years", str(y), {"name": str(y), "start_date": request.form.get("start_date"),
                                                  "end_date": request.form.get("end_date"), "is_current": False})
            audit("year.add", "academic_year", str(y))
        elif action == "edit":
            y = request.form.get("year")
            ay = repo().get("academic_years", y) or abort(404)
            new = {"start_date": request.form.get("start_date") or None, "end_date": request.form.get("end_date") or None}
            if new["start_date"] and new["end_date"] and new["end_date"] < new["start_date"]:
                flash("วันสิ้นสุดต้องไม่ก่อนวันเริ่ม", "danger")
            else:
                repo().update("academic_years", y, new)
                audit("year.edit", "academic_year", y, old={k: ay.get(k) for k in new}, new=new)
                flash(f"แก้ไขปีการศึกษา {y} แล้ว", "success")
        elif action == "delete":
            y = request.form.get("year")
            ay = repo().get("academic_years", y) or abort(404)
            used = ay.get("is_current") or repo().where("position_holders", academic_year=int(y)) or any(
                p.get("academic_year") == int(y) for p in repo().all("positions"))
            if used:
                flash("ลบไม่ได้: เป็นวาระปัจจุบัน หรือมีผังตำแหน่ง/ผู้ถือตำแหน่งของปีนี้อยู่", "danger")
            else:
                repo().delete("academic_years", y)
                audit("year.delete", "academic_year", y)
                flash(f"ลบปีการศึกษา {y} แล้ว", "info")
        elif action == "current":
            y = request.form["year"]
            # ตรวจตำแหน่ง locked ต้องมีผู้ถือครบก่อนเปิดวาระ (ORG-02)
            missing = []
            for p in repo().all("positions"):
                if in_year(p, int(y)) and p.get("locked_required") and not repo().where(
                        "position_holders", position_id=p["id"], academic_year=int(y), status="ACTIVE"):
                    missing.append(p["name"])
            if missing and not request.form.get("force"):
                flash("ยังเปิดวาระไม่ได้ ตำแหน่งต่อไปนี้ยังไม่มีผู้ถือ: " + ", ".join(missing), "danger")
                return redirect(url_for("admin.years"))
            for ay in repo().all("academic_years"):
                repo().update("academic_years", ay["id"], {"is_current": ay["id"] == y})
            repo().set("system_settings", "main", {"current_academic_year": int(y)}, merge=True)
            audit("year.set_current", "academic_year", y)
            flash(f"ตั้งปีการศึกษา {y} เป็นวาระปัจจุบันแล้ว สิทธิ์ของวาระเดิมสิ้นสุดทันที", "success")
            for p in vacant_fallback_positions(int(y)):
                msg = f"ตำแหน่ง{p['name']}ว่าง ใช้ผู้สำรอง: " + ", ".join(
                    FUNCTIONS.get(x, (x,))[0] for x in p.get("fallback_functions") or [])
                flash(msg, "warning")
                notify([u["id"] for u in repo().all("users") if "SYS_ADMIN" in (u.get("system_roles") or [])],
                       "เปิดวาระ: มีตำแหน่งว่างที่ใช้ผู้สำรอง", msg, url_for("admin.holders", year=y))
        return redirect(url_for("admin.years"))
    items = sorted(repo().all("academic_years"), key=lambda y: y["id"], reverse=True)
    return render_template("admin/years.html", items=items)


# ---------- ผู้ถือตำแหน่งตามวาระ ----------
@bp.route("/holders", methods=["GET", "POST"])
@permission_required("admin.holders")
def holders():
    year = int(request.values.get("year") or current_year())
    positions = {p["id"]: p for p in repo().all("positions")}
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            code = (request.form.get("identifier") or "").strip()
            key = "student_code" if code.isdigit() else "email"
            found = repo().where("users", **{key: code if key == "student_code" else code.lower()})
            pos = positions.get(request.form.get("position_id"))
            if not found or not pos or not in_year(pos, year):
                flash("ไม่พบผู้ใช้หรือตำแหน่ง", "danger")
            elif pos.get("single_holder") and repo().where("position_holders", position_id=pos["id"],
                                                           academic_year=year, status="ACTIVE"):
                flash(f"ตำแหน่ง {pos['name']} มีผู้ถือได้คนเดียวและมีผู้ถืออยู่แล้วในปี {year}", "danger")
            elif repo().where("position_holders", position_id=pos["id"], user_id=found[0]["id"],
                              academic_year=year, status="ACTIVE"):
                flash("ผู้ใช้นี้ถือตำแหน่งนี้อยู่แล้ว", "warning")
            else:
                hid = repo().add("position_holders", {
                    "user_id": found[0]["id"], "position_id": pos["id"], "position_code": pos["code"],
                    "academic_year": year, "start_date": request.form.get("start_date") or now_iso()[:10],
                    "end_date": request.form.get("end_date") or None, "status": "ACTIVE",
                    "source": "ADMIN", "created_at": now_iso(),
                })
                audit("holder.add", "position_holder", hid, new={"user": found[0]["id"], "position": pos["code"], "year": year})
                notify(found[0]["id"], "คุณได้รับการแต่งตั้งเป็น " + pos["name"], f"ปีการศึกษา {year}", url_for("auth.profile"))
                flash("เพิ่มผู้ถือตำแหน่งแล้ว", "success")
        elif action == "rename_division":
            rename_division(request.form.get("code"), request.form.get("name"))
        elif action == "edit":
            h = repo().get("position_holders", request.form.get("holder_id")) or abort(404)
            pos = positions.get(request.form.get("position_id"))
            if h.get("academic_year") != year or h.get("status") != "ACTIVE":
                abort(400)
            if not pos or not in_year(pos, year):
                flash("ไม่พบตำแหน่งที่เลือก", "danger")
            elif pos["id"] != h["position_id"] and pos.get("single_holder") and repo().where(
                    "position_holders", position_id=pos["id"], academic_year=year, status="ACTIVE"):
                flash(f"ตำแหน่ง {pos['name']} มีผู้ถือได้คนเดียวและมีผู้ถืออยู่แล้วในปี {year}", "danger")
            else:
                new = {"position_id": pos["id"], "position_code": pos["code"],
                       "start_date": request.form.get("start_date") or h.get("start_date")}
                repo().update("position_holders", h["id"], new)
                audit("holder.edit", "position_holder", h["id"], old={k: h.get(k) for k in new}, new=new)
                if pos["id"] != h["position_id"]:
                    notify(h["user_id"], "ตำแหน่งของคุณถูกแก้ไขเป็น " + pos["name"], f"ปีการศึกษา {year}", url_for("auth.profile"))
                flash("แก้ไขผู้ถือตำแหน่งแล้ว", "success")
        elif action == "end":
            h = repo().get("position_holders", request.form.get("holder_id"))
            if h:
                repo().update("position_holders", h["id"], {"status": "ENDED", "end_date": now_iso()[:10]})
                audit("holder.end", "position_holder", h["id"])
                flash("สิ้นสุดการถือตำแหน่งแล้ว สิทธิ์หายทันที", "info")
        return redirect(url_for("admin.holders", year=year))
    hs = repo().where("position_holders", academic_year=year)
    users = {u["id"]: u for u in repo().all("users")}
    level_order = {"BOARD": 0, "STAFF": 1, "COMMITTEE": 2, "SUBCOMMITTEE": 3}
    hs.sort(key=lambda h: (h.get("status") != "ACTIVE", level_order.get(positions.get(h["position_id"], {}).get("level"), 9),
                           h.get("position_code", "")))
    in_chart = [p for p in positions.values() if in_year(p, year)]
    missing = [p for p in in_chart if p.get("locked_required") and not any(
        h["position_id"] == p["id"] and h.get("status") == "ACTIVE" for h in hs)]
    pos_list = sorted(in_chart, key=lambda p: (level_order.get(p.get("level"), 9), p.get("division_id") or "", p["code"]))
    vacant = [{"name": p["name"], "backup": ", ".join(FUNCTIONS.get(x, (x,))[0] for x in p.get("fallback_functions") or [])}
              for p in vacant_fallback_positions(year)]
    return render_template("admin/holders.html", holders=hs, users=users, positions=positions, pos_list=pos_list,
                           year=year, missing=missing, vacant=vacant,
                           divisions=sorted(repo().all("divisions"), key=lambda d: d["id"]),
                           edit=next((h for h in hs if h["id"] == request.args.get("edit") and h.get("status") == "ACTIVE"), None),
                           years=sorted(repo().all("academic_years"), key=lambda y: y["id"]))


@dataset("holders")
def ds_holders(args):
    if not (can("admin.holders") or can("report.view")):
        abort(403)
    year = int(args.get("year") or current_year())
    positions = {p["id"]: p for p in repo().all("positions")}
    divisions = {d["id"]: d for d in repo().all("divisions")}
    rows = []
    for h in repo().where("position_holders", academic_year=year, status="ACTIVE"):
        u = repo().get("users", h["user_id"]) or {}
        p = positions.get(h["position_id"], {})
        rows.append([year, p.get("name"), (divisions.get(p.get("division_id")) or {}).get("name"), p.get("level"),
                     u.get("student_code"), u.get("first_name"), u.get("last_name")])
    return f"holders-{year}", ["ปีการศึกษา", "ตำแหน่ง", "กอง", "ระดับ", "รหัสนักศึกษา", "ชื่อ", "นามสกุล"], rows


# ---------- กอง/ตำแหน่ง (ผังบอร์ดรายปี — SRS v3 หัวข้อ 2) ----------
def _position_id(code, year):
    """ผังปีแรกใช้รหัสเป็น id; ผังของปีอื่นที่รหัสซ้ำใช้ รหัส-ปี"""
    return code if not repo().get("positions", code) else f"{code}-{year}"


@bp.route("/positions", methods=["GET", "POST"])
@permission_required("admin.manage")
def positions():
    year = int(request.values.get("year") or current_year())
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "division":
            code = f.get("code", "").strip().upper()
            if code:
                repo().set("divisions", code, {"code": code, "name": f.get("name", "").strip()})
                audit("division.save", "division", code)
                flash("บันทึกกองแล้ว", "success")
        elif action == "rename_division":
            rename_division(f.get("code"), f.get("name"))
        elif action == "delete_division":
            code = f.get("code")
            d = repo().get("divisions", code) or abort(404)
            n = division_usage(code)
            if n:
                flash(f"ลบกอง {d.get('name')} ไม่ได้: มีตำแหน่งหรือข้อมูลอ้างถึงกองนี้ {n} รายการ (เปลี่ยนชื่อได้)", "danger")
            else:
                repo().delete("divisions", code)
                audit("division.delete", "division", code, old={"name": d.get("name")})
                flash(f"ลบกอง {d.get('name')} แล้ว", "info")
        elif action == "position":
            pid = f.get("position_id")
            existing = repo().get("positions", pid) if pid else None
            code = (existing or {}).get("code") or f.get("code", "").strip().upper()
            level = f.get("level") if f.get("level") in ("BOARD", "COMMITTEE", "SUBCOMMITTEE", "STAFF") else "BOARD"
            fn = f.get("function_code") if f.get("function_code") in FUNCTIONS else ""
            if existing and existing.get("locked_required"):
                # ตำแหน่ง locked แก้หน้าที่ (สิทธิ์) ไม่ได้ (PERM-01)
                fn, level = existing.get("function_code") or existing.get("code"), existing.get("level")
            fallback = [x for x in f.getlist("fallback_functions") if x in FUNCTIONS and x]
            if not code or not f.get("name", "").strip():
                flash("กรุณากรอกรหัสและชื่อตำแหน่ง", "danger")
            else:
                all_years = bool(f.get("all_years")) or level != "BOARD"
                data = {
                    "code": code, "name": f.get("name", "").strip(), "level": level,
                    "division_id": f.get("division_id") or None, "function_code": fn, "roles": roles_for(level, fn),
                    "parent_id": f.get("parent_id") or None, "single_holder": bool(f.get("single_holder")),
                    "locked_required": existing.get("locked_required", False) if existing else False,
                    "academic_year": None if all_years else year, "fallback_functions": fallback,
                    "has_fallback": bool(fallback),
                }
                pid = existing["id"] if existing else _position_id(code, year)
                repo().set("positions", pid, data)
                audit("position.save", "position", pid, new={"function": fn, "fallback": fallback, "year": data["academic_year"]})
                flash("บันทึกตำแหน่งแล้ว", "success")
        elif action == "delete_position":
            pos = repo().get("positions", f.get("position_id"))
            if not pos:
                abort(404)
            if pos.get("locked_required") and pos.get("academic_year") in (None, current_year()):
                flash("ตำแหน่ง locked ของวาระปัจจุบันลบไม่ได้", "danger")
            elif repo().where("position_holders", position_id=pos["id"]):
                flash("ตำแหน่งนี้มีประวัติผู้ถือ ลบไม่ได้ (สิ้นสุดผู้ถือแล้วปล่อยว่างไว้แทน)", "danger")
            else:
                repo().delete("positions", pos["id"])
                audit("position.delete", "position", pos["id"], old={"name": pos.get("name")})
                flash("ลบตำแหน่งแล้ว", "info")
        elif action == "copy_chart":
            # คัดลอกผังบอร์ดของปีที่ดูอยู่ไปปีใหม่ เพื่อปรับผังปีหน้าโดยไม่แก้โค้ด
            to = int(f.get("to_year") or 0)
            src = [p for p in repo().all("positions") if p.get("academic_year") == year]
            if not repo().get("academic_years", str(to)):
                flash("ยังไม่มีปีการศึกษาปลายทาง เพิ่มที่หน้าปีการศึกษาก่อน", "danger")
            elif any(p.get("academic_year") == to for p in repo().all("positions")):
                flash(f"ปี {to} มีผังอยู่แล้ว", "warning")
            elif not src:
                flash(f"ปี {year} ไม่มีผังให้คัดลอก", "warning")
            else:
                ids = {p["id"]: f"{p['code']}-{to}" for p in src}
                repo().set_many([("positions", ids[p["id"]], dict(p, academic_year=to, parent_id=ids.get(p.get("parent_id"))))
                                 for p in src])
                audit("position.copy_chart", "academic_year", str(to), new={"from": year, "count": len(src)})
                flash(f"คัดลอกผัง {len(src)} ตำแหน่งจากปี {year} ไปปี {to} แล้ว", "success")
                year = to
        elif action == "seed_defaults":
            # เพิ่มกอง/ตำแหน่งตั้งต้นที่ยังไม่มี (เช่น กองที่เพิ่มในรุ่นใหม่) ไม่แก้ของที่มีอยู่แล้ว
            from ..seed import run_seed
            before = (len(repo().all("divisions")), len(repo().all("positions")))
            run_seed(current_year())  # รวมการปรับชื่อตั้งต้นที่เปลี่ยนในรุ่นใหม่ (เฉพาะรายการที่ยังใช้ชื่อเดิม)
            after = (len(repo().all("divisions")), len(repo().all("positions")))
            audit("system.seed_defaults", "system_settings", "seed_info", new={"divisions": after[0] - before[0], "positions": after[1] - before[1]})
            flash(f"เพิ่มกอง {after[0] - before[0]} กอง และตำแหน่ง {after[1] - before[1]} ตำแหน่งที่ยังไม่มี", "success")
        elif action == "migrate_v3":
            from ..seed import migrate_v3
            done = migrate_v3(current_year())
            audit("system.migrate_v3", "system_settings", "migrations", new={k: v for k, v in done.items()})
            flash(f"ย้ายเป็นผังบอร์ด 69 แล้ว: ตำแหน่ง {done['positions']} · ย้ายผู้ถือ {done['holders_moved']} · "
                  f"แก้ข้อมูลที่อ้างกองเดิม {done['division_refs']} · ล้างตัวตนเรื่องนิรนาม {done['anon_tickets']} · "
                  f"คำขอยืม {done['equipment_requests']}", "success")
            if done["holders_ended"]:
                flash("ผู้ถือตำแหน่งที่ถูกสิ้นสุดเพราะตำแหน่งถูกยกเลิก (แต่งตั้งใหม่ได้ที่หน้าผู้ถือตำแหน่ง): "
                      + ", ".join(done["holders_ended"]), "warning")
        return redirect(url_for("admin.positions", year=year))
    divs = sorted(repo().all("divisions"), key=lambda d: d["id"])
    level_order = {"BOARD": 0, "STAFF": 1, "COMMITTEE": 2, "SUBCOMMITTEE": 3}
    every = repo().all("positions")
    pos = sorted([p for p in every if in_year(p, year)],
                 key=lambda p: (level_order.get(p.get("level"), 9), p.get("division_id") or "", p["code"]))
    by_id = {p["id"]: p for p in every}
    holders = repo().where("position_holders", academic_year=year, status="ACTIVE")
    for p in pos:
        p["holder_count"] = sum(1 for h in holders if h["position_id"] == p["id"])
        p["parent_name"] = (by_id.get(p.get("parent_id")) or {}).get("name")
    from .org import DIVISION_ORDER, _depth
    div_names = {d["id"]: d.get("name") for d in divs}
    for p in pos:
        p["depth"] = _depth(p, by_id)
    board = [p for p in pos if p.get("level") == "BOARD"]
    order = DIVISION_ORDER + sorted(d for d in div_names if d not in DIVISION_ORDER)
    groups = [{"title": "ส่วนกลาง (ไม่สังกัดกอง)", "icon": "star", "rows": [p for p in board if not p.get("division_id")]}]
    for d in order:
        rows = [p for p in pos if p.get("division_id") == d]
        if d in div_names or rows:
            groups.append({"title": div_names.get(d, d), "icon": "diagram-3", "rows": rows})
    rest = [p for p in pos if p.get("level") != "BOARD" and not p.get("division_id")]
    if rest:
        groups.append({"title": "อื่น ๆ (ใช้ทุกปี)", "icon": "people", "rows": rest})
    for g in groups:
        g["rows"].sort(key=lambda p: (level_order.get(p.get("level"), 9), p["depth"], p.get("name") or ""))
    groups = [g for g in groups if g["rows"]]
    old_codes = set(OLD_POSITIONS) | set(OLD_DIVISIONS)
    needs_migration = not (repo().get("system_settings", "migrations") or {}).get("v3") and (
        any(p["id"] in old_codes for p in every) or any(d["id"] in old_codes for d in divs)
        or any(p.get("level") == "BOARD" and "function_code" not in p for p in every))
    return render_template("admin/positions.html", divs=divs, pos=pos, role_labels=ROLE_LABELS, functions=FUNCTIONS,
                           year=year, years=sorted(repo().all("academic_years"), key=lambda y: y["id"]),
                           needs_migration=needs_migration, groups=groups,
                           edit=repo().get("positions", request.args.get("edit")) if request.args.get("edit") else None)


# ---------- ตารางสาขา ----------
@bp.route("/programs", methods=["GET", "POST"])
@permission_required("admin.manage")
def programs():
    if request.method == "POST":
        f = request.form
        code = f.get("code", "").strip()
        if len(code) != 2 or not code.isdigit():
            flash("รหัสสาขาต้องเป็นตัวเลข 2 หลัก", "danger")
        else:
            old = repo().get("programs", code)
            data = {"code": code, "name_th": f.get("name_th", "").strip(),
                    "former_names": [x.strip() for x in f.get("former_names", "").split(",") if x.strip()],
                    "faculty_code": f.get("faculty_code", "SCI").strip() or "SCI",
                    "is_science_faculty": bool(f.get("is_science_faculty")),
                    "admitting_until": int(f["admitting_until"]) if f.get("admitting_until") else None}
            repo().set("programs", code, data)
            audit("program.save", "program", code, old=old, new=data)
            flash("บันทึกแล้ว", "success")
        return redirect(url_for("admin.programs"))
    items = sorted(repo().all("programs"), key=lambda p: p["id"])
    levels = sorted(repo().all("student_level_codes"), key=lambda p: p["id"])
    return render_template("admin/programs.html", items=items, levels=levels,
                           edit=repo().get("programs", request.args.get("edit")) if request.args.get("edit") else None)


# ---------- ตั้งค่าระบบ ----------
@bp.route("/settings", methods=["GET", "POST"])
@permission_required("admin.manage")
def system_settings():
    if request.method == "POST":
        cur = settings()
        new = {}
        for k, default in DEFAULT_SETTINGS.items():
            if k == "current_academic_year":
                continue
            if isinstance(default, bool):
                new[k] = bool(request.form.get(k))
            else:
                try:
                    new[k] = int(request.form.get(k, default))
                except ValueError:
                    new[k] = default
        repo().set("system_settings", "main", new, merge=True)
        audit("settings.update", "system_settings", "main", old={k: cur.get(k) for k in new}, new=new)
        flash("บันทึกการตั้งค่าแล้ว", "success")
        return redirect(url_for("admin.system_settings"))
    return render_template("admin/settings.html", s=settings())


# ---------- สิทธิ์ ----------
@bp.route("/permissions", methods=["GET", "POST"])
@permission_required("admin.manage")
def permissions():
    extra = repo().get("system_settings", "permission_extra") or {}
    if request.method == "POST":
        perm = request.form.get("perm")
        if perm not in PERMISSIONS or PERMISSIONS[perm][2]:
            abort(403)  # สิทธิ์ locked แก้ไม่ได้
        roles = [r for r in request.form.getlist("roles") if r in ROLE_LABELS]
        extra = {k: v for k, v in extra.items() if k != "id"}
        old = extra.get(perm)
        extra[perm] = roles
        repo().set("system_settings", "permission_extra", extra)
        audit("permission.update", "permission", perm, old=old, new=roles)
        flash("บันทึกสิทธิ์แล้ว", "success")
        return redirect(url_for("admin.permissions"))
    perms = {k: v for k, v in PERMISSIONS.items()
             if current_app.config.get("ACTIVITIES_ENABLED") or not k.startswith("act.")}
    return render_template("admin/permissions.html", perms=perms, extra=extra, role_labels=ROLE_LABELS)


# ---------- Audit ----------
def _audit_items(args):
    items = repo().all("audit_logs")
    q = (args.get("q") or "").strip()
    if q:
        items = [a for a in items if q in (a.get("action") or "") or q in (a.get("actor_name") or "")
                 or q in str(a.get("entity_id") or "")]
    return sorted(items, key=lambda a: a.get("at", ""), reverse=True)[:1000]


@bp.route("/audit")
@permission_required("audit.view")
def audit_log():
    return render_template("admin/audit.html", items=_audit_items(request.args)[:300], args=request.args)


@dataset("audit")
def ds_audit(args):
    if not can("audit.view"):
        abort(403)
    rows = [[a.get("at"), a.get("actor_name"), a.get("action"), a.get("entity_type"), a.get("entity_id"),
             a.get("note")] for a in _audit_items(args)]
    return "audit-log", ["เวลา", "ผู้ทำ", "การกระทำ", "ประเภท", "รหัสรายการ", "หมายเหตุ"], rows


# ---------- มอบสิทธิ์คัดกรอง (PERM-02) ----------
@bp.route("/delegations", methods=["GET", "POST"])
@permission_required("delegation.manage")
def delegations():
    if not current_app.config.get("ACTIVITIES_ENABLED"):
        abort(404)
    if request.method == "POST":
        if request.form.get("action") == "grant":
            to = request.form.get("to_user_id")
            act = repo().get("activities", request.form.get("activity_id"))
            if to not in holders_of_role("PERSONNEL_HEAD") or not act:
                flash("ผู้รับมอบต้องเป็นหัวหน้าฝ่ายบุคลากรในวาระปัจจุบัน", "danger")
            else:
                did = repo().add("delegations", {
                    "permission": "act.screen", "from_user_id": current_user()["id"], "to_user_id": to,
                    "scope_type": "ACTIVITY", "scope_id": act["id"], "scope_name": act.get("title"),
                    "expires_at": form_dt(request.form.get("expires_at")), "revoked_at": None, "created_at": now_iso(),
                })
                audit("delegation.grant", "delegation", did, new={"to": to, "activity": act["id"]})
                notify(to, "ได้รับมอบหมายให้คัดกรองทีมงาน", act.get("title"), url_for("activities.detail", aid=act["id"]))
                flash("มอบสิทธิ์แล้ว", "success")
        elif request.form.get("action") == "revoke":
            d = repo().get("delegations", request.form.get("delegation_id"))
            if d:
                repo().update("delegations", d["id"], {"revoked_at": now_iso()})
                audit("delegation.revoke", "delegation", d["id"])
                flash("ถอนสิทธิ์แล้ว", "info")
        return redirect(url_for("admin.delegations"))
    items = sorted(repo().all("delegations"), key=lambda d: d.get("created_at", ""), reverse=True)
    for d in items:
        exp = parse_dt(d.get("expires_at"))
        d["active"] = not d.get("revoked_at") and (not exp or exp > now())
    users = {u["id"]: u for u in repo().all("users")}
    heads = [users[i] for i in holders_of_role("PERSONNEL_HEAD") if i in users]
    acts = [a for a in repo().where("activities", academic_year=current_year())]
    return render_template("admin/delegations.html", items=items, users=users, heads=heads, acts=acts)
