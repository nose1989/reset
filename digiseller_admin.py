#!/usr/bin/env python3
"""Local Digiseller web admin.

Run:
  python3 -m pip install --user httpx certifi
  cp .env.example .env
  # edit .env and set DIGISELLER_API_KEY
  python3 digiseller_admin.py
  open http://127.0.0.1:8765
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    raise SystemExit("Missing dependency: python3 -m pip install --user httpx certifi")

APP_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = APP_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
API_BASE = "https://api.digiseller.com/api"
APP_VERSION = "v8.4-right-toggle"


@dataclass
class UploadItem:
    filename: str
    content_type: str
    data: bytes


def load_env(path: Path = APP_DIR / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def short(value: Any, length: int = 110) -> str:
    text = clean_text(value)
    return text if len(text) <= length else text[: length - 1] + "…"


TRANSLATE_CACHE: dict[tuple[str, str, str], tuple[str, str]] = {}
LANG_LABELS = {
    "zh": "中文",
    "zh-CN": "中文",
    "ru": "Русский",
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "uk": "Українська",
}
PROTECTED_TOKEN_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w.-]+\.\w+|(?=\b[A-Za-z0-9._:+/@#%=-]{6,}\b)(?=[A-Za-z0-9._:+/@#%=-]*[0-9@._:+/#%=-])[A-Za-z0-9._:+/@#%=-]+")


def lang_label(lang: str) -> str:
    return LANG_LABELS.get(lang, lang or "auto")


def heuristic_language(text: str) -> str:
    value = clean_text(text)
    if not value:
        return ""
    if re.search(r"[\u0400-\u04ff]", value):
        return "ru"
    if re.search(r"[\u4e00-\u9fff]", value):
        return "zh-CN"
    latin = len(re.findall(r"[A-Za-z]", value))
    if latin >= 3:
        return "en"
    return ""


def protect_tokens(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def repl(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"987654321{len(protected) - 1}123456789"

    prepared = PROTECTED_TOKEN_RE.sub(repl, text)
    return prepared, protected


def restore_tokens(text: str, protected: list[str]) -> str:
    restored = text
    for idx, token in enumerate(protected):
        restored = restored.replace(f"987654321{idx}123456789", token)
        restored = restored.replace(f"987654321 {idx} 123456789", token)
    return restored


def google_translate(text: str, target_lang: str, source_lang: str = "auto") -> tuple[str, str]:
    value = clean_text(text)
    if not value:
        return "", ""
    if target_lang in {"zh", "zh-CN"} and heuristic_language(value) in {"zh", "zh-CN"}:
        return value, "zh-CN"
    cache_key = (source_lang, target_lang, value)
    if cache_key in TRANSLATE_CACHE:
        return TRANSLATE_CACHE[cache_key]
    prepared, protected = protect_tokens(value)
    try:
        with httpx.Client(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as translate_http:
            response = translate_http.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": source_lang, "tl": target_lang, "dt": "t", "q": prepared},
            )
            response.raise_for_status()
            data = response.json()
        translated = "".join(part[0] for part in data[0] if part and part[0]).strip()
        translated = restore_tokens(translated, protected)
        detected = str(data[2] or source_lang or "")
        result = (translated or value, detected)
    except Exception:
        result = (value, heuristic_language(value) or source_lang)
    TRANSLATE_CACHE[cache_key] = result
    return result


def detect_buyer_language(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("seller") == 1 or msg.get("is_file"):
            continue
        text = clean_text(msg.get("message"))
        lang = heuristic_language(text)
        if lang and lang not in {"zh", "zh-CN"}:
            return lang
    for msg in reversed(messages):
        if msg.get("seller") == 1 or msg.get("is_file"):
            continue
        text = clean_text(msg.get("message"))
        if text:
            _, lang = google_translate(text, "zh-CN")
            if lang and lang not in {"zh", "zh-CN", "auto"}:
                return lang
    return "en"


def translate_incoming_html(text: str, message_id: Any) -> str:
    try:
        translated, source_lang = google_translate(text, "zh-CN")
    except Exception:
        return h(text)
    if not translated or translated == text or source_lang in {"zh", "zh-CN"}:
        return h(text)
    return (
        f"<div class='translated-message' id='msg-{h(message_id)}'>"
        f"<div class='translated-text'>{h(translated)}</div>"
        f"<div class='original-inline'>原文：{h(text)}</div>"
        f"<div class='original-text' hidden>{h(text)}</div>"
        f"<button class='toggle-original' type='button'>&#26597;&#30475;&#21407;&#25991;</button>"
        f"<span class='translation-label'>{h(lang_label(source_lang))} → &#20013;&#25991;</span>"
        f"</div>"
    )


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def looks_like_image_name(value: Any) -> bool:
    text = clean_text(value).lower().split("?", 1)[0]
    return text.endswith(IMAGE_EXTENSIONS)


def attachment_html(msg: dict[str, Any], allow_guess_preview: bool = False) -> str:
    text = clean_text(msg.get("message") or msg.get("text") or "")
    filename = msg.get("filename") or text
    is_image = msg.get("is_img") == 1 or looks_like_image_name(filename)
    url = msg.get("url") or ""
    preview = msg.get("preview") or ""
    if allow_guess_preview and is_image and not preview and msg.get("id"):
        preview = "https://graph.digiseller.ru/img_deb.ashx?f=" + urllib.parse.quote(f"{msg.get('id')}/{filename}") + "&w=360"
    if not filename and not url and not preview:
        return h(text)
    name_html = f"<span class='file-name'>{h(filename or 'file')}</span>"
    link = url or preview
    open_html = f" · <a href='{h(link)}' target='_blank'>open</a>" if link else ""
    if is_image and preview:
        image_html = f"<a href='{h(link or preview)}' target='_blank'><img class='thumb' src='{h(preview)}' loading='lazy'></a>"
    else:
        image_html = "<div class='image-note'>No image preview URL returned by API</div>" if is_image else ""
    prefix = h(text) + "<br>" if text and text != filename else ""
    return f"<div class='file-preview'>{prefix}{name_html}{open_html}{image_html}</div>"


class DigisellerClient:
    def __init__(self) -> None:
        load_env()
        self.seller_id = int(os.getenv("DIGISELLER_SELLER_ID", "1437041"))
        self.api_key = os.getenv("DIGISELLER_API_KEY", "").strip()
        self.http = httpx.Client(timeout=35, headers={"Accept": "application/json"})
        self._token: str | None = None
        self.valid_thru: str | None = None

    def configured(self) -> bool:
        return bool(self.api_key)

    def login(self) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("DIGISELLER_API_KEY is missing. Put it in .env")
        ts = int(time.time())
        sign = hashlib.sha256((self.api_key + str(ts)).encode()).hexdigest()
        payload = {"seller_id": self.seller_id, "timestamp": ts, "sign": sign}
        data = self.post("/apilogin", json_body=payload, auth=False)
        if not data.get("token"):
            raise RuntimeError(f"Login failed: {json.dumps(data, ensure_ascii=False)}")
        self._token = data["token"]
        self.valid_thru = data.get("valid_thru")
        return data

    @property
    def token(self) -> str:
        if not self._token:
            self.login()
        assert self._token
        return self._token

    def get(self, path: str, params: dict[str, Any] | None = None, auth: bool = True) -> Any:
        params = dict(params or {})
        if auth:
            params["token"] = self.token
        r = self.http.get(API_BASE + path, params=params)
        r.raise_for_status()
        return r.json()

    def post(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        params: dict[str, Any] | None = None,
    ) -> Any:
        params = dict(params or {})
        if auth:
            params["token"] = self.token
        r = self.http.post(API_BASE + path, params=params, json=json_body or {})
        r.raise_for_status()
        if not r.content:
            return {}
        return r.json()

    def sales(self, days: int, rows: int, page: int = 1) -> dict[str, Any]:
        finish = dt.datetime.utcnow()
        start = finish - dt.timedelta(days=days)
        return self.post(
            "/seller-sells/v2",
            json_body={
                "date_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "date_finish": finish.strftime("%Y-%m-%d %H:%M:%S"),
                "returned": 0,
                "page": page,
                "rows": rows,
            },
        )

    def chats(self, page_size: int = 50, only_unread: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"pageSize": page_size, "page": 1}
        if only_unread:
            params["filter_new"] = 1
        data = self.get("/debates/v2/chats", params=params)
        return data.get("chats", []) if isinstance(data, dict) else []

    def chat_messages(self, order_id: int, count: int = 200) -> list[dict[str, Any]]:
        data = self.get("/debates/v2", params={"id_i": order_id, "count": min(count, 200)})
        return data if isinstance(data, list) else []

    def all_chat_messages(self, order_id: int) -> list[dict[str, Any]]:
        messages = self.chat_messages(order_id, count=200)
        seen = {str(msg.get("id")) for msg in messages if msg.get("id")}
        while messages:
            oldest_id = messages[0].get("id")
            if not oldest_id:
                break
            data = self.get("/debates/v2", params={"id_i": order_id, "count": 200, "old_id": oldest_id})
            older = data if isinstance(data, list) else []
            older = [msg for msg in older if str(msg.get("id")) not in seen]
            if not older:
                break
            for msg in older:
                if msg.get("id"):
                    seen.add(str(msg.get("id")))
            messages = older + messages
        return messages

    def admin_messages(self, only_unread: bool = True) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"count": 100}
        if only_unread:
            params["only_unread"] = 1
        data = self.get("/messages/v2", params=params)
        return data if isinstance(data, list) else []

    def upload_chat_files(self, uploads: list[UploadItem]) -> list[dict[str, str]]:
        if not uploads:
            return []
        files = [
            ("files[]", (item.filename, item.data, item.content_type or "application/octet-stream"))
            for item in uploads
        ]
        r = self.http.post(
            API_BASE + "/debates/v2/upload-preview",
            params={"token": self.token, "lang": "en-US"},
            files=files,
        )
        r.raise_for_status()
        data = r.json()
        uploaded: list[dict[str, str]] = []
        for item in data.get("files", []):
            if int(item.get("error_num") or 0) != 0:
                raise RuntimeError(item.get("error") or item.get("message") or "File upload failed")
            uploaded.append(
                {
                    "newid": str(item.get("newid") or ""),
                    "name": str(item.get("name") or ""),
                    "type": str(item.get("type") or ""),
                }
            )
        return uploaded

    def send_chat_message(self, order_id: int, message: str, uploads: list[UploadItem]) -> None:
        files = self.upload_chat_files(uploads)
        payload: dict[str, Any] = {"message": message, "files": files}
        self.post("/debates/v2/", json_body=payload, params={"id_i": order_id})

    def product(self, product_id: int) -> dict[str, Any]:
        return self.get(f"/products/{product_id}/data", params={"seller_id": self.seller_id, "lang": "en-US"})

    def download_images(self, order_id: int) -> list[dict[str, Any]]:
        order_dir = DOWNLOAD_DIR / str(order_id)
        order_dir.mkdir(parents=True, exist_ok=True)
        saved: list[dict[str, Any]] = []
        for msg in self.all_chat_messages(order_id):
            if msg.get("seller") == 1:
                continue
            filename = msg.get("filename") or clean_text(msg.get("message")) or f"image_{msg.get('id')}.png"
            is_image = msg.get("is_img") == 1 or filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            url = msg.get("url") or msg.get("preview")
            if not is_image or not url:
                continue
            safe = re.sub(r'[\\/:*?"<>|]+', "_", urllib.parse.unquote(filename))
            dest = order_dir / f"{msg.get('id')}_{safe}"
            r = self.http.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://my.digiseller.com/"})
            if r.status_code == 200:
                dest.write_bytes(r.content)
                saved.append({"filename": filename, "date": msg.get("date_written"), "path": dest, "bytes": len(r.content)})
            else:
                saved.append({"filename": filename, "date": msg.get("date_written"), "error": f"HTTP {r.status_code}"})
        return saved


client = DigisellerClient()


def start_auto_reload() -> None:
    watched = [Path(__file__).resolve()]
    mtimes = {path: path.stat().st_mtime for path in watched if path.exists()}

    def watch() -> None:
        while True:
            time.sleep(2)
            for path, old_mtime in list(mtimes.items()):
                try:
                    new_mtime = path.stat().st_mtime
                except OSError:
                    continue
                if new_mtime != old_mtime:
                    print(f"Detected update in {path.name}; restarting...", flush=True)
                    os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=watch, daemon=True).start()


STYLE = """
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;margin:0;background:#f6f8fb;color:#1f2937}a{color:#0b65c2;text-decoration:none}.top{background:#1f7acb;color:white;padding:14px 22px}.top a{color:white;margin-right:18px;font-weight:600}.wrap{padding:22px;max-width:1280px;margin:auto}.card{background:white;border:1px solid #d9e2ec;border-radius:10px;padding:18px;margin:0 0 18px 0;box-shadow:0 1px 2px #0001}table{border-collapse:collapse;width:100%;background:white}th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;vertical-align:top;font-size:14px}th{background:#f3f6fa}.muted{color:#6b7280}.ok{color:#047857;font-weight:700}.bad{color:#b91c1c;font-weight:700}input,button{font-size:14px;padding:8px;border:1px solid #cbd5e1;border-radius:6px}button{background:#1f7acb;color:white;cursor:pointer}.msg-seller{background:#eef6ff}.msg-buyer{background:#fff}.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px}

.messages-layout{display:grid;grid-template-columns:360px minmax(0,1fr);height:calc(100vh - 120px);min-height:0;background:white;border:1px solid #d9e2ec;border-radius:12px;overflow:hidden;box-shadow:0 1px 2px #0001}.conversation-list{border-right:1px solid #e5e7eb;overflow-y:scroll;min-height:0;background:#fff}.conversation-title{font-size:34px;font-weight:800;padding:22px 22px 14px}.conversation-item{display:grid;grid-template-columns:48px minmax(0,1fr) auto;gap:12px;padding:12px 14px;border-bottom:1px solid #eef2f7;color:#1f2937}.conversation-item:hover{background:#f4f8ff}.conversation-item.active{background:#3f85d6;color:#fff}.conversation-item.active .muted,.conversation-item.active .preview{color:#eaf2ff}.avatar{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#111827;color:#fff;font-weight:800}.conversation-name{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.preview{color:#9ca3af;line-height:1.25;max-height:38px;overflow:hidden}.conversation-time{font-size:14px;white-space:nowrap}.badge{display:inline-block;min-width:18px;padding:2px 6px;border-radius:999px;background:#ef4444;color:white;font-size:12px;text-align:center;margin-top:6px}.conversation-panel{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;background:#fff}.conversation-header{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;padding:18px 24px;border-bottom:1px solid #e5e7eb}.conversation-header-title{font-size:18px;font-weight:800}.conversation-body{padding:20px 24px;overflow-y:scroll;min-height:0;flex:1 1 auto;scrollbar-gutter:stable}.chat-row{margin:0 0 18px}.chat-meta{display:flex;justify-content:space-between;gap:12px;color:#6b7280;font-size:13px;margin-bottom:6px}.chat-author{font-weight:800;color:#1f2937}.chat-bubble{display:inline-block;max-width:78%;border-radius:10px;padding:10px 12px;line-height:1.45;background:#f3f4f6;white-space:pre-wrap;text-align:left}.chat-row.seller{text-align:right}.chat-row.seller .chat-meta{justify-content:flex-end}.chat-row.seller .chat-bubble{background:#eef6ff}.chat-row.buyer .chat-bubble{background:#fff;border:1px solid #e5e7eb}.toolbar a{margin-left:12px}.empty-state{padding:40px;color:#6b7280;text-align:center}.conversation-list::-webkit-scrollbar,.conversation-body::-webkit-scrollbar,.reply-editor::-webkit-scrollbar{width:12px}.conversation-list::-webkit-scrollbar-thumb,.conversation-body::-webkit-scrollbar-thumb,.reply-editor::-webkit-scrollbar-thumb{background:#94a3b8;border-radius:999px;border:3px solid #f8fafc}@media(max-width:850px){.messages-layout{grid-template-columns:1fr;height:calc(100vh - 110px)}.conversation-panel{min-height:0}.conversation-list{max-height:260px;border-right:0;border-bottom:1px solid #e5e7eb}}

.alert-controls{position:fixed;right:18px;bottom:18px;z-index:50;display:flex;gap:8px;align-items:center}.alert-button{background:#16a34a;color:#fff;border:0;border-radius:999px;padding:10px 14px;font-weight:800;box-shadow:0 4px 14px #0002}.alert-button.off{background:#64748b}.alert-pill{display:none;background:#dc2626;color:#fff;border-radius:999px;padding:9px 12px;font-weight:800;box-shadow:0 4px 14px #0002}.alert-pill.show{display:inline-block}.unread-dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#ef4444;margin-left:6px}
.thumb{max-width:220px;max-height:160px;border:1px solid #e5e7eb;border-radius:8px;display:block;margin-top:8px;background:#f8fafc}.file-preview{margin-top:6px}.file-name{font-weight:700}.image-note{font-size:12px;color:#6b7280;margin-top:4px}
.reply-editor{flex:0 0 auto;max-height:260px;overflow-y:auto;border-top:1px solid #e5e7eb;background:#f8fafc;padding:14px 18px}.reply-editor textarea{width:100%;min-height:92px;box-sizing:border-box;resize:vertical;border:1px solid #cbd5e1;border-radius:8px;padding:10px;font:14px/1.45 inherit;background:white}.reply-toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.reply-toolbar button{background:#e0ecff;color:#0f3b66;border-color:#b9d4ff}.reply-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px}.reply-actions input[type=file]{background:white;max-width:480px}.reply-hint,.selected-files{font-size:13px;color:#64748b}.notice{border-radius:8px;padding:9px 12px;margin:0 0 10px}.notice.ok-bg{background:#dcfce7;color:#166534}.notice.bad-bg{background:#fee2e2;color:#991b1b}
.translated-message{white-space:normal}.translated-text,.original-text{white-space:pre-wrap}.toggle-original{margin-top:8px;background:#f1f5f9;color:#334155;border-color:#cbd5e1;padding:5px 8px;font-size:12px}.translation-label{display:inline-block;margin-left:8px;color:#64748b;font-size:12px}
.original-inline{white-space:pre-wrap;color:#64748b;font-size:12px;margin-top:6px;border-top:1px dashed #cbd5e1;padding-top:6px}
</style>
"""


def layout(title: str, body: str) -> bytes:
    nav = f"""
    <div class="top">
      <a href="/">Dashboard</a>
      <a href="/sales">Sales</a>
      <a href="/chats">Messages</a>
      <a href="/unread">Unread</a>
      <a href="/admin-messages">Admin</a>
      <a href="/product">Product</a>
      <span style="float:right;font-weight:700">Digiseller Admin {APP_VERSION}</span>
    </div>
    """
    alert_ui = """
    <div class="alert-controls">
      <span id="unread-alert-pill" class="alert-pill"></span>
      <button id="enable-alerts" class="alert-button off" type="button">Enable alerts</button>
    </div>
    <script>
    (() => {
      const intervalMs = 15000;
      const btn = document.getElementById('enable-alerts');
      const pill = document.getElementById('unread-alert-pill');
      let enabled = localStorage.getItem('digisellerAlertsEnabled') === '1';
      let lastTotal = Number(localStorage.getItem('digisellerLastUnreadTotal') || '0');
      let audioCtx = null;
      const baseTitle = document.title;

      function setButton() {
        btn.textContent = enabled ? 'Alerts on' : 'Enable alerts';
        btn.classList.toggle('off', !enabled);
      }
      function beep() {
        try {
          audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
          const osc = audioCtx.createOscillator();
          const gain = audioCtx.createGain();
          osc.type = 'sine';
          osc.frequency.value = 880;
          gain.gain.setValueAtTime(0.001, audioCtx.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.55);
          osc.connect(gain).connect(audioCtx.destination);
          osc.start();
          osc.stop(audioCtx.currentTime + 0.6);
        } catch (e) {}
      }
      function speak(text) {
        try {
          const u = new SpeechSynthesisUtterance(text);
          u.lang = 'zh-CN';
          u.rate = 1;
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(u);
        } catch (e) {}
      }
      function browserNotify(text, url) {
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        const n = new Notification('Digiseller 新消息', {body: text});
        n.onclick = () => { window.focus(); if (url) location.href = url; };
      }
      function alertUnread(data, force=false) {
        const latest = data.latest || {};
        const who = latest.email || '买家';
        const order = latest.order_id || '';
        const text = `Digiseller 有新的未读消息，来自 ${who}，订单 ${order}`;
        beep();
        speak(text);
        browserNotify(text, latest.url);
      }
      async function poll(force=false) {
        try {
          const res = await fetch('/api/unread-count', {cache: 'no-store'});
          const data = await res.json();
          const total = Number(data.total || 0);
          pill.textContent = total > 0 ? `Unread ${total}` : '';
          pill.classList.toggle('show', total > 0);
          document.title = total > 0 ? `(${total}) ${baseTitle}` : baseTitle;
          if (enabled && total > 0 && (force || total > lastTotal)) alertUnread(data, force);
          lastTotal = total;
          localStorage.setItem('digisellerLastUnreadTotal', String(lastTotal));
        } catch (e) {}
      }
      btn.addEventListener('click', async () => {
        enabled = true;
        localStorage.setItem('digisellerAlertsEnabled', '1');
        lastTotal = 0;
        localStorage.setItem('digisellerLastUnreadTotal', '0');
        if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
        setButton();
        beep();
        speak('Digiseller 消息提醒已开启');
        poll(true);
      });
      setButton();
      poll(false);
      setInterval(poll, intervalMs);
    })();
    </script>
    """
    translation_ui = """
    <script>
    document.addEventListener('click', (event) => {
      const button = event.target.closest('.toggle-original');
      if (!button) return;
      const wrap = button.closest('.translated-message');
      const translated = wrap.querySelector('.translated-text');
      const original = wrap.querySelector('.original-text');
      const originalInline = wrap.querySelector('.original-inline');
      const showingOriginal = !original.hidden;
      original.hidden = showingOriginal;
      translated.hidden = !showingOriginal;
      if (originalInline) originalInline.hidden = !showingOriginal;
      button.textContent = showingOriginal ? '查看原文' : '显示中文';
    });
    </script>
    """
    html_doc = f"<!doctype html><html><head><meta charset='utf-8'><title>{h(title)}</title>{STYLE}</head><body>{nav}{alert_ui}{translation_ui}<div class='wrap'>{body}</div></body></html>"
    return html_doc.encode("utf-8")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{h(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def unread_summary() -> dict[str, Any]:
    buyer = [c for c in client.chats(only_unread=True) if int(c.get("cnt_new") or 0) > 0]
    admin: list[dict[str, Any]] = []  # Digiseller admin notices endpoint returns historical items; only buyer chats are counted as unread.
    latest: dict[str, Any] | None = None
    for chat in buyer:
        rec = {
            "type": "buyer",
            "order_id": chat.get("id_i"),
            "email": chat.get("email"),
            "product": clean_text(chat.get("product")),
            "last_date": chat.get("last_date"),
            "cnt_new": int(chat.get("cnt_new") or 0),
            "url": f"/chats?order_id={chat.get('id_i')}",
        }
        if latest is None or str(rec.get("last_date") or "") > str(latest.get("last_date") or ""):
            latest = rec
    total = sum(int(c.get("cnt_new") or 0) for c in buyer) + len(admin)
    return {
        "ok": True,
        "buyer_unread_chats": len(buyer),
        "buyer_unread_messages": sum(int(c.get("cnt_new") or 0) for c in buyer),
        "admin_unread": len(admin),
        "total": total,
        "latest": latest,
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def notify_desktop(text: str) -> None:
    print("\a" + text, flush=True)
    if sys.platform == "darwin":
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.Popen(["say", "-v", "Tingting", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_watch(interval: int = 15) -> None:
    print(f"Watching Digiseller unread messages every {interval}s. Press Ctrl+C to stop.")
    previous_total = 0
    while True:
        try:
            summary = unread_summary()
            total = int(summary.get("total") or 0)
            latest = summary.get("latest") or {}
            print(f"[{summary['checked_at']}] unread={total}", flush=True)
            if total > previous_total:
                who = latest.get("email") or "买家"
                order_id = latest.get("order_id") or ""
                notify_desktop(f"Digiseller 有新的未读消息，来自 {who}，订单 {order_id}")
            previous_total = total
        except Exception as exc:
            print(f"watch error: {exc}", flush=True)
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def send_html(self, title: str, body: str, status: int = 200) -> None:
        data = layout(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def params(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def q(self, name: str, default: str = "") -> str:
        return self.params().get(name, [default])[0]

    def read_form(self) -> tuple[dict[str, str], list[UploadItem]]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 50 * 1024 * 1024:
            raise RuntimeError("Upload is too large; keep total attachments under 50 MB")
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        fields: dict[str, str] = {}
        uploads: list[UploadItem] = []
        if content_type.startswith("multipart/form-data"):
            raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            for part in msg.iter_parts():
                if part.get_content_disposition() != "form-data":
                    continue
                name = part.get_param("name", header="content-disposition") or ""
                filename = part.get_filename()
                data = part.get_payload(decode=True) or b""
                if filename:
                    safe_name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
                    if safe_name and data:
                        uploads.append(UploadItem(safe_name, part.get_content_type(), data))
                elif name:
                    charset = part.get_content_charset() or "utf-8"
                    fields[name] = data.decode(charset, errors="replace")
        else:
            parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            fields = {key: values[0] for key, values in parsed.items() if values}
        return fields, uploads

    def reply_editor(self, order_id: int, target_lang: str) -> str:
        editor_id = f"reply-{order_id}"
        member_template = "\u4f1a\u5458\u4fe1\u606f\uff1a\n\u8d26\u53f7\uff1a\n\u5bc6\u7801\uff1a\n\u767b\u5f55\u5730\u5740\uff1a\n\u4f7f\u7528\u8bf4\u660e\uff1a"
        attachment_template = "\u9644\u4ef6\u8bf4\u660e\uff1a\u8bf7\u67e5\u770b\u672c\u6d88\u606f\u9644\u5e26\u7684\u56fe\u7247\u3001\u9644\u4ef6\u6216\u6587\u6863\u3002"
        reference_template = "\u6587\u732e\u8d44\u6599\uff1a\u8bf7\u67e5\u770b\u9644\u4ef6\u4e2d\u7684\u8d44\u6599\uff0c\u5982\u6709\u95ee\u9898\u8bf7\u7ee7\u7eed\u7559\u8a00\u3002"
        return f"""
        <form id="{editor_id}" class="reply-editor" method="post" action="/chats/send" enctype="multipart/form-data">
          <input type="hidden" name="order_id" value="{order_id}">
          <input type="hidden" name="target_lang" value="{h(target_lang)}">
          <textarea id="{editor_id}-message" name="message" placeholder="&#22312;&#36825;&#37324;&#22238;&#22797;&#20250;&#21592;&#20449;&#24687;&#65292;&#21487;&#22635;&#20889;&#36134;&#21495;&#12289;&#23494;&#30721;&#12289;&#38142;&#25509;&#12289;&#20351;&#29992;&#35828;&#26126;&#31561;&#12290;"></textarea>
          <div class="reply-toolbar">
            <button type="button" data-insert="{h(member_template)}">&#20250;&#21592;&#20449;&#24687;&#27169;&#26495;</button>
            <button type="button" data-insert="{h(attachment_template)}">&#38468;&#20214;&#35828;&#26126;</button>
            <button type="button" data-insert="{h(reference_template)}">&#25991;&#29486;&#35828;&#26126;</button>
          </div>
          <div class="reply-actions">
            <input id="{editor_id}-files" name="files" type="file" multiple accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.rtf,.zip,.rar,.7z">
            <button type="submit">&#21457;&#36865;&#22238;&#22797;</button>
            <span class="reply-hint">&#20013;&#25991;&#20250;&#33258;&#21160;&#32763;&#35793;&#20026; {h(lang_label(target_lang))} &#20877;&#21457;&#36865;&#12290;&#25903;&#25345;&#22270;&#29255;&#12289;&#38468;&#20214;&#21644;&#25991;&#26723;/&#25991;&#29486;&#12290;</span>
          </div>
          <div id="{editor_id}-selected" class="selected-files"></div>
        </form>
        <script>
        (() => {{
          const root = document.getElementById('{editor_id}');
          const textarea = document.getElementById('{editor_id}-message');
          const input = document.getElementById('{editor_id}-files');
          const selected = document.getElementById('{editor_id}-selected');
          root.querySelectorAll('[data-insert]').forEach((button) => {{
            button.addEventListener('click', () => {{
              const text = button.dataset.insert || '';
              const prefix = textarea.value && !textarea.value.endsWith('\\n') ? '\\n' : '';
              textarea.value += prefix + text;
              textarea.focus();
            }});
          }});
          input.addEventListener('change', () => {{
            const names = Array.from(input.files || []).map((file) => file.name);
            selected.textContent = names.length ? `\u5df2\u9009\u62e9\uff1a${{names.join('\u3001')}}` : '';
          }});
          root.addEventListener('submit', () => {{
            const button = root.querySelector('button[type="submit"]');
            button.disabled = true;
            button.textContent = '\u53d1\u9001\u4e2d...';
          }});
        }})();
        </script>
        """
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                return self.home()
            if path == "/sales":
                return self.sales()
            if path == "/chats":
                return self.chats()
            if path == "/chat":
                return self.chat()
            if path == "/unread":
                return self.unread()
            if path == "/admin-messages":
                return self.admin_messages_page()
            if path == "/product":
                return self.product()
            if path == "/download-images":
                return self.download_images()
            if path == "/api/unread-count":
                return self.api_unread_count()
            if path == "/api/version":
                return self.send_json({"version": APP_VERSION, "file": str(Path(__file__).resolve())})
            if path == "/api/chat-debug":
                return self.api_chat_debug()
            if path.startswith("/downloads/"):
                return self.serve_download(path)
            return self.send_html("Not found", "<div class='card bad'>Not found</div>", 404)
        except Exception as exc:
            self.send_html("Error", f"<div class='card bad'>Error</div><pre class='card code'>{h(exc)}</pre>", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/chats/send":
                return self.send_chat_reply()
            return self.send_html("Not found", "<div class='card bad'>Not found</div>", 404)
        except Exception as exc:
            self.send_html("Error", f"<div class='card bad'>Error</div><pre class='card code'>{h(exc)}</pre>", 500)

    def send_chat_reply(self) -> None:
        fields, uploads = self.read_form()
        order_id = int(fields.get("order_id", "0") or 0)
        message = fields.get("message", "").strip()
        target_lang = fields.get("target_lang", "").strip() or "en"
        if not order_id:
            raise RuntimeError("Order ID is missing")
        if not message and not uploads:
            raise RuntimeError("Type a message or choose at least one file")
        if message and target_lang not in {"zh", "zh-CN"} and heuristic_language(message) in {"zh", "zh-CN"}:
            message, _ = google_translate(message, target_lang, "zh-CN")
        client.send_chat_message(order_id, message, uploads)
        self.redirect(f"/chats?order_id={order_id}&sent=1&tl={urllib.parse.quote(target_lang)}")

    def home(self) -> None:
        configured = client.configured()
        status = "<span class='ok'>configured</span>" if configured else "<span class='bad'>missing .env</span>"
        login_info = ""
        if configured:
            try:
                data = client.login()
                login_info = f"<div class='stat'>API login: <span class='ok'>OK</span><br>seller_id: {h(data.get('seller_id'))}<br>valid_thru: {h(data.get('valid_thru'))}</div>"
            except Exception as exc:
                login_info = f"<div class='stat bad'>API login failed: {h(exc)}</div>"
        body = f"""
        <div class='card'><h2>Digiseller Local Admin</h2><p>Config: {status}</p><p class='muted'>API Key is read from <code>.env</code>; never paste it into code or chat.</p></div>
        <div class='grid'>{login_info}<div class='stat'><b>Quick links</b><br><a href='/sales'>Recent sales</a><br><a href='/unread'>Unread messages</a><br><a href='/chats'>Buyer chats</a></div></div>
        """
        self.send_html("Dashboard", body)

    def sales(self) -> None:
        days = int(self.q("days", "7"))
        rows = int(self.q("rows", "50"))
        page = int(self.q("page", "1"))
        data = client.sales(days, rows, page)
        trs = []
        for r in data.get("rows", []):
            trs.append([
                h(r.get("date_pay")),
                f"<a href='/chats?order_id={h(r.get('invoice_id'))}'>{h(r.get('invoice_id'))}</a>",
                h(r.get("product_id")),
                h(short(r.get("product_name"), 90)),
                h(f"{r.get('amount_in')} {r.get('amount_currency')}"),
                h(r.get("partner_id")),
                h(r.get("referer")),
            ])
        form = f"""<form><input name='days' value='{days}' size='4'> days <input name='rows' value='{rows}' size='4'> rows <button>Refresh</button></form>"""
        body = f"<div class='card'><h2>Sales</h2>{form}<p>total_rows={h(data.get('total_rows'))} pages={h(data.get('pages'))}</p></div>" + table(["Paid", "Order", "Product ID", "Product", "Amount", "Partner", "Referer"], trs)
        self.send_html("Sales", body)

    def chats(self) -> None:
        chats = client.chats(page_size=100)
        selected_order = int(self.q("order_id", "0") or 0)
        if not selected_order and chats:
            selected_order = int(chats[0].get("id_i") or 0)

        items = []
        selected_chat: dict[str, Any] | None = None
        selected_messages: list[dict[str, Any]] = []
        for chat in chats:
            order_id = int(chat.get("id_i") or 0)
            if order_id == selected_order:
                selected_chat = chat
                selected_messages = client.all_chat_messages(order_id)
            email = str(chat.get("email") or "unknown")
            name = email.split("@", 1)[0] or email
            initials = (name[:1] or "?").upper()
            unread = int(chat.get("cnt_new") or 0)
            active = " active" if order_id == selected_order else ""
            preview = short(chat.get("product"), 80)
            when = str(chat.get("last_date") or "")
            short_when = when[11:16] if len(when) >= 16 else when
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            items.append(
                f"<a class='conversation-item{active}' href='/chats?order_id={order_id}'>"
                f"<div class='avatar'>{h(initials)}</div>"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                f"<div class='conversation-time'>{h(short_when)}{badge}</div></a>"
            )

        if selected_order and selected_chat is None:
            selected_messages = client.all_chat_messages(selected_order)
            if selected_messages:
                selected_chat = {"id_i": selected_order, "email": f"order-{selected_order}", "product": "Direct order lookup"}

        if selected_chat:
            buyer_name = str(selected_chat.get("email") or "Buyer").split("@", 1)[0]
            buyer_lang = detect_buyer_language(selected_messages)
            header = (
                f"<div><div class='conversation-header-title'>{h(buyer_name)}</div>"
                f"<div class='muted'>Order {h(selected_chat.get('id_i'))} · {h(short(selected_chat.get('product'), 110))} · Messages loaded: {len(selected_messages)} · Reply language: {h(lang_label(buyer_lang))}</div></div>"
                f"<div class='toolbar'><a href='/chat?order_id={selected_order}'>Table view</a>"
                f"<a href='/download-images?order_id={selected_order}'>Download images</a></div>"
            )
            rows = []
            total_messages = len(selected_messages)
            for idx, msg in enumerate(selected_messages, 1):
                is_seller = msg.get("seller") == 1
                cls = "seller" if is_seller else "buyer"
                author = "nose1989" if is_seller else buyer_name
                text = clean_text(msg.get("message"))
                try:
                    if msg.get("is_file"):
                        text_html = attachment_html(msg)
                    else:
                        text_html = translate_incoming_html(text, msg.get("id"))
                except Exception as exc:
                    text_html = h(text or f"Message render error: {exc}")
                msg_no = f"#{idx}/{total_messages}"
                msg_id = f" · ID {h(msg.get('id'))}" if msg.get("id") else ""
                rows.append(
                    f"<div class='chat-row {cls}'>"
                    f"<div class='chat-meta'><span class='chat-author'>{h(author)} <span class='muted'>{msg_no}{msg_id}</span></span><span>{h(msg.get('date_written'))}</span></div>"
                    f"<div class='chat-bubble'>{text_html}</div></div>"
                )
            notice = ""
            if self.q("sent") == "1":
                notice = "<div class='notice ok-bg'>&#22238;&#22797;&#24050;&#21457;&#36865;&#65292;&#27491;&#22312;&#26174;&#31034;&#26368;&#26032;&#23545;&#35805;&#12290;</div>"
            panel = f"<div class='conversation-panel'><div class='conversation-header'>{header}</div><div class='conversation-body'>{notice}{''.join(rows)}</div>{self.reply_editor(selected_order, buyer_lang)}</div>"
        else:
            panel = "<div class='conversation-panel'><div class='empty-state'>No chats found</div></div>"

        body = f"<div class='messages-layout'><div class='conversation-list'><div class='conversation-title'>Messages</div>{''.join(items)}</div>{panel}</div>"
        self.send_html("Messages", body)

    def chat(self) -> None:
        order_id = int(self.q("order_id", "0"))
        if not order_id:
            return self.send_html("Chat", "<div class='card'>Pass ?order_id=...</div>")
        msgs = client.all_chat_messages(order_id)
        rows = []
        for m in msgs:
            who = "Seller" if m.get("seller") == 1 else "Buyer"
            cls = "msg-seller" if m.get("seller") == 1 else "msg-buyer"
            text = clean_text(m.get("message"))
            if m.get("is_file"):
                text = attachment_html(m)
            else:
                text = h(text)
            rows.append([h(m.get("date_written")), h(who), f"<div class='{cls}'>{text}</div>"])
        body = f"<div class='card'><h2>Chat {order_id}</h2><p><a href='/download-images?order_id={order_id}'>Download buyer images</a></p></div>" + table(["Date", "Who", "Message"], rows)
        self.send_html("Chat", body)

    def unread(self) -> None:
        buyer = [c for c in client.chats(only_unread=True) if int(c.get("cnt_new") or 0) > 0]
        admin: list[dict[str, Any]] = []  # Do not count historical admin notices as unread.
        b_rows = [[f"<a href='/chats?order_id={h(c.get('id_i'))}'>{h(c.get('id_i'))}</a>", h(c.get("last_date")), h(c.get("cnt_new")), h(c.get("email")), h(short(c.get("product"), 100))] for c in buyer]
        a_rows = [[h(m.get("date")), h(m.get("id")), h(short(m.get("text") or m.get("message"), 180))] for m in admin]
        body = f"<div class='card'><h2>Unread</h2><p>Buyer unread: {len(buyer)} | Admin unread: {len(admin)}</p></div><h3>Buyer chats</h3>{table(['Order','Last','New','Email','Product'], b_rows)}<h3>Admin messages</h3>{table(['Date','ID','Text'], a_rows)}"
        self.send_html("Unread", body)

    def admin_messages_page(self) -> None:
        limit = max(5, min(int(self.q("limit", "20") or 20), 100))
        messages = client.admin_messages(only_unread=False)[:limit]
        rows = []
        for m in messages:
            pseudo = dict(m)
            pseudo["id"] = m.get("id")
            pseudo["text"] = m.get("text") or m.get("message")
            content = attachment_html(pseudo, allow_guess_preview=True)
            rows.append([
                h(m.get("date")),
                h(m.get("id")),
                h(m.get("seller_nickname") or m.get("seller_full_name") or ""),
                content,
            ])
        controls = f"<form><input name='limit' value='{limit}' size='4'> rows <button>Refresh</button> <a href='/admin-messages?limit=20'>20</a> · <a href='/admin-messages?limit=50'>50</a></form>"
        body = "<div class='card'><h2>Admin messages</h2>" + controls + "<p class='muted'>Shows filename plus image preview. Default only renders 20 rows to keep the page fast.</p></div>" + table(["Date", "ID", "From", "Text / Image"], rows)
        self.send_html("Admin messages", body)

    def product(self) -> None:
        pid = self.q("product_id", "")
        form = f"<div class='card'><h2>Product</h2><form><input name='product_id' placeholder='5870983' value='{h(pid)}'><button>Lookup</button></form></div>"
        if not pid:
            return self.send_html("Product", form)
        data = client.product(int(pid))
        product = data.get("product", data)
        summary = {k: product.get(k) for k in ["id", "name", "price", "currency", "is_available", "num_in_stock", "owner", "type_good"] if k in product}
        self.send_html("Product", form + f"<pre class='card code'>{h(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>")

    def download_images(self) -> None:
        order_id = int(self.q("order_id", "0"))
        if not order_id:
            return self.send_html("Images", "<div class='card'>Pass ?order_id=...</div>")
        saved = client.download_images(order_id)
        rows = []
        for item in saved:
            if item.get("path"):
                rel = "/downloads/" + urllib.parse.quote(str(Path(item["path"]).relative_to(DOWNLOAD_DIR)))
                result = f"<a href='{rel}' target='_blank'>download</a> ({h(item.get('bytes'))} bytes)"
            else:
                result = h(item.get("error"))
            rows.append([h(item.get("date")), h(item.get("filename")), result])
        self.send_html("Images", f"<div class='card'><h2>Images for order {order_id}</h2><p>Saved to {h(DOWNLOAD_DIR / str(order_id))}</p></div>" + table(["Date", "Filename", "Result"], rows))

    def api_unread_count(self) -> None:
        self.send_json(unread_summary())

    def api_chat_debug(self) -> None:
        order_id = int(self.q("order_id", "0") or 0)
        if not order_id:
            return self.send_json({"error": "order_id is required"}, 400)
        messages = client.all_chat_messages(order_id)
        self.send_json(
            {
                "version": APP_VERSION,
                "order_id": order_id,
                "count": len(messages),
                "ids": [msg.get("id") for msg in messages],
                "messages": messages,
            }
        )

    def serve_download(self, path: str) -> None:
        rel = urllib.parse.unquote(path.removeprefix("/downloads/"))
        file_path = (DOWNLOAD_DIR / rel).resolve()
        if not str(file_path).startswith(str(DOWNLOAD_DIR.resolve())) or not file_path.exists():
            return self.send_html("Not found", "Not found", 404)
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f"attachment; filename={file_path.name}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        interval = 15
        if "--interval" in sys.argv:
            idx = sys.argv.index("--interval")
            if idx + 1 < len(sys.argv):
                interval = int(sys.argv[idx + 1])
        run_watch(interval=interval)
        return
    host = os.getenv("DIGISELLER_ADMIN_HOST", "127.0.0.1")
    port = int(os.getenv("DIGISELLER_ADMIN_PORT", "8765"))
    start_auto_reload()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Digiseller admin running at http://{host}:{port}")
    print("Open the page and click 'Enable alerts' to allow sound/voice notifications.")
    print("Background watcher: python3 digiseller_admin.py watch --interval 15")
    print("Press Ctrl+C to stop")
    server.serve_forever()


if __name__ == "__main__":
    main()
