"""ทะเบียนกรรมการ/อนุกรรมการ: นำเข้าจากไฟล์ที่ส่งออกจาก Google Sheet (CSV หรือ Excel)

ขั้นตอน: อัปโหลดไฟล์ -> ระบบอ่านและแสดงตัวอย่าง + ให้จับคู่ "ฝ่าย" กับกอง -> ยืนยัน -> บันทึก
- คนที่มีบัญชีแล้วได้ตำแหน่งกรรมการ/อนุกรรมการของกองทันที
- คนที่ยังไม่มีบัญชี ระบบจำรหัสนักศึกษาไว้ และผูกตำแหน่งให้เองเมื่อสมัครสมาชิกหรือเข้าสู่ระบบ
- ไม่เก็บ LineID และคอลัมน์อื่นที่ระบบไม่ใช้
"""
import csv
import io
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, current_year, in_year, permission_required
from ..services import audit
from ..utils import now_iso, sort_thai

bp = Blueprint("roster", __name__, url_prefix="/admin/roster")
TYPES = {"COMM": ("กรรมการ", "COMMITTEE", "COMM_"), "SUBCOMM": ("อนุกรรมการ", "SUBCOMMITTEE", "SUB_")}
MAX_ROWS = 1500
# หัวคอลัมน์ที่ระบบใช้ (จับคู่จากคำที่อยู่ในหัวคอลัมน์ของไฟล์)
COLUMNS = {"student_code": ("รหัสนักศึกษา", "รหัส นศ", "student"), "name": ("ชื่อ",), "program": ("สาขา",),
           "unit": ("ฝ่าย", "กอง"), "year_level": ("ชั้นปี",), "status": ("สถานะ",)}
PREFIXES = ("นางสาว", "น.ส.", "นาง", "นาย", "ว่าที่ร้อยตรีหญิง", "ว่าที่ร้อยตรี", "ว่าที่ ร.ต.หญิง", "ว่าที่ ร.ต.")
# เดากองจากชื่อฝ่าย (ผู้นำเข้าแก้ได้ในหน้าตัวอย่าง)
DIVISION_HINTS = [("PR", ("ประชาสัมพันธ์", "สื่อ", "รูป", "วิดีโอ", "วีดีโอ", "แบนเนอร์", "คอนเทนต์", "reels", "กราฟิก")),
                  ("POLICY", ("แผน", "นโยบาย")),
                  ("ACTIVITY", ("กิจกรรม", "วิชาการ", "พิธีกร", "กีฬา", "นันทนาการ", "จิตอาสา", "วัฒนธรรม")),
                  ("ADMIN", ("บริหาร", "สัมพันธ์", "บุคลากร", "อุปกรณ์", "สถานที่", "สวัสดิการ", "พยาบาล", "จัดหาทุน", "ทุน",
                             "เลขา", "เหรัญญิก"))]


# ---------- อ่านไฟล์ ----------
def read_table(filename, data):
    """คืนรายการแถว (list ของ list ข้อความ) จาก .csv หรือ .xlsx"""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        return [["" if c is None else str(c).strip() for c in row] for row in ws.iter_rows(values_only=True)]
    if name.endswith(".csv") or name.endswith(".tsv") or name.endswith(".txt"):
        text = None
        for enc in ("utf-8-sig", "cp874"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("อ่านไฟล์ไม่ได้ กรุณาบันทึกเป็น CSV (UTF-8) หรือ Excel (.xlsx)")
        delim = "\t" if text.count("\t") > text.count(",") else ","
        return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim)]
    raise ValueError("รองรับเฉพาะไฟล์ .xlsx และ .csv")


def split_name(full):
    full = re.sub(r"\s+", " ", (full or "").strip())
    for p in PREFIXES:
        if full.startswith(p):
            full = full[len(p):].strip()
            break
    first, _, last = full.partition(" ")
    return first, last.strip()


def clean_code(value):
    """รหัสนักศึกษาจาก Excel อาจมาเป็น 406765001.0"""
    v = re.sub(r"\.0+$", "", str(value or "").strip())
    return re.sub(r"\D", "", v)


def parse_rows(table):
    """หาแถวหัวตาราง จับคู่คอลัมน์ แล้วคืน (rows, ข้อความแจ้งเตือน)"""
    head_i = next((i for i, r in enumerate(table[:15]) if any("รหัสนักศึกษา" in c for c in r)), None)
    if head_i is None:
        raise ValueError('ไม่พบหัวคอลัมน์ "รหัสนักศึกษา" ในไฟล์')
    head = table[head_i]
    idx = {}
    for key, words in COLUMNS.items():
        for i, h in enumerate(head):
            if i not in idx.values() and any(w in h.lower() for w in words):
                idx[key] = i
                break
    if "name" not in idx:
        raise ValueError('ไม่พบคอลัมน์ชื่อ (เช่น "ชื่อ - นามสกุล")')
    rows, skipped, seen = [], 0, set()

    def cell(r, key):
        i = idx.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    for r in table[head_i + 1:]:
        code = clean_code(cell(r, "student_code"))
        first, last = split_name(cell(r, "name"))
        if not code or not first or code in seen:
            skipped += bool(any(c for c in r))
            continue
        seen.add(code)
        yl = clean_code(cell(r, "year_level"))
        rows.append({"student_code": code, "first_name": first, "last_name": last, "program": cell(r, "program"),
                     "unit": cell(r, "unit"), "year_level": int(yl) if yl.isdigit() and len(yl) == 1 else None,
                     "status": cell(r, "status")})
        if len(rows) > MAX_ROWS:
            raise ValueError(f"ไฟล์มีเกิน {MAX_ROWS} แถว กรุณาแบ่งไฟล์")
    return rows, skipped


def guess_division(unit, valid):
    low = (unit or "").lower()
    for div, words in DIVISION_HINTS:
        if div in valid and any(w in low for w in words):
            return div
    return ""


# ---------- ผูกตำแหน่งกับบัญชี ----------
def _ensure_holder(user, entry):
    """ให้ผู้ใช้ถือตำแหน่งตามทะเบียน (ไม่สร้างซ้ำ) คืน True ถ้าเพิ่งสร้าง"""
    year = entry["academic_year"]
    have = repo().where("position_holders", user_id=user["id"], position_id=entry["position_id"], academic_year=year,
                        status="ACTIVE")
    created = False
    if not have:
        hid = repo().add("position_holders", {
            "user_id": user["id"], "position_id": entry["position_id"], "position_code": entry["position_id"],
            "academic_year": year, "start_date": now_iso()[:10], "end_date": None, "status": "ACTIVE",
            "source": "ROSTER", "roster_id": entry["id"], "created_at": now_iso()})
        created = True
    else:
        hid = have[0]["id"]
    if entry.get("user_id") != user["id"] or entry.get("holder_id") != hid:
        repo().update("member_roster", entry["id"], {"user_id": user["id"], "holder_id": hid, "linked_at": now_iso()})
    return created


def attach_user(user):
    """เรียกตอนสมัครสมาชิก/เข้าสู่ระบบ: ถ้ารหัสนักศึกษาอยู่ในทะเบียนของวาระปัจจุบัน ให้ได้ตำแหน่งทันที"""
    code = (user or {}).get("student_code")
    if not code:
        return 0
    n = 0
    for e in repo().where("member_roster", student_code=code, academic_year=current_year()):
        if not e.get("user_id") and e.get("position_id"):
            n += _ensure_holder(user, e)
    return n


def _end_holder(entry):
    h = repo().get("position_holders", entry.get("holder_id")) if entry.get("holder_id") else None
    if h and h.get("source") == "ROSTER" and h.get("status") == "ACTIVE":
        repo().update("position_holders", h["id"], {"status": "ENDED", "end_date": now_iso()[:10]})


# ---------- หน้าจอ ----------
def _positions(year):
    return {p["id"]: p for p in repo().all("positions") if in_year(p, year)}


def _filter(args):
    year = int(args.get("year") or current_year())
    rows = repo().where("member_roster", academic_year=year)
    if args.get("type") in TYPES:
        rows = [r for r in rows if r.get("member_type") == args["type"]]
    if args.get("division"):
        rows = [r for r in rows if r.get("division_id") == args["division"]]
    if args.get("link") == "pending":
        rows = [r for r in rows if not r.get("user_id")]
    q = (args.get("q") or "").strip()
    if q:
        rows = [r for r in rows if q in r.get("student_code", "") or q in f"{r.get('first_name', '')} {r.get('last_name', '')}"]
    rows = sort_thai(rows, lambda r: r.get("first_name"))
    rows.sort(key=lambda r: (r.get("member_type") != "COMM", r.get("division_id") or ""))
    return year, rows


@bp.route("/", methods=["GET", "POST"])
@permission_required("admin.holders")
def index():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        year = int(f.get("year") or current_year())
        if action == "upload":
            fs = request.files.get("file")
            mtype = f.get("member_type")
            if mtype not in TYPES:
                flash("กรุณาเลือกว่าเป็นไฟล์กรรมการหรืออนุกรรมการ", "danger")
            elif not fs or not fs.filename:
                flash("กรุณาเลือกไฟล์", "danger")
            else:
                try:
                    rows, skipped = parse_rows(read_table(fs.filename, fs.read()))
                except ValueError as e:
                    flash(str(e), "danger")
                    return redirect(url_for("roster.index", year=year))
                except Exception:  # noqa: BLE001 ไฟล์เสียหรือไม่ใช่ Excel จริง
                    flash("อ่านไฟล์ไม่ได้ ตรวจสอบว่าเป็นไฟล์ .xlsx หรือ .csv ที่ดาวน์โหลดจาก Google Sheet", "danger")
                    return redirect(url_for("roster.index", year=year))
                if not rows:
                    flash("ไม่พบแถวข้อมูลที่มีรหัสนักศึกษาและชื่อ", "warning")
                    return redirect(url_for("roster.index", year=year))
                for old in repo().where("roster_imports", created_by=current_user()["id"]):
                    repo().delete("roster_imports", old["id"])
                iid = repo().add("roster_imports", {"academic_year": year, "member_type": mtype, "rows": rows,
                                                    "skipped": skipped, "filename": fs.filename[:120],
                                                    "created_by": current_user()["id"], "created_at": now_iso()})
                return redirect(url_for("roster.preview", iid=iid))
        elif action == "delete":
            e = repo().get("member_roster", f.get("roster_id")) or abort(404)
            _end_holder(e)
            repo().delete("member_roster", e["id"])
            audit("roster.delete", "member_roster", e["id"], old={"student_code": e.get("student_code")})
            flash("ลบออกจากทะเบียนและสิ้นสุดตำแหน่งที่มาจากทะเบียนแล้ว", "info")
        elif action == "clear":
            mtype = f.get("member_type")
            rows = [r for r in repo().where("member_roster", academic_year=year) if r.get("member_type") == mtype]
            for e in rows:
                _end_holder(e)
                repo().delete("member_roster", e["id"])
            audit("roster.clear", "academic_year", str(year), new={"type": mtype, "count": len(rows)})
            flash(f"ล้างทะเบียน{TYPES.get(mtype, ('',))[0]}ปี {year} แล้ว {len(rows)} รายการ", "info")
        else:
            abort(400)
        return redirect(url_for("roster.index", year=year))
    year, rows = _filter(request.args)
    everyone = repo().where("member_roster", academic_year=year)
    stats = {k: {"all": sum(1 for r in everyone if r.get("member_type") == k),
                 "linked": sum(1 for r in everyone if r.get("member_type") == k and r.get("user_id"))} for k in TYPES}
    divisions = {d["id"]: d for d in repo().all("divisions")}
    return render_template("admin/roster.html", year=year, rows=rows, stats=stats, types=TYPES, divisions=divisions,
                           args=request.args, years=sorted(repo().all("academic_years"), key=lambda y: y["id"]))


@bp.route("/import/<iid>", methods=["GET", "POST"])
@permission_required("admin.holders")
def preview(iid):
    imp = repo().get("roster_imports", iid)
    if not imp or imp.get("created_by") != current_user()["id"]:
        abort(404)
    year, mtype = imp["academic_year"], imp["member_type"]
    label, level, prefix = TYPES[mtype]
    positions = _positions(year)
    divisions = {d["id"]: d for d in repo().all("divisions")}
    # กองที่มีตำแหน่งกรรมการ/อนุกรรมการในผังของปีนั้น
    div_pos = {p.get("division_id"): p["id"] for p in positions.values() if p.get("level") == level and p.get("division_id")}
    rows = imp["rows"]
    units = sorted({r["unit"] for r in rows})
    statuses = sorted({r["status"] for r in rows})
    if request.method == "POST":
        f = request.form
        if f.get("action") == "cancel":
            repo().delete("roster_imports", iid)
            return redirect(url_for("roster.index", year=year))
        unit_map = {u: f.get(f"unit_{i}") for i, u in enumerate(units)}
        keep = {s for i, s in enumerate(statuses) if f.get(f"status_{i}")}
        users = {u.get("student_code"): u for u in repo().all("users") if u.get("student_code")}
        done = {"saved": 0, "linked": 0, "pending": 0, "no_division": 0, "skipped_status": 0}
        for r in rows:
            if r["status"] not in keep:
                done["skipped_status"] += 1
                continue
            div = unit_map.get(r["unit"])
            if div not in div_pos:
                done["no_division"] += 1
                continue
            rid = f"{year}_{mtype}_{r['student_code']}"
            old = repo().get("member_roster", rid)
            if old and old.get("position_id") != div_pos[div]:
                _end_holder(old)  # ย้ายกอง: สิ้นสุดตำแหน่งเดิมที่มาจากทะเบียน
                old = None
            entry = {"academic_year": year, "member_type": mtype, "student_code": r["student_code"],
                     "first_name": r["first_name"], "last_name": r["last_name"], "program": r["program"], "unit": r["unit"],
                     "year_level": r["year_level"], "status_text": r["status"], "division_id": div,
                     "position_id": div_pos[div], "user_id": (old or {}).get("user_id"),
                     "holder_id": (old or {}).get("holder_id"), "imported_by": current_user()["id"],
                     "imported_at": now_iso()}
            repo().set("member_roster", rid, entry)
            entry["id"] = rid
            done["saved"] += 1
            u = users.get(r["student_code"])
            if u:
                _ensure_holder(u, entry)
                done["linked"] += 1
            else:
                done["pending"] += 1
        repo().delete("roster_imports", iid)
        audit("roster.import", "academic_year", str(year), new=dict(done, type=mtype, file=imp.get("filename")))
        flash(f"นำเข้า{label} {done['saved']} คน: มีบัญชีแล้วและได้ตำแหน่งทันที {done['linked']} · รอสมัครสมาชิก {done['pending']}"
              + (f" · ข้ามเพราะไม่ได้เลือกกอง {done['no_division']}" if done["no_division"] else "")
              + (f" · ข้ามตามสถานะ {done['skipped_status']}" if done["skipped_status"] else ""), "success")
        return redirect(url_for("roster.index", year=year, type=mtype))
    choices = [(d, divisions.get(d, {}).get("name", d)) for d in div_pos]
    guess = {u: guess_division(u, div_pos) for u in units}
    counts = {u: sum(1 for r in rows if r["unit"] == u) for u in units}
    st_counts = {s: sum(1 for r in rows if r["status"] == s) for s in statuses}
    known = {u.get("student_code") for u in repo().all("users") if u.get("student_code")}
    return render_template("admin/roster_preview.html", imp=imp, label=label, year=year, rows=rows, units=units,
                           statuses=statuses, choices=choices, guess=guess, counts=counts, st_counts=st_counts,
                           have_account=sum(1 for r in rows if r["student_code"] in known))


@dataset("member_roster")
def ds_roster(args):
    if not can("admin.holders"):
        abort(403)
    year, rows = _filter(args)
    divisions = {d["id"]: d for d in repo().all("divisions")}
    out = [[TYPES.get(r.get("member_type"), ("",))[0], r.get("student_code"), r.get("first_name"), r.get("last_name"),
            r.get("program"), divisions.get(r.get("division_id"), {}).get("name"), r.get("unit"), r.get("year_level"),
            r.get("status_text"), "มีบัญชีแล้ว" if r.get("user_id") else "รอสมัครสมาชิก"] for r in rows]
    return f"member-roster-{year}", ["ประเภท", "รหัสนักศึกษา", "ชื่อ", "นามสกุล", "สาขาวิชา", "กอง", "ฝ่าย", "ชั้นปี",
                                      "สถานะในสโม", "บัญชี"], out
