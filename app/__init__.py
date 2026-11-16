"""STA-SCMS ระบบบริหารจัดการสโมสรนักศึกษาคณะวิทยาศาสตร์เทคโนโลยีและการเกษตร"""
from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect

from .config import Config
from .db import init_repo, repo

csrf = CSRFProtect()


def create_app(config_object=Config, **overrides):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_object)
    app.config.update(overrides)
    csrf.init_app(app)
    init_repo(app)

    from . import exports, rbac, utils
    from .services import display_name
    from .views import (activities, admin, announcements, auth, cron, dashboard, documents, equipment,
                        public, recruitment, reports, team, tickets)
    from .views import line as line_view

    for mod in (public, auth, dashboard, admin, recruitment, activities, team, equipment, tickets,
                announcements, documents, reports, cron, line_view):
        app.register_blueprint(mod.bp)
    csrf.exempt(line_view.webhook)  # LINE เรียกเข้ามาเอง ตรวจด้วยลายเซ็นแทน CSRF
    app.register_blueprint(exports.bp)
    csrf.exempt(cron.bp)

    app.jinja_env.filters["thaidate"] = utils.thai_date
    app.jinja_env.filters["thaiday"] = lambda v: utils.thai_date(v, with_time=False)
    app.jinja_env.filters["inputdt"] = utils.input_dt
    app.jinja_env.filters["uname"] = display_name
    app.jinja_env.filters["extract"] = lambda key, mapping: mapping.get(key, key)

    @app.context_processor
    def inject():
        u = rbac.current_user()
        unread = 0
        if u:
            unread = len(repo().where("notifications", user_id=u["id"], is_read=False))
        return {
            "me": u,
            "my_roles": rbac.roles(),
            "activities_on": bool(app.config.get("ACTIVITIES_ENABLED")),
            "can": rbac.can,
            "has_role": rbac.has_role,
            "ROLE_LABELS": rbac.ROLE_LABELS,
            "unread_count": unread,
            "db_backend": app.config.get("DB_BACKEND"),
            "STATUS": STATUS_LABELS,
        }

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="คุณไม่มีสิทธิ์เข้าถึงหน้านี้"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="ไม่พบหน้าที่ต้องการ"), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("error.html", code=413, message="ไฟล์ใหญ่เกินกำหนด"), 413

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp

    return app


STATUS_LABELS = {
    # ทั่วไป
    "DRAFT": ("ร่าง", "secondary"), "OPEN": ("เปิดรับ", "success"), "CLOSED": ("ปิดรับ", "dark"),
    "PENDING": ("รออนุมัติ", "warning"), "APPROVED": ("อนุมัติ", "success"), "REJECTED": ("ปฏิเสธ", "danger"),
    "CANCELLED": ("ยกเลิก", "secondary"), "RECONSIDER": ("รอพิจารณาใหม่", "warning"),
    # ใบสมัคร
    "SUBMITTED": ("ส่งแล้ว", "info"), "QUALIFIED": ("ผ่านคุณสมบัติ", "primary"), "UNQUALIFIED": ("ไม่ผ่านคุณสมบัติ", "danger"),
    "INTERVIEW": ("รอสัมภาษณ์", "info"), "IN_BATCH": ("รออนุมัติ", "warning"), "PASS": ("ผ่าน", "success"),
    "RESERVE": ("สำรอง", "info"), "FAIL": ("ไม่ผ่าน", "danger"), "CONFIRMED": ("ยืนยันแล้ว", "success"),
    "DECLINED": ("สละสิทธิ์", "secondary"), "APPOINTED": ("แต่งตั้งแล้ว", "success"),
    "SCREEN_PASS": ("ผ่านคัดกรอง", "primary"), "SCREEN_FAIL": ("ไม่ผ่านคัดกรอง", "danger"),
    "WORKING": ("ปฏิบัติงาน", "primary"), "DONE": ("เสร็จสิ้น", "success"), "ABSENT": ("ขาด", "danger"),
    "ANNOUNCED": ("ประกาศผลแล้ว", "success"),
    # อุปกรณ์
    "ISSUED": ("จ่ายแล้ว", "primary"), "PARTIAL_RETURNED": ("คืนบางส่วน", "info"), "RETURNED": ("คืนครบ", "success"),
    "AVAILABLE": ("ว่าง", "success"), "BORROWED": ("ถูกยืม", "primary"), "REPAIR": ("ซ่อม", "warning"),
    "LOST": ("สูญหาย", "danger"), "DISPOSED": ("ตัดจำหน่าย", "secondary"), "PAID": ("ชำระแล้ว", "success"),
    "WAIVED": ("ยกเว้น", "secondary"), "IN_PROGRESS": ("กำลังดำเนินการ", "info"), "COMPLETED": ("เสร็จแล้ว", "success"),
    # คำร้อง
    "CLAIMED": ("รับเรื่องแล้ว", "primary"), "WAITING_INFO": ("รอข้อมูลเพิ่ม", "warning"),
    "ANSWERED": ("ตอบกลับแล้ว", "info"),
    # ประกาศ
    "SCHEDULED": ("ตั้งเวลา", "info"), "PUBLISHED": ("เผยแพร่", "success"), "ARCHIVED": ("เก็บถาวร", "secondary"),
    # ผู้ใช้
    "ACTIVE": ("ใช้งาน", "success"), "SUSPENDED": ("ระงับ", "danger"), "ENDED": ("หมดวาระ", "secondary"),
}
