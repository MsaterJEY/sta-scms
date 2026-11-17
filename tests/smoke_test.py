"""ทดสอบการทำงานหลักแบบ end-to-end ด้วยฐานข้อมูลไฟล์ชั่วคราว

รัน: python tests/smoke_test.py
"""
import io
import os
import re
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.pop("FIREBASE_CREDENTIALS", None)

from app import create_app  # noqa: E402
from app.db import repo  # noqa: E402
from app.utils import now  # noqa: E402

tmp = tempfile.mkdtemp()
app = create_app(LOCAL_DB_PATH=os.path.join(tmp, "db.json"), LOCAL_UPLOAD_DIR=os.path.join(tmp, "up"),
                 WTF_CSRF_ENABLED=False, TESTING=True, SETUP_TOKEN="tok", CRON_SECRET="cron", SERVER_NAME="localhost",
                 ACTIVITIES_ENABLED=True)

PW = "demo12345"
FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def dt(days=0, hours=0):
    return (now() + timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M")


def client_for(code):
    c = app.test_client()
    r = c.post("/login", data={"identifier": code, "password": PW})
    assert r.status_code == 302, f"login {code} failed"
    return c


def get_ok(c, url, label=None):
    r = c.get(url)
    check(r.status_code == 200, f"GET {label or url} -> {r.status_code}")
    return r


with app.app_context():
    import subprocess
    env = dict(os.environ, LOCAL_DB_PATH=os.path.join(tmp, "db.json"), LOCAL_UPLOAD_DIR=os.path.join(tmp, "up"))
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "scripts", "seed.py"), "--demo",
                    "--admin-email", "admin@demo.local", "--admin-password", PW], check=True, env=env)

    print("== public")
    pub = app.test_client()
    for u in ["/", "/news", "/faq", "/login", "/register"]:
        get_ok(pub, u)
    check(pub.get("/app/").status_code == 302, "dashboard requires login")
    check(pub.get("/setup?token=tok").status_code == 200, "setup page with token")
    check(pub.get("/setup").status_code == 404, "setup hidden without token")

    print("== register + student code parsing")
    r = pub.post("/register", data={"mode": "student", "student_code": "406765007", "first_name": "สมชาย", "last_name": "ใจดี",
                                    "email": "s7@demo.local", "password": PW, "password2": PW, "consent": "1"})
    check(r.status_code == 302, "register SCI")
    u7 = repo().where("users", student_code="406765007")[0]
    check(u7["account_type"] == "SCI" and u7["year_level"] == 3 and u7["program_code"] == "65", "parse 406765007 -> SCI ปี 3 สาขา 65")
    u_other = repo().where("users", student_code="406111011")[0]
    check(u_other["account_type"] == "OTHER_MRU", "406111011 -> OTHER_MRU")
    r = pub.post("/register", data={"mode": "external", "first_name": "a", "last_name": "b", "email": "x@x.com",
                                    "password": PW, "password2": PW, "consent": "1"})
    check(not repo().where("users", email="x@x.com"), "external registration blocked by default")

    admin = client_for("admin@demo.local")
    pres, psec, avp, asec = (client_for(c) for c in ("406765001", "406765002", "406765003", "406765004"))
    eqh, ph, pr, acvp = (client_for(c) for c in ("406765005", "406765006", "406765008", "406765009"))
    stu, other = client_for("406865010"), client_for("406111011")
    s7 = client_for("406765007")

    print("== admin pages")
    for u in ["/admin/users", "/admin/users?review=1", "/admin/holders", "/admin/positions", "/admin/years", "/admin/programs",
              "/admin/settings", "/admin/permissions", "/admin/audit", f"/admin/users/{u7['id']}"]:
        get_ok(admin, u)
    check(stu.get("/admin/users").status_code == 403, "student blocked from admin")
    r = admin.post("/admin/holders", data={"action": "add", "identifier": "406865010", "position_id": "EQUIP_HEAD", "year": 2569})
    check(len(repo().where("position_holders", position_id="EQUIP_HEAD", status="ACTIVE")) == 1, "single-holder EQUIP_HEAD not duplicated")
    r = admin.post("/admin/permissions", data={"perm": "eqp.manage", "roles": ["PR"]})
    check(r.status_code == 403, "locked permission cannot be changed")

    print("== dashboard / exports")
    for c in (stu, eqh, avp, pres):
        get_ok(c, "/app/")
        get_ok(c, "/profile")
    r = admin.get("/export/users?fmt=tsv")
    check(r.status_code == 200 and "รหัสนักศึกษา\t" in r.get_data(as_text=True), "export users TSV header")
    r = admin.get("/export/users?fmt=csv")
    check(r.data.startswith("﻿".encode()), "CSV has UTF-8 BOM")
    r = admin.get("/export/users?fmt=xlsx")
    check(r.status_code == 200 and r.data[:2] == b"PK", "XLSX export")
    check(len(repo().all("export_jobs")) >= 3, "export_jobs logged")
    check(stu.get("/export/users?fmt=tsv").status_code == 403, "student cannot export users")

    print("== recruitment (REC)")
    r = ph.post("/recruit/new", data={"title": "รับสมัครกรรมการ 2569", "type": "COMMITTEE", "academic_year": 2569,
                                      "open_at": dt(-1), "close_at": dt(7), "min_year": 1,
                                      "quota_COMM_ACTIVITY": 2, "quota_COMM_PR": 1,
                                      "crit_name": ["บุคลิก", "ความรู้"], "crit_max": [10, 10], "crit_weight": [1, 1]})
    rid = repo().all("recruitment_rounds")[0]["id"]
    ph.post(f"/recruit/{rid}/status", data={"status": "OPEN"})
    check(repo().get("recruitment_rounds", rid)["status"] == "OPEN", "round opened (no approval needed)")
    check(any(a.get("source_type") == "recruitment_round" for a in repo().all("announcements")), "auto draft announcement")
    rps = repo().where("round_positions", round_id=rid)
    rp_ac = next(x for x in rps if x["position_id"] == "COMM_ACTIVITY")["id"]
    rp_sp = next(x for x in rps if x["position_id"] == "COMM_PR")["id"]
    get_ok(stu, f"/recruit/{rid}", "round detail (student)")
    stu.post(f"/recruit/{rid}/apply", data={"choice_1": rp_ac, "choice_2": rp_ac})
    check(not repo().where("member_applications", round_id=rid), "duplicate choice rejected")
    stu.post(f"/recruit/{rid}/apply", data={"choice_1": rp_ac, "choice_2": rp_sp, "gpa": "3.2"})
    s7.post(f"/recruit/{rid}/apply", data={"choice_1": rp_sp, "gpa": "2.8"})
    eqh.post(f"/recruit/{rid}/apply", data={"choice_1": rp_sp})
    other.post(f"/recruit/{rid}/apply", data={"choice_1": rp_sp})
    apps = repo().where("member_applications", round_id=rid)
    check(len(apps) == 2, "BOARD and OTHER_MRU cannot apply; 2 applications")
    stu.post(f"/recruit/{rid}/apply", data={"choice_1": rp_sp})
    check(len(repo().where("member_applications", round_id=rid)) == 2, "one application per round")
    get_ok(ph, f"/recruit/{rid}", "round detail (personnel head)")
    r = ph.get(f"/recruit/{rid}")
    check("3.2" in r.get_data(as_text=True), "personnel head sees GPA")
    r = eqh.get(f"/recruit/{rid}")
    check("3.2" not in r.get_data(as_text=True), "other board does not see GPA")
    for a in apps:
        ph.post(f"/recruit/application/{a['id']}/screen", data={"result": "QUALIFIED"})
    ph.post(f"/recruit/{rid}/sessions", data={"start_at": dt(2), "location": "ห้อง 101"})
    sid = repo().all("interview_sessions")[0]["id"]
    ph.post(f"/recruit/{rid}/sessions", data={"action": "assign", "session_id": sid, "app_ids": [a["id"] for a in apps]})
    crits = repo().where("score_criteria", round_id=rid)
    for c, score_val in ((eqh, 8), (pres, 9)):
        for a in apps:
            c.post(f"/recruit/application/{a['id']}/score", data={"action": "submit", **{f"c_{k['id']}": score_val for k in crits}})
    check(len([s for s in repo().all("interview_scores") if s.get("submitted_at")]) == 8, "board scores submitted")
    eqh.post(f"/recruit/application/{apps[0]['id']}/score", data={"action": "submit", **{f"c_{k['id']}": 1 for k in crits}})
    check(all(s["score"] == 8 for s in repo().where("interview_scores", scorer_id=repo().where("users", student_code="406765005")[0]["id"])), "score locked after submit")
    check(stu.get(f"/recruit/application/{apps[0]['id']}/score").status_code == 403, "student cannot score")
    get_ok(ph, f"/recruit/{rid}/batch", "batch form")
    form = {}
    for a in apps:
        ch = sorted(repo().where("application_choices", application_id=a["id"]), key=lambda c: c["rank"])[0]
        form[f"res_{a['id']}"] = "PASS"
        form[f"choice_{a['id']}"] = ch["id"]
    ph.post(f"/recruit/{rid}/batch", data=form)
    bid = repo().all("selection_batches")[0]["id"]
    check(ph.get(f"/export/recruit_results?fmt=tsv&batch_id={bid}").status_code == 403, "results export blocked before approval")
    get_ok(avp, f"/recruit/batch/{bid}", "batch view (ADMIN_VP)")
    get_ok(avp, "/app/approvals", "approvals center")
    avp.post(f"/recruit/batch/{bid}", data={"action": "reject"})
    check(repo().get("selection_batches", bid)["status"] == "PENDING", "reject without reason refused")
    avp.post(f"/recruit/batch/{bid}", data={"action": "approve"})
    b = repo().get("selection_batches", bid)
    check(b["status"] == "PENDING", "ALL mode: 1/2 approval not final")
    eqh.post(f"/recruit/batch/{bid}", data={"action": "approve"})
    check(len(repo().all("approval_logs")) == 1, "non-approver cannot approve")
    ph.post(f"/recruit/batch/{bid}", data={"action": "announce"})
    check(not any(a.get("source_type") == "selection_batch" for a in repo().all("announcements")), "cannot draft result announcement before approval")
    asec.post(f"/recruit/batch/{bid}", data={"action": "approve"})
    check(repo().get("selection_batches", bid)["status"] == "APPROVED", "ALL mode: approved after both")
    r = ph.get(f"/export/recruit_results?fmt=tsv&batch_id={bid}")
    check(r.status_code == 200 and "406865010" in r.get_data(as_text=True), "results export after approval")
    ph.post(f"/recruit/batch/{bid}", data={"action": "announce"})
    ann = next(a for a in repo().all("announcements") if a.get("source_type") == "selection_batch")
    get_ok(stu, "/recruit/my", "my applications")
    res = repo().where("selection_results", user_id=repo().where("users", student_code="406865010")[0]["id"])[0]
    stu.post("/recruit/my", data={"result_id": res["id"], "action": "confirm"})
    check(repo().where("position_holders", user_id=res["user_id"], position_id="COMM_ACTIVITY", status="ACTIVE"), "appointment creates position holder")

    print("== announcements (ANN)")
    get_ok(pr, "/manage/announcements")
    get_ok(pr, f"/manage/announcements/{ann['id']}/edit", "announcement edit")
    pr.post(f"/manage/announcements/{ann['id']}/status", data={"action": "publish"})
    check(repo().get("announcements", ann["id"])["status"] == "PUBLISHED", "result announcement published after approval")
    get_ok(pub, f"/news/{ann['slug']}", "public news detail")
    pr.post("/manage/announcements/new", data={"title": "ข่าวทดสอบ", "body": "เนื้อหา", "category_id": "NEWS"})
    a2 = next(a for a in repo().all("announcements") if a["title"] == "ข่าวทดสอบ")
    pr.post(f"/manage/announcements/{a2['id']}/status", data={"action": "schedule", "publish_at": dt(hours=-1)})
    check("ข่าวทดสอบ" in pub.get("/news").get_data(as_text=True), "scheduled (past) visible")
    check(stu.get("/manage/announcements").status_code == 403, "student cannot manage announcements")

    print("== FAQ")
    acvp_sec = client_for("406765004")  # ADMIN_SEC is DIV_SEC
    acvp_sec.post("/manage/faq", data={"action": "save", "category_id": "GENERAL", "question": "ยืมของยังไง", "answer": "ส่งคำขอ", "published": "1"})
    check("ยืมของยังไง" in pub.get("/faq").get_data(as_text=True), "FAQ visible publicly")

    print("== activities (ACT)")
    acvp.post("/activities/new", data={"title": "ค่ายวิชาการ", "division_id": "ACTIVITY", "start_at": dt(10), "end_at": dt(11), "location": "หอประชุม"})
    act = repo().all("activities")[0]
    check(act["division_id"] == "ACTIVITY", "DIV_VP creates activity in own division")
    r = acvp.post("/activities/new", data={"title": "x", "division_id": "PR", "start_at": dt(1), "end_at": dt(2)})
    check(r.status_code == 403, "cannot create in other division")
    acvp.post(f"/activities/{act['id']}/positions", data={"name": "พิธีกร", "quota": 2, "groups": ["STUDENT"], "open_at": dt(-1), "close_at": dt(5)})
    pos = repo().all("activity_positions")[0]
    acvp.post(f"/activities/position/{pos['id']}/status", data={"status": "OPEN"})
    get_ok(stu, f"/activities/{act['id']}", "activity detail (student)")
    s7.post(f"/activities/position/{pos['id']}/apply")
    eqh.post(f"/activities/position/{pos['id']}/apply")
    other.post(f"/activities/position/{pos['id']}/apply")
    check(len(repo().all("activity_applications")) == 1, "only eligible SCI non-board can apply")
    app_id = repo().all("activity_applications")[0]["id"]
    r = ph.post(f"/activities/application/{app_id}/screen", data={"result": "SCREEN_PASS"})
    check(r.status_code == 403, "personnel head needs delegation to screen")
    get_ok(avp, "/admin/delegations")
    ph_id = repo().where("users", student_code="406765006")[0]["id"]
    avp.post("/admin/delegations", data={"action": "grant", "to_user_id": ph_id, "activity_id": act["id"], "expires_at": dt(30)})
    ph.post(f"/activities/application/{app_id}/screen", data={"result": "SCREEN_PASS"})
    check(repo().get("activity_applications", app_id)["status"] == "SCREEN_PASS", "screen with delegation")
    get_ok(ph, f"/activities/{act['id']}", "activity detail (screener)")
    ph.post(f"/activities/{act['id']}/batch", data={f"res_{app_id}": "APPROVED"})
    sb = repo().all("staff_selection_batches")[0]
    r = ph.post(f"/activities/batch/{sb['id']}", data={"action": "approve"})
    check(r.status_code == 403, "screener cannot approve")
    asec.post(f"/activities/batch/{sb['id']}", data={"action": "approve"})
    check(repo().get("staff_selection_batches", sb["id"])["status"] == "APPROVED", "ANY mode: one approval enough")
    staff = repo().all("activity_staff")[0]
    s7.post(f"/activities/staff/{staff['id']}/confirm", data={"action": "confirm"})
    check(repo().get("activity_staff", staff["id"])["confirm_status"] == "CONFIRMED", "staff confirmed")
    acvp.post(f"/activities/{act['id']}/sessions", data={"name": "วันแรก", "start_at": dt(0, -1), "end_at": dt(0, 2)})
    sess = repo().all("activity_sessions")[0]
    get_ok(acvp, f"/activities/session/{sess['id']}/qr", "QR page")
    tok = acvp.get(f"/activities/session/{sess['id']}/token").get_json()["url"]
    path = tok.split("localhost", 1)[1]
    r = s7.get(path)
    check("เช็กชื่อสำเร็จ" in r.get_data(as_text=True), "QR check-in")
    r = s7.get(path)
    check("ไปแล้ว" in r.get_data(as_text=True), "check-in only once")
    r = stu.get(path)
    check("เฉพาะทีมงาน" in r.get_data(as_text=True), "non-staff cannot check in")
    r = s7.get(f"/activities/checkin/{sess['id']}?t=bad")
    check("หมดอายุ" in r.get_data(as_text=True), "bad token rejected")
    h = repo().all("activity_hours")[0]
    check(abs(h["hours"] - 3.0) < 0.01, "hours computed from attendance")
    get_ok(s7, "/activities/my")
    check(acvp.get(f"/export/activity_staff?fmt=tsv&activity_id={act['id']}").status_code == 200, "staff export")

    print("== equipment (EQP)")

    def snap(client, rid, stage):
        """ผู้ยืมถ่ายรูปก่อนยืม/ตอนคืน"""
        return client.post(f"/equipment/request/{rid}/action", content_type="multipart/form-data",
                           data={"action": "photo", "stage": stage, "photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg")})

    eqh.post("/equipment/manage", data={"category_id": "AV", "code": "SPK", "name": "ลำโพง", "is_serialized": "1",
                                        "replacement_cost": 5000, "is_active": "1"})
    eqh.post("/equipment/manage", data={"category_id": "TENT", "code": "CHR", "name": "เก้าอี้", "quantity_total": 50,
                                        "replacement_cost": 200, "is_active": "1"})
    spk = repo().where("equipment_models", code="SPK")[0]
    chr_ = repo().where("equipment_models", code="CHR")[0]
    for tag in ("SPK-001", "SPK-002"):
        eqh.post(f"/equipment/model/{spk['id']}", data={"action": "add_unit", "asset_tag": tag})
    r = stu.post("/equipment/manage", data={"code": "X", "name": "x"})
    check(r.status_code == 403, "only EQUIP_HEAD manages equipment")
    check(avp.post("/equipment/manage", data={"code": "X", "name": "x"}).status_code == 403, "ADMIN_VP cannot manage equipment")
    get_ok(stu, "/equipment/", "catalog")
    get_ok(stu, f"/equipment/request/new?start={dt(1)}&end={dt(2)}", "request form")
    stu.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(2), "purpose": "งาน", f"qty_{spk['id']}": 1,
                                             f"qty_{chr_['id']}": 10, "consent": "1"})
    req = repo().where("equipment_requests", requester_id=repo().where("users", student_code="406865010")[0]["id"])[0]
    check(req["status"] == "PENDING" and not req["is_self_request"], "request created")
    check(len(repo().where("equipment_photos", request_id=req["id"], stage="PICKUP")) == 1, "photo of the items is saved with the request")
    stu.post("/equipment/request/new", data={"borrow_at": dt(1), "due_at": dt(2), "purpose": "ไม่มีรูป", f"qty_{chr_['id']}": 1, "consent": "1"})
    check(len(repo().all("equipment_requests")) == 1, "request without item photos is refused")
    stu.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(2), "purpose": "x", f"qty_{chr_['id']}": 60, "consent": "1"})
    check(len(repo().all("equipment_requests")) == 1, "over-availability request rejected")
    other.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(2), "purpose": "x", f"qty_{chr_['id']}": 1, "consent": "1"})
    check(len(repo().all("equipment_requests")) == 1, "OTHER_MRU must attach student card")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    other.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(2), "purpose": "x", f"qty_{chr_['id']}": 1,
                                               "consent": "1", "borrower_doc": (io.BytesIO(png), "card.png")})
    check(len(repo().all("borrower_documents")) == 1, "borrower doc uploaded (encrypted)")
    doc = repo().all("borrower_documents")[0]
    raw = open(os.path.join(tmp, "up", doc["file_path"]), "rb").read()
    check(not raw.startswith(b"\x89PNG"), "borrower doc encrypted at rest")
    check(eqh.get(f"/equipment/doc/{doc['id']}").data == png, "EQUIP_HEAD can view decrypted doc")
    check(avp.get(f"/equipment/doc/{doc['id']}").status_code == 403, "others cannot view borrower doc")
    rid_ = req["id"]
    items = repo().where("equipment_request_items", request_id=rid_)
    get_ok(eqh, f"/equipment/request/{rid_}", "request detail (head)")
    appr = {"action": "approve", **{f"approve_{i['id']}": i["qty_requested"] for i in items}}
    kept = repo().where("equipment_photos", request_id=rid_)[0]
    repo().delete("equipment_photos", kept["id"])  # จำลองคำขอเก่าที่ยังไม่มีรูป
    eqh.post(f"/equipment/request/{rid_}/action", data=appr)
    check(repo().get("equipment_requests", rid_)["status"] == "PENDING", "cannot approve a request that has no item photos")
    check("ต้องถ่ายรูปอุปกรณ์ที่จะยืมทั้งหมดก่อน" in stu.get(f"/equipment/request/{rid_}").get_data(as_text=True)
          and "ยังไม่มีรูปอุปกรณ์จากผู้ยืม" in eqh.get(f"/equipment/request/{rid_}").get_data(as_text=True), "both sides see the photo requirement")
    snap(stu, rid_, "PICKUP")
    eqh.post(f"/equipment/request/{rid_}/action", data=appr)
    check(repo().get("equipment_requests", rid_)["status"] == "APPROVED", "approved by EQUIP_HEAD")
    it_spk = next(i for i in items if i["model_id"] == spk["id"])
    ph0 = repo().where("equipment_photos", request_id=rid_)[0]
    check(stu.get(f"/equipment/photo/{ph0['id']}").status_code == 200 and eqh.get(f"/equipment/photo/{ph0['id']}").status_code == 200
          and psec.get(f"/equipment/photo/{ph0['id']}").status_code == 403, "item photo visible to borrower and EQUIP_HEAD only")
    check(snap(eqh, rid_, "PICKUP").status_code == 403, "only the borrower uploads photos")
    snap(stu, rid_, "RETURN")
    check(len(repo().where("equipment_photos", request_id=rid_)) == 1, "return photo not accepted before pickup")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "issue", f"units_{it_spk['id']}": "SPK-001", "receiver_name": "x", "id_checked": "1"})
    check(repo().get("equipment_requests", rid_)["status"] == "ISSUED", "issued")
    check(repo().where("equipment_units", asset_tag="SPK-001")[0]["status"] == "BORROWED", "unit borrowed")
    unit1 = repo().where("equipment_units", asset_tag="SPK-001")[0]
    it_chr = next(i for i in items if i["model_id"] == chr_["id"])
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "return", f"ret_{it_chr['id']}": 8, f"lost_{it_chr['id']}": 2})
    check(repo().get("equipment_requests", rid_)["status"] == "ISSUED", "cannot receive a return before the return photo")
    snap(stu, rid_, "RETURN")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "return", f"ret_{it_chr['id']}": 8, f"lost_{it_chr['id']}": 2})
    check(repo().get("equipment_requests", rid_)["status"] == "PARTIAL_RETURNED", "partial return")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "return", f"unit_{unit1['id']}": "RETURN"})
    check(repo().get("equipment_requests", rid_)["status"] == "PARTIAL_RETURNED", "every return needs a new photo")
    snap(stu, rid_, "RETURN")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "return", f"unit_{unit1['id']}": "RETURN"})
    check(repo().get("equipment_requests", rid_)["status"] == "RETURNED", "fully returned")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "close"})
    check(repo().get("equipment_requests", rid_)["status"] != "CLOSED", "cannot close with lost items and no fine")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "fine", "type": "LOSS", "amount": 400})
    fine = repo().all("equipment_fines")[0]
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "close"})
    check(repo().get("equipment_requests", rid_)["status"] != "CLOSED", "cannot close with pending fine")
    stu.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(3), "due_at": dt(4), "purpose": "x", f"qty_{chr_['id']}": 1, "consent": "1"})
    check(len(repo().where("equipment_requests", requester_id=req["requester_id"])) == 1, "blocked while fine pending")
    eqh.post(f"/equipment/fine/{fine['id']}", data={"status": "PAID"})
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "close"})
    check(repo().get("equipment_requests", rid_)["status"] == "CLOSED", "closed after fine paid")
    check(len(repo().all("equipment_transactions")) >= 4, "transactions appended")
    # self request
    eqh.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(2), "purpose": "ใช้เอง", f"qty_{chr_['id']}": 5, "consent": "1"})
    sr = next(r for r in repo().all("equipment_requests") if r.get("is_self_request"))
    i_sr = repo().where("equipment_request_items", request_id=sr["id"])[0]
    r = eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "approve", f"approve_{i_sr['id']}": 5})
    check(r.status_code == 403, "EQUIP_HEAD cannot approve own request")
    avp.post(f"/equipment/request/{sr['id']}/action", data={"action": "approve", f"approve_{i_sr['id']}": 5})
    check(repo().get("equipment_requests", sr["id"])["status"] == "APPROVED", "ADMIN_VP approves self request")
    eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "issue", "receiver_name": "me"})
    snap(eqh, sr["id"], "RETURN")
    eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "return", f"ret_{i_sr['id']}": 5})
    eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "close"})
    check(repo().get("equipment_requests", sr["id"])["status"] == "RETURNED", "self request needs ADMIN_VP return confirm")
    avp.post(f"/equipment/request/{sr['id']}/action", data={"action": "confirm_return"})
    eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "close"})
    check(repo().get("equipment_requests", sr["id"])["status"] == "CLOSED", "self request closed after confirm")
    for u in ["/equipment/requests", "/equipment/requests?status=OVERDUE", "/equipment/fines", "/equipment/maintenance", "/equipment/manage",
              f"/equipment/model/{spk['id']}"]:
        get_ok(eqh, u)
    eqh.post("/equipment/maintenance", data={"action": "add", "asset_tag": "SPK-002", "issue": "เสียงแตก"})
    check(repo().where("equipment_units", asset_tag="SPK-002")[0]["status"] == "REPAIR", "unit in repair")
    for ds in ("equipment_models", "equipment_requests", "equipment_fines"):
        check(eqh.get(f"/export/{ds}?fmt=xlsx").status_code == 200, f"export {ds}")

    print("== equipment rules set by EQUIP_HEAD")
    get_ok(eqh, "/equipment/settings")
    check(stu.get("/equipment/settings").status_code == 403 and admin.get("/equipment/settings").status_code == 403
          and avp.post("/equipment/settings", data={"action": "rules", "max_loan_days": 30}).status_code == 403,
          "only EQUIP_HEAD sets loan rules (not even SYS_ADMIN)")
    eqh.post("/equipment/settings", data={"action": "rules", "max_loan_days": 0, "late_fine_per_item": 20})
    check(not repo().get("system_settings", "equipment"), "invalid loan rules rejected")
    eqh.post("/equipment/settings", data={"action": "rules", "max_loan_days": 3, "late_fine_per_item": 20, "late_fine_per_day": "1"})
    es = repo().get("system_settings", "equipment")
    check(es["max_loan_days"] == 3 and es["late_fine_per_item"] == 20 and es["late_fine_per_day"], "EQUIP_HEAD saves loan rules")
    cat_html = stu.get("/equipment/").get_data(as_text=True)
    check("ไม่เกิน <strong>3 วัน</strong>" in cat_html and "20 บาท" in cat_html, "borrowers see the rules in the catalog")
    n_req = len(repo().all("equipment_requests"))
    psec.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(6), "purpose": "ยาวเกิน", "consent": "1", f"qty_{chr_['id']}": 1})
    check(len(repo().all("equipment_requests")) == n_req, "loan longer than the maximum is refused")
    psec.post("/equipment/request/new", content_type="multipart/form-data", data={"photos": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"p" * 120), "p.jpg"), "borrow_at": dt(1), "due_at": dt(3), "purpose": "ทดสอบค่าปรับ", "consent": "1",
                                               f"qty_{chr_['id']}": 2})
    lr = [x for x in repo().all("equipment_requests") if x.get("purpose") == "ทดสอบค่าปรับ"][0]
    check(lr["status"] == "PENDING", "loan within the maximum is accepted")
    li = repo().where("equipment_request_items", request_id=lr["id"])[0]
    eqh.post(f"/equipment/request/{lr['id']}/action", data={"action": "approve", f"qty_{li['id']}": 2})
    eqh.post(f"/equipment/request/{lr['id']}/action", data={"action": "issue", "receiver_name": "x", "id_checked": "1"})
    check(repo().get("equipment_requests", lr["id"])["status"] == "ISSUED", "loan issued")
    from app.utils import to_iso as _to_iso
    repo().update("equipment_requests", lr["id"], {"due_at": _to_iso(now() - timedelta(days=2, hours=1))})
    snap(psec, lr["id"], "RETURN")
    eqh.post(f"/equipment/request/{lr['id']}/action", data={"action": "return", f"ret_{li['id']}": 2})
    lf = repo().where("equipment_fines", request_id=lr["id"])
    check(len(lf) == 1 and lf[0]["type"] == "LATE" and lf[0]["amount"] == 60 and lf[0]["status"] == "PENDING",
          "late return auto-records fine: 1 item x 20 baht x 3 days")
    # ข้อความจากฝ่ายอุปกรณ์ + QR รับเงิน + แจ้งชำระด้วยสลิป
    eqh.post("/equipment/settings", content_type="multipart/form-data",
             data={"action": "payment", "pay_name": "สโมสรนักศึกษา", "pay_note": "โอนแล้วแนบสลิป", "pay_qr": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"q" * 90), "qr.png")})
    es = repo().get("system_settings", "equipment")
    check(es.get("pay_qr_path") and es["max_loan_days"] == 3, "payment QR saved without losing the loan rules")
    check(stu.post("/equipment/settings", data={"action": "payment", "pay_name": "x"}).status_code == 403, "only EQUIP_HEAD sets the payment QR")
    check(psec.post(f"/equipment/request/{lr['id']}/action", data={"action": "feedback", "message": "x"}).status_code == 403, "borrower cannot send feedback")
    eqh.post(f"/equipment/request/{lr['id']}/action", data={"action": "feedback", "message": "คืนช้า 3 วัน กรุณาชำระค่าปรับ", "with_qr": "1"})
    pg = psec.get(f"/equipment/request/{lr['id']}").get_data(as_text=True)
    check("คืนช้า 3 วัน กรุณาชำระค่าปรับ" in pg and "/equipment/pay-qr" in pg and "60.00 บาท" in pg and "สโมสรนักศึกษา" in pg,
          "borrower sees feedback, payment QR and the amount to pay")
    check(psec.get("/equipment/pay-qr").status_code == 200 and app.test_client().get("/equipment/pay-qr").status_code == 302, "payment QR needs login")

    def slip(c, fill):
        return c.post(f"/equipment/request/{lr['id']}/action", content_type="multipart/form-data",
                      data={"action": "slip", "fine_id": lf[0]["id"], "slip": (io.BytesIO(b"\xff\xd8\xff\xe0" + fill * 80), "s.jpg")})
    check(slip(stu, b"a").status_code == 403, "only the borrower submits the slip")
    slip(psec, b"a")
    f1 = repo().get("equipment_fines", lf[0]["id"])
    check(f1["slip_status"] == "SUBMITTED" and f1["status"] == "PENDING", "slip submitted, fine waits for review")
    check(psec.get(f"/equipment/slip/{f1['id']}").status_code == 200 and eqh.get(f"/equipment/slip/{f1['id']}").status_code == 200
          and stu.get(f"/equipment/slip/{f1['id']}").status_code == 403, "slip visible to borrower and EQUIP_HEAD only")
    eqh.post(f"/equipment/fine/{f1['id']}", data={"status": "SLIP_REJECT", "note": "ยอดไม่ครบ"})
    f1 = repo().get("equipment_fines", lf[0]["id"])
    check(f1["slip_status"] == "REJECTED" and not f1["slip_path"] and "ยอดไม่ครบ" in psec.get(f"/equipment/request/{lr['id']}").get_data(as_text=True),
          "EQUIP_HEAD rejects a slip with a reason")
    slip(psec, b"b")
    eqh.post(f"/equipment/fine/{lf[0]['id']}", data={"status": "PAID"})
    f1 = repo().get("equipment_fines", lf[0]["id"])
    check(f1["status"] == "PAID" and f1["slip_status"] == "CONFIRMED", "EQUIP_HEAD confirms the payment")
    psec_id = repo().where("users", student_code="406765002")[0]["id"]
    titles = [n["title"] for n in repo().where("notifications", user_id=psec_id)]
    check(all(any(k in t for t in titles) for k in ["ได้รับอนุมัติ", "รับอุปกรณ์แล้ว", "บันทึกรับคืน", "ค่าปรับคืนอุปกรณ์ล่าช้า", "ข้อความจากฝ่ายอุปกรณ์", "ไม่ผ่าน", "ยืนยันรับชำระแล้ว"]),
          "borrower is notified at every step")
    eqh_id = repo().where("users", student_code="406765005")[0]["id"]
    titles = [n["title"] for n in repo().where("notifications", user_id=eqh_id)]
    check(all(any(k in t for t in titles) for k in ["คำขอยืมอุปกรณ์ใหม่", "ส่งรูปก่อนยืม", "ส่งรูปตอนคืน", "แจ้งชำระเงินแล้ว"]), "EQUIP_HEAD is notified of borrower actions")
    eqh.post(f"/equipment/request/{lr['id']}/action", data={"action": "close"})
    check(repo().get("equipment_requests", lr["id"])["status"] == "CLOSED", "loan closes after the fine is settled")
    # ลบอุปกรณ์/รายชิ้น/หมวด
    eqh.post("/equipment/settings", data={"action": "category", "name": "หมวดทดสอบ"})
    tc = repo().where("equipment_categories", name="หมวดทดสอบ")[0]
    eqh.post("/equipment/manage", data={"category_id": tc["id"], "code": "TMP", "name": "ของชั่วคราว", "is_serialized": "1", "is_active": "1"})
    tmp_m = repo().where("equipment_models", code="TMP")[0]
    eqh.post("/equipment/settings", data={"action": "delete_category", "category_id": tc["id"]})
    check(repo().get("equipment_categories", tc["id"]), "category with equipment cannot be deleted")
    eqh.post(f"/equipment/model/{tmp_m['id']}", data={"action": "add_unit", "asset_tag": "TMP-1"})
    tu = repo().where("equipment_units", asset_tag="TMP-1")[0]
    check(stu.post(f"/equipment/model/{tmp_m['id']}", data={"action": "delete"}).status_code == 403, "others cannot delete equipment")
    eqh.post(f"/equipment/model/{tmp_m['id']}", data={"action": "delete_unit", "unit_id": tu["id"]})
    check(not repo().get("equipment_units", tu["id"]), "EQUIP_HEAD deletes an unused unit")
    eqh.post(f"/equipment/model/{tmp_m['id']}", data={"action": "delete"})
    check(not repo().get("equipment_models", tmp_m["id"]), "never-borrowed equipment is deleted for real")
    eqh.post(f"/equipment/model/{chr_['id']}", data={"action": "delete"})
    check(repo().get("equipment_models", chr_["id"])["is_active"] is False, "borrowed equipment is deactivated, history kept")
    repo().update("equipment_models", chr_["id"], {"is_active": True})
    eqh.post("/equipment/settings", data={"action": "delete_category", "category_id": tc["id"]})
    check(not repo().get("equipment_categories", tc["id"]), "empty category deleted")
    eqh.post("/equipment/settings", data={"action": "rules", "max_loan_days": 7, "late_fine_per_item": 0})

    print("== tickets (TKT)")
    get_ok(stu, "/tickets/new")
    stu.post("/tickets/new", data={"category_id": "GENERAL", "title": "แอร์เสีย", "detail": "ห้อง 201"})
    t1 = repo().where("tickets", title="แอร์เสีย")[0]
    check(t1["handler_roles"] == ["ADMIN_SEC", "PRES_SEC"], "normal routing")
    other.post("/tickets/new", data={"category_id": "GENERAL", "title": "x", "detail": "x"})
    check(not repo().where("tickets", title="x"), "OTHER_MRU cannot submit ticket")
    get_ok(psec, f"/tickets/{t1['id']}", "handler views ticket")
    psec.post(f"/tickets/{t1['id']}/action", data={"action": "claim"})
    asec.post(f"/tickets/{t1['id']}/action", data={"action": "claim"})
    check(repo().get("tickets", t1["id"])["handler_id"] == repo().where("users", student_code="406765002")[0]["id"], "first claimer owns")
    asec.post(f"/tickets/{t1['id']}/action", data={"action": "message", "body": "ตอบ"})
    check(not [m for m in repo().where("ticket_messages", ticket_id=t1["id"]) if not m.get("is_internal")], "non-owner handler cannot reply")
    psec.post(f"/tickets/{t1['id']}/action", data={"action": "message", "body": "โน้ตภายใน", "is_internal": "1"})
    psec.post(f"/tickets/{t1['id']}/action", data={"action": "message", "body": "กำลังซ่อม"})
    r = stu.get(f"/tickets/{t1['id']}")
    check("กำลังซ่อม" in r.get_data(as_text=True) and "โน้ตภายใน" not in r.get_data(as_text=True), "submitter sees reply but not internal note")
    check("โน้ตภายใน" in avp.get(f"/tickets/{t1['id']}").get_data(as_text=True), "ADMIN_VP sees internal note (view only)")
    avp.post(f"/tickets/{t1['id']}/action", data={"action": "claim"})
    check(repo().get("tickets", t1["id"])["handler_id"] != repo().where("users", student_code="406765003")[0]["id"], "ADMIN_VP cannot claim")
    psec.post(f"/tickets/{t1['id']}/action", data={"action": "close"})
    check(repo().get("tickets", t1["id"])["status"] != "CLOSED", "close requires reason")
    psec.post(f"/tickets/{t1['id']}/action", data={"action": "close", "reason": "ซ่อมแล้ว"})
    check(repo().get("tickets", t1["id"])["status"] == "CLOSED", "closed")
    stu.post(f"/tickets/{t1['id']}/action", data={"action": "rate", "score": 5})
    check(repo().get("ticket_ratings", t1["id"])["score"] == 5, "rated")
    stu.post(f"/tickets/{t1['id']}/action", data={"action": "reopen"})
    check(repo().get("tickets", t1["id"])["status"] == "IN_PROGRESS", "reopened within 7 days")
    # complaint against ADMIN_SEC, anonymous (SRS v3: ไม่เก็บตัวตนผู้ส่ง ใช้รหัสติดตาม + PIN)
    stu_id = repo().where("users", student_code="406865010")[0]["id"]
    r = stu.post("/tickets/new", data={"category_id": "COMPLAINT", "title": "ร้องเรียนเลขา", "detail": "...", "is_anonymous": "1",
                                       "target_position_ids": ["ADMIN_SEC"], "files": (io.BytesIO(b"x" * 50), "a.txt")},
                 content_type="multipart/form-data")
    t2 = repo().where("tickets", title="ร้องเรียนเลขา")[0]
    html = r.get_data(as_text=True)
    pin = re.search(r"PIN</div><div[^>]*>(\d{6})<", html)
    check(r.status_code == 200 and t2["tracking_code"] in html and pin, "anonymous ticket shows tracking code and PIN once")
    pin = pin.group(1)
    check(t2["submitter_id"] is None and pin not in str(t2) and not t2["files"], "anonymous ticket stores no submitter, no PIN, no files")
    check(t2["created_at"].endswith("T00:00:00+07:00"), "anonymous created_at rounded to the day")
    check(not repo().where("consents", ref=t2["id"]) and stu_id not in str(repo().where("audit_logs", entity_id=t2["id"])),
          "no consent or audit row links the submitter")
    check(t2["handler_roles"] == ["PRES_SEC"] and "ADMIN_SEC" in t2["hidden_roles"], "routing when ADMIN_SEC targeted")
    check(asec.get(f"/tickets/{t2['id']}").status_code == 403, "hidden from target")
    check("ร้องเรียนเลขา" not in asec.get("/tickets/").get_data(as_text=True), "hidden from target queue")
    check("ร้องเรียนเลขา" not in app.test_client().get(f"/tickets/{t2['id']}").get_data(as_text=True), "guest without PIN cannot read")
    r = psec.get(f"/tickets/{t2['id']}")
    check(r.status_code == 200 and "นิรนาม" in r.get_data(as_text=True) and "406865010" not in r.get_data(as_text=True), "handler sees anonymous")
    anon = app.test_client()  # ผู้ส่งกลับมาดูโดยไม่ล็อกอิน
    get_ok(anon, "/tickets/anon")
    anon.post("/tickets/anon", data={"code": t2["tracking_code"], "pin": "000000" if pin != "000000" else "111111"})
    check(anon.get(f"/tickets/{t2['id']}").status_code == 302, "wrong PIN gives no access")
    r = anon.post("/tickets/anon", data={"code": t2["tracking_code"], "pin": pin})
    check(r.status_code == 302 and anon.get(f"/tickets/{t2['id']}").status_code == 200, "tracking code + PIN opens the ticket")
    anon.post(f"/tickets/{t2['id']}/action", data={"action": "message", "body": "ข้อมูลเพิ่ม"})
    msg = repo().where("ticket_messages", ticket_id=t2["id"])[0]
    check(msg["sender_id"] is None and msg["body"] == "ข้อมูลเพิ่ม", "anonymous owner replies without identity")
    t_lock = dict(t2)
    for _ in range(5):
        app.test_client().post("/tickets/anon", data={"code": t2["tracking_code"], "pin": "999999" if pin != "999999" else "888888"})
    check(repo().get("tickets", t2["id"]).get("pin_locked_until"), "PIN locked after repeated failures")
    locked = app.test_client()
    locked.post("/tickets/anon", data={"code": t2["tracking_code"], "pin": pin})
    check(locked.get(f"/tickets/{t2['id']}").status_code == 302, "correct PIN refused while locked")
    repo().update("tickets", t2["id"], {"pin_locked_until": None})
    for i in range(2):
        stu.post("/tickets/new", data={"category_id": "SUGGESTION", "title": f"นิรนาม {i}", "detail": "...", "is_anonymous": "1"})
    stu.post("/tickets/new", data={"category_id": "SUGGESTION", "title": "นิรนามเกินโควตา", "detail": "...", "is_anonymous": "1"})
    check(not repo().where("tickets", title="นิรนามเกินโควตา") and stu_id not in str(repo().all("ticket_anon_quota")),
          "anonymous daily quota enforced without storing user id")
    # เรื่องร้องเรียนนายกไม่รับผ่านระบบ (TKT-04, TKT-16, TKT-17)
    form_html = stu.get("/tickets/new").get_data(as_text=True)
    check('value="PRES"' not in form_html and "เรื่องร้องเรียนนายกสโมสรไม่รับผ่านระบบนี้" in form_html, "PRES not selectable, notice shown")
    stu.post("/tickets/new", data={"category_id": "COMPLAINT", "title": "ร้องเรียนนายก", "detail": "...", "target_position_ids": ["PRES"]})
    t3 = repo().where("tickets", title="ร้องเรียนนายก")[0]
    check(t3["target_position_ids"] == [] and set(t3["handler_roles"]) == {"ADMIN_SEC", "PRES_SEC"}, "PRES target dropped, normal queue")
    asec.post(f"/tickets/{t3['id']}/action", data={"action": "close_scope"})
    t3 = repo().get("tickets", t3["id"])
    check(t3["status"] == "CLOSED" and t3.get("out_of_scope") and not t3.get("reopen_until"), "close as out of scope (president matter)")
    stu.post("/tickets/new", data={"category_id": "COMPLAINT", "title": "ร้องเรียนสองเลขา", "detail": "...",
                                   "target_position_ids": ["ADMIN_SEC", "PRES_SEC"]})
    t4 = repo().where("tickets", title="ร้องเรียนสองเลขา")[0]
    check(t4["handler_roles"] == ["PRES"] and t4["routing_level"] == "PRESIDENT", "both secretaries targeted -> PRES")
    # escalate ไปนายกเท่านั้น และไม่มีการเปิดตัวตน
    psec.post(f"/tickets/{t2['id']}/action", data={"action": "escalate", "to": "ADVISOR", "reason": "ร้ายแรง"})
    t2 = repo().get("tickets", t2["id"])
    check(t2["routing_level"] == "PRESIDENT" and t2["handler_roles"] == ["PRES"], "escalated to PRES only")
    check(pres.post(f"/tickets/{t2['id']}/action", data={"action": "reveal", "reason": "x"}).status_code == 400
          and "เปิดตัวตน" not in pres.get(f"/tickets/{t2['id']}").get_data(as_text=True), "identity reveal removed")
    check(len(repo().all("ticket_access_logs")) > 0, "ticket access logged")
    get_ok(pres, "/tickets/stats")
    r = pres.get("/export/ticket_stats?fmt=tsv")
    check(r.status_code == 200 and "< 5" in r.get_data(as_text=True), "ticket stats hide small groups")

    print("== documents / reports / cron")
    drive = "https://drive.google.com/file/d/abc/view"
    acvp.post("/documents/", data={"title": "โครงการค่าย", "doc_type": "PROJECT", "division_id": "ACTIVITY",
                                   "access_level": "DIVISION", "url": drive})
    d = repo().where("documents", title="โครงการค่าย")[0]
    r = acvp.get(f"/documents/{d['id']}/open")
    check(r.status_code == 302 and r.headers["Location"] == drive, "owner opens doc link (redirect)")
    check(eqh.get(f"/documents/{d['id']}/open").status_code == 403, "other division cannot see DIVISION doc")
    check(pres.get(f"/documents/{d['id']}/open").status_code == 302, "PRES sees division doc")
    acvp.post("/documents/", data={"title": "x", "division_id": "ACTIVITY", "url": "javascript:alert(1)"})
    acvp.post("/documents/", data={"title": "y", "division_id": "ACTIVITY", "url": "http://insecure.example"})
    check(len(repo().all("documents")) == 1, "non-https link rejected")
    check("โครงการค่าย" not in eqh.get("/documents/").get_data(as_text=True), "doc hidden from other division list")
    get_ok(pres, "/documents/")
    acvp.post("/documents/", data={"action": "delete", "doc_id": d["id"]})
    check(not repo().all("documents"), "creator deletes doc link")

    print("== activity evidence (max 5 images)")
    jpg = b"\xff\xd8\xff\xe0" + b"0" * 200
    files = [(io.BytesIO(jpg), f"e{i}.jpg") for i in range(3)]
    acvp.post(f"/activities/{act['id']}/report", data={"result_summary": "ok", "evidence": files}, content_type="multipart/form-data")
    check(len(repo().where("activity_evidence", activity_id=act["id"])) == 3, "3 evidence images saved")
    files = [(io.BytesIO(jpg), f"f{i}.jpg") for i in range(3)]
    acvp.post(f"/activities/{act['id']}/report", data={"result_summary": "ok", "evidence": files}, content_type="multipart/form-data")
    check(len(repo().where("activity_evidence", activity_id=act["id"])) == 3, "over limit (3+3>5) rejected")
    acvp.post(f"/activities/{act['id']}/report", data={"result_summary": "ok", "evidence": [(io.BytesIO(b"%PDF-1.4"), "a.pdf")]},
              content_type="multipart/form-data")
    check(len(repo().where("activity_evidence", activity_id=act["id"])) == 3, "evidence must be image")
    ev = repo().where("activity_evidence", activity_id=act["id"])[0]
    check(acvp.get(f"/activities/evidence/{ev['id']}").data == jpg, "manager views evidence image")
    check(stu.get(f"/activities/evidence/{ev['id']}").status_code == 403, "student cannot view evidence")
    acvp.post(f"/activities/evidence/{ev['id']}/delete")
    check(len(repo().where("activity_evidence", activity_id=act["id"])) == 2, "evidence deleted")
    big = b"\xff\xd8\xff\xe0" + b"0" * (2 * 1024 * 1024 + 10)
    acvp.post(f"/activities/{act['id']}/report", data={"evidence": [(io.BytesIO(big), "big.jpg")]}, content_type="multipart/form-data")
    check(len(repo().where("activity_evidence", activity_id=act["id"])) == 2, "file over 2 MB rejected")
    get_ok(pres, "/reports/")
    check(stu.get("/reports/").status_code == 403, "student cannot view reports")
    for ds in ("report_activities", "report_hours", "holders", "documents"):
        check(pres.get(f"/export/{ds}?fmt=csv").status_code == 200, f"export {ds}")
    check(pres.get("/export/audit?fmt=csv").status_code == 403, "audit export blocked for PRES")
    check(admin.get("/export/audit?fmt=csv").status_code == 200, "audit export for admin")
    r = pub.get("/cron/daily")
    check(r.status_code == 401, "cron requires secret")
    r = pub.get("/cron/daily", headers={"Authorization": "Bearer cron"})
    check(r.status_code == 200, "cron runs")
    get_ok(stu, "/app/notifications")
    check(len(repo().all("audit_logs")) > 20, "audit logs written")

    print("== team recruitment (TEAM)")
    ph = client_for("406765006")
    guest = app.test_client()  # pub ล็อกอินค้างจากขั้นลงทะเบียนแล้ว
    r = ph.post("/team/manage/new", data={"title": "งานทดสอบทีม", "start_date": "2026-12-01"})
    tev = repo().where("team_events", title="งานทดสอบทีม")[0]
    tid = tev["id"]
    check(r.status_code == 302 and tev["status"] == "WAITING", "create team event (waiting)")
    check(guest.get(f"/team/{tid}").status_code == 404, "waiting event hidden from public")
    ph.post(f"/team/manage/{tid}/status", data={"action": "OPEN"})
    check(repo().get("team_events", tid)["status"] == "WAITING", "cannot open without positions")
    ph.post(f"/team/manage/{tid}/positions", data={"name": "พิธีกร", "division_id": "ADMIN", "quota": "9", "applicant_type": "ALL",
                                                   "type_cap": "1", "comm_quota": "8", "sub_quota": "8", "gender": "ANY"})
    check(not repo().where("team_positions", event_id=tid), "type caps over quota rejected")
    ph.post(f"/team/manage/{tid}/positions", data={"name": "พิธีกร", "division_id": "ADMIN", "quota": "1", "applicant_type": "ALL",
                                                   "gender": "M", "restrict": ["ADMIN"]})
    tp = repo().where("team_positions", event_id=tid)[0]
    ph.post(f"/team/manage/{tid}/status", data={"action": "OPEN"})
    check(repo().get("team_events", tid)["status"] == "OPEN", "open team event")
    ph.post("/team/manage/new", data={"title": "งานวิซาร์ด", "start_date": "2026-12-05", "next_step": "2", "wizard": "1"})
    wid = repo().where("team_events", title="งานวิซาร์ด")[0]["id"]
    ph.post(f"/team/manage/{wid}/finish", data={"open_now": "1"})
    check(repo().get("team_events", wid)["status"] == "WAITING", "wizard cannot finish without positions")
    ph.post(f"/team/manage/{wid}/positions", data={"wizard": "1", "name": "สวัสดิการ", "division_id": "ADMIN", "quota": "2",
                                                   "applicant_type": "ALL", "gender": "ANY", "scope": "limit"})
    check(not repo().where("team_positions", event_id=wid), "limited scope needs at least one division")
    ph.post(f"/team/manage/{wid}/positions", data={"wizard": "1", "name": "สวัสดิการ", "division_id": "ADMIN", "quota": "2",
                                                   "applicant_type": "ALL", "gender": "ANY", "scope": "all", "restrict": ["ADMIN"]})
    check(repo().where("team_positions", event_id=wid)[0]["restrict_divisions"] == [], "scope all clears division limits")
    for st in (1, 2, 3):
        get_ok(ph, f"/team/manage/{wid}/edit?wizard=1&step={st}")
    ph.post(f"/team/manage/{wid}/finish", data={"open_now": "1"})
    check(repo().get("team_events", wid)["status"] == "OPEN", "wizard finish opens event")
    ph.post(f"/team/manage/{wid}/status", data={"action": "delete"})
    check("งานทดสอบทีม" in guest.get("/team/").get_data(as_text=True), "public sees open team event")
    def cov(fill):
        return (io.BytesIO(jpg[:4] + fill * 200), "c.jpg")
    base = {"title": "งานทดสอบทีม", "start_date": "2026-12-01"}
    ph.post(f"/team/manage/{tid}/edit", data=dict(base, cover=cov(b"1")), content_type="multipart/form-data")
    page1 = ph.get(f"/team/manage/{tid}/edit").get_data(as_text=True)
    url1 = page1.split('data-saved="')[1].split('"')[0].replace("&amp;", "&")
    img1 = ph.get(url1).data
    ph.post(f"/team/manage/{tid}/edit", data=dict(base, cover=cov(b"2")), content_type="multipart/form-data")
    url2 = ph.get(f"/team/manage/{tid}/edit").get_data(as_text=True).split('data-saved="')[1].split('"')[0].replace("&amp;", "&")
    check(url1 != url2 and ph.get(url2).data != img1, "replacing cover without ticking remove changes image and its URL")
    ph.post(f"/team/manage/{tid}/edit", data=dict(base, remove_cover="on"))
    check(not repo().get("team_events", tid).get("cover_path"), "remove cover")
    check("กรุณาเข้าสู่ระบบก่อนสมัคร" in get_ok(guest, f"/team/{tid}").get_data(as_text=True), "guest must log in to apply")
    check(guest.post(f"/team/position/{tp['id']}/apply").status_code == 302 and not repo().where("team_applications", event_id=tid),
          "guest cannot apply")
    ph.post(f"/team/position/{tp['id']}/apply", data={})
    check(not repo().where("team_applications", event_id=tid), "gender required for gender-limited position")
    acv = client_for("406765009")
    acv.post(f"/team/position/{tp['id']}/apply", data={"gender": "M"})
    check(not repo().where("team_applications", event_id=tid), "other division blocked by restriction")
    ph.post(f"/team/position/{tp['id']}/apply", data={"gender": "M"})
    tapps = repo().where("team_applications", event_id=tid)
    check(len(tapps) == 1 and tapps[0]["member_type"] == "COMM" and tapps[0]["home_division_id"] == "ADMIN", "member applies")
    ph_id = repo().where("users", student_code="406765006")[0]["id"]
    conf = [n for n in repo().where("notifications", user_id=ph_id) if "สมัครทีมงานสำเร็จ: งานทดสอบทีม" in n["title"]]
    check(len(conf) == 1 and "พิธีกร" in conf[0]["body"], "applicant gets a success confirmation for the application")
    ph.post(f"/team/position/{tp['id']}/apply", data={})
    eqh.post(f"/team/position/{tp['id']}/apply", data={"gender": "M"})
    check(len(repo().where("team_applications", event_id=tid)) == 1, "duplicate and over-quota applications rejected")
    ph.post(f"/team/application/{tapps[0]['id']}/status", data={"action": "APPROVED"})
    check(repo().get("team_applications", tapps[0]["id"])["status"] == "APPROVED", "approve applicant")
    lact = repo().where("activities", title="ค่ายวิชาการ")[0]["id"]
    tsid = f"team_{tapps[0]['id']}"
    check(not repo().get("activity_staff", tsid), "unlinked team event grants no activity staff")
    ph.post(f"/team/manage/{tid}/edit", data={"title": "งานทดสอบทีม", "start_date": "2026-12-01", "activity_id": lact})
    ts = repo().get("activity_staff", tsid) or {}
    check(ts.get("activity_id") == lact and ts.get("confirm_status") == "CONFIRMED", "linking activity makes approved applicant staff")
    check("ผ่านหน้ารับสมัครทีมงาน" in get_ok(acv, f"/activities/{lact}").get_data(as_text=True), "activity page shows linked team event")
    r = acv.get(f"/export/activity_staff?fmt=tsv&activity_id={lact}")
    check(r.status_code == 200 and "พิธีกร" in r.get_data(as_text=True), "staff export includes team-sourced staff")
    ph.post(f"/team/application/{tapps[0]['id']}/status", data={"action": "REJECTED"})
    check(not repo().get("activity_staff", tsid), "rejecting removes activity staff")
    ph.post(f"/team/application/{tapps[0]['id']}/status", data={"action": "APPROVED"})
    check(repo().get("activity_staff", tsid), "re-approving restores activity staff")
    check(f"/activities/{lact}" in ph.get("/team/my").get_data(as_text=True), "my applications link to activity")
    ph.post(f"/team/manage/{tid}/applicants/add", data={"full_name": "คนนอก ทดสอบ", "position_id": tp["id"]})
    check(len(repo().where("team_applications", event_id=tid)) == 2, "admin adds outsider past quota")
    for u in ["/team/manage", "/team/manage/new", f"/team/manage/{tid}/edit", f"/team/manage/{tid}/edit?step=2", f"/team/manage/{tid}/applicants?status=active", f"/team/manage/{tid}/print",
              "/team/my", f"/team/{tid}"]:
        get_ok(ph, u, f"{u} as team manager")
    check("คนนอก ทดสอบ" in ph.get(f"/team/manage/{tid}/list.txt").get_data(as_text=True), "copy list text")
    r = ph.get(f"/export/team_applicants?fmt=tsv&event_id={tid}")
    check(r.status_code == 200 and "คนนอก ทดสอบ" in r.get_data(as_text=True), "export team applicants")
    check(client_for("406111011").get("/team/manage").status_code == 403, "non-manager cannot manage team events")
    check(acv.post(f"/team/manage/{tid}/status", data={"action": "delete"}).status_code == 403, "delete needs team.delete")
    ph.post(f"/team/manage/{tid}/status", data={"action": "delete"})
    check(not repo().get("team_events", tid) and not repo().where("team_applications", event_id=tid), "delete event with its data")
    check(not repo().get("activity_staff", tsid), "deleting event removes linked staff")

    print("== SRS v3: org chart, fallback, advisor, feature flag")
    from app.rbac import holders_of_role
    from app.seed import migrate_v3, run_seed
    run_seed(2569)
    board = [p for p in repo().all("positions") if p.get("level") == "BOARD"]
    check(len(board) == 23 and len(repo().all("divisions")) == 5 and len(repo().all("positions")) == 34,
          "seed is idempotent: 23 board positions, 5 divisions")
    check(repo().get("divisions", "SECRETARY")["name"] == "กองเลขา" and repo().get("positions", "SUB_SECRETARY")["level"] == "SUBCOMMITTEE",
          "secretary division has committee and subcommittee positions")
    repo().delete("divisions", "SECRETARY"); repo().delete("positions", "COMM_SECRETARY"); repo().delete("positions", "SUB_SECRETARY")
    admin.post("/admin/positions", data={"action": "seed_defaults", "year": 2569})
    check(repo().get("divisions", "SECRETARY") and repo().get("positions", "COMM_SECRETARY"), "admin can add missing default divisions/positions")
    check(all(p.get("academic_year") == 2569 and "function_code" in p for p in board), "board chart bound to year with function codes")
    # สิทธิ์สำรองเมื่อตำแหน่งบุคลากรว่าง
    avp_id = repo().where("users", student_code="406765003")[0]["id"]
    check(avp.get("/recruit/new").status_code == 403, "ADMIN_VP has no rec.manage while PERSONNEL_HEAD is held")
    ph_hold = repo().where("position_holders", position_id="PERSONNEL_HEAD", status="ACTIVE")[0]
    admin.post("/admin/holders", data={"action": "end", "holder_id": ph_hold["id"], "year": 2569})
    check(avp.get("/recruit/new").status_code == 200 and asec.get("/recruit/new").status_code == 200,
          "vacant PERSONNEL_HEAD: ADMIN_VP and ADMIN_SEC get rec.manage")
    check(avp_id in holders_of_role("PERSONNEL_HEAD"), "role notifications reach fallback holders")
    check(eqh.get("/recruit/new").status_code == 403, "other board members get no fallback")
    avp.post("/recruit/new", data={"title": "รอบสิทธิ์สำรอง", "type": "COMMITTEE", "academic_year": 2569, "open_at": dt(-1),
                                   "close_at": dt(7), "min_year": 1, "quota_COMM_ADMIN": 1,
                                   "crit_name": ["ก"], "crit_max": [10], "crit_weight": [1]})
    logs = [a for a in repo().where("audit_logs", action="recruit.round_save") if a.get("via_fallback")]
    check(logs and "rec.manage" in logs[-1]["via_fallback"] and "ใช้สิทธิ์สำรอง" in logs[-1]["note"], "audit log marks fallback use")
    check("ใช้ผู้สำรอง" in admin.get("/admin/holders").get_data(as_text=True), "holders page shows vacancy fallback")
    admin.post("/admin/holders", data={"action": "add", "identifier": "406765006", "position_id": "PERSONNEL_HEAD", "year": 2569})
    check(avp.get("/recruit/new").status_code == 403 and ph.get("/recruit/new").status_code == 200, "rights return when position is filled")
    # ผังรายปี
    get_ok(admin, "/admin/positions")
    admin.post("/admin/years", data={"action": "add", "year": 2570, "start_date": "2027-06-01", "end_date": "2028-05-31"})
    admin.post("/admin/positions", data={"action": "copy_chart", "year": 2569, "to_year": 2570})
    c70 = [p for p in repo().all("positions") if p.get("academic_year") == 2570]
    check(len(c70) == 23 and repo().get("positions", "ADMIN_SEC-2570")["parent_id"] == "ADMIN_VP-2570", "copy chart to next year keeps hierarchy")
    admin.post("/admin/positions", data={"action": "position", "year": 2570, "position_id": "PERSONNEL_HEAD-2570", "name": "หัวหน้าฝ่ายบุคลากรและสวัสดิการ",
                                         "level": "BOARD", "division_id": "ADMIN", "function_code": "PERSONNEL_HEAD", "single_holder": "1"})
    check(repo().get("positions", "PERSONNEL_HEAD-2570")["name"].endswith("สวัสดิการ") and repo().get("positions", "PERSONNEL_HEAD")["name"] == "หัวหน้าฝ่ายบุคลากร",
          "editing next year's chart leaves the current chart untouched")
    admin.post("/admin/positions", data={"action": "position", "year": 2569, "position_id": "EQUIP_HEAD", "name": "หัวหน้าฝ่ายอุปกรณ์และสถานที่",
                                         "level": "BOARD", "division_id": "ADMIN", "function_code": "", "single_holder": "1"})
    check("EQUIP_HEAD" in repo().get("positions", "EQUIP_HEAD")["roles"], "locked position keeps its function")
    check("ADMIN_VP-2570" not in [p["id"] for p in repo().all("positions") if p.get("academic_year") in (None, 2569)]
          and 'value="ADMIN_VP-2570"' not in admin.get("/admin/holders?year=2569").get_data(as_text=True), "holders page lists only that year's chart")
    # อาจารย์ที่ปรึกษา: Dashboard สรุปอย่างเดียว
    adv_id = repo().add("users", {"email": "advisor@demo.local", "first_name": "อาจารย์", "last_name": "ที่ปรึกษา", "account_type": "EXTERNAL",
                                  "status": "ACTIVE", "system_roles": [], "failed_logins": 0, "created_at": "2026-01-01T00:00:00+07:00",
                                  "password_hash": repo().where("users", student_code="406765001")[0]["password_hash"]})
    admin.post("/admin/holders", data={"action": "add", "identifier": "advisor@demo.local", "position_id": "ADVISOR", "year": 2569})
    adv = client_for("advisor@demo.local")
    r = get_ok(adv, "/reports/advisor", "advisor dashboard")
    body = r.get_data(as_text=True)
    check("406865010" not in body and "ร้องเรียน" in body and "ร้องเรียนเลขา" not in body, "advisor dashboard has totals only, no names or content")
    for u in ["/reports/", "/tickets/stats", "/documents/", "/admin/audit", f"/tickets/{t2['id']}", "/equipment/manage"]:
        check(adv.get(u).status_code in (302, 403), f"advisor blocked from {u}")
    check(adv.get("/export/ticket_stats?fmt=tsv").status_code == 403 and adv.get("/export/holders?fmt=tsv").status_code == 403,
          "advisor cannot export detailed data")
    check(stu.get("/reports/advisor").status_code == 403, "advisor dashboard needs dashboard.advisor")
    # ประกาศ: ทีมสื่อร่างได้อย่างเดียว
    repo().add("position_holders", {"user_id": stu_id, "position_id": "PHOTO_HEAD", "position_code": "PHOTO_HEAD", "academic_year": 2569,
                                    "start_date": "2026-06-01", "end_date": None, "status": "ACTIVE", "source": "TEST", "created_at": "x"})
    photo = client_for("406865010")
    photo.post("/manage/announcements/new", data={"title": "ร่างจากทีมสื่อ", "body": "x", "category_id": "NEWS", "is_pinned": "1"})
    dr = repo().where("announcements", title="ร่างจากทีมสื่อ")[0]
    check(dr["status"] == "DRAFT" and not dr["is_pinned"], "media team creates draft only")
    check(photo.post(f"/manage/announcements/{dr['id']}/status", data={"action": "publish"}).status_code == 403, "media team cannot publish")
    pr.post(f"/manage/announcements/{dr['id']}/status", data={"action": "publish"})
    check(repo().get("announcements", dr["id"])["status"] == "PUBLISHED", "PR_VP publishes the draft")
    check(photo.get(f"/manage/announcements/{dr['id']}/edit").status_code == 403, "media team cannot edit published item")
    for hh in repo().where("position_holders", user_id=stu_id, position_id="PHOTO_HEAD"):
        repo().delete("position_holders", hh["id"])
    # อุปกรณ์: ชื่อกิจกรรมเป็นข้อความ
    check("event_name" in get_ok(stu, "/equipment/request/new").get_data(as_text=True) and 'name="activity_id"' not in stu.get("/equipment/request/new").get_data(as_text=True),
          "equipment request uses free-text event name")
    # เอกสาร: บอร์ดในกองบันทึกเอกสารของกองตนได้
    eqh.post("/documents/", data={"title": "ทะเบียนครุภัณฑ์", "doc_type": "OTHER", "division_id": "ADMIN", "access_level": "DIVISION",
                                  "url": "https://drive.google.com/file/d/eq/view"})
    check(repo().where("documents", title="ทะเบียนครุภัณฑ์"), "board member records document for own division")
    eqh.post("/documents/", data={"title": "ข้ามกอง", "division_id": "PR", "url": "https://drive.google.com/file/d/x/view"})
    check(not repo().where("documents", title="ข้ามกอง"), "board member cannot record for another division")
    # ปิดโมดูลกิจกรรม
    app.config["ACTIVITIES_ENABLED"] = False
    check(stu.get("/activities/").status_code == 404 and acvp.get("/activities/new").status_code == 404, "activities module off -> 404")
    home = stu.get("/").get_data(as_text=True)
    check("/activities/" not in home and "/team/" in home, "activity links hidden, team recruitment kept")
    for u in ["/app/", "/reports/", "/documents/", "/team/manage/new", "/admin/permissions", "/tickets/"]:
        get_ok(pres if u == "/reports/" else (admin if u.startswith("/admin") else ph), u, f"{u} with activities off")
    check("act.manage" not in admin.get("/admin/permissions").get_data(as_text=True), "activity permissions hidden when off")
    check(avp.get("/admin/delegations").status_code == 404 and 'name="activity_id"' not in ph.get("/team/manage/new").get_data(as_text=True),
          "delegation page and team-activity link hidden when off")
    app.config["ACTIVITIES_ENABLED"] = True

    print("== SRS v3: migration from the pre-v3 structure")
    r_ = repo()
    r_.set("divisions", "SPORT", {"code": "SPORT", "name": "กองกีฬา"})
    r_.set("positions", "SPORT_VP", {"code": "SPORT_VP", "name": "อุปนายกกองกีฬา", "level": "BOARD", "division_id": "SPORT",
                                     "roles": ["BOARD", "DIV_VP"], "single_holder": True, "locked_required": False})
    r_.set("positions", "PR_HEAD", {"code": "PR_HEAD", "name": "หัวหน้าฝ่ายประชาสัมพันธ์", "level": "BOARD", "division_id": "ADMIN",
                                    "roles": ["BOARD", "PR"], "single_holder": False, "locked_required": False})
    u7id = repo().where("users", student_code="406765007")[0]["id"]
    h1 = r_.add("position_holders", {"user_id": u7id, "position_id": "SPORT_VP", "position_code": "SPORT_VP", "academic_year": 2569, "status": "ACTIVE"})
    h2 = r_.add("position_holders", {"user_id": u7id, "position_id": "PR_HEAD", "position_code": "PR_HEAD", "academic_year": 2569, "status": "ACTIVE"})
    tp_old = r_.add("team_positions", {"event_id": "x", "name": "กรรมการผู้ตัดสิน", "division_id": "SPORT", "restrict_divisions": ["SPORT", "ADMIN"]})
    tk_old = r_.add("tickets", {"tracking_code": "TOLD", "is_anonymous": True, "submitter_id": u7id, "status": "SUBMITTED", "category_id": "GENERAL",
                                "title": "เก่า", "handler_roles": [], "viewer_roles": []})
    eq_old = r_.add("equipment_requests", {"requester_id": u7id, "activity_id": act["id"], "status": "CLOSED"})
    r_.delete("system_settings", "migrations")
    check("ย้ายเป็นผังบอร์ด 69" in admin.get("/admin/positions").get_data(as_text=True), "positions page offers the v3 migration")
    admin.post("/admin/positions", data={"action": "migrate_v3"})
    check(r_.get("position_holders", h1)["position_id"] == "SPORT_HEAD" and r_.get("position_holders", h1)["status"] == "ACTIVE",
          "old division VP moved to the same-named unit head")
    check(r_.get("position_holders", h2)["status"] == "ENDED", "holder of a removed position is ended")
    check(not r_.get("positions", "SPORT_VP") and not r_.get("divisions", "SPORT"), "old positions and divisions removed")
    tpm = r_.get("team_positions", tp_old)
    check(tpm["division_id"] == "ACTIVITY" and tpm["unit_name"] == "กีฬา" and tpm["restrict_divisions"] == ["ACTIVITY", "ADMIN"],
          "team position moved to the activity division, unit name kept")
    check(r_.get("tickets", tk_old)["submitter_id"] is None, "existing anonymous tickets lose submitter id")
    eqm = r_.get("equipment_requests", eq_old)
    check(eqm["event_name"] == act["title"] and eqm["activity_id"] is None, "equipment request keeps event name as text")
    check("ย้ายเป็นผังบอร์ด 69 (v3)" not in admin.get("/admin/positions").get_data(as_text=True), "migration banner gone after migrating")
    check(migrate_v3(2569)["holders_moved"] == 0, "migration is repeatable")
    r_.delete("team_positions", tp_old)

    print("== LINE OA notifications (NTF-02..09, simulated LINE API)")
    import base64, hashlib, hmac as _hmac, json as _json
    from app import line as line_mod
    check(stu.get("/line/settings").status_code == 200 and "ยังไม่ได้เปิดใช้" in stu.get("/line/settings").get_data(as_text=True),
          "LINE settings page explains when LINE is off")
    check(app.test_client().post("/line/webhook", data="{}").status_code == 404, "webhook closed when LINE is not configured")
    sent, quota = [], {"limit": 200, "used": 10, "push_status": 200}

    def fake_line(method, path, payload):
        if path == "/message/quota":
            return 200, {"type": "limited", "value": quota["limit"]}
        if path == "/message/quota/consumption":
            return 200, {"totalUsage": quota["used"]}
        sent.append((path, payload))
        return (quota["push_status"] if path == "/message/push" else 200), {}

    app.config.update(LINE_CHANNEL_ACCESS_TOKEN="tok", LINE_CHANNEL_SECRET="sec", LINE_ADD_FRIEND_URL="https://line.me/R/ti/p/@demo",
                      LINE_TRANSPORT=fake_line, SITE_URL="https://sta.example")

    def hook(events, secret="sec"):
        raw = _json.dumps({"events": events}).encode()
        sig = base64.b64encode(_hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        return app.test_client().post("/line/webhook", data=raw, headers={"X-Line-Signature": sig, "Content-Type": "application/json"})

    def say(text, who="Ustu"):
        return hook([{"type": "message", "replyToken": "rt", "source": {"userId": who}, "message": {"type": "text", "text": text}}])

    check(hook([], secret="wrong").status_code == 401, "webhook rejects a bad signature")
    check("รับแจ้งเตือนทาง LINE" in stu.get("/app/").get_data(as_text=True), "dashboard shows LINE banner")
    stu.post("/line/settings", data={"action": "code", "ev_EQP_DECISION": "1"})
    check(not repo().all("line_link_codes"), "link code needs PDPA consent")
    r = stu.post("/line/settings", data={"action": "code", "consent": "1", "ev_EQP_DECISION": "1", "ev_TICKET_REPLY": "1"}, follow_redirects=True)
    code = re.search(r"font-monospace\">(\d{6})<", r.get_data(as_text=True)).group(1)
    stored = repo().all("line_link_codes")[0]
    check(code not in str(stored) and stored["user_id"] == stu_id, "link code stored as hash only")
    check(code not in ph.get("/line/settings").get_data(as_text=True), "code shown only in the requesting session")
    say("000000" if code != "000000" else "111111")
    check(not line_mod.link_of(stu_id) and "ไม่ถูกต้อง" in sent[-1][1]["messages"][0]["text"], "wrong code is refused by reply")
    for _ in range(5):
        say("999999" if code != "999999" else "888888", who="Uattacker")
    say(code, who="Uattacker")
    check(not line_mod.link_of(stu_id), "LINE account locked after repeated wrong codes")
    say(code)
    lk = line_mod.link_of(stu_id)
    check(lk and lk["line_user_id"] == "Ustu" and lk["status"] == "ACTIVE" and sent[-1][0] == "/message/reply", "code in chat links the account, confirmed by reply")
    check(not repo().all("line_link_codes"), "code is single use")
    check("ผูกบัญชีแล้ว" in stu.get("/line/settings").get_data(as_text=True) and "รับแจ้งเตือนทาง LINE</strong>" not in stu.get("/app/").get_data(as_text=True),
          "settings show linked state and banner disappears")
    sent.clear()
    from app.services import notify as _notify
    with app.test_request_context():
        _notify(stu_id, "คำขอยืมได้รับอนุมัติ", "ข้อความลับภายใน", "/equipment/requests/1", event="EQP_DECISION")
        _notify(stu_id, "ผลคัดเลือก", "x", "/recruit/my", event="RECRUIT")
    pushes = [x for x in sent if x[0] == "/message/push"]
    check(len(pushes) == 1 and pushes[0][1]["to"] == "Ustu", "push only for event types the user opted into")
    txt = pushes[0][1]["messages"][0]["text"]
    check("ข้อความลับภายใน" not in txt and "https://sta.example/equipment/requests/1" in txt, "LINE text is generic with a link, no item content")
    sent.clear()
    from app.views.equipment import eqp_notify
    with app.test_request_context():
        eqp_notify(stu_id, "รับอุปกรณ์แล้ว (EQTEST)", "รายละเอียดภายใน", "/equipment/request/1")
    pushes = [x for x in sent if x[0] == "/message/push"]
    check(len(pushes) == 1 and "ยืม-คืนอุปกรณ์: รับอุปกรณ์แล้ว (EQTEST)" in pushes[0][1]["messages"][0]["text"]
          and "รายละเอียดภายใน" not in pushes[0][1]["messages"][0]["text"], "every equipment event pushes a short LINE message")
    check(repo().where("notification_deliveries", user_id=stu_id, status="SENT"), "delivery recorded")
    say("เช็กสถานะคำขอยืม")
    check("คำขอยืม" in sent[-1][1]["messages"][0]["text"] and sent[-1][0] == "/message/reply", "rich menu answered by reply")
    say("คำร้องของฉัน", who="Unobody")
    check("ยังไม่ได้ผูกบัญชี" in sent[-1][1]["messages"][0]["text"], "menu asks unlinked LINE users to link first")
    # โควตาใกล้เต็ม -> หยุดส่ง ใช้อีเมลแทน และแจ้งผู้ดูแลระบบ
    quota["used"] = 185
    repo().delete("system_settings", "line_quota")
    sent.clear()
    with app.test_request_context():
        _notify(stu_id, "คำขอยืมถูกปฏิเสธ", "x", "/equipment/requests/1", event="EQP_DECISION")
    check(not [x for x in sent if x[0] == "/message/push"] and repo().where("notification_deliveries", user_id=stu_id, status="FAILED", error="quota"),
          "stops pushing when quota is nearly used")
    check(any("โควตา" in n["title"] for n in repo().all("notifications")), "admin warned about LINE quota")
    check("185" in admin.get("/line/settings").get_data(as_text=True), "admin sees quota usage")
    quota["used"] = 10
    repo().delete("system_settings", "line_quota")
    # ผู้ใช้บล็อก OA
    hook([{"type": "unfollow", "source": {"userId": "Ustu"}}])
    check(line_mod.link_of(stu_id)["status"] == "BLOCKED" and not line_mod.wants(stu_id, "EQP_DECISION"), "blocking the OA deactivates the link")
    hook([{"type": "follow", "replyToken": "rt", "source": {"userId": "Ustu"}}])
    check(line_mod.link_of(stu_id)["status"] == "ACTIVE", "unblocking reactivates the link")
    # เรื่องนิรนามไม่แจ้งทาง LINE
    sent.clear()
    psec.post(f"/tickets/{t2['id']}/action", data={"action": "message", "body": "x", "is_internal": "1"})
    check(not [x for x in sent if x[0] == "/message/push"], "anonymous tickets never push to LINE")
    # สรุปรายการรออนุมัติ
    repo().set("line_links", avp_id, {"user_id": avp_id, "line_user_id": "Uavp", "status": "ACTIVE", "linked_at": "x"})
    repo().set("notification_preferences", avp_id, {"line": {"APPROVAL": True}})
    rq = repo().add("approval_requests", {"flow_code": "REC_FINAL", "subject_type": "t", "subject_id": "1", "title": "ทดสอบ", "link": "/",
                                          "status": "PENDING", "cycle": 1, "requested_by": None, "created_at": "x"})
    sent.clear()
    app.test_client().get("/cron/daily", headers={"Authorization": "Bearer cron"})
    dig = [x for x in sent if x[0] == "/message/push" and x[1]["to"] == "Uavp"]
    check(len(dig) == 1 and "รออนุมัติ" in dig[0][1]["messages"][0]["text"], "approvers get one daily digest message")
    repo().delete("approval_requests", rq)
    repo().set("notification_preferences", stu_id, {"line": {"TEAM": True}})
    sent.clear()
    with app.test_request_context():
        _notify(stu_id, "สมัครทีมงานสำเร็จ: งานรับน้อง", "ตำแหน่ง พิธีกร", "/team/my", event="TEAM", line_text="สมัครทีมงานสำเร็จ: งานรับน้อง · ตำแหน่ง พิธีกร")
    tp_push = [x for x in sent if x[0] == "/message/push"]
    check(len(tp_push) == 1 and "สมัครทีมงานสำเร็จ: งานรับน้อง" in tp_push[0][1]["messages"][0]["text"], "team application confirmation is pushed to LINE")
    stu.post("/line/settings", data={"action": "unlink"})
    check(not line_mod.link_of(stu_id) and not repo().get("notification_preferences", stu_id), "unlink deletes LINE user id and preferences")
    app.config.update(LINE_CHANNEL_ACCESS_TOKEN="", LINE_CHANNEL_SECRET="", LINE_TRANSPORT=None)

    print("== home banners")
    home0 = app.test_client().get("/").get_data(as_text=True)
    check("/static/img/banners/" in home0, "home shows default banners until one is uploaded")
    get_ok(admin, "/manage/banners")
    check(stu.get("/manage/banners").status_code == 403, "student cannot manage banners")
    admin.post("/manage/banners", data={"action": "add", "title": "งานรับน้อง", "link": "javascript:alert(1)",
                                        "image": [(io.BytesIO(jpg), "b1.jpg"), (io.BytesIO(jpg[:4] + b"2" * 300), "b2.jpg")]},
               content_type="multipart/form-data")
    bn = sorted(repo().all("home_banners"), key=lambda b: b["order"])
    check(len(bn) == 2 and bn[0]["link"] == "" and bn[0]["title"] == "งานรับน้อง", "upload banners, unsafe link dropped")
    home1 = app.test_client().get("/").get_data(as_text=True)
    check(f"/banner/{bn[0]['id']}" in home1 and "/static/img/banners/" not in home1, "home uses uploaded banners")
    check(app.test_client().get(f"/banner/{bn[0]['id']}").data == jpg, "banner image is public")
    admin.post("/manage/banners", data={"action": "down", "banner_id": bn[0]["id"]})
    check(repo().get("home_banners", bn[0]["id"])["order"] == 2 and repo().get("home_banners", bn[1]["id"])["order"] == 1, "reorder banners")
    admin.post("/manage/banners", data={"action": "save", "banner_id": bn[0]["id"], "title": "x", "link": "/news"})
    check(f"/banner/{bn[0]['id']}" not in app.test_client().get("/").get_data(as_text=True)
          and repo().get("home_banners", bn[0]["id"])["link"] == "/news", "hidden banner not shown, internal link kept")
    for b in bn:
        admin.post("/manage/banners", data={"action": "delete", "banner_id": b["id"]})
    check(not repo().all("home_banners") and "/static/img/banners/" in app.test_client().get("/").get_data(as_text=True),
          "deleting all banners restores the defaults")

    print("== organization chart")
    guest2 = app.test_client()
    check("ยังไม่มีข้อมูล" in get_ok(guest2, "/org/").get_data(as_text=True), "public org page, empty state")
    check(guest2.get("/org/manage").status_code == 302 and stu.get("/org/manage").status_code == 403
          and pres.get("/org/manage").status_code == 403, "only the two secretaries can edit the org chart")
    get_ok(psec, "/org/manage")
    get_ok(asec, "/org/manage")
    asec.post("/org/manage", data={"action": "save", "year": 2569, "first_name": "สมหญิง", "last_name": "ใจงาม", "position_id": "PRES",
                                   "program_code": "65", "student_code": "406765099", "photo": (io.BytesIO(jpg), "p.jpg")},
              content_type="multipart/form-data")
    om = repo().where("org_members", first_name="สมหญิง")[0]
    page = guest2.get("/org/").get_data(as_text=True)
    check(all(x in page for x in ["สมหญิง ใจงาม", "นายกสโมสร", "เทคโนโลยีสารสนเทศ", "406765099", f"/org/photo/{om['id']}"]),
          "public page shows name, position, program, student code and photo")
    check(guest2.get(f"/org/photo/{om['id']}").data == jpg, "org photo is public")
    asec.post("/org/manage", data={"action": "save", "year": 2569, "first_name": "x", "last_name": "y", "position_id": "NOPE"})
    asec.post("/org/manage", data={"action": "save", "year": 2569, "first_name": "x", "last_name": "y", "position_id": "PRES", "student_code": "abc"})
    check(len(repo().all("org_members")) == 1, "invalid position or student code rejected")
    psec.post("/org/manage", data={"action": "import", "year": 2569})
    imported = repo().where("org_members", academic_year=2569)
    check(len(imported) > 5 and all(set(x) <= {"id", "academic_year", "position_id", "position_title", "first_name", "last_name", "student_code",
                                                 "program_code", "photo_path", "order", "created_by", "created_at"} for x in imported),
          "import from holders copies only the disclosable fields")
    page = guest2.get("/org/").get_data(as_text=True)
    check("กองบริหารและสัมพันธ์" in page and "@demo.local" not in page, "org page grouped by division, no account email")
    n_before = len(imported)
    psec.post("/org/manage", data={"action": "import", "year": 2569})
    check(len(repo().where("org_members", academic_year=2569)) == n_before, "importing twice adds no duplicates")
    get_ok(psec, f"/org/manage?edit={om['id']}")
    psec.post("/org/manage", data={"action": "delete", "year": 2569, "member_id": om["id"]})
    check(not repo().get("org_members", om["id"]) and "สมหญิง ใจงาม" not in guest2.get("/org/").get_data(as_text=True), "delete org member")
    check("ยังไม่มีข้อมูล" in guest2.get("/org/?year=2570").get_data(as_text=True), "other years are separate")

    print("== member roster import (CSV / Excel from Google Sheet)")
    from openpyxl import Workbook
    get_ok(asec, "/admin/roster/")
    check(stu.get("/admin/roster/").status_code == 403, "student cannot open the roster")
    csv_text = ("ลำดับการสมัคร,รหัสนักศึกษา,ชื่อ - นามสกุล,สาขาวิชา,ฝ่าย,ชั้นปี,สถานะในสโม,LineID\n"
                "1,406865010,นายนักศึกษา ทดสอบ,เทคโนโลยีสารสนเทศ,ฝ่ายกีฬา,2,ปกติ,line-secret-1\n"
                "2,406765555,นางสาวใหม่ ยังไม่สมัคร,คณิตศาสตร์,ฝ่ายสื่อ,1,ปกติ,line-secret-2\n"
                "3,406765556,นายลา ออกแล้ว,คณิตศาสตร์,ฝ่ายสื่อ,1,ลาออก,line-secret-3\n"
                "4,,ไม่มีรหัส,,,,,\n")
    r = asec.post("/admin/roster/", data={"action": "upload", "year": 2569, "member_type": "SUBCOMM",
                                          "file": (io.BytesIO(csv_text.encode("utf-8-sig")), "sub.csv")},
                  content_type="multipart/form-data")
    imp = repo().all("roster_imports")[0]
    check(r.status_code == 302 and len(imp["rows"]) == 3 and "line-secret" not in str(imp), "CSV parsed, LineID not kept")
    check(imp["rows"][0]["first_name"] == "นักศึกษา" and imp["rows"][1]["first_name"] == "ใหม่" and imp["rows"][1]["last_name"] == "ยังไม่สมัคร",
          "name prefix removed and name split")
    pv = get_ok(asec, f"/admin/roster/import/{imp['id']}").get_data(as_text=True)
    check("ฝ่ายกีฬา" in pv and "ลาออก" in pv and not repo().all("member_roster"), "preview shows units and statuses, nothing saved yet")
    check(psec.get(f"/admin/roster/import/{imp['id']}").status_code in (403, 404), "another user cannot open the pending import")
    units = sorted({x["unit"] for x in imp["rows"]})
    statuses = sorted({x["status"] for x in imp["rows"]})
    form = {"action": "confirm"}
    form.update({f"unit_{i}": ("ACTIVITY" if "กีฬา" in u else "PR") for i, u in enumerate(units)})
    form.update({f"status_{i}": "1" for i, s in enumerate(statuses) if s == "ปกติ"})
    asec.post(f"/admin/roster/import/{imp['id']}", data=form)
    ros = {x["student_code"]: x for x in repo().all("member_roster")}
    check(set(ros) == {"406865010", "406765555"} and ros["406865010"]["position_id"] == "SUB_ACTIVITY" and ros["406765555"]["position_id"] == "SUB_PR",
          "import saves chosen statuses with mapped divisions")
    check(ros["406865010"]["user_id"] == stu_id and repo().where("position_holders", user_id=stu_id, position_id="SUB_ACTIVITY", status="ACTIVE"),
          "existing account gets the position immediately")
    check(ros["406765555"]["user_id"] is None, "member without an account waits for registration")
    newc = app.test_client()
    newc.post("/register", data={"mode": "student", "student_code": "406765555", "first_name": "ใหม่", "last_name": "ยังไม่สมัคร",
                                 "email": "new555@demo.local", "password": PW, "password2": PW, "consent": "1"})
    new_u = repo().where("users", student_code="406765555")[0]
    check(repo().where("position_holders", user_id=new_u["id"], position_id="SUB_PR", status="ACTIVE")
          and repo().get("member_roster", ros["406765555"]["id"])["user_id"] == new_u["id"], "position attaches when the member registers")
    check("อนุกรรมการ" in newc.get("/team/my").get_data(as_text=True), "team recruitment recognises the imported member")
    wb = Workbook()
    ws = wb.active
    ws.append(["ลำดับ", "รหัสนักศึกษา", "ชื่อ - สกุล", "สาขาวิชา", "ฝ่ายเดิม", "ชั้นปี", "สถานะในสโม"])
    ws.append([1, 406865010, "นาย นักศึกษา ทดสอบ", "เทคโนโลยีสารสนเทศ", "ฝ่ายแผนและนโยบาย", 2, "ปกติ"])
    buf = io.BytesIO()
    wb.save(buf)
    asec.post("/admin/roster/", data={"action": "upload", "year": 2569, "member_type": "COMM", "file": (io.BytesIO(buf.getvalue()), "comm.xlsx")},
              content_type="multipart/form-data")
    imp2 = repo().all("roster_imports")[0]
    check(imp2["rows"][0]["student_code"] == "406865010" and imp2["rows"][0]["unit"] == "ฝ่ายแผนและนโยบาย", "Excel parsed with numeric student code")
    check('value="POLICY" selected' in asec.get(f"/admin/roster/import/{imp2['id']}").get_data(as_text=True), "division guessed from unit name")
    asec.post(f"/admin/roster/import/{imp2['id']}", data={"action": "confirm", "unit_0": "POLICY", "status_0": "1"})
    check(repo().where("position_holders", user_id=stu_id, position_id="COMM_POLICY", status="ACTIVE"), "committee import from Excel")
    n_h = len(repo().where("position_holders", user_id=stu_id))
    asec.post("/admin/roster/", data={"action": "upload", "year": 2569, "member_type": "COMM", "file": (io.BytesIO(buf.getvalue()), "comm.xlsx")},
              content_type="multipart/form-data")
    asec.post(f"/admin/roster/import/{repo().all('roster_imports')[0]['id']}", data={"action": "confirm", "unit_0": "POLICY", "status_0": "1"})
    check(len(repo().where("position_holders", user_id=stu_id)) == n_h and len(repo().where("member_roster", student_code="406865010")) == 2,
          "re-importing the same file creates no duplicates")
    r = asec.get("/export/member_roster?fmt=tsv&year=2569")
    check(r.status_code == 200 and "406765555" in r.get_data(as_text=True), "roster export")
    asec.post("/admin/roster/", data={"action": "upload", "year": 2569, "member_type": "COMM", "file": (io.BytesIO(b"not excel"), "x.xlsx")},
              content_type="multipart/form-data")
    check(not repo().all("roster_imports"), "broken file rejected")
    mv = get_ok(pres, "/members/", "members summary as board").get_data(as_text=True)
    check("กองแผนและนโยบาย" in mv and "406765555" in mv and "รวมทั้งหมด" in mv, "summary lists roster members grouped by division")
    check("406765555" not in pres.get("/members/?division=POLICY").get_data(as_text=True)
          and "406765555" in pres.get("/members/?division=PR&type=SUBCOMM").get_data(as_text=True), "summary filters by division and type")
    check("นำเข้า/แก้ไขทะเบียน" not in mv and "นำเข้า/แก้ไขทะเบียน" in asec.get("/members/").get_data(as_text=True),
          "only roster managers see the import link")
    check(newc.get("/members/").status_code == 403 and app.test_client().get("/members/").status_code == 302,
          "subcommittee members and guests cannot view the summary")
    r = pres.get("/export/members_summary?fmt=tsv&year=2569")
    check(r.status_code == 200 and "406765555" in r.get_data(as_text=True) and newc.get("/export/members_summary?fmt=tsv").status_code == 403,
          "summary export follows the same permission")
    asec.post("/admin/roster/", data={"action": "delete", "year": 2569, "roster_id": ros["406865010"]["id"]})
    check(not repo().where("position_holders", user_id=stu_id, position_id="SUB_ACTIVITY", status="ACTIVE"), "removing from roster ends that position")
    asec.post("/admin/roster/", data={"action": "clear", "year": 2569, "member_type": "COMM"})
    asec.post("/admin/roster/", data={"action": "clear", "year": 2569, "member_type": "SUBCOMM"})
    check(not repo().all("member_roster") and not repo().where("position_holders", user_id=new_u["id"], status="ACTIVE"), "clear roster ends roster positions")

    print("== edit buttons for admin and ADMIN_SEC")
    # ชื่อกอง
    admin.post("/admin/positions", data={"action": "rename_division", "year": 2569, "code": "SECRETARY", "name": "กองเลขานุการ"})
    check(repo().get("divisions", "SECRETARY")["name"] == "กองเลขานุการ", "admin renames a division")
    check("กองเลขานุการ" in admin.get("/admin/roster/").get_data(as_text=True), "new division name shows on other pages")
    asec.post("/admin/holders", data={"action": "rename_division", "year": 2569, "code": "SECRETARY", "name": "กองเลขา"})
    check(repo().get("divisions", "SECRETARY")["name"] == "กองเลขา", "ADMIN_SEC renames a division from the holders page")
    check(stu.post("/admin/holders", data={"action": "rename_division", "code": "SECRETARY", "name": "x"}).status_code == 403
          and asec.post("/admin/positions", data={"action": "rename_division", "code": "SECRETARY", "name": "x"}).status_code == 403,
          "others cannot rename divisions")
    admin.post("/admin/positions", data={"action": "delete_division", "year": 2569, "code": "ADMIN"})
    check(repo().get("divisions", "ADMIN"), "division in use cannot be deleted")
    admin.post("/admin/positions", data={"action": "division", "year": 2569, "code": "TEMPDIV", "name": "กองชั่วคราว"})
    admin.post("/admin/positions", data={"action": "delete_division", "year": 2569, "code": "TEMPDIV"})
    check(not repo().get("divisions", "TEMPDIV"), "unused division deleted")
    # ปีการศึกษา
    admin.post("/admin/years", data={"action": "add", "year": 2571})
    admin.post("/admin/years", data={"action": "edit", "year": "2571", "start_date": "2028-06-01", "end_date": "2029-05-31"})
    check(repo().get("academic_years", "2571")["end_date"] == "2029-05-31", "edit academic year dates")
    admin.post("/admin/years", data={"action": "delete", "year": "2569"})
    check(repo().get("academic_years", "2569"), "current academic year cannot be deleted")
    admin.post("/admin/years", data={"action": "delete", "year": "2571"})
    check(not repo().get("academic_years", "2571"), "unused academic year deleted")
    # ผู้ถือตำแหน่ง
    hh = repo().where("position_holders", position_id="PR_VP", academic_year=2569, status="ACTIVE")[0]
    check("แก้ไขผู้ถือตำแหน่ง" in get_ok(asec, f"/admin/holders?year=2569&edit={hh['id']}").get_data(as_text=True), "holder edit form")
    asec.post("/admin/holders", data={"action": "edit", "year": 2569, "holder_id": hh["id"], "position_id": "PRES", "start_date": "2026-06-01"})
    check(repo().get("position_holders", hh["id"])["position_id"] == "PR_VP", "cannot move a holder into an occupied single-holder position")
    asec.post("/admin/holders", data={"action": "edit", "year": 2569, "holder_id": hh["id"], "position_id": "PR_SEC", "start_date": "2026-06-15"})
    hh2 = repo().get("position_holders", hh["id"])
    check(hh2["position_id"] == "PR_SEC" and hh2["start_date"] == "2026-06-15", "ADMIN_SEC edits a holder's position and start date")
    asec.post("/admin/holders", data={"action": "edit", "year": 2569, "holder_id": hh["id"], "position_id": "PR_VP", "start_date": "2026-06-15"})
    # ทะเบียนกรรมการ/อนุกรรมการ
    repo().set("member_roster", "2569_SUBCOMM_406865010", {"academic_year": 2569, "member_type": "SUBCOMM", "student_code": "406865010",
               "first_name": "นักศึกษา", "last_name": "ทดสอบ", "program": "", "unit": "ฝ่ายสื่อ", "year_level": 2, "status_text": "ปกติ",
               "division_id": "PR", "position_id": "SUB_PR", "user_id": None, "holder_id": None})
    from app.views.roster import attach_user
    attach_user(repo().get("users", stu_id))
    check("แก้ไข: 406865010" in get_ok(asec, "/admin/roster/?year=2569&edit=2569_SUBCOMM_406865010").get_data(as_text=True), "roster edit form")
    asec.post("/admin/roster/", data={"action": "edit", "year": 2569, "roster_id": "2569_SUBCOMM_406865010", "first_name": "นักศึกษา",
                                      "last_name": "แก้แล้ว", "division_id": "SECRETARY", "unit": "ฝ่ายเอกสาร", "program": "ไอที",
                                      "year_level": 3, "status_text": "ปกติ"})
    re_ = repo().get("member_roster", "2569_SUBCOMM_406865010")
    check(re_["last_name"] == "แก้แล้ว" and re_["position_id"] == "SUB_SECRETARY" and re_["year_level"] == 3, "roster row edited")
    check(repo().where("position_holders", user_id=stu_id, position_id="SUB_SECRETARY", status="ACTIVE")
          and not repo().where("position_holders", user_id=stu_id, position_id="SUB_PR", status="ACTIVE"), "moving division moves the position")
    asec.post("/admin/roster/", data={"action": "delete", "year": 2569, "roster_id": "2569_SUBCOMM_406865010"})
    # หมวด FAQ
    fc = repo().add("faq_categories", {"name": "หมวดเดิม"})
    asec.post("/manage/faq", data={"action": "category_rename", "category_id": fc, "name": "หมวดใหม่"})
    check(repo().get("faq_categories", fc)["name"] == "หมวดใหม่", "rename FAQ category")
    asec.post("/manage/faq", data={"action": "category_delete", "category_id": fc})
    check(not repo().get("faq_categories", fc), "delete empty FAQ category")

    print("== media division rename, grouped positions page, org chart image")
    from app.seed import apply_renames
    check(repo().get("divisions", "PR")["name"] == "กองสื่อ" and repo().get("positions", "PR_VP")["name"] == "อุปนายกฝ่ายยุทธศาสตร์สื่อ"
          and repo().get("positions", "SUB_PR")["name"] == "อนุกรรมการกองสื่อ", "media division and VP use the new default names")
    repo().update("divisions", "PR", {"name": "กองประชาสัมพันธ์"})
    repo().update("positions", "PR_VP", {"name": "ชื่อที่ผู้ดูแลตั้งเอง"})
    admin.post("/admin/positions", data={"action": "seed_defaults", "year": 2569})
    check(repo().get("divisions", "PR")["name"] == "กองสื่อ" and repo().get("positions", "PR_VP")["name"] == "ชื่อที่ผู้ดูแลตั้งเอง",
          "update button renames old default names only, keeps custom names")
    repo().update("positions", "PR_VP", {"name": "อุปนายกฝ่ายยุทธศาสตร์สื่อ"})
    pg = get_ok(admin, "/admin/positions").get_data(as_text=True)
    check(pg.index("</i>ส่วนกลาง (ไม่สังกัดกอง)</span>") < pg.index("</i>กองบริหารและสัมพันธ์</span>") < pg.index("</i>กองสื่อ</span>")
          and "อุปนายกฝ่ายยุทธศาสตร์สื่อ" in pg,
          "positions page groups by division in chart order")
    check("pos-active" in admin.get("/admin/positions?edit=PR_VP").get_data(as_text=True), "edited position is highlighted")
    check(app.test_client().get("/org/chart/2569").status_code == 404, "no chart image yet")
    psec.post("/org/manage", content_type="multipart/form-data",
              data={"action": "chart_upload", "year": 2569, "caption": "ผังบอร์ด 69", "chart": (io.BytesIO(jpg), "chart.jpg")})
    oc = repo().get("org_charts", "2569")
    check(oc and oc["caption"] == "ผังบอร์ด 69" and app.test_client().get("/org/chart/2569").data == jpg, "org chart image uploaded and public")
    opg = app.test_client().get("/org/").get_data(as_text=True)
    check("/org/chart/2569" in opg and "ผังบอร์ด 69" in opg and "ผังโครงสร้างสโมสรนักศึกษา" in opg, "chart image embedded on the public org page")
    check(stu.post("/org/manage", data={"action": "chart_delete", "year": 2569}).status_code == 403 and repo().get("org_charts", "2569"),
          "only org managers change the chart image")
    psec.post("/org/manage", data={"action": "chart_delete", "year": 2569})
    check(not repo().get("org_charts", "2569") and "/org/chart/2569" not in app.test_client().get("/org/").get_data(as_text=True), "chart image removed")

    print("== board chart drawn from data")
    from app.views.org import board_tree
    tr = board_tree(2569, [])
    check(tr["root"]["id"] == "PRES" and {n["id"] for n in tr["aides"]} == {"PRES_SEC", "TREASURER"}
          and [n["id"] for n in tr["columns"]] == ["ADMIN_VP", "ACT_VP", "PR_VP", "POLICY_VP"], "chart: president, two aides, four VP columns in board order")
    col = {n["id"]: n for n in tr["columns"]}
    check({c["id"] for c in col["ADMIN_VP"]["children"]} == {"ADMIN_SEC", "PERSONNEL_HEAD", "FUNDRAISING_HEAD", "EQUIP_HEAD", "WELFARE_HEAD"}
          and {c["id"] for c in col["ACT_VP"]["children"]} == {"ACT_SEC", "ACADEMIC_HEAD", "SPORT_HEAD", "VOLUNTEER_HEAD", "CULTURE_HEAD"}
          and not col["POLICY_VP"]["children"], "chart: units under each VP match the whiteboard")
    media = col["PR_VP"]["children"]
    check([c["id"] for c in media] == ["PR_SEC", "MEDIA_HEAD"]
          and {c["id"] for c in media[1]["children"]} == {"CONTENT_TEAM", "PHOTO_HEAD", "VIDEO_HEAD", "BANNER_HEAD"}, "chart: media teams nested under the media head")
    psec.post("/org/manage", data={"action": "save", "year": 2569, "first_name": "อัง", "last_name": "ทดสอบ", "position_id": "PRES"})
    opg = app.test_client().get("/org/").get_data(as_text=True)
    check("ผังโครงสร้างบอร์ดบริหาร" in opg and "oc-root" in opg and opg.index("oc-root") < opg.index("อัง ทดสอบ") < opg.index("oc-aides"),
          "public page draws the chart with the holder's name in the box")
    check("ผังโครงสร้างบอร์ดบริหาร" in app.test_client().get("/org/?year=2570").get_data(as_text=True)
          and "ผังโครงสร้างบอร์ดบริหาร" not in app.test_client().get("/org/?year=2580").get_data(as_text=True), "chart follows each year's own board positions")
    for x in repo().where("org_members", first_name="อัง"):
        repo().delete("org_members", x["id"])

    print("== admin menu access")
    asec_c = client_for("406765004")
    check("ผู้ถือตำแหน่งตามวาระ" in get_ok(asec_c, "/admin/holders", "holders as ADMIN_SEC").get_data(as_text=True), "ADMIN_SEC uses holders page")
    home_html = asec_c.get("/app/").get_data(as_text=True)
    check("/admin/holders" in home_html and "/admin/users" not in home_html and "/admin/settings" not in home_html,
          "ADMIN_SEC side menu shows only holders")
    pub_html = admin.get("/").get_data(as_text=True)
    check("/admin/users" not in pub_html and "/equipment/manage" not in pub_html and "/app/" in pub_html,
          "public top menu has no staff/admin items, only a link into the portal")
    for u in ["/admin/users", "/admin/positions", "/admin/years", "/admin/programs", "/admin/settings", "/admin/permissions",
              "/admin/audit", "/export/audit?fmt=tsv"]:
        check(asec_c.get(u).status_code == 403, f"ADMIN_SEC blocked from {u}")
    get_ok(asec_c, "/admin/delegations", "delegations still open to ADMIN_SEC")
    pres_c = client_for("406765001")
    check(pres_c.get("/admin/audit").status_code == 403 and "/admin/audit" not in pres_c.get("/").get_data(as_text=True),
          "PRES no longer sees audit log")
    check(admin.post("/admin/permissions", data={"perm": "audit.view", "roles": ["PRES"]}).status_code == 403, "audit.view is locked")
    for who in ("406765001", "406765003", "406865010"):
        c = client_for(who)
        check(c.get("/admin/holders").status_code == 403 and c.get("/admin/users").status_code == 403, f"{who} blocked from admin menu")
        check("ผู้ดูแลระบบ</h6>" not in c.get("/").get_data(as_text=True), f"{who} does not see admin menu")
    r = admin.post("/admin/permissions", data={"perm": "admin.manage", "roles": ["PRES"]})
    check(r.status_code == 403, "admin.manage is locked")

    print("== login lockout")
    lc = app.test_client()
    for _ in range(5):
        lc.post("/login", data={"identifier": "406765007", "password": "wrong"})
    r = lc.post("/login", data={"identifier": "406765007", "password": PW})
    check(r.status_code == 200 and "ล็อก" in r.get_data(as_text=True), "account locked after 5 failures")

    print("== render pages per role")
    pages = {
        "406765006": ["/recruit/", "/recruit/new", f"/recruit/{rid}/edit"],
        "406865010": ["/recruit/", "/activities/", "/tickets/", "/equipment/requests?mine=1", f"/equipment/request/{rid_}",
                      f"/tickets/{t1['id']}", "/recruit/my"],
        "406765009": ["/activities/new", f"/activities/{act['id']}/edit", f"/activities/{act['id']}",
                      f"/activities/batch/{sb['id']}", "/documents/", "/team/manage"],
        "406765008": ["/manage/announcements/new", "/manage/announcements?status=DRAFT"],
        "406765002": ["/manage/faq", "/tickets/", "/tickets/?status=CLOSED", "/manage/faq?edit=" + repo().all("faqs")[0]["id"]],
        "406765003": ["/app/approvals", "/admin/delegations", f"/equipment/request/{sr['id']}", "/tickets/", f"/recruit/batch/{bid}"],
        "406111011": ["/equipment/", "/equipment/request/new", "/app/", "/profile"],
        "admin@demo.local": ["/admin/positions?edit=PRES", "/admin/programs?edit=64", "/reports/?year=2569"],
    }
    for who, urls in pages.items():
        c = client_for(who)
        for u in urls:
            get_ok(c, u, f"{u} as {who}")

print("== Firestore chunked file storage (in-memory fake)")
import app.db as dbmod  # noqa: E402
from app import storage  # noqa: E402


class FakeFirestore:
    def __init__(self):
        self.d = {}

    def set(self, col, i, data, merge=False):
        self.d.setdefault(col, {})[i] = dict(data)

    def get(self, col, i):
        v = self.d.get(col, {}).get(i)
        return dict(v, id=i) if v is not None else None

    def delete(self, col, i):
        self.d.get(col, {}).pop(i, None)


with app.app_context():
    real, fake = dbmod._repo, FakeFirestore()
    dbmod._repo = fake
    app.config["DB_BACKEND"] = "firestore"
    try:
        blob = os.urandom(2 * 1024 * 1024 + 123)
        p = storage.save_bytes("t", blob, "pdf", "application/pdf", encrypt=True)
        meta = fake.get("files", p[3:].split(".")[0])
        check(p.startswith("fs:") and meta["chunks"] >= 3, f"split into {meta['chunks']} chunks")
        check(all(len(c["data"]) <= 900 * 1024 for c in fake.d["file_chunks"].values()), "each chunk under 1 MiB doc limit")
        check(storage.load_bytes(p) == blob, "chunks reassemble + decrypt")
        storage.delete_file(p)
        check(not fake.d["file_chunks"] and not fake.d["files"], "delete removes all chunks")
    finally:
        dbmod._repo = real
        app.config["DB_BACKEND"] = "local"

print()
print("FAILED:" if FAILS else "ALL PASSED", len(FAILS))
for f in FAILS:
    print(" -", f)
sys.exit(1 if FAILS else 0)
