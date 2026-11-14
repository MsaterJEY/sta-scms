"""หน้าสาธารณะ (ANN-01, ANN-06, FAQ-02) ไม่ต้องล็อกอิน"""
import os

from flask import Blueprint, abort, current_app, render_template, request

from ..db import repo
from ..services import approval_state
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


def home_banners():
    """รูป Banner หน้าแรก: ทุกไฟล์รูปใน static/img/banners เรียงตามชื่อไฟล์"""
    folder = os.path.join(current_app.static_folder, "img", "banners")
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(BANNER_EXTS))
    except OSError:
        names = []
    return ["img/banners/" + n for n in names]


@bp.route("/")
def home():
    all_anns = published_announcements()
    anns = all_anns[:6]
    notices = [a for a in all_anns if a.get("category_id") == "NOTICE"][:6]
    cats = {c["id"]: c for c in repo().all("announcement_categories")}
    rounds = [r for r in repo().where("recruitment_rounds", status="OPEN")]
    open_positions = [p for p in repo().where("activity_positions", status="OPEN")]
    return render_template("public/home.html", anns=anns, notices=notices, cats=cats, rounds=rounds,
                           open_positions=open_positions, banners=home_banners(),
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
