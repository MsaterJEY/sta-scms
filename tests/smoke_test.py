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
                 WTF_CSRF_ENABLED=False, TESTING=True, SETUP_TOKEN="tok", CRON_SECRET="cron", SERVER_NAME="localhost")

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
                                      "quota_COMM_ACADEMIC": 2, "quota_COMM_SPORT": 1,
                                      "crit_name": ["บุคลิก", "ความรู้"], "crit_max": [10, 10], "crit_weight": [1, 1]})
    rid = repo().all("recruitment_rounds")[0]["id"]
    ph.post(f"/recruit/{rid}/status", data={"status": "OPEN"})
    check(repo().get("recruitment_rounds", rid)["status"] == "OPEN", "round opened (no approval needed)")
    check(any(a.get("source_type") == "recruitment_round" for a in repo().all("announcements")), "auto draft announcement")
    rps = repo().where("round_positions", round_id=rid)
    rp_ac = next(x for x in rps if x["position_id"] == "COMM_ACADEMIC")["id"]
    rp_sp = next(x for x in rps if x["position_id"] == "COMM_SPORT")["id"]
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
    check(repo().where("position_holders", user_id=res["user_id"], position_id="COMM_ACADEMIC", status="ACTIVE"), "appointment creates position holder")

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
    acvp.post("/activities/new", data={"title": "ค่ายวิชาการ", "division_id": "ACADEMIC", "start_at": dt(10), "end_at": dt(11), "location": "หอประชุม"})
    act = repo().all("activities")[0]
    check(act["division_id"] == "ACADEMIC", "DIV_VP creates activity in own division")
    r = acvp.post("/activities/new", data={"title": "x", "division_id": "SPORT", "start_at": dt(1), "end_at": dt(2)})
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
    stu.post("/equipment/request/new", data={"borrow_at": dt(1), "due_at": dt(2), "purpose": "งาน", f"qty_{spk['id']}": 1,
                                             f"qty_{chr_['id']}": 10, "consent": "1"})
    req = repo().where("equipment_requests", requester_id=repo().where("users", student_code="406865010")[0]["id"])[0]
    check(req["status"] == "PENDING" and not req["is_self_request"], "request created")
    stu.post("/equipment/request/new", data={"borrow_at": dt(1), "due_at": dt(2), "purpose": "x", f"qty_{chr_['id']}": 60, "consent": "1"})
    check(len(repo().all("equipment_requests")) == 1, "over-availability request rejected")
    other.post("/equipment/request/new", data={"borrow_at": dt(1), "due_at": dt(2), "purpose": "x", f"qty_{chr_['id']}": 1, "consent": "1"})
    check(len(repo().all("equipment_requests")) == 1, "OTHER_MRU must attach student card")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    other.post("/equipment/request/new", data={"borrow_at": dt(1), "due_at": dt(2), "purpose": "x", f"qty_{chr_['id']}": 1,
                                               "consent": "1", "borrower_doc": (io.BytesIO(png), "card.png")},
               content_type="multipart/form-data")
    check(len(repo().all("borrower_documents")) == 1, "borrower doc uploaded (encrypted)")
    doc = repo().all("borrower_documents")[0]
    raw = open(os.path.join(tmp, "up", doc["file_path"]), "rb").read()
    check(not raw.startswith(b"\x89PNG"), "borrower doc encrypted at rest")
    check(eqh.get(f"/equipment/doc/{doc['id']}").data == png, "EQUIP_HEAD can view decrypted doc")
    check(avp.get(f"/equipment/doc/{doc['id']}").status_code == 403, "others cannot view borrower doc")
    rid_ = req["id"]
    items = repo().where("equipment_request_items", request_id=rid_)
    get_ok(eqh, f"/equipment/request/{rid_}", "request detail (head)")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "approve", **{f"approve_{i['id']}": i["qty_requested"] for i in items}})
    check(repo().get("equipment_requests", rid_)["status"] == "APPROVED", "approved by EQUIP_HEAD")
    it_spk = next(i for i in items if i["model_id"] == spk["id"])
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "issue", f"units_{it_spk['id']}": "SPK-001", "receiver_name": "x", "id_checked": "1"})
    check(repo().get("equipment_requests", rid_)["status"] == "ISSUED", "issued")
    check(repo().where("equipment_units", asset_tag="SPK-001")[0]["status"] == "BORROWED", "unit borrowed")
    unit1 = repo().where("equipment_units", asset_tag="SPK-001")[0]
    it_chr = next(i for i in items if i["model_id"] == chr_["id"])
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "return", f"ret_{it_chr['id']}": 8, f"lost_{it_chr['id']}": 2})
    check(repo().get("equipment_requests", rid_)["status"] == "PARTIAL_RETURNED", "partial return")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "return", f"unit_{unit1['id']}": "RETURN"})
    check(repo().get("equipment_requests", rid_)["status"] == "RETURNED", "fully returned")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "close"})
    check(repo().get("equipment_requests", rid_)["status"] != "CLOSED", "cannot close with lost items and no fine")
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "fine", "type": "LOSS", "amount": 400})
    fine = repo().all("equipment_fines")[0]
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "close"})
    check(repo().get("equipment_requests", rid_)["status"] != "CLOSED", "cannot close with pending fine")
    stu.post("/equipment/request/new", data={"borrow_at": dt(3), "due_at": dt(4), "purpose": "x", f"qty_{chr_['id']}": 1, "consent": "1"})
    check(len(repo().where("equipment_requests", requester_id=req["requester_id"])) == 1, "blocked while fine pending")
    eqh.post(f"/equipment/fine/{fine['id']}", data={"status": "PAID"})
    eqh.post(f"/equipment/request/{rid_}/action", data={"action": "close"})
    check(repo().get("equipment_requests", rid_)["status"] == "CLOSED", "closed after fine paid")
    check(len(repo().all("equipment_transactions")) >= 4, "transactions appended")
    # self request
    eqh.post("/equipment/request/new", data={"borrow_at": dt(1), "due_at": dt(2), "purpose": "ใช้เอง", f"qty_{chr_['id']}": 5, "consent": "1"})
    sr = next(r for r in repo().all("equipment_requests") if r.get("is_self_request"))
    i_sr = repo().where("equipment_request_items", request_id=sr["id"])[0]
    r = eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "approve", f"approve_{i_sr['id']}": 5})
    check(r.status_code == 403, "EQUIP_HEAD cannot approve own request")
    avp.post(f"/equipment/request/{sr['id']}/action", data={"action": "approve", f"approve_{i_sr['id']}": 5})
    check(repo().get("equipment_requests", sr["id"])["status"] == "APPROVED", "ADMIN_VP approves self request")
    eqh.post(f"/equipment/request/{sr['id']}/action", data={"action": "issue", "receiver_name": "me"})
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
    # complaint against ADMIN_SEC, anonymous
    stu.post("/tickets/new", data={"category_id": "COMPLAINT", "title": "ร้องเรียนเลขา", "detail": "...", "is_anonymous": "1",
                                   "target_position_ids": ["ADMIN_SEC"]})
    t2 = repo().where("tickets", title="ร้องเรียนเลขา")[0]
    check(t2["handler_roles"] == ["PRES_SEC"] and "ADMIN_SEC" in t2["hidden_roles"], "routing when ADMIN_SEC targeted")
    check(asec.get(f"/tickets/{t2['id']}").status_code == 403, "hidden from target")
    check("ร้องเรียนเลขา" not in asec.get("/tickets/").get_data(as_text=True), "hidden from target queue")
    r = psec.get(f"/tickets/{t2['id']}")
    check(r.status_code == 200 and "นิรนาม" in r.get_data(as_text=True), "handler sees anonymous")
    # complaint against PRES -> ADVISOR
    stu.post("/tickets/new", data={"category_id": "COMPLAINT", "title": "ร้องเรียนนายก", "detail": "...", "target_position_ids": ["PRES"]})
    t3 = repo().where("tickets", title="ร้องเรียนนายก")[0]
    check(t3["handler_roles"] == ["ADVISOR"] and "PRES_SEC" in t3["hidden_roles"], "PRES targeted -> ADVISOR, hidden from PRES_SEC")
    check(pres.get(f"/tickets/{t3['id']}").status_code == 403 and psec.get(f"/tickets/{t3['id']}").status_code == 403, "hidden from PRES and PRES_SEC")
    # escalate + reveal
    psec.post(f"/tickets/{t2['id']}/action", data={"action": "escalate", "to": "PRESIDENT", "reason": "ร้ายแรง"})
    t2 = repo().get("tickets", t2["id"])
    check(t2["routing_level"] == "PRESIDENT", "escalated")
    pres.post(f"/tickets/{t2['id']}/action", data={"action": "reveal"})
    check(not repo().all("ticket_identity_reveals"), "reveal requires reason")
    pres.post(f"/tickets/{t2['id']}/action", data={"action": "reveal", "reason": "ตรวจสอบข้อเท็จจริง"})
    check("406865010" in pres.get(f"/tickets/{t2['id']}").get_data(as_text=True), "PRES sees identity after reveal")
    check(len(repo().all("ticket_access_logs")) > 0, "ticket access logged")
    get_ok(pres, "/tickets/stats")
    r = pres.get("/export/ticket_stats?fmt=tsv")
    check(r.status_code == 200 and "< 5" in r.get_data(as_text=True), "ticket stats hide small groups")

    print("== documents / reports / cron")
    drive = "https://drive.google.com/file/d/abc/view"
    acvp.post("/documents/", data={"title": "โครงการค่าย", "doc_type": "PROJECT", "division_id": "ACADEMIC",
                                   "access_level": "DIVISION", "url": drive})
    d = repo().where("documents", title="โครงการค่าย")[0]
    r = acvp.get(f"/documents/{d['id']}/open")
    check(r.status_code == 302 and r.headers["Location"] == drive, "owner opens doc link (redirect)")
    check(eqh.get(f"/documents/{d['id']}/open").status_code == 403, "other division cannot see DIVISION doc")
    check(pres.get(f"/documents/{d['id']}/open").status_code == 302, "PRES sees division doc")
    acvp.post("/documents/", data={"title": "x", "division_id": "ACADEMIC", "url": "javascript:alert(1)"})
    acvp.post("/documents/", data={"title": "y", "division_id": "ACADEMIC", "url": "http://insecure.example"})
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
    ph.post(f"/team/position/{tp['id']}/apply", data={})
    eqh.post(f"/team/position/{tp['id']}/apply", data={"gender": "M"})
    check(len(repo().where("team_applications", event_id=tid)) == 1, "duplicate and over-quota applications rejected")
    ph.post(f"/team/application/{tapps[0]['id']}/status", data={"action": "APPROVED"})
    check(repo().get("team_applications", tapps[0]["id"])["status"] == "APPROVED", "approve applicant")
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

    print("== admin menu access")
    asec_c = client_for("406765004")
    check("ผู้ถือตำแหน่งตามวาระ" in get_ok(asec_c, "/admin/holders", "holders as ADMIN_SEC").get_data(as_text=True), "ADMIN_SEC uses holders page")
    home_html = asec_c.get("/").get_data(as_text=True)
    check("/admin/holders" in home_html and "/admin/users" not in home_html and "/admin/settings" not in home_html,
          "ADMIN_SEC menu shows only holders")
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
                      f"/activities/batch/{sb['id']}", "/documents/", "/reports/"],
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
