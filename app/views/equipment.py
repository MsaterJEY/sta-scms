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
    return render_template("eqp/catalog.html", models=models, cats=cats, start=start, end=end, cat=cat)


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
        elif action == "delete" and not repo().where("equipment_request_items", model_id=mid):
            repo().update("equipment_models", mid, {"is_active": False})
            audit("eqp.model_deactivate", "equipment_model", mid)
            flash("ปิดการใช้งานอุปกรณ์แล้ว", "info")
        return redirect(url_for("equipment.model_detail", mid=mid))
    units = sorted(repo().where("equipment_units", model_id=mid), key=lambda u: u.get("asset_tag", ""))
    hist = sorted(repo().where("equipment_transactions", model_id=mid), key=lambda t: t.get("occurred_at", ""), reverse=True)[:50]
    cats = repo().all("equipment_categories")
    return render_template("eqp/model.html", mo=m, m_edit=m, units=units, hist=hist, cats=cats, cap=capacity(m))


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
            notify_role("EQUIP_HEAD", "มีคำขอยืมอุปกรณ์ใหม่", display_name(u), url_for("equipment.request_detail", rid=rid))
        audit("eqp.request_create", "equipment_request", rid, new={"self": is_self})
        flash("ส่งคำขอยืมแล้ว", "success")
        return redirect(url_for("equipment.request_detail", rid=rid))
    start = form_dt(request.args.get("start")) or now_iso()
    end = form_dt(request.args.get("end")) or to_iso(parse_dt(start) + timedelta(days=1))
    for m in models:
        m["avail"] = available(m, start, end)
    return render_template("eqp/request_form.html", models=models, doc_type=doc_type, blocks=borrow_blocks(u),
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
                           show_contact=manage or r["requester_id"] == u["id"],
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

    if action == "cancel":
        if r["requester_id"] != u["id"] or r["status"] not in ("PENDING", "APPROVED"):
            abort(403)
        repo().update("equipment_requests", rid, {"status": "CANCELLED"})
        audit("eqp.request_cancel", "equipment_request", rid)

    elif action in ("approve", "reject"):
        if r["status"] != "PENDING":
            abort(400)
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
            notify(r["requester_id"], "คำขอยืมถูกปฏิเสธ", reason, url_for("equipment.request_detail", rid=rid))
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
            notify(r["requester_id"], "คำขอยืมได้รับอนุมัติ", f"รับอุปกรณ์ได้ {thai_date(r['borrow_at'])}",
                   url_for("equipment.request_detail", rid=rid))
        audit(f"eqp.request_{action}", "equipment_request", rid, new={"reason": reason})

    elif action == "issue":
        # EQP-09 จ่ายอุปกรณ์ บันทึกผู้รับ สแกน/เลือกรายชิ้น ตรวจบัตร
        if not manage or r["status"] != "APPROVED":
            abort(403)
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

    elif action == "return":
        # EQP-10 รับคืน (รองรับคืนบางส่วน) บันทึกสภาพ ชำรุด สูญหาย
        if not manage or r["status"] not in OUT_REQ:
            abort(403)
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
        repo().update("equipment_requests", rid, {"status": "RETURNED" if done else "PARTIAL_RETURNED"})
        if done and r.get("is_self_request"):
            notify_role("ADMIN_VP", "รอยืนยันรับคืน (หัวหน้าฝ่ายอุปกรณ์ยืมเอง)", "", url_for("equipment.request_detail", rid=rid))
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
        notify(r["requester_id"], "มีค่าชดใช้อุปกรณ์", f"{amount:,.2f} บาท ชำระหน้างานที่ฝ่ายอุปกรณ์",
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
    if st not in ("PAID", "WAIVED") or (st == "WAIVED" and not note):
        flash("การยกเว้นต้องระบุเหตุผล", "danger")
    else:
        repo().update("equipment_fines", fid, {"status": st, "settled_at": form_dt(request.form.get("settled_at")) or now_iso(),
                                               "note": (f.get("note") or "") + (" | " + note if note else "")})
        audit("eqp.fine_settle", "equipment_fine", fid, new={"status": st, "note": note})
    return redirect(request.referrer or url_for("equipment.fines"))


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
                       url_for("equipment.request_detail", rid=r["id"]))
            elif due - t < timedelta(days=1):
                notify(r["requester_id"], "ใกล้ครบกำหนดคืนอุปกรณ์", thai_date(r.get("due_at")),
                       url_for("equipment.request_detail", rid=r["id"]))
    removed = 0
    for d in repo().all("borrower_documents"):
        da = parse_dt(d.get("delete_after"))
        if da and da < t and not repo().where("equipment_fines", request_id=d["request_id"], status="PENDING"):
            delete_file(d["file_path"])
            repo().delete("borrower_documents", d["id"])
            removed += 1
    return removed
