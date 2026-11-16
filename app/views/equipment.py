"""3.0 อุปกรณ์และการยืม-คืน (EQP-01..18)

สิทธิ์จัดการทั้งหมดเป็นของ EQUIP_HEAD (locked) ตรวจที่ฝั่งเซิร์ฟเวอร์ทุก route
ธุรกรรม (equipment_transactions) เพิ่มอย่างเดียว ไม่มี route แก้ไข/ลบ
"""
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, holders_of_role, login_required, permission_required, user_roles
from ..services import approval_state, audit, create_approval, decide, display_name, notify, notify_role
from ..storage import UploadError, delete_file, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import form_dt, gen_code, now, now_iso, overlaps, parse_dt, setting, thai_date, to_iso

bp = Blueprint("equipment", __name__, url_prefix="/equipment")
ACTIVE_REQ = ("PENDING", "APPROVED", "ISSUED", "PARTIAL_RETURNED")
OUT_REQ = ("ISSUED", "PARTIAL_RETURNED")


def _req(rid):
    r = repo().get("equipment_requests", rid)
    if not r:
        abort(404)
    return r


def items_of(rid):
    return repo().where("equipment_request_items", request_id=rid)


def outstanding(item):
    return int(item.get("qty_issued") or 0) - int(item.get("qty_returned") or 0) - int(item.get("qty_lost") or 0) \
        - int(item.get("qty_damaged") or 0)


def is_overdue(req):
    """'เกินกำหนด' คำนวณจากวันคืน ไม่เก็บเป็นสถานะ (EQP-15)"""
    due = parse_dt(req.get("due_at"))
    return req.get("status") in OUT_REQ and due is not None and due < now()


def capacity(model):
    if model.get("is_serialized"):
        units = repo().where("equipment_units", model_id=model["id"])
        return len([u for u in units if u.get("status") in ("AVAILABLE", "BORROWED")])
    in_repair = sum(int(m.get("qty") or 0) for m in repo().where("equipment_maintenance", model_id=model["id"], status="IN_PROGRESS")
                    if not m.get("unit_id"))
    return max(0, int(model.get("quantity_total") or 0) - in_repair)


def available(model, start, end, exclude_request=None):
    """EQP-04 คงเหลือในช่วงเวลา: ความจุ หักคำขอที่อนุมัติ/จ่ายแล้วที่ทับช่วงเวลา และของค้างคืนเกินกำหนด"""
    used = 0
    for it in repo().where("equipment_request_items", model_id=model["id"]):
        if it["request_id"] == exclude_request:
            continue
        r = repo().get("equipment_requests", it["request_id"])
        if not r or r.get("status") not in ("APPROVED",) + OUT_REQ:
            continue
        if r["status"] == "APPROVED":
            qty = int(it.get("qty_approved") or 0)
        else:
            qty = outstanding(it)
        if overlaps(start, end, r.get("borrow_at"), r.get("due_at")) or is_overdue(r):
            used += qty
    return max(0, capacity(model) - used)


def borrow_blocks(user):
    """EQP-06 บล็อกเมื่อมีของเกินกำหนด/ค่าชดใช้ค้าง หรือเกินโควตายืมพร้อมกัน"""
    reasons = []
    mine = repo().where("equipment_requests", requester_id=user["id"])
    if any(is_overdue(r) for r in mine):
        reasons.append("มีอุปกรณ์ค้างคืนเกินกำหนด")
    if repo().where("equipment_fines", user_id=user["id"], status="PENDING"):
        reasons.append("มีค่าชดใช้ค้างชำระ")
    active = [r for r in mine if r.get("status") in ACTIVE_REQ]
    if len(active) >= int(setting("max_concurrent_loans") or 3):
        reasons.append(f"มีคำขอที่ยังไม่ปิดครบโควตา {setting('max_concurrent_loans')} รายการ")
    return reasons


def required_doc(user):
    if "EQUIP_HEAD" in user_roles(user):
        return None
    t = user.get("account_type")
    if t == "OTHER_MRU":
        return "STUDENT_CARD"
    if t == "EXTERNAL":
        return "NATIONAL_ID"
    if setting("require_sci_borrower_doc"):
        return "STUDENT_CARD"
    return None


EQP_DEFAULTS = {"max_loan_days": 7, "late_fine_per_item": 0.0, "late_fine_per_day": True,
                "pay_name": "", "pay_note": "", "pay_qr_path": None}
MAX_PHOTOS = 3  # ต่อการอัปโหลดหนึ่งครั้ง (คำขอ HTTP จำกัด 4 MB)
PHOTO_STAGES = {"PICKUP": "ก่อนยืม", "RETURN": "ตอนคืน"}


def eqp_notify(user_ids, title, body="", link=None):
    """แจ้งเตือนทุกรายการของการยืม-คืน: ในระบบ + LINE (ผู้ที่ผูกบัญชีและเปิดรับไว้) ข้อความ LINE ไม่มีรายละเอียดส่วนบุคคล"""
    notify(user_ids, title, body, link, event="EQP_DECISION", line_text="ยืม-คืนอุปกรณ์: " + title)


def photos_of(rid, stage=None):
    rows = sorted(repo().where("equipment_photos", request_id=rid), key=lambda x: x.get("created_at") or "")
    return [x for x in rows if not stage or x.get("stage") == stage]


def needs_pickup_photo(r):
    """ต้องมีรูปก่อนยืมอย่างน้อย 1 รูปก่อนจ่ายอุปกรณ์"""
    return not photos_of(r["id"], "PICKUP")


def needs_return_photo(r):
    """ต้องมีรูปตอนคืนที่ยังไม่ถูกใช้กับการรับคืนครั้งก่อน (คืนบางส่วนก็ต้องถ่ายใหม่ทุกครั้ง)"""
    return not any(not x.get("used") for x in photos_of(r["id"], "RETURN"))


def eqp_settings():
    """ค่าที่หัวหน้าฝ่ายอุปกรณ์กำหนด: ระยะเวลายืมสูงสุด อัตราค่าปรับคืนล่าช้า และช่องทางรับชำระ"""
    doc = repo().get("system_settings", "equipment") or {}
    return {k: doc.get(k, v) for k, v in EQP_DEFAULTS.items()}


def late_days(r, at=None):
    """จำนวนวันที่เกินกำหนดคืน (ปัดขึ้นเป็นวัน) 0 = ไม่เกิน"""
    due = parse_dt(r.get("due_at"))
    at = at or now()
    if not due or at <= due:
        return 0
    return int((at - due).total_seconds() // 86400) + 1


def late_fine_amount(r, lines, at=None):
    """ค่าปรับคืนล่าช้า = อัตราต่อรายการ x จำนวนรายการ (x จำนวนวันที่เกิน ถ้าตั้งให้คิดรายวัน)"""
    s = eqp_settings()
    days = late_days(r, at)
    rate = float(s["late_fine_per_item"] or 0)
    if not days or not rate or not lines:
        return 0.0, days
    return rate * lines * (days if s["late_fine_per_day"] else 1), days


def _can_borrow(user):
    if user.get("account_type") == "EXTERNAL" and not setting("allow_external_borrowers"):
        return False
    return True


# ---------- แคตตาล็อก ----------
@bp.route("/")
@login_required
def catalog():
    models = [m for m in repo().all("equipment_models") if m.get("is_active", True)]
    cats = {c["id"]: c for c in repo().all("equipment_categories")}
    start = form_dt(request.args.get("start")) or now_iso()
    end = form_dt(request.args.get("end")) or to_iso(parse_dt(start) + timedelta(days=1))
    cat = request.args.get("cat")
    if cat:
        models = [m for m in models if m.get("category_id") == cat]
    for m in models:
        m["avail"] = available(m, start, end)
        m["cap"] = capacity(m)
    models.sort(key=lambda m: (m.get("category_id") or "", m.get("name") or ""))
    return render_template("eqp/catalog.html", models=models, cats=cats, start=start, end=end, cat=cat, rules=eqp_settings())


@bp.route("/image/<mid>")
@login_required
def image(mid):
    m = repo().get("equipment_models", mid) or abort(404)
    if not m.get("image_path"):
        abort(404)
    return Response(load_bytes(m["image_path"]), mimetype=mimetype_for(m["image_path"]),
                    headers={"Cache-Control": "private, max-age=3600"})


# ---------- ทะเบียนอุปกรณ์ (EQP-01, EQP-02) ----------
@bp.route("/manage", methods=["GET", "POST"])
@permission_required("eqp.manage")
def manage():
    if request.method == "POST":
        f = request.form
        mid = f.get("model_id")
        data = {"category_id": f.get("category_id"), "code": f.get("code", "").strip().upper(),
                "name": f.get("name", "").strip(), "description": f.get("description", ""),
                "is_serialized": bool(f.get("is_serialized")), "quantity_total": int(f.get("quantity_total") or 0),
                "replacement_cost": float(f.get("replacement_cost") or 0), "is_active": bool(f.get("is_active")),
                "location": f.get("location", "")}
        dup = [m for m in repo().where("equipment_models", code=data["code"]) if m["id"] != mid]
        if not data["code"] or not data["name"] or dup:
            flash("รหัสต้องไม่ว่างและไม่ซ้ำ และต้องมีชื่ออุปกรณ์", "danger")
            return redirect(url_for("equipment.manage"))
        img = request.files.get("image")
        if img and img.filename:
            try:
                b, ext, ctype = validate_upload(img, images_only=True, allow_pdf=False)
                data["image_path"] = save_bytes("equipment", b, ext, ctype)
            except UploadError as e:
                flash(str(e), "danger")
        if mid:
            old = repo().get("equipment_models", mid)
            repo().update("equipment_models", mid, data)
            audit("eqp.model_update", "equipment_model", mid, old=old, new=data)
        else:
            data["created_at"] = now_iso()
            mid = repo().add("equipment_models", data)
            audit("eqp.model_create", "equipment_model", mid, new=data)
        flash("บันทึกอุปกรณ์แล้ว", "success")
        return redirect(url_for("equipment.model_detail", mid=mid))
    models = sorted(repo().all("equipment_models"), key=lambda m: m.get("code", ""))
    for m in models:
        m["cap"] = capacity(m)
    cats = {c["id"]: c for c in repo().all("equipment_categories")}
    return render_template("eqp/manage.html", models=models, cats=cats)


@bp.route("/model/<mid>", methods=["GET", "POST"])
@permission_required("eqp.manage")
def model_detail(mid):
    m = repo().get("equipment_models", mid) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_unit":
            tag = request.form.get("asset_tag", "").strip().upper() or gen_code(m["code"] + "-", 5)
            if repo().where("equipment_units", asset_tag=tag):
                flash("รหัสครุภัณฑ์ซ้ำ", "danger")
            else:
                uid = repo().add("equipment_units", {"model_id": mid, "asset_tag": tag, "condition": request.form.get("condition", "ดี"),
                                                     "status": "AVAILABLE", "location": request.form.get("location", m.get("location", "")),
                                                     "acquired_at": request.form.get("acquired_at") or None,
                                                     "disposed_at": None, "disposal_reason": None})
                audit("eqp.unit_add", "equipment_unit", uid, new={"tag": tag})
                flash(f"เพิ่มรายชิ้น {tag} แล้ว", "success")
        elif action == "delete_unit":
            un = repo().get("equipment_units", request.form.get("unit_id"))
            if not un or un.get("model_id") != mid:
                abort(404)
            used = any(un["id"] in (it.get("unit_ids") or []) for it in repo().where("equipment_request_items", model_id=mid))
            if un.get("status") == "BORROWED" or used:
                flash("รายชิ้นนี้เคยถูกยืมหรือกำลังถูกยืม ลบไม่ได้ (เปลี่ยนสถานะเป็นตัดจำหน่ายแทน)", "danger")
            else:
                repo().delete("equipment_units", un["id"])
                audit("eqp.unit_delete", "equipment_unit", un["id"], old={"tag": un.get("asset_tag")})
                flash(f"ลบรายชิ้น {un.get('asset_tag')} แล้ว", "info")
        elif action == "delete":
            if repo().where("equipment_request_items", model_id=mid):
                # เคยถูกยืม: เก็บประวัติไว้ ปิดการใช้งานแทนการลบ
                repo().update("equipment_models", mid, {"is_active": False})
                audit("eqp.model_deactivate", "equipment_model", mid)
                flash("อุปกรณ์นี้มีประวัติการยืม จึงปิดการใช้งานแทนการลบ (ไม่แสดงในแคตตาล็อกอีก)", "info")
            else:
                for un in repo().where("equipment_units", model_id=mid):
                    repo().delete("equipment_units", un["id"])
                if m.get("image_path"):
                    delete_file(m["image_path"])
                repo().delete("equipment_models", mid)
                audit("eqp.model_delete", "equipment_model", mid, old={"code": m.get("code"), "name": m.get("name")})
                flash("ลบอุปกรณ์แล้ว", "info")
                return redirect(url_for("equipment.manage"))
        return redirect(url_for("equipment.model_detail", mid=mid))

    units = sorted(repo().where("equipment_units", model_id=mid), key=lambda u: u.get("asset_tag", ""))
    hist = sorted(repo().where("equipment_transactions", model_id=mid), key=lambda t: t.get("occurred_at", ""), reverse=True)[:50]
    cats = repo().all("equipment_categories")
    return render_template("eqp/model.html", mo=m, m_edit=m, units=units, hist=hist, cats=cats, cap=capacity(m))


@bp.route("/settings", methods=["GET", "POST"])
@permission_required("eqp.manage")
def settings():
    """หัวหน้าฝ่ายอุปกรณ์กำหนดระยะเวลายืมสูงสุด อัตราค่าปรับ และหมวดอุปกรณ์"""
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "rules":
            try:
                days = int(f.get("max_loan_days") or 0)
                rate = float(f.get("late_fine_per_item") or 0)
            except ValueError:
                days, rate = 0, -1
            if not 1 <= days <= 365 or not 0 <= rate <= 100000:
                flash("ระยะเวลายืมต้องอยู่ระหว่าง 1-365 วัน และค่าปรับต้องไม่ติดลบ", "danger")
            else:
                old = eqp_settings()
                new = {"max_loan_days": days, "late_fine_per_item": rate, "late_fine_per_day": bool(f.get("late_fine_per_day"))}
                repo().set("system_settings", "equipment", dict(new, updated_by=current_user()["id"], updated_at=now_iso()), merge=True)
                audit("eqp.settings_update", "system_settings", "equipment", old=old, new=new)
                flash("บันทึกการตั้งค่าแล้ว ใช้กับคำขอยืมใหม่ทันที", "success")
        elif action == "payment":
            cur = eqp_settings()
            new = {"pay_name": f.get("pay_name", "").strip()[:120], "pay_note": f.get("pay_note", "").strip()[:200]}
            fs = request.files.get("pay_qr")
            if fs and fs.filename:
                try:
                    blob, ext, ctype = validate_upload(fs, images_only=True, allow_pdf=False)
                except UploadError as e:
                    flash(f"รูป QR: {e}", "danger")
                    return redirect(url_for("equipment.settings"))
                new["pay_qr_path"] = save_bytes("eqp_pay", blob, ext, ctype)
            elif f.get("remove_qr"):
                new["pay_qr_path"] = None
            if "pay_qr_path" in new and cur.get("pay_qr_path"):
                delete_file(cur["pay_qr_path"])
            repo().set("system_settings", "equipment", dict(new, updated_by=current_user()["id"], updated_at=now_iso()), merge=True)
            audit("eqp.payment_settings", "system_settings", "equipment", new={"pay_name": new["pay_name"], "qr_changed": "pay_qr_path" in new})
            flash("บันทึกช่องทางรับชำระแล้ว", "success")
        elif action == "category":
            name = f.get("name", "").strip()[:60]
            cid = f.get("category_id")
            if not name:
                flash("กรุณาระบุชื่อหมวด", "danger")
            elif cid:
                repo().update("equipment_categories", cid, {"name": name})
                flash("แก้ไขหมวดแล้ว", "success")
            else:
                repo().add("equipment_categories", {"name": name, "is_active": True})
                flash("เพิ่มหมวดแล้ว", "success")
            audit("eqp.category_save", "equipment_category", cid, new={"name": name})
        elif action == "delete_category":
            cid = f.get("category_id")
            if repo().where("equipment_models", category_id=cid):
                flash("หมวดนี้มีอุปกรณ์อยู่ ย้ายอุปกรณ์ไปหมวดอื่นก่อนลบ", "danger")
            else:
                repo().delete("equipment_categories", cid)
                audit("eqp.category_delete", "equipment_category", cid)
                flash("ลบหมวดแล้ว", "info")
        else:
            abort(400)
        return redirect(url_for("equipment.settings"))
    cats = sorted(repo().all("equipment_categories"), key=lambda c: c.get("name") or "")
    counts = {c["id"]: len(repo().where("equipment_models", category_id=c["id"])) for c in cats}
    return render_template("eqp/settings.html", s=eqp_settings(), cats=cats, counts=counts)


# ---------- คำขอยืม (EQP-03..06, EQP-08) ----------
@bp.route("/request/new", methods=["GET", "POST"])
@login_required
def request_new():
    u = current_user()
    if not _can_borrow(u):
        abort(403)
    models = sorted([m for m in repo().all("equipment_models") if m.get("is_active", True)], key=lambda m: m.get("name", ""))
    doc_type = required_doc(u)
    if request.method == "POST":
        f = request.form
        blocks = borrow_blocks(u)
        if blocks:
            for b in blocks:
                flash(b, "danger")
            return redirect(url_for("equipment.request_new"))
        start, end = form_dt(f.get("borrow_at")), form_dt(f.get("due_at"))
        if not start or not end or parse_dt(end) <= parse_dt(start):
            flash("วันยืม/วันคืนไม่ถูกต้อง", "danger")
            return redirect(url_for("equipment.request_new"))
        max_days = int(eqp_settings()["max_loan_days"])
        if parse_dt(end) - parse_dt(start) > timedelta(days=max_days):
            flash(f"ยืมได้ไม่เกิน {max_days} วันต่อครั้ง กรุณาเลือกวันคืนใหม่", "danger")
            return redirect(url_for("equipment.request_new", start=f.get("borrow_at"), end=f.get("due_at")))
        lines = []
        for m in models:
            q = int(f.get(f"qty_{m['id']}") or 0)
            if q > 0:
                av = available(m, start, end)
                if q > av:
                    flash(f"{m['name']} คงเหลือในช่วงเวลานี้ {av} ชิ้น", "danger")
                    return redirect(url_for("equipment.request_new"))
                lines.append((m, q))
        if not lines:
            flash("กรุณาระบุจำนวนอย่างน้อย 1 รายการ", "danger")
            return redirect(url_for("equipment.request_new"))
        doc_file = request.files.get("borrower_doc")
        doc_payload = None
        if doc_type:
            try:
                b, ext, ctype = validate_upload(doc_file, images_only=True)
                doc_payload = (b, ext, ctype)
            except UploadError as e:
                flash(f"เอกสารผู้ยืม: {e}", "danger")
                return redirect(url_for("equipment.request_new"))
        if not f.get("consent"):
            flash("กรุณายินยอมการเก็บข้อมูลเพื่อการยืมอุปกรณ์", "danger")
            return redirect(url_for("equipment.request_new"))
        # ผู้ยืมต้องถ่ายรูปอุปกรณ์ที่จะยืมทั้งหมดก่อนส่งคำขอให้หัวหน้าฝ่ายพิจารณา
        photo_payloads = []
        for fs in [x for x in request.files.getlist("photos") if x and x.filename][:MAX_PHOTOS]:
            try:
                photo_payloads.append(validate_upload(fs, images_only=True, allow_pdf=False))
            except UploadError as e:
                flash(f"รูปอุปกรณ์ {fs.filename}: {e}", "danger")
                return redirect(url_for("equipment.request_new", start=f.get("borrow_at"), end=f.get("due_at")))
        if not photo_payloads:
            flash("กรุณาถ่ายรูปอุปกรณ์ที่จะยืมทั้งหมดอย่างน้อย 1 รูปก่อนส่งคำขอ", "danger")
            return redirect(url_for("equipment.request_new", start=f.get("borrow_at"), end=f.get("due_at")))
        is_self = "EQUIP_HEAD" in user_roles(u)
        rid = repo().add("equipment_requests", {
            "tracking_code": gen_code("EQ"), "requester_id": u["id"], "activity_id": None, "event_name": f.get("event_name", "").strip()[:200],
            "purpose": f.get("purpose", "").strip(), "borrow_at": start, "due_at": end, "status": "PENDING",
            "is_self_request": is_self, "approval_request_id": None, "created_at": now_iso(),
        })
        for m, q in lines:
            repo().add("equipment_request_items", {"request_id": rid, "model_id": m["id"], "qty_requested": q,
                                                   "qty_approved": 0, "qty_issued": 0, "qty_returned": 0,
                                                   "qty_lost": 0, "qty_damaged": 0, "unit_ids": []})
        for blob, ext, ctype in photo_payloads:
            repo().add("equipment_photos", {"request_id": rid, "stage": "PICKUP", "file_path": save_bytes("eqp_photos", blob, ext, ctype),
                                            "note": f.get("photo_note", "").strip()[:200], "uploaded_by": u["id"],
                                            "created_at": now_iso()})
        if doc_payload:
            path = save_bytes("borrower_docs", *doc_payload, encrypt=True)
            repo().add("borrower_documents", {"request_id": rid, "user_id": u["id"], "doc_type": doc_type, "file_path": path,
                                              "uploaded_at": now_iso(), "delete_after": None})
        repo().add("consents", {"user_id": u["id"], "purpose": "EQUIPMENT_BORROW", "ref": rid, "granted_at": now_iso(),
                                "expires_at": None})
        if is_self:
            # ยืมเอง -> ADMIN_VP อนุมัติแทน (EQP-08)
            req_id = create_approval("EQP_SELF", "equipment_request", rid, "คำขอยืมอุปกรณ์ของหัวหน้าฝ่ายอุปกรณ์",
                                     url_for("equipment.request_detail", rid=rid))
            repo().update("equipment_requests", rid, {"approval_request_id": req_id})
        else:
            eqp_notify(list(holders_of_role("EQUIP_HEAD")), "มีคำขอยืมอุปกรณ์ใหม่", display_name(u), url_for("equipment.request_detail", rid=rid))
        audit("eqp.request_create", "equipment_request", rid, new={"self": is_self})
        flash("ส่งคำขอยืมแล้ว", "success")
        return redirect(url_for("equipment.request_detail", rid=rid))
    start = form_dt(request.args.get("start")) or now_iso()
    end = form_dt(request.args.get("end")) or to_iso(parse_dt(start) + timedelta(days=1))
    for m in models:
        m["avail"] = available(m, start, end)
    return render_template("eqp/request_form.html", models=models, doc_type=doc_type, blocks=borrow_blocks(u), rules=eqp_settings(),
                           max_photos=MAX_PHOTOS,
                           too_long=parse_dt(end) - parse_dt(start) > timedelta(days=int(eqp_settings()["max_loan_days"])),
                           start=start, end=end, preselect=request.args.get("model"))


def _filter_requests(args, user):
    if can("eqp.manage") or can("eqp.report"):
        reqs = repo().all("equipment_requests")
    elif can("eqp.self_approve"):
        reqs = [r for r in repo().all("equipment_requests") if r.get("is_self_request") or r["requester_id"] == user["id"]]
    else:
        reqs = repo().where("equipment_requests", requester_id=user["id"])
    st = args.get("status")
    if st == "OVERDUE":
        reqs = [r for r in reqs if is_overdue(r)]
    elif st:
        reqs = [r for r in reqs if r.get("status") == st]
    if args.get("mine"):
        reqs = [r for r in reqs if r["requester_id"] == user["id"]]
    return sorted(reqs, key=lambda r: r.get("created_at", ""), reverse=True)


@bp.route("/requests")
@login_required
def requests_list():
    u = current_user()
    reqs = _filter_requests(request.args, u)
    users = {r["requester_id"]: repo().get("users", r["requester_id"]) or {} for r in reqs}
    for r in reqs:
        r["overdue"] = is_overdue(r)
    return render_template("eqp/requests.html", reqs=reqs, users=users, status=request.args.get("status"))


def _can_view_request(r, u):
    return r["requester_id"] == u["id"] or can("eqp.manage") or can("eqp.report") or \
        (r.get("is_self_request") and can("eqp.self_approve"))


@bp.route("/request/<rid>")
@login_required
def request_detail(rid):
    r = _req(rid)
    u = current_user()
    if not _can_view_request(r, u):
        abort(403)
    items = items_of(rid)
    models = {m["id"]: m for m in repo().all("equipment_models")}
    units = {}
    for it in items:
        for un in repo().where("equipment_units", model_id=it["model_id"]):
            units[un["id"]] = un
    for it in items:
        it["avail"] = available(models[it["model_id"]], r["borrow_at"], r["due_at"], exclude_request=rid) if it["model_id"] in models else 0
        it["outstanding"] = outstanding(it)
    docs = repo().where("borrower_documents", request_id=rid)
    fines = repo().where("equipment_fines", request_id=rid)
    txs = sorted(repo().where("equipment_transactions", request_id=rid), key=lambda t: t.get("occurred_at", ""))
    req_state = approval_state(repo().get("approval_requests", r.get("approval_request_id"))) if r.get("approval_request_id") else None
    requester = repo().get("users", r["requester_id"])
    manage = can("eqp.manage")
    return render_template("eqp/request_detail.html", r=r, items=items, models=models, units=units, docs=docs, fines=fines,
                           txs=txs, req_state=req_state, requester=requester, manage=manage,
                           self_approver=can("eqp.self_approve") and r.get("is_self_request"),
                           overdue=is_overdue(r), event_name=r.get("event_name") or (repo().get("activities", r.get("activity_id")) or {}).get("title"),
                           show_contact=manage or r["requester_id"] == u["id"], rules=eqp_settings(),
                           is_requester=r["requester_id"] == u["id"], photos=photos_of(rid), stages=PHOTO_STAGES,
                           need_pickup=r.get("status") in ("PENDING", "APPROVED") and needs_pickup_photo(r),
                           need_return=r.get("status") in OUT_REQ and needs_return_photo(r), max_photos=MAX_PHOTOS,
                           feedback=sorted(repo().where("equipment_feedback", request_id=rid), key=lambda x: x.get("created_at") or ""), late=late_days(r) if r.get("status") in OUT_REQ else 0,
                           default_fine=sum(int(it.get("qty_lost") or 0) * float(models.get(it["model_id"], {}).get("replacement_cost") or 0)
                                            for it in items))


def _tx(r, item, typ, qty, unit_id=None, condition=None, note=None, id_checked=None):
    return repo().add("equipment_transactions", {
        "request_id": r["id"], "request_item_id": item["id"] if item else None, "model_id": item["model_id"] if item else None,
        "unit_id": unit_id, "type": typ, "qty": qty, "condition_after": condition, "id_checked": id_checked,
        "actor_id": current_user()["id"], "occurred_at": now_iso(), "note": note,
    })


@bp.route("/request/<rid>/action", methods=["POST"])
@login_required
def request_action(rid):
    r = _req(rid)
    u = current_user()
    action = request.form.get("action")
    items = items_of(rid)
    manage = can("eqp.manage")

    link = url_for("equipment.request_detail", rid=rid)
    code = r.get("tracking_code")
    heads = list(holders_of_role("EQUIP_HEAD"))

    if action == "cancel":
        if r["requester_id"] != u["id"] or r["status"] not in ("PENDING", "APPROVED"):
            abort(403)
        repo().update("equipment_requests", rid, {"status": "CANCELLED"})
        audit("eqp.request_cancel", "equipment_request", rid)
        eqp_notify(heads, f"ผู้ยืมยกเลิกคำขอ {code}", "", link)

    elif action == "photo":
        # ผู้ยืมถ่ายรูปอุปกรณ์ก่อนรับของ และทุกครั้งที่นำมาคืน
        stage = request.form.get("stage")
        if r["requester_id"] != u["id"] or stage not in PHOTO_STAGES:
            abort(403)
        if (stage == "PICKUP" and r["status"] not in ("PENDING", "APPROVED")) or (stage == "RETURN" and r["status"] not in OUT_REQ):
            flash("ยังไม่ถึงขั้นตอนที่ต้องถ่ายรูปนี้", "danger")
            return redirect(link)
        files = [x for x in request.files.getlist("photos") if x and x.filename][:MAX_PHOTOS]
        saved = 0
        for fs in files:
            try:
                blob, ext, ctype = validate_upload(fs, images_only=True, allow_pdf=False)
            except UploadError as e:
                flash(f"{fs.filename}: {e}", "danger")
                continue
            repo().add("equipment_photos", {"request_id": rid, "stage": stage, "file_path": save_bytes("eqp_photos", blob, ext, ctype),
                                            "note": request.form.get("note", "").strip()[:200], "uploaded_by": u["id"],
                                            "created_at": now_iso()})
            saved += 1
        if not saved:
            flash("กรุณาถ่ายหรือเลือกรูปอย่างน้อย 1 รูป", "danger")
            return redirect(link)
        audit("eqp.photo_upload", "equipment_request", rid, new={"stage": stage, "count": saved})
        eqp_notify(heads, f"ผู้ยืมส่งรูป{PHOTO_STAGES[stage]}แล้ว ({code})", "", link)
        flash(f"บันทึกรูป{PHOTO_STAGES[stage]} {saved} รูปแล้ว " + ("" if stage == "PICKUP" else "นำอุปกรณ์ไปคืนที่ฝ่ายอุปกรณ์ได้เลย"), "success")

    elif action == "feedback":
        # หัวหน้าฝ่ายอุปกรณ์ส่งข้อความถึงผู้ยืม แนบ QR รับเงินได้
        if not manage:
            abort(403)
        msg = request.form.get("message", "").strip()[:1000]
        if not msg:
            flash("กรุณาพิมพ์ข้อความ", "danger")
            return redirect(link)
        with_qr = bool(request.form.get("with_qr")) and bool(eqp_settings().get("pay_qr_path"))
        fb = repo().add("equipment_feedback", {"request_id": rid, "message": msg, "with_qr": with_qr, "by": u["id"], "created_at": now_iso()})
        audit("eqp.feedback", "equipment_feedback", fb)
        eqp_notify(r["requester_id"], f"มีข้อความจากฝ่ายอุปกรณ์ ({code})", "", link)

    elif action == "slip":
        # ผู้ยืมแนบหลักฐานการโอนค่าปรับ/ค่าชดใช้ รอหัวหน้าฝ่ายอุปกรณ์ตรวจ
        fine = repo().get("equipment_fines", request.form.get("fine_id"))
        if r["requester_id"] != u["id"] or not fine or fine.get("request_id") != rid or fine.get("status") != "PENDING":
            abort(403)
        try:
            blob, ext, ctype = validate_upload(request.files.get("slip"), images_only=True, allow_pdf=False)
        except UploadError as e:
            flash(f"หลักฐานการโอน: {e}", "danger")
            return redirect(link)
        if fine.get("slip_path"):
            delete_file(fine["slip_path"])
        repo().update("equipment_fines", fine["id"], {"slip_path": save_bytes("eqp_slips", blob, ext, ctype, encrypt=True),
                                                      "slip_at": now_iso(), "slip_status": "SUBMITTED", "slip_reject_reason": None})
        audit("eqp.slip_upload", "equipment_fine", fine["id"])
        eqp_notify(heads, f"ผู้ยืมแจ้งชำระเงินแล้ว รอตรวจสอบ ({code})", f"{float(fine.get('amount') or 0):,.2f} บาท", link)
        flash("ส่งหลักฐานการโอนแล้ว รอฝ่ายอุปกรณ์ตรวจสอบ", "success")

    elif action in ("approve", "reject"):
        if r["status"] != "PENDING":
            abort(400)
        if action == "approve" and needs_pickup_photo(r):
            flash("ยังอนุมัติไม่ได้: ผู้ยืมต้องถ่ายรูปอุปกรณ์ที่จะยืมก่อน", "danger")
            return redirect(link)
        reason = request.form.get("reason", "").strip()
        if r.get("is_self_request"):
            # EQUIP_HEAD อนุมัติคำขอของตัวเองไม่ได้ ต้องเป็น ADMIN_VP
            if not can("eqp.self_approve"):
                abort(403)
            req = repo().get("approval_requests", r["approval_request_id"])
            ok, msg = decide(req, "APPROVE" if action == "approve" else "REJECT", reason)
            if not ok:
                flash(msg, "danger")
                return redirect(url_for("equipment.request_detail", rid=rid))
        elif not manage:
            abort(403)
        if action == "reject":
            if not reason:
                flash("การปฏิเสธต้องระบุเหตุผล", "danger")
                return redirect(url_for("equipment.request_detail", rid=rid))
            repo().update("equipment_requests", rid, {"status": "REJECTED", "reject_reason": reason, "decided_by": u["id"],
                                                      "decided_at": now_iso()})
            eqp_notify(r["requester_id"], "คำขอยืมถูกปฏิเสธ", reason, url_for("equipment.request_detail", rid=rid))
        else:
            models = {m["id"]: m for m in repo().all("equipment_models")}
            for it in items:
                q = int(request.form.get(f"approve_{it['id']}", it["qty_requested"]) or 0)
                q = max(0, min(q, it["qty_requested"]))
                av = available(models[it["model_id"]], r["borrow_at"], r["due_at"], exclude_request=rid)
                if q > av:
                    flash(f"{models[it['model_id']]['name']} คงเหลือเพียง {av}", "danger")
                    return redirect(url_for("equipment.request_detail", rid=rid))
                repo().update("equipment_request_items", it["id"], {"qty_approved": q})
            repo().update("equipment_requests", rid, {"status": "APPROVED", "decided_by": u["id"], "decided_at": now_iso()})
            eqp_notify(r["requester_id"], "คำขอยืมได้รับอนุมัติ", f"รับอุปกรณ์ได้ {thai_date(r['borrow_at'])}",
                   url_for("equipment.request_detail", rid=rid))
        audit(f"eqp.request_{action}", "equipment_request", rid, new={"reason": reason})

    elif action == "issue":
        # EQP-09 จ่ายอุปกรณ์ บันทึกผู้รับ สแกน/เลือกรายชิ้น ตรวจบัตร
        if not manage or r["status"] != "APPROVED":
            abort(403)
        if needs_pickup_photo(r):
            flash("ยังจ่ายอุปกรณ์ไม่ได้: ผู้ยืมต้องถ่ายรูปอุปกรณ์ก่อนยืมในระบบก่อน", "danger")
            return redirect(link)
        id_checked = bool(request.form.get("id_checked"))
        for it in items:
            m = repo().get("equipment_models", it["model_id"])
            if m.get("is_serialized"):
                tags = [t.strip().upper() for t in request.form.get(f"units_{it['id']}", "").replace(",", "\n").split() if t.strip()]
                if len(tags) != it["qty_approved"]:
                    flash(f"{m['name']}: ต้องระบุรหัสครุภัณฑ์ {it['qty_approved']} รายการ", "danger")
                    return redirect(url_for("equipment.request_detail", rid=rid))
                unit_ids = []
                for tag in tags:
                    un = next(iter(repo().where("equipment_units", asset_tag=tag, model_id=m["id"])), None)
                    if not un or un.get("status") != "AVAILABLE":
                        flash(f"รายชิ้น {tag} ไม่พร้อมจ่าย", "danger")
                        return redirect(url_for("equipment.request_detail", rid=rid))
                    unit_ids.append(un["id"])
                for uid_ in unit_ids:
                    repo().update("equipment_units", uid_, {"status": "BORROWED"})
                    _tx(r, it, "ISSUE", 1, unit_id=uid_, id_checked=id_checked)
                repo().update("equipment_request_items", it["id"], {"qty_issued": len(unit_ids), "unit_ids": unit_ids})
            else:
                q = it["qty_approved"]
                if q:
                    _tx(r, it, "ISSUE", q, id_checked=id_checked)
                repo().update("equipment_request_items", it["id"], {"qty_issued": q})
        repo().update("equipment_requests", rid, {"status": "ISSUED", "issued_by": u["id"], "issued_at": now_iso(),
                                                  "receiver_name": request.form.get("receiver_name", "").strip(),
                                                  "id_checked": id_checked})
        audit("eqp.issue", "equipment_request", rid)
        eqp_notify(r["requester_id"], f"รับอุปกรณ์แล้ว ({code})", f"กำหนดคืน {thai_date(r['due_at'])}", link)

    elif action == "return":
        # EQP-10 รับคืน (รองรับคืนบางส่วน) บันทึกสภาพ ชำรุด สูญหาย
        if not manage or r["status"] not in OUT_REQ:
            abort(403)
        if needs_return_photo(r):
            flash("ยังรับคืนไม่ได้: ผู้ยืมต้องถ่ายรูปอุปกรณ์ตอนคืนในระบบก่อน (ต้องถ่ายใหม่ทุกครั้งที่คืน)", "danger")
            return redirect(link)
        before = {it["id"]: outstanding(it) for it in items}
        for it in items:
            m = repo().get("equipment_models", it["model_id"])
            if m.get("is_serialized"):
                for uid_ in it.get("unit_ids") or []:
                    un = repo().get("equipment_units", uid_)
                    if not un or un.get("status") != "BORROWED":
                        continue
                    outcome = request.form.get(f"unit_{uid_}")
                    cond = request.form.get(f"cond_{uid_}", "")
                    if outcome == "RETURN":
                        repo().update("equipment_units", uid_, {"status": "AVAILABLE", "condition": cond or un.get("condition")})
                        _tx(r, it, "RETURN", 1, unit_id=uid_, condition=cond)
                        it["qty_returned"] = int(it.get("qty_returned") or 0) + 1
                    elif outcome == "DAMAGED":
                        repo().update("equipment_units", uid_, {"status": "REPAIR", "condition": cond or "ชำรุด"})
                        _tx(r, it, "DAMAGED", 1, unit_id=uid_, condition=cond)
                        repo().add("equipment_maintenance", {"model_id": m["id"], "unit_id": uid_, "qty": 1,
                                                             "issue": cond or "ชำรุดหลังยืม", "status": "IN_PROGRESS", "cost": 0,
                                                             "reported_at": now_iso(), "recorded_by": u["id"], "request_id": rid})
                        it["qty_damaged"] = int(it.get("qty_damaged") or 0) + 1
                    elif outcome == "LOST":
                        repo().update("equipment_units", uid_, {"status": "LOST"})
                        _tx(r, it, "LOST", 1, unit_id=uid_, condition=cond)
                        it["qty_lost"] = int(it.get("qty_lost") or 0) + 1
            else:
                left = outstanding(it)
                ret = min(left, int(request.form.get(f"ret_{it['id']}") or 0))
                dmg = min(left - ret, int(request.form.get(f"dmg_{it['id']}") or 0))
                lost = min(left - ret - dmg, int(request.form.get(f"lost_{it['id']}") or 0))
                cond = request.form.get(f"cond_{it['id']}", "")
                if ret:
                    _tx(r, it, "RETURN", ret, condition=cond)
                if dmg:
                    _tx(r, it, "DAMAGED", dmg, condition=cond)
                    repo().add("equipment_maintenance", {"model_id": m["id"], "unit_id": None, "qty": dmg,
                                                         "issue": cond or "ชำรุดหลังยืม", "status": "IN_PROGRESS", "cost": 0,
                                                         "reported_at": now_iso(), "recorded_by": u["id"], "request_id": rid})
                if lost:
                    _tx(r, it, "LOST", lost, condition=cond)
                    repo().update("equipment_models", m["id"], {"quantity_total": max(0, int(m.get("quantity_total") or 0) - lost)})
                it["qty_returned"] = int(it.get("qty_returned") or 0) + ret
                it["qty_damaged"] = int(it.get("qty_damaged") or 0) + dmg
                it["qty_lost"] = int(it.get("qty_lost") or 0) + lost
            repo().update("equipment_request_items", it["id"], {k: it.get(k, 0) for k in ("qty_returned", "qty_damaged", "qty_lost")})
        items = items_of(rid)
        done = all(outstanding(it) == 0 for it in items)
        repo().update("equipment_requests", rid, {"status": "RETURNED" if done else "PARTIAL_RETURNED", "last_return_at": now_iso()})
        for ph in photos_of(rid, "RETURN"):  # รูปชุดนี้ใช้กับการรับคืนครั้งนี้แล้ว ครั้งถัดไปต้องถ่ายใหม่
            if not ph.get("used"):
                repo().update("equipment_photos", ph["id"], {"used": True})
        eqp_notify(r["requester_id"], f"ฝ่ายอุปกรณ์บันทึกรับคืน{'ครบแล้ว' if done else 'บางส่วน'} ({code})", "", link)
        # ค่าปรับคืนล่าช้า: คิดตามจำนวนรายการที่รับคืนในครั้งนี้หลังเลยกำหนด
        lines = sum(1 for it in items if outstanding(it) < before.get(it["id"], 0))
        amount, days = late_fine_amount(r, lines)
        if amount > 0:
            per_day = eqp_settings()["late_fine_per_day"]
            note = (f"คืนล่าช้า {days} วัน · {lines} รายการ × {eqp_settings()['late_fine_per_item']:,.2f} บาท"
                    + (f" × {days} วัน" if per_day else "") + " (คำนวณอัตโนมัติ)")
            fid = repo().add("equipment_fines", {"request_id": rid, "user_id": r["requester_id"], "type": "LATE", "amount": amount,
                                                 "status": "PENDING", "recorded_by": u["id"], "created_at": now_iso(),
                                                 "settled_at": None, "note": note, "auto": True})
            eqp_notify(r["requester_id"], "มีค่าปรับคืนอุปกรณ์ล่าช้า", f"{amount:,.2f} บาท ชำระหน้างานที่ฝ่ายอุปกรณ์",
                   url_for("equipment.request_detail", rid=rid))
            audit("eqp.fine_auto_late", "equipment_fine", fid, new={"amount": amount, "days": days, "lines": lines})
            flash(f"คืนล่าช้า {days} วัน ระบบบันทึกค่าปรับ {amount:,.2f} บาทให้แล้ว (แก้ไขหรือยกเว้นได้ที่ส่วนค่าชดใช้)", "warning")
        if done and r.get("is_self_request"):
            eqp_notify(list(holders_of_role("ADMIN_VP")), "รอยืนยันรับคืน (หัวหน้าฝ่ายอุปกรณ์ยืมเอง)", "", url_for("equipment.request_detail", rid=rid))
        audit("eqp.return", "equipment_request", rid)
        if any(int(it.get("qty_lost") or 0) or int(it.get("qty_damaged") or 0) for it in items):
            flash("มีรายการชำรุด/สูญหาย กรุณาบันทึกค่าชดใช้ (หรือยกเว้น) ก่อนปิดคำขอ", "warning")

    elif action == "confirm_return":
        # EQP-08 ADMIN_VP ยืนยันรับคืนกรณียืมเอง (ไม่เปิดสิทธิ์จัดการอุปกรณ์ให้)
        if not (r.get("is_self_request") and can("eqp.self_approve")) or r["status"] != "RETURNED":
            abort(403)
        repo().update("equipment_requests", rid, {"return_confirmed_by": u["id"], "return_confirmed_at": now_iso()})
        audit("eqp.confirm_return", "equipment_request", rid)

    elif action == "fine":
        if not manage:
            abort(403)
        typ = request.form.get("type")
        amount = float(request.form.get("amount") or 0)
        if typ not in ("LATE", "DAMAGE", "LOSS") or amount < 0:
            abort(400)
        fid = repo().add("equipment_fines", {"request_id": rid, "user_id": r["requester_id"], "type": typ, "amount": amount,
                                             "status": "PENDING", "recorded_by": u["id"], "created_at": now_iso(),
                                             "settled_at": None, "note": request.form.get("note", "")})
        eqp_notify(r["requester_id"], "มีค่าชดใช้อุปกรณ์", f"{amount:,.2f} บาท ชำระหน้างานที่ฝ่ายอุปกรณ์",
               url_for("equipment.request_detail", rid=rid))
        audit("eqp.fine_add", "equipment_fine", fid, new={"amount": amount, "type": typ})

    elif action == "close":
        if not manage:
            abort(403)
        items = items_of(rid)
        if r["status"] not in ("RETURNED",) or any(outstanding(it) for it in items):
            flash("ปิดได้เมื่อทุกรายการคืน/สูญหาย/ชำรุดครบตามที่จ่าย", "danger")
        elif repo().where("equipment_fines", request_id=rid, status="PENDING"):
            flash("ยังมีค่าชดใช้ค้าง", "danger")
        elif r.get("is_self_request") and not r.get("return_confirmed_at"):
            flash("รอ อุปนายกฝ่ายบริหารฯ ยืนยันรับคืนก่อน", "danger")
        elif any(int(it.get("qty_lost") or 0) or int(it.get("qty_damaged") or 0) for it in items) \
                and not repo().where("equipment_fines", request_id=rid):
            flash("มีรายการชำรุด/สูญหาย ต้องบันทึกค่าชดใช้หรือยกเว้นก่อน", "danger")
        else:
            days = int(setting("borrower_doc_retention_days") or 30)
            repo().update("equipment_requests", rid, {"status": "CLOSED", "closed_at": now_iso()})
            for d in repo().where("borrower_documents", request_id=rid):
                repo().update("borrower_documents", d["id"], {"delete_after": to_iso(now() + timedelta(days=days))})
            audit("eqp.close", "equipment_request", rid)
            eqp_notify(r["requester_id"], f"ปิดรายการยืมแล้ว ({code})", "", link)
            flash("ปิดคำขอแล้ว", "success")
    else:
        abort(400)
    return redirect(url_for("equipment.request_detail", rid=rid))


@bp.route("/fine/<fid>", methods=["POST"])
@permission_required("eqp.manage")
def settle_fine(fid):
    """EQP-11 ชำระหน้างาน หรือยกเว้นพร้อมเหตุผล"""
    f = repo().get("equipment_fines", fid) or abort(404)
    st = request.form.get("status")
    note = request.form.get("note", "").strip()
    link = url_for("equipment.request_detail", rid=f["request_id"])
    amount = f"{float(f.get('amount') or 0):,.2f} บาท"
    if st == "SLIP_REJECT":
        if not f.get("slip_path") or not note:
            flash("การตีกลับหลักฐานต้องระบุเหตุผล", "danger")
        else:
            delete_file(f["slip_path"])
            repo().update("equipment_fines", fid, {"slip_path": None, "slip_status": "REJECTED", "slip_reject_reason": note})
            audit("eqp.slip_reject", "equipment_fine", fid, new={"reason": note})
            eqp_notify(f["user_id"], "หลักฐานการโอนไม่ผ่าน กรุณาส่งใหม่", note, link)
    elif st not in ("PAID", "WAIVED") or (st == "WAIVED" and not note):
        flash("การยกเว้นต้องระบุเหตุผล", "danger")
    else:
        repo().update("equipment_fines", fid, {"status": st, "settled_at": form_dt(request.form.get("settled_at")) or now_iso(),
                                               "slip_status": "CONFIRMED" if st == "PAID" and f.get("slip_path") else f.get("slip_status"),
                                               "note": (f.get("note") or "") + (" | " + note if note else "")})
        audit("eqp.fine_settle", "equipment_fine", fid, new={"status": st, "note": note})
        eqp_notify(f["user_id"], "ยืนยันรับชำระแล้ว" if st == "PAID" else "ยกเว้นค่าปรับ/ค่าชดใช้ให้แล้ว", amount, link)
    return redirect(request.referrer or url_for("equipment.fines"))


@bp.route("/photo/<pid>")
@login_required
def photo(pid):
    ph = repo().get("equipment_photos", pid) or abort(404)
    if not _can_view_request(_req(ph["request_id"]), current_user()):
        abort(403)
    return Response(load_bytes(ph["file_path"]), mimetype=mimetype_for(ph["file_path"]),
                    headers={"Cache-Control": "private, max-age=3600"})


@bp.route("/slip/<fid>")
@login_required
def slip(fid):
    f = repo().get("equipment_fines", fid) or abort(404)
    if not f.get("slip_path") or not (f.get("user_id") == current_user()["id"] or can("eqp.manage")):
        abort(403)
    audit("eqp.slip_view", "equipment_fine", fid)
    return Response(load_bytes(f["slip_path"]), mimetype=mimetype_for(f["slip_path"]), headers={"Cache-Control": "no-store"})


@bp.route("/pay-qr")
@login_required
def pay_qr():
    path = eqp_settings().get("pay_qr_path")
    if not path:
        abort(404)
    return Response(load_bytes(path), mimetype=mimetype_for(path), headers={"Cache-Control": "private, max-age=600"})


@bp.route("/fines")
@login_required
def fines():
    if not (can("eqp.manage") or can("eqp.report")):
        abort(403)
    items = sorted(repo().all("equipment_fines"), key=lambda f: f.get("created_at", ""), reverse=True)
    st = request.args.get("status")
    if st:
        items = [f for f in items if f.get("status") == st]
    users = {f["user_id"]: repo().get("users", f["user_id"]) or {} for f in items}
    return render_template("eqp/fines.html", items=items, users=users, status=st)


@bp.route("/doc/<did>")
@permission_required("eqp.manage")
def borrower_doc(did):
    """EQP-16 เปิดดูเอกสารผู้ยืมได้เฉพาะ EQUIP_HEAD บันทึกทุกครั้ง"""
    d = repo().get("borrower_documents", did) or abort(404)
    audit("eqp.borrower_doc_view", "borrower_document", did, note=d.get("request_id"))
    data = load_bytes(d["file_path"])
    return Response(data, mimetype=mimetype_for(d["file_path"]),
                    headers={"Cache-Control": "no-store", "Content-Disposition": "inline"})


# ---------- ซ่อมบำรุง/ตัดจำหน่าย (EQP-12) ----------
@bp.route("/maintenance", methods=["GET", "POST"])
@permission_required("eqp.manage")
def maintenance():
    u = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            mid = request.form.get("model_id")
            tag = request.form.get("asset_tag", "").strip().upper()
            unit = next(iter(repo().where("equipment_units", asset_tag=tag)), None) if tag else None
            if unit and unit.get("status") != "AVAILABLE":
                flash("รายชิ้นนี้ไม่อยู่ในสถานะว่าง", "danger")
                return redirect(url_for("equipment.maintenance"))
            if unit:
                repo().update("equipment_units", unit["id"], {"status": "REPAIR"})
                mid = unit["model_id"]
            repo().add("equipment_maintenance", {"model_id": mid, "unit_id": unit["id"] if unit else None,
                                                 "qty": 1 if unit else int(request.form.get("qty") or 1),
                                                 "issue": request.form.get("issue", ""), "status": "IN_PROGRESS", "cost": 0,
                                                 "reported_at": now_iso(), "recorded_by": u["id"]})
            flash("บันทึกส่งซ่อมแล้ว", "success")
        elif action in ("complete", "dispose"):
            m = repo().get("equipment_maintenance", request.form.get("mid")) or abort(404)
            reason = request.form.get("reason", "").strip()
            if action == "dispose" and not reason:
                flash("ตัดจำหน่ายต้องระบุเหตุผล", "danger")
                return redirect(url_for("equipment.maintenance"))
            repo().update("equipment_maintenance", m["id"], {"status": "COMPLETED" if action == "complete" else "DISPOSED",
                                                             "cost": float(request.form.get("cost") or 0),
                                                             "completed_at": now_iso(), "note": reason})
            if m.get("unit_id"):
                if action == "complete":
                    repo().update("equipment_units", m["unit_id"], {"status": "AVAILABLE"})
                else:
                    repo().update("equipment_units", m["unit_id"], {"status": "DISPOSED", "disposed_at": now_iso()[:10],
                                                                    "disposal_reason": reason})
            elif action == "dispose":
                model = repo().get("equipment_models", m["model_id"])
                repo().update("equipment_models", model["id"],
                              {"quantity_total": max(0, int(model.get("quantity_total") or 0) - int(m.get("qty") or 0))})
            audit(f"eqp.maintenance_{action}", "equipment_maintenance", m["id"], new={"reason": reason})
            flash("บันทึกแล้ว", "success")
        return redirect(url_for("equipment.maintenance"))
    items = sorted(repo().all("equipment_maintenance"), key=lambda m: m.get("reported_at", ""), reverse=True)
    models = {m["id"]: m for m in repo().all("equipment_models")}
    units = {un["id"]: un for un in repo().all("equipment_units")}
    return render_template("eqp/maintenance.html", items=items, models=models, units=units)


# ---------- ส่งออก (EQP-17) ----------
@dataset("equipment_models")
def ds_models(args):
    if not (can("eqp.manage") or can("eqp.report")):
        abort(403)
    cats = {c["id"]: c for c in repo().all("equipment_categories")}
    rows = []
    for m in sorted(repo().all("equipment_models"), key=lambda m: m.get("code", "")):
        rows.append([m.get("code"), m.get("name"), (cats.get(m.get("category_id")) or {}).get("name"),
                     "รายชิ้น" if m.get("is_serialized") else "นับจำนวน", capacity(m), m.get("replacement_cost"),
                     m.get("location"), m.get("is_active", True)])
    return "equipment", ["รหัส", "ชื่อ", "หมวด", "ประเภท", "จำนวนใช้งานได้", "ราคาทดแทน", "ที่เก็บ", "ใช้งาน"], rows


@dataset("equipment_requests")
def ds_requests(args):
    u = current_user()
    if not (can("eqp.manage") or can("eqp.report")):
        args = dict(args, mine="1")
    models = {m["id"]: m for m in repo().all("equipment_models")}
    rows = []
    for r in _filter_requests(args, u):
        req_user = repo().get("users", r["requester_id"]) or {}
        for it in items_of(r["id"]):
            rows.append([r.get("tracking_code"), req_user.get("student_code"), display_name(req_user),
                         req_user.get("account_type"), (models.get(it["model_id"]) or {}).get("name"), it.get("qty_requested"),
                         it.get("qty_approved"), it.get("qty_issued"), it.get("qty_returned"), it.get("qty_damaged"),
                         it.get("qty_lost"), thai_date(r.get("borrow_at")), thai_date(r.get("due_at")), r.get("status"),
                         "เกินกำหนด" if is_overdue(r) else "", "ผู้ขอคือหัวหน้าฝ่าย" if r.get("is_self_request") else ""])
    return "equipment-requests", ["รหัสคำขอ", "รหัสนักศึกษา", "ผู้ยืม", "ประเภทผู้ใช้", "อุปกรณ์", "ขอ", "อนุมัติ", "จ่าย",
                                  "คืน", "ชำรุด", "สูญหาย", "วันยืม", "กำหนดคืน", "สถานะ", "เกินกำหนด", "แท็ก"], rows


@dataset("equipment_fines")
def ds_fines(args):
    if not (can("eqp.manage") or can("eqp.report")):
        abort(403)
    rows = []
    for f in repo().all("equipment_fines"):
        u = repo().get("users", f["user_id"]) or {}
        r = repo().get("equipment_requests", f["request_id"]) or {}
        rows.append([r.get("tracking_code"), u.get("student_code"), display_name(u), f.get("type"), f.get("amount"),
                     f.get("status"), thai_date(f.get("settled_at")), f.get("note")])
    return "equipment-fines", ["รหัสคำขอ", "รหัสนักศึกษา", "ผู้ยืม", "ประเภท", "จำนวนเงิน", "สถานะ", "วันที่ชำระ", "หมายเหตุ"], rows


def daily_equipment_jobs():
    """งานรายวัน: แจ้งเตือนใกล้ครบ/เกินกำหนด และลบเอกสารผู้ยืมที่ครบอายุ"""
    t = now()
    heads = holders_of_role("EQUIP_HEAD")
    for r in repo().all("equipment_requests"):
        due = parse_dt(r.get("due_at"))
        if r.get("status") in OUT_REQ and due:
            if due < t:
                notify([r["requester_id"], *heads], "อุปกรณ์เกินกำหนดคืน", r.get("tracking_code"),
                       url_for("equipment.request_detail", rid=r["id"]), event="EQP_DUE")
            elif due - t < timedelta(days=1):
                notify(r["requester_id"], "ใกล้ครบกำหนดคืนอุปกรณ์", thai_date(r.get("due_at")),
                       url_for("equipment.request_detail", rid=r["id"]), event="EQP_DUE")
    removed = 0
    for d in repo().all("borrower_documents"):
        da = parse_dt(d.get("delete_after"))
        if da and da < t and not repo().where("equipment_fines", request_id=d["request_id"], status="PENDING"):
            delete_file(d["file_path"])
            repo().delete("borrower_documents", d["id"])
            removed += 1
    return removed
