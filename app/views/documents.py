"""4.0 เอกสาร (DOC-01..03) — ทะเบียนลิงก์เอกสาร

ระบบไม่เก็บไฟล์เอกสาร เก็บเฉพาะชื่อ ประเภท กอง กิจกรรม และลิงก์ (เช่น Google Drive)
การแชร์ไฟล์จริงควบคุมที่ Google Drive ส่วนระบบควบคุมว่าใครเห็นรายการ/ลิงก์

ระดับการเข้าถึง (ข้อสมมติ DOC-02, SRS v3: อาจารย์ที่ปรึกษาไม่อยู่ในรายชื่อผู้เข้าถึงเอกสาร):
- PRIVATE  : ผู้สร้าง
- DIVISION : ผู้สร้าง + กองเจ้าของเอกสาร + นายก
- BOARD    : ผู้สร้าง + บอร์ดบริหารทุกคน
- MEMBERS  : ผู้ถือตำแหน่งทุกคนในวาระ (บอร์ด กรรมการ อนุกรรมการ)

ผู้บันทึกเอกสาร: บอร์ดบริหารบันทึกได้เฉพาะของกองที่ตนสังกัด (SRS v3 หัวข้อ 8)
"""
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import board_divisions, can, current_user, current_year, login_required, user_holdings, user_roles
from ..services import audit
from ..utils import now_iso

bp = Blueprint("documents", __name__, url_prefix="/documents")
DOC_TYPES = {"APPOINTMENT": "คำสั่งแต่งตั้ง", "PROJECT": "โครงการ", "REPORT": "รายงานผล", "EVIDENCE": "ภาพ/หลักฐาน",
             "OTHER": "อื่น ๆ"}
ACCESS = {"PRIVATE": "เฉพาะผู้สร้าง", "DIVISION": "กองเจ้าของเอกสาร", "BOARD": "บอร์ดบริหาร", "MEMBERS": "สมาชิกสโมสรทุกคน"}


def valid_link(url):
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return False
    return p.scheme == "https" and bool(p.netloc)


def member_holdings(user):
    """ตำแหน่งของสมาชิกสโมสร (ไม่นับอาจารย์ที่ปรึกษา/ระดับ STAFF)"""
    return [h for h in user_holdings(user["id"]) if h["position"].get("level") != "STAFF"]


def can_see(d, user):
    roles = user_roles(user)
    if d.get("created_by") == user["id"]:
        return True
    lvl = d.get("access_level", "DIVISION")
    if lvl == "PRIVATE":
        return False
    if lvl == "MEMBERS":
        return bool(member_holdings(user))
    if lvl == "BOARD":
        return "BOARD" in roles
    # DIVISION
    if "PRES" in roles:
        return True
    divs = {h["position"].get("division_id") for h in member_holdings(user)}
    return d.get("division_id") in divs


def can_edit(d, user):
    return d.get("created_by") == user["id"] or (can("doc.upload") and d.get("division_id") in board_divisions(user))


def visible_docs(args):
    u = current_user()
    docs = [d for d in repo().all("documents") if can_see(d, u)]
    q = (args.get("q") or "").strip()
    if q:
        docs = [d for d in docs if q in (d.get("title") or "") or q in (d.get("note") or "")]
    for key in ("doc_type", "activity_id", "division_id"):
        if args.get(key):
            docs = [d for d in docs if d.get(key) == args[key]]
    if args.get("year"):
        docs = [d for d in docs if str(d.get("academic_year")) == args["year"]]
    return sorted(docs, key=lambda d: d.get("created_at", ""), reverse=True)


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    u = current_user()
    if not (member_holdings(u) or can("report.view")):
        abort(403)
    my_divs = board_divisions(u)
    activities_on = bool(current_app.config.get("ACTIVITIES_ENABLED"))
    if request.method == "POST":
        f = request.form
        action = f.get("action", "add")
        if action == "delete":
            d = repo().get("documents", f.get("doc_id")) or abort(404)
            if not can_edit(d, u):
                abort(403)
            repo().delete("documents", d["id"])
            audit("document.delete", "document", d["id"], old={"title": d.get("title"), "url": d.get("url")})
            flash("ลบรายการแล้ว (ไฟล์ใน Google Drive ไม่ถูกลบ)", "info")
            return redirect(url_for("documents.index"))
        if not can("doc.upload") or f.get("division_id") not in my_divs:
            abort(403)
        link = (f.get("url") or "").strip()
        if not f.get("title", "").strip():
            flash("กรุณาระบุชื่อเอกสาร", "danger")
        elif not valid_link(link):
            flash("ลิงก์ต้องขึ้นต้นด้วย https://", "danger")
        else:
            act = repo().get("activities", f.get("activity_id")) if activities_on and f.get("activity_id") else None
            did = repo().add("documents", {
                "title": f["title"].strip()[:200], "doc_type": f.get("doc_type", "OTHER"), "url": link,
                "note": f.get("note", "").strip()[:500], "division_id": f["division_id"],
                "activity_id": act["id"] if act else None, "academic_year": current_year(),
                "access_level": f.get("access_level", "DIVISION") if f.get("access_level") in ACCESS else "DIVISION",
                "created_by": u["id"], "created_at": now_iso(),
            })
            audit("document.add_link", "document", did)
            flash("บันทึกลิงก์เอกสารแล้ว", "success")
        return redirect(url_for("documents.index"))
    docs = visible_docs(request.args)
    for d in docs:
        d["editable"] = can_edit(d, u)
    divs = {d["id"]: d for d in repo().all("divisions")}
    acts = {a["id"]: a for a in repo().all("activities")} if activities_on else {}
    users = {d["created_by"]: repo().get("users", d["created_by"]) or {} for d in docs}
    return render_template("docs/index.html", docs=docs, divs=divs, acts=acts, users=users, my_divs=my_divs,
                           DOC_TYPES=DOC_TYPES, ACCESS=ACCESS, args=request.args)


@bp.route("/<did>/open")
@login_required
def open_link(did):
    """ตรวจสิทธิ์และบันทึกการเปิดดู แล้วพาไปยังลิงก์เอกสาร"""
    d = repo().get("documents", did) or abort(404)
    if not can_see(d, current_user()) or not valid_link(d.get("url")):
        abort(403)
    audit("document.view", "document", did)
    return redirect(d["url"])


@dataset("documents")
def ds_documents(args):
    divs = {d["id"]: d for d in repo().all("divisions")}
    rows = [[d.get("title"), DOC_TYPES.get(d.get("doc_type")), (divs.get(d.get("division_id")) or {}).get("name"),
             d.get("academic_year"), ACCESS.get(d.get("access_level")), d.get("url"), d.get("created_at")]
            for d in visible_docs(args)]
    return "documents", ["ชื่อเอกสาร", "ประเภท", "กอง", "ปีการศึกษา", "ระดับการเข้าถึง", "ลิงก์", "วันที่บันทึก"], rows
