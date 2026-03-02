import os
import json
import requests
import time
import base64
from urllib import parse
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import (
    Column, DateTime, Integer, String, create_engine
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
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


def pikpak_get_download_url(token: str, file_id: str):
    url = f"{PIKPAK_DRIVE_API}/drive/v1/files/{file_id}"
    r = requests.get(url, params={"usage": "FETCH"}, headers=pikpak_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def _get_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


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
    token = _get_token()
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
    token = _get_token()
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
    token = _get_token()
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
    token = _get_token()
    if not token:
        return jsonify({"error": "Unauthenticated"}), 401
    try:
        return jsonify(pikpak_get_download_url(token, file_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    db = SessionLocal()
    try:
        tasks = db.query(Task).order_by(Task.date_created.desc()).limit(100).all()
        return jsonify([{
            "id": t.id, "status": t.status, "url": t.url,
            "date_created": t.date_created.isoformat() if t.date_created else None
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
