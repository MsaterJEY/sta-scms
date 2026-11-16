"""8.0 ประกาศและ FAQ (ANN-02..06, FAQ-01)"""
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, permission_required
from ..services import approval_state, audit, notify
from ..storage import UploadError, load_bytes, mimetype_for, save_bytes, validate_upload
from ..utils import form_dt, now_iso, slugify
from .public import is_published

bp = Blueprint("announcements", __name__, url_prefix="/manage")


def _ann(aid):
    a = repo().get("announcements", aid)
    if not a:
        abort(404)
    return a


def results_blocked(a):
    """ANN-04 ประกาศผลคัดเลือกเผยแพร่ไม่ได้จนกว่าชุดผลอนุมัติครบ"""
    if not a.get("approval_request_id"):
        return False
    return not approval_state(repo().get("approval_requests", a["approval_request_id"]))["approved"]


def draft_only():
    """ทีมสื่อ (ann.draft): สร้าง/แก้ได้เฉพาะร่าง เผยแพร่ไม่ได้ (SRS v3 หัวข้อ 8)"""
    return not can("ann.manage")


@bp.route("/announcements")
@permission_required("ann.manage", "ann.draft")
def manage():
    st = request.args.get("status")
    items = sorted(repo().all("announcements"), key=lambda a: a.get("created_at", ""), reverse=True)
    if st:
        items = [a for a in items if a.get("status") == st]
    cats = {c["id"]: c for c in repo().all("announcement_categories")}
    for a in items:
        a["blocked"] = results_blocked(a)
        a["live"] = is_published(a)
    return render_template("ann/manage.html", items=items, cats=cats, status=st, draft_only=draft_only())


@bp.route("/announcements/new", methods=["GET", "POST"])
@bp.route("/announcements/<aid>/edit", methods=["GET", "POST"])
@permission_required("ann.manage", "ann.draft")
def form(aid=None):
    a = _ann(aid) if aid else None
    if a and draft_only() and a.get("status") != "DRAFT":
        abort(403)
    if request.method == "POST":
        f = request.form
        data = {"title": f.get("title", "").strip(), "body": f.get("body", ""), "category_id": f.get("category_id"),
                "is_pinned": bool(f.get("is_pinned")) and not draft_only(), "target_group": f.get("target_group", "ALL")}
        if not data["title"]:
            flash("กรุณาระบุหัวข้อ", "danger")
            return redirect(request.url)
        cover = request.files.get("cover")
        if cover and cover.filename:
            try:
                b, ext, ctype = validate_upload(cover, images_only=True, allow_pdf=False)
                data["cover_path"] = save_bytes("covers", b, ext, ctype)
            except UploadError as e:
                flash(str(e), "danger")
        if a:
            repo().update("announcements", aid, data)
        else:
            data.update({"slug": slugify(data["title"]), "status": "DRAFT", "publish_at": None,
                         "author_id": current_user()["id"], "source_type": None, "source_id": None,
                         "approval_request_id": None, "created_at": now_iso()})
            aid = repo().add("announcements", data)
        audit("announcement.save", "announcement", aid)
        flash("บันทึกแล้ว", "success")
        return redirect(url_for("announcements.form", aid=aid))
    cats = repo().all("announcement_categories")
    return render_template("ann/form.html", a=a, cats=cats, blocked=results_blocked(a) if a else False,
                           draft_only=draft_only())


@bp.route("/announcements/<aid>/status", methods=["POST"])
@permission_required("ann.manage")
def set_status(aid):
    a = _ann(aid)
    action = request.form.get("action")
    upd = {}
    if action in ("publish", "schedule"):
        if results_blocked(a):
            flash("ประกาศผลคัดเลือกเผยแพร่ไม่ได้จนกว่าชุดผลจะอนุมัติครบ", "danger")
            return redirect(url_for("announcements.form", aid=aid))
        if action == "publish":
            upd = {"status": "PUBLISHED", "publish_at": now_iso(), "published_by": current_user()["id"]}
        else:
            t = form_dt(request.form.get("publish_at"))
            if not t:
                flash("กรุณาระบุเวลาเผยแพร่", "danger")
                return redirect(url_for("announcements.form", aid=aid))
            upd = {"status": "SCHEDULED", "publish_at": t, "published_by": current_user()["id"]}
    elif action == "archive":
        upd = {"status": "ARCHIVED", "archived_at": now_iso()}
    elif action == "draft":
        upd = {"status": "DRAFT"}
    elif action == "pin":
        upd = {"is_pinned": not a.get("is_pinned")}
    else:
        abort(400)
    repo().update("announcements", aid, upd)
    audit(f"announcement.{action}", "announcement", aid, new=upd)
    if action == "publish":
        _notify_publish(repo().get("announcements", aid))
    flash("บันทึกแล้ว", "success")
    return redirect(url_for("announcements.manage"))


def _notify_publish(a):
    """ANN-05 แจ้งเตือนกลุ่มเป้าหมาย"""
    if a.get("notified"):
        return
    group = a.get("target_group", "ALL")
    users = [u for u in repo().all("users") if u.get("status") == "ACTIVE"]
    if group == "SCI":
        users = [u for u in users if u.get("account_type") == "SCI"]
    elif group == "NONE":
        users = []
    notify([u["id"] for u in users], a["title"], "", url_for("public.news_detail", slug=a["slug"]))
    repo().update("announcements", a["id"], {"notified": True})


@bp.route("/cover/<aid>")
def cover(aid):
    a = _ann(aid)
    if not a.get("cover_path") or not (is_published(a) or can("ann.manage") or can("ann.draft")):
        abort(404)
    return Response(load_bytes(a["cover_path"]), mimetype=mimetype_for(a["cover_path"]),
                    headers={"Cache-Control": "public, max-age=86400"})


def publish_scheduled():
    """งาน cron: เปลี่ยนสถานะประกาศที่ถึงเวลา และแจ้งเตือน"""
    n = 0
    for a in repo().where("announcements", status="SCHEDULED"):
        if is_published(a):
            repo().update("announcements", a["id"], {"status": "PUBLISHED"})
            _notify_publish(a)
            n += 1
    return n


# ---------- FAQ (FAQ-01) ----------
@bp.route("/faq", methods=["GET", "POST"])
@permission_required("faq.manage")
def faq_manage():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "save":
            data = {"category_id": f.get("category_id"), "question": f.get("question", "").strip(),
                    "answer": f.get("answer", ""), "status": "PUBLISHED" if f.get("published") else "DRAFT",
                    "order": int(f.get("order") or 0), "updated_by": current_user()["id"], "updated_at": now_iso()}
            if f.get("faq_id"):
                repo().update("faqs", f["faq_id"], data)
            else:
                repo().add("faqs", data)
        elif action == "delete":
            repo().delete("faqs", f.get("faq_id"))
        elif action == "category" and f.get("name", "").strip():
            repo().add("faq_categories", {"name": f["name"].strip()})
        audit(f"faq.{action}", "faq", f.get("faq_id"))
        flash("บันทึกแล้ว", "success")
        return redirect(url_for("announcements.faq_manage"))
    cats = repo().all("faq_categories")
    faqs = sorted(repo().all("faqs"), key=lambda x: (x.get("category_id") or "", x.get("order", 0)))
    edit = repo().get("faqs", request.args.get("edit")) if request.args.get("edit") else None
    return render_template("ann/faq_manage.html", cats=cats, faqs=faqs, edit=edit)


@dataset("announcement")
def ds_announcement(args):
    """คัดลอกข้อมูลดิบของประกาศรายการเดียว"""
    if not (can("ann.manage") or can("ann.draft")):
        abort(403)
    a = _ann(args.get("id"))
    cat = repo().get("announcement_categories", a.get("category_id")) or {}
    return "announcement", ["หัวข้อ", "หมวด", "เนื้อหา", "ลิงก์"], [[a["title"], cat.get("name"), a.get("body"),
                                                                     url_for("public.news_detail", slug=a["slug"], _external=True)]]
