"""ชุดคัดลอก/ส่งออกข้อมูล (หัวข้อ 8, EXP-01)

ทุกโมดูลลงทะเบียนชุดข้อมูลด้วย @dataset("ชื่อ") ฟังก์ชันคืน (ชื่อไฟล์, headers, rows)
และต้องตรวจสิทธิ์เอง (abort(403)) ตามขอบเขตของผู้ใช้
ปุ่มคัดลอกและไฟล์ส่งออกเรียกฟังก์ชันเดียวกัน จึงได้คอลัมน์และลำดับตรงกันเสมอ
"""
import csv
import io

from flask import Blueprint, Response, abort, request

from .db import repo
from .rbac import current_user, login_required
from .utils import now, now_iso

bp = Blueprint("export", __name__, url_prefix="/export")
DATASETS = {}


def dataset(name):
    def deco(fn):
        DATASETS[name] = fn
        return fn
    return deco


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "ใช่" if v else "ไม่ใช่"
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _log(name, method, count):
    u = current_user()
    repo().add("export_jobs", {
        "user_id": u["id"], "entity_type": name, "method": method,
        "row_count": count, "params": dict(request.args), "created_at": now_iso(),
    })


@bp.route("/<name>")
@login_required
def export(name):
    fn = DATASETS.get(name)
    if not fn:
        abort(404)
    fmt = request.args.get("fmt", "tsv")
    filename, headers, rows = fn(request.args)
    rows = [[_clean(c) for c in r] for r in rows]
    headers = [_clean(h) for h in headers]
    stamp = now().strftime("%Y%m%d-%H%M")
    if fmt == "tsv":
        # คัดลอกลงคลิปบอร์ด: แถวแรกหัวคอลัมน์ คั่นแท็บ
        if len(rows) == 1 and request.args.get("single") == "1":
            text = "\n".join(f"{h}: {v}" for h, v in zip(headers, rows[0]))
        else:
            text = "\n".join("\t".join(r) for r in [headers] + rows)
        _log(name, "CLIPBOARD", len(rows))
        return Response(text, mimetype="text/plain; charset=utf-8", headers={"X-Row-Count": str(len(rows))})
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        data = "﻿" + buf.getvalue()  # UTF-8 with BOM (NFR-16)
        _log(name, "CSV", len(rows))
        return Response(data.encode("utf-8"), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}-{stamp}.csv"'})
    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font

        wb = Workbook()
        ws = wb.active
        ws.title = "data"
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        # เก็บทุกเซลล์เป็นข้อความ เพื่อรักษาศูนย์นำหน้า (รหัสสาขา)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.number_format = "@"
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
        out = io.BytesIO()
        wb.save(out)
        _log(name, "XLSX", len(rows))
        return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{filename}-{stamp}.xlsx"'})
    abort(400)
