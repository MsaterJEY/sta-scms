"""1.0 บุคลากรและรับสมัครกรรมการ/อนุกรรมการ (REC-01..11)"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import repo
from ..exports import dataset
from ..rbac import can, current_user, current_year, is_board, login_required, permission_required
from ..services import (approval_state, audit, create_approval, decide, display_name, notify, notify_role,
                        resubmit)
from ..utils import form_dt, now, now_iso, parse_dt, setting, slugify, sort_thai, thai_date

bp = Blueprint("recruit", __name__, url_prefix="/recruit")


def _round(rid):
    r = repo().get("recruitment_rounds", rid)
    if not r:
        abort(404)
    return r


def round_is_open(r):
    if r.get("status") != "OPEN":
        return False
    o, c = parse_dt(r.get("open_at")), parse_dt(r.get("close_at"))
    t = now()
    return (not o or o <= t) and (not c or t <= c)


def round_positions(rid):
    positions = {p["id"]: p for p in repo().all("positions")}
    out = repo().where("round_positions", round_id=rid)
    for rp in out:
        rp["position"] = positions.get(rp["position_id"], {})
    return out


def check_eligibility(r, user, gpa):
    """REC-03 ตรวจคุณสมบัติอัตโนมัติตามเกณฑ์ที่รอบกำหนด"""
    rules = r.get("eligibility_rules") or {}
    reasons = []
    yl = user.get("year_level") or 0
    if rules.get("min_year") and yl < int(rules["min_year"]):
        reasons.append(f"ชั้นปีต่ำกว่า {rules['min_year']}")
    if rules.get("max_year") and yl > int(rules["max_year"]):
        reasons.append(f"ชั้นปีสูงกว่า {rules['max_year']}")
    if rules.get("programs") and user.get("program_code") not in rules["programs"]:
        reasons.append("สาขาไม่อยู่ในกลุ่มที่เปิดรับ")
    if rules.get("min_gpa"):
        try:
            if gpa is None or float(gpa) < float(rules["min_gpa"]):
                reasons.append(f"เกรดเฉลี่ยต่ำกว่า {rules['min_gpa']}")
        except ValueError:
            reasons.append("เกรดเฉลี่ยไม่ถูกต้อง")
    return (not reasons), reasons


def score_summary(round_id):
    """คะแนนรวมถ่วงน้ำหนักต่อใบสมัคร: เฉลี่ยจากผู้ให้คะแนนที่ส่งแล้ว"""
    crit = {c["id"]: c for c in repo().where("score_criteria", round_id=round_id)}
    per = {}
    for s in repo().where("interview_scores", round_id=round_id):
        if not s.get("submitted_at") or s["criterion_id"] not in crit:
            continue
        c = crit[s["criterion_id"]]
        val = float(s["score"]) / float(c.get("max_score") or 1) * float(c.get("weight") or 1)
        per.setdefault(s["application_id"], {}).setdefault(s["scorer_id"], 0)
        per[s["application_id"]][s["scorer_id"]] += val
    total_w = sum(float(c.get("weight") or 1) for c in crit.values()) or 1
    out = {}
    for app_id, scorers in per.items():
        vals = [v / total_w * 100 for v in scorers.values()]
        out[app_id] = {"avg": round(sum(vals) / len(vals), 2), "count": len(vals),
                       "by_scorer": {k: round(v / total_w * 100, 2) for k, v in scorers.items()}}
    return out


# ---------- รายการรอบ ----------
@bp.route("/")
@login_required
def index():
    rounds = sorted(repo().all("recruitment_rounds"), key=lambda r: r.get("open_at") or "", reverse=True)
    if not (can("rec.manage") or can("rec.score") or can("rec.approve")):
        rounds = [r for r in rounds if r.get("status") in ("OPEN", "CLOSED", "ANNOUNCED")]
    for r in rounds:
        r["is_open"] = round_is_open(r)
    return render_template("recruit/index.html", rounds=rounds)


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<rid>/edit", methods=["GET", "POST"])
@permission_required("rec.manage")
def round_form(rid=None):
    r = _round(rid) if rid else None
    positions = [p for p in repo().all("positions") if p.get("level") in ("COMMITTEE", "SUBCOMMITTEE")]
    if request.method == "POST":
        f = request.form
        rtype = f.get("type")
        data = {
            "title": f.get("title", "").strip(), "type": rtype, "description": f.get("description", ""),
            "academic_year": int(f.get("academic_year") or current_year()),
            "open_at": form_dt(f.get("open_at")), "close_at": form_dt(f.get("close_at")),
            "eligibility_rules": {
                "min_year": int(f["min_year"]) if f.get("min_year") else None,
                "max_year": int(f["max_year"]) if f.get("max_year") else None,
                "programs": f.getlist("programs"),
                "min_gpa": float(f["min_gpa"]) if f.get("min_gpa") else None,
            },
        }
        if not data["title"]:
            flash("กรุณาระบุชื่อรอบ", "danger")
            return redirect(request.url)
        if r:
            repo().update("recruitment_rounds", rid, data)
        else:
            data.update({"status": "DRAFT", "created_by": current_user()["id"], "created_at": now_iso()})
            rid = repo().add("recruitment_rounds", data)
        # ตำแหน่งในรอบ (เปิดได้เฉพาะ COMMITTEE/SUBCOMMITTEE)
        existing = {rp["position_id"]: rp for rp in repo().where("round_positions", round_id=rid)}
        for p in positions:
            if p.get("level") != rtype:
                continue
            quota = f.get(f"quota_{p['id']}")
            if quota and int(quota) > 0:
                payload = {"round_id": rid, "position_id": p["id"], "quota": int(quota),
                           "requirements": f.get(f"req_{p['id']}", "")}
                if p["id"] in existing:
                    repo().update("round_positions", existing[p["id"]]["id"], payload)
                else:
                    repo().add("round_positions", payload)
            elif p["id"] in existing and not repo().where("application_choices", round_position_id=existing[p["id"]]["id"]):
                repo().delete("round_positions", existing[p["id"]]["id"])
        # เกณฑ์คะแนน
        names, maxes, weights = f.getlist("crit_name"), f.getlist("crit_max"), f.getlist("crit_weight")
        old_crit = repo().where("score_criteria", round_id=rid)
        if not repo().where("interview_scores", round_id=rid):
            for c in old_crit:
                repo().delete("score_criteria", c["id"])
            for n, m, w in zip(names, maxes, weights):
                if n.strip():
                    repo().add("score_criteria", {"round_id": rid, "name": n.strip(), "max_score": float(m or 10),
                                                  "weight": float(w or 1)})
        audit("recruit.round_save", "recruitment_round", rid, new=data)
        flash("บันทึกรอบรับสมัครแล้ว", "success")
        return redirect(url_for("recruit.detail", rid=rid))
    rps = {rp["position_id"]: rp for rp in repo().where("round_positions", round_id=rid)} if rid else {}
    crits = repo().where("score_criteria", round_id=rid) if rid else []
    programs = sorted(repo().all("programs"), key=lambda p: p["id"])
    return render_template("recruit/round_form.html", r=r, positions=positions, rps=rps, crits=crits,
                           programs=programs, year=current_year())


@bp.route("/<rid>/status", methods=["POST"])
@permission_required("rec.manage")
def set_status(rid):
    r = _round(rid)
    st = request.form.get("status")
    if st not in ("OPEN", "CLOSED", "DRAFT"):
        abort(400)
    if st == "OPEN" and not repo().where("round_positions", round_id=rid):
        flash("ต้องกำหนดตำแหน่งที่เปิดรับอย่างน้อย 1 ตำแหน่ง", "danger")
        return redirect(url_for("recruit.detail", rid=rid))
    repo().update("recruitment_rounds", rid, {"status": st})
    audit("recruit.round_status", "recruitment_round", rid, old=r.get("status"), new=st)
    if st == "OPEN" and not repo().where("announcements", source_type="recruitment_round", source_id=rid):
        _draft_round_announcement(r)
        flash("สร้างร่างประกาศรับสมัครส่งให้ฝ่ายประชาสัมพันธ์แล้ว", "info")
    flash("เปลี่ยนสถานะแล้ว", "success")
    return redirect(url_for("recruit.detail", rid=rid))


def _draft_round_announcement(r):
    """ANN-03 / REC-09 ร่างประกาศอัตโนมัติ ใช้ข้อมูลดิบตรงกับที่คัดลอกได้"""
    lines = [f"ชื่อรอบ: {r.get('title')}", f"ประเภท: {'กรรมการ' if r.get('type') == 'COMMITTEE' else 'อนุกรรมการ'}",
             f"เปิดรับ: {thai_date(r.get('open_at'))}", f"ปิดรับ: {thai_date(r.get('close_at'))}", "ตำแหน่งที่เปิดรับ:"]
    for rp in round_positions(r["id"]):
        lines.append(f"- {rp['position'].get('name')} จำนวน {rp.get('quota')} คน {rp.get('requirements') or ''}")
    title = f"เปิดรับสมัคร{r.get('title')}"
    repo().add("announcements", {
        "category_id": "RECRUIT", "slug": slugify(title), "title": title, "body": "\n".join(lines),
        "status": "DRAFT", "is_pinned": False, "publish_at": None, "author_id": current_user()["id"],
        "source_type": "recruitment_round", "source_id": r["id"], "approval_request_id": None, "created_at": now_iso(),
    })
    notify_role("PR", "มีร่างประกาศรับสมัครใหม่", title, url_for("announcements.manage"))


# ---------- รายละเอียดรอบ ----------
@bp.route("/<rid>")
@login_required
def detail(rid):
    r = _round(rid)
    u = current_user()
    rps = round_positions(rid)
    rp_map = {rp["id"]: rp for rp in rps}
    my_app = next(iter(repo().where("member_applications", round_id=rid, user_id=u["id"])), None)
    my_choices = sorted(repo().where("application_choices", application_id=my_app["id"]), key=lambda c: c["rank"]) if my_app else []
    ctx = {"r": r, "rps": rps, "rp_map": rp_map, "my_app": my_app, "my_choices": my_choices,
           "is_open": round_is_open(r), "max_choices": int(setting("max_member_choices")),
           "board": is_board(u), "crits": repo().where("score_criteria", round_id=rid),
           "my_session": repo().get("interview_sessions", my_app.get("interview_session_id")) if my_app else None}
    if can("rec.manage") or can("rec.score") or can("rec.approve"):
        apps = repo().where("member_applications", round_id=rid)
        users = {a["user_id"]: repo().get("users", a["user_id"]) or {} for a in apps}
        choices = {}
        for c in repo().where("application_choices", round_id=rid):
            choices.setdefault(c["application_id"], []).append(c)
        for v in choices.values():
            v.sort(key=lambda c: c["rank"])
        apps = sort_thai(apps, lambda a: users[a["user_id"]].get("first_name"))
        status_f = request.args.get("status")
        if status_f:
            apps = [a for a in apps if a.get("status") == status_f]
        sessions = sorted(repo().where("interview_sessions", round_id=rid), key=lambda s: s.get("start_at") or "")
        programs = {p["id"]: p for p in repo().all("programs")}
        batches = sorted(repo().where("selection_batches", round_id=rid), key=lambda b: b.get("created_at", ""))
        for b in batches:
            b["state"] = approval_state(repo().get("approval_requests", b.get("approval_request_id")))
        my_scored = {s["application_id"] for s in repo().where("interview_scores", round_id=rid, scorer_id=u["id"])
                     if s.get("submitted_at")}
        summary = score_summary(rid) if (can("rec.manage") or can("rec.view_scores")) else {}
        ctx.update({"apps": apps, "users": users, "choices": choices, "sessions": sessions, "programs": programs,
                    "batches": batches, "my_scored": my_scored, "summary": summary, "status_f": status_f})
    return render_template("recruit/round.html", **ctx)


@bp.route("/<rid>/apply", methods=["POST"])
@login_required
def apply(rid):
    """REC-02 สมัคร: SCI เท่านั้น หนึ่งใบต่อคนต่อรอบ เลือกได้สูงสุด 3 อันดับ ไม่ซ้ำ ผู้ถือ BOARD สมัครไม่ได้"""
    r = _round(rid)
    u = current_user()
    if u.get("account_type") != "SCI":
        flash("เฉพาะนักศึกษาคณะวิทยาศาสตร์ฯ เท่านั้นที่สมัครได้", "danger")
    elif is_board(u):
        flash("ผู้ถือตำแหน่งบอร์ดบริหารสมัครรอบนี้ไม่ได้", "danger")
    elif not round_is_open(r):
        flash("รอบนี้ไม่ได้เปิดรับสมัครในขณะนี้", "danger")
    elif repo().where("member_applications", round_id=rid, user_id=u["id"]):
        flash("คุณสมัครรอบนี้แล้ว", "warning")
    else:
        max_c = int(setting("max_member_choices"))
        picks = [request.form.get(f"choice_{i}") for i in range(1, max_c + 1)]
        picks = [p for p in picks if p]
        valid = {rp["id"] for rp in repo().where("round_positions", round_id=rid)}
        if not picks:
            flash("กรุณาเลือกตำแหน่งอย่างน้อย 1 อันดับ", "danger")
        elif len(set(picks)) != len(picks) or not set(picks) <= valid:
            flash("ห้ามเลือกตำแหน่งซ้ำในใบสมัครเดียว", "danger")
        else:
            gpa = request.form.get("gpa") or None
            ok, reasons = check_eligibility(r, u, gpa)
            aid = repo().add("member_applications", {
                "round_id": rid, "user_id": u["id"], "status": "SUBMITTED", "submitted_at": now_iso(),
                "gpa": float(gpa) if gpa else None, "statement": request.form.get("statement", "")[:3000],
                "auto_eligible": ok, "auto_reasons": reasons, "screened_by": None, "screen_result": None,
                "consent_at": now_iso(),
            })
            for i, p in enumerate(picks, start=1):
                repo().add("application_choices", {"application_id": aid, "round_id": rid, "round_position_id": p, "rank": i})
            repo().add("consents", {"user_id": u["id"], "purpose": "MEMBER_APPLICATION", "ref": aid, "granted_at": now_iso(),
                                    "expires_at": None})
            audit("recruit.apply", "member_application", aid)
            flash("ส่งใบสมัครแล้ว" + ("" if ok else " (ระบบพบว่าอาจไม่ผ่านคุณสมบัติ: " + ", ".join(reasons) + ")"),
                  "success" if ok else "warning")
    return redirect(url_for("recruit.detail", rid=rid))


@bp.route("/application/<aid>/screen", methods=["POST"])
@permission_required("rec.manage")
def screen(aid):
    a = repo().get("member_applications", aid) or abort(404)
    result = request.form.get("result")
    reason = request.form.get("reason", "").strip()
    if result not in ("QUALIFIED", "UNQUALIFIED"):
        abort(400)
    if result == "UNQUALIFIED" and not reason:
        flash("ไม่ผ่านคุณสมบัติต้องระบุเหตุผล", "danger")
        return redirect(url_for("recruit.detail", rid=a["round_id"]))
    repo().update("member_applications", aid, {"status": result, "screen_result": result, "screen_reason": reason,
                                               "screened_by": current_user()["id"], "screened_at": now_iso()})
    audit("recruit.screen", "member_application", aid, new={"result": result, "reason": reason})
    notify(a["user_id"], "ผลตรวจคุณสมบัติใบสมัคร", "ผ่านคุณสมบัติ" if result == "QUALIFIED" else f"ไม่ผ่าน: {reason}",
           url_for("recruit.detail", rid=a["round_id"]))
    return redirect(url_for("recruit.detail", rid=a["round_id"], _anchor="apps"))


@bp.route("/<rid>/sessions", methods=["POST"])
@permission_required("rec.manage")
def add_session(rid):
    _round(rid)
    if request.form.get("action") == "assign":
        sid = request.form.get("session_id")
        s = repo().get("interview_sessions", sid)
        for aid in request.form.getlist("app_ids"):
            a = repo().get("member_applications", aid)
            if a and a["round_id"] == rid and a["status"] in ("QUALIFIED", "INTERVIEW") and s:
                repo().update("member_applications", aid, {"status": "INTERVIEW", "interview_session_id": sid})
                notify(a["user_id"], "นัดสัมภาษณ์", f"{thai_date(s.get('start_at'))} ที่ {s.get('location')}",
                       url_for("recruit.detail", rid=rid))
        flash("จัดผู้สมัครเข้ารอบสัมภาษณ์แล้ว", "success")
    else:
        repo().add("interview_sessions", {"round_id": rid, "start_at": form_dt(request.form.get("start_at")),
                                          "location": request.form.get("location", "").strip()})
        flash("เพิ่มรอบสัมภาษณ์แล้ว", "success")
    return redirect(url_for("recruit.detail", rid=rid, _anchor="interview"))


# ---------- ให้คะแนนสัมภาษณ์ (REC-06) ----------
@bp.route("/application/<aid>/score", methods=["GET", "POST"])
@permission_required("rec.score")
def score(aid):
    a = repo().get("member_applications", aid) or abort(404)
    u = current_user()
    if a["status"] not in ("INTERVIEW", "QUALIFIED"):
        flash("ใบสมัครนี้ยังไม่อยู่ในขั้นสัมภาษณ์", "warning")
        return redirect(url_for("recruit.detail", rid=a["round_id"]))
    crits = repo().where("score_criteria", round_id=a["round_id"])
    mine = {s["criterion_id"]: s for s in repo().where("interview_scores", application_id=aid, scorer_id=u["id"])}
    locked = any(s.get("submitted_at") for s in mine.values())
    if request.method == "POST":
        if locked:
            flash("ส่งคะแนนแล้ว แก้ไขไม่ได้", "danger")
            return redirect(request.url)
        submit = request.form.get("action") == "submit"
        for c in crits:
            raw = request.form.get(f"c_{c['id']}")
            if raw in (None, ""):
                if submit:
                    flash("ต้องให้คะแนนครบทุกเกณฑ์ก่อนส่ง", "danger")
                    return redirect(request.url)
                continue
            val = max(0.0, min(float(raw), float(c.get("max_score") or 10)))
            doc_id = f"{aid}_{c['id']}_{u['id']}"  # unique (การสัมภาษณ์, เกณฑ์, ผู้ให้คะแนน)
            repo().set("interview_scores", doc_id, {
                "application_id": aid, "round_id": a["round_id"], "criterion_id": c["id"], "scorer_id": u["id"],
                "score": val, "submitted_at": now_iso() if submit else None,
            })
        if submit:
            audit("recruit.score_submit", "member_application", aid)
            flash("ส่งคะแนนแล้ว (ล็อก)", "success")
            return redirect(url_for("recruit.detail", rid=a["round_id"], _anchor="score"))
        flash("บันทึกร่างคะแนนแล้ว", "info")
        return redirect(request.url)
    applicant = repo().get("users", a["user_id"])
    choices = sorted(repo().where("application_choices", application_id=aid), key=lambda c: c["rank"])
    rp_map = {rp["id"]: rp for rp in round_positions(a["round_id"])}
    return render_template("recruit/score.html", a=a, crits=crits, mine=mine, locked=locked, applicant=applicant,
                           choices=choices, rp_map=rp_map)


# ---------- ชุดผลคัดเลือก (REC-07, REC-08) ----------
@bp.route("/<rid>/batch", methods=["GET", "POST"])
@bp.route("/<rid>/batch/<bid>/edit", methods=["GET", "POST"])
@permission_required("rec.manage")
def batch_form(rid, bid=None):
    r = _round(rid)
    batch = repo().get("selection_batches", bid) if bid else None
    if batch and batch.get("status") == "APPROVED":
        flash("ชุดนี้อนุมัติแล้ว แก้ไขไม่ได้", "warning")
        return redirect(url_for("recruit.batch_view", bid=bid))
    apps = [a for a in repo().where("member_applications", round_id=rid)
            if a["status"] in ("QUALIFIED", "INTERVIEW", "IN_BATCH", "RESERVE", "DECLINED")]
    in_other = set()
    for b in repo().where("selection_batches", round_id=rid):
        if bid and b["id"] == bid:
            continue
        for res in repo().where("selection_results", batch_id=b["id"]):
            if res.get("result") == "PASS" and not res.get("removed") and res.get("confirm_status") != "DECLINED":
                in_other.add(res["application_id"])
    apps = [a for a in apps if a["id"] not in in_other]
    summary = score_summary(rid)
    if request.method == "POST":
        rows = []
        for a in apps:
            res = request.form.get(f"res_{a['id']}")
            if res in ("PASS", "RESERVE", "FAIL"):
                rows.append((a, res, request.form.get(f"choice_{a['id']}"), request.form.get(f"reason_{a['id']}", "")))
        if not rows:
            flash("ยังไม่ได้เลือกผลของผู้สมัคร", "danger")
            return redirect(request.url)
        if batch:
            for old in repo().where("selection_results", batch_id=batch["id"]):
                repo().delete("selection_results", old["id"])
        else:
            bid = repo().add("selection_batches", {"round_id": rid, "status": "DRAFT", "created_by": current_user()["id"],
                                                   "created_at": now_iso(), "approval_request_id": None,
                                                   "kind": request.form.get("kind", "MAIN")})
        for a, res, choice_id, reason in rows:
            ch = repo().get("application_choices", choice_id) if choice_id else None
            repo().add("selection_results", {
                "batch_id": bid, "round_id": rid, "application_id": a["id"], "user_id": a["user_id"],
                "choice_id": ch["id"] if ch else None, "round_position_id": ch["round_position_id"] if ch else None,
                "result": res, "reason": reason, "avg_score": (summary.get(a["id"]) or {}).get("avg"),
                "removed": False, "confirm_status": None,
            })
            repo().update("member_applications", a["id"], {"status": "IN_BATCH"})
        batch = repo().get("selection_batches", bid)
        req = repo().get("approval_requests", batch.get("approval_request_id")) if batch.get("approval_request_id") else None
        if req:
            resubmit(req)
        else:
            req_id = create_approval("REC_FINAL", "selection_batch", bid, f"ผลคัดเลือก: {r.get('title')}",
                                     url_for("recruit.batch_view", bid=bid))
            repo().update("selection_batches", bid, {"approval_request_id": req_id})
        repo().update("selection_batches", bid, {"status": "PENDING"})
        audit("recruit.batch_submit", "selection_batch", bid)
        flash("ส่งชุดผลคัดเลือกเพื่ออนุมัติแล้ว", "success")
        return redirect(url_for("recruit.batch_view", bid=bid))
    users = {a["user_id"]: repo().get("users", a["user_id"]) or {} for a in apps}
    choices = {}
    for c in repo().where("application_choices", round_id=rid):
        choices.setdefault(c["application_id"], []).append(c)
    for v in choices.values():
        v.sort(key=lambda c: c["rank"])
    existing = {res["application_id"]: res for res in repo().where("selection_results", batch_id=bid)} if bid else {}
    apps.sort(key=lambda a: -(summary.get(a["id"]) or {}).get("avg", -1))
    return render_template("recruit/batch_form.html", r=r, apps=apps, users=users, choices=choices,
                           rp_map={rp["id"]: rp for rp in round_positions(rid)}, summary=summary,
                           existing=existing, batch=batch)


def _batch_rows(bid):
    rows = repo().where("selection_results", batch_id=bid)
    order = {"PASS": 0, "RESERVE": 1, "FAIL": 2}
    rows.sort(key=lambda x: (order.get(x["result"], 9), -(x.get("avg_score") or 0)))
    return rows


def finalize_batch(batch):
    """เมื่ออนุมัติครบ: อัปเดตสถานะใบสมัครและแจ้งผู้สมัคร"""
    if batch.get("status") == "APPROVED":
        return
    repo().update("selection_batches", batch["id"], {"status": "APPROVED", "approved_at": now_iso()})
    for res in _batch_rows(batch["id"]):
        if res.get("removed"):
            repo().update("member_applications", res["application_id"], {"status": "INTERVIEW"})
            continue
        repo().update("member_applications", res["application_id"], {"status": res["result"]})
        msg = {"PASS": "ผ่านการคัดเลือก กรุณายืนยันสิทธิ์", "RESERVE": "อยู่ในรายชื่อสำรอง", "FAIL": "ไม่ผ่านการคัดเลือก"}[res["result"]]
        notify(res["user_id"], "ประกาศผลคัดเลือก", msg, url_for("recruit.my"))
    notify_role("PERSONNEL_HEAD", "ชุดผลคัดเลือกอนุมัติครบแล้ว", "", url_for("recruit.batch_view", bid=batch["id"]))
    audit("recruit.batch_approved", "selection_batch", batch["id"])


@bp.route("/batch/<bid>", methods=["GET", "POST"])
@login_required
def batch_view(bid):
    batch = repo().get("selection_batches", bid) or abort(404)
    if not (can("rec.manage") or can("rec.approve") or can("report.view")):
        abort(403)
    r = _round(batch["round_id"])
    req = repo().get("approval_requests", batch.get("approval_request_id"))
    state = approval_state(req)
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("approve", "reject"):
            if not can("rec.approve"):
                abort(403)
            ok, msg = decide(req, "APPROVE" if action == "approve" else "REJECT", request.form.get("reason", ""))
            flash(msg, "success" if ok else "danger")
            if ok:
                if action == "reject":
                    repo().update("selection_batches", bid, {"status": "RECONSIDER"})
                elif approval_state(repo().get("approval_requests", req["id"]))["approved"]:
                    finalize_batch(repo().get("selection_batches", bid))
        elif action == "remove":
            # ผู้อนุมัติถอดรายชื่อรายคนได้ก่อนยืนยัน
            if not can("rec.approve"):
                abort(403)
            cur = [l for l in state["logs"] if l.get("cycle") == req.get("cycle", 1) and l["decision"] == "APPROVE"]
            res = repo().get("selection_results", request.form.get("result_id"))
            if cur:
                flash("มีผู้อนุมัติแล้วในรอบนี้ ถ้าต้องการถอดรายชื่อให้ปฏิเสธพร้อมเหตุผลเพื่อส่งกลับ", "warning")
            elif res and res["batch_id"] == bid:
                repo().update("selection_results", res["id"], {"removed": not res.get("removed"),
                                                               "removed_by": current_user()["id"]})
                audit("recruit.result_toggle_remove", "selection_result", res["id"])
        elif action == "announce":
            if not can("rec.manage"):
                abort(403)
            if not state["approved"]:
                flash("ประกาศผลได้หลังอนุมัติครบเท่านั้น", "danger")
            else:
                _draft_result_announcement(r, batch, req)
                flash("สร้างร่างประกาศผลส่งให้ฝ่ายประชาสัมพันธ์แล้ว", "success")
        return redirect(url_for("recruit.batch_view", bid=bid))
    rows = _batch_rows(bid)
    users = {x["user_id"]: repo().get("users", x["user_id"]) or {} for x in rows}
    rp_map = {rp["id"]: rp for rp in round_positions(batch["round_id"])}
    summary = score_summary(batch["round_id"]) if can("rec.view_scores") else {}
    scorers = {}
    for s in summary.values():
        for sid in s["by_scorer"]:
            scorers.setdefault(sid, display_name(repo().get("users", sid)))
    return render_template("recruit/batch_view.html", batch=batch, r=r, rows=rows, users=users, rp_map=rp_map,
                           state=state, req=req, summary=summary, scorers=scorers)


def _draft_result_announcement(r, batch, req):
    rp_map = {rp["id"]: rp for rp in round_positions(r["id"])}
    lines = [f"ประกาศผลการคัดเลือก {r.get('title')}", "", "รหัสนักศึกษา\tชื่อ-นามสกุล\tตำแหน่ง\tผล"]
    for res in _batch_rows(batch["id"]):
        if res.get("removed") or res["result"] == "FAIL":
            continue
        u = repo().get("users", res["user_id"]) or {}
        pos = (rp_map.get(res.get("round_position_id")) or {}).get("position", {}).get("name", "-")
        lines.append(f"{u.get('student_code')}\t{display_name(u)}\t{pos}\t{'ผ่าน' if res['result'] == 'PASS' else 'สำรอง'}")
    title = f"ประกาศผลการคัดเลือก {r.get('title')}"
    repo().add("announcements", {
        "category_id": "RECRUIT", "slug": slugify(title), "title": title, "body": "\n".join(lines), "status": "DRAFT",
        "is_pinned": False, "publish_at": None, "author_id": current_user()["id"], "source_type": "selection_batch",
        "source_id": batch["id"], "approval_request_id": req["id"], "created_at": now_iso(),
    })
    notify_role("PR", "มีร่างประกาศผลคัดเลือก", title, url_for("announcements.manage"))


# ---------- ใบสมัครของฉัน ยืนยัน/สละสิทธิ์ (REC-10, REC-11) ----------
@bp.route("/my", methods=["GET", "POST"])
@login_required
def my():
    u = current_user()
    if request.method == "POST":
        res = repo().get("selection_results", request.form.get("result_id"))
        batch = repo().get("selection_batches", res["batch_id"]) if res else None
        if not res or res["user_id"] != u["id"] or res["result"] != "PASS" or res.get("confirm_status") \
                or not batch or batch.get("status") != "APPROVED" or res.get("removed"):
            abort(400)
        if request.form.get("action") == "confirm":
            rp = repo().get("round_positions", res["round_position_id"])
            r = repo().get("recruitment_rounds", res["round_id"])
            hid = repo().add("position_holders", {
                "user_id": u["id"], "position_id": rp["position_id"], "position_code": rp["position_id"],
                "academic_year": int(r.get("academic_year") or current_year()), "start_date": now_iso()[:10],
                "end_date": None, "status": "ACTIVE", "source": "RECRUIT", "selection_result_id": res["id"],
                "created_at": now_iso(),
            })
            repo().update("selection_results", res["id"], {"confirm_status": "CONFIRMED", "position_holder_id": hid})
            repo().update("member_applications", res["application_id"], {"status": "APPOINTED"})
            audit("recruit.confirm", "selection_result", res["id"], new={"holder": hid})
            flash("ยืนยันสิทธิ์และแต่งตั้งเข้าตำแหน่งแล้ว", "success")
        else:
            repo().update("selection_results", res["id"], {"confirm_status": "DECLINED"})
            repo().update("member_applications", res["application_id"], {"status": "DECLINED"})
            audit("recruit.decline", "selection_result", res["id"])
            notify_role("PERSONNEL_HEAD", "มีผู้สละสิทธิ์ กรุณาจัดชุดเลื่อนผู้สำรอง", display_name(u),
                        url_for("recruit.detail", rid=res["round_id"]))
            flash("สละสิทธิ์แล้ว", "info")
        return redirect(url_for("recruit.my"))
    apps = repo().where("member_applications", user_id=u["id"])
    rounds = {a["round_id"]: repo().get("recruitment_rounds", a["round_id"]) or {} for a in apps}
    results = []
    for res in repo().where("selection_results", user_id=u["id"]):
        b = repo().get("selection_batches", res["batch_id"])
        if b and b.get("status") == "APPROVED" and not res.get("removed"):
            res["round"] = rounds.get(res["round_id"]) or repo().get("recruitment_rounds", res["round_id"])
            rp = repo().get("round_positions", res.get("round_position_id")) or {}
            res["position"] = repo().get("positions", rp.get("position_id")) or {}
            results.append(res)
    return render_template("recruit/my.html", apps=apps, rounds=rounds, results=results)


# ---------- ส่งออก ----------
@dataset("recruit_applicants")
def ds_applicants(args):
    if not can("rec.manage"):
        abort(403)
    rid = args.get("round_id")
    rp_map = {rp["id"]: rp for rp in round_positions(rid)}
    programs = {p["id"]: p for p in repo().all("programs")}
    rows = []
    for a in repo().where("member_applications", round_id=rid):
        u = repo().get("users", a["user_id"]) or {}
        ch = sorted(repo().where("application_choices", application_id=a["id"]), key=lambda c: c["rank"])
        names = [(rp_map.get(c["round_position_id"]) or {}).get("position", {}).get("name", "") for c in ch]
        rows.append([u.get("student_code"), u.get("first_name"), u.get("last_name"),
                     (programs.get(u.get("program_code")) or {}).get("name_th"), u.get("year_level"),
                     *(names + ["", "", ""])[:3], a.get("gpa"), a.get("status"), u.get("phone")])
    rows = sort_thai(rows, lambda x: x[1])
    return "applicants", ["รหัสนักศึกษา", "ชื่อ", "นามสกุล", "สาขา", "ชั้นปี", "อันดับ 1", "อันดับ 2", "อันดับ 3",
                          "เกรดเฉลี่ย", "สถานะ", "เบอร์โทร"], rows


@dataset("recruit_results")
def ds_results(args):
    """คัดลอก/ส่งออกผลคัดเลือกได้เฉพาะชุดที่อนุมัติครบ"""
    if not (can("rec.manage") or can("rec.approve") or can("report.view")):
        abort(403)
    batch = repo().get("selection_batches", args.get("batch_id")) or abort(404)
    if not approval_state(repo().get("approval_requests", batch.get("approval_request_id")))["approved"]:
        abort(403)
    rp_map = {rp["id"]: rp for rp in round_positions(batch["round_id"])}
    rows = []
    for res in _batch_rows(batch["id"]):
        if res.get("removed"):
            continue
        u = repo().get("users", res["user_id"]) or {}
        pos = (rp_map.get(res.get("round_position_id")) or {}).get("position", {}).get("name", "")
        rows.append([u.get("student_code"), u.get("first_name"), u.get("last_name"), pos,
                     {"PASS": "ผ่าน", "RESERVE": "สำรอง", "FAIL": "ไม่ผ่าน"}[res["result"]],
                     {"CONFIRMED": "ยืนยัน", "DECLINED": "สละสิทธิ์"}.get(res.get("confirm_status"), "")])
    return "selection-results", ["รหัสนักศึกษา", "ชื่อ", "นามสกุล", "ตำแหน่ง", "ผล", "การยืนยัน"], rows
