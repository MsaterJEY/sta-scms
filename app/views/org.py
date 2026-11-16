"""โครงสร้างองค์กร: หน้าสาธารณะแสดงทำเนียบสโมสรตามปีการศึกษา และหน้าใส่ข้อมูล

ผู้ใส่ข้อมูล: เลขานุการนายก และเลขาฝ่ายบริหารและสัมพันธ์ (สิทธิ์ org.manage)
ข้อมูลที่เปิดเผยต่อสาธารณะมีเฉพาะ ชื่อ นามสกุล สาขา รหัสนักศึกษา ตำแหน่ง และรูปภาพ — ไม่ดึงข้อมูลอื่นของบัญชีมาแสดง
"""
import hashlib

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..rbac import can, current_user, current_year, in_year, permission_required
from ..services import audit
from ..storage import UploadError, delete_file, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import now_iso, sort_thai

bp = Blueprint("org", __name__, url_prefix="/org")
DIVISION_ORDER = ["ADMIN", "ACTIVITY", "PR", "POLICY"]
LEVEL_ORDER = {"BOARD": 0, "COMMITTEE": 1, "SUBCOMMITTEE": 2, "STAFF": 3}
LEVEL_LABELS = {"BOARD": "คณะกรรมการบริหาร", "COMMITTEE": "กรรมการ", "SUBCOMMITTEE": "อนุกรรมการ", "STAFF": "อาจารย์ที่ปรึกษา"}


def photo_url(m):
    """URL รูปพร้อมรุ่น (v) ตามไฟล์ เพราะรูปถูกแคชที่เบราว์เซอร์"""
    if not m.get("photo_path"):
        return None
    v = hashlib.md5(m["photo_path"].encode()).hexdigest()[:8]
    return url_for("org.photo", mid=m["id"], v=v)


def _depth(pos, by_id):
    """ลำดับชั้นของตำแหน่งในผัง (นายก = 0)"""
    d, seen = 0, set()
    while pos and pos.get("parent_id") and pos["parent_id"] not in seen:
        seen.add(pos["id"])
        pos = by_id.get(pos["parent_id"])
        d += 1
    return d


def chart(year):
    """สมาชิกของปีนั้นพร้อมข้อมูลตำแหน่ง/กอง/สาขา เรียงตามลำดับชั้นในผัง"""
    positions = {p["id"]: p for p in repo().all("positions")}
    divisions = {d["id"]: d for d in repo().all("divisions")}
    programs = {p["id"]: p for p in repo().all("programs")}
    rows = repo().where("org_members", academic_year=year)
    for m in rows:
        pos = positions.get(m.get("position_id")) or {}
        m["position_name"] = m.get("position_title") or pos.get("name") or "-"
        m["level"] = pos.get("level") or "BOARD"
        m["division_id"] = pos.get("division_id")
        m["division_name"] = (divisions.get(pos.get("division_id")) or {}).get("name")
        m["depth"] = _depth(pos, positions)
        m["program_name"] = (programs.get(m.get("program_code")) or {}).get("name_th") or ""
        m["full_name"] = f"{m.get('first_name', '')} {m.get('last_name', '')}".strip()
        m["photo"] = photo_url(m)
    rows = sort_thai(rows, lambda m: m["full_name"])
    rows.sort(key=lambda m: (LEVEL_ORDER.get(m["level"], 9), m["depth"], int(m.get("order") or 0), m["position_name"]))
    return rows, divisions


def sections(rows, divisions):
    """จัดกลุ่มสำหรับแสดงผล: ส่วนกลาง (ไม่สังกัดกอง) แล้วตามด้วยกองตามลำดับผัง"""
    top = [m for m in rows if not m["division_id"] and m["level"] == "BOARD"]
    advisors = [m for m in rows if m["level"] == "STAFF"]
    order = DIVISION_ORDER + sorted(d for d in divisions if d not in DIVISION_ORDER)
    groups = []
    for d in order:
        members = [m for m in rows if m["division_id"] == d]
        if members:
            groups.append({"name": divisions.get(d, {}).get("name", d),
                           "board": [m for m in members if m["level"] == "BOARD"],
                           "others": [(LEVEL_LABELS[lv], [m for m in members if m["level"] == lv])
                                      for lv in ("COMMITTEE", "SUBCOMMITTEE") if any(m["level"] == lv for m in members)]})
    loose = [m for m in rows if not m["division_id"] and m["level"] in ("COMMITTEE", "SUBCOMMITTEE")]
    return top, advisors, groups, loose


def _years_with_data():
    years = {int(y["id"]) for y in repo().all("academic_years") if str(y["id"]).isdigit()}
    return sorted(years | {current_year()}, reverse=True)


@bp.route("/")
def index():
    year = int(request.args.get("year") or current_year())
    rows, divisions = chart(year)
    top, advisors, groups, loose = sections(rows, divisions)
    return render_template("org/index.html", year=year, years=_years_with_data(), total=len(rows), top=top,
                           advisors=advisors, groups=groups, loose=loose,
                           can_manage=bool(current_user()) and can("org.manage"))


@bp.route("/photo/<mid>")
def photo(mid):
    m = repo().get("org_members", mid)
    if not m or not m.get("photo_path"):
        abort(404)
    return Response(load_bytes(m["photo_path"]), mimetype=mimetype_for(m["photo_path"]),
                    headers={"Cache-Control": "public, max-age=86400"})


@bp.route("/manage", methods=["GET", "POST"])
@permission_required("org.manage")
def manage():
    year = int(request.values.get("year") or current_year())
    positions = sorted([p for p in repo().all("positions") if in_year(p, year)],
                       key=lambda p: (LEVEL_ORDER.get(p.get("level"), 9), p.get("division_id") or "", p.get("name") or ""))
    pos_ids = {p["id"] for p in positions}
    programs = sorted(repo().all("programs"), key=lambda p: p["id"])
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        m = repo().get("org_members", f.get("member_id")) if f.get("member_id") else None
        if m and m.get("academic_year") != year:
            abort(404)
        if action == "save":
            data = {"first_name": f.get("first_name", "").strip()[:80], "last_name": f.get("last_name", "").strip()[:80],
                    "student_code": f.get("student_code", "").strip()[:20], "program_code": f.get("program_code") or None,
                    "position_id": f.get("position_id"), "position_title": f.get("position_title", "").strip()[:120],
                    "order": int(f.get("order") or 0) if (f.get("order") or "0").lstrip("-").isdigit() else 0}
            error = None
            if not data["first_name"] or not data["last_name"]:
                error = "กรุณากรอกชื่อและนามสกุล"
            elif data["position_id"] not in pos_ids:
                error = "กรุณาเลือกตำแหน่ง"
            elif data["student_code"] and not data["student_code"].isdigit():
                error = "รหัสนักศึกษาต้องเป็นตัวเลข"
            elif data["program_code"] and data["program_code"] not in {p["id"] for p in programs}:
                error = "ไม่พบสาขาที่เลือก"
            fs = request.files.get("photo")
            if not error and fs and fs.filename:
                try:
                    blob, ext, ctype = validate_upload(fs, images_only=True, allow_pdf=False)
                    data["photo_path"] = save_bytes("org", blob, ext, ctype)
                except UploadError as e:
                    error = f"รูปภาพ: {e}"
            elif not error and m and f.get("remove_photo"):
                data["photo_path"] = None
            if error:
                flash(error, "danger")
                return redirect(url_for("org.manage", year=year, edit=m["id"] if m else None))
            if m:
                if "photo_path" in data and m.get("photo_path"):
                    delete_file(m["photo_path"])
                repo().update("org_members", m["id"], data)
                audit("org.member_update", "org_member", m["id"])
            else:
                data.update({"academic_year": year, "created_by": current_user()["id"], "created_at": now_iso()})
                data.setdefault("photo_path", None)
                mid = repo().add("org_members", data)
                audit("org.member_add", "org_member", mid, new={"position": data["position_id"]})
            flash("บันทึกแล้ว", "success")
        elif action == "delete" and m:
            if m.get("photo_path"):
                delete_file(m["photo_path"])
            repo().delete("org_members", m["id"])
            audit("org.member_delete", "org_member", m["id"], old={"name": f"{m.get('first_name')} {m.get('last_name')}"})
            flash("ลบรายการแล้ว", "info")
        elif action == "import":
            # ดึงผู้ถือตำแหน่งระดับบอร์ดของปีนั้นมาเป็นรายการตั้งต้น (เฉพาะ ชื่อ นามสกุล สาขา รหัสนักศึกษา ตำแหน่ง)
            have = {(x.get("position_id"), x.get("student_code") or x.get("first_name")) for x in repo().where("org_members", academic_year=year)}
            n = 0
            for h in repo().where("position_holders", academic_year=year, status="ACTIVE"):
                pos = next((p for p in positions if p["id"] == h["position_id"]), None)
                u = repo().get("users", h["user_id"]) if pos else None
                if not u or pos.get("level") != "BOARD" or (pos["id"], u.get("student_code") or u.get("first_name")) in have:
                    continue
                repo().add("org_members", {"academic_year": year, "position_id": pos["id"], "position_title": "",
                                           "first_name": u.get("first_name") or "", "last_name": u.get("last_name") or "",
                                           "student_code": u.get("student_code") or "", "program_code": u.get("program_code"),
                                           "photo_path": None, "order": 0, "created_by": current_user()["id"],
                                           "created_at": now_iso()})
                n += 1
            audit("org.import_holders", "academic_year", str(year), new={"count": n})
            flash(f"นำเข้าจากผู้ถือตำแหน่ง {n} รายการ" if n else "ไม่มีรายการใหม่ให้นำเข้า", "success" if n else "info")
        else:
            abort(400)
        return redirect(url_for("org.manage", year=year))
    rows, _ = chart(year)
    edit = repo().get("org_members", request.args.get("edit")) if request.args.get("edit") else None
    if edit:
        edit["photo"] = photo_url(edit)
    return render_template("org/manage.html", year=year, years=_years_with_data(), rows=rows, positions=positions,
                           programs=programs, edit=edit if edit and edit.get("academic_year") == year else None)
