"""จัดเก็บไฟล์ (ไม่ใช้ Firebase Storage — ใช้ได้กับแผนฟรี Spark)

- ใช้ Firestore: แบ่งไฟล์เป็นชิ้นละ ~900 KB เก็บใน collection `file_chunks`
  ข้อมูลไฟล์อยู่ใน `files/{file_id}` (เอกสาร Firestore จำกัด 1 MiB ต่อเอกสาร)
- ใช้ฐานข้อมูลไฟล์ในเครื่อง (โหมดพัฒนา): เก็บในโฟลเดอร์ LOCAL_UPLOAD_DIR

ไฟล์ทั้งหมดอ่านผ่าน route ที่ตรวจสิทธิ์เท่านั้น ไม่มี public URL
ไฟล์สำคัญ (เอกสารผู้ยืม ไฟล์แนบคำร้อง) เข้ารหัสด้วย Fernet ก่อนเก็บ
"""
import base64
import hashlib
import os
import uuid

from flask import current_app

from .db import repo
from .utils import now_iso

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
CHUNK_SIZE = 900 * 1024
FS_PREFIX = "fs:"

# ตรวจ magic bytes ขั้นต้น (NFR-03)
_SIGNATURES = {
    "pdf": [b"%PDF"],
    "png": [b"\x89PNG"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF8"],
    "webp": [b"RIFF"],
    "docx": [b"PK"], "xlsx": [b"PK"], "pptx": [b"PK"],
    "doc": [b"\xd0\xcf\x11\xe0"], "xls": [b"\xd0\xcf\x11\xe0"], "ppt": [b"\xd0\xcf\x11\xe0"],
}


class UploadError(ValueError):
    pass


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def max_file_bytes():
    return int(float(current_app.config.get("MAX_FILE_MB", 2)) * 1024 * 1024)


def validate_upload(file_storage, images_only=False, allow_pdf=True):
    """คืน (bytes, ext, content_type) หรือ raise UploadError"""
    if not file_storage or not file_storage.filename:
        raise UploadError("ไม่พบไฟล์")
    ext = _ext(file_storage.filename)
    if images_only:
        allowed = IMAGE_EXT | ({"pdf"} if allow_pdf else set())
    else:
        allowed = ALLOWED_EXT
    if ext not in allowed:
        raise UploadError("ชนิดไฟล์ไม่รองรับ")
    data = file_storage.read()
    if not data:
        raise UploadError("ไฟล์ว่าง")
    if len(data) > max_file_bytes():
        raise UploadError(f"ไฟล์ใหญ่เกิน {current_app.config.get('MAX_FILE_MB', 2)} MB")
    sigs = _SIGNATURES.get(ext)
    if sigs and not any(data.startswith(s) for s in sigs):
        raise UploadError("เนื้อหาไฟล์ไม่ตรงกับนามสกุล")
    return data, ext, file_storage.mimetype or "application/octet-stream"


def _fernet():
    from cryptography.fernet import Fernet

    key = current_app.config.get("FILE_ENCRYPTION_KEY")
    if not key:
        # ไม่ได้ตั้งกุญแจแยก: สร้างจาก SECRET_KEY (แนะนำให้ตั้ง FILE_ENCRYPTION_KEY ใน production)
        key = base64.urlsafe_b64encode(hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()).decode()
    return Fernet(key.encode())


def _use_firestore():
    return current_app.config.get("DB_BACKEND") == "firestore"


def save_bytes(folder, data, ext, content_type, encrypt=False):
    """บันทึกไฟล์ คืน path สำหรับเก็บในเอกสาร"""
    if encrypt:
        data = _fernet().encrypt(data)
        content_type = "application/octet-stream"
    file_id = uuid.uuid4().hex
    suffix = f".{ext}{'.enc' if encrypt else ''}"
    if _use_firestore():
        chunks = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)] or [b""]
        for i, chunk in enumerate(chunks):
            repo().set("file_chunks", f"{file_id}_{i}", {"file_id": file_id, "seq": i, "data": chunk})
        repo().set("files", file_id, {"folder": folder, "ext": ext, "encrypted": encrypt, "size": len(data),
                                      "chunks": len(chunks), "content_type": content_type, "created_at": now_iso()})
        return f"{FS_PREFIX}{file_id}{suffix}"
    path = f"{folder}/{file_id}{suffix}"
    full = os.path.join(current_app.config["LOCAL_UPLOAD_DIR"], path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return path


def _fs_id(path):
    return path[len(FS_PREFIX):].split(".", 1)[0]


def load_bytes(path):
    if path.startswith(FS_PREFIX):
        file_id = _fs_id(path)
        meta = repo().get("files", file_id)
        if not meta:
            raise FileNotFoundError(path)
        parts = []
        for i in range(int(meta.get("chunks") or 0)):
            c = repo().get("file_chunks", f"{file_id}_{i}")
            if not c:
                raise FileNotFoundError(path)
            parts.append(bytes(c["data"]))
        data = b"".join(parts)
    else:
        with open(os.path.join(current_app.config["LOCAL_UPLOAD_DIR"], path), "rb") as f:
            data = f.read()
    if path.endswith(".enc"):
        data = _fernet().decrypt(data)
    return data


def delete_file(path):
    try:
        if path.startswith(FS_PREFIX):
            file_id = _fs_id(path)
            meta = repo().get("files", file_id) or {}
            for i in range(int(meta.get("chunks") or 0)):
                repo().delete("file_chunks", f"{file_id}_{i}")
            repo().delete("files", file_id)
        else:
            os.remove(os.path.join(current_app.config["LOCAL_UPLOAD_DIR"], path))
    except Exception:  # noqa: BLE001
        pass


def mimetype_for(path):
    ext = _ext(path.replace(".enc", ""))
    return {
        "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "csv": "text/csv", "txt": "text/plain",
    }.get(ext, "application/octet-stream")
