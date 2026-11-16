"""LINE OA: หน้าตั้งค่าการแจ้งเตือน/ผูกบัญชี และ webhook (NTF-02, NTF-03, NTF-07, NTF-08)"""
import re
import secrets

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

from .. import line
from ..db import repo
from ..rbac import can, current_user, login_required
from ..services import audit
from ..utils import now, now_iso, parse_dt, thai_date

bp = Blueprint("line", __name__, url_prefix="/line")
MENU_LOANS, MENU_TICKETS = "เช็กสถานะคำขอยืม", "คำร้องของฉัน"
LOAN_STATUS = {"PENDING": "รอพิจารณา", "APPROVED": "อนุมัติแล้ว รอรับของ", "ISSUED": "ยืมอยู่",
               "PARTIAL_RETURNED": "คืนบางส่วน", "RETURNED": "คืนครบ รอปิดรายการ"}
TICKET_STATUS = {"SUBMITTED": "รอรับเรื่อง", "CLAIMED": "รับเรื่องแล้ว", "IN_PROGRESS": "กำลังดำเนินการ",
                 "WAITING_INFO": "รอข้อมูลเพิ่มจากคุณ", "ANSWERED": "ตอบกลับแล้ว", "CLOSED": "ปิดเรื่องแล้ว"}


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    u = current_user()
    link = line.link_of(u["id"])
    if request.method == "POST":
        action = request.form.get("action")
        if action == "dismiss":
            repo().update("users", u["id"], {"line_banner_dismissed": True})
            return redirect(request.referrer or url_for("dashboard.index"))
        if not line.configured():
            abort(404)
        if action == "code":
            if not request.form.get("consent"):
                flash("กรุณายินยอมให้ระบบเก็บรหัสผู้ใช้ LINE เพื่อส่งแจ้งเตือนก่อนขอรหัส", "danger")
            else:
                session.setdefault("line_sid", secrets.token_hex(16))
                code = line.new_link_code(u["id"], session["line_sid"])
                session["line_code"] = code
                prefs = {e: bool(request.form.get(f"ev_{e}")) for e in line.EVENTS}
                repo().set("notification_preferences", u["id"], {"line": prefs, "updated_at": now_iso()})
                repo().add("consents", {"user_id": u["id"], "purpose": "LINE_NOTIFY", "ref": None, "granted_at": now_iso(),
                                        "expires_at": None})
                audit("line.request_code", "user", u["id"])
        elif action == "prefs" and link:
            prefs = {e: bool(request.form.get(f"ev_{e}")) for e in line.EVENTS}
            repo().set("notification_preferences", u["id"], {"line": prefs, "updated_at": now_iso()})
            flash("บันทึกประเภทการแจ้งเตือนแล้ว", "success")
        elif action == "unlink":
            line.unlink(u["id"])
            session.pop("line_code", None)
            audit("line.unlink", "user", u["id"])
            flash("ยกเลิกการผูกบัญชี LINE และลบรหัสผู้ใช้ LINE ออกจากระบบแล้ว", "info")
        return redirect(url_for("line.settings"))
    code = None
    if not link and session.get("line_code"):
        # แสดงรหัสเฉพาะ session ที่ขอ และเฉพาะช่วงที่รหัสยังใช้ได้
        doc = repo().get("line_link_codes", line._code_hash(session["line_code"])) if line.configured() else None
        exp = parse_dt(doc.get("expires_at")) if doc else None
        if doc and doc.get("session_id") == session.get("line_sid") and exp and exp > now():
            code = {"value": session["line_code"], "expires": thai_date(doc["expires_at"])}
        else:
            session.pop("line_code", None)
    quota = line.quota_state() if line.configured() and can("admin.manage") else None
    return render_template("dashboard/line.html", link=link, prefs=line.prefs_of(u["id"]), events=line.EVENTS, code=code,
                           quota=quota, configured=line.configured())


def _linked_user(line_user_id):
    links = [x for x in repo().where("line_links", line_user_id=line_user_id) if x.get("status") == "ACTIVE"]
    return links[0]["user_id"] if links else None


def _menu_reply(uid, text):
    """NTF-08 เมนูของ OA ตอบด้วย reply (ไม่นับโควตา) แสดงเฉพาะรหัสติดตามและสถานะ ไม่มีเนื้อหา"""
    if text == MENU_LOANS:
        rows = [r for r in repo().where("equipment_requests", requester_id=uid) if r.get("status") in LOAN_STATUS]
        if not rows:
            return "คุณไม่มีคำขอยืมที่ยังไม่ปิด"
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return "คำขอยืมของคุณ\n" + "\n".join(
            f"• {r.get('tracking_code')}: {LOAN_STATUS[r['status']]} (กำหนดคืน {thai_date(r.get('due_at'))})" for r in rows[:5])
    rows = sorted(repo().where("tickets", submitter_id=uid), key=lambda t: t.get("created_at") or "", reverse=True)
    if not rows:
        return "คุณไม่มีคำร้องที่ส่งแบบระบุตัวตน (เรื่องนิรนามติดตามได้ในระบบด้วยรหัสติดตามและ PIN เท่านั้น)"
    return "คำร้องของคุณ\n" + "\n".join(f"• {t.get('tracking_code')}: {TICKET_STATUS.get(t.get('status'), t.get('status'))}"
                                         for t in rows[:5])


@bp.route("/webhook", methods=["POST"])
def webhook():
    if not line.configured():
        abort(404)
    body = request.get_data()
    if not line.verify_signature(body, request.headers.get("X-Line-Signature", "")):
        abort(401)
    for ev in (request.get_json(silent=True) or {}).get("events", []):
        src = (ev.get("source") or {}).get("userId")
        if not src:
            continue
        kind = ev.get("type")
        if kind == "unfollow":
            # ผู้ใช้บล็อก OA: ตั้งสถานะการผูกเป็นไม่ใช้งาน
            for x in repo().where("line_links", line_user_id=src):
                repo().update("line_links", x["id"], {"status": "BLOCKED"})
        elif kind == "follow":
            for x in repo().where("line_links", line_user_id=src):
                repo().update("line_links", x["id"], {"status": "ACTIVE"})
            line.reply(ev.get("replyToken"), "ขอบคุณที่เพิ่มเพื่อน บัญชีนี้ใช้ส่งแจ้งเตือนของระบบสโมสรนักศึกษาเท่านั้น\n"
                       "วิธีผูกบัญชี: เข้าระบบ > ตั้งค่าการแจ้งเตือน LINE > กด \"ขอรหัสผูกบัญชี\" แล้วส่งรหัส 6 หลักมาในแชตนี้")
        elif kind == "message" and (ev.get("message") or {}).get("type") == "text":
            text = (ev["message"].get("text") or "").strip()
            if re.fullmatch(r"\d{6}", text):
                ok, msg = line.redeem_code(src, text)
                line.reply(ev.get("replyToken"), msg)
                continue
            uid = _linked_user(src)
            if text in (MENU_LOANS, MENU_TICKETS):
                line.reply(ev.get("replyToken"), _menu_reply(uid, text) if uid else
                           "ยังไม่ได้ผูกบัญชี กรุณาขอรหัสผูกบัญชีในระบบแล้วส่งรหัส 6 หลักมาในแชตนี้")
            else:
                line.reply(ev.get("replyToken"), f"บัญชีนี้ใช้ส่งแจ้งเตือนอัตโนมัติ ไม่มีเจ้าหน้าที่ตอบข้อความ\n"
                           f"พิมพ์ \"{MENU_LOANS}\" หรือ \"{MENU_TICKETS}\" เพื่อดูสถานะ หรือส่งรหัส 6 หลักเพื่อผูกบัญชี")
    return jsonify({"ok": True})
