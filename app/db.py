"""ชั้นเข้าถึงข้อมูล (Repository)

ใช้ Cloud Firestore เมื่อมี FIREBASE_CREDENTIALS
ถ้าไม่มีจะใช้ไฟล์ JSON ในเครื่องแทน (สำหรับพัฒนา/ทดสอบเท่านั้น)

ใช้เฉพาะ query แบบเท่ากับ (equality) เพื่อไม่ต้องสร้าง composite index
การเรียงลำดับทำใน Python (ข้อมูลระดับหลักพันรายการ)
"""
import base64
import json
import os
import threading
import uuid

_repo = None


class LocalRepo:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self._write({})

    def _read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, col, doc_id):
        if not doc_id:
            return None
        with self.lock:
            doc = self._read().get(col, {}).get(str(doc_id))
        return dict(doc, id=str(doc_id)) if doc is not None else None

    def add(self, col, data):
        doc_id = uuid.uuid4().hex[:20]
        self.set(col, doc_id, data)
        return doc_id

    def set(self, col, doc_id, data, merge=False):
        data = {k: v for k, v in data.items() if k != "id"}
        with self.lock:
            db = self._read()
            c = db.setdefault(col, {})
            if merge and str(doc_id) in c:
                c[str(doc_id)].update(data)
            else:
                c[str(doc_id)] = data
            self._write(db)
        return str(doc_id)

    def set_many(self, items):
        """เขียนหลายเอกสารในครั้งเดียว: items = [(collection, doc_id, data)]"""
        with self.lock:
            db = self._read()
            for col, doc_id, data in items:
                db.setdefault(col, {})[str(doc_id)] = {k: v for k, v in data.items() if k != "id"}
            self._write(db)

    def update(self, col, doc_id, data):
        data = {k: v for k, v in data.items() if k != "id"}
        with self.lock:
            db = self._read()
            c = db.setdefault(col, {})
            if str(doc_id) not in c:
                raise KeyError(f"{col}/{doc_id} not found")
            c[str(doc_id)].update(data)
            self._write(db)

    def delete(self, col, doc_id):
        with self.lock:
            db = self._read()
            db.get(col, {}).pop(str(doc_id), None)
            self._write(db)

    def where(self, col, **eq):
        with self.lock:
            items = self._read().get(col, {})
        out = []
        for k, v in items.items():
            if all(v.get(f) == val for f, val in eq.items()):
                out.append(dict(v, id=k))
        return out

    def all(self, col):
        return self.where(col)


class FirestoreRepo:
    def __init__(self, cred_raw):
        import firebase_admin
        from firebase_admin import credentials, firestore

        raw = cred_raw.strip()
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        info = json.loads(raw)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(info))
        self.client = firestore.client()

    def _doc(self, snap):
        d = snap.to_dict() or {}
        d["id"] = snap.id
        return d

    def get(self, col, doc_id):
        if not doc_id:
            return None
        snap = self.client.collection(col).document(str(doc_id)).get()
        return self._doc(snap) if snap.exists else None

    def add(self, col, data):
        data = {k: v for k, v in data.items() if k != "id"}
        ref = self.client.collection(col).document()
        ref.set(data)
        return ref.id

    def set(self, col, doc_id, data, merge=False):
        data = {k: v for k, v in data.items() if k != "id"}
        self.client.collection(col).document(str(doc_id)).set(data, merge=merge)
        return str(doc_id)

    def set_many(self, items):
        """เขียนหลายเอกสารด้วย batch (1 round trip ต่อ 400 เอกสาร): items = [(collection, doc_id, data)]"""
        items = list(items)
        for i in range(0, len(items), 400):
            batch = self.client.batch()
            for col, doc_id, data in items[i:i + 400]:
                batch.set(self.client.collection(col).document(str(doc_id)), {k: v for k, v in data.items() if k != "id"})
            batch.commit()

    def update(self, col, doc_id, data):
        data = {k: v for k, v in data.items() if k != "id"}
        self.client.collection(col).document(str(doc_id)).update(data)

    def delete(self, col, doc_id):
        self.client.collection(col).document(str(doc_id)).delete()

    def where(self, col, **eq):
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self.client.collection(col)
        for f, v in eq.items():
            q = q.where(filter=FieldFilter(f, "==", v))
        return [self._doc(s) for s in q.stream()]

    def all(self, col):
        return [self._doc(s) for s in self.client.collection(col).stream()]


def init_repo(app):
    global _repo
    cred = app.config.get("FIREBASE_CREDENTIALS")
    if cred:
        _repo = FirestoreRepo(cred)
        app.config["DB_BACKEND"] = "firestore"
    else:
        _repo = LocalRepo(app.config["LOCAL_DB_PATH"])
        app.config["DB_BACKEND"] = "local"
    return _repo


def repo():
    return _repo
