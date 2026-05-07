import os
import json
import requests
import threading
import base64
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import func

app = Flask(__name__, static_folder="static")
CORS(app)

# ─── DB setup ───────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///tasks.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Transfer(Base):
    __tablename__ = "transfer"
    id = Column(Integer, primary_key=True, index=True)
    status = Column(String, index=True, default="pending")   # pending | transferring | done | error
    date_created = Column(DateTime(timezone=True), server_default=func.now())
    file_id = Column(String)
    file_name = Column(String)
    file_size = Column(String)
    dropbox_path = Column(String)
    error_msg = Column(Text)


class Task(Base):
    __tablename__ = "task"
    id = Column(Integer, primary_key=True, index=True)
    status = Column(String, index=True, default="draft")
    sort = Column(Integer, default=0)
    date_created = Column(DateTime(timezone=True), server_default=func.now())
    url = Column(String)


Base.metadata.create_all(bind=engine)

# ─── PikPak API helpers ──────────────────────────
PIKPAK_API = "https://user.mypikpak.com"
PIKPAK_DRIVE_API = "https://api-drive.mypikpak.com"
CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Copvx5vS60L70I"


def pikpak_get_captcha_token(device_id: str, username: str = "") -> str:
    url = f"{PIKPAK_API}/v1/shield/captcha/init"
    payload = {
        "client_id": CLIENT_ID,
        "action": "POST:/v1/auth/signin",
        "device_id": device_id,
        "captcha_token": "",
        "meta": {"phone_number": username, "email": username},
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
        "X-Device-ID": device_id,
        "X-Client-ID": CLIENT_ID,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    data = r.json()
    return data.get("captcha_token", "")


def pikpak_login(username: str, password: str):
    import uuid
    device_id = uuid.uuid4().hex
    captcha_token = pikpak_get_captcha_token(device_id, username)
    url = f"{PIKPAK_API}/v1/auth/signin"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": username,
        "password": password,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
        "X-Device-ID": device_id,
        "X-Client-ID": CLIENT_ID,
        "X-Captcha-Token": captcha_token,
        "Referer": "https://pc.mypikpak.com",
        "Accept": "*/*",
    }
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def pikpak_headers(token: str):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def pikpak_list_files(token: str, parent_id: str = "", page_token: str = ""):
    url = f"{PIKPAK_DRIVE_API}/drive/v1/files"
    params = {
        "parent_id": parent_id,
        "thumbnail_size": "SIZE_SMALL",
        "with_audit": "true",
        "limit": 100,
        "filters": json.dumps({"trashed": {"eq": False}}),
    }
    if page_token:
        params["page_token"] = page_token
    r = requests.get(url, params=params, headers=pikpak_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def pikpak_offline_download(token: str, url_to_dl: str, parent_id: str = ""):
    endpoint = f"{PIKPAK_DRIVE_API}/drive/v1/files"
    payload = {
        "kind": "drive#file",
        "upload_type": "UPLOAD_TYPE_URL",
        "url": {"url": url_to_dl},
        "parent_id": parent_id,
        "name": "",
    }
    r = requests.post(endpoint, json=payload, headers=pikpak_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def pikpak_delete_files(token: str, file_ids: list):
    url = f"{PIKPAK_DRIVE_API}/drive/v1/files:batchTrash"
    r = requests.post(url, json={"ids": file_ids}, headers=pikpak_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def pikpak_get_file_info(token: str, file_id: str):
    url = f"{PIKPAK_DRIVE_API}/drive/v1/files/{file_id}"
    r = requests.get(url, params={"usage": "FETCH"}, headers=pikpak_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def pikpak_extract_download_url(file_info: dict) -> str:
    """Extract the best download URL from a PikPak file info response."""
    # Prefer web_content_link (direct HTTP link)
    if file_info.get("web_content_link"):
        return file_info["web_content_link"]
    # Try medias[0].link.url
    medias = file_info.get("medias") or []
    for media in medias:
        link = media.get("link") or {}
        if link.get("url"):
            return link["url"]
    # Try links map
    links = file_info.get("links") or {}
    for content_type, link_obj in links.items():
        if isinstance(link_obj, dict) and link_obj.get("url"):
            return link_obj["url"]
    return ""


def _get_pikpak_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


# ─── Dropbox API helpers ─────────────────────────
DROPBOX_CONTENT_API = "https://content.dropboxapi.com/2"
CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB per chunk


def _dbox_auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def dropbox_upload_small(dropbox_token: str, data: bytes, dest_path: str):
    """Upload files ≤ 100 MB directly."""
    api_arg = json.dumps({
        "path": dest_path,
        "mode": "overwrite",
        "autorename": True,
        "mute": True,
    })
    r = requests.post(
        f"{DROPBOX_CONTENT_API}/files/upload",
        headers={
            **_dbox_auth(dropbox_token),
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": api_arg,
        },
        data=data,
        timeout=600,
    )
    r.raise_for_status()
    return r.json()


def dropbox_session_upload(dropbox_token: str, source_url: str, dest_path: str):
    """Streaming session upload for large files (> 100 MB)."""
    auth = _dbox_auth(dropbox_token)

    # 1. Start session
    start = requests.post(
        f"{DROPBOX_CONTENT_API}/files/upload_session/start",
        headers={
            **auth,
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps({"close": False}),
        },
        data=b"",
        timeout=60,
    )
    start.raise_for_status()
    session_id = start.json()["session_id"]
    offset = 0
    buf = b""

    # 2. Stream source → append chunks
    with requests.get(source_url, stream=True, timeout=300) as src:
        src.raise_for_status()
        for raw in src.iter_content(chunk_size=8192):
            if not raw:
                continue
            buf += raw
            while len(buf) >= CHUNK_SIZE:
                chunk = buf[:CHUNK_SIZE]
                buf = buf[CHUNK_SIZE:]
                api_arg = json.dumps({
                    "cursor": {"session_id": session_id, "offset": offset},
                    "close": False,
                })
                r = requests.post(
                    f"{DROPBOX_CONTENT_API}/files/upload_session/append_v2",
                    headers={
                        **auth,
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": api_arg,
                    },
                    data=chunk,
                    timeout=600,
                )
                r.raise_for_status()
                offset += len(chunk)

    # 3. Finish with remaining bytes
    finish_arg = json.dumps({
        "cursor": {"session_id": session_id, "offset": offset},
        "commit": {
            "path": dest_path,
            "mode": "overwrite",
            "autorename": True,
            "mute": True,
        },
    })
    fin = requests.post(
        f"{DROPBOX_CONTENT_API}/files/upload_session/finish",
        headers={
            **auth,
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": finish_arg,
        },
        data=buf,
        timeout=600,
    )
    fin.raise_for_status()
    return fin.json()


def do_transfer(transfer_id: int, pikpak_token: str, dropbox_token: str):
    """Background thread: PikPak → Dropbox transfer."""
    db = SessionLocal()
    try:
        xfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
        if not xfer:
            return

        xfer.status = "transferring"
        db.commit()

        # 1. Get PikPak download URL
        file_info = pikpak_get_file_info(pikpak_token, xfer.file_id)
        dl_url = pikpak_extract_download_url(file_info)
        if not dl_url:
            raise Exception(
                f"PikPak não retornou URL de download. Resposta: {json.dumps(file_info)[:400]}"
            )

        dest_path = xfer.dropbox_path

        # 2. Choose upload strategy based on file size
        file_size = int(xfer.file_size or 0)
        if file_size == 0:
            try:
                head = requests.head(dl_url, timeout=20, allow_redirects=True)
                file_size = int(head.headers.get("content-length", 0))
            except Exception:
                file_size = 0

        if 0 < file_size <= CHUNK_SIZE:
            # Small file: download fully then upload
            r = requests.get(dl_url, timeout=300)
            r.raise_for_status()
            dropbox_upload_small(dropbox_token, r.content, dest_path)
        else:
            # Large / unknown size: streaming session upload
            dropbox_session_upload(dropbox_token, dl_url, dest_path)

        xfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
        xfer.status = "done"
        db.commit()

    except Exception as e:
        db.rollback()
        try:
            xfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
            if xfer:
                xfer.status = "error"
                xfer.error_msg = str(e)[:1000]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ─── Custom error handlers (always return JSON) ──

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found", "path": request.path}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method Not Allowed"}), 405


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal Server Error", "detail": str(e)}), 500


# ─── Routes ─────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    token = data.get("token")
    if token:
        return jsonify({"access_token": token, "method": "token"})
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "Provide token or username+password"}), 400
    try:
        result = pikpak_login(username, password)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/files", methods=["GET"])
def list_files():
    token = _get_pikpak_token()
    if not token:
        return jsonify({"error": "Unauthenticated"}), 401
    parent_id = request.args.get("parent_id", "")
    page_token = request.args.get("page_token", "")
    try:
        return jsonify(pikpak_list_files(token, parent_id, page_token))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/offline", methods=["POST"])
def offline_download():
    token = _get_pikpak_token()
    if not token:
        return jsonify({"error": "Unauthenticated"}), 401
    data = request.json or {}
    url_to_dl = data.get("url")
    parent_id = data.get("parent_id", "")
    if not url_to_dl:
        return jsonify({"error": "url required"}), 400
    try:
        return jsonify(pikpak_offline_download(token, url_to_dl, parent_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/files/delete", methods=["POST"])
def delete_files():
    token = _get_pikpak_token()
    if not token:
        return jsonify({"error": "Unauthenticated"}), 401
    data = request.json or {}
    file_ids = data.get("ids", [])
    if not file_ids:
        return jsonify({"error": "ids required"}), 400
    try:
        return jsonify(pikpak_delete_files(token, file_ids))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/files/<file_id>/download", methods=["GET"])
def get_download_url(file_id):
    token = _get_pikpak_token()
    if not token:
        return jsonify({"error": "Unauthenticated"}), 401
    try:
        return jsonify(pikpak_get_file_info(token, file_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─── Transfer routes ─────────────────────────────

@app.route("/api/transfer", methods=["POST"])
def start_transfer():
    """
    Start a PikPak → Dropbox transfer.
    Headers: Authorization: Bearer <pikpak_token>
    Body: { file_id, file_name, file_size, dropbox_path, dropbox_token }
    """
    pikpak_token = _get_pikpak_token()
    if not pikpak_token:
        return jsonify({"error": "Token PikPak necessário no header Authorization"}), 401

    data = request.json or {}
    dropbox_token = data.get("dropbox_token")
    file_id = data.get("file_id")
    file_name = data.get("file_name", "arquivo")
    file_size = str(data.get("file_size", "0"))
    dropbox_path = data.get("dropbox_path") or f"/PikPak/{file_name}"

    if not dropbox_token:
        return jsonify({"error": "dropbox_token necessário"}), 400
    if not file_id:
        return jsonify({"error": "file_id necessário"}), 400

    db = SessionLocal()
    try:
        xfer = Transfer(
            file_id=file_id,
            file_name=file_name,
            file_size=file_size,
            dropbox_path=dropbox_path,
            status="pending",
        )
        db.add(xfer)
        db.commit()
        db.refresh(xfer)
        xfer_id = xfer.id

        t = threading.Thread(
            target=do_transfer,
            args=(xfer_id, pikpak_token, dropbox_token),
            daemon=True,
        )
        t.start()

        return jsonify({"id": xfer_id, "status": "pending", "dropbox_path": dropbox_path})
    finally:
        db.close()


@app.route("/api/transfer", methods=["GET"])
def list_transfers():
    db = SessionLocal()
    try:
        transfers = db.query(Transfer).order_by(Transfer.date_created.desc()).limit(100).all()
        return jsonify([
            {
                "id": t.id,
                "status": t.status,
                "file_name": t.file_name,
                "file_size": t.file_size,
                "dropbox_path": t.dropbox_path,
                "error_msg": t.error_msg,
                "date_created": t.date_created.isoformat() if t.date_created else None,
            }
            for t in transfers
        ])
    finally:
        db.close()


@app.route("/api/transfer/<int:transfer_id>", methods=["GET"])
def get_transfer(transfer_id):
    db = SessionLocal()
    try:
        t = db.query(Transfer).filter(Transfer.id == transfer_id).first()
        if not t:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "id": t.id,
            "status": t.status,
            "file_name": t.file_name,
            "file_size": t.file_size,
            "dropbox_path": t.dropbox_path,
            "error_msg": t.error_msg,
            "date_created": t.date_created.isoformat() if t.date_created else None,
        })
    finally:
        db.close()


@app.route("/api/transfer/<int:transfer_id>", methods=["DELETE"])
def delete_transfer(transfer_id):
    db = SessionLocal()
    try:
        t = db.query(Transfer).filter(Transfer.id == transfer_id).first()
        if not t:
            return jsonify({"error": "not found"}), 404
        db.delete(t)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ─── Legacy task routes ───────────────────────────

@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    db = SessionLocal()
    try:
        tasks = db.query(Task).order_by(Task.date_created.desc()).limit(100).all()
        return jsonify([{
            "id": t.id, "status": t.status, "url": t.url,
            "date_created": t.date_created.isoformat() if t.date_created else None,
        } for t in tasks])
    finally:
        db.close()


@app.route("/api/tasks", methods=["POST"])
def add_task():
    data = request.json or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "url required"}), 400
    db = SessionLocal()
    try:
        task = Task(url=url, status="draft")
        db.add(task)
        db.commit()
        db.refresh(task)
        return jsonify({"id": task.id, "status": task.status})
    finally:
        db.close()


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return jsonify({"error": "not found"}), 404
        db.delete(task)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
