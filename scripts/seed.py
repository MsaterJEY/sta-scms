"""seed ข้อมูลตั้งต้น และ (ถ้าระบุ) สร้างผู้ดูแลระบบคนแรก

ใช้งาน:
  python scripts/seed.py
  python scripts/seed.py --admin-email admin@example.com --admin-password "รหัสผ่าน"
  python scripts/seed.py --demo        # เพิ่มบัญชีตัวอย่างสำหรับทดสอบ (ห้ามใช้ใน production)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.db import repo  # noqa: E402
from app.seed import run_seed  # noqa: E402
from app.utils import now_iso, parse_student_code  # noqa: E402

DEMO = [
    # (รหัสนักศึกษา, ชื่อ, นามสกุล, ตำแหน่ง)
    ("406765001", "นายก", "ทดสอบ", "PRES"),
    ("406765002", "เลขานายก", "ทดสอบ", "PRES_SEC"),
    ("406765003", "อุปนายกบห", "ทดสอบ", "ADMIN_VP"),
    ("406765004", "เลขาบห", "ทดสอบ", "ADMIN_SEC"),
    ("406765005", "หัวหน้าอุปกรณ์", "ทดสอบ", "EQUIP_HEAD"),
    ("406765006", "หัวหน้าบุคลากร", "ทดสอบ", "PERSONNEL_HEAD"),
    ("406765008", "ประชาสัมพันธ์", "ทดสอบ", "PR_VP"),
    ("406765009", "อุปนายกกิจกรรม", "ทดสอบ", "ACT_VP"),
    ("406865010", "นักศึกษา", "ทดสอบ", None),
    ("406111011", "ต่างคณะ", "ทดสอบ", None),
]


def make_user(code, first, last, password):
    p = parse_student_code(code)
    existing = repo().where("users", student_code=code)
    if existing:
        return existing[0]["id"]
    return repo().add("users", {
        "student_code": code, "email": f"{code}@demo.local", "first_name": first, "last_name": last,
        "account_type": p["account_type"], "level_code": p["level_code"], "admit_year": p["admit_year"],
        "program_code": p["program_code"], "year_level": p["year_level"], "program_source": "AUTO",
        "needs_review": p["needs_review"], "password_hash": generate_password_hash(password), "status": "ACTIVE",
        "student_status": "STUDYING", "system_roles": [], "failed_logins": 0, "created_at": now_iso(),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2569)
    ap.add_argument("--admin-email")
    ap.add_argument("--admin-password")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--demo-password", default="demo12345")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        print("backend:", app.config["DB_BACKEND"])
        run_seed(args.year)
        print("seeded")
        if args.admin_email and args.admin_password:
            email = args.admin_email.lower()
            if not repo().where("users", email=email):
                repo().add("users", {
                    "email": email, "student_code": None, "first_name": "ผู้ดูแล", "last_name": "ระบบ",
                    "account_type": "EXTERNAL", "status": "ACTIVE", "system_roles": ["SYS_ADMIN"],
                    "password_hash": generate_password_hash(args.admin_password), "failed_logins": 0,
                    "created_at": now_iso(),
                })
                print("admin created:", email)
        if args.demo:
            for code, first, last, pos in DEMO:
                uid = make_user(code, first, last, args.demo_password)
                if pos and not repo().where("position_holders", user_id=uid, position_id=pos, academic_year=args.year):
                    repo().add("position_holders", {"user_id": uid, "position_id": pos, "position_code": pos,
                                                    "academic_year": args.year, "start_date": now_iso()[:10],
                                                    "end_date": None, "status": "ACTIVE", "source": "SEED",
                                                    "created_at": now_iso()})
            print(f"demo users created (password: {args.demo_password})")


if __name__ == "__main__":
    main()
