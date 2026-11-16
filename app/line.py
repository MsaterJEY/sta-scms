"""แจ้งเตือนผ่าน LINE Official Account (Messaging API) — SRS v3 หัวข้อ 6 (NTF-02..09)

- ช่องทางเสริม: แจ้งเตือนในระบบและอีเมลยังเป็นช่องทางหลัก ผู้ใช้ไม่เชื่อม LINE ก็ใช้งานได้ครบ
- push นับโควตารายเดือน จึงส่งเฉพาะเหตุการณ์สำคัญที่ผู้ใช้เลือกรับ และหยุดส่งเมื่อโควตาใกล้เต็ม (NTF-06)
- reply ไม่นับโควตา ใช้ตอบยืนยันการผูกบัญชีและเมนูของ OA (NTF-08)
- ข้อความสั้น ไม่มีข้อมูลอ่อนไหว มีแต่ประเภทเหตุการณ์และลิงก์เข้าระบบ (NTF-04)
- โทเค็นของ OA อยู่ในตัวแปรสภาพแวดล้อม ไม่เก็บในฐานข้อมูล
"""
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import timedelta

from flask import current_app

from .db import repo
from .utils import now, now_iso, parse_dt, to_iso

API = "https://api.line.me/v2/bot"
# event_type: (ชื่อในหน้าตั้งค่า, ข้อความที่ส่ง)
EVENTS = {
    "APPROVAL": ("รายการรออนุมัติ (สรุปวันละครั้ง)", "คุณมีรายการรออนุมัติ {count} รายการ"),
    # ใช้กับทุกรายการของการยืม-คืน (คำขอ อนุมัติ จ่าย รูป รับคืน ค่าปรับ ข้อความ การชำระ) ข้อความจริงส่งมาจากผู้เรียก
    "EQP_DECISION": ("ทุกรายการยืม-คืนอุปกรณ์ (คำขอ อนุมัติ จ่าย-คืน ค่าปรับ ข้อความจากฝ่ายอุปกรณ์)", "มีความเคลื่อนไหวในรายการยืม-คืนอุปกรณ์ของคุณ"),
    "EQP_DUE": ("ใกล้ครบ/เกินกำหนดคืนอุปกรณ์", "มีอุปกรณ์ที่ใกล้ครบกำหนดหรือเกินกำหนดคืน"),
    "RECRUIT": ("นัดสัมภาษณ์/ผลคัดเลือกกรรมการ", "มีความคืบหน้าการสมัครกรรมการ/อนุกรรมการของคุณ"),
    "TEAM": ("สมัครทีมงานกิจกรรม (ยืนยันการสมัคร และผลการพิจารณา)", "มีความเคลื่อนไหวในการสมัครทีมงานกิจกรรมของคุณ"),
    "TICKET_REPLY": ("คำร้องมีผู้ตอบ (ไม่รวมเรื่องนิรนาม)", "คำร้องของคุณมีการตอบกลับ"),
}
CODE_MINUTES = 10
MAX_CODE_FAILS, FAIL_LOCK_MINUTES = 5, 15


def configured():
    c = current_app.config
    return bool(c.get("LINE_CHANNEL_ACCESS_TOKEN") and c.get("LINE_CHANNEL_SECRET"))


# ---------- เรียก Messaging API ----------
def _call(method, path, payload=None):
    """คืน (status_code, dict) — ทดสอบได้ด้วย config LINE_TRANSPORT"""
    fake = current_app.config.get("LINE_TRANSPORT")
    if fake:
        return fake(method, path, payload)
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + current_app.config["LINE_CHANNEL_ACCESS_TOKEN"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except Exception as e:  # noqa: BLE001 เครือข่ายล่ม/หมดเวลา ถือว่าส่งไม่สำเร็จ
        return 0, {"message": str(e)[:200]}


def verify_signature(body, signature):
    """NTF-07 ตรวจลายเซ็น webhook ทุกครั้ง (HMAC-SHA256 ด้วย channel secret)"""
    secret = current_app.config.get("LINE_CHANNEL_SECRET") or ""
    if not secret or not signature:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, signature)


def reply(reply_token, text):
    if reply_token:
        _call("POST", "/message/reply", {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:1000]}]})


# ---------- โควตา (NTF-06) ----------
def quota_state(force=False):
    """จำนวนที่ใช้แล้วจาก LINE แคชไว้ 10 นาที: {limit, used, blocked, checked_at}"""
    st = repo().get("system_settings", "line_quota") or {}
    checked = parse_dt(st.get("checked_at"))
    if not force and checked and now() - checked < timedelta(minutes=10):
        return st
    s1, q = _call("GET", "/message/quota")
    s2, c = _call("GET", "/message/quota/consumption")
    if s1 != 200 or s2 != 200:
        return st or {"limit": None, "used": None, "blocked": False}
    limit = q.get("value") if q.get("type") == "limited" else None
    used = int(c.get("totalUsage") or 0)
    pct = float(current_app.config.get("LINE_QUOTA_STOP_PERCENT") or 90)
    blocked = limit is not None and used >= limit * pct / 100
    new = {"limit": limit, "used": used, "blocked": blocked, "checked_at": now_iso(),
           "alerted_day": st.get("alerted_day")}
    if blocked and st.get("alerted_day") != now().strftime("%Y-%m-%d"):
        new["alerted_day"] = now().strftime("%Y-%m-%d")
        for u in repo().all("users"):
            if "SYS_ADMIN" in (u.get("system_roles") or []):
                repo().add("notifications", {"user_id": u["id"], "title": "โควตาข้อความ LINE ใกล้เต็ม หยุดส่งทาง LINE แล้ว",
                                             "body": f"ใช้ไป {used} จาก {limit} ข้อความ ระบบใช้อีเมลแทนจนกว่าโควตาจะเริ่มรอบใหม่",
                                             "link": "/line/settings", "is_read": False, "created_at": now_iso()})
    repo().set("system_settings", "line_quota", new)
    return new


# ---------- การผูกบัญชีและการเลือกรับ ----------
def link_of(user_id):
    return repo().get("line_links", user_id)


def prefs_of(user_id):
    return (repo().get("notification_preferences", user_id) or {}).get("line") or {}


def wants(user_id, event):
    link = link_of(user_id)
    return bool(link and link.get("status") == "ACTIVE" and prefs_of(user_id).get(event))


def _log(user_id, event, channel, status, error=None):
    repo().add("notification_deliveries", {"user_id": user_id, "event_type": event, "channel": channel, "status": status,
                                           "error": error, "sent_at": now_iso()})


def deliver(user_id, event, link=None, count=None, text=None):
    """ส่ง LINE ให้ผู้ใช้ที่เลือกรับเหตุการณ์นี้ คืน None (ไม่ได้เลือกรับ) / "SENT" / "FAILED"

    FAILED = ผู้ใช้เลือกรับทาง LINE แต่ส่งไม่ได้ (บล็อก OA โควตาหมด เครือข่ายล่ม) ผู้เรียกใช้อีเมลแทน (NTF-05)
    """
    if event not in EVENTS or not configured() or not wants(user_id, event):
        return None
    if quota_state().get("blocked"):
        _log(user_id, event, "LINE", "FAILED", "quota")
        return "FAILED"
    text = (text or EVENTS[event][1].format(count=count or 1))[:300]
    site = (current_app.config.get("SITE_URL") or "").rstrip("/")
    if site and link and link.startswith("/"):
        text += f"\nดูรายละเอียด: {site}{link}"
    status, body = _call("POST", "/message/push", {"to": link_of(user_id)["line_user_id"],
                                                    "messages": [{"type": "text", "text": text}]})
    if status == 200:
        _log(user_id, event, "LINE", "SENT")
        return "SENT"
    if status == 429:  # โควตาหมด: หยุดส่งจนกว่าจะตรวจโควตารอบถัดไป
        st = repo().get("system_settings", "line_quota") or {}
        repo().set("system_settings", "line_quota", dict({k: v for k, v in st.items() if k != "id"}, blocked=True,
                                                         checked_at=now_iso()))
    _log(user_id, event, "LINE", "FAILED", f"{status} {body.get('message', '')}"[:200])
    return "FAILED"


def _code_hash(code):
    return hmac.new(current_app.config["SECRET_KEY"].encode(), ("line-link:" + code).encode(), hashlib.sha256).hexdigest()


def new_link_code(user_id, session_id):
    """NTF-02 รหัสใช้ครั้งเดียว อายุ 10 นาที ผูกกับ session ของผู้ขอ (เก็บเฉพาะค่าแฮช)"""
    import secrets
    for old in repo().where("line_link_codes", user_id=user_id):
        repo().delete("line_link_codes", old["id"])
    code = f"{secrets.randbelow(10 ** 6):06d}"
    repo().set("line_link_codes", _code_hash(code), {"user_id": user_id, "session_id": session_id, "used_at": None,
                                                     "expires_at": to_iso(now() + timedelta(minutes=CODE_MINUTES)),
                                                     "created_at": now_iso()})
    return code


def redeem_code(line_user_id, code):
    """จับคู่รหัสจากแชตกับบัญชี คืน (ok, ข้อความตอบกลับ) — จำกัดจำนวนครั้งที่ใส่ผิดต่อบัญชี LINE"""
    att = repo().get("line_attempts", line_user_id) or {}
    locked = parse_dt(att.get("locked_until"))
    if locked and locked > now():
        return False, f"ใส่รหัสผิดหลายครั้ง กรุณาลองใหม่ในอีก {FAIL_LOCK_MINUTES} นาที"
    doc = repo().get("line_link_codes", _code_hash(code))
    exp = parse_dt(doc.get("expires_at")) if doc else None
    if not doc or doc.get("used_at") or not exp or exp < now():
        fails = int(att.get("failed") or 0) + 1
        upd = {"failed": fails, "locked_until": None}
        if fails >= MAX_CODE_FAILS:
            upd = {"failed": 0, "locked_until": to_iso(now() + timedelta(minutes=FAIL_LOCK_MINUTES))}
        repo().set("line_attempts", line_user_id, upd)
        return False, "รหัสไม่ถูกต้องหรือหมดอายุ กรุณากด \"ขอรหัสผูกบัญชี\" ในระบบอีกครั้ง"
    # บัญชี LINE หนึ่งบัญชีผูกได้กับผู้ใช้คนเดียว
    for other in repo().where("line_links", line_user_id=line_user_id):
        if other["id"] != doc["user_id"]:
            repo().delete("line_links", other["id"])
    repo().set("line_links", doc["user_id"], {"user_id": doc["user_id"], "line_user_id": line_user_id, "status": "ACTIVE",
                                              "linked_at": now_iso()})
    repo().delete("line_link_codes", doc["id"])
    repo().delete("line_attempts", line_user_id)
    return True, "ผูกบัญชีสำเร็จ คุณจะได้รับแจ้งเตือนตามประเภทที่เลือกไว้ ปิดหรือยกเลิกได้ทุกเมื่อที่หน้าตั้งค่าการแจ้งเตือนในระบบ"


def unlink(user_id):
    """ยกเลิกการผูก: ลบรหัสผู้ใช้ LINE (ข้อมูลส่วนบุคคล) และการเลือกรับ"""
    repo().delete("line_links", user_id)
    repo().delete("notification_preferences", user_id)
    for old in repo().where("line_link_codes", user_id=user_id):
        repo().delete("line_link_codes", old["id"])
