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
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
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

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")

APP_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = APP_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
ASSET_DIR = APP_DIR / "assets"
SHINCHAN_LOGO = ASSET_DIR / "shinchan-logo.png"
COMMON_PHRASES_FILE = APP_DIR / "common_phrases.json"
COMMON_PHRASES_DIR = APP_DIR / "common_phrase_files"
COMMON_PHRASES_DIR.mkdir(exist_ok=True)
API_BASE = "https://api.digiseller.com/api"
APP_VERSION = "v8.11-chat-refresh"


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


def sort_time(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            pass
    return 0.0


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
        if msg.get("seller") == 1 or is_attachment_message(msg):
            continue
        text = clean_text(msg.get("message"))
        lang = heuristic_language(text)
        if lang and lang not in {"zh", "zh-CN"}:
            return lang
    for msg in reversed(messages):
        if msg.get("seller") == 1 or is_attachment_message(msg):
            continue
        text = clean_text(msg.get("message"))
        if text:
            _, lang = google_translate(text, "zh-CN")
            if lang and lang not in {"zh", "zh-CN", "auto"}:
                return lang
    return "en"


def save_common_phrase_button(text: str) -> str:
    value = clean_text(text)
    if not value:
        return ""
    return f"<button class='save-common-phrase' type='button' data-text='{h(value)}'>&#20445;&#23384;&#20026;&#24120;&#29992;&#35821;</button>"


def message_text_html(text: str, allow_save: bool = False) -> str:
    actions = save_common_phrase_button(text) if allow_save else ""
    if not actions:
        return h(text)
    return (
        f"<div class='plain-message'>"
        f"<div class='plain-text'>{h(text)}</div>"
        f"<div class='message-actions'>{actions}</div>"
        f"</div>"
    )


def should_translate_text(text: str) -> bool:
    value = clean_text(text)
    if not value:
        return False
    if looks_like_image_name(value):
        return False
    if re.fullmatch(r"[\d\s.,:+#/_-]+", value):
        return False
    return bool(heuristic_language(value))


def translate_incoming_html(text: str, message_id: Any, should_translate: bool = True) -> str:
    source_lang = heuristic_language(text)
    if not should_translate or source_lang in {"zh", "zh-CN"} or not should_translate_text(text):
        return message_text_html(text, allow_save=not should_translate)
    message_key = h(message_id or hashlib.sha1(text.encode("utf-8")).hexdigest()[:12])
    return (
        f"<div class='translated-message' id='msg-{message_key}' data-pending='1'>"
        f"<div class='translated-text'>&#32763;&#35793;&#20013;...</div>"
        f"<div class='original-inline'>&#21407;&#25991;&#65306;{h(text)}</div>"
        f"<div class='original-text' hidden>{h(text)}</div>"
        f"<div class='message-actions'>"
        f"<button class='toggle-original' type='button'>&#26597;&#30475;&#21407;&#25991;</button>"
        f"{save_common_phrase_button(text)}"
        f"<span class='translation-label'>{h(lang_label(source_lang or 'auto'))} → &#20013;&#25991;</span>"
        f"</div>"
        f"</div>"
    )



IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def looks_like_image_name(value: Any) -> bool:
    text = clean_text(value).lower().split("?", 1)[0]
    return text.endswith(IMAGE_EXTENSIONS)


def is_attachment_message(msg: dict[str, Any]) -> bool:
    if msg.get("is_file") or msg.get("is_img") == 1:
        return True
    if msg.get("url") or msg.get("preview"):
        return True
    return looks_like_image_name(msg.get("filename") or msg.get("message") or msg.get("text"))


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
        if auth and r.status_code == 401:
            self._token = None
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
        if auth and r.status_code == 401:
            self._token = None
            params["token"] = self.token
            r = self.http.post(API_BASE + path, params=params, json=json_body or {})
        r.raise_for_status()
        if not r.content:
            return {}
        return r.json()

    def session_get(self, path: str) -> Any:
        session_id = urllib.parse.quote(self.token, safe="")
        r = self.http.get(API_BASE + path.format(session_id=session_id))
        if r.status_code == 401:
            self._token = None
            session_id = urllib.parse.quote(self.token, safe="")
            r = self.http.get(API_BASE + path.format(session_id=session_id))
        r.raise_for_status()
        return r.json()

    def online_setting(self) -> dict[str, Any]:
        data = self.session_get("/getonlinesetting/{session_id}")
        return data if isinstance(data, dict) else {"raw": data}

    def public_seller_url(self) -> str:
        configured = os.getenv("DIGISELLER_PUBLIC_SELLER_URL", "").strip()
        if configured:
            return configured
        if self.seller_id == 1437041:
            return f"https://plati.market/seller/hello1989/{self.seller_id}/?lang=en-US"
        return ""

    def public_seller_online_status(self) -> dict[str, Any]:
        url = self.public_seller_url()
        if not url:
            return {"enabled": False}
        r = self.http.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cache-Control": "no-cache",
                "User-Agent": "Mozilla/5.0",
            },
            follow_redirects=True,
        )
        r.raise_for_status()
        text = r.text
        online = bool(re.search(r">\s*Online\s*<", text, flags=re.I)) and "color-text-success" in text
        offline = bool(re.search(r">\s*Offline\s*<", text, flags=re.I))
        return {"enabled": True, "url": str(r.url), "online": online, "offline": offline}

    def set_online(self) -> dict[str, Any]:
        try:
            value = int(os.getenv("DIGISELLER_ONLINE_VALUE", "1") or "1")
        except ValueError:
            value = 1
        data = self.session_get(f"/setonlinesetting/{{session_id}}/{value}")
        if isinstance(data, dict) and int(data.get("retval") or 0) != 0:
            raise RuntimeError(data.get("desc") or f"setonlinesetting failed: {data}")
        return data if isinstance(data, dict) else {"raw": data}

    def seller_online_status(self) -> dict[str, Any]:
        configured = os.getenv("DIGISELLER_ONLINE_VERIFY_TYPE", "seller").strip() or "seller"
        corr_types = [configured] + [item for item in ("seller", "user", "visitor", "anonym") if item != configured]
        errors = []
        for corr_type in corr_types:
            try:
                data = self.session_get(
                    "/getonlinestatus/{session_id}/"
                    + urllib.parse.quote(corr_type, safe="")
                    + "/"
                    + urllib.parse.quote(str(self.seller_id), safe="")
                )
            except Exception as exc:
                errors.append(f"{corr_type}: {exc}")
                continue
            if isinstance(data, dict):
                if int(data.get("retval") or 0) == 0:
                    data["corr_type"] = corr_type
                    return data
                errors.append(f"{corr_type}: {data.get('desc') or data}")
            else:
                return {"raw": data, "corr_type": corr_type}
        raise RuntimeError("; ".join(errors) or "getonlinestatus failed")

    def messenger_heartbeat(self) -> dict[str, Any]:
        errors = []
        for path in (
            "/checknewchats/{session_id}/-1/0/-1/0",
            "/checknewchats/{session_id}/0/0/-1/0",
            "/checknewchats/{session_id}/-1/-1/-1/-1",
            "/unreadchats/{session_id}/buyer",
            "/chatlist/{session_id}/buyer",
        ):
            try:
                data = self.session_get(path)
            except Exception as exc:
                errors.append(str(exc))
                continue
            if isinstance(data, dict):
                if int(data.get("retval") or 0) == 0:
                    return data
                errors.append(data.get("desc") or f"checknewchats failed: {data}")
            else:
                return {"raw": data}
        raise RuntimeError("; ".join(errors))

    def guest_chats(self, limit: int = 10) -> list[dict[str, Any]]:
        chats: list[dict[str, Any]] = []
        errors = []
        for corr_type in ("visitor", "anonym"):
            try:
                data = self.session_get(f"/chatlist/{{session_id}}/{corr_type}")
            except Exception as exc:
                errors.append(f"{corr_type}: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            for item in data.get("chats") or []:
                if not isinstance(item, dict):
                    continue
                chat = dict(item)
                chat["CorrType"] = str(chat.get("Type") or corr_type)
                chats.append(chat)
        if errors and not chats:
            raise RuntimeError("; ".join(errors))
        chats.sort(key=lambda item: sort_time(item.get("DateWriteUtc") or item.get("DateWrite")), reverse=True)
        return chats[:limit]

    def guest_messages(self, corr_type: str, corr_id: int) -> list[dict[str, Any]]:
        session_id = urllib.parse.quote(self.token, safe="")
        r = self.http.get(
            API_BASE + f"/messages/v3/{session_id}",
            params={"corrType": corr_type, "corrID": corr_id, "lastID": 0, "getDeleted": "false"},
        )
        if r.status_code == 401:
            self._token = None
            session_id = urllib.parse.quote(self.token, safe="")
            r = self.http.get(
                API_BASE + f"/messages/v3/{session_id}",
                params={"corrType": corr_type, "corrID": corr_id, "lastID": 0, "getDeleted": "false"},
            )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            raw_messages = data
        elif isinstance(data, dict):
            raw_messages = data.get("messages") or data.get("Messages") or data.get("items") or data.get("Items") or []
        else:
            raw_messages = []
        messages: list[dict[str, Any]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            messages.append(
                {
                    "id": item.get("ID") or item.get("id"),
                    "seller": 1 if int(item.get("IsAuthor") or item.get("isAuthor") or 0) else 0,
                    "message": item.get("Text") or item.get("text") or "",
                    "date_written": item.get("DateWrite") or item.get("dateWrite") or item.get("DateWriteUtc") or "",
                    "date_seen": item.get("DateSeen") or item.get("dateSeen") or item.get("DateView") or item.get("dateView") or item.get("DateViewed") or item.get("dateViewed") or "",
                    "is_viewed": item.get("IsViewed") or item.get("isViewed"),
                    "is_file": 1 if item.get("FileName") or item.get("fileName") else 0,
                    "filename": item.get("FileName") or item.get("fileName") or "",
                }
            )
        messages.sort(key=lambda item: sort_time(item.get("date_written")))
        return messages

    def mark_guest_read(self, corr_type: str, corr_id: int) -> None:
        self.session_get(
            "/messages/setviewed/{session_id}?corrID="
            + urllib.parse.quote(str(corr_id), safe="")
            + "&corrType="
            + urllib.parse.quote(corr_type, safe="")
        )

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

    def mark_chat_read(self, order_id: int) -> None:
        self.post("/debates/v2/seen", params={"id_i": order_id})

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

    def purchase_info(self, invoice_id: int) -> dict[str, Any]:
        data = self.get(f"/purchase/info/{invoice_id}")
        if not isinstance(data, dict):
            return {}
        content = data.get("content")
        return content if isinstance(content, dict) else data

    def product(self, product_id: int) -> dict[str, Any]:
        return self.get(f"/products/{product_id}/data", params={"seller_id": self.seller_id, "lang": "en-US"})

    def shop_products(self, page: int = 1, rows: int = 500) -> dict[str, Any]:
        return self.post(
            "/shop/products",
            json_body={
                "seller": {"id": self.seller_id},
                "pages": {"num": page, "rows": rows},
                "lang": "en-US",
                "show_all": "0",
            },
        )

    def add_text_stock(self, product_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
        data = self.post("/product/content/add/text", json_body={"product_id": product_id, "content": items})
        if isinstance(data, dict) and int(data.get("retval") or 0) != 0:
            raise RuntimeError(data.get("retdesc") or data.get("desc") or f"Stock upload failed: {data}")
        return data if isinstance(data, dict) else {"raw": data}

    def unique_code(self, unique_code: str) -> dict[str, Any]:
        safe_code = urllib.parse.quote(unique_code, safe="")
        data = self.get(f"/purchases/unique-code/{safe_code}")
        return data if isinstance(data, dict) else {}

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
UNREAD_CACHE: dict[str, Any] = {"time": 0.0, "data": None}
PURCHASE_INFO_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
ONLINE_KEEPALIVE_STATUS: dict[str, Any] = {
    "enabled": False,
    "last_ok": "",
    "last_set": "",
    "last_heartbeat": "",
    "last_error": "",
    "last_checked": "",
    "setting": None,
    "period": None,
    "status": None,
    "verified_online": False,
    "public_online": False,
    "public_url": "",
    "set_error": "",
    "heartbeat_error": "",
    "setting_error": "",
    "verify_error": "",
    "public_error": "",
    "public_checked_at": "",
    "public_checked_ts": 0.0,
    "recovery_error": "",
    "failure_count": 0,
}
CHAT_KEEPALIVE_BROWSER_STATUS: dict[str, Any] = {
    "enabled": False,
    "opened": False,
    "reused": False,
    "last_open": "",
    "error": "",
}


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
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;margin:0;background:#f6f8fb;color:#1f2937}a{color:#0b65c2;text-decoration:none}.top{background:#1f7acb;color:white;padding:10px 22px;display:flex;align-items:center;gap:16px}.brand-logo{width:42px;height:42px;object-fit:contain;border-radius:50%;background:#fff;box-shadow:0 1px 4px #0002}.top a{color:white;font-weight:600}.top-nav{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.top-version{margin-left:auto;font-weight:700;white-space:nowrap}.top-online{border:1px solid #bfdbfe;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:800;white-space:nowrap;background:#dbeafe;color:#0f3b66}.top-online.ok{background:#dcfce7;color:#166534;border-color:#bbf7d0}.top-online.bad{background:#fee2e2;color:#991b1b;border-color:#fecaca}.unique-lookup{position:relative;flex:1 1 320px;max-width:520px}.unique-lookup input{width:100%;box-sizing:border-box;border-color:#7db5e8;border-radius:3px;background:#fff;color:#1f2937;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.unique-results{position:absolute;top:calc(100% + 2px);left:0;right:0;z-index:80;background:#fff;color:#1f2937;border:1px solid #9eb8ce;border-radius:0 0 4px 4px;box-shadow:0 8px 16px #0002;overflow:hidden}.unique-results[hidden]{display:none}.unique-title{background:#eef3f7;color:#5b6b7a;font-size:12px;font-weight:800;padding:8px 12px;text-transform:uppercase}.unique-result{display:flex;align-items:center;gap:12px;width:100%;box-sizing:border-box;padding:12px;background:#fff;color:#1f2937;border:0;border-radius:0;text-align:left}.unique-result:hover{background:#eef6ff}.unique-icon{width:28px;height:28px;flex:0 0 auto;border:1px solid #cbd5e1;background:#f8fafc;color:#64748b;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800}.unique-main{min-width:0}.unique-product{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:700}.unique-meta{display:block;color:#6b7280;font-size:12px;margin-top:2px}.unique-message{padding:12px;color:#6b7280;font-size:14px}.unique-error{color:#b91c1c}.wrap{padding:22px;max-width:1280px;margin:auto}.card{background:white;border:1px solid #d9e2ec;border-radius:10px;padding:18px;margin:0 0 18px 0;box-shadow:0 1px 2px #0001}table{border-collapse:collapse;width:100%;background:white}th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;vertical-align:top;font-size:14px}th{background:#f3f6fa}.muted{color:#6b7280}.ok{color:#047857;font-weight:700}.bad{color:#b91c1c;font-weight:700}input,button{font-size:14px;padding:8px;border:1px solid #cbd5e1;border-radius:6px}button{background:#1f7acb;color:white;cursor:pointer}.msg-seller{background:#eef6ff}.msg-buyer{background:#fff}.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px}

.messages-layout{display:grid;grid-template-columns:360px minmax(0,1fr);height:calc(100vh - 120px);min-height:0;background:white;border:1px solid #d9e2ec;border-radius:12px;overflow:hidden;box-shadow:0 1px 2px #0001}.conversation-list{border-right:1px solid #e5e7eb;overflow-y:scroll;min-height:0;background:#fff}.conversation-title{font-size:34px;font-weight:800;padding:22px 22px 14px}.conversation-item{display:grid;grid-template-columns:48px minmax(0,1fr) auto;gap:12px;padding:12px 14px;border-bottom:1px solid #eef2f7;color:#1f2937}.conversation-item:hover{background:#f4f8ff}.conversation-item.active{background:#3f85d6;color:#fff}.conversation-item.active .muted,.conversation-item.active .preview{color:#eaf2ff}.avatar{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#111827;color:#fff;font-weight:800}.conversation-name{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.preview{color:#9ca3af;line-height:1.25;max-height:38px;overflow:hidden}.conversation-time{font-size:14px;white-space:nowrap}.badge{display:inline-block;min-width:18px;padding:2px 6px;border-radius:999px;background:#ef4444;color:white;font-size:12px;text-align:center;margin-top:6px}.conversation-panel{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;background:#fff}.conversation-panel.loading{position:relative}.chat-loading{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:#64748b;background:#fff}.chat-loading-spinner{width:34px;height:34px;border:4px solid #dbeafe;border-top-color:#1f7acb;border-radius:50%;animation:spin .8s linear infinite}.chat-loading-title{font-weight:800;color:#1f2937}.chat-loading-subtitle{font-size:13px}.loading-line{height:12px;border-radius:999px;background:linear-gradient(90deg,#eef2f7,#dbeafe,#eef2f7);background-size:200% 100%;animation:shine 1.1s linear infinite}.loading-lines{width:min(520px,70%);display:flex;flex-direction:column;gap:10px}.loading-lines .short{width:55%}@keyframes spin{to{transform:rotate(360deg)}}@keyframes shine{to{background-position:-200% 0}}.conversation-header{flex:0 0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 24px;border-bottom:1px solid #e5e7eb}.conversation-header-main{min-width:0}.conversation-header-side{display:flex;flex-direction:column;align-items:flex-end;gap:8px;max-width:360px;text-align:right}.conversation-header-title{font-size:18px;font-weight:800}.order-options{background:#f8fafc;border:1px solid #dbe4ee;border-radius:8px;padding:8px 10px;color:#334155;font-size:13px;line-height:1.35}.order-options-title{color:#64748b;font-size:12px;font-weight:800;margin-bottom:4px}.order-option-name{font-weight:800}.order-option-value{color:#0f172a}.conversation-body{padding:20px 24px;overflow-y:scroll;min-height:0;flex:1 1 auto;scrollbar-gutter:stable}.chat-row{margin:0 0 18px}.chat-meta{display:flex;justify-content:space-between;gap:12px;color:#6b7280;font-size:13px;margin-bottom:6px}.chat-author{font-weight:800;color:#1f2937}.chat-bubble{display:inline-block;max-width:78%;border-radius:10px;padding:10px 12px;line-height:1.45;background:#f3f4f6;white-space:pre-wrap;text-align:left}.chat-row.seller{text-align:right}.chat-row.seller .chat-meta{justify-content:flex-end}.chat-row.seller .chat-bubble{background:#eef6ff}.chat-row.buyer .chat-bubble{background:#fff;border:1px solid #e5e7eb}.read-receipt{display:inline-flex;align-items:center;gap:3px;margin:0 8px;color:#047857;font-size:12px;font-weight:800;white-space:nowrap}.toolbar a{margin-left:12px}.empty-state{padding:40px;color:#6b7280;text-align:center}.conversation-list::-webkit-scrollbar,.conversation-body::-webkit-scrollbar,.reply-editor::-webkit-scrollbar{width:12px}.conversation-list::-webkit-scrollbar-thumb,.conversation-body::-webkit-scrollbar-thumb,.reply-editor::-webkit-scrollbar-thumb{background:#94a3b8;border-radius:999px;border:3px solid #f8fafc}@media(max-width:850px){.messages-layout{grid-template-columns:1fr;height:calc(100vh - 110px)}.conversation-panel{min-height:0}.conversation-list{max-height:260px;border-right:0;border-bottom:1px solid #e5e7eb}.conversation-header{flex-direction:column}.conversation-header-side{align-items:flex-start;text-align:left;max-width:none}}

.alert-controls{position:fixed;right:18px;bottom:18px;z-index:50;display:flex;gap:8px;align-items:center}.alert-button{background:#16a34a;color:#fff;border:0;border-radius:999px;padding:10px 14px;font-weight:800;box-shadow:0 4px 14px #0002}.alert-button.off{background:#64748b}.alert-pill{display:none;background:#dc2626;color:#fff;border-radius:999px;padding:9px 12px;font-weight:800;box-shadow:0 4px 14px #0002}.alert-pill.show{display:inline-block}.unread-dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#ef4444;margin-left:6px}
.thumb{max-width:220px;max-height:160px;border:1px solid #e5e7eb;border-radius:8px;display:block;margin-top:8px;background:#f8fafc}.file-preview{margin-top:6px}.file-name{font-weight:700}.image-note{font-size:12px;color:#6b7280;margin-top:4px}
.reply-editor{flex:0 0 auto;max-height:260px;overflow-y:auto;border-top:1px solid #e5e7eb;background:#f8fafc;padding:14px 18px}.reply-editor textarea{width:100%;min-height:92px;box-sizing:border-box;resize:vertical;border:1px solid #cbd5e1;border-radius:8px;padding:10px;font:14px/1.45 inherit;background:white}.reply-toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.reply-toolbar button{background:#e0ecff;color:#0f3b66;border-color:#b9d4ff}.reply-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px}.reply-dropzone{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px dashed #93c5fd;border-radius:8px;background:#eff6ff;padding:8px 10px;color:#0f3b66}.reply-editor.dragover textarea{border-color:#2563eb;background:#eff6ff}.reply-dropzone.dragover,.reply-editor.dragover .reply-dropzone{background:#dbeafe;border-color:#2563eb}.reply-dropzone input[type=file]{background:white;max-width:360px}.reply-dropzone-text{font-size:13px;font-weight:700}.reply-hint,.selected-files{font-size:13px;color:#64748b}.common-phrases{border-top:1px solid #e5e7eb;background:#f8fafc;padding:10px 18px 14px}.common-phrase-title{font-size:13px;font-weight:800;color:#334155;margin-bottom:8px}.common-phrase-buttons{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}.common-phrase-buttons form{margin:0}.common-phrase-buttons button{background:#e0ecff;color:#0f3b66;border-color:#b9d4ff}.phrase-manager form[action='/phrases/save']{border:1px dashed #cbd5e1;border-radius:10px;padding:12px;background:#fff}.phrase-manager textarea{width:100%;box-sizing:border-box;min-height:130px;resize:vertical}.phrase-manager.dragover form[action='/phrases/save']{border-color:#2563eb;background:#eff6ff}.phrase-manager.dragover textarea{border-color:#2563eb;background:#eff6ff}.phrase-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:start;margin-bottom:10px}.phrase-empty{color:#64748b;font-size:14px}.selected-files{margin-top:10px}.selected-summary{margin-bottom:8px}.file-preview-grid{display:flex;flex-wrap:wrap;gap:8px}.file-chip{display:flex;align-items:center;gap:8px;max-width:230px;border:1px solid #cbd5e1;border-radius:8px;background:white;padding:6px 8px;color:#334155}.file-chip img{width:54px;height:54px;object-fit:cover;border-radius:6px;border:1px solid #e2e8f0;cursor:pointer}.file-chip-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-chip-icon{width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:6px;background:#e2e8f0;color:#475569;font-weight:800}.preview-modal{position:fixed;inset:0;z-index:120;display:flex;align-items:center;justify-content:center;background:#0f172acc;padding:24px}.preview-modal[hidden]{display:none}.preview-modal img{max-width:95vw;max-height:90vh;border-radius:8px;background:white;box-shadow:0 20px 50px #0008}.preview-modal-close{position:absolute;right:18px;top:14px;background:#fff;color:#0f172a;border:0;border-radius:999px;width:34px;height:34px;font-size:22px;line-height:1}.notice{border-radius:8px;padding:9px 12px;margin:0 0 10px}.notice.ok-bg{background:#dcfce7;color:#166534}.notice.bad-bg{background:#fee2e2;color:#991b1b}
.translated-message,.plain-message{white-space:normal}.translated-text,.original-text,.plain-text{white-space:pre-wrap}.message-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px}.toggle-original,.save-common-phrase{background:#f1f5f9;color:#334155;border-color:#cbd5e1;padding:5px 8px;font-size:12px}.save-common-phrase.saved{background:#dcfce7;color:#166534;border-color:#bbf7d0}.save-common-phrase.failed{background:#fee2e2;color:#991b1b;border-color:#fecaca}.translation-label{display:inline-block;color:#64748b;font-size:12px}
.original-inline{white-space:pre-wrap;color:#64748b;font-size:12px;margin-top:6px;border-top:1px dashed #cbd5e1;padding-top:6px}
.phrase-files{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.phrase-file{display:flex;align-items:center;gap:8px;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc;padding:6px 8px}.phrase-file img{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid #e2e8f0}.phrase-file-name{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.phrase-upload{display:flex;align-items:center;justify-content:center;gap:10px;min-height:68px;margin-top:10px;border:1px dashed #93c5fd;border-radius:10px;background:#eff6ff;color:#0f3b66;font-weight:700;padding:10px;cursor:pointer}.phrase-upload input{background:white}.phrase-image-preview,.common-phrase-buttons .common-phrase-preview{border:0;background:transparent;padding:0;cursor:pointer}.phrase-image-preview img,.common-phrase-preview img{display:block;width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid #cbd5e1}.common-phrase-card{display:inline-flex;align-items:center;gap:8px;max-width:360px;border:1px solid #b9d4ff;border-radius:10px;background:#eaf3ff;padding:6px;box-shadow:0 1px 1px #0001}.common-phrase-previews{display:flex;gap:6px;align-items:center;flex-shrink:0}.common-phrase-card .common-phrase-send{display:flex;flex-direction:column;align-items:flex-start;gap:2px;min-width:0;border:0;background:transparent;color:#0f3b66;padding:4px 6px;text-align:left}.common-phrase-text{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700}.common-phrase-files-note{font-size:11px;color:#64748b}.common-phrase-file-chip{display:inline-flex;align-items:center;border:1px solid #cbd5e1;border-radius:6px;background:#f8fafc;color:#475569;padding:4px 6px;font-size:12px}.common-phrase-preview.broken{display:none}.phrase-pending{margin-top:8px}
.chat-keepalive-btn{border:1px solid #bfdbfe;border-radius:999px;background:#eff6ff;color:#0f3b66;padding:4px 10px;font-size:12px;font-weight:800;white-space:nowrap}.chat-keepalive-btn.ok{background:#dcfce7;color:#166534;border-color:#bbf7d0}.chat-keepalive-btn.warn{background:#fef3c7;color:#92400e;border-color:#fde68a}
</style>
"""


def layout(title: str, body: str) -> bytes:
    online = refresh_public_online_status()
    online_error = str(online.get("last_error") or "")
    online_last_ok = str(online.get("last_ok") or "")
    online_last_set = str(online.get("last_set") or "")
    online_last_heartbeat = str(online.get("last_heartbeat") or "")
    online_set_error = str(online.get("set_error") or "")
    online_heartbeat_error = str(online.get("heartbeat_error") or "")
    online_setting_error = str(online.get("setting_error") or "")
    online_verify_error = str(online.get("verify_error") or "")
    online_public_error = str(online.get("public_error") or "")
    online_public_checked_at = str(online.get("public_checked_at") or "")
    online_recovery_error = str(online.get("recovery_error") or "")
    if online.get("verified_online") or online.get("public_online"):
        online_label = "Online verified"
        online_class = "ok"
    elif online_error and online_error != "disabled":
        online_label = "Online error"
        online_class = "bad"
    elif online_last_set or online_last_heartbeat:
        online_label = "Online heartbeat"
        online_class = ""
    elif online_last_ok:
        online_label = "Online active"
        online_class = "ok"
    else:
        online_label = "Online checking"
        online_class = ""
    online_title = f"Last verified: {online_last_ok or '-'} | Last set: {online_last_set or '-'} | Last chat heartbeat: {online_last_heartbeat or '-'} | API status: {online.get('status') if online.get('status') is not None else '-'} | Public online: {'yes' if online.get('public_online') else 'no'} | Set error: {online_set_error or '-'} | Chat heartbeat error: {online_heartbeat_error or '-'} | Setting error: {online_setting_error or '-'} | Verify error: {online_verify_error or '-'} | Public checked: {online_public_checked_at or '-'} | Public verify error: {online_public_error or '-'} | Recovering: {online_recovery_error or '-'} | Error: {online_error or '-'}"
    chat_keepalive_url = get_chat_keepalive_url()
    chat_browser = CHAT_KEEPALIVE_BROWSER_STATUS.copy()
    chat_button_label = "Chat window active" if chat_browser.get("opened") else "Open chat window"
    chat_button_class = "ok" if chat_browser.get("opened") else "warn"
    nav = f"""
    <div class="top">
      <a href="/" aria-label="Home"><img class="brand-logo" src="/assets/shinchan-logo.png" alt="Crayon Shin-chan"></a>
      <div class="top-nav">
        <a href="/">Dashboard</a>
        <a href="/sales">Sales</a>
        <a href="/chats">Messages</a>
        <a href="/unread">Unread</a>
        <a href="/admin-messages">Admin</a>
        <a href="/phrases">&#24120;&#29992;&#35821;</a>
        <a href="/product">Product</a>
        <a href="/stock">Stock</a>
      </div>
      <form id="unique-code-form" class="unique-lookup" action="/unique-code" method="get">
        <input id="unique-code-input" name="code" maxlength="16" autocomplete="off" spellcheck="false" placeholder="Enter 16-digit verification code">
        <div id="unique-code-results" class="unique-results" hidden>
          <div class="unique-title">GUID</div>
          <div id="unique-code-result-body" class="unique-message">Enter a 16-character code</div>
        </div>
      </form>
      <span class="top-version">Digiseller Admin {APP_VERSION}</span>
      <span id="online-keepalive-pill" class="top-online {online_class}" title="{h(online_title)}">{h(online_label)}</span>
      <button id="chat-keepalive-button" class="chat-keepalive-btn {chat_button_class}" type="button" data-url="{h(chat_keepalive_url)}" title="The app opens the seller chat as a top-level browser window on startup; click only if it is closed.">{h(chat_button_label)}</button>
    </div>
    <script>
    (() => {{
      const form = document.getElementById('unique-code-form');
      const input = document.getElementById('unique-code-input');
      const results = document.getElementById('unique-code-results');
      const body = document.getElementById('unique-code-result-body');
      if (!form || !input || !results || !body) return;
      let timer = null;
      let controller = null;
      const codePattern = /^[A-Za-z0-9]{{16}}$/;

      function show(message, isError=false) {{
        body.className = isError ? 'unique-message unique-error' : 'unique-message';
        body.textContent = message;
        results.hidden = false;
      }}
      function hideSoon() {{
        setTimeout(() => {{ results.hidden = true; }}, 140);
      }}
      function openCode(code) {{
        if (!codePattern.test(code)) return;
        location.href = '/unique-code?code=' + encodeURIComponent(code);
      }}
      async function lookup(code) {{
        if (controller) controller.abort();
        controller = new AbortController();
        show('Checking...');
        try {{
          const res = await fetch('/api/unique-code?code=' + encodeURIComponent(code), {{cache: 'no-store', signal: controller.signal}});
          const data = await res.json();
          if (!res.ok || !data.ok) {{
            show(data.error || 'No matching GUID found', true);
            return;
          }}
          const item = data.item || {{}};
          body.className = '';
          body.innerHTML = `
            <button class="unique-result" type="button" data-code="${{code}}">
              <span class="unique-icon">ID</span>
              <span class="unique-main">
                <span class="unique-product">${{escapeHtml(item.product_name || 'Matched order')}}</span>
                <span class="unique-meta">Order ${{escapeHtml(String(item.invoice || ''))}} · ${{escapeHtml(item.state_label || '')}}</span>
              </span>
            </button>`;
          const button = body.querySelector('.unique-result');
          if (button) button.addEventListener('mousedown', (event) => {{ event.preventDefault(); openCode(code); }});
          results.hidden = false;
        }} catch (error) {{
          if (error.name !== 'AbortError') show('Lookup failed', true);
        }}
      }}
      function escapeHtml(value) {{
        return String(value).replace(/[&<>"']/g, (char) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
      }}
      input.addEventListener('input', () => {{
        const code = input.value.trim();
        clearTimeout(timer);
        if (!code) {{ results.hidden = true; return; }}
        if (code.length < 16) {{ show('Enter a 16-character code'); return; }}
        if (!codePattern.test(code)) {{ show('Code must be 16 letters or digits', true); return; }}
        timer = setTimeout(() => lookup(code), 200);
      }});
      input.addEventListener('focus', () => {{ if (input.value.trim()) results.hidden = false; }});
      input.addEventListener('blur', hideSoon);
      form.addEventListener('submit', (event) => {{
        const code = input.value.trim();
        if (!codePattern.test(code)) {{
          event.preventDefault();
          show('Code must be 16 letters or digits', true);
          return;
        }}
      }});
    }})();
    (() => {{
      const pill = document.getElementById('online-keepalive-pill');
      if (!pill) return;
      function applyStatus(status) {{
        const error = status.last_error || '';
        const lastOk = status.last_ok || '';
        const lastSet = status.last_set || '';
        const lastHeartbeat = status.last_heartbeat || '';
        const setError = status.set_error || '';
        const heartbeatError = status.heartbeat_error || '';
        const settingError = status.setting_error || '';
        const verifyError = status.verify_error || '';
        const publicError = status.public_error || '';
        const publicCheckedAt = status.public_checked_at || '';
        const recoveryError = status.recovery_error || '';
        let label = 'Online checking';
        let cls = '';
        if (status.verified_online || status.public_online) {{
          label = 'Online verified';
          cls = 'ok';
        }} else if (error && error !== 'disabled') {{
          const chatOpened = status.chat_browser && status.chat_browser.opened;
          label = chatOpened ? 'Online verifying' : 'Online error';
          cls = chatOpened ? '' : 'bad';
        }} else if (lastSet || lastHeartbeat) {{
          label = 'Online heartbeat';
        }} else if (lastOk) {{
          label = 'Online active';
          cls = 'ok';
        }}
        pill.textContent = label;
        pill.className = `top-online ${{cls}}`;
        pill.title = `Last verified: ${{lastOk || '-'}} | Last set: ${{lastSet || '-'}} | Last chat heartbeat: ${{lastHeartbeat || '-'}} | API status: ${{status.status ?? '-'}} | Public online: ${{status.public_online ? 'yes' : 'no'}} | Set error: ${{setError || '-'}} | Chat heartbeat error: ${{heartbeatError || '-'}} | Setting error: ${{settingError || '-'}} | Verify error: ${{verifyError || '-'}} | Public checked: ${{publicCheckedAt || '-'}} | Public verify error: ${{publicError || '-'}} | Recovering: ${{recoveryError || '-'}} | Error: ${{error || '-'}}`;
      }}
      async function refreshOnlineStatus() {{
        try {{
          const res = await fetch('/api/online-keepalive', {{cache: 'no-store'}});
          if (res.ok) applyStatus(await res.json());
        }} catch (e) {{}}
      }}
      refreshOnlineStatus();
      setInterval(refreshOnlineStatus, 15000);
    }})();
    (() => {{
      const btn = document.getElementById('chat-keepalive-button');
      if (!btn) return;
      const url = btn.dataset.url || 'https://chat.digiseller.com/asp/messenger.asp?mode=s';
      const windowName = 'digiseller-chat-keepalive';
      let chatWindow = null;
      function updateLabel() {{
        const popupActive = chatWindow && !chatWindow.closed;
        if (popupActive) {{
          btn.textContent = 'Chat window open';
          btn.classList.add('ok');
          btn.classList.remove('warn');
        }}
      }}
      function openChatKeepalive() {{
        chatWindow = window.open(url, windowName, 'width=740,height=520,scrollbars=no,resizable=yes');
        updateLabel();
      }}
      btn.addEventListener('click', openChatKeepalive);
      updateLabel();
      setInterval(updateLabel, 15000);
    }})();
    </script>
    """
    alert_ui = """
    <div class="alert-controls">
      <span id="unread-alert-pill" class="alert-pill"></span>
      <button id="enable-alerts" class="alert-button off" type="button">&#24320;&#21551;&#25552;&#37266;</button>
    </div>
    <script>
    (() => {
      const intervalMs = 60000;
      const btn = document.getElementById('enable-alerts');
      const pill = document.getElementById('unread-alert-pill');
      let enabled = localStorage.getItem('digisellerAlertsEnabled') === '1';
      let lastTotal = Number(localStorage.getItem('digisellerLastUnreadTotal') || '0');
      let audioCtx = null;
      let inFlight = false;
      let pollTimer = null;
      const baseTitle = document.title;

      function setButton() {
        btn.textContent = enabled ? '\u63d0\u9192\u5df2\u5f00\u542f' : '\u5f00\u542f\u63d0\u9192';
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
      let sweetVoice = null;
      function findSweetVoice() {
        const voices = window.speechSynthesis?.getVoices?.() || [];
        const chineseVoices = voices.filter((voice) => /zh|Chinese|Mandarin|\u666e\u901a\u8bdd|\u4e2d\u6587/i.test(`${voice.lang} ${voice.name}`));
        const sweetHints = ['Xiaoxiao', 'Xiaochen', 'Xiaoyi', 'Tingting', 'Meijia', 'Yaoyao', 'Hanhan', 'Huihui'];
        sweetVoice = sweetHints.map((hint) => chineseVoices.find((voice) => voice.name.includes(hint))).find(Boolean)
          || chineseVoices.find((voice) => /female|woman|girl|\u5973/i.test(voice.name))
          || chineseVoices[0]
          || null;
      }
      if ('speechSynthesis' in window) {
        findSweetVoice();
        window.speechSynthesis.onvoiceschanged = findSweetVoice;
      }
      function speak(text) {
        try {
          const u = new SpeechSynthesisUtterance(text);
          if (sweetVoice) u.voice = sweetVoice;
          u.lang = sweetVoice?.lang || 'zh-CN';
          u.pitch = 1.35;
          u.rate = 0.92;
          u.volume = 1;
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(u);
        } catch (e) {}
      }
      function browserNotify(title, body, url) {
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        const n = new Notification(title, {body});
        n.onclick = () => { window.focus(); if (url) location.href = url; };
      }
      function alertUnread(data, force=false) {
        const latest = data.latest || {};
        const who = latest.email || '';
        const order = latest.order_id || '';
        const title = '\u6709\u65b0\u7684\u4e70\u5bb6\u6d88\u606f\u4e86';
        const details = [];
        if (who) details.push(`\u6765\u81ea ${who}`);
        if (order) details.push(`\u8ba2\u5355 ${order}`);
        beep();
        speak(title);
        browserNotify(title, details.join('\uff0c') || title, latest.url);
      }
      function schedulePoll() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = enabled ? setInterval(() => poll(false), intervalMs) : null;
      }
      async function poll(force=false) {
        if ((!enabled && !force) || inFlight || (document.hidden && !force)) return;
        inFlight = true;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        try {
          const res = await fetch('/api/unread-count?force=1', {cache: 'no-store', signal: controller.signal});
          const data = await res.json();
          const total = Number(data.total || 0);
          pill.textContent = total > 0 ? `\u672a\u8bfb ${total}` : '';
          pill.classList.toggle('show', total > 0);
          document.title = total > 0 ? `(${total}) ${baseTitle}` : baseTitle;
          if (window.handleDigisellerUnreadData) window.handleDigisellerUnreadData(data);
          if (enabled && total > 0 && (force || total > lastTotal)) alertUnread(data, force);
          lastTotal = total;
          localStorage.setItem('digisellerLastUnreadTotal', String(lastTotal));
        } catch (e) {
        } finally {
          clearTimeout(timeout);
          inFlight = false;
        }
      }
      btn.addEventListener('click', async () => {
        enabled = !enabled;
        localStorage.setItem('digisellerAlertsEnabled', enabled ? '1' : '0');
        if (!enabled) {
          pill.classList.remove('show');
          document.title = baseTitle;
          setButton();
          schedulePoll();
          return;
        }
        lastTotal = 0;
        localStorage.setItem('digisellerLastUnreadTotal', '0');
        if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
        setButton();
        schedulePoll();
        beep();
        speak('\u63d0\u9192\u5df2\u7ecf\u5f00\u542f');
        poll(true);
      });
      document.addEventListener('visibilitychange', () => { if (!document.hidden && enabled) poll(false); });
      setButton();
      schedulePoll();
      window.refreshDigisellerUnread = poll;
      if (enabled) poll(false);
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
    document.addEventListener('click', async (event) => {
      const button = event.target.closest('.save-common-phrase');
      if (!button) return;
      const text = button.dataset.text || '';
      if (!text.trim()) return;
      const originalLabel = button.textContent;
      button.disabled = true;
      button.classList.remove('saved', 'failed');
      button.textContent = '\u4fdd\u5b58\u4e2d...';
      try {
        const res = await fetch('/api/common-phrases', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text})
        });
        if (!res.ok) throw new Error('save failed');
        const data = await res.json();
        button.textContent = data.created ? '\u5df2\u4fdd\u5b58' : '\u5df2\u5b58\u5728';
        button.classList.add('saved');
      } catch (e) {
        button.textContent = '\u4fdd\u5b58\u5931\u8d25';
        button.classList.add('failed');
        setTimeout(() => {
          button.disabled = false;
          button.classList.remove('failed');
          button.textContent = originalLabel || '\u4fdd\u5b58\u4e3a\u5e38\u7528\u8bed';
        }, 1800);
      }
    });
    window.loadDigisellerTranslations = async function(root=document) {
      const nodes = Array.from(root.querySelectorAll(".translated-message[data-pending='1']"));
      if (!nodes.length) return;
      const messages = nodes.map((node) => ({
        id: node.id.replace(/^msg-/, ''),
        text: (node.querySelector('.original-text') || {}).textContent || ''
      })).filter((item) => item.text);
      if (!messages.length) return;
      try {
        const res = await fetch('/api/translate-batch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({messages})
        });
        if (!res.ok) return;
        const data = await res.json();
        (data.results || []).forEach((item) => {
          const node = root.querySelector(`#msg-${CSS.escape(String(item.id))}`);
          if (!node) return;
          const translated = node.querySelector('.translated-text');
          const label = node.querySelector('.translation-label');
          if (translated) translated.textContent = item.translated || item.text || '';
          if (label) label.textContent = `${item.label || item.source_lang || 'auto'} → 中文`;
          node.dataset.pending = '0';
        });
      } catch (e) {}
    };
    window.loadDigisellerTranslations(document);
    </script>
    """
    html_doc = f"<!doctype html><html><head><meta charset='utf-8'><title>{h(title)}</title><link rel='icon' type='image/png' href='/favicon.ico'><link rel='apple-touch-icon' href='/assets/shinchan-logo.png'>{STYLE}</head><body>{nav}{alert_ui}{translation_ui}<div class='wrap'>{body}</div></body></html>"
    return html_doc.encode("utf-8")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{h(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


UNIQUE_CODE_RE = re.compile(r"^[A-Za-z0-9]{16}$")


def valid_unique_code(code: str) -> bool:
    return bool(UNIQUE_CODE_RE.fullmatch(code.strip()))


def unique_code_label(state: Any) -> str:
    labels = {
        1: "not verified",
        2: "delivered, waiting confirmation",
        3: "delivery confirmed",
        4: "delivery refuted",
        5: "verified, goods not delivered",
    }
    try:
        return labels.get(int(state), str(state or "unknown"))
    except (TypeError, ValueError):
        return str(state or "unknown")


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def delivery_mode(product: dict[str, Any]) -> tuple[str, str]:
    unique_code_verification = product.get("unique_code_verification")
    if isinstance(unique_code_verification, dict):
        if bool_value(unique_code_verification.get("automatic")):
            return "\u81ea\u52a8\u53d1\u8d27\uff08\u81ea\u52a8\u6838\u9a8c16\u4f4d\u7801\uff09", "ok"
        return "\u624b\u52a8\u53d1\u8d27\uff08\u9700\u4e70\u5bb616\u4f4d\u7801\uff09", "bad"
    values = [
        str(product.get("content_type") or ""),
        str(product.get("type") or ""),
        str(product.get("good_type") or ""),
    ]
    if any("digiseller" in value.lower() and "code" in value.lower() for value in values):
        verify_code = product.get("verify_code")
        auto_verify = verify_code.get("auto_verify") if isinstance(verify_code, dict) else None
        if bool_value(auto_verify):
            return "\u81ea\u52a8\u53d1\u8d27\uff08\u81ea\u52a8\u6838\u9a8c16\u4f4d\u7801\uff09", "ok"
        return "\u624b\u52a8\u53d1\u8d27\uff08\u9700\u4e70\u5bb616\u4f4d\u7801\uff09", "bad"
    return "\u81ea\u52a8\u53d1\u8d27", "ok"


def delivery_mode_html(product: dict[str, Any]) -> str:
    label, css_class = delivery_mode(product)
    return f"<span class='{css_class}'>{h(label)}</span>"


def unique_code_lookup(code: str) -> dict[str, Any]:
    if not valid_unique_code(code):
        raise ValueError("Unique code must be exactly 16 letters or digits")
    data = client.unique_code(code)
    retval = data.get("retval")
    if retval not in (None, 0, "0"):
        raise RuntimeError(data.get("retdesc") or f"Lookup failed: {retval}")
    product_id = data.get("id_goods") or data.get("product_id")
    product_name = str(
        data.get("product")
        or data.get("product_name")
        or data.get("name")
        or data.get("goods_name")
        or ""
    ).strip()
    if not product_name and product_id:
        try:
            product_data = client.product(int(product_id))
            product = product_data.get("product", product_data)
            product_name = str(product.get("name") or "").strip()
        except Exception:
            product_name = ""
    state = data.get("unique_code_state") if isinstance(data.get("unique_code_state"), dict) else {}
    return {
        "code": code,
        "invoice": data.get("inv") or data.get("invoice_id"),
        "product_id": product_id,
        "product_name": product_name or (f"Product {product_id}" if product_id else "Unknown product"),
        "amount": data.get("amount"),
        "currency": data.get("type_curr") or data.get("currency_type"),
        "date_pay": data.get("date_pay"),
        "email": data.get("email"),
        "state": state.get("state") if isinstance(state, dict) else None,
        "state_label": unique_code_label(state.get("state")) if isinstance(state, dict) else "unknown",
        "raw": data,
    }


def parse_stock_lines(raw: str, variant_id: int = 0) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in raw.splitlines():
        value = line.strip()
        if not value:
            continue
        serial = ""
        if "\t" in value:
            serial, value = [part.strip() for part in value.split("\t", 1)]
        elif " | " in value:
            serial, value = [part.strip() for part in value.split(" | ", 1)]
        if not value:
            continue
        item: dict[str, Any] = {"serial": serial, "value": value}
        if variant_id:
            item["id_v"] = variant_id
        items.append(item)
    return items


def unread_summary() -> dict[str, Any]:
    buyer = [c for c in client.chats(only_unread=True) if int(c.get("cnt_new") or 0) > 0]
    try:
        guest = [
            c
            for c in client.guest_chats(limit=10)
            if not int(c.get("IsAuthor") or 0) and not int(c.get("IsViewed") or 0)
        ]
    except Exception:
        guest = []
    admin: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    buyer_unread: list[dict[str, Any]] = []
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
        buyer_unread.append(rec)
        if latest is None or str(rec.get("last_date") or "") > str(latest.get("last_date") or ""):
            latest = rec
    for chat in guest:
        corr_id = int(chat.get("CorrID") or 0)
        corr_type = str(chat.get("CorrType") or chat.get("Type") or "visitor")
        rec = {
            "type": "guest",
            "order_id": "",
            "email": chat.get("Name") or f"GUEST-{corr_id}",
            "product": clean_text(chat.get("PurchaseName") or chat.get("Text")),
            "last_date": chat.get("DateWriteUtc") or chat.get("DateWrite"),
            "cnt_new": 1,
            "url": f"/chats?kind=guest&corr_type={urllib.parse.quote(corr_type)}&corr_id={corr_id}",
        }
        if latest is None or sort_time(rec.get("last_date")) > sort_time(latest.get("last_date")):
            latest = rec
    total = sum(int(c.get("cnt_new") or 0) for c in buyer) + len(guest) + len(admin)
    return {
        "ok": True,
        "buyer_unread_chats": len(buyer),
        "buyer_unread_messages": sum(int(c.get("cnt_new") or 0) for c in buyer),
        "buyer_unread": buyer_unread,
        "guest_unread_chats": len(guest),
        "admin_unread": len(admin),
        "total": total,
        "latest": latest,
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def clear_unread_cache() -> None:
    UNREAD_CACHE["time"] = 0.0
    UNREAD_CACHE["data"] = None


def phrase_user_key() -> str:
    return str(client.seller_id)


def load_common_phrases() -> list[dict[str, Any]]:
    if not COMMON_PHRASES_FILE.exists():
        return []
    data = json.loads(COMMON_PHRASES_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    values = data.get(phrase_user_key(), [])
    if not isinstance(values, list):
        return []
    phrases: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        phrase_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        raw_files = item.get("files") or []
        files = [file for file in raw_files if isinstance(file, dict) and file.get("stored")]
        if phrase_id and (text or files):
            phrases.append({"id": phrase_id, "text": text, "files": files})
    return phrases


def save_common_phrases(phrases: list[dict[str, Any]]) -> None:
    data: dict[str, list[dict[str, Any]]] = {}
    if COMMON_PHRASES_FILE.exists():
        loaded = json.loads(COMMON_PHRASES_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key, values in loaded.items():
                if isinstance(key, str) and isinstance(values, list):
                    data[key] = values
    data[phrase_user_key()] = phrases
    tmp = COMMON_PHRASES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(COMMON_PHRASES_FILE)


def save_text_common_phrase(text: str) -> tuple[bool, str]:
    value = clean_text(text)
    if not value:
        raise RuntimeError("Text is empty")
    phrases = load_common_phrases()
    for phrase in phrases:
        phrase_text = clean_text(phrase.get("text"))
        if phrase_text == value and not phrase.get("files"):
            return False, str(phrase["id"])
    phrase_id = new_phrase_id(value)
    phrases.append({"id": phrase_id, "text": value, "files": []})
    save_common_phrases(phrases)
    return True, phrase_id


def localize_option_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict)]
        for locale in ("zh-CN", "zh", "ru-RU", "ru", "en-US", "en"):
            for item in candidates:
                if str(item.get("locale") or item.get("lang") or "").lower() == locale.lower():
                    text = first_option_text(item, ("value", "user_data", "text", "name", "title", "label"))
                    if text:
                        return text
        values = [localize_option_value(item) for item in value]
        return " / ".join(value for value in values if value)
    if isinstance(value, dict):
        text = first_option_text(value, ("value", "user_data", "text", "name", "title", "caption", "label"))
        if text:
            return text
    return clean_text(value)


def first_option_text(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in raw:
            continue
        text = localize_option_value(raw.get(key))
        if text:
            return text
    return ""


def normalize_option_items(raw_options: Any) -> list[Any]:
    if raw_options is None:
        return []
    if isinstance(raw_options, dict):
        for key in ("option", "options", "items", "values", "parameters", "params"):
            if key in raw_options:
                nested = normalize_option_items(raw_options.get(key))
                if nested:
                    return nested
        return [raw_options]
    if isinstance(raw_options, list):
        return raw_options
    return []


def option_sources_from_info(info: dict[str, Any]) -> list[Any]:
    sources: list[Any] = []
    for key in ("options", "option", "parameters", "params"):
        if key in info:
            sources.append(info.get(key))
    content = info.get("content")
    if isinstance(content, dict):
        for key in ("options", "option", "parameters", "params"):
            if key in content:
                sources.append(content.get(key))
    return sources


def purchase_options_from_info(info: dict[str, Any]) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in option_sources_from_info(info):
        for raw in normalize_option_items(source):
            if isinstance(raw, dict):
                name = first_option_text(raw, ("name", "parameter", "title", "label", "caption"))
                value = first_option_text(
                    raw,
                    (
                        "user_data",
                        "user_data_text",
                        "value",
                        "variant",
                        "selected",
                        "text",
                        "option_value",
                        "value_text",
                    ),
                )
                variant_id = first_option_text(raw, ("variant_id", "user_data_id", "value_id", "selected_id"))
                if value and variant_id and variant_id != value:
                    value = f"{value} ({variant_id})"
                elif not value and variant_id:
                    value = variant_id
            else:
                name = ""
                value = localize_option_value(raw)
            if not value:
                continue
            if not name:
                name = "选项"
            item = (name, value)
            if item in seen:
                continue
            seen.add(item)
            options.append(item)
    return options


def cached_purchase_info(order_id: int) -> dict[str, Any]:
    if not order_id:
        return {}
    now = time.time()
    cached = PURCHASE_INFO_CACHE.get(order_id)
    if cached and now - cached[0] < 300:
        return cached[1]
    try:
        info = client.purchase_info(order_id)
    except Exception:
        info = {}
    PURCHASE_INFO_CACHE[order_id] = (now, info)
    return info


def order_options_html(order_id: int) -> str:
    options = purchase_options_from_info(cached_purchase_info(order_id))
    if not options:
        return ""
    rows = "".join(
        f"<div class='order-option'><span class='order-option-name'>{h(name)}:</span> <span class='order-option-value'>{h(value)}</span></div>"
        for name, value in options
    )
    return f"<div class='order-options'><div class='order-options-title'>&#39069;&#22806;&#36873;&#39033;</div>{rows}</div>"


def new_phrase_id(text: str) -> str:
    seed = f"{time.time_ns()}:{text}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def phrase_file_url(stored: str) -> str:
    return "/phrase-files/" + urllib.parse.quote(stored, safe="")


def phrase_file_reference(file: dict[str, Any]) -> tuple[str, str, str]:
    filename = str(file.get("filename") or file.get("name") or "")
    stored = str(file.get("stored") or file.get("file") or "")
    if not stored and filename:
        candidate = (COMMON_PHRASES_DIR / filename).resolve()
        if str(candidate).startswith(str(COMMON_PHRASES_DIR.resolve())) and candidate.exists():
            stored = filename
    file_url = phrase_file_url(stored) if stored else str(file.get("preview") or file.get("url") or "")
    return filename or stored or "file", stored, file_url


def phrase_file_is_image(file: dict[str, Any], filename: str, file_url: str) -> bool:
    content_type = str(file.get("content_type") or file.get("type") or "")
    if content_type.startswith("image/"):
        return True
    return looks_like_image_name(filename) or looks_like_image_name(file_url)


def save_phrase_uploads(phrase_id: str, uploads: list[UploadItem], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files = list(existing)
    for upload in uploads:
        suffix = Path(upload.filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ""
        digest = hashlib.sha1(upload.data).hexdigest()[:16]
        stored = f"{phrase_id}-{time.time_ns()}-{digest}{suffix}"
        (COMMON_PHRASES_DIR / stored).write_bytes(upload.data)
        files.append({"filename": upload.filename, "content_type": upload.content_type, "stored": stored})
    return files


def phrase_upload_items(phrase: dict[str, Any]) -> list[UploadItem]:
    uploads: list[UploadItem] = []
    for file in phrase.get("files") or []:
        if not isinstance(file, dict):
            continue
        _, stored, _ = phrase_file_reference(file)
        if not stored:
            continue
        file_path = (COMMON_PHRASES_DIR / stored).resolve()
        if not str(file_path).startswith(str(COMMON_PHRASES_DIR.resolve())) or not file_path.exists():
            continue
        uploads.append(UploadItem(str(file.get("filename") or file_path.name), str(file.get("content_type") or "application/octet-stream"), file_path.read_bytes()))
    return uploads


def remove_phrase_files(phrase: dict[str, Any]) -> None:
    for file in phrase.get("files") or []:
        if not isinstance(file, dict):
            continue
        _, stored, _ = phrase_file_reference(file)
        if not stored:
            continue
        file_path = (COMMON_PHRASES_DIR / stored).resolve()
        if str(file_path).startswith(str(COMMON_PHRASES_DIR.resolve())):
            file_path.unlink(missing_ok=True)


def update_online_keepalive_status(**values: Any) -> None:
    values.setdefault("last_checked", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    ONLINE_KEEPALIVE_STATUS.update(values)


def refresh_public_online_status(force: bool = False) -> dict[str, Any]:
    now = time.time()
    last_checked_ts = float(ONLINE_KEEPALIVE_STATUS.get("public_checked_ts") or 0)
    if not force and now - last_checked_ts < 10:
        data = ONLINE_KEEPALIVE_STATUS.copy()
        data["chat_browser"] = CHAT_KEEPALIVE_BROWSER_STATUS.copy()
        return data

    checked_at = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        public_data = client.public_seller_online_status()
    except Exception as exc:
        update_online_keepalive_status(
            public_error=str(exc),
            public_checked_at=checked_at,
            public_checked_ts=now,
        )
    else:
        public_online = bool(public_data.get("online"))
        updates: dict[str, Any] = {
            "public_online": public_online,
            "public_url": public_data.get("url") or ONLINE_KEEPALIVE_STATUS.get("public_url", ""),
            "public_error": "",
            "public_checked_at": checked_at,
            "public_checked_ts": now,
            "public_response": public_data,
        }
        if public_online:
            updates.update(
                {
                    "verified_online": True,
                    "last_ok": checked_at,
                    "last_error": "",
                    "recovery_error": "",
                    "failure_count": 0,
                }
            )
        update_online_keepalive_status(**updates)

    data = ONLINE_KEEPALIVE_STATUS.copy()
    data["chat_browser"] = CHAT_KEEPALIVE_BROWSER_STATUS.copy()
    return data


def run_online_keepalive(interval: int) -> None:
    while True:
        if not client.configured():
            update_online_keepalive_status(enabled=False, last_error="missing .env")
            time.sleep(interval)
            continue
        checked_at = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        set_data: dict[str, Any] = {}
        heartbeat_data: dict[str, Any] = {}
        setting_data: dict[str, Any] = {}
        status_data: dict[str, Any] = {}
        public_data: dict[str, Any] = {}
        set_error = ""
        heartbeat_error = ""
        setting_error = ""
        verify_error = ""
        public_error = ""
        try:
            set_data = client.set_online()
        except Exception as exc:
            set_error = str(exc)
        try:
            heartbeat_data = client.messenger_heartbeat()
        except Exception as exc:
            heartbeat_error = str(exc)
        try:
            setting_data = client.online_setting()
        except Exception as exc:
            setting_error = str(exc)
        try:
            status_data = client.seller_online_status()
        except Exception as exc:
            verify_error = str(exc)
        try:
            public_data = client.public_seller_online_status()
        except Exception as exc:
            public_error = str(exc)
        status = status_data.get("status")
        try:
            status_value = int(status or 0)
        except (TypeError, ValueError):
            status_value = 0
        public_online = bool(public_data.get("online"))
        verified_online = status_value > 0 or public_online
        keepalive_ok = not set_error or not heartbeat_error
        cycle_error = "" if (verified_online or keepalive_ok) else " | ".join(item for item in (set_error, heartbeat_error) if item)
        failure_count = int(ONLINE_KEEPALIVE_STATUS.get("failure_count") or 0)
        failure_count = failure_count + 1 if cycle_error else 0
        visible_error = cycle_error if failure_count >= 3 else ""
        update_online_keepalive_status(
            enabled=True,
            last_checked=checked_at,
            last_ok=checked_at if verified_online else "",
            last_set=checked_at if not set_error else ONLINE_KEEPALIVE_STATUS.get("last_set", ""),
            last_heartbeat=checked_at if not heartbeat_error else ONLINE_KEEPALIVE_STATUS.get("last_heartbeat", ""),
            last_error=visible_error,
            setting=setting_data.get("setting", ONLINE_KEEPALIVE_STATUS.get("setting")),
            period=setting_data.get("period", ONLINE_KEEPALIVE_STATUS.get("period")),
            status=status,
            verified_online=verified_online,
            public_online=public_online,
            public_url=public_data.get("url") or ONLINE_KEEPALIVE_STATUS.get("public_url", ""),
            set_error=set_error,
            heartbeat_error=heartbeat_error,
            setting_error=setting_error,
            verify_error=verify_error,
            public_error=public_error,
            recovery_error=cycle_error if cycle_error and not visible_error else "",
            failure_count=failure_count,
            set_response=set_data,
            heartbeat_response=heartbeat_data,
            public_response=public_data,
        )
        time.sleep(interval)


def get_chat_keepalive_url() -> str:
    return os.getenv(
        "DIGISELLER_CHAT_KEEPALIVE_URL",
        "https://chat.digiseller.com/asp/messenger.asp?mode=s",
    ).strip()


def chat_keepalive_window_exists(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not host:
        return False
    if sys.platform == "darwin":
        script = f"""
        if application "Google Chrome" is not running then return "0"
        tell application "Google Chrome"
            repeat with chromeWindow in windows
                repeat with chromeTab in tabs of chromeWindow
                    set tabUrl to URL of chromeTab
                    if tabUrl contains "{host}" and tabUrl contains "{path}" then return "1"
                end repeat
            end repeat
        end tell
        return "0"
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.stdout.strip() == "1"
    if sys.platform.startswith("linux"):
        try:
            result = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        windows = result.stdout.lower()
        return "digiseller" in windows and ("chat" in windows or "messenger" in windows)
    return False


def start_chat_keepalive_browser() -> None:
    enabled = os.getenv("DIGISELLER_CHAT_OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no", "off"}
    CHAT_KEEPALIVE_BROWSER_STATUS.update({"enabled": enabled, "opened": False, "reused": False, "error": ""})
    if not enabled:
        return

    def open_chat() -> None:
        opened_at = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        chat_url = get_chat_keepalive_url()
        if chat_keepalive_window_exists(chat_url):
            CHAT_KEEPALIVE_BROWSER_STATUS.update({"opened": True, "reused": True, "last_open": opened_at, "error": ""})
            print("Chat keepalive browser window already exists; skipping auto-open.", flush=True)
            return
        try:
            opened = webbrowser.open_new(chat_url)
        except Exception as exc:
            CHAT_KEEPALIVE_BROWSER_STATUS.update({"opened": False, "reused": False, "last_open": opened_at, "error": str(exc)})
            print(f"Chat keepalive browser open failed: {exc}", flush=True)
            return
        CHAT_KEEPALIVE_BROWSER_STATUS.update(
            {
                "opened": bool(opened),
                "reused": False,
                "last_open": opened_at,
                "error": "" if opened else "browser open returned false",
            }
        )
        if opened:
            print("Chat keepalive browser window opened.", flush=True)
        else:
            print("Chat keepalive browser window was not opened; use the top-bar fallback button.", flush=True)

    threading.Timer(2.0, open_chat).start()


def start_online_keepalive() -> None:
    enabled = os.getenv("DIGISELLER_KEEP_ONLINE", "1").strip().lower() not in {"0", "false", "no", "off"}
    if not enabled:
        update_online_keepalive_status(enabled=False, last_error="disabled")
        return
    try:
        interval = max(5, int(os.getenv("DIGISELLER_KEEP_ONLINE_INTERVAL", "15") or "15"))
    except ValueError:
        interval = 15
    threading.Thread(target=run_online_keepalive, args=(interval,), daemon=True).start()


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
                who = latest.get("email") or "\u4e70\u5bb6"
                order_id = latest.get("order_id") or ""
                notify_desktop(f"\u6709\u65b0\u7684\u4e70\u5bb6\u6d88\u606f\u4e86\uff0c\u6765\u81ea {who}\uff0c\u8ba2\u5355 {order_id}")
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
        phrase_forms = []
        for phrase in load_common_phrases():
            text = str(phrase.get("text") or "")
            files = phrase.get("files") or []
            file_names: list[str] = []
            preview_items = []
            for file in files:
                if not isinstance(file, dict):
                    continue
                filename, _, file_url = phrase_file_reference(file)
                if filename:
                    file_names.append(filename)
                if phrase_file_is_image(file, filename, file_url) and file_url:
                    preview_items.append(
                        f"<button class='common-phrase-preview' type='button' data-preview-src='{h(file_url)}' data-preview-name='{h(filename)}'>"
                        f"<img src='{h(file_url)}' alt='{h(filename)}' loading='lazy'></button>"
                    )
                elif filename:
                    preview_items.append(f"<span class='common-phrase-file-chip'>{h(filename)}</span>")
            fallback_label = file_names[0] if file_names else "Attachment phrase"
            label = short(text or fallback_label, 48)
            file_note = f"<span class='common-phrase-files-note'>+{len(files)} file</span>" if files else ""
            previews_html = f"<div class='common-phrase-previews'>{''.join(preview_items)}</div>" if preview_items else ""
            phrase_forms.append(
                f"<form class='common-phrase-card' method='post' action='/chats/send'>"
                f"<input type='hidden' name='order_id' value='{order_id}'>"
                f"<input type='hidden' name='target_lang' value='{h(target_lang)}'>"
                f"<input type='hidden' name='phrase_id' value='{h(phrase['id'])}'>"
                f"{previews_html}"
                f"<button class='common-phrase-send' type='submit' title='{h(text or fallback_label)}'>"
                f"<span class='common-phrase-text'>{h(label)}</span>{file_note}</button></form>"
            )
        if phrase_forms:
            phrases_html = (
                "<div class='common-phrases'><div class='common-phrase-title'>&#24120;&#29992;&#35821;&#65288;&#28857;&#20987;&#31435;&#21363;&#21457;&#36865;&#65289;</div>"
                f"<div class='common-phrase-buttons'>{''.join(phrase_forms)}</div></div>"
            )
        else:
            phrases_html = "<div class='common-phrases'><div class='phrase-empty'>&#36824;&#27809;&#26377;&#24120;&#29992;&#35821;&#65292;<a href='/phrases'>&#21435;&#28155;&#21152;</a></div></div>"
        return f"""
        <form id="{editor_id}" class="reply-editor" method="post" action="/chats/send" enctype="multipart/form-data">
          <input type="hidden" name="order_id" value="{order_id}">
          <input type="hidden" name="target_lang" value="{h(target_lang)}">
          <textarea id="{editor_id}-message" name="message" placeholder="&#22312;&#36825;&#37324;&#22238;&#22797;&#20250;&#21592;&#20449;&#24687;&#65292;&#21487;&#22635;&#20889;&#36134;&#21495;&#12289;&#23494;&#30721;&#12289;&#38142;&#25509;&#12289;&#20351;&#29992;&#35828;&#26126;&#31561;&#12290;"></textarea>
          <div class="reply-actions">
            <div id="{editor_id}-dropzone" class="reply-dropzone">
              <input id="{editor_id}-files" name="files" type="file" multiple accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.rtf,.zip,.rar,.7z">
              <span class="reply-dropzone-text">&#25302;&#25341;&#22270;&#29255;/&#38468;&#20214;&#21040;&#36825;&#37324;&#65292;&#25110;&#28857;&#20987;&#36873;&#25321;&#25991;&#20214;</span>
            </div>
            <button type="submit">&#21457;&#36865;&#22238;&#22797;</button>
            <span class="reply-hint">&#20013;&#25991;&#20250;&#33258;&#21160;&#32763;&#35793;&#20026; {h(lang_label(target_lang))} &#20877;&#21457;&#36865;&#12290;&#25903;&#25345;&#22270;&#29255;&#12289;&#38468;&#20214;&#12289;&#25991;&#26723;/&#25991;&#29486;&#65292;&#20063;&#21487;&#30452;&#25509; Ctrl+V &#31896;&#36148;&#21098;&#36148;&#26495;&#22270;&#29255;&#12290;</span>
          </div>
          <div id="{editor_id}-selected" class="selected-files"></div>
        </form>
        {phrases_html}
        <script>
        (() => {{
          const root = document.getElementById('{editor_id}');
          const textarea = document.getElementById('{editor_id}-message');
          const input = document.getElementById('{editor_id}-files');
          const dropzone = document.getElementById('{editor_id}-dropzone');
          const selected = document.getElementById('{editor_id}-selected');
          let previewUrls = [];
          let selectedFiles = Array.from(input.files || []);
          const previewModal = document.createElement('div');
          previewModal.className = 'preview-modal';
          previewModal.hidden = true;
          previewModal.innerHTML = '<button class="preview-modal-close" type="button" aria-label="Close">×</button><img alt="">';
          document.body.appendChild(previewModal);
          const modalImage = previewModal.querySelector('img');
          function openImagePreview(url, name) {{
            modalImage.src = url;
            modalImage.alt = name;
            previewModal.hidden = false;
          }}
          function closeImagePreview() {{
            previewModal.hidden = true;
            modalImage.removeAttribute('src');
          }}
          previewModal.addEventListener('click', (event) => {{
            if (event.target === previewModal || event.target.closest('.preview-modal-close')) closeImagePreview();
          }});
          document.addEventListener('keydown', (event) => {{
            if (event.key === 'Escape' && !previewModal.hidden) closeImagePreview();
          }});
          document.querySelectorAll('.common-phrase-preview').forEach((button) => {{
            if (button.dataset.previewReady) return;
            button.dataset.previewReady = '1';
            const img = button.querySelector('img');
            if (img) {{
              img.addEventListener('error', () => {{
                const fallback = document.createElement('span');
                fallback.className = 'common-phrase-file-chip';
                fallback.textContent = button.dataset.previewName || 'IMAGE';
                button.replaceWith(fallback);
              }});
            }}
            button.addEventListener('click', () => {{
              openImagePreview(button.dataset.previewSrc || '', button.dataset.previewName || '');
            }});
          }});
          function syncInputFiles() {{
            const dataTransfer = new DataTransfer();
            selectedFiles.forEach((file) => dataTransfer.items.add(file));
            input.files = dataTransfer.files;
          }}
          function clearPreviews() {{
            previewUrls.forEach((url) => URL.revokeObjectURL(url));
            previewUrls = [];
            selected.replaceChildren();
          }}
          function renderSelectedFiles() {{
            clearPreviews();
            const files = selectedFiles;
            if (!files.length) return;
            const summary = document.createElement('div');
            summary.className = 'selected-summary';
            summary.textContent = `\u5df2\u9009\u62e9\uff1a${{files.map((file) => file.name).join('\u3001')}}`;
            selected.appendChild(summary);
            const grid = document.createElement('div');
            grid.className = 'file-preview-grid';
            files.forEach((file) => {{
              const chip = document.createElement('div');
              chip.className = 'file-chip';
              if (file.type.startsWith('image/')) {{
                const img = document.createElement('img');
                const url = URL.createObjectURL(file);
                previewUrls.push(url);
                img.src = url;
                img.alt = file.name;
                img.title = '\u70b9\u51fb\u67e5\u770b\u5927\u56fe';
                img.addEventListener('click', () => openImagePreview(url, file.name));
                chip.appendChild(img);
              }} else {{
                const icon = document.createElement('span');
                icon.className = 'file-chip-icon';
                icon.textContent = 'FILE';
                chip.appendChild(icon);
              }}
              const name = document.createElement('span');
              name.className = 'file-chip-name';
              name.title = file.name;
              name.textContent = file.name;
              chip.appendChild(name);
              grid.appendChild(chip);
            }});
            selected.appendChild(grid);
          }}
          function addFiles(files) {{
            selectedFiles = [...selectedFiles, ...Array.from(files || [])];
            syncInputFiles();
            renderSelectedFiles();
          }}
          function clipboardImageFiles(event) {{
            const clipboard = event.clipboardData;
            if (!clipboard) return [];
            const files = [];
            const items = Array.from(clipboard.items || []);
            items.forEach((item, index) => {{
              if (item.kind !== 'file' || !item.type.startsWith('image/')) return;
              const file = item.getAsFile();
              if (!file) return;
              const ext = (file.type.split('/')[1] || 'png').replace(/[^a-z0-9]/gi, '').toLowerCase() || 'png';
              const name = file.name && file.name !== 'image.png' ? file.name : `clipboard-${{Date.now()}}-${{index + 1}}.${{ext}}`;
              files.push(new File([file], name, {{type: file.type || 'image/png'}}));
            }});
            if (!files.length) {{
              Array.from(clipboard.files || []).forEach((file, index) => {{
                if (!file.type.startsWith('image/')) return;
                const ext = (file.type.split('/')[1] || 'png').replace(/[^a-z0-9]/gi, '').toLowerCase() || 'png';
                const name = file.name || `clipboard-${{Date.now()}}-${{index + 1}}.${{ext}}`;
                files.push(new File([file], name, {{type: file.type || 'image/png'}}));
              }});
            }}
            return files;
          }}
          root.querySelectorAll('[data-insert]').forEach((button) => {{
            button.addEventListener('click', () => {{
              const text = button.dataset.insert || '';
              const prefix = textarea.value && !textarea.value.endsWith('\\n') ? '\\n' : '';
              textarea.value += prefix + text;
              textarea.focus();
            }});
          }});
          input.addEventListener('change', () => {{
            addFiles(input.files);
          }});
          textarea.addEventListener('paste', (event) => {{
            const files = clipboardImageFiles(event);
            if (!files.length) return;
            event.preventDefault();
            addFiles(files);
          }});
          function showDragTarget() {{
            root.classList.add('dragover');
            dropzone.classList.add('dragover');
          }}
          function hideDragTarget() {{
            root.classList.remove('dragover');
            dropzone.classList.remove('dragover');
          }}
          [root, textarea, dropzone].forEach((target) => {{
            target.addEventListener('dragenter', (event) => {{
              event.preventDefault();
              event.stopPropagation();
              showDragTarget();
            }});
            target.addEventListener('dragover', (event) => {{
              event.preventDefault();
              event.stopPropagation();
              showDragTarget();
            }});
            target.addEventListener('dragleave', (event) => {{
              event.preventDefault();
              event.stopPropagation();
              if (!root.contains(event.relatedTarget)) hideDragTarget();
            }});
            target.addEventListener('drop', (event) => {{
              event.preventDefault();
              event.stopPropagation();
              hideDragTarget();
              addFiles(event.dataTransfer.files);
            }});
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
            if path in {"/favicon.ico", "/assets/shinchan-logo.png"}:
                return self.serve_logo()
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
            if path == "/phrases":
                return self.phrases_page()
            if path == "/product":
                return self.product()
            if path == "/stock":
                return self.stock()
            if path == "/unique-code":
                return self.unique_code_page()
            if path == "/download-images":
                return self.download_images()
            if path == "/api/unique-code":
                return self.api_unique_code()
            if path == "/api/unread-count":
                return self.api_unread_count()
            if path == "/api/online-keepalive":
                return self.api_online_keepalive()
            if path == "/api/version":
                return self.send_json({"version": APP_VERSION, "file": str(Path(__file__).resolve())})
            if path == "/api/chat-panel":
                return self.api_chat_panel()
            if path == "/api/chat-debug":
                return self.api_chat_debug()
            if path.startswith("/downloads/"):
                return self.serve_download(path)
            if path.startswith("/phrase-files/"):
                return self.serve_phrase_file(path)
            return self.send_html("Not found", "<div class='card bad'>Not found</div>", 404)
        except Exception as exc:
            self.send_html("Error", f"<div class='card bad'>Error</div><pre class='card code'>{h(exc)}</pre>", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/chats/send":
                return self.send_chat_reply()
            if path == "/phrases/save":
                return self.save_phrase()
            if path == "/phrases/delete":
                return self.delete_phrase()
            if path == "/phrases/file-delete":
                return self.delete_phrase_file()
            if path == "/stock/upload":
                return self.upload_stock()
            if path == "/api/translate-batch":
                return self.api_translate_batch()
            if path == "/api/common-phrases":
                return self.api_save_common_phrase()
            return self.send_html("Not found", "<div class='card bad'>Not found</div>", 404)
        except Exception as exc:
            self.send_html("Error", f"<div class='card bad'>Error</div><pre class='card code'>{h(exc)}</pre>", 500)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1024 * 1024:
            raise RuntimeError("JSON body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def send_chat_reply(self) -> None:
        fields, uploads = self.read_form()
        order_id = int(fields.get("order_id", "0") or 0)
        message = fields.get("message", "").strip()
        phrase_id = fields.get("phrase_id", "").strip()
        target_lang = fields.get("target_lang", "").strip() or "en"
        if not order_id:
            raise RuntimeError("Order ID is missing")
        if phrase_id:
            phrase = next((item for item in load_common_phrases() if item["id"] == phrase_id), None)
            if phrase:
                if not message:
                    message = str(phrase.get("text") or "").strip()
                uploads.extend(phrase_upload_items(phrase))
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
        online = ONLINE_KEEPALIVE_STATUS.copy()
        if online.get("last_error"):
            online_state = "<span class='bad'>error</span>"
        elif online.get("verified_online") or online.get("public_online"):
            online_state = "<span class='ok'>verified online</span>"
        elif online.get("last_set") or online.get("last_heartbeat"):
            online_state = "<span class='muted'>heartbeat sent, not verified</span>"
        else:
            online_state = "<span class='bad'>not active</span>"
        online_info = (
            f"<div class='stat'><b>Online keepalive</b><br>Status: {online_state}"
            f"<br>Last verified: {h(online.get('last_ok') or '-')}"
            f"<br>Last set: {h(online.get('last_set') or '-')}"
            f"<br>Last chat heartbeat: {h(online.get('last_heartbeat') or '-')}"
            f"<br>Buyer-visible status: {h(online.get('status') if online.get('status') is not None else '-')}"
            f"<br>Public seller page: {h('online' if online.get('public_online') else 'not online')}"
            f"<br>Public URL: {h(online.get('public_url') or '-')}"
            f"<br>Setting: {h(online.get('setting') if online.get('setting') is not None else '-')}"
            f" · period: {h(online.get('period') if online.get('period') is not None else '-')}"
            f"<br>Set error: {h(online.get('set_error') or '-')}"
            f"<br>Chat heartbeat error: {h(online.get('heartbeat_error') or '-')}"
            f"<br>Setting error: {h(online.get('setting_error') or '-')}"
            f"<br>Verify error: {h(online.get('verify_error') or '-')}"
            f"<br>Public verify error: {h(online.get('public_error') or '-')}"
            f"<br>Recovering: {h(online.get('recovery_error') or '-')}"
            f"<br>Error: {h(online.get('last_error') or '-')}</div>"
        )
        body = f"""
        <div class='card'><h2>Digiseller Local Admin</h2><p>Config: {status}</p><p class='muted'>API Key is read from <code>.env</code>; never paste it into code or chat.</p></div>
        <div class='grid'>{login_info}{online_info}<div class='stat'><b>Quick links</b><br><a href='/sales'>Recent sales</a><br><a href='/unread'>Unread messages</a><br><a href='/chats'>Buyer chats</a></div></div>
        """
        self.send_html("Dashboard", body)

    def phrases_page(self) -> None:
        phrases = load_common_phrases()
        rows = []
        for phrase in phrases:
            phrase_id = phrase["id"]
            text = str(phrase.get("text") or "")
            file_rows = []
            file_delete_forms = []
            for file in phrase.get("files") or []:
                filename, stored, file_url = phrase_file_reference(file)
                delete_form_id = "delete-file-" + re.sub(r"[^a-zA-Z0-9_-]", "-", stored or filename)
                preview = (
                    f"<button class='phrase-image-preview' type='button' data-preview-src='{h(file_url)}' data-preview-name='{h(filename)}'>"
                    f"<img src='{h(file_url)}' alt='{h(filename)}' loading='lazy'></button>"
                    if phrase_file_is_image(file, filename, file_url) and file_url
                    else "<span class='file-chip-icon'>FILE</span>"
                )
                file_rows.append(
                    f"<div class='phrase-file'>{preview}<span class='phrase-file-name'>{h(filename)}</span>"
                    f"<button type='submit' form='{h(delete_form_id)}'>&#21024;&#38500;&#38468;&#20214;</button></div>"
                )
                file_delete_forms.append(
                    f"<form id='{h(delete_form_id)}' method='post' action='/phrases/file-delete'>"
                    f"<input type='hidden' name='id' value='{h(phrase_id)}'>"
                    f"<input type='hidden' name='stored' value='{h(stored)}'>"
                    f"</form>"
                )
            files_html = f"<div class='phrase-files'>{''.join(file_rows)}</div>" if file_rows else ""
            rows.append(
                f"<div class='card phrase-manager'>"
                f"<form method='post' action='/phrases/save' enctype='multipart/form-data'>"
                f"<input type='hidden' name='id' value='{h(phrase_id)}'>"
                f"<textarea name='text'>{h(text)}</textarea>"
                f"{files_html}"
                f"<label class='phrase-upload'>&#28155;&#21152;&#22270;&#29255;/&#38468;&#20214;&#65288;&#21487;&#25302;&#25341;&#25110; Ctrl+V &#31896;&#36148;&#22270;&#29255;&#65289; <input name='files' type='file' multiple accept='image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.rtf,.zip,.rar,.7z'></label>"
                f"<p><button type='submit'>&#20445;&#23384;</button></p>"
                f"</form>"
                f"{''.join(file_delete_forms)}"
                f"<form method='post' action='/phrases/delete'>"
                f"<input type='hidden' name='id' value='{h(phrase_id)}'>"
                f"<button type='submit'>&#21024;&#38500;</button>"
                f"</form></div>"
            )
        existing = "".join(rows) if rows else "<div class='card phrase-empty'>&#24403;&#21069;&#29992;&#25143;&#36824;&#27809;&#26377;&#24120;&#29992;&#35821;&#12290;</div>"
        phrase_editor_js = """
        <script>
        (() => {
          const previewModal = document.createElement('div');
          previewModal.className = 'preview-modal';
          previewModal.hidden = true;
          previewModal.innerHTML = '<button class="preview-modal-close" type="button" aria-label="Close">×</button><img alt="">';
          document.body.appendChild(previewModal);
          const modalImage = previewModal.querySelector('img');
          function openImagePreview(url, name) {
            modalImage.src = url;
            modalImage.alt = name || '';
            previewModal.hidden = false;
          }
          function closeImagePreview() {
            previewModal.hidden = true;
            modalImage.removeAttribute('src');
          }
          previewModal.addEventListener('click', (event) => {
            if (event.target === previewModal || event.target.closest('.preview-modal-close')) closeImagePreview();
          });
          document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && !previewModal.hidden) closeImagePreview();
          });
          document.querySelectorAll('.phrase-image-preview').forEach((button) => {
            const img = button.querySelector('img');
            if (img) {
              img.addEventListener('error', () => {
                const fallback = document.createElement('span');
                fallback.className = 'common-phrase-file-chip';
                fallback.textContent = button.dataset.previewName || 'IMAGE';
                button.replaceWith(fallback);
              });
            }
            button.addEventListener('click', () => openImagePreview(button.dataset.previewSrc || '', button.dataset.previewName || ''));
          });
          function clipboardImageFiles(event) {
            const clipboard = event.clipboardData;
            if (!clipboard) return [];
            const files = [];
            Array.from(clipboard.items || []).forEach((item, index) => {
              if (item.kind !== 'file' || !item.type.startsWith('image/')) return;
              const file = item.getAsFile();
              if (!file) return;
              const ext = (file.type.split('/')[1] || 'png').replace(/[^a-z0-9]/gi, '').toLowerCase() || 'png';
              const name = file.name && file.name !== 'image.png' ? file.name : `clipboard-${Date.now()}-${index + 1}.${ext}`;
              files.push(new File([file], name, {type: file.type || 'image/png'}));
            });
            if (!files.length) {
              Array.from(clipboard.files || []).forEach((file, index) => {
                if (!file.type.startsWith('image/')) return;
                const ext = (file.type.split('/')[1] || 'png').replace(/[^a-z0-9]/gi, '').toLowerCase() || 'png';
                const name = file.name || `clipboard-${Date.now()}-${index + 1}.${ext}`;
                files.push(new File([file], name, {type: file.type || 'image/png'}));
              });
            }
            return files;
          }
          function setupPhraseForm(form) {
            const input = form.querySelector('input[type="file"][name="files"]');
            const textarea = form.querySelector('textarea[name="text"]');
            if (!input || !textarea) return;
            let selectedFiles = [];
            let selectedPreviewUrls = [];
            const pending = document.createElement('div');
            pending.className = 'phrase-pending selected-files';
            input.closest('label').after(pending);
            function syncFiles() {
              const dataTransfer = new DataTransfer();
              selectedFiles.forEach((file) => dataTransfer.items.add(file));
              input.files = dataTransfer.files;
            }
            function render() {
              selectedPreviewUrls.forEach((url) => URL.revokeObjectURL(url));
              selectedPreviewUrls = [];
              pending.replaceChildren();
              if (!selectedFiles.length) return;
              const summary = document.createElement('div');
              summary.className = 'selected-summary';
              summary.textContent = `\u5f85\u4e0a\u4f20\uff1a${selectedFiles.map((file) => file.name).join('\u3001')}`;
              pending.appendChild(summary);
              const grid = document.createElement('div');
              grid.className = 'file-preview-grid';
              selectedFiles.forEach((file) => {
                const chip = document.createElement('div');
                chip.className = 'file-chip';
                if (file.type.startsWith('image/')) {
                  const img = document.createElement('img');
                  const url = URL.createObjectURL(file);
                  selectedPreviewUrls.push(url);
                  img.src = url;
                  img.alt = file.name;
                  img.title = '\u70b9\u51fb\u67e5\u770b\u5927\u56fe';
                  img.addEventListener('click', () => openImagePreview(url, file.name));
                  chip.appendChild(img);
                } else {
                  const icon = document.createElement('span');
                  icon.className = 'file-chip-icon';
                  icon.textContent = 'FILE';
                  chip.appendChild(icon);
                }
                const name = document.createElement('span');
                name.className = 'file-chip-name';
                name.title = file.name;
                name.textContent = file.name;
                chip.appendChild(name);
                grid.appendChild(chip);
              });
              pending.appendChild(grid);
            }
            function addFiles(files) {
              selectedFiles = [...selectedFiles, ...Array.from(files || [])];
              syncFiles();
              render();
            }
            input.addEventListener('change', () => addFiles(input.files));
            textarea.addEventListener('paste', (event) => {
              const files = clipboardImageFiles(event);
              if (!files.length) return;
              event.preventDefault();
              addFiles(files);
            });
            const uploadLabel = input.closest('label');
            [form, textarea, uploadLabel].filter(Boolean).forEach((target) => {
              target.addEventListener('dragenter', (event) => {
                event.preventDefault();
                event.stopPropagation();
                form.closest('.phrase-manager')?.classList.add('dragover');
              });
              target.addEventListener('dragover', (event) => {
                event.preventDefault();
                event.stopPropagation();
                form.closest('.phrase-manager')?.classList.add('dragover');
              });
              target.addEventListener('dragleave', (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (!form.contains(event.relatedTarget)) form.closest('.phrase-manager')?.classList.remove('dragover');
              });
              target.addEventListener('drop', (event) => {
                event.preventDefault();
                event.stopPropagation();
                form.closest('.phrase-manager')?.classList.remove('dragover');
                addFiles(event.dataTransfer.files);
              });
            });
          }
          document.querySelectorAll('.phrase-manager form[action="/phrases/save"]').forEach(setPhraseForm);
        })();
        </script>
        """
        body = (
            f"<div class='card'><h2>&#24120;&#29992;&#35821;</h2>"
            f"<p class='muted'>&#24403;&#21069;&#29992;&#25143;&#65306;{h(phrase_user_key())}&#12290;&#36825;&#37324;&#31649;&#29702;&#30340;&#24120;&#29992;&#35821;&#20250;&#26174;&#31034;&#22312;&#22238;&#22797;&#32534;&#36753;&#22120;&#19979;&#26041;&#65292;&#28857;&#20987;&#21363;&#21487;&#21457;&#36865;&#12290;&#32534;&#36753;&#24120;&#29992;&#35821;&#26102;&#25903;&#25345;&#25302;&#25341;&#38468;&#20214;&#12289;Ctrl+V &#31896;&#36148;&#21098;&#36148;&#26495;&#22270;&#29255;&#12290;</p></div>"
            f"<div class='card phrase-manager'><h3>&#26032;&#22686;&#24120;&#29992;&#35821;</h3>"
            f"<form method='post' action='/phrases/save' enctype='multipart/form-data'><textarea name='text' placeholder='&#36755;&#20837;&#24120;&#29992;&#22238;&#22797;&#20869;&#23481;&#65292;&#25903;&#25345; emoji'></textarea>"
            f"<label class='phrase-upload'>&#22270;&#29255;/&#38468;&#20214;&#65288;&#21487;&#25302;&#25341;&#25110; Ctrl+V &#31896;&#36148;&#22270;&#29255;&#65289; <input name='files' type='file' multiple accept='image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.rtf,.zip,.rar,.7z'></label>"
            f"<p><button type='submit'>&#28155;&#21152;</button></p></form></div>"
            f"{existing}"
            f"{phrase_editor_js}"
        )
        self.send_html("Common phrases", body)

    def save_phrase(self) -> None:
        fields, uploads = self.read_form()
        phrase_id = fields.get("id", "").strip()
        text = fields.get("text", "").strip()
        phrases = load_common_phrases()
        if phrase_id:
            updated = []
            for phrase in phrases:
                if phrase["id"] == phrase_id:
                    files = save_phrase_uploads(phrase_id, uploads, phrase.get("files") or [])
                    if text or files:
                        updated.append({"id": phrase_id, "text": text, "files": files})
                    else:
                        remove_phrase_files(phrase)
                else:
                    updated.append(phrase)
            phrases = updated
        elif text or uploads:
            phrase_id = new_phrase_id(text)
            phrases.append({"id": phrase_id, "text": text, "files": save_phrase_uploads(phrase_id, uploads, [])})
        save_common_phrases(phrases)
        self.redirect("/phrases")

    def delete_phrase(self) -> None:
        fields, _ = self.read_form()
        phrase_id = fields.get("id", "").strip()
        for phrase in load_common_phrases():
            if phrase["id"] == phrase_id:
                remove_phrase_files(phrase)
        phrases = [phrase for phrase in load_common_phrases() if phrase["id"] != phrase_id]
        save_common_phrases(phrases)
        self.redirect("/phrases")

    def delete_phrase_file(self) -> None:
        fields, _ = self.read_form()
        phrase_id = fields.get("id", "").strip()
        stored = fields.get("stored", "").strip()
        phrases = []
        for phrase in load_common_phrases():
            if phrase["id"] != phrase_id:
                phrases.append(phrase)
                continue
            files = []
            for file in phrase.get("files") or []:
                _, file_stored, _ = phrase_file_reference(file)
                if file_stored == stored:
                    file_path = (COMMON_PHRASES_DIR / stored).resolve()
                    if str(file_path).startswith(str(COMMON_PHRASES_DIR.resolve())):
                        file_path.unlink(missing_ok=True)
                else:
                    files.append(file)
            if phrase.get("text") or files:
                phrases.append({"id": phrase_id, "text": phrase.get("text") or "", "files": files})
        save_common_phrases(phrases)
        self.redirect("/phrases")

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

    def seller_read_receipt_html(self, msg: dict[str, Any]) -> str:
        if msg.get("seller") != 1:
            return ""
        read_at = ""
        for key in ("date_seen", "dateSeen", "DateSeen", "date_view", "dateView", "DateView", "date_viewed", "dateViewed", "DateViewed"):
            value = msg.get(key)
            if value:
                read_at = str(value)
                break
        if not read_at and str(msg.get("is_viewed") or msg.get("isViewed") or msg.get("IsViewed") or "").lower() in {"1", "true", "yes"}:
            read_at = "yes"
        if not read_at:
            return ""
        title = "Buyer read" if read_at == "yes" else f"Buyer read at {read_at}"
        return f"<span class='read-receipt' title='{h(title)}'>✓ 已读</span>"

    def chat_panel_html(self, selected_order: int, selected_chat: dict[str, Any] | None, selected_messages: list[dict[str, Any]]) -> str:
        if selected_chat:
            buyer_name = str(selected_chat.get("email") or "Buyer").split("@", 1)[0]
            buyer_lang = detect_buyer_language(selected_messages)
            options_html = order_options_html(selected_order)
            header = (
                f"<div class='conversation-header-main'><div class='conversation-header-title'>{h(buyer_name)}</div>"
                f"<div class='muted'>Order {h(selected_chat.get('id_i'))} · {h(short(selected_chat.get('product'), 110))} · Messages loaded: {len(selected_messages)} · Reply language: {h(lang_label(buyer_lang))}</div></div>"
                f"<div class='conversation-header-side'>{options_html}<div class='toolbar'><a href='/chat?order_id={selected_order}'>Table view</a>"
                f"<a href='/download-images?order_id={selected_order}'>Download images</a></div></div>"
            )
            rows = []
            total_messages = len(selected_messages)
            for idx, msg in enumerate(selected_messages, 1):
                is_seller = msg.get("seller") == 1
                cls = "seller" if is_seller else "buyer"
                author = "nose1989" if is_seller else buyer_name
                text = clean_text(msg.get("message"))
                try:
                    if is_attachment_message(msg):
                        text_html = attachment_html(msg)
                    else:
                        text_html = translate_incoming_html(text, msg.get("id"), should_translate=not is_seller)
                except Exception as exc:
                    text_html = h(text or f"Message render error: {exc}")
                msg_no = f"#{idx}/{total_messages}"
                msg_id = f" · ID {h(msg.get('id'))}" if msg.get("id") else ""
                read_receipt = self.seller_read_receipt_html(msg)
                rows.append(
                    f"<div class='chat-row {cls}'>"
                    f"<div class='chat-meta'><span class='chat-author'>{h(author)} <span class='muted'>{msg_no}{msg_id}</span></span><span>{read_receipt}{h(msg.get('date_written'))}</span></div>"
                    f"<div class='chat-bubble'>{text_html}</div></div>"
                )
            notice = ""
            if self.q("sent") == "1":
                notice = "<div class='notice ok-bg'>&#22238;&#22797;&#24050;&#21457;&#36865;&#65292;&#27491;&#22312;&#26174;&#31034;&#26368;&#26032;&#23545;&#35805;&#12290;</div>"
            return f"<div id='chat-panel' class='conversation-panel' data-kind='order' data-order-id='{selected_order}' data-message-count='{len(selected_messages)}'><div class='conversation-header'>{header}</div><div class='conversation-body'>{notice}{''.join(rows)}</div>{self.reply_editor(selected_order, buyer_lang)}</div>"
        return "<div id='chat-panel' class='conversation-panel' data-kind='order'><div class='empty-state'>No chats found</div></div>"

    def guest_chat_panel_html(self, chat: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        corr_id = int(chat.get("CorrID") or chat.get("corrID") or 0)
        corr_type = str(chat.get("CorrType") or chat.get("Type") or "visitor")
        name = str(chat.get("Name") or f"GUEST-{corr_id}")
        purchase = chat.get("PurchaseName") or "Pre-order consultation"
        header = (
            f"<div><div class='conversation-header-title'>{h(name)}</div>"
            f"<div class='muted'>Guest consultation · {h(corr_type)} #{corr_id} · {h(short(purchase, 110))} · Latest {len(messages)} messages</div></div>"
            "<div class='toolbar'><span class='muted'>Pre-order chat</span></div>"
        )
        rows = []
        total_messages = len(messages)
        for idx, msg in enumerate(messages, 1):
            is_seller = msg.get("seller") == 1
            cls = "seller" if is_seller else "buyer"
            author = "nose1989" if is_seller else name
            text = clean_text(msg.get("message"))
            if is_attachment_message(msg):
                text_html = attachment_html(msg, allow_guess_preview=True)
            else:
                text_html = translate_incoming_html(text, msg.get("id"), should_translate=not is_seller)
            msg_no = f"#{idx}/{total_messages}"
            msg_id = f" · ID {h(msg.get('id'))}" if msg.get("id") else ""
            rows.append(
                f"<div class='chat-row {cls}'>"
                f"<div class='chat-meta'><span class='chat-author'>{h(author)} <span class='muted'>{msg_no}{msg_id}</span></span><span>{h(msg.get('date_written'))}</span></div>"
                f"<div class='chat-bubble'>{text_html}</div></div>"
            )
        if not rows:
            rows.append("<div class='empty-state'>No guest messages loaded</div>")
        return f"<div id='chat-panel' class='conversation-panel' data-kind='guest' data-corr-id='{corr_id}' data-message-count='{len(messages)}'><div class='conversation-header'>{header}</div><div class='conversation-body'>{''.join(rows)}</div></div>"

    def api_chat_panel(self) -> None:
        if self.q("kind") == "guest":
            corr_id = int(self.q("corr_id", "0") or 0)
            corr_type = self.q("corr_type", "visitor")
            if not corr_id:
                return self.send_json({"ok": False, "error": "corr_id is required"}, 400)
            client.mark_guest_read(corr_type, corr_id)
            clear_unread_cache()
            messages = client.guest_messages(corr_type, corr_id)[-10:]
            chat = {
                "CorrID": corr_id,
                "CorrType": corr_type,
                "Name": self.q("name", f"GUEST-{corr_id}"),
                "PurchaseName": self.q("product", "Pre-order consultation"),
            }
            return self.send_json({"ok": True, "kind": "guest", "corr_id": corr_id, "count": len(messages), "read": True, "html": self.guest_chat_panel_html(chat, messages)})
        order_id = int(self.q("order_id", "0") or 0)
        if not order_id:
            return self.send_json({"ok": False, "error": "order_id is required"}, 400)
        email = self.q("email", f"order-{order_id}")
        product = self.q("product", "Direct order lookup")
        client.mark_chat_read(order_id)
        clear_unread_cache()
        messages = client.all_chat_messages(order_id)
        selected_chat = {"id_i": order_id, "email": email, "product": product} if messages else None
        self.send_json({"ok": True, "order_id": order_id, "count": len(messages), "read": True, "html": self.chat_panel_html(order_id, selected_chat, messages)})

    def api_translate_batch(self) -> None:
        payload = self.read_json_body()
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return self.send_json({"ok": False, "error": "messages must be a list"}, 400)
        results = []
        for item in messages[:100]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            text = clean_text(item.get("text"))
            if not text:
                continue
            translated, source_lang = google_translate(text, "zh-CN")
            results.append(
                {
                    "id": item_id,
                    "text": text,
                    "translated": translated,
                    "source_lang": source_lang,
                    "label": lang_label(source_lang),
                }
            )
        self.send_json({"ok": True, "results": results})

    def api_save_common_phrase(self) -> None:
        payload = self.read_json_body()
        text = clean_text(payload.get("text"))
        if not text:
            return self.send_json({"ok": False, "error": "text is required"}, 400)
        created, phrase_id = save_text_common_phrase(text)
        self.send_json({"ok": True, "created": created, "id": phrase_id})

    def chats(self) -> None:
        chats = client.chats(page_size=100)
        guest_error = ""
        try:
            guest_chats = client.guest_chats(limit=10)
        except Exception as exc:
            guest_chats = []
            guest_error = str(exc)
        selected_kind = self.q("kind", "order")
        selected_order = 0 if selected_kind == "guest" else int(self.q("order_id", "0") or 0)
        selected_corr_id = int(self.q("corr_id", "0") or 0) if selected_kind == "guest" else 0
        selected_corr_type = self.q("corr_type", "visitor")
        if selected_kind != "guest" and not selected_order and chats:
            selected_order = int(chats[0].get("id_i") or 0)
            selected_kind = "order"
        elif not selected_corr_id and guest_chats:
            selected_kind = "guest"
            selected_corr_id = int(guest_chats[0].get("CorrID") or 0)
            selected_corr_type = str(guest_chats[0].get("CorrType") or "visitor")
        if selected_kind == "order" and selected_order:
            client.mark_chat_read(selected_order)
            clear_unread_cache()

        items = []
        selected_chat: dict[str, Any] | None = None
        selected_guest_chat: dict[str, Any] | None = None
        for chat in chats:
            order_id = int(chat.get("id_i") or 0)
            if selected_kind == "order" and order_id == selected_order:
                selected_chat = chat
            email = str(chat.get("email") or "unknown")
            name = email.split("@", 1)[0] or email
            initials = (name[:1] or "?").upper()
            unread = 0 if selected_kind == "order" and order_id == selected_order else int(chat.get("cnt_new") or 0)
            active = " active" if selected_kind == "order" and order_id == selected_order else ""
            preview = short(chat.get("product"), 80)
            when = str(chat.get("last_date") or "")
            short_when = when[11:16] if len(when) >= 16 else when
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            items.append(
                f"<a class='conversation-item{active}' data-kind='order' data-order-id='{order_id}' data-email='{h(email)}' data-product='{h(chat.get('product'))}' href='/chats?order_id={order_id}'>"
                f"<div class='avatar'>{h(initials)}</div>"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                f"<div class='conversation-time'>{h(short_when)}{badge}</div></a>"
            )
        if guest_chats or guest_error:
            items.append("<div class='conversation-title' style='font-size:18px;padding-top:18px'>Guest consultations</div>")
        if guest_error:
            items.append(f"<div class='conversation-item'><div class='avatar'>!</div><div><div class='conversation-name'>Guest API error</div><div class='preview'>{h(short(guest_error, 100))}</div></div><div></div></div>")
        for chat in guest_chats:
            corr_id = int(chat.get("CorrID") or 0)
            corr_type = str(chat.get("CorrType") or chat.get("Type") or "visitor")
            if selected_kind == "guest" and corr_id == selected_corr_id and corr_type == selected_corr_type:
                selected_guest_chat = chat
            name = str(chat.get("Name") or f"GUEST-{corr_id}")
            initials = (name.replace("GUEST-", "")[:1] or "G").upper()
            is_selected_guest = selected_kind == "guest" and corr_id == selected_corr_id and corr_type == selected_corr_type
            unread = 0 if is_selected_guest else (1 if not int(chat.get("IsAuthor") or 0) and not int(chat.get("IsViewed") or 0) else 0)
            active = " active" if selected_kind == "guest" and corr_id == selected_corr_id and corr_type == selected_corr_type else ""
            preview = chat.get("Text") or chat.get("PurchaseName") or ""
            when = str(chat.get("DateWrite") or chat.get("DateWriteUtc") or "")
            short_when = when[11:16] if len(when) >= 16 and "-" in when[:10] else when[-5:]
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            href = f"/chats?kind=guest&corr_type={urllib.parse.quote(corr_type)}&corr_id={corr_id}&name={urllib.parse.quote(name)}&product={urllib.parse.quote(str(chat.get('PurchaseName') or ''))}"
            items.append(
                f"<a class='conversation-item{active}' data-kind='guest' data-corr-id='{corr_id}' data-corr-type='{h(corr_type)}' data-name='{h(name)}' data-product='{h(chat.get('PurchaseName'))}' href='{h(href)}'>"
                f"<div class='avatar'>{h(initials)}</div>"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                f"<div class='conversation-time'>{h(short_when)}{badge}</div></a>"
            )

        selected_messages: list[dict[str, Any]] = []
        if selected_kind == "guest" and selected_corr_id:
            if selected_guest_chat is None:
                selected_guest_chat = {
                    "CorrID": selected_corr_id,
                    "CorrType": selected_corr_type,
                    "Name": self.q("name", f"GUEST-{selected_corr_id}"),
                    "PurchaseName": self.q("product", "Pre-order consultation"),
                }
            client.mark_guest_read(selected_corr_type, selected_corr_id)
            clear_unread_cache()
            selected_messages = client.guest_messages(selected_corr_type, selected_corr_id)[-10:]
        elif selected_order:
            selected_messages = client.all_chat_messages(selected_order)
            if selected_chat is None and selected_messages:
                selected_chat = {"id_i": selected_order, "email": f"order-{selected_order}", "product": "Direct order lookup"}

        panel = self.guest_chat_panel_html(selected_guest_chat, selected_messages) if selected_kind == "guest" and selected_guest_chat else self.chat_panel_html(selected_order, selected_chat, selected_messages)
        ajax = """
        <script>
        (() => {
          const list = document.getElementById('conversation-list');
          if (!list) return;
          let refreshingActiveOrder = false;
          function runPanelScripts(panel) {
            panel.querySelectorAll('script').forEach((oldScript) => {
              const script = document.createElement('script');
              script.textContent = oldScript.textContent;
              document.body.appendChild(script);
              script.remove();
            });
          }
          function conversationBadge(link) {
            let badge = link.querySelector('.badge');
            if (badge) return badge;
            const time = link.querySelector('.conversation-time');
            if (!time) return null;
            badge = document.createElement('div');
            badge.className = 'badge';
            time.appendChild(badge);
            return badge;
          }
          function setConversationBadge(link, count) {
            const value = Number(count || 0);
            if (value <= 0) {
              clearConversationBadge(link);
              return;
            }
            const badge = conversationBadge(link);
            if (badge) badge.textContent = String(value);
          }
          function clearConversationBadge(link) {
            const badge = link?.querySelector('.badge');
            if (badge) badge.remove();
            if (link) delete link.dataset.pendingUnread;
          }
          function paramsForLink(link) {
            const kind = link.dataset.kind || 'order';
            return kind === 'guest'
              ? new URLSearchParams({kind: 'guest', corr_id: link.dataset.corrId || '', corr_type: link.dataset.corrType || 'visitor', name: link.dataset.name || '', product: link.dataset.product || ''})
              : new URLSearchParams({order_id: link.dataset.orderId || '', email: link.dataset.email || '', product: link.dataset.product || ''});
          }
          function escapeHtml(value) {
            return String(value).replace(/[&<>"']/g, (char) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
          }
          function showPanelLoading(panel, link) {
            const name = link.querySelector('.conversation-name')?.textContent?.trim() || '';
            const preview = link.querySelector('.preview')?.textContent?.trim() || '';
            panel.className = 'conversation-panel loading';
            panel.innerHTML = `
              <div class="conversation-header">
                <div class="conversation-header-main">
                  <div class="conversation-header-title">${escapeHtml(name || '\u52a0\u8f7d\u4e2d')}</div>
                  <div class="muted">${escapeHtml(preview || '\u6b63\u5728\u52a0\u8f7d\u8ba2\u5355\u6d88\u606f')}</div>
                </div>
              </div>
              <div class="chat-loading">
                <div class="chat-loading-spinner"></div>
                <div class="chat-loading-title">\u6b63\u5728\u52a0\u8f7d\u6d88\u606f...</div>
                <div class="chat-loading-subtitle">\u5207\u6362\u8ba2\u5355\u540e\u4f1a\u81ea\u52a8\u663e\u793a\u6700\u65b0\u5bf9\u8bdd</div>
                <div class="loading-lines">
                  <div class="loading-line"></div>
                  <div class="loading-line short"></div>
                  <div class="loading-line"></div>
                </div>
              </div>`;
          }
          async function loadPanel(link, options = {}) {
            const panel = document.getElementById('chat-panel');
            if (!panel) { location.href = link.href; return; }
            if (!options.silent) showPanelLoading(panel, link);
            try {
              const res = await fetch('/api/chat-panel?' + paramsForLink(link).toString(), {cache: 'no-store'});
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || 'Load failed');
              panel.outerHTML = data.html;
              const newPanel = document.getElementById('chat-panel');
              if (newPanel) runPanelScripts(newPanel);
              if (newPanel && window.loadDigisellerTranslations) window.loadDigisellerTranslations(newPanel);
              if (options.clearBadge) clearConversationBadge(link);
              if (options.pushHistory) history.pushState(null, '', link.href);
              if (options.refreshUnread && window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
            } catch (error) {
              if (!options.silent) location.href = link.href;
            }
          }
          list.addEventListener('click', async (event) => {
            const link = event.target.closest('.conversation-item');
            if (!link) return;
            event.preventDefault();
            list.querySelectorAll('.conversation-item.active').forEach((item) => item.classList.remove('active'));
            link.classList.add('active');
            await loadPanel(link, {pushHistory: true, clearBadge: true, refreshUnread: true});
          });
          window.handleDigisellerUnreadData = (data) => {
            const buyerUnread = Array.isArray(data.buyer_unread) ? data.buyer_unread : [];
            const unreadByOrder = new Map();
            buyerUnread.forEach((item) => {
              const orderId = String(item.order_id || '');
              const count = Number(item.cnt_new || 0);
              if (orderId && count > 0) unreadByOrder.set(orderId, count);
            });
            list.querySelectorAll('.conversation-item[data-kind="order"]').forEach((link) => {
              const count = unreadByOrder.get(String(link.dataset.orderId || '')) || 0;
              if (count > 0) setConversationBadge(link, count);
              else if (!link.classList.contains('active')) clearConversationBadge(link);
            });
            const active = list.querySelector('.conversation-item.active[data-kind="order"]');
            if (!active) return;
            const activeCount = unreadByOrder.get(String(active.dataset.orderId || '')) || 0;
            if (activeCount <= 0 || refreshingActiveOrder) return;
            active.dataset.pendingUnread = String(activeCount);
            setConversationBadge(active, activeCount);
            refreshingActiveOrder = true;
            loadPanel(active, {silent: true}).finally(() => { refreshingActiveOrder = false; });
          };
          window.addEventListener('popstate', () => location.reload());
          if (window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
          setInterval(() => {
            if (!document.hidden && window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
          }, 15000);
        })();
        </script>
        """
        body = f"<div class='messages-layout'><div id='conversation-list' class='conversation-list'><div class='conversation-title'>Messages</div>{''.join(items)}</div>{panel}</div>{ajax}"
        self.send_html("Messages", body)

    def chat(self) -> None:
        order_id = int(self.q("order_id", "0"))
        if not order_id:
            return self.send_html("Chat", "<div class='card'>Pass ?order_id=...</div>")
        client.mark_chat_read(order_id)
        clear_unread_cache()
        msgs = client.all_chat_messages(order_id)
        rows = []
        for m in msgs:
            receipt = self.seller_read_receipt_html(m)
            who = ("Seller " + receipt) if m.get("seller") == 1 else "Buyer"
            cls = "msg-seller" if m.get("seller") == 1 else "msg-buyer"
            text = clean_text(m.get("message"))
            if m.get("is_file"):
                text = attachment_html(m)
            else:
                text = h(text)
            rows.append([h(m.get("date_written")), who, f"<div class='{cls}'>{text}</div>"])
        body = f"<div class='card'><h2>Chat {order_id}</h2><p><a href='/download-images?order_id={order_id}'>Download buyer images</a></p></div>" + table(["Date", "Who", "Message"], rows)
        self.send_html("Chat", body)

    def unread(self) -> None:
        buyer = [c for c in client.chats(only_unread=True) if int(c.get("cnt_new") or 0) > 0]
        try:
            guest = [
                c
                for c in client.guest_chats(limit=10)
                if not int(c.get("IsAuthor") or 0) and not int(c.get("IsViewed") or 0)
            ]
        except Exception:
            guest = []
        admin: list[dict[str, Any]] = []  # Do not count historical admin notices as unread.
        b_rows = [[f"<a href='/chats?order_id={h(c.get('id_i'))}'>{h(c.get('id_i'))}</a>", h(c.get("last_date")), h(c.get("cnt_new")), h(c.get("email")), h(short(c.get("product"), 100))] for c in buyer]
        g_rows = [
            [
                f"<a href='/chats?kind=guest&corr_type={h(c.get('CorrType') or c.get('Type') or 'visitor')}&corr_id={h(c.get('CorrID'))}'>{h(c.get('Name') or ('GUEST-' + str(c.get('CorrID') or '')))}</a>",
                h(c.get("DateWrite") or c.get("DateWriteUtc")),
                h(c.get("CorrType") or c.get("Type") or "visitor"),
                h(short(c.get("Text"), 120)),
                h(short(c.get("PurchaseName"), 80)),
            ]
            for c in guest
        ]
        a_rows = [[h(m.get("date")), h(m.get("id")), h(short(m.get("text") or m.get("message"), 180))] for m in admin]
        body = f"<div class='card'><h2>Unread</h2><p>Buyer unread: {len(buyer)} | Guest unread: {len(guest)} | Admin unread: {len(admin)}</p></div><h3>Buyer chats</h3>{table(['Order','Last','New','Email','Product'], b_rows)}<h3>Guest consultations</h3>{table(['Guest','Last','Type','Text','Product'], g_rows)}<h3>Admin messages</h3>{table(['Date','ID','Text'], a_rows)}"
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

    def stock(self) -> None:
        pid = self.q("product_id", "").strip()
        variant_id = self.q("variant_id", "0").strip()
        count = self.q("uploaded", "").strip()
        result = ""
        if count:
            result = f"<div class='card ok'>Uploaded {h(count)} stock item(s) to product {h(pid)}.</div>"
        product_info = ""
        if pid:
            try:
                data = client.product(int(pid))
                product = data.get("product", data)
                summary = {k: product.get(k) for k in ["id", "name", "is_available", "num_in_stock", "type_good"] if k in product}
                summary["delivery_mode"] = delivery_mode(product)[0]
                product_info = f"<pre class='card code'>{h(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>"
            except Exception as exc:
                product_info = f"<div class='card bad'>Product lookup failed: {h(exc)}</div>"
        form = f"""
        <div class='card'>
          <h2>Bulk stock upload</h2>
          <form action='/stock/upload' method='post'>
            <p><input name='product_id' placeholder='Product ID' value='{h(pid)}' required>
            <input name='variant_id' placeholder='Variant ID optional' value='{h(variant_id)}'></p>
            <p><textarea name='stock' rows='14' style='width:100%;box-sizing:border-box' placeholder='One stock item per line. Use either value only, or serial<TAB>value, or serial | value.' required></textarea></p>
            <label><input type='checkbox' name='confirm' value='1' required> Confirm upload these lines into this product stock</label>
            <p><button type='submit'>Upload stock</button></p>
          </form>
          <p class='muted'>Adds Text/Url product content through Digiseller <code>/product/content/add/text</code>. Blank lines are ignored.</p>
        </div>
        """
        self.send_html("Bulk stock upload", result + form + product_info)

    def upload_stock(self) -> None:
        fields, _ = self.read_form()
        product_id = int(fields.get("product_id", "0") or 0)
        variant_id = int(fields.get("variant_id", "0") or 0)
        raw_stock = fields.get("stock", "")
        if not product_id:
            raise RuntimeError("Product ID is required")
        if fields.get("confirm") != "1":
            raise RuntimeError("Confirm the upload before submitting")
        items = parse_stock_lines(raw_stock, variant_id=variant_id)
        if not items:
            raise RuntimeError("Paste at least one stock item")
        if len(items) > 1000:
            raise RuntimeError("Upload at most 1000 stock items at once")
        response = client.add_text_stock(product_id, items)
        rows = [
            ["Product ID", h(product_id)],
            ["Uploaded lines", h(len(items))],
            ["Variant ID", h(variant_id or "-")],
        ]
        body = (
            "<div class='card'><h2>Stock uploaded</h2>"
            + table(["Field", "Value"], rows)
            + f"<p><a href='/stock?product_id={product_id}&variant_id={variant_id}&uploaded={len(items)}'>Back to stock uploader</a></p></div>"
            + f"<details class='card'><summary>API response</summary><pre class='code'>{h(json.dumps(response, ensure_ascii=False, indent=2))}</pre></details>"
        )
        self.send_html("Stock uploaded", body)

    def product(self) -> None:
        pid = self.q("product_id", "")
        form = f"<div class='card'><h2>Product</h2><form><input name='product_id' placeholder='5870983' value='{h(pid)}'><button>Lookup</button></form></div>"
        if not pid:
            try:
                all_items: list[dict[str, Any]] = []
                page = 1
                while True:
                    data = client.shop_products(page=page, rows=100)
                    items = data.get("product", [])
                    if not isinstance(items, list):
                        break
                    all_items.extend(items)
                    total_pages = int(data.get("totalPages") or 1)
                    if page >= total_pages:
                        break
                    page += 1
                rows_html: list[list[Any]] = []
                for item in all_items:
                    pid_val = str(item.get("id") or "")
                    name = clean_text(item.get("name"))
                    price = f"{item.get('price') or ''} {item.get('currency') or ''}".strip()
                    available = "Yes" if int(item.get("is_available") or 0) else "No"
                    delivery = "<span class='muted'>Unknown</span>"
                    if pid_val:
                        try:
                            product_data = client.product(int(pid_val))
                            product_detail = product_data.get("product", product_data)
                            if isinstance(product_detail, dict):
                                delivery = delivery_mode_html(product_detail)
                        except Exception as exc:
                            delivery = f"<span class='bad'>{h(short(exc, 60))}</span>"
                    rows_html.append([
                        f"<a href='/product?product_id={h(pid_val)}'>{h(pid_val)}</a>",
                        h(name),
                        h(price),
                        f"<span class='{'ok' if available == 'Yes' else 'bad'}'>{available}</span>",
                        delivery,
                        f"<a href='/stock?product_id={h(pid_val)}'>Stock</a>",
                    ])
                products_table = table(["ID", "Name", "Price", "Available", "Delivery", "Actions"], rows_html)
                summary = f"<div class='card'><h2>Active Products ({len(all_items)})</h2>{products_table}</div>"
            except Exception as exc:
                summary = f"<div class='card bad'>Failed to load products: {h(exc)}</div>"
            return self.send_html("Product", form + summary)
        data = client.product(int(pid))
        product = data.get("product", data)
        summary = {k: product.get(k) for k in ["id", "name", "price", "currency", "is_available", "num_in_stock", "owner", "type_good"] if k in product}
        summary["delivery_mode"] = delivery_mode(product)[0]
        stock_link = f"<div class='card'><a href='/stock?product_id={h(pid)}'>Bulk upload stock for this product</a></div>"
        self.send_html("Product", form + stock_link + f"<pre class='card code'>{h(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>")

    def unique_code_page(self) -> None:
        code = self.q("code", "").strip()
        form = f"""
        <div class='card'>
          <h2>GUID / unique code lookup</h2>
          <form>
            <input name='code' maxlength='16' placeholder='808CD67F03894103' value='{h(code)}'>
            <button>Verify</button>
          </form>
          <p class='muted'>Use the 16-character Digiseller buyer verification code.</p>
        </div>
        """
        if not code:
            return self.send_html("GUID lookup", form)
        if not valid_unique_code(code):
            return self.send_html("GUID lookup", form + "<div class='card bad'>Code must be exactly 16 letters or digits.</div>", 400)
        item = unique_code_lookup(code)
        rows = [
            ["Code", h(item.get("code"))],
            ["Order", f"<a href='/chats?order_id={h(item.get('invoice'))}'>{h(item.get('invoice'))}</a>" if item.get("invoice") else ""],
            ["Product", h(item.get("product_name"))],
            ["Product ID", f"<a href='/product?product_id={h(item.get('product_id'))}'>{h(item.get('product_id'))}</a>" if item.get("product_id") else ""],
            ["Amount", h(f"{item.get('amount') or ''} {item.get('currency') or ''}".strip())],
            ["Paid", h(item.get("date_pay"))],
            ["Buyer email", h(item.get("email"))],
            ["State", h(item.get("state_label"))],
        ]
        details = "<div class='card'><h2>Verification result</h2>" + table(["Field", "Value"], rows) + "</div>"
        raw = f"<details class='card'><summary>Raw API response</summary><pre class='code'>{h(json.dumps(item.get('raw'), ensure_ascii=False, indent=2))}</pre></details>"
        self.send_html("GUID lookup", form + details + raw)

    def api_unique_code(self) -> None:
        code = self.q("code", "").strip()
        if not valid_unique_code(code):
            return self.send_json({"ok": False, "error": "Code must be exactly 16 letters or digits"}, 400)
        try:
            item = unique_code_lookup(code)
        except Exception as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 404)
        self.send_json({"ok": True, "item": {key: value for key, value in item.items() if key != "raw"}})

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
        now = time.time()
        cached = UNREAD_CACHE.get("data")
        force = self.q("force", "0") in {"1", "true", "yes"}
        if not force and cached is not None and now - float(UNREAD_CACHE.get("time") or 0) < 30:
            return self.send_json(cached)
        data = unread_summary()
        UNREAD_CACHE["time"] = now
        UNREAD_CACHE["data"] = data
        self.send_json(data)

    def api_online_keepalive(self) -> None:
        self.send_json(refresh_public_online_status(force=True))

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

    def serve_logo(self) -> None:
        if not SHINCHAN_LOGO.exists():
            return self.send_html("Not found", "Not found", 404)
        data = SHINCHAN_LOGO.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_phrase_file(self, path: str) -> None:
        rel = urllib.parse.unquote(path.removeprefix("/phrase-files/"))
        if not rel or "/" in rel or "\\" in rel:
            self.send_error(404)
            return
        file_path = (COMMON_PHRASES_DIR / rel).resolve()
        if not str(file_path).startswith(str(COMMON_PHRASES_DIR.resolve())) or not file_path.exists():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        for phrase in load_common_phrases():
            for file in phrase.get("files") or []:
                if str(file.get("stored") or "") == rel:
                    saved_type = str(file.get("content_type") or "")
                    if saved_type and saved_type != "application/octet-stream":
                        content_type = saved_type
                    break
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
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
    start_online_keepalive()
    start_chat_keepalive_browser()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Digiseller admin running at http://{host}:{port}")
    print("Open the page and click the alerts button to allow sound/voice notifications.")
    print("Online keepalive: enabled by default; set DIGISELLER_KEEP_ONLINE=0 to disable.")
    print("Chat keepalive window: opens automatically by default; set DIGISELLER_CHAT_OPEN_BROWSER=0 to disable.")
    print("Background watcher: python3 digiseller_admin.py watch --interval 15")
    print("Press Ctrl+C to stop")
    server.serve_forever()


if __name__ == "__main__":
    main()
