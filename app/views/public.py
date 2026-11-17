"""หน้าสาธารณะ (ANN-01, ANN-06, FAQ-02) ไม่ต้องล็อกอิน"""
import hashlib
import os

from flask import abort, Blueprint, current_app, render_template, request, Response, url_for

from ..db import repo
from ..services import approval_state
from ..storage import load_bytes, mimetype_for
from ..utils import now, parse_dt
from .team import open_events

bp = Blueprint("public", __name__)


def is_published(a):
    """ประกาศที่แสดงต่อสาธารณะ: เผยแพร่แล้ว หรือถึงเวลาที่ตั้งไว้ และถ้าเป็นผลคัดเลือกต้องอนุมัติครบ (ANN-04)"""
    st = a.get("status")
    if st == "PUBLISHED":
        ok = True
    elif st == "SCHEDULED":
        t = parse_dt(a.get("publish_at"))
        ok = bool(t and t <= now())
    else:
        ok = False
    if ok and a.get("approval_request_id"):
        ok = approval_state(repo().get("approval_requests", a["approval_request_id"]))["approved"]
    return ok


def published_announcements(category=None):
    items = [a for a in repo().all("announcements") if is_published(a)]
    if category:
        items = [a for a in items if a.get("category_id") == category]
    items.sort(key=lambda a: a.get("publish_at") or a.get("created_at") or "", reverse=True)
    items.sort(key=lambda a: not a.get("is_pinned"))
    return items


BANNER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".svg")
COMING_SOON = {
    "shop": ("ร้านค้าสโมสร", "bag"),
    "rooms": ("จองใช้ห้อง", "door-open"),
    "org": ("องค์กรและโครงสร้างสโมสร", "diagram-3"),
}


def uploaded_banners(only_active=True):
    rows = sorted(repo().all("home_banners"), key=lambda b: (int(b.get("order") or 0), b.get("created_at") or ""))
    return [b for b in rows if b.get("is_active", True)] if only_active else rows


def banner_url(b):
    """URL รูปพร้อมรุ่น (v) ตามไฟล์ เพราะรูปถูกแคชที่เบราว์เซอร์"""
    v = hashlib.md5((b.get("file_path") or "").encode()).hexdigest()[:8]
    return url_for("public.banner", bid=b["id"], v=v)


def home_banners():
    """รูป Banner หน้าแรก: รูปที่อัปโหลดในหน้าจัดการ Banner ถ้ายังไม่มีใช้รูปตั้งต้นใน static/img/banners"""
    rows = uploaded_banners()
    if rows:
        return [{"url": banner_url(b), "title": b.get("title") or "", "link": b.get("link") or ""} for b in rows]
    folder = os.path.join(current_app.static_folder, "img", "banners")
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(BANNER_EXTS))
    except OSError:
        names = []
    return [{"url": url_for("static", filename="img/banners/" + n), "title": "", "link": "", "default": True} for n in names]


@bp.route("/banner/<bid>")
def banner(bid):
    b = repo().get("home_banners", bid)
    if not b or not b.get("file_path"):
        abort(404)
    return Response(load_bytes(b["file_path"]), mimetype=mimetype_for(b["file_path"]),
                    headers={"Cache-Control": "public, max-age=86400"})


@bp.route("/")
def home():
    all_anns = published_announcements()
    anns = all_anns[:6]
    notices = [a for a in all_anns if a.get("category_id") == "NOTICE"][:6]
    cats = {c["id"]: c for c in repo().all("announcement_categories")}
    rounds = [r for r in repo().where("recruitment_rounds", status="OPEN")]
    open_positions = repo().where("activity_positions", status="OPEN") if current_app.config.get("ACTIVITIES_ENABLED") else []
    from .join import is_open as join_is_open
    join_rounds = [r for r in repo().where("join_rounds", status="OPEN") if join_is_open(r)]
    return render_template("public/home.html", anns=anns, notices=notices, cats=cats, rounds=rounds, join_rounds=join_rounds,
                           open_positions=open_positions, banners=home_banners(),
                           gallery=[a for a in all_anns if a.get("cover_path")][:4],
                           team_events=open_events())


@bp.route("/soon/<key>")
def coming_soon(key):
    if key not in COMING_SOON:
        abort(404)
    title, icon = COMING_SOON[key]
    return render_template("public/coming_soon.html", title=title, icon=icon)


@bp.route("/news")
def news():
    cat = request.args.get("cat")
    q = (request.args.get("q") or "").strip()
    items = published_announcements(cat)
    if q:
        items = [a for a in items if q in a.get("title", "") or q in a.get("body", "")]
    cats = {c["id"]: c for c in repo().all("announcement_categories")}
    return render_template("public/news.html", items=items, cats=cats, cat=cat, q=q)


@bp.route("/news/<slug>")
def news_detail(slug):
    items = repo().where("announcements", slug=slug)
    if not items or not is_published(items[0]):
        abort(404)
    cats = {c["id"]: c for c in repo().all("announcement_categories")}
    return render_template("public/news_detail.html", a=items[0], cats=cats)


@bp.route("/faq")
def faq():
    cats = repo().all("faq_categories")
    faqs = [f for f in repo().where("faqs", status="PUBLISHED")]
    q = (request.args.get("q") or "").strip()
    if q:
        faqs = [f for f in faqs if q in f.get("question", "") or q in f.get("answer", "")]
    grouped = {c["id"]: [f for f in faqs if f.get("category_id") == c["id"]] for c in cats}
    return render_template("public/faq.html", cats=cats, grouped=grouped, q=q)
