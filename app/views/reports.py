"""5.0 รายงานและสถิติข้ามโมดูล (RPT-01..06)"""
from collections import Counter

from datetime import timedelta

from flask import Blueprint, abort, current_app, render_template, request

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_year, login_required
from ..services import approval_state
from ..utils import now, to_iso
from .equipment import is_overdue
from .tickets import advisor_ticket_summary, ticket_stats

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _allowed():
    return any(can(p) for p in ("report.view", "eqp.report", "rec.manage", "admin.manage"))


def _activities_on():
    return bool(current_app.config.get("ACTIVITIES_ENABLED"))


@bp.route("/")
@login_required
def index():
    if not _allowed():
        abort(403)
    year = int(request.args.get("year") or current_year())
    users = repo().all("users")
    programs = {p["id"]: p for p in repo().all("programs")}
    by_program = Counter((programs.get(u.get("program_code")) or {}).get("name_th", "อื่น ๆ")
                         for u in users if u.get("account_type") == "SCI")
    by_type = Counter(u.get("account_type") for u in users)

    rounds = repo().where("recruitment_rounds", academic_year=year)
    rec = []
    for r in rounds:
        apps = repo().where("member_applications", round_id=r["id"])
        approved_pass = 0
        for b in repo().where("selection_batches", round_id=r["id"]):
            if approval_state(repo().get("approval_requests", b.get("approval_request_id")))["approved"]:
                approved_pass += len([x for x in repo().where("selection_results", batch_id=b["id"])
                                      if x["result"] == "PASS" and not x.get("removed")])
        rec.append({"round": r, "apps": len(apps), "pass": approved_pass})

    acts = repo().where("activities", academic_year=year) if _activities_on() else []
    divs = {d["id"]: d for d in repo().all("divisions")}
    act_rows = []
    for a in acts:
        staff = [s for s in repo().where("activity_staff", activity_id=a["id"]) if s.get("confirm_status") == "CONFIRMED"]
        hours = sum(float(h.get("hours") or 0) for h in repo().where("activity_hours", activity_id=a["id"]))
        act_rows.append({"a": a, "div": (divs.get(a.get("division_id")) or {}).get("name"), "staff": len(staff), "hours": hours})

    reqs = repo().all("equipment_requests")
    eqp = {"total": len(reqs), "active": len([r for r in reqs if r.get("status") in ("APPROVED", "ISSUED", "PARTIAL_RETURNED")]),
           "overdue": len([r for r in reqs if is_overdue(r)]),
           "fines_pending": sum(float(f.get("amount") or 0) for f in repo().where("equipment_fines", status="PENDING"))}
    model_count = Counter()
    for it in repo().all("equipment_request_items"):
        model_count[it["model_id"]] += int(it.get("qty_issued") or 0)
    models = {m["id"]: m for m in repo().all("equipment_models")}
    top_models = [(models.get(k, {}).get("name", k), v) for k, v in model_count.most_common(8)]

    return render_template("reports/index.html", year=year, by_program=dict(by_program), by_type=dict(by_type), rec=rec,
                           act_rows=act_rows, eqp=eqp, top_models=top_models, tkt=ticket_stats(),
                           show_tkt=can("report.view") or can("tkt.handle"),
                           years=sorted(repo().all("academic_years"), key=lambda y: y["id"], reverse=True))


@bp.route("/advisor")
@login_required
def advisor():
    """Dashboard อาจารย์ที่ปรึกษา (SRS v3 หัวข้อ 4.2): ตัวเลขสรุปเท่านั้น ไม่มีชื่อ เนื้อหา หรือข้อมูลรายบุคคล"""
    if not can("dashboard.advisor"):
        abort(403)
    year = current_year()
    users = [u for u in repo().all("users") if u.get("status") == "ACTIVE"]
    since = to_iso(now() - timedelta(days=30))
    reqs = repo().all("equipment_requests")
    usage = {
        "users": len(users),
        "active_users": len([u for u in users if (u.get("last_login_at") or "") >= since]),
        "new_users": len([u for u in users if (u.get("created_at") or "") >= since]),
        "applicants": sum(len(repo().where("member_applications", round_id=r["id"]))
                          for r in repo().where("recruitment_rounds", academic_year=year)),
        "loans": len(reqs),
        "loans_active": len([r for r in reqs if r.get("status") in ("APPROVED", "ISSUED", "PARTIAL_RETURNED")]),
        "loans_overdue": len([r for r in reqs if is_overdue(r)]),
    }
    rows, total = advisor_ticket_summary()
    return render_template("reports/advisor.html", year=year, usage=usage, rows=rows, total=total)


@dataset("report_activities")
def ds_activities(args):
    if not _allowed() or not _activities_on():
        abort(403)
    year = int(args.get("year") or current_year())
    divs = {d["id"]: d for d in repo().all("divisions")}
    rows = []
    for a in sorted(repo().where("activities", academic_year=year), key=lambda a: a.get("start_at") or ""):
        staff = [s for s in repo().where("activity_staff", activity_id=a["id"]) if s.get("confirm_status") == "CONFIRMED"]
        hours = sum(float(h.get("hours") or 0) for h in repo().where("activity_hours", activity_id=a["id"]))
        rows.append([a.get("title"), (divs.get(a.get("division_id")) or {}).get("name"), a.get("start_at"), a.get("end_at"),
                     a.get("location"), a.get("status"), len(staff), round(hours, 2), a.get("participants"),
                     a.get("result_summary")])
    return f"activities-{year}", ["กิจกรรม", "กอง", "เริ่ม", "สิ้นสุด", "สถานที่", "สถานะ", "ทีมงานยืนยัน", "ชั่วโมงรวม",
                                  "ผู้เข้าร่วม", "สรุปผล"], rows


@dataset("report_hours")
def ds_hours(args):
    """ชั่วโมงกิจกรรมรวมรายบุคคล"""
    if not _allowed() or not _activities_on():
        abort(403)
    total = Counter()
    for h in repo().all("activity_hours"):
        total[h["user_id"]] += float(h.get("hours") or 0)
    rows = []
    for uid, hrs in total.items():
        u = repo().get("users", uid) or {}
        rows.append([u.get("student_code"), u.get("first_name"), u.get("last_name"), u.get("program_code"), round(hrs, 2)])
    rows.sort(key=lambda r: -r[4])
    return "activity-hours-total", ["รหัสนักศึกษา", "ชื่อ", "นามสกุล", "รหัสสาขา", "ชั่วโมงรวม"], rows
