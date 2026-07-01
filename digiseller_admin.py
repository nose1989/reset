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

import base64
import concurrent.futures
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
import uuid
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
SALES_ORDER_SEEN_FILE = APP_DIR / "sales_order_seen.json"
DIGISELLER_STOCK_CACHE_FILE = APP_DIR / "digiseller_stock_cache.json"
API_BASE = "https://api.digiseller.com/api"
APP_VERSION = "v8.21-funpay-sync-products"
GGSEL_INLINE_ATTACHMENT_MAX_BYTES = 128 * 1024
GGSEL_TEXT_ATTACHMENT_TYPES = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
}
GGSEL_TEXT_ATTACHMENT_SUFFIXES = {
    ".csv",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".text",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
PROMPT_SOURCE_REPOS: tuple[tuple[str, str, str], ...] = (
    (
        "nprasann/ai-engineering-prompt-templates",
        "https://github.com/nprasann/ai-engineering-prompt-templates",
        "MIT；通用 AI 编程提示词模板，强调先计划后执行。",
    ),
    (
        "PatrickJS/awesome-cursorrules",
        "https://github.com/PatrickJS/awesome-cursorrules",
        "Cursor rules 合集；适合参考 .cursor/rules 的项目约束写法。",
    ),
    (
        "satinath-nit/ai-dev-playbook",
        "https://github.com/satinath-nit/ai-dev-playbook",
        "面向真实编码任务的提示词流程，可参考任务拆分方式。",
    ),
    (
        "aminrj/ai-copilot-prompts",
        "https://github.com/aminrj/ai-copilot-prompts",
        "Copilot、Claude Code、Cursor 等工具的分类提示词合集。",
    ),
)
CODING_PROMPT_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "title": "只读检查：先要最小改动计划",
        "use": "不确定怎么改时先用，避免 AI 直接大改。",
        "prompt": """请先只读检查这个仓库，不要改文件。
目标：<写清楚你要完成的功能或 bug>
请输出：
1. 需要看的关键文件
2. 最小可行改动方案
3. 预计只会修改哪些文件
4. 需要运行的 lint/typecheck/test 命令
限制：不要重构，不要新增依赖，除非你说明必须新增的理由。""",
    },
    {
        "title": "执行：严格按最小改动实现",
        "use": "计划确认后使用，让 AI 只改必要位置。",
        "prompt": """请按上面的计划实现，要求代码改动最小。
只修改完成目标必须修改的文件，不做无关重构，不改测试来迎合实现。
如果发现计划不成立，先停下来说明原因和新的最小方案。
完成后请列出改动文件，并运行可用的 lint/typecheck/test。""",
    },
    {
        "title": "修 bug：先复现再修",
        "use": "页面、接口、聊天、上传等行为异常时使用。",
        "prompt": """请先根据描述复现问题并定位根因，不要马上改代码。
问题：<描述现象、页面、输入、期望结果>
确认根因后，用最小代码改动修复。
不要改变现有 API、页面路径或数据格式，除非这是修复所必需。
修复后说明复现步骤、修复点、验证命令。""",
    },
    {
        "title": "给现有项目加小功能",
        "use": "适合本地 admin 页面新增一个入口、按钮、字段或小工具。",
        "prompt": """请在现有代码风格下添加这个小功能：<功能描述>。
优先复用现有函数、CSS、路由和数据结构。
不要引入框架，不要拆文件，不要新增依赖，除非现有结构无法完成。
UI 文案保持简短；如果需要新页面，请加到现有导航并保持样式一致。""",
    },
    {
        "title": "让 AI 生成项目规则",
        "use": "给 Cursor、Windsurf、Claude Code 或 Devin 写项目约束。",
        "prompt": """请阅读 README、主要入口文件和现有代码风格，生成一份简短项目规则。
规则要包含：运行方式、禁止提交的文件、代码风格、测试命令、最小改动原则。
不要编造不存在的命令；不确定的地方标记为“需要确认”。""",
    },
    {
        "title": "提交前自查",
        "use": "准备 PR 前使用，减少遗漏。",
        "prompt": """请审查当前 diff：
1. 是否有无关文件或临时文件
2. 是否泄露密钥、cookie、token
3. 是否有可更小的实现
4. 是否破坏已有页面/接口
5. 还需要运行哪些检查
请只给高风险问题和必须修复项，不要泛泛而谈。""",
    },
)


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
        key = key.strip()
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value.strip().strip('"').strip("'")


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def strip_path_prefix(path: str, prefix: str) -> str:
    return path[len(prefix) :] if path.startswith(prefix) else path


def short(value: Any, length: int = 110) -> str:
    text = clean_text(value)
    return text if len(text) <= length else text[: length - 1] + "…"


def parse_ymd_date(value: Any, default: dt.date) -> dt.date:
    text = clean_text(value)
    if not text:
        return default
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return default


def int_from_query(value: Any) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 else None


def stat_value(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if current is not None else ""


def format_stat(value: Any, suffix: str = "") -> str:
    if value in (None, ""):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return h(value)
    if number.is_integer():
        text = f"{int(number):,}"
    else:
        text = f"{number:,.2f}".rstrip("0").rstrip(".")
    return h(f"{text}{suffix}")


def product_brand(product: Any) -> tuple[str, str, str, str, str] | None:
    value = clean_text(product).lower()
    if not value:
        return None
    for needle, name, mark, logo, background in PRODUCT_BRANDS:
        if needle in value:
            return name, mark, logo, background, value
    return None


def product_avatar_html(product: Any, fallback: str) -> str:
    brand = product_brand(product)
    if not brand:
        product_text = clean_text(product)
        if not product_text:
            return f"<div class='avatar'>{h(fallback)}</div>"
        words = re.findall(r"[A-Za-z0-9]+", product_text)
        label = words[0] if words else short(product_text, 8)
        mark = label[:2].upper() or fallback
        return (
            f"<div class='avatar product-logo-avatar generic-product-avatar' title='{h(product_text)}'>"
            f"<span class='product-logo-mark'>{h(mark)}</span>"
            f"<span class='product-logo-name'>{h(short(label, 8))}</span></div>"
        )
    name, mark, logo, background, _ = brand
    fallback_color = "#ffffff" if background.lower() in {"#111111", "#111827", "#000000"} else "#111827"
    return (
        f"<div class='avatar product-logo-avatar brand-image-avatar' title='{h(name)}' style='background:{h(background)}'>"
        f"<img class='product-brand-logo' src='{h(logo)}' alt='{h(name)}' loading='lazy' "
        "onerror=\"this.hidden=true;this.nextElementSibling.hidden=false\">"
        f"<span class='product-logo-fallback' style='color:{h(fallback_color)}' hidden>{h(mark)}</span></div>"
    )


def sort_time(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            pass
    now = dt.datetime.now()
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                candidate = now.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0)
                if candidate.timestamp() > time.time() + 300:
                    candidate = candidate - dt.timedelta(days=1)
                return candidate.timestamp()
            except ValueError:
                pass
    relative_match = re.fullmatch(r"(today|yesterday|сегодня|вчера),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", text, re.I)
    if relative_match:
        base = now
        if relative_match.group(1).lower() in {"yesterday", "вчера"}:
            base = base - dt.timedelta(days=1)
        return base.replace(
            hour=int(relative_match.group(2)),
            minute=int(relative_match.group(3)),
            second=int(relative_match.group(4) or 0),
            microsecond=0,
        ).timestamp()
    date_match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", text)
    if date_match:
        try:
            parsed = dt.datetime(now.year, int(date_match.group(2)), int(date_match.group(1)))
            if parsed.timestamp() > time.time() + 86400:
                parsed = parsed.replace(year=now.year - 1)
            return parsed.timestamp()
        except ValueError:
            pass
    month_names = {
        "jan": 1,
        "january": 1,
        "фев": 2,
        "февраля": 2,
        "feb": 2,
        "february": 2,
        "мар": 3,
        "марта": 3,
        "mar": 3,
        "march": 3,
        "апр": 4,
        "апреля": 4,
        "apr": 4,
        "april": 4,
        "май": 5,
        "мая": 5,
        "may": 5,
        "июн": 6,
        "июня": 6,
        "jun": 6,
        "june": 6,
        "июл": 7,
        "июля": 7,
        "jul": 7,
        "july": 7,
        "авг": 8,
        "августа": 8,
        "aug": 8,
        "august": 8,
        "сен": 9,
        "сентября": 9,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "окт": 10,
        "октября": 10,
        "oct": 10,
        "october": 10,
        "ноя": 11,
        "ноября": 11,
        "nov": 11,
        "november": 11,
        "дек": 12,
        "декабря": 12,
        "dec": 12,
        "december": 12,
    }
    month_match = re.fullmatch(
        r"(\d{1,2})\s+([A-Za-zА-Яа-яЁё.]+)(?:\s+(\d{4}))?,?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?.*",
        text,
        re.I,
    )
    if month_match:
        month = month_names.get(month_match.group(2).strip(".").lower())
        if month:
            try:
                parsed = dt.datetime(
                    int(month_match.group(3) or now.year),
                    month,
                    int(month_match.group(1)),
                    int(month_match.group(4)),
                    int(month_match.group(5)),
                    int(month_match.group(6) or 0),
                )
                if parsed.timestamp() > time.time() + 86400:
                    parsed = parsed.replace(year=now.year - 1)
                return parsed.timestamp()
            except ValueError:
                pass
    for fmt in ("%d %B, %H:%M:%S", "%d %b, %H:%M:%S", "%d %B, %H:%M", "%d %b, %H:%M"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            parsed = parsed.replace(year=now.year)
            if parsed.timestamp() > time.time() + 86400:
                parsed = parsed.replace(year=now.year - 1)
            return parsed.timestamp()
        except ValueError:
            pass
    return 0.0


def full_time_from_date(date_value: Any, time_value: Any) -> str:
    time_text = str(time_value or "").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", time_text):
        return time_text
    date_timestamp = sort_time(date_value)
    if not date_timestamp:
        return time_text
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed_time = dt.datetime.strptime(time_text, fmt)
            parsed_date = dt.datetime.fromtimestamp(date_timestamp)
            return parsed_date.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=parsed_time.second,
                microsecond=0,
            ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return time_text


def is_recent_time(value: Any, days: int) -> bool:
    timestamp = sort_time(value)
    return bool(timestamp and timestamp >= time.time() - max(int(days), 1) * 86400)


TRANSLATE_CACHE: dict[tuple[str, str, str], tuple[str, str]] = {}
TRANSLATE_CACHE_FILE = APP_DIR / "translation_cache.json"
TRANSLATE_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60
TRANSLATE_CACHE_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
TRANSLATE_CACHE_LOCK = threading.Lock()
TRANSLATE_CACHE_LOADED = False


def translation_cache_id(source_lang: str, target_lang: str, text: str) -> str:
    payload = json.dumps([source_lang, target_lang, text], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def translation_cache_is_fresh(updated_at: object) -> bool:
    try:
        updated = dt.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - updated).total_seconds() < TRANSLATE_CACHE_TTL_SECONDS


def load_translation_cache() -> None:
    global TRANSLATE_CACHE_LOADED
    if TRANSLATE_CACHE_LOADED:
        return
    with TRANSLATE_CACHE_LOCK:
        if TRANSLATE_CACHE_LOADED:
            return
        fresh_data: dict[str, object] = {}
        if TRANSLATE_CACHE_FILE.exists():
            try:
                data = json.loads(TRANSLATE_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict):
                for key, item in data.items():
                    if not isinstance(item, dict) or not translation_cache_is_fresh(item.get("updated_at")):
                        continue
                    source_lang = str(item.get("source_lang") or "auto")
                    target_lang = str(item.get("target_lang") or "")
                    value = str(item.get("text") or "")
                    translated = str(item.get("translated") or "")
                    detected = str(item.get("detected") or source_lang)
                    if target_lang and value and translated:
                        TRANSLATE_CACHE[(source_lang, target_lang, value)] = (translated, detected)
                        fresh_data[str(key)] = item
                if len(fresh_data) != len(data):
                    TRANSLATE_CACHE_FILE.write_text(json.dumps(fresh_data, ensure_ascii=False, indent=2), encoding="utf-8")
        TRANSLATE_CACHE_LOADED = True


def prune_translation_cache() -> None:
    global TRANSLATE_CACHE_LOADED
    with TRANSLATE_CACHE_LOCK:
        fresh_cache: dict[tuple[str, str, str], tuple[str, str]] = {}
        fresh_data: dict[str, object] = {}
        if TRANSLATE_CACHE_FILE.exists():
            try:
                data = json.loads(TRANSLATE_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict):
                for key, item in data.items():
                    if not isinstance(item, dict) or not translation_cache_is_fresh(item.get("updated_at")):
                        continue
                    source_lang = str(item.get("source_lang") or "auto")
                    target_lang = str(item.get("target_lang") or "")
                    value = str(item.get("text") or "")
                    translated = str(item.get("translated") or "")
                    detected = str(item.get("detected") or source_lang)
                    if target_lang and value and translated:
                        fresh_cache[(source_lang, target_lang, value)] = (translated, detected)
                        fresh_data[str(key)] = item
        TRANSLATE_CACHE.clear()
        TRANSLATE_CACHE.update(fresh_cache)
        TRANSLATE_CACHE_LOADED = True
        try:
            if fresh_data:
                TRANSLATE_CACHE_FILE.write_text(json.dumps(fresh_data, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                TRANSLATE_CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def start_translation_cache_cleanup() -> None:
    load_translation_cache()

    def cleanup_loop() -> None:
        while True:
            time.sleep(TRANSLATE_CACHE_CLEANUP_INTERVAL_SECONDS)
            prune_translation_cache()

    threading.Thread(target=cleanup_loop, daemon=True).start()


def save_translation_cache_item(source_lang: str, target_lang: str, text: str, translated: str, detected: str) -> None:
    with TRANSLATE_CACHE_LOCK:
        try:
            if TRANSLATE_CACHE_FILE.exists():
                data = json.loads(TRANSLATE_CACHE_FILE.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            data[translation_cache_id(source_lang, target_lang, text)] = {
                "source_lang": source_lang,
                "target_lang": target_lang,
                "text": text,
                "translated": translated,
                "detected": detected,
                "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            TRANSLATE_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def cached_translation(text: str, target_lang: str, source_lang: str = "auto") -> tuple[str, str] | None:
    value = clean_text(text)
    if not value:
        return None
    load_translation_cache()
    return TRANSLATE_CACHE.get((source_lang, target_lang, value))


# Local-only record of the original Chinese we typed for each outgoing message,
# keyed by the (translated) text that was actually sent to the platform. This is
# never sent anywhere — it only lets our own clients (PC page + mobile app) show
# the original Chinese under our sent bubbles so we remember what we said. Unlike
# the translation cache it does not expire.
SENT_ORIGINALS_FILE = APP_DIR / "sent_originals.json"
SENT_ORIGINALS: dict[str, str] = {}
SENT_ORIGINALS_LOCK = threading.Lock()
SENT_ORIGINALS_LOADED = False


def _sent_original_key(translated: str) -> str:
    return " ".join(clean_text(translated).split())


def load_sent_originals() -> None:
    global SENT_ORIGINALS_LOADED
    if SENT_ORIGINALS_LOADED:
        return
    with SENT_ORIGINALS_LOCK:
        if SENT_ORIGINALS_LOADED:
            return
        if SENT_ORIGINALS_FILE.exists():
            try:
                data = json.loads(SENT_ORIGINALS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for key, value in data.items():
                        if isinstance(key, str) and isinstance(value, str):
                            SENT_ORIGINALS[key] = value
            except Exception:
                pass
        SENT_ORIGINALS_LOADED = True


def record_sent_original(translated: str, original: str) -> None:
    """Remember that `original` (Chinese) was sent to the platform as `translated`."""
    key = _sent_original_key(translated)
    orig = clean_text(original)
    if not key or not orig or key == _sent_original_key(orig):
        return
    load_sent_originals()
    with SENT_ORIGINALS_LOCK:
        if SENT_ORIGINALS.get(key) == orig:
            return
        SENT_ORIGINALS[key] = orig
        try:
            SENT_ORIGINALS_FILE.write_text(
                json.dumps(SENT_ORIGINALS, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass


def sent_original_for(translated: str) -> str:
    """Return the original Chinese we typed for a sent (translated) message, if any."""
    key = _sent_original_key(translated)
    if not key:
        return ""
    load_sent_originals()
    return SENT_ORIGINALS.get(key, "")


def seller_bubble_text(translated: str) -> str:
    """Text to show in my own sent bubble on my client: the original Chinese I
    typed, falling back to the sent text when no original was recorded. The buyer
    still receives the translated `translated` text — only my client shows Chinese."""
    return sent_original_for(translated) or translated


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
OPTION_TRANSLATIONS = {
    "выбор типа": "\u9009\u62e9\u7c7b\u578b",
    "я понимаю": "\u6211\u660e\u767d",
    "плюс (личный аккаунт, предоставленный продавцом пакет)": "PLUS\uff08\u4e2a\u4eba\u8d26\u53f7\uff0c\u5356\u5bb6\u63d0\u4f9b\u5957\u9910\uff09",
    "гарантия 7 дней (личный аккаунт предоставляется продавцом)": "7 \u5929\u8d28\u4fdd\uff08\u5356\u5bb6\u63d0\u4f9b\u4e2a\u4eba\u8d26\u53f7\uff09",
}
PROTECTED_TOKEN_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w.-]+\.\w+|(?=\b[A-Za-z0-9._:+/@#%=-]{6,}\b)(?=[A-Za-z0-9._:+/@#%=-]*[0-9@._:+/#%=-])[A-Za-z0-9._:+/@#%=-]+")
PRODUCT_BRANDS = [
    ("genspark", "Genspark", "GS", "https://www.google.com/s2/favicons?domain=genspark.ai&sz=64", "#ffffff"),
    ("manus", "Manus", "M", "/assets/brand-logos/manus.png", "#111111"),
    ("windsurf", "Windsurf", "W", "https://www.google.com/s2/favicons?domain=windsurf.com&sz=64", "#ffffff"),
    ("codeium", "Codeium", "C", "https://www.google.com/s2/favicons?domain=codeium.com&sz=64", "#ffffff"),
    ("cursor", "Cursor", "C", "https://www.google.com/s2/favicons?domain=cursor.com&sz=64", "#ffffff"),
    ("claude", "Claude", "C", "https://www.google.com/s2/favicons?domain=claude.ai&sz=64", "#ffffff"),
    ("openai", "OpenAI", "AI", "https://www.google.com/s2/favicons?domain=openai.com&sz=64", "#ffffff"),
    ("chatgpt", "ChatGPT", "GPT", "https://www.google.com/s2/favicons?domain=openai.com&sz=64", "#ffffff"),
    ("gemini", "Gemini", "G", "https://www.google.com/s2/favicons?domain=gemini.google.com&sz=64", "#ffffff"),
    ("krea", "Krea", "K", "https://www.google.com/s2/favicons?domain=krea.ai&sz=64", "#ffffff"),
    ("pimeyes", "PimEyes", "PE", "https://www.google.com/s2/favicons?domain=pimeyes.com&sz=64", "#ffffff"),
    ("elevenlabs", "ElevenLabs", "11", "https://www.google.com/s2/favicons?domain=elevenlabs.io&sz=64", "#ffffff"),
    ("sora", "Sora", "S", "https://www.google.com/s2/favicons?domain=openai.com&sz=64", "#ffffff"),
]
CHINESE_TEXT_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
FUNPAY_CHAT_BASE = "https://funpay.com"
RECENT_ORDER_DAYS = 30
RECENT_CHAT_DAYS = 30


def lang_label(lang: str) -> str:
    return LANG_LABELS.get(lang, lang or "auto")


def heuristic_language(text: str) -> str:
    value = clean_text(text)
    if not value:
        return ""
    if re.search(r"[\u0400-\u04ff]", value):
        return "ru"
    if CHINESE_TEXT_RE.search(value):
        return "zh-CN"
    latin = len(re.findall(r"[A-Za-z]", value))
    if latin >= 3:
        return "en"
    return ""


def has_chinese_text(text: str) -> bool:
    return bool(CHINESE_TEXT_RE.search(clean_text(text)))


def should_translate_outgoing_message(text: str, target_lang: str) -> bool:
    return target_lang not in {"zh", "zh-CN"} and has_chinese_text(text)


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
    load_translation_cache()
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
    save_translation_cache_item(source_lang, target_lang, value, result[0], result[1])
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
    cached = cached_translation(text, "zh-CN")
    pending = "0" if cached else "1"
    translated_text = cached[0] if cached else "&#32763;&#35793;&#20013;..."
    label = lang_label(cached[1] if cached else source_lang or "auto")
    translated_html = h(translated_text) if cached else translated_text
    return (
        f"<div class='translated-message' id='msg-{message_key}' data-pending='{pending}'>"
        f"<div class='translated-text'>{translated_html}</div>"
        f"<div class='original-inline'>&#21407;&#25991;&#65306;{h(text)}</div>"
        f"<div class='original-text' hidden>{h(text)}</div>"
        f"<div class='message-actions'>"
        f"<button class='toggle-original' type='button'>&#26597;&#30475;&#21407;&#25991;</button>"
        f"{save_common_phrase_button(text)}"
        f"<span class='translation-label'>{h(label)} → &#20013;&#25991;</span>"
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
        days = max(int(days), 1)
        rows = min(max(int(rows), 1), 50)
        page = max(int(page), 1)
        finish = dt.datetime.utcnow() + dt.timedelta(days=1)
        start = finish - dt.timedelta(days=days)
        body = {
            "date_start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_finish": finish.strftime("%Y-%m-%d %H:%M:%S"),
            "returned": 0,
            "rows": rows,
        }

        def fetch_page(page_number: int) -> dict[str, Any]:
            return self.post("/seller-sells/v2", json_body={**body, "page": page_number})

        data = fetch_page(page)
        pages = int(data.get("pages") or 1)
        all_rows = list(data.get("rows") or [])
        unique_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in all_rows:
            if not isinstance(item, dict):
                continue
            row_key = str(item.get("invoice_id") or item.get("id") or len(unique_rows))
            if row_key in seen:
                continue
            seen.add(row_key)
            unique_rows.append(item)
        unique_rows.sort(key=lambda item: sort_time(item.get("date_pay") or item.get("date")), reverse=True)
        total_rows = int(data.get("total_rows") or len(unique_rows))
        return {**data, "rows": unique_rows[:rows], "total_rows": total_rows, "pages": pages}

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

    def delete_content(self, product_id: int, content_id: int) -> None:
        params = {"ProductId": product_id, "ContentId": content_id, "token": self.token}
        r = self.http.get(API_BASE + "/product/content/delete", params=params)
        if r.status_code == 401:
            self._token = None
            params["token"] = self.token
            r = self.http.get(API_BASE + "/product/content/delete", params=params)
        r.raise_for_status()
        if not r.content:
            return
        data = r.json()
        if isinstance(data, dict) and int(data.get("retval") or 0) != 0:
            raise RuntimeError(data.get("retdesc") or data.get("desc") or f"Stock delete failed: {data}")

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


class GgselClient:
    def __init__(self) -> None:
        load_env()
        self.seller_id = os.getenv("GGSEL_SELLER_ID", "").strip()
        self.api_key = os.getenv("GGSEL_API_KEY", "").strip()
        self.api_base = os.getenv("GGSEL_API_BASE", "https://seller.ggsel.com/api_sellers/api").strip().rstrip("/")
        self.seller_cookie = os.getenv("GGSEL_SELLER_COOKIE", os.getenv("GGSEL_COOKIE", "")).strip()
        self.seller_office_base = os.getenv("GGSEL_SELLER_OFFICE_BASE", "https://seller.ggsel.com").strip().rstrip("/")
        default_seller_office_api_base = self.seller_office_base if self.seller_office_base.endswith("/api") else self.seller_office_base + "/api"
        self.seller_office_api_base = os.getenv("GGSEL_SELLER_OFFICE_API_BASE", default_seller_office_api_base).strip().rstrip("/")
        self.http = httpx.Client(timeout=35, headers={"Accept": "application/json", "User-Agent": "Digiseller Local Admin"})
        self._token: str | None = None
        self.valid_thru: str | None = None

    def configured(self) -> bool:
        return bool(self.seller_id and self.api_key)

    def login(self) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GGSEL_API_KEY is missing. Put it in .env")
        if not self.seller_id:
            raise RuntimeError("GGSEL_SELLER_ID is missing. Put it in .env")
        ts = int(time.time())
        sign = hashlib.sha256((self.api_key + str(ts)).encode()).hexdigest()
        payload = {"seller_id": int(self.seller_id), "timestamp": ts, "sign": sign}
        data = self.post("/apilogin", json_body=payload, auth=False)
        if not data.get("token"):
            raise RuntimeError(f"GGSEL login failed: {json.dumps(data, ensure_ascii=False)}")
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
        query = dict(params or {})
        if auth:
            query["token"] = self.token
        r = self.http.get(self.api_base + path, params=query)
        if auth and r.status_code == 401:
            self._token = None
            query["token"] = self.token
            r = self.http.get(self.api_base + path, params=query)
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL API HTTP {r.status_code}: {short(r.text, 400)}")
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"GGSEL API returned non-JSON from {path}: {short(r.text, 400)}")
        data = r.json()
        if isinstance(data, dict):
            retval = data.get("retval")
            if retval not in (None, 0, "0"):
                raise RuntimeError(data.get("retdesc") or data.get("desc") or f"GGSEL API retval={retval}")
        return data

    def post(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        params: dict[str, Any] | None = None,
    ) -> Any:
        query = dict(params or {})
        if auth:
            query["token"] = self.token
        r = self.http.post(self.api_base + path, params=query, json=json_body or {})
        if auth and r.status_code == 401:
            self._token = None
            query["token"] = self.token
            r = self.http.post(self.api_base + path, params=query, json=json_body or {})
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL API HTTP {r.status_code}: {short(r.text, 400)}")
        if not r.content:
            return {}
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"GGSEL API returned non-JSON from {path}: {short(r.text, 400)}")
        return r.json()

    def sales(self, top: int = 20) -> dict[str, Any]:
        data = self.get("/seller-last-sales", {"top": min(max(top, 1), 100)})
        return data if isinstance(data, dict) else {"sales": data}

    def purchase_info(self, invoice_id: int) -> dict[str, Any]:
        data = self.get(f"/purchase/info/{urllib.parse.quote(str(invoice_id), safe='')}")
        return data if isinstance(data, dict) else {}

    def chats(self, page_size: int = 20, page: int = 1, only_unread: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"pagesize": min(max(page_size, 1), 100), "page": max(page, 1)}
        if only_unread:
            params["filter_new"] = 1
        data = self.get("/debates/v2/chats", params)
        return data if isinstance(data, dict) else {"items": data}

    def mark_chat_read(self, order_id: int) -> None:
        self.post("/debates/v2/seen", params={"id_i": order_id})

    def chat_messages(self, order_id: int, count: int = 200) -> list[dict[str, Any]]:
        data = self.get("/debates/v2", {"id_i": order_id, "count": min(max(count, 1), 200)})
        if not isinstance(data, list):
            return []
        messages: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            message = dict(item)
            message["seller"] = 0 if int(message.get("buyer") or 0) else 1
            messages.append(message)
        messages.sort(key=lambda item: sort_time(item.get("date_written")))
        return messages

    def send_chat_message(self, order_id: int, message: str, uploads: list[UploadItem]) -> None:
        message, file_uploads = self.prepare_chat_uploads(message, uploads)
        if file_uploads:
            if self.seller_cookie:
                self.send_seller_office_chat_message(order_id, message, file_uploads)
                return
            unsupported = ", ".join(item.filename for item in file_uploads)
            raise RuntimeError(
                f"GGSEL_SELLER_COOKIE is not loaded from {APP_DIR / '.env'}. Restart the app after adding it, "
                f"or attach small text files that can be sent as message text: {unsupported}"
            )
        payload: dict[str, Any] = {"message": message}
        data = self.post("/debates/v2", json_body=payload, params={"id_i": order_id})
        if isinstance(data, dict) and data.get("retval") not in (None, 0, "0"):
            raise RuntimeError(data.get("retdesc") or data.get("desc") or f"GGSEL send failed: {data}")

    def prepare_chat_uploads(self, message: str, uploads: list[UploadItem]) -> tuple[str, list[UploadItem]]:
        if not uploads:
            return message, []
        text_parts: list[str] = []
        file_uploads: list[UploadItem] = []
        for item in uploads:
            text = self.text_upload_body(item)
            if text is None:
                file_uploads.append(item)
            else:
                text_parts.append(f"{item.filename}:\n{text}")
        if not text_parts:
            return message, file_uploads
        body = "\n\n".join(text_parts)
        if message:
            return f"{message.rstrip()}\n\n{body}", file_uploads
        return body, file_uploads

    def text_upload_body(self, item: UploadItem) -> str | None:
        content_type = (item.content_type or "").split(";", 1)[0].lower()
        guessed_type = (mimetypes.guess_type(item.filename)[0] or "").lower()
        suffix = Path(item.filename).suffix.lower()
        text_like = (
            content_type.startswith("text/")
            or guessed_type.startswith("text/")
            or content_type in GGSEL_TEXT_ATTACHMENT_TYPES
            or guessed_type in GGSEL_TEXT_ATTACHMENT_TYPES
            or suffix in GGSEL_TEXT_ATTACHMENT_SUFFIXES
        )
        if not text_like:
            return None
        if len(item.data) > GGSEL_INLINE_ATTACHMENT_MAX_BYTES:
            raise RuntimeError(
                f"GGSEL text attachment is too large to send as chat text: {item.filename} "
                f"(max {GGSEL_INLINE_ATTACHMENT_MAX_BYTES // 1024} KB)"
            )
        return item.data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()

    def upload_chat_files(self, uploads: list[UploadItem]) -> list[dict[str, str]]:
        if not uploads:
            return []
        files = [
            ("files[]", (item.filename, item.data, item.content_type or "application/octet-stream"))
            for item in uploads
        ]
        params = {"token": self.token, "lang": "en-US"}
        r = self.http.post(self.api_base + "/debates/v2/upload-preview", params=params, files=files)
        if r.status_code == 401:
            self._token = None
            params["token"] = self.token
            r = self.http.post(self.api_base + "/debates/v2/upload-preview", params=params, files=files)
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL file upload HTTP {r.status_code}: {short(r.text, 400)}")
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"GGSEL file upload returned non-JSON: {short(r.text, 400)}")
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"GGSEL file upload returned invalid data: {short(r.text, 400)}")
        if data.get("retval") not in (None, 0, "0"):
            raise RuntimeError(data.get("retdesc") or data.get("desc") or f"GGSEL file upload failed: {data}")
        uploaded: list[dict[str, str]] = []
        items = data.get("files")
        if not isinstance(items, list):
            raise RuntimeError(f"GGSEL file upload returned invalid files data: {short(r.text, 400)}")
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(f"GGSEL file upload returned invalid file item: {short(r.text, 400)}")
            if int(item.get("error_num") or 0) != 0:
                raise RuntimeError(item.get("error") or item.get("message") or "GGSEL file upload failed")
            uploaded.append(
                {
                    "newid": str(item.get("newid") or ""),
                    "name": str(item.get("name") or ""),
                    "type": str(item.get("type") or ""),
                }
            )
        if len(uploaded) != len(uploads):
            raise RuntimeError("GGSEL file upload did not return every file")
        return uploaded

    def send_seller_office_chat_message(self, order_id: int, message: str, uploads: list[UploadItem]) -> None:
        if not self.seller_cookie:
            raise RuntimeError("GGSEL attachment replies need GGSEL_SELLER_COOKIE in .env")
        unsupported = [item.filename for item in uploads if not self.is_image_upload(item)]
        if unsupported:
            raise RuntimeError("GGSEL attachment replies currently support images only: " + ", ".join(unsupported))
        items: list[tuple[str, str]] = [(message, self.chat_image_data_url(uploads[0]))]
        items.extend(("", self.chat_image_data_url(item)) for item in uploads[1:])
        conversation_ids = [str(order_id)]
        resolved_conversation_ids = False
        for text, image in items:
            payload = {"text": text, "image": image, "input_id": str(uuid.uuid4())}
            index = 0
            data: Any = {}
            last_404: RuntimeError | None = None
            while index < len(conversation_ids):
                conversation_id = conversation_ids[index]
                try:
                    data = self.seller_office_send_conversation_payload(conversation_id, payload)
                    conversation_ids = [conversation_id]
                    break
                except RuntimeError as exc:
                    if "HTTP 404" not in str(exc):
                        raise
                    last_404 = exc
                    if not resolved_conversation_ids:
                        for resolved_id in self.seller_office_conversation_ids(order_id):
                            if resolved_id not in conversation_ids:
                                conversation_ids.append(resolved_id)
                        resolved_conversation_ids = True
                    index += 1
            else:
                if last_404:
                    raise last_404
                raise RuntimeError(f"GGSEL seller office conversation was not found for order {order_id}")
            if isinstance(data, dict) and data.get("retval") not in (None, 0, "0"):
                raise RuntimeError(data.get("retdesc") or data.get("desc") or f"GGSEL seller office send failed: {data}")

    def seller_office_send_conversation_payload(self, conversation_id: str, payload: dict[str, str]) -> Any:
        return self.seller_office_request(
            "POST",
            f"/api_seller_office/v1/conversations/{urllib.parse.quote(conversation_id, safe='')}/messages",
            json_body=payload,
        )

    def seller_office_conversation_ids(self, order_id: int) -> list[str]:
        candidates: list[str] = []
        fallback_candidates: list[str] = []

        def add(target: list[str], value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in candidates and text not in fallback_candidates:
                target.append(text)

        for row in self.seller_office_conversation_rows({"page": 1, "limit": 100, "q": str(order_id)}):
            row_id = self.seller_office_conversation_row_id(row)
            if self.seller_office_conversation_matches_order(row, order_id):
                add(candidates, row_id)
            else:
                add(fallback_candidates, row_id)
        for page in range(1, 4):
            for row in self.seller_office_conversation_rows({"page": page, "limit": 100}):
                if self.seller_office_conversation_matches_order(row, order_id):
                    add(candidates, self.seller_office_conversation_row_id(row))
        candidates.extend(fallback_candidates)
        if candidates:
            return candidates
        raise RuntimeError(f"GGSEL seller office conversation was not found for order {order_id}")

    def seller_office_conversation_rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        data = self.seller_office_get("/api_seller_office/v1/conversations", params)
        rows = data.get("data") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError(f"GGSEL seller office conversation search returned invalid data: {data}")
        return [row for row in rows if isinstance(row, dict)]

    def seller_office_conversation_row_id(self, row: dict[str, Any]) -> str:
        return str(row.get("id") or "").strip()

    def seller_office_conversation_matches_order(self, row: dict[str, Any], order_id: int) -> bool:
        target = str(order_id)
        for key in ("order_id", "invoice_id", "id_i", "purchase_id", "order_number"):
            if str(row.get(key) or "") == target:
                return True
        for key in ("order", "invoice", "purchase"):
            value = row.get(key)
            if not isinstance(value, dict):
                continue
            for nested_key in ("id", "order_id", "invoice_id", "id_i", "number"):
                if str(value.get(nested_key) or "") == target:
                    return True
        return False

    def chat_image_data_url(self, item: UploadItem) -> str:
        content_type = self.image_upload_content_type(item)
        return f"data:{content_type};base64,{base64.b64encode(item.data).decode('ascii')}"

    def is_image_upload(self, item: UploadItem) -> bool:
        content_type = (item.content_type or "").split(";", 1)[0].lower()
        return content_type.startswith("image/") or looks_like_image_name(item.filename)

    def image_upload_content_type(self, item: UploadItem) -> str:
        content_type = (item.content_type or "").split(";", 1)[0].lower()
        if content_type.startswith("image/"):
            return content_type
        guessed_type = mimetypes.guess_type(item.filename)[0]
        if guessed_type and guessed_type.startswith("image/"):
            return guessed_type
        return "image/png"

    def session_get(self, path: str) -> Any:
        session_id = urllib.parse.quote(self.token, safe="")
        r = self.http.get(self.api_base + path.format(session_id=session_id))
        if r.status_code == 401:
            self._token = None
            session_id = urllib.parse.quote(self.token, safe="")
            r = self.http.get(self.api_base + path.format(session_id=session_id))
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL API HTTP {r.status_code}: {short(r.text, 400)}")
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"GGSEL API returned non-JSON from {path}: {short(r.text, 400)}")
        return r.json()

    def online_setting(self) -> dict[str, Any]:
        data = self.session_get("/getonlinesetting/{session_id}")
        return data if isinstance(data, dict) else {"raw": data}

    def set_online(self) -> dict[str, Any]:
        try:
            value = int(os.getenv("GGSEL_ONLINE_VALUE", os.getenv("DIGISELLER_ONLINE_VALUE", "1")) or "1")
        except ValueError:
            value = 1
        data = self.session_get(f"/setonlinesetting/{{session_id}}/{value}")
        if isinstance(data, dict) and int(data.get("retval") or 0) != 0:
            raise RuntimeError(data.get("retdesc") or data.get("desc") or f"GGSEL setonlinesetting failed: {data}")
        return data if isinstance(data, dict) else {"raw": data}

    def seller_online_status(self) -> dict[str, Any]:
        configured = os.getenv("GGSEL_ONLINE_VERIFY_TYPE", os.getenv("DIGISELLER_ONLINE_VERIFY_TYPE", "seller")).strip() or "seller"
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
                errors.append(f"{corr_type}: {data.get('retdesc') or data.get('desc') or data}")
            else:
                return {"raw": data, "corr_type": corr_type}
        raise RuntimeError("; ".join(errors) or "GGSEL getonlinestatus failed")

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
                errors.append(data.get("retdesc") or data.get("desc") or f"GGSEL checknewchats failed: {data}")
            else:
                return {"raw": data}
        raise RuntimeError("; ".join(errors))

    def reviews(self, count: int = 20, page: int = 1, review_type: str = "all") -> dict[str, Any]:
        data = self.get("/reviews", {"type": review_type, "page": max(page, 1), "count": min(max(count, 1), 100)})
        return data if isinstance(data, dict) else {"reviews": data}

    def public_product_title(self, product_id: Any) -> str:
        url = self.product_url(product_id)
        if not url:
            return ""
        r = self.http.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0",
            },
            follow_redirects=True,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL product page HTTP {r.status_code}: {short(r.text, 400)}")
        text = r.text
        for pattern in (
            r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)",
            r"<title[^>]*>(.*?)</title>",
            r"<h1[^>]*>(.*?)</h1>",
        ):
            match = re.search(pattern, text, flags=re.I | re.S)
            if match:
                title = clean_text(match.group(1))
                title = re.sub(r"\s*[|—-]\s*ggsel\s*$", "", title, flags=re.I).strip()
                if title and not looks_like_opaque_product_ref(title):
                    return title
        return ""

    def product_url(self, product_id: Any) -> str:
        if not product_id:
            return ""
        return f"https://ggsel.net/en/catalog/product/{urllib.parse.quote(str(product_id))}"

    def seller_office_configured(self) -> bool:
        return bool(self.seller_cookie)

    def seller_office_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.seller_cookie:
            raise RuntimeError("GGSEL_SELLER_COOKIE is missing. Put the seller login cookie in .env to send stock items.")
        headers = {
            "Accept": "application/json",
            "User-Agent": "Digiseller Local Admin",
            "Cookie": self.seller_cookie,
            "Origin": self.seller_office_base,
            "Referer": f"{self.seller_office_base}/en/offers",
        }
        r = self.http.request(
            method,
            self.seller_office_api_base + path,
            params=params or {},
            json=json_body,
            headers=headers,
            follow_redirects=True,
        )
        if r.status_code in (401, 403):
            raise RuntimeError("GGSEL seller cookie is missing or expired")
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL seller office HTTP {r.status_code}: {short(r.text, 400)}")
        if not r.content:
            return {}
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"GGSEL seller office returned non-JSON from {path}: {short(r.text, 400)}")
        return r.json()

    def seller_office_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.seller_office_request("GET", path, params=params)

    def seller_office_dashboard(self, start_date: dt.date, end_date: dt.date, offer_ids: list[int] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "date_start": start_date.isoformat(),
            "date_end": end_date.isoformat(),
            "scope": "active",
        }
        if offer_ids:
            params["offer_ids[]"] = offer_ids
        data = self.seller_office_get("/api_seller_office/v1/dashboard", params)
        dashboard = data.get("data") if isinstance(data, dict) else data
        return dashboard if isinstance(dashboard, dict) else {}

    def seller_office_delete(self, path: str) -> None:
        self.seller_office_request("DELETE", path)

    def seller_office_fetch_text(self, url_or_path: str) -> str:
        if not self.seller_cookie:
            raise RuntimeError("GGSEL_SELLER_COOKIE is missing. Put the seller login cookie in .env to send stock items.")
        target = url_or_path.strip()
        if not target:
            return ""
        if target.startswith("//"):
            target = "https:" + target
        elif target.startswith("/"):
            target = self.seller_office_base + target
        elif not re.match(r"https?://", target, flags=re.I):
            target = self.seller_office_base + "/" + target.lstrip("/")
        headers = {
            "Accept": "text/plain, application/octet-stream, application/json;q=0.9, */*;q=0.8",
            "User-Agent": "Digiseller Local Admin",
            "Referer": f"{self.seller_office_base}/en/offers",
        }
        if urllib.parse.urlparse(target).netloc == urllib.parse.urlparse(self.seller_office_base).netloc:
            headers["Cookie"] = self.seller_cookie
        r = self.http.get(target, headers=headers, follow_redirects=True)
        content_length = r.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1_000_000:
                    raise RuntimeError("Stock file is too large to send as text")
            except ValueError:
                pass
        if r.status_code in (401, 403):
            raise RuntimeError("GGSEL seller cookie is missing or expired")
        if r.status_code >= 400:
            raise RuntimeError(f"GGSEL seller office file HTTP {r.status_code}: {short(r.text, 400)}")
        content = r.content
        if len(content) > 1_000_000:
            raise RuntimeError("Stock file is too large to send as text")
        if not content:
            return ""
        content_type = r.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                for key in ("content", "text", "value", "data"):
                    value = data.get(key)
                    if isinstance(value, str) and clean_text(value):
                        return clean_text(value)
        if b"\x00" in content[:2048]:
            raise RuntimeError("Stock file is not a text file")
        for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
            try:
                return clean_text(content.decode(encoding))
            except UnicodeDecodeError:
                continue
        return clean_text(content.decode("utf-8", errors="replace"))

    def seller_office_offer_by_ggsel_id(self, ggsel_product_id: Any) -> dict[str, Any]:
        data = self.seller_office_get(f"/api/v1/offers/ggsel/{urllib.parse.quote(str(ggsel_product_id), safe='')}")
        offer = data.get("data") if isinstance(data, dict) else None
        return offer if isinstance(offer, dict) else {}

    def seller_office_search_offer(self, query: str) -> dict[str, Any]:
        search = clean_text(query)
        if not search:
            return {}
        data = self.seller_office_get("/api/v1/offers", {"search": search, "page": 1, "limit": 20})
        rows = data.get("data") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return {}
        normalized_search = search.casefold()
        best = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = clean_text(row.get("title"))
            normalized_title = title.casefold()
            if normalized_title == normalized_search:
                return row
            if not best and (normalized_search in normalized_title or normalized_title in normalized_search):
                best = row
        return best if isinstance(best, dict) else {}

    def seller_office_offer_products(self, offer_id: int, page: int = 1, limit: int = 20) -> dict[str, Any]:
        data = self.seller_office_get(f"/api/v1/offers/{offer_id}/products", {"page": page, "limit": limit})
        return data if isinstance(data, dict) else {}

    def seller_office_delete_product(self, offer_id: int, product_id: int) -> None:
        self.seller_office_delete(f"/api_seller_office/v1/offers/{offer_id}/products/{product_id}")


class FunPayClient:
    def __init__(self) -> None:
        load_env()
        self.golden_key = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()
        self.http = httpx.Client(
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            cookies={"golden_key": self.golden_key} if self.golden_key else {},
            follow_redirects=True,
        )

    def configured(self) -> bool:
        return bool(self.golden_key)

    def ensure_configured(self) -> None:
        if not self.golden_key:
            raise RuntimeError("FUNPAY_GOLDEN_KEY is missing. Put it in .env")

    def auth_expired(self, response: httpx.Response) -> bool:
        text = response.text[:4000]
        return (
            "account/login" in str(response.url)
            or "Log In / FunPay" in text
            or bool(re.search(r'name=["\']login["\']', text, re.I) and re.search(r'name=["\']password["\']', text, re.I))
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self.ensure_configured()
        url = path if path.startswith("http") else FUNPAY_CHAT_BASE + path
        r = self.http.get(url, params=params or {})
        if self.auth_expired(r):
            raise RuntimeError("FunPay golden_key is missing or expired")
        if r.status_code >= 400:
            raise RuntimeError(f"FunPay HTTP {r.status_code}: {short(r.text, 400)}")
        return r

    def post(self, path: str, data: dict[str, Any], referer: str) -> Any:
        self.ensure_configured()
        r = self.http.post(
            FUNPAY_CHAT_BASE + path,
            data=data,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": FUNPAY_CHAT_BASE + referer if referer.startswith("/") else referer,
            },
        )
        if self.auth_expired(r):
            raise RuntimeError("FunPay golden_key is missing or expired")
        if r.status_code >= 400:
            raise RuntimeError(f"FunPay HTTP {r.status_code}: {short(r.text, 400)}")
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"FunPay returned non-JSON from {path}: {short(r.text, 400)}")
        return r.json()

    def post_form(self, path: str, data: Any, referer: str) -> dict[str, Any]:
        self.ensure_configured()
        encoded_data = urllib.parse.urlencode(data, doseq=True) if not isinstance(data, (bytes, bytearray, str)) else data
        r = self.http.post(
            FUNPAY_CHAT_BASE + path,
            content=encoded_data,
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": FUNPAY_CHAT_BASE + referer if referer.startswith("/") else referer,
            },
        )
        if self.auth_expired(r):
            raise RuntimeError("FunPay golden_key is missing or expired")
        if r.status_code >= 400:
            raise RuntimeError(f"FunPay HTTP {r.status_code}: {short(r.text, 400)}")
        try:
            parsed = r.json()
        except ValueError:
            raise RuntimeError(f"FunPay returned non-JSON from {path}: {short(r.text, 400)}")
        return parsed if isinstance(parsed, dict) else {}

    def upload_chat_files(self, uploads: list[UploadItem], referer: str) -> list[str]:
        file_ids: list[str] = []
        for item in uploads:
            r = self.http.post(
                FUNPAY_CHAT_BASE + "/en/file/addChatImage",
                files={"file": (item.filename, item.data, item.content_type or "application/octet-stream")},
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": FUNPAY_CHAT_BASE + referer if referer.startswith("/") else referer,
                },
            )
            if r.status_code >= 400:
                raise RuntimeError(f"FunPay upload HTTP {r.status_code}: {short(r.text, 400)}")
            data = r.json()
            file_id = str(data.get("fileId") or data.get("id") or "")
            if not file_id:
                raise RuntimeError(f"FunPay upload did not return fileId: {short(r.text, 400)}")
            file_ids.append(file_id)
        return file_ids

    def app_data(self, html_text: str) -> dict[str, Any]:
        match = re.search(r"""data-app-data\s*=\s*(["'])(.*?)\1""", html_text, re.S | re.I)
        if not match:
            return {}
        try:
            data = json.loads(html.unescape(match.group(2)))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def tag_attrs(self, tag: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for match in re.finditer(r"""([A-Za-z0-9_:-]+)\s*=\s*(["'])(.*?)\2""", tag):
            attrs[match.group(1).lower()] = html.unescape(match.group(3))
        return attrs

    def first_text(self, html_text: str, class_name: str) -> str:
        match = re.search(
            rf'<[^>]*class="[^"]*\b{re.escape(class_name)}\b[^"]*"[^>]*>(.*?)</[^>]+>',
            html_text,
            re.S | re.I,
        )
        return clean_text(match.group(1) if match else "")

    def chat_page(self, node_id: int | None = None) -> str:
        params = {"node": str(node_id)} if node_id else None
        return self.get("/en/chat/", params=params).text

    def chat_product_from_page(self, page: str) -> str:
        match = re.search(r'<a\b[^>]*href="https://funpay\.com/en/lots/offer\?id=\d+"[^>]*>(.*?)</a>', page, re.S | re.I)
        return clean_text(match.group(1) if match else "")

    def chat_product(self, node_id: int) -> str:
        return self.chat_product_from_page(self.chat_page(node_id))

    def paid_order_id_from_message(self, text: Any) -> str:
        order_match = re.search(r"\border\s+#([A-Za-z0-9]+)", clean_text(text), re.I)
        return order_match.group(1) if order_match else ""

    def recent_order_dates(self, limit: int = 100) -> dict[str, str]:
        page = self.get("/orders/trade").text
        dates: dict[str, str] = {}
        pattern = re.compile(r'<a\b[^>]*class="[^"]*\btc-item\b[^"]*"[^>]*>.*?</a>', re.S | re.I)
        for item in pattern.findall(page):
            order_id = self.first_text(item, "tc-order").lstrip("#").strip()
            order_date = self.first_text(item, "tc-date-time")
            timestamp = sort_time(order_date)
            if order_id and timestamp:
                dates[order_id] = dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            if len(dates) >= limit:
                break
        return dates

    def latest_order_date_for_messages(self, messages: list[dict[str, Any]], order_dates: dict[str, str]) -> str:
        for message in reversed(messages):
            order_id = self.paid_order_id_from_message(message.get("message"))
            if order_id and order_dates.get(order_id):
                return order_dates[order_id]
        return ""

    def message_sort_date(self, message_date: Any, order_date: Any) -> str:
        text = str(message_date or "").strip()
        if not text:
            return ""
        if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
            return full_time_from_date(order_date, text)
        return text

    def csrf_token_from_page(self, page: str) -> str:
        data = self.app_data(page)
        csrf_token = str(data.get("csrf-token") or data.get("csrf_token") or "")
        if csrf_token:
            return csrf_token
        for pattern in (
            r'<meta\b[^>]*(?:name|property)=["\']csrf-token["\'][^>]*\bcontent=["\']([^"\']+)["\']',
            r'<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*(?:name|property)=["\']csrf-token["\']',
            r'<input\b[^>]*\bname=["\']csrf_token["\'][^>]*\bvalue=["\']([^"\']+)["\']',
            r'<input\b[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bname=["\']csrf_token["\']',
            r'["\']csrf-token["\']\s*:\s*["\']([^"\']+)["\']',
            r'["\']csrf_token["\']\s*:\s*["\']([^"\']+)["\']',
        ):
            match = re.search(pattern, page, re.I)
            if match:
                return html.unescape(match.group(1))
        return ""

    def chat_node_name_from_page(self, page: str) -> str:
        fallback = ""
        for match in re.finditer(r"<[^>]+\bdata-name\s*=\s*(['\"])(.*?)\1[^>]*>", page, re.I | re.S):
            attrs = self.tag_attrs(match.group(0))
            data_name = attrs.get("data-name") or ""
            if not data_name:
                continue
            class_name = attrs.get("class") or ""
            if "chat" in class_name.split():
                return data_name
            if not fallback:
                fallback = data_name
        return fallback

    def chat_attrs_from_page(self, page: str) -> dict[str, str]:
        for match in re.finditer(r"<div\b[^>]*>", page, re.I):
            attrs = self.tag_attrs(match.group(0))
            if "chat" in (attrs.get("class") or "").split():
                return attrs
        return {}

    def account_home_page(self) -> str:
        return self.get("/en/").text

    def account_user_id(self) -> int:
        page = self.account_home_page()
        app_data = self.app_data(page)
        user_id = int(app_data.get("userId") or 0)
        if user_id:
            return user_id
        match = re.search(r'(?:https?:\/\/(?:www\.)?funpay\.com)?\/(?:en\/)?users\/(\d+)\/?', page, re.I)
        if match:
            return int(match.group(1))
        raise RuntimeError("Could not determine FunPay account user ID")

    def account_profile_page(self, user_id: int | None = None) -> str:
        return self.get(f"/en/users/{user_id or self.account_user_id()}/").text

    def active_offer_categories(self) -> list[dict[str, Any]]:
        user_id = self.account_user_id()
        page = self.account_profile_page(user_id)
        node_ids: list[int] = []
        seen: set[int] = set()
        for match in re.finditer(r'(?:https?:\/\/(?:www\.)?funpay\.com)?\/(?:en\/)?lots\/(\d+)\/?', page, re.I):
            node_id = int(match.group(1))
            if node_id not in seen:
                seen.add(node_id)
                node_ids.append(node_id)
        if not node_ids:
            raise RuntimeError("No active FunPay offer categories found")
        game_ids = self.offer_category_game_ids_from_page(page)
        titles = self.offer_category_titles_from_page(page)
        return [
            {
                "node_id": node_id,
                "game_id": int(game_ids.get(node_id) or 0),
                "product_name": titles.get(node_id) or f"Category {node_id}",
            }
            for node_id in node_ids
        ]

    def active_offer_node_ids(self) -> list[int]:
        return [item["node_id"] for item in self.active_offer_categories()]

    def active_offer_titles(self) -> dict[int, str]:
        return {
            int(item.get("node_id") or 0): str(item.get("product_name") or "")
            for item in self.active_offer_categories()
            if int(item.get("node_id") or 0)
        }

    def offer_category_titles_from_page(self, page: str) -> dict[int, str]:
        titles: dict[int, str] = {}
        pattern = re.compile(
            r'<a\b([^>]*)href=["\'](?:https?:\/\/(?:www\.)?funpay\.com)?\/(?:en\/)?lots\/(\d+)\/?["\']([^>]*)>(.*?)</a>',
            re.S | re.I,
        )
        for match in pattern.finditer(page):
            node_id = int(match.group(2))
            attrs = self.tag_attrs("<a " + match.group(1) + " " + match.group(3) + ">")
            title = clean_text(attrs.get("title") or match.group(4))
            if not title or title == str(node_id):
                block = page[max(0, match.start() - 800) : min(len(page), match.end() + 800)]
                for class_name in ("tc-title", "lot-title", "offer-title", "media-heading", "category-title"):
                    title = self.first_text(block, class_name)
                    if title:
                        break
            if title:
                titles[node_id] = title
        return titles

    def offer_category_game_ids_from_page(self, page: str) -> dict[int, int]:
        game_titles = list(
            re.finditer(
                r'<[^>]*class=["\'][^"\']*\bgame-title\b[^"\']*["\'][^>]*>',
                page,
                re.I,
            )
        )
        game_ids: dict[int, int] = {}
        for index, game_title in enumerate(game_titles):
            attrs = self.tag_attrs(game_title.group(0))
            try:
                game_id = int(attrs.get("data-id") or 0)
            except (TypeError, ValueError):
                continue
            if not game_id:
                continue
            end = game_titles[index + 1].start() if index + 1 < len(game_titles) else len(page)
            block = page[game_title.end() : end]
            for match in re.finditer(r'(?:https?:\/\/(?:www\.)?funpay\.com)?\/(?:en\/)?lots\/(\d+)\/?', block, re.I):
                game_ids[int(match.group(1))] = game_id
        return game_ids

    def offer_category_game_id(self, node_id: int) -> int:
        page = self.get(f"/en/lots/{node_id}/trade").text
        for pattern in [
            r'\bdata-game=["\'](\d+)["\']',
            r'\bdata-game-id=["\'](\d+)["\']',
            r'\bdata-game_id=["\'](\d+)["\']',
            r'\bname=["\']game_id["\'][^>]*\bvalue=["\'](\d+)["\']',
            r'\bvalue=["\'](\d+)["\'][^>]*\bname=["\']game_id["\']',
            r'["\']game_id["\']\s*:\s*["\']?(\d+)',
            r'["\']gameId["\']\s*:\s*["\']?(\d+)',
        ]:
            match = re.search(pattern, page, re.I)
            if match:
                return int(match.group(1))
        game_ids = self.offer_category_game_ids_from_page(page)
        if node_id in game_ids:
            return game_ids[node_id]
        game_ids = self.offer_category_game_ids_from_page(self.account_profile_page())
        if node_id in game_ids:
            return game_ids[node_id]
        game_ids = self.offer_category_game_ids_from_page(self.account_home_page())
        if node_id in game_ids:
            return game_ids[node_id]
        raise RuntimeError(f"Could not determine FunPay game ID for category {node_id}")

    def raise_offer_category(self, game_id: int, node_id: int, node_ids: list[int] | None = None) -> dict[str, Any]:
        fields: list[tuple[str, str]] = [("game_id", str(game_id)), ("node_id", str(node_id))]
        for selected_node_id in node_ids or []:
            fields.append(("node_ids[]", str(selected_node_id)))
        return self.post_form("/lots/raise", fields, f"/en/lots/{node_id}/trade")

    def raise_modal_node_ids(self, data: dict[str, Any]) -> list[int]:
        modal = data.get("modal")
        if not isinstance(modal, str):
            return []
        node_ids: list[int] = []
        seen: set[int] = set()
        for match in re.finditer(r'<input\b[^>]*>', modal, re.I):
            tag = match.group(0)
            if re.search(r'\bdisabled(?:\s|=|>|/)', tag, re.I):
                continue
            attrs = self.tag_attrs(tag)
            input_type = attrs.get("type", "").lower()
            input_name = attrs.get("name", "")
            if input_type and input_type != "checkbox":
                continue
            if input_name and input_name != "node_ids[]":
                continue
            try:
                node_id = int(attrs.get("value") or 0)
            except (TypeError, ValueError):
                continue
            if not node_id:
                continue
            if node_id not in seen:
                seen.add(node_id)
                node_ids.append(node_id)
        return node_ids

    def raise_response_message(self, data: dict[str, Any]) -> str:
        candidates = [
            data.get("message"),
            data.get("msg"),
            data.get("error_description"),
            data.get("error_message"),
            data.get("error") if isinstance(data.get("error"), (str, dict)) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                message = ", ".join(str(value) for value in candidate.values() if value)
            else:
                message = str(candidate or "")
            message = message.strip()
            if message:
                return message
        return ""

    def classify_raise_response(self, node_id: int, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data.get("modal"), str) and data.get("modal", "").strip():
            return {"node_id": node_id, "status": "failed", "message": "FunPay asked to choose categories"}
        message = self.raise_response_message(data)
        failed = data.get("success") is False or data.get("status") == "error" or bool(data.get("error"))
        if not failed:
            return {"node_id": node_id, "status": "success", "message": message or "Offers boosted"}
        cooldown = not message or bool(re.search(r"уже|ранее|час|минут|секунд|подня|повтор|огранич|cooldown|later|wait|raised|ожид", message, re.I))
        return {"node_id": node_id, "status": "skipped" if cooldown else "failed", "message": message or "Already boosted or time-limited"}

    def quiet_boost_failure(self, message: str) -> bool:
        return bool(re.search(r"Could not determine FunPay game ID|No active FunPay offer categories|404|403|not found|forbidden", message, re.I))

    def raise_node_offers(
        self,
        node_id: int,
        node_ids: list[int] | None = None,
        game_id: int | None = None,
    ) -> tuple[dict[str, Any], list[int]]:
        try:
            selected_node_ids = node_ids or []
            game_id = game_id or self.offer_category_game_id(node_id)
            data = self.raise_offer_category(game_id, node_id, selected_node_ids)
            if selected_node_ids:
                return self.classify_raise_response(node_id, data), selected_node_ids
            modal_node_ids = self.raise_modal_node_ids(data)
            if not modal_node_ids and not (isinstance(data.get("modal"), str) and data.get("modal", "").strip()):
                return self.classify_raise_response(node_id, data), []
            if not modal_node_ids:
                modal_node_ids = [node_id]
            confirmed = self.raise_offer_category(game_id, node_id, modal_node_ids)
            if self.raise_modal_node_ids(confirmed):
                return {"node_id": node_id, "status": "failed", "message": "FunPay asked to choose categories again"}, []
            result = self.classify_raise_response(node_id, confirmed)
            if result.get("status") == "skipped":
                result = {**result, "status": "success", "message": "Offers boosted"}
            return result, modal_node_ids
        except Exception as exc:
            message = str(exc)
            status = "skipped" if self.quiet_boost_failure(message) else "failed"
            return {"node_id": node_id, "status": status, "message": message}, []

    def boost_all_active_offers(self) -> dict[str, Any]:
        categories = self.active_offer_categories()
        game_groups: dict[int, list[int]] = {}
        unknown_game_node_ids: list[int] = []
        product_names: dict[int, str] = {}
        for category in categories:
            node_id = int(category.get("node_id") or 0)
            game_id = int(category.get("game_id") or 0)
            if not node_id:
                continue
            product_names[node_id] = str(category.get("product_name") or "")
            if game_id:
                game_groups.setdefault(game_id, []).append(node_id)
            else:
                unknown_game_node_ids.append(node_id)
        runs: list[tuple[int, list[int], int | None]] = [
            (node_ids[0], node_ids, game_id) for game_id, node_ids in game_groups.items() if node_ids
        ]
        runs.extend((node_id, [], None) for node_id in unknown_game_node_ids)
        results: list[dict[str, Any]] = []
        raised_together: set[int] = set()
        for index, (node_id, selected_node_ids, game_id) in enumerate(runs):
            if node_id in raised_together:
                results.append(
                    {
                        "node_id": node_id,
                        "product_name": product_names.get(node_id) or "",
                        "status": "success",
                        "message": "Boosted with another category",
                    }
                )
                continue
            result, co_raised = self.raise_node_offers(node_id, selected_node_ids, game_id)
            result = {**result, "product_name": product_names.get(node_id) or ""}
            results.append(result)
            boosted_node_ids = [node_id, *[item for item in co_raised if item != node_id]]
            if result.get("status") == "success":
                remember_funpay_boost_cooldown(
                    boosted_node_ids,
                    time.time() + FUNPAY_BOOST_INTERVAL_SECONDS,
                    product_names,
                )
                raised_together.update(item for item in boosted_node_ids if item != node_id)
                for boosted_node_id in boosted_node_ids:
                    if boosted_node_id != node_id:
                        results.append(
                            {
                                "node_id": boosted_node_id,
                                "product_name": product_names.get(boosted_node_id) or "",
                                "status": "success",
                                "message": "Boosted with another category",
                            }
                        )
            else:
                for failed_node_id in boosted_node_ids:
                    record_funpay_boost_attempt(
                        failed_node_id,
                        str(result.get("status") or "failed"),
                        str(result.get("message") or "-"),
                        product_name=product_names.get(failed_node_id) or "",
                    )
            if index < len(runs) - 1:
                time.sleep(0.7)
        return {
            "status": "completed",
            "total": len(results),
            "success": sum(1 for item in results if item.get("status") == "success"),
            "skipped": sum(1 for item in results if item.get("status") == "skipped"),
            "failed": sum(1 for item in results if item.get("status") == "failed"),
            "results": results,
        }

    def chats(self, limit: int = 50) -> list[dict[str, Any]]:
        page = self.chat_page()
        rows: list[dict[str, Any]] = []
        pattern = re.compile(r'<a\b[^>]*class="[^"]*\bcontact-item\b[^"]*"[^>]*>.*?</a>', re.S | re.I)
        for item in pattern.findall(page):
            attrs = self.tag_attrs(item)
            node_id = int(attrs.get("data-id") or 0)
            if not node_id:
                continue
            node_msg = int(attrs.get("data-node-msg") or 0)
            user_msg = int(attrs.get("data-user-msg") or 0)
            rows.append(
                {
                    "node_id": node_id,
                    "name": self.first_text(item, "media-user-name") or f"FunPay-{node_id}",
                    "message": self.first_text(item, "contact-item-message"),
                    "last_date": self.first_text(item, "contact-item-time"),
                    "node_msg": node_msg,
                    "user_msg": user_msg,
                    "cnt_new": 1 if node_msg and user_msg and node_msg != user_msg else 0,
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def recent_orders(self, order_days: int = RECENT_ORDER_DAYS, chat_days: int = RECENT_CHAT_DAYS, limit: int = 50) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_orders: set[str] = set()
        try:
            order_dates = self.recent_order_dates(limit=limit)
        except Exception:
            order_dates = {}
        for chat in self.chats(limit=limit):
            if not is_recent_time(chat.get("last_date"), chat_days):
                continue
            node_id = int(chat.get("node_id") or 0)
            if not node_id:
                continue
            messages = self.chat_messages(node_id)
            product = self.chat_product(node_id)
            for message in messages:
                text = clean_text(message.get("message"))
                if not is_recent_time(message.get("date_written"), order_days):
                    continue
                order_match = re.search(r"\border\s+#([A-Za-z0-9]+)", text, re.I)
                if not order_match or "has paid for" not in text.lower():
                    continue
                order_id = order_match.group(1)
                if order_id in seen_orders:
                    continue
                seen_orders.add(order_id)
                order_date = order_dates.get(order_id) or self.message_sort_date(message.get("date_written"), message.get("date_written"))
                rows.append(
                    {
                        "source": "FunPay",
                        "platform": "funpay",
                        "date_pay": order_date or message.get("date_written") or chat.get("last_date"),
                        "invoice_id": order_id,
                        "chat_node_id": node_id,
                        "email": chat.get("name") or f"FunPay-{node_id}",
                        "product_id": "",
                        "product_name": product or chat.get("message") or "FunPay order",
                        "amount_in": "",
                        "amount_currency": "",
                        "partner_id": "-",
                        "referer": "funpay.com",
                    }
                )
        rows.sort(key=lambda item: sort_time(item.get("date_pay")), reverse=True)
        return rows

    def chat_messages_from_page(self, node_id: int, page: str) -> list[dict[str, Any]]:
        app_data = self.app_data(page)
        my_user_id = int(app_data.get("userId") or 0)
        messages: list[dict[str, Any]] = []
        matches = list(re.finditer(r'<div\b[^>]*class="[^"]*\bchat-msg-item\b[^"]*"[^>]*id="message-(\d+)"[^>]*>', page, re.I))
        last_author = ""
        last_seller = 0
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else page.find("</form>", match.end())
            if end < 0:
                end = len(page)
            block = page[match.start() : end]
            author = last_author
            seller = last_seller
            author_match = re.search(r'<a\b[^>]*href="https://funpay\.com/en/users/(\d+)/"[^>]*class="[^"]*\bchat-msg-author-link\b[^"]*"[^>]*>(.*?)</a>', block, re.S | re.I)
            if author_match:
                author = clean_text(author_match.group(2))
                seller = 1 if my_user_id and int(author_match.group(1)) == my_user_id else 0
                last_author = author
                last_seller = seller
            elif "chat-msg-with-head" in match.group(0):
                media = self.first_text(block, "media-user-name")
                if media:
                    author = media.replace("notification", "").replace("auto-reply", "").strip() or "FunPay"
                    seller = 1 if author == "FunPay" else 0
                    last_author = author
                    last_seller = seller
            date_match = re.search(r'<div\b[^>]*class="[^"]*\bchat-msg-date\b[^"]*"[^>]*title="([^"]*)"[^>]*>(.*?)</div>', block, re.S | re.I)
            date_written = clean_text(date_match.group(1) if date_match else "")
            if not date_written and date_match:
                date_written = clean_text(date_match.group(2))
            text = self.first_text(block, "chat-msg-text") or self.first_text(block, "chat-msg-body")
            messages.append(
                {
                    "id": int(match.group(1)),
                    "seller": seller,
                    "author": author or "FunPay",
                    "message": text,
                    "date_written": date_written,
                    "platform": "funpay",
                }
            )
        return messages

    def chat_messages(self, node_id: int) -> list[dict[str, Any]]:
        return self.chat_messages_from_page(node_id, self.chat_page(node_id))

    def send_chat_payload(self, node_id: int, page: str, content: str, image_id: str = "") -> None:
        csrf_token = self.csrf_token_from_page(page) or self.csrf_token_from_page(self.account_home_page())
        attrs = self.chat_attrs_from_page(page)
        node_name = attrs.get("data-name") or self.chat_node_name_from_page(page) or str(node_id)
        if not csrf_token:
            raise RuntimeError("FunPay page did not expose a CSRF token")
        ids = [int(value) for value in re.findall(r'id="message-(\d+)"', page)]
        request_data = {
            "node": node_name,
            "last_message": max(ids) if ids else -1,
            "content": content,
        }
        for key in ("data-compact", "data-show_avatar"):
            if attrs.get(key):
                request_data[key[5:]] = attrs[key]
        request = {
            "action": "chat_message",
            "data": request_data,
        }
        if image_id:
            request["data"]["image_id"] = image_id
        objects = [
            {
                "type": "chat_node",
                "id": node_name,
                "tag": "00000000",
                "data": {"node": node_name, "last_message": -1, "content": ""},
            }
        ]
        response = self.post(
            "/runner/",
            {
                "objects": json.dumps(objects, ensure_ascii=False, separators=(",", ":")),
                "request": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                "csrf_token": csrf_token,
            },
            referer=f"/en/chat/?node={node_id}",
        )
        result = response.get("response") if isinstance(response, dict) else None
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(str(result.get("error")))

    def send_chat_message(self, node_id: int, message: str, uploads: list[UploadItem]) -> None:
        page = self.chat_page(node_id)
        referer = f"/en/chat/?node={node_id}"
        if message or not uploads:
            self.send_chat_payload(node_id, page, message)
            page = self.chat_page(node_id)
        for file_id in self.upload_chat_files(uploads, referer):
            self.send_chat_payload(node_id, page, "", file_id)
            page = self.chat_page(node_id)


client = DigisellerClient()
ggsel_client = GgselClient()
funpay_client = FunPayClient()
UNREAD_CACHE: dict[str, Any] = {"time": 0.0, "data": None}
SALES_ORDER_BADGE_CACHE: dict[str, Any] = {"time": 0.0, "data": None}
PURCHASE_INFO_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
GGSEL_ORDER_INFO_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
GGSEL_PRODUCT_TITLE_CACHE: dict[str, tuple[float, str]] = {}
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
    "ggsel_last_set": "",
    "ggsel_last_heartbeat": "",
    "ggsel_setting": None,
    "ggsel_period": None,
    "ggsel_status": None,
    "ggsel_verified_online": False,
    "ggsel_set_error": "",
    "ggsel_heartbeat_error": "",
    "ggsel_setting_error": "",
    "ggsel_verify_error": "",
}
CHAT_KEEPALIVE_BROWSER_STATUS: dict[str, Any] = {
    "enabled": False,
    "opened": False,
    "reused": False,
    "last_open": "",
    "error": "",
}
FUNPAY_BOOST_INTERVAL_SECONDS = 12 * 3600
FUNPAY_BOOST_HISTORY_FILE = APP_DIR / "funpay_boost_history.json"
FUNPAY_BOOST_STATUS: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "last_run": "",
    "next_run": "",
    "last_error": "",
    "last_result": None,
}
FUNPAY_BOOST_COOLDOWNS: dict[int, float] = {}
FUNPAY_BOOST_HISTORY: dict[int, dict[str, Any]] = {}
FUNPAY_BOOST_LOCK = threading.Lock()
FUNPAY_BOOST_THREAD: threading.Thread | None = None

if FUNPAY_BOOST_HISTORY_FILE.exists():
    try:
        raw_funpay_boost_history = json.loads(FUNPAY_BOOST_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(raw_funpay_boost_history, dict):
            for raw_node_id, raw_item in raw_funpay_boost_history.items():
                if not isinstance(raw_item, dict):
                    continue
                node_id = int(raw_node_id)
                item = raw_item.copy()
                FUNPAY_BOOST_HISTORY[node_id] = item
                cooldown_until = float(item.get("cooldown_until_ts") or 0)
                if cooldown_until > time.time():
                    FUNPAY_BOOST_COOLDOWNS[node_id] = cooldown_until
    except Exception:
        FUNPAY_BOOST_HISTORY = {}
        FUNPAY_BOOST_COOLDOWNS = {}


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

.messages-layout{display:grid;grid-template-columns:360px minmax(0,1fr);height:calc(100vh - 120px);min-height:0;background:white;border:1px solid #d9e2ec;border-radius:12px;overflow:hidden;box-shadow:0 1px 2px #0001}.conversation-list{border-right:1px solid #e5e7eb;overflow-y:scroll;min-height:0;background:#fff}.conversation-title{font-size:34px;font-weight:800;padding:22px 22px 14px}.conversation-item{display:grid;grid-template-columns:48px minmax(0,1fr) auto;gap:12px;padding:12px 14px;border-bottom:1px solid #eef2f7;color:#1f2937}.conversation-item:hover{background:#f4f8ff}.conversation-item.active{background:#3f85d6;color:#fff}.conversation-item.active .muted,.conversation-item.active .preview{color:#eaf2ff}.avatar{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#111827;color:#fff;font-weight:800}.product-logo-avatar{box-sizing:border-box;flex-direction:column;gap:1px;border:1px solid #cbd5e1;line-height:1}.brand-image-avatar{overflow:hidden;padding:5px}.product-brand-logo{display:block;max-width:36px;max-height:36px;width:36px;height:36px;object-fit:contain}.product-logo-fallback{font-size:14px;font-weight:900;color:#111827}.product-logo-mark{font-size:15px;font-weight:900;letter-spacing:-.04em}.product-logo-name{max-width:42px;font-size:8px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.conversation-item.active .product-logo-avatar{border-color:#eff6ff;box-shadow:0 0 0 2px #ffffff55}.conversation-name{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.preview{color:#9ca3af;line-height:1.25;max-height:38px;overflow:hidden}.conversation-time{font-size:14px;white-space:nowrap}.badge{display:inline-block;min-width:18px;padding:2px 6px;border-radius:999px;background:#ef4444;color:white;font-size:12px;text-align:center;margin-top:6px}.conversation-panel{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;background:#fff}.conversation-panel.loading{position:relative}.chat-loading{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:#64748b;background:#fff}.chat-loading-spinner{width:34px;height:34px;border:4px solid #dbeafe;border-top-color:#1f7acb;border-radius:50%;animation:spin .8s linear infinite}.chat-loading-title{font-weight:800;color:#1f2937}.chat-loading-subtitle{font-size:13px}.loading-line{height:12px;border-radius:999px;background:linear-gradient(90deg,#eef2f7,#dbeafe,#eef2f7);background-size:200% 100%;animation:shine 1.1s linear infinite}.loading-lines{width:min(520px,70%);display:flex;flex-direction:column;gap:10px}.loading-lines .short{width:55%}@keyframes spin{to{transform:rotate(360deg)}}@keyframes shine{to{background-position:-200% 0}}.conversation-header{flex:0 0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 24px;border-bottom:1px solid #e5e7eb}.conversation-header-main{min-width:0}.conversation-header-side{display:flex;flex-direction:column;align-items:flex-end;gap:8px;max-width:360px;text-align:right}.conversation-header-title{font-size:18px;font-weight:800}.order-options{background:#f8fafc;border:1px solid #dbe4ee;border-radius:8px;padding:8px 10px;color:#334155;font-size:13px;line-height:1.35}.order-options-title{color:#64748b;font-size:12px;font-weight:800;margin-bottom:4px}.order-option-name{font-weight:800}.order-option-value{color:#0f172a}.conversation-body{padding:20px 24px;overflow-y:scroll;min-height:0;flex:1 1 auto;scrollbar-gutter:stable}.chat-row{margin:0 0 18px}.chat-meta{display:flex;justify-content:space-between;gap:12px;color:#6b7280;font-size:13px;margin-bottom:6px}.chat-author{font-weight:800;color:#1f2937}.chat-bubble{display:inline-block;max-width:78%;border-radius:10px;padding:10px 12px;line-height:1.45;background:#f3f4f6;white-space:pre-wrap;text-align:left}.chat-row.seller{text-align:right}.chat-row.seller .chat-meta{justify-content:flex-end}.chat-row.seller .chat-bubble{background:#eef6ff}.chat-row.buyer .chat-bubble{background:#fff;border:1px solid #e5e7eb}.read-receipt{display:inline-flex;align-items:center;gap:3px;margin:0 8px;color:#047857;font-size:12px;font-weight:800;white-space:nowrap}.toolbar a{margin-left:12px}.empty-state{padding:40px;color:#6b7280;text-align:center}.conversation-list::-webkit-scrollbar,.conversation-body::-webkit-scrollbar,.reply-editor::-webkit-scrollbar{width:12px}.conversation-list::-webkit-scrollbar-thumb,.conversation-body::-webkit-scrollbar-thumb,.reply-editor::-webkit-scrollbar-thumb{background:#94a3b8;border-radius:999px;border:3px solid #f8fafc}@media(max-width:850px){.messages-layout{grid-template-columns:1fr;height:calc(100vh - 110px)}.conversation-panel{min-height:0}.conversation-list{max-height:260px;border-right:0;border-bottom:1px solid #e5e7eb}.conversation-header{flex-direction:column}.conversation-header-side{align-items:flex-start;text-align:left;max-width:none}}

.alert-controls{position:fixed;right:18px;bottom:18px;z-index:50;display:flex;gap:8px;align-items:center}.alert-button{background:#16a34a;color:#fff;border:0;border-radius:999px;padding:10px 14px;font-weight:800;box-shadow:0 4px 14px #0002}.alert-button.off{background:#64748b}.alert-pill{display:none;background:#dc2626;color:#fff;border-radius:999px;padding:9px 12px;font-weight:800;box-shadow:0 4px 14px #0002}.alert-pill.show{display:inline-block}.unread-dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#ef4444;margin-left:6px}
.avatar.has-pending-dot{position:relative}.avatar.has-pending-dot::before{content:'';position:absolute;top:-2px;right:-2px;width:12px;height:12px;border-radius:50%;background:#ef4444;border:2px solid #fff;z-index:2}.pending-status-btn{background:#fef3c7;color:#92400e;border-color:#fcd34d}.pending-status-btn:hover{background:#fde68a}.done-status-btn{background:#dcfce7;color:#166534;border-color:#bbf7d0}.done-status-btn:hover{background:#bbf7d0}
.thumb{max-width:220px;max-height:160px;border:1px solid #e5e7eb;border-radius:8px;display:block;margin-top:8px;background:#f8fafc}.file-preview{margin-top:6px}.file-name{font-weight:700}.image-note{font-size:12px;color:#6b7280;margin-top:4px}
.reply-editor{flex:0 0 auto;max-height:260px;overflow-y:auto;border-top:1px solid #e5e7eb;background:#f8fafc;padding:14px 18px}.reply-editor textarea{width:100%;min-height:92px;box-sizing:border-box;resize:vertical;border:1px solid #cbd5e1;border-radius:8px;padding:10px;font:14px/1.45 inherit;background:white}.reply-toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.reply-toolbar button{background:#e0ecff;color:#0f3b66;border-color:#b9d4ff}.reply-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px}.reply-dropzone{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px dashed #93c5fd;border-radius:8px;background:#eff6ff;padding:8px 10px;color:#0f3b66}.reply-editor.dragover textarea{border-color:#2563eb;background:#eff6ff}.reply-dropzone.dragover,.reply-editor.dragover .reply-dropzone{background:#dbeafe;border-color:#2563eb}.reply-dropzone input[type=file]{background:white;max-width:360px}.reply-dropzone-text{font-size:13px;font-weight:700}.reply-hint,.selected-files{font-size:13px;color:#64748b}.common-phrases{border-top:1px solid #e5e7eb;background:#f8fafc;padding:10px 18px 14px}.common-phrase-title{font-size:13px;font-weight:800;color:#334155;margin-bottom:8px}.common-phrase-buttons{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}.common-phrase-buttons form{margin:0}.common-phrase-buttons button{background:#e0ecff;color:#0f3b66;border-color:#b9d4ff}.phrase-manager form[action='/phrases/save']{border:1px dashed #cbd5e1;border-radius:10px;padding:12px;background:#fff}.phrase-manager textarea{width:100%;box-sizing:border-box;min-height:130px;resize:vertical}.phrase-manager.dragover form[action='/phrases/save']{border-color:#2563eb;background:#eff6ff}.phrase-manager.dragover textarea{border-color:#2563eb;background:#eff6ff}.phrase-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:start;margin-bottom:10px}.phrase-empty{color:#64748b;font-size:14px}.selected-files{margin-top:10px}.selected-summary{margin-bottom:8px}.file-preview-grid{display:flex;flex-wrap:wrap;gap:8px}.file-chip{display:flex;align-items:center;gap:8px;max-width:230px;border:1px solid #cbd5e1;border-radius:8px;background:white;padding:6px 8px;color:#334155}.file-chip img{width:54px;height:54px;object-fit:cover;border-radius:6px;border:1px solid #e2e8f0;cursor:pointer}.file-chip-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-chip-icon{width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:6px;background:#e2e8f0;color:#475569;font-weight:800}.preview-modal{position:fixed;inset:0;z-index:120;display:flex;align-items:center;justify-content:center;background:#0f172acc;padding:24px}.preview-modal[hidden]{display:none}.preview-modal img{max-width:95vw;max-height:90vh;border-radius:8px;background:white;box-shadow:0 20px 50px #0008}.preview-modal-close{position:absolute;right:18px;top:14px;background:#fff;color:#0f172a;border:0;border-radius:999px;width:34px;height:34px;font-size:22px;line-height:1}.notice{border-radius:8px;padding:9px 12px;margin:0 0 10px}.notice.ok-bg{background:#dcfce7;color:#166534}.notice.bad-bg{background:#fee2e2;color:#991b1b}
.translated-message,.plain-message{white-space:normal}.translated-text,.original-text,.plain-text{white-space:pre-wrap}.message-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px}.toggle-original,.save-common-phrase{background:#f1f5f9;color:#334155;border-color:#cbd5e1;padding:5px 8px;font-size:12px}.save-common-phrase.saved{background:#dcfce7;color:#166534;border-color:#bbf7d0}.save-common-phrase.failed{background:#fee2e2;color:#991b1b;border-color:#fecaca}.translation-label{display:inline-block;color:#64748b;font-size:12px}
.original-inline{white-space:pre-wrap;color:#64748b;font-size:12px;margin-top:6px;border-top:1px dashed #cbd5e1;padding-top:6px}
.phrase-files{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.phrase-file{display:flex;align-items:center;gap:8px;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc;padding:6px 8px}.phrase-file img{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid #e2e8f0}.phrase-file-name{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.phrase-upload{display:flex;align-items:center;justify-content:center;gap:10px;min-height:68px;margin-top:10px;border:1px dashed #93c5fd;border-radius:10px;background:#eff6ff;color:#0f3b66;font-weight:700;padding:10px;cursor:pointer}.phrase-upload input{background:white}.phrase-image-preview,.common-phrase-buttons .common-phrase-preview{border:0;background:transparent;padding:0;cursor:pointer}.phrase-image-preview img,.common-phrase-preview img{display:block;width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid #cbd5e1}.common-phrase-card{display:inline-flex;align-items:center;gap:8px;max-width:360px;border:1px solid #b9d4ff;border-radius:10px;background:#eaf3ff;padding:6px;box-shadow:0 1px 1px #0001}.common-phrase-previews{display:flex;gap:6px;align-items:center;flex-shrink:0}.common-phrase-card .common-phrase-send{display:flex;flex-direction:column;align-items:flex-start;gap:2px;min-width:0;border:0;background:transparent;color:#0f3b66;padding:4px 6px;text-align:left}.common-phrase-text{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700}.common-phrase-files-note{font-size:11px;color:#64748b}.common-phrase-file-chip{display:inline-flex;align-items:center;border:1px solid #cbd5e1;border-radius:6px;background:#f8fafc;color:#475569;padding:4px 6px;font-size:12px}.common-phrase-preview.broken{display:none}.phrase-pending{margin-top:8px}
.chat-keepalive-btn{border:1px solid #bfdbfe;border-radius:999px;background:#eff6ff;color:#0f3b66;padding:4px 10px;font-size:12px;font-weight:800;white-space:nowrap}.chat-keepalive-btn.ok{background:#dcfce7;color:#166534;border-color:#bbf7d0}.chat-keepalive-btn.warn{background:#fef3c7;color:#92400e;border-color:#fde68a}
.funpay-boost-controls{position:fixed;left:18px;bottom:18px;z-index:100;display:flex;flex-direction:column;align-items:flex-start;gap:6px}.funpay-boost-button{border:1px solid #bbf7d0;border-radius:999px;background:#16a34a;color:white;padding:10px 16px;font-weight:900;box-shadow:0 8px 18px #0002}.funpay-boost-button.off{background:#475569;border-color:#cbd5e1}.funpay-boost-button.running{background:#f59e0b;border-color:#fde68a}.funpay-boost-pill{max-width:340px;border:1px solid #d9e2ec;border-radius:999px;background:white;color:#334155;padding:6px 10px;font-size:12px;box-shadow:0 3px 10px #0001}.funpay-boost-pill.bad{color:#b91c1c;border-color:#fecaca;background:#fff7f7}.funpay-boost-link{border:1px solid #bfdbfe;border-radius:999px;background:white;color:#0b65c2;padding:5px 10px;font-size:12px;font-weight:800;box-shadow:0 3px 10px #0001}.funpay-boost-clear-errors{border-color:#fecaca;color:#b91c1c}

.messages-layout{height:calc(100vh - 116px)}
.conversation-list-header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid #e5e7eb;padding:16px 14px 12px;box-shadow:0 1px 0 #eef2f7}
.conversation-list-title{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin-bottom:10px}.conversation-list-title h2{margin:0;font-size:28px;line-height:1}.conversation-counts{font-size:12px;color:#64748b;text-align:right;white-space:nowrap}.conversation-search{width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:8px;padding:9px 10px;margin-bottom:10px;background:#f8fafc}.conversation-filters{display:flex;gap:8px;flex-wrap:wrap}.conversation-filter{border:1px solid #cbd5e1;background:#fff;color:#334155;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800;cursor:pointer}.conversation-filter.active{background:#1f7acb;color:#fff;border-color:#1f7acb}.conversation-item[hidden]{display:none}.conversation-empty-filter{display:none;padding:28px 16px;color:#64748b;text-align:center}.conversation-empty-filter.visible{display:block}.conversation-section{padding:14px 14px 7px;color:#64748b;font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.04em;background:#f8fafc;border-bottom:1px solid #eef2f7}.conversation-section[hidden]{display:none}.chat-bubble{word-break:break-word}.reply-editor{border-top:1px solid #e5e7eb;background:#fbfdff}.reply-editor textarea{min-height:84px}.pending-send .chat-bubble{opacity:.75}.pending-send.send-failed .chat-bubble{background:#fee2e2;color:#991b1b}

.platform-badge{display:inline-block;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:900;background:#e0f2fe;color:#075985}.platform-badge.ggsel{background:#fef3c7;color:#92400e}.platform-badge.funpay{background:#dcfce7;color:#166534}.sales-source{white-space:nowrap}
.sales-toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.sales-toolbar input{max-width:90px}.sales-toolbar .sales-search{flex:1 1 260px;max-width:420px}.sales-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:12px 0}.sales-stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px}.sales-stat b{display:block;font-size:20px;margin-bottom:4px}.sales-table .order-link{font-weight:800}.sales-table .chat-action{display:inline-block;background:#1f7acb;color:#fff;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800}.sales-table tbody tr[hidden]{display:none}.sales-product{max-width:360px}.sales-empty-filter{display:none;padding:24px;text-align:center;color:#64748b}.sales-empty-filter.visible{display:block}
.pjax-bar{position:fixed;top:0;left:0;height:3px;width:0;background:#1f7acb;box-shadow:0 0 8px #1f7acb99;z-index:9999;opacity:0;pointer-events:none}.pjax-bar.active{width:88%;opacity:1;transition:width 8s cubic-bezier(.1,.7,.1,1)}#pjax-root{transition:opacity .12s}#pjax-root.pjax-loading{opacity:.55;pointer-events:none}
</style>
"""


# Client-side partial navigation (pjax). Clicking a top-nav link or submitting a
# GET form only swaps the #pjax-root content instead of reloading the whole page,
# so the shared shell (nav, online keepalive, alert poller) keeps running and
# heavy pages like Sales/Messages feel instant instead of doing a full refresh.
# Page scripts that start intervals or set window-level handlers register a
# teardown via window.pjaxOnCleanup so nothing leaks between navigations.
PJAX_SCRIPT = """
<script>
(() => {
  if (window.__pjaxReady || !window.history || !window.fetch || !window.DOMParser) return;
  window.__pjaxReady = true;
  const PATHS = new Set(['/', '/sales', '/chats', '/chat', '/unread', '/admin-messages', '/phrases', '/coding-prompts', '/product', '/stock', '/ggsel', '/ggsel-views', '/funpay-boost', '/unique-code']);
  window.__pjaxCleanups = window.__pjaxCleanups || [];
  window.pjaxOnCleanup = (fn) => { if (typeof fn === 'function') window.__pjaxCleanups.push(fn); };
  function runCleanups() {
    const fns = window.__pjaxCleanups.splice(0);
    fns.forEach((fn) => { try { fn(); } catch (e) {} });
  }
  const bar = document.createElement('div');
  bar.className = 'pjax-bar';
  document.body.appendChild(bar);
  function pjaxable(href) {
    try {
      const url = new URL(href, location.href);
      return url.origin === location.origin && PATHS.has(url.pathname);
    } catch (e) {
      return false;
    }
  }
  function runScripts(container) {
    container.querySelectorAll('script').forEach((oldScript) => {
      const script = document.createElement('script');
      for (const attr of oldScript.attributes) script.setAttribute(attr.name, attr.value);
      script.textContent = oldScript.textContent;
      oldScript.replaceWith(script);
    });
  }
  let seq = 0;
  async function load(url, push) {
    const root = document.getElementById('pjax-root');
    if (!root) { location.href = url; return; }
    const mySeq = ++seq;
    bar.classList.add('active');
    root.classList.add('pjax-loading');
    try {
      const res = await fetch(url, {headers: {'X-Pjax': '1'}, cache: 'no-store'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const text = await res.text();
      if (mySeq !== seq) return;
      const doc = new DOMParser().parseFromString(text, 'text/html');
      const next = doc.getElementById('pjax-root');
      if (!next) { location.href = url; return; }
      runCleanups();
      root.innerHTML = next.innerHTML;
      if (doc.title) document.title = doc.title;
      runScripts(root);
      if (window.loadDigisellerTranslations) window.loadDigisellerTranslations(root);
      if (push !== false) history.pushState({pjax: true}, '', url);
      window.scrollTo(0, 0);
    } catch (e) {
      location.href = url;
    } finally {
      if (mySeq === seq) {
        bar.classList.remove('active');
        root.classList.remove('pjax-loading');
      }
    }
  }
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a');
    if (!link) return;
    const target = link.getAttribute('target');
    if ((target && target !== '_self') || link.hasAttribute('download') || link.dataset.noPjax === '1') return;
    if (!pjaxable(link.href)) return;
    if (new URL(link.href, location.href).href === location.href) { event.preventDefault(); return; }
    event.preventDefault();
    load(link.href, true);
  });
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.dataset.noPjax === '1') return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'get') return;
    if (!form.closest('#pjax-root')) return;
    let url;
    try {
      url = new URL(form.getAttribute('action') || location.href, location.href);
    } catch (e) {
      return;
    }
    if (!pjaxable(url.href)) return;
    url.search = new URLSearchParams(new FormData(form)).toString();
    event.preventDefault();
    load(url.href, true);
  });
  window.addEventListener('popstate', () => {
    if (pjaxable(location.href)) load(location.href, false);
  });
})();
</script>
"""


def layout(title: str, body: str, *, include_funpay_boost: bool = False) -> bytes:
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
    ggsel_last_set = str(online.get("ggsel_last_set") or "")
    ggsel_last_heartbeat = str(online.get("ggsel_last_heartbeat") or "")
    if online.get("verified_online") or online.get("public_online") or online.get("ggsel_verified_online"):
        online_label = "Online verified"
        online_class = "ok"
    elif online_error and online_error != "disabled":
        online_label = "Online error"
        online_class = "bad"
    elif online_last_set or online_last_heartbeat or ggsel_last_set or ggsel_last_heartbeat:
        online_label = "Online heartbeat"
        online_class = ""
    elif online_last_ok:
        online_label = "Online active"
        online_class = "ok"
    else:
        online_label = "Online checking"
        online_class = ""
    online_title = f"Last verified: {online_last_ok or '-'} | Last set: {online_last_set or '-'} | Last chat heartbeat: {online_last_heartbeat or '-'} | API status: {online.get('status') if online.get('status') is not None else '-'} | Public online: {'yes' if online.get('public_online') else 'no'} | GGSEL set: {ggsel_last_set or '-'} | GGSEL heartbeat: {ggsel_last_heartbeat or '-'} | GGSEL status: {online.get('ggsel_status') if online.get('ggsel_status') is not None else '-'} | Set error: {online_set_error or '-'} | Chat heartbeat error: {online_heartbeat_error or '-'} | Setting error: {online_setting_error or '-'} | Verify error: {online_verify_error or '-'} | GGSEL set error: {online.get('ggsel_set_error') or '-'} | GGSEL heartbeat error: {online.get('ggsel_heartbeat_error') or '-'} | GGSEL verify error: {online.get('ggsel_verify_error') or '-'} | Public checked: {online_public_checked_at or '-'} | Public verify error: {online_public_error or '-'} | Recovering: {online_recovery_error or '-'} | Error: {online_error or '-'}"
    chat_keepalive_url = get_chat_keepalive_url()
    chat_browser = CHAT_KEEPALIVE_BROWSER_STATUS.copy()
    chat_button_label = "Chat window active" if chat_browser.get("opened") else "Open chat window"
    chat_button_class = "ok" if chat_browser.get("opened") else "warn"
    sales_badge = sales_order_badge_summary()
    sales_badge_count = int(sales_badge.get("count") or 0)
    sales_badge_html = (
        f"<span id=\"sales-order-badge\" style=\"display:inline-flex;align-items:center;justify-content:center;min-width:17px;height:17px;padding:0 5px;border-radius:999px;background:#ef4444;color:#fff;font-size:11px;font-weight:900;line-height:1;box-shadow:0 0 0 2px #1f7acb\">{h(sales_badge_count)}</span>"
        if sales_badge_count > 0
        else ""
    )
    try:
        messages_badge_count = int(cached_unread_summary().get("message_nav_unread") or 0)
    except Exception:
        messages_badge_count = 0
    messages_badge_html = (
        f"<span id=\"messages-unread-badge\" style=\"display:inline-flex;align-items:center;justify-content:center;min-width:17px;height:17px;padding:0 5px;border-radius:999px;background:#ef4444;color:#fff;font-size:11px;font-weight:900;line-height:1;box-shadow:0 0 0 2px #1f7acb\">{h(messages_badge_count)}</span>"
        if messages_badge_count > 0
        else ""
    )
    nav = f"""
    <div class="top">
      <a href="/" aria-label="Home"><img class="brand-logo" src="/assets/shinchan-logo.png" alt="Crayon Shin-chan"></a>
      <div class="top-nav">
        <a href="/">Dashboard</a>
        <a href="/sales" style="display:inline-flex;align-items:center;gap:5px">Sales{sales_badge_html}</a>
        <a id="messages-nav-link" href="/chats" style="display:inline-flex;align-items:center;gap:5px">Messages{messages_badge_html}</a>
        <a href="/unread">Unread</a>
        <a href="/admin-messages">Admin</a>
        <a href="/phrases">&#24120;&#29992;&#35821;</a>
        <a href="/coding-prompts">AI&#25552;&#31034;&#35789;</a>
        <a href="/product">Product</a>
        <a href="/stock">Stock</a>
        <a href="/ggsel">GGSEL</a>
        <a href="/ggsel-views">GGSEL浏览量</a>
        <a href="/funpay-boost">FunPay Boost</a>
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
        const ggselLastSet = status.ggsel_last_set || '';
        const ggselLastHeartbeat = status.ggsel_last_heartbeat || '';
        const setError = status.set_error || '';
        const heartbeatError = status.heartbeat_error || '';
        const settingError = status.setting_error || '';
        const verifyError = status.verify_error || '';
        const publicError = status.public_error || '';
        const publicCheckedAt = status.public_checked_at || '';
        const recoveryError = status.recovery_error || '';
        let label = 'Online checking';
        let cls = '';
        if (status.verified_online || status.public_online || status.ggsel_verified_online) {{
          label = 'Online verified';
          cls = 'ok';
        }} else if (error && error !== 'disabled') {{
          const chatOpened = status.chat_browser && status.chat_browser.opened;
          label = chatOpened ? 'Online verifying' : 'Online error';
          cls = chatOpened ? '' : 'bad';
        }} else if (lastSet || lastHeartbeat || ggselLastSet || ggselLastHeartbeat) {{
          label = 'Online heartbeat';
        }} else if (lastOk) {{
          label = 'Online active';
          cls = 'ok';
        }}
        pill.textContent = label;
        pill.className = `top-online ${{cls}}`;
        pill.title = `Last verified: ${{lastOk || '-'}} | Last set: ${{lastSet || '-'}} | Last chat heartbeat: ${{lastHeartbeat || '-'}} | API status: ${{status.status ?? '-'}} | Public online: ${{status.public_online ? 'yes' : 'no'}} | GGSEL set: ${{ggselLastSet || '-'}} | GGSEL heartbeat: ${{ggselLastHeartbeat || '-'}} | GGSEL status: ${{status.ggsel_status ?? '-'}} | Set error: ${{setError || '-'}} | Chat heartbeat error: ${{heartbeatError || '-'}} | Setting error: ${{settingError || '-'}} | Verify error: ${{verifyError || '-'}} | GGSEL set error: ${{status.ggsel_set_error || '-'}} | GGSEL heartbeat error: ${{status.ggsel_heartbeat_error || '-'}} | GGSEL verify error: ${{status.ggsel_verify_error || '-'}} | Public checked: ${{publicCheckedAt || '-'}} | Public verify error: ${{publicError || '-'}} | Recovering: ${{recoveryError || '-'}} | Error: ${{error || '-'}}`;
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
    (() => {{
      const badge = document.getElementById('sales-order-badge');
      if (!badge) return;
      function setBadge(count) {{
        const value = Number(count || 0);
        badge.textContent = String(value);
        badge.hidden = value <= 0;
      }}
      async function refreshSalesBadge() {{
        try {{
          const res = await fetch('/api/sales-order-count', {{cache: 'no-store'}});
          if (!res.ok) return;
          const data = await res.json();
          setBadge(data.count || 0);
        }} catch (e) {{}}
      }}
      setInterval(refreshSalesBadge, 30000);
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
      function updateNavBadge(linkId, badgeId, count) {
        const link = document.getElementById(linkId);
        if (!link) return;
        let badge = document.getElementById(badgeId);
        if (count <= 0) {
          if (badge) badge.remove();
          return;
        }
        if (!badge) {
          badge = document.createElement('span');
          badge.id = badgeId;
          badge.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;min-width:17px;height:17px;padding:0 5px;border-radius:999px;background:#ef4444;color:#fff;font-size:11px;font-weight:900;line-height:1;box-shadow:0 0 0 2px #1f7acb';
          link.appendChild(badge);
        }
        badge.textContent = String(count);
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
          updateNavBadge('messages-nav-link', 'messages-unread-badge', Number(data.message_nav_unread || 0));
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
    funpay_boost_ui = """
    <div class="funpay-boost-controls">
      <div id="funpay-boost-pill" class="funpay-boost-pill">FunPay Boost checking...</div>
      <button id="funpay-boost-button" class="funpay-boost-button off" type="button">Start FunPay Boost</button>
      <button id="funpay-boost-clear-errors" class="funpay-boost-link funpay-boost-clear-errors" type="button">清除错误记录</button>
    </div>
    <script>
    (() => {
      const btn = document.getElementById('funpay-boost-button');
      const pill = document.getElementById('funpay-boost-pill');
      const clearErrorsBtn = document.getElementById('funpay-boost-clear-errors');
      if (!btn || !pill) return;
      let enabled = false;
      function render(data) {
        enabled = Boolean(data.enabled);
        btn.classList.toggle('off', !enabled);
        btn.classList.toggle('running', Boolean(data.running));
        btn.textContent = data.running
          ? 'FunPay Boost running...'
          : enabled ? 'Stop FunPay 12-hour Boost' : 'Start FunPay 12-hour Boost';
        pill.classList.toggle('bad', Boolean(data.last_error));
        const parts = [];
        if (data.last_error) parts.push(`Error: ${data.last_error}`);
        else if (data.running) parts.push('Checking cooldown and boosting...');
        else parts.push(enabled ? '12-hour Boost enabled' : '12-hour Boost disabled');
        if (data.last_result && !data.running) {
          const result = data.last_result;
          const total = Number(result.total || 0);
          const success = Number(result.success || 0);
          const skipped = Number(result.skipped || 0);
          const failed = Number(result.failed || 0);
          parts.push(total > 0
            ? `Last check: ${success} boosted, ${skipped} skipped, ${failed} failed`
            : 'Last check: no active categories');
        }
        if (data.next_run) parts.push(`Next 12-hour check: ${data.next_run}`);
        if (data.failed_count) parts.push(`Failed records: ${data.failed_count}`);
        pill.textContent = parts.join(' · ');
      }
      async function loadStatus() {
        try {
          const res = await fetch('/api/funpay-boost-status', {cache: 'no-store'});
          if (!res.ok) throw new Error('status failed');
          render(await res.json());
        } catch (e) {
          pill.textContent = 'FunPay Boost status unavailable';
          pill.classList.add('bad');
        }
      }
      async function toggle() {
        btn.disabled = true;
        try {
          const res = await fetch('/api/funpay-boost-toggle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: !enabled})
          });
          if (!res.ok) throw new Error(await res.text());
          render(await res.json());
        } catch (e) {
          pill.textContent = String(e.message || e);
          pill.classList.add('bad');
        } finally {
          btn.disabled = false;
        }
      }
      async function clearErrors() {
        if (!confirm('只清除失败记录，保留已提升和冷却记录。继续？')) return;
        clearErrorsBtn.disabled = true;
        try {
          const res = await fetch('/api/funpay-boost-clear-errors', {method: 'POST'});
          if (!res.ok) throw new Error(await res.text());
          window.location.reload();
        } catch (e) {
          pill.textContent = String(e.message || e);
          pill.classList.add('bad');
        } finally {
          clearErrorsBtn.disabled = false;
        }
      }
      btn.addEventListener('click', toggle);
      if (clearErrorsBtn) clearErrorsBtn.addEventListener('click', clearErrors);
      const boostStatusTimer = setInterval(loadStatus, 15000);
      loadStatus();
      if (window.pjaxOnCleanup) window.pjaxOnCleanup(() => clearInterval(boostStatusTimer));
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
      const nodes = Array.from(root.querySelectorAll(".translated-message[data-pending='1']:not([data-loading='1'])")).reverse();
      if (!nodes.length) return;
      const chunkSize = 8;
      for (let start = 0; start < nodes.length; start += chunkSize) {
        const chunk = nodes.slice(start, start + chunkSize);
        chunk.forEach((node) => { node.dataset.loading = '1'; });
        const messages = chunk.map((node) => ({
          id: node.id.replace(/^msg-/, ''),
          text: (node.querySelector('.original-text') || {}).textContent || ''
        })).filter((item) => item.text);
        if (!messages.length) {
          chunk.forEach((node) => { delete node.dataset.loading; });
          continue;
        }
        try {
          const res = await fetch('/api/translate-batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({messages})
          });
          if (!res.ok) throw new Error('translate failed');
          const data = await res.json();
          (data.results || []).forEach((item) => {
            const node = root.querySelector(`#msg-${CSS.escape(String(item.id))}`);
            if (!node) return;
            const translated = node.querySelector('.translated-text');
            const label = node.querySelector('.translation-label');
            if (translated) translated.textContent = item.translated || item.text || '';
            if (label) label.textContent = String(item.label || item.source_lang || 'auto') + ' \u2192 \u4e2d';
            node.dataset.pending = '0';
            delete node.dataset.loading;
          });
        } catch (e) {
          chunk.forEach((node) => { delete node.dataset.loading; });
        }
      }
    };
    window.loadDigisellerTranslations(document);
    </script>
    """
    pjax_root = f"<div id='pjax-root'>{funpay_boost_ui if include_funpay_boost else ''}<div class='wrap'>{body}</div></div>"
    html_doc = f"<!doctype html><html><head><meta charset='utf-8'><title>{h(title)}</title><link rel='icon' type='image/png' href='/favicon.ico'><link rel='apple-touch-icon' href='/assets/shinchan-logo.png'>{STYLE}</head><body>{nav}{alert_ui}{translation_ui}{PJAX_SCRIPT}{pjax_root}</body></html>"
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


def digiseller_stock_user_key() -> str:
    return str(client.seller_id)


def load_digiseller_stock_cache() -> dict[str, Any]:
    if not DIGISELLER_STOCK_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(DIGISELLER_STOCK_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_digiseller_stock_cache(data: dict[str, Any]) -> None:
    tmp = DIGISELLER_STOCK_CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DIGISELLER_STOCK_CACHE_FILE)


def record_digiseller_stock_upload(product_id: int, items: list[dict[str, Any]], response: dict[str, Any]) -> None:
    uploaded = response.get("content")
    if not isinstance(uploaded, list):
        return
    data = load_digiseller_stock_cache()
    user_key = digiseller_stock_user_key()
    products = data.setdefault(user_key, {})
    if not isinstance(products, dict):
        products = {}
        data[user_key] = products
    product_key = str(product_id)
    entries = products.setdefault(product_key, [])
    if not isinstance(entries, list):
        entries = []
        products[product_key] = entries
    for item, saved in zip(items, uploaded):
        if not isinstance(saved, dict):
            continue
        content_id = int(saved.get("content_id") or 0)
        value = clean_text(item.get("value"))
        if not content_id or not value:
            continue
        entries.append(
            {
                "content_id": content_id,
                "value": value,
                "serial": clean_text(item.get("serial")),
                "variant_id": int(item.get("id_v") or 0),
                "created_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
        )
    save_digiseller_stock_cache(data)


def first_cached_digiseller_stock(product_id: int) -> dict[str, Any]:
    data = load_digiseller_stock_cache()
    products = data.get(digiseller_stock_user_key())
    if not isinstance(products, dict):
        return {}
    entries = products.get(str(product_id))
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        if isinstance(entry, dict) and int(entry.get("content_id") or 0) and clean_text(entry.get("value")):
            return entry
    return {}


def remove_cached_digiseller_stock(product_id: int, content_id: int) -> None:
    data = load_digiseller_stock_cache()
    products = data.get(digiseller_stock_user_key())
    if not isinstance(products, dict):
        return
    entries = products.get(str(product_id))
    if not isinstance(entries, list):
        return
    products[str(product_id)] = [entry for entry in entries if not isinstance(entry, dict) or int(entry.get("content_id") or 0) != content_id]
    save_digiseller_stock_cache(data)


def ggsel_chat_order_id(chat: dict[str, Any]) -> int:
    try:
        return int(chat.get("id_i") or chat.get("invoice_id") or 0)
    except (TypeError, ValueError):
        return 0


def ggsel_chat_last_date(chat: dict[str, Any]) -> Any:
    return chat.get("last_message") or chat.get("date")


def unread_summary() -> dict[str, Any]:
    try:
        buyer = [
            c
            for c in client.chats(only_unread=True)
            if int(c.get("cnt_new") or 0) > 0 and is_recent_time(c.get("last_date"), RECENT_CHAT_DAYS)
        ]
    except Exception:
        buyer = []
    ggsel_buyer: list[dict[str, Any]] = []
    if ggsel_client.configured():
        try:
            ggsel_data = ggsel_client.chats(page_size=50, only_unread=True)
            ggsel_rows = ggsel_data.get("items") or ggsel_data.get("chats") or []
            ggsel_buyer = [
                c
                for c in ggsel_rows
                if isinstance(c, dict) and int(c.get("cnt_new") or 0) > 0 and is_recent_time(ggsel_chat_last_date(c), RECENT_CHAT_DAYS)
            ]
        except Exception:
            ggsel_buyer = []
    funpay_buyer: list[dict[str, Any]] = []
    if funpay_client.configured():
        try:
            funpay_buyer = [
                c
                for c in funpay_client.chats(limit=50)
                if int(c.get("cnt_new") or 0) > 0 and is_recent_time(c.get("last_date"), RECENT_CHAT_DAYS)
            ]
        except Exception:
            funpay_buyer = []
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

    def add_latest(rec: dict[str, Any]) -> None:
        nonlocal latest
        if latest is None or sort_time(rec.get("last_date")) > sort_time(latest.get("last_date")):
            latest = rec

    for chat in buyer:
        rec = {
            "type": "buyer",
            "platform": "digiseller",
            "order_id": chat.get("id_i"),
            "email": chat.get("email"),
            "product": clean_text(chat.get("product")),
            "last_date": chat.get("last_date"),
            "cnt_new": int(chat.get("cnt_new") or 0),
            "url": f"/chats?order_id={chat.get('id_i')}",
        }
        buyer_unread.append(rec)
        add_latest(rec)
    for chat in ggsel_buyer:
        order_id = ggsel_chat_order_id(chat)
        email = chat.get("email") or f"ggsel-{order_id}"
        product = clean_text(chat.get("product") or "GGSEL order")
        rec = {
            "type": "buyer",
            "platform": "ggsel",
            "order_id": order_id,
            "email": email,
            "product": product,
            "last_date": ggsel_chat_last_date(chat),
            "cnt_new": int(chat.get("cnt_new") or 0),
            "url": "/chats?" + urllib.parse.urlencode({"platform": "ggsel", "order_id": str(order_id or ""), "email": str(email), "product": product}),
        }
        buyer_unread.append(rec)
        add_latest(rec)
    for chat in funpay_buyer:
        node_id = chat.get("node_id")
        name = chat.get("name") or f"FunPay-{node_id}"
        rec = {
            "type": "buyer",
            "platform": "funpay",
            "order_id": node_id,
            "email": name,
            "product": clean_text(chat.get("message") or "FunPay chat"),
            "last_date": chat.get("last_date"),
            "cnt_new": int(chat.get("cnt_new") or 0),
            "url": "/chats?" + urllib.parse.urlencode({"platform": "funpay", "order_id": str(node_id or ""), "email": str(name), "product": "FunPay chat"}),
        }
        buyer_unread.append(rec)
        add_latest(rec)
    for chat in guest:
        corr_id = int(chat.get("CorrID") or 0)
        corr_type = str(chat.get("CorrType") or chat.get("Type") or "visitor")
        rec = {
            "type": "guest",
            "platform": "digiseller",
            "order_id": "",
            "email": chat.get("Name") or f"GUEST-{corr_id}",
            "product": clean_text(chat.get("PurchaseName") or chat.get("Text")),
            "last_date": chat.get("DateWriteUtc") or chat.get("DateWrite"),
            "cnt_new": 1,
            "url": f"/chats?kind=guest&corr_type={urllib.parse.quote(corr_type)}&corr_id={corr_id}",
        }
        add_latest(rec)
    digiseller_unread_messages = sum(int(c.get("cnt_new") or 0) for c in buyer)
    ggsel_unread_messages = sum(int(c.get("cnt_new") or 0) for c in ggsel_buyer)
    funpay_unread_messages = sum(int(c.get("cnt_new") or 0) for c in funpay_buyer)
    message_nav_unread = digiseller_unread_messages + ggsel_unread_messages + funpay_unread_messages + len(guest)
    total = digiseller_unread_messages + ggsel_unread_messages + funpay_unread_messages + len(guest) + len(admin)
    return {
        "ok": True,
        "buyer_unread_chats": len(buyer) + len(ggsel_buyer) + len(funpay_buyer),
        "buyer_unread_messages": digiseller_unread_messages + ggsel_unread_messages + funpay_unread_messages,
        "digiseller_unread_chats": len(buyer),
        "digiseller_unread_messages": digiseller_unread_messages,
        "ggsel_unread_chats": len(ggsel_buyer),
        "ggsel_unread_messages": ggsel_unread_messages,
        "funpay_unread_chats": len(funpay_buyer),
        "funpay_unread_messages": funpay_unread_messages,
        "buyer_unread": buyer_unread,
        "guest_unread_chats": len(guest),
        "admin_unread": len(admin),
        "message_nav_unread": message_nav_unread,
        "total": total,
        "latest": latest,
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

def cached_unread_summary(max_age: int = 30) -> dict[str, Any]:
    now = time.time()
    cached = UNREAD_CACHE.get("data")
    if cached is not None and now - float(UNREAD_CACHE.get("time") or 0) < max_age:
        return cached
    data = unread_summary()
    UNREAD_CACHE["time"] = now
    UNREAD_CACHE["data"] = data
    return data


def clear_unread_cache() -> None:
    UNREAD_CACHE["time"] = 0.0
    UNREAD_CACHE["data"] = None


def safe_mark_chat_read(order_id: int) -> None:
    try:
        client.mark_chat_read(order_id)
    except Exception:
        return
    clear_unread_cache()


def safe_mark_ggsel_chat_read(order_id: int) -> None:
    try:
        ggsel_client.mark_chat_read(order_id)
    except Exception:
        return
    clear_unread_cache()


def order_chat_href(order_id: Any, email: Any = "", product: Any = "") -> str:
    params = {"order_id": str(order_id or "").strip()}
    email_text = clean_text(email)
    product_text = clean_text(product)
    if email_text:
        params["email"] = email_text
    if product_text:
        params["product"] = product_text
    return "/chats?" + urllib.parse.urlencode(params)


def load_sales_order_state() -> dict[str, Any]:
    if not SALES_ORDER_SEEN_FILE.exists():
        return {"initialized": False, "seen_invoice_ids": []}
    try:
        data = json.loads(SALES_ORDER_SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"initialized": False, "seen_invoice_ids": []}
    if not isinstance(data, dict):
        return {"initialized": False, "seen_invoice_ids": []}
    seen = [str(item) for item in data.get("seen_invoice_ids") or [] if str(item)]
    return {"initialized": bool(data.get("initialized")), "seen_invoice_ids": seen}


def save_sales_order_state(state: dict[str, Any]) -> None:
    seen = [str(item) for item in state.get("seen_invoice_ids") or [] if str(item)]
    payload = {
        "initialized": bool(state.get("initialized")),
        "seen_invoice_ids": seen[:500],
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    SALES_ORDER_SEEN_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sales_invoice_id(row: dict[str, Any]) -> str:
    return str(row.get("invoice_id") or row.get("id_i") or row.get("id") or "").strip()


def sales_order_badge_summary(force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = SALES_ORDER_BADGE_CACHE.get("data")
    if not force and cached is not None and now - float(SALES_ORDER_BADGE_CACHE.get("time") or 0) < 45:
        return cached
    try:
        rows = [row for row in client.sales(3, 50).get("rows", []) if isinstance(row, dict)]
        invoice_ids: list[str] = []
        for row in rows:
            invoice_id = sales_invoice_id(row)
            if invoice_id and invoice_id not in invoice_ids:
                invoice_ids.append(invoice_id)
        state = load_sales_order_state()
        if not state.get("initialized"):
            save_sales_order_state({"initialized": True, "seen_invoice_ids": invoice_ids})
            unseen_ids: list[str] = []
        else:
            seen = set(str(item) for item in state.get("seen_invoice_ids") or [])
            unseen_ids = [invoice_id for invoice_id in invoice_ids if invoice_id not in seen]
        data = {
            "ok": True,
            "count": len(unseen_ids),
            "invoice_ids": unseen_ids,
            "latest_invoice_id": invoice_ids[0] if invoice_ids else "",
            "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    except Exception as exc:
        data = {"ok": False, "count": 0, "error": short(exc, 160)}
    SALES_ORDER_BADGE_CACHE["time"] = now
    SALES_ORDER_BADGE_CACHE["data"] = data
    return data


def mark_sales_orders_seen(rows: list[dict[str, Any]]) -> None:
    invoice_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        invoice_id = sales_invoice_id(row)
        if invoice_id and invoice_id not in invoice_ids:
            invoice_ids.append(invoice_id)
    state = load_sales_order_state()
    existing = [str(item) for item in state.get("seen_invoice_ids") or [] if str(item)]
    merged = invoice_ids + [invoice_id for invoice_id in existing if invoice_id not in invoice_ids]
    save_sales_order_state({"initialized": True, "seen_invoice_ids": merged})
    SALES_ORDER_BADGE_CACHE["time"] = time.time()
    SALES_ORDER_BADGE_CACHE["data"] = {
        "ok": True,
        "count": 0,
        "invoice_ids": [],
        "latest_invoice_id": invoice_ids[0] if invoice_ids else "",
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


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


def translate_common_phrases_to_chinese() -> tuple[int, int]:
    """One-off: rewrite every stored common phrase's text into Chinese (zh-CN).

    Phrases are stored in the language we type and re-translated to the buyer on
    send, so normalizing existing phrases to Chinese makes the defaults Chinese.
    Ids and files are preserved; empty texts and text already in Chinese are left
    unchanged. Every user key in the file is processed. Returns
    (translated_count, total_phrases_with_text).
    """
    if not COMMON_PHRASES_FILE.exists():
        return 0, 0
    data = json.loads(COMMON_PHRASES_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0, 0
    translated = 0
    total = 0
    for values in data.values():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            total += 1
            if heuristic_language(text) in {"zh", "zh-CN"}:
                continue
            zh, _ = google_translate(text, "zh-CN")
            zh = (zh or "").strip()
            if zh and zh != text:
                item["text"] = zh
                translated += 1
    tmp = COMMON_PHRASES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(COMMON_PHRASES_FILE)
    return translated, total


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
        for key in ("option", "options", "items", "values", "parameters", "params", "fields", "field", "answers"):
            if key in raw_options:
                nested = normalize_option_items(raw_options.get(key))
                if nested:
                    return nested
        item_keys = {"name", "parameter", "title", "label", "caption", "field", "field_name", "question", "key", "value", "user_data", "text", "answer"}
        if not item_keys.intersection(raw_options):
            return [{"name": key, "value": value} for key, value in raw_options.items()]
        return [raw_options]
    if isinstance(raw_options, list):
        return raw_options
    return []


def option_sources_from_info(info: dict[str, Any]) -> list[Any]:
    source_keys = (
        "options",
        "option",
        "parameters",
        "params",
        "fields",
        "answers",
        "user_data",
        "form_data",
        "additional_fields",
    )
    sources: list[Any] = []
    for container in (info, info.get("content"), info.get("order"), info.get("purchase"), info.get("sale"), info.get("product")):
        if not isinstance(container, dict):
            continue
        for key in source_keys:
            if key in container:
                sources.append(container.get(key))
    return sources


def purchase_options_from_info(info: dict[str, Any]) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in option_sources_from_info(info):
        for raw in normalize_option_items(source):
            if isinstance(raw, dict):
                name = first_option_text(raw, ("name", "parameter", "title", "label", "caption", "field", "field_name", "question", "key"))
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
                        "answer",
                        "answers",
                        "user_value",
                        "input",
                        "choice",
                        "choices",
                    ),
                )
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


def translate_option_text(text: str) -> str:
    value = clean_text(text)
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    if normalized in OPTION_TRANSLATIONS:
        return OPTION_TRANSLATIONS[normalized]
    if not value or not should_translate_text(value):
        return value
    translated, _ = google_translate(value, "zh-CN")
    return translated or value


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


def order_options_block_html(options: list[tuple[str, str]]) -> str:
    if not options:
        return ""
    rows = "".join(
        f"<div class='order-option'><span class='order-option-name' title='{h(name)}'>{h(translate_option_text(name))}:</span> "
        f"<span class='order-option-value' title='{h(value)}'>{h(translate_option_text(value))}</span></div>"
        for name, value in options
    )
    return f"<div class='order-options'><div class='order-options-title'>&#39069;&#22806;&#36873;&#39033;</div>{rows}</div>"


def order_options_html(order_id: int) -> str:
    return order_options_block_html(purchase_options_from_info(cached_purchase_info(order_id)))


def cached_ggsel_order_info(order_id: int) -> dict[str, Any]:
    if not order_id or not ggsel_client.configured():
        return {}
    now = time.time()
    cached = GGSEL_ORDER_INFO_CACHE.get(order_id)
    if cached and now - cached[0] < 300:
        return cached[1]
    info: dict[str, Any] = {}
    try:
        info = ggsel_client.purchase_info(order_id)
    except Exception:
        info = {}
    if not info:
        try:
            rows = ggsel_client.sales(100).get("sales") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("invoice_id") or row.get("id_i") or row.get("id") or "")
                if row_id == str(order_id):
                    info = row
                    break
        except Exception:
            info = {}
    GGSEL_ORDER_INFO_CACHE[order_id] = (now, info)
    return info


def looks_like_opaque_product_ref(value: Any) -> bool:
    text = clean_text(value)
    if not text:
        return False
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, flags=re.I):
        return True
    return bool(re.fullmatch(r"[0-9a-f]{24,}", text, flags=re.I))


def looks_like_ggsel_product_id(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{5,}", clean_text(value)))


def cached_ggsel_product_title(product_id: Any) -> str:
    product_id_text = clean_text(product_id)
    if not looks_like_ggsel_product_id(product_id_text):
        return ""
    now = time.time()
    cached = GGSEL_PRODUCT_TITLE_CACHE.get(product_id_text)
    if cached and now - cached[0] < 6 * 60 * 60:
        return cached[1]
    title = ""
    try:
        title = ggsel_client.public_product_title(product_id_text)
    except Exception:
        title = ""
    GGSEL_PRODUCT_TITLE_CACHE[product_id_text] = (now, title)
    return title


def ggsel_order_product_name_from_info(info: dict[str, Any]) -> str:
    containers: list[dict[str, Any]] = []
    for value in (info, info.get("content"), info.get("product"), info.get("sale")):
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in ("name", "product_name", "name_goods", "goods_name", "title", "product_title"):
            product = clean_text(container.get(key))
            if product and not looks_like_opaque_product_ref(product):
                return product
        product = container.get("product")
        if isinstance(product, dict):
            for key in ("name", "product_name", "name_goods", "goods_name", "title", "product_title"):
                product_name = clean_text(product.get(key))
                if product_name and not looks_like_opaque_product_ref(product_name):
                    return product_name
    return ""


def ggsel_order_product_name(order_id: int, fallback: Any = "") -> str:
    product = ggsel_order_product_name_from_info(cached_ggsel_order_info(order_id))
    if product:
        return product
    product_id = ggsel_order_product_id(order_id, fallback)
    title = cached_ggsel_product_title(product_id)
    if title:
        return title
    fallback_text = clean_text(fallback)
    if fallback_text and not looks_like_opaque_product_ref(fallback_text):
        return fallback_text
    return "GGSEL order"


def ggsel_order_product_id_from_info(info: dict[str, Any]) -> str:
    containers: list[dict[str, Any]] = []
    for value in (info, info.get("content"), info.get("product"), info.get("sale")):
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in ("item_id", "product_id", "id_goods", "id", "good"):
            product_id = clean_text(container.get(key))
            if product_id:
                return product_id
        product = container.get("product")
        if isinstance(product, dict):
            for key in ("id", "product_id", "id_goods", "good"):
                product_id = clean_text(product.get(key))
                if product_id:
                    return product_id
    return ""


def ggsel_order_product_id(order_id: int, fallback: Any = "") -> str:
    product_id = ggsel_order_product_id_from_info(cached_ggsel_order_info(order_id))
    if product_id:
        return product_id
    fallback_text = clean_text(fallback)
    return fallback_text or str(order_id)


def ggsel_order_amount_from_info(info: dict[str, Any]) -> tuple[str, str]:
    containers: list[dict[str, Any]] = []
    for value in (info, info.get("content"), info.get("sale"), info.get("order"), info.get("purchase")):
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        amount = clean_text(container.get("amount") or container.get("amount_in") or container.get("sum") or container.get("total"))
        currency = clean_text(container.get("type_curr") or container.get("currency") or container.get("currency_type"))
        if amount:
            return amount, currency
    for container in containers:
        amount_usd = clean_text(container.get("amount_usd") or container.get("sum_usd") or container.get("total_usd"))
        if amount_usd:
            return amount_usd, "USD"
    return "", ""


def ggsel_sale_order_amount(sale: dict[str, Any]) -> tuple[str, str]:
    try:
        order_id = int(sale.get("invoice_id") or sale.get("id_i") or sale.get("id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    amount = ""
    currency = ""
    if order_id:
        amount, currency = ggsel_order_amount_from_info(cached_ggsel_order_info(order_id))
    if not amount:
        amount, currency = ggsel_order_amount_from_info(sale)
    return amount, currency


def ggsel_seller_office_offer_for_order(order_id: int, fallback_product: Any = "") -> dict[str, Any]:
    product_id = ggsel_order_product_id(order_id, fallback_product)
    if looks_like_ggsel_product_id(product_id):
        try:
            offer = ggsel_client.seller_office_offer_by_ggsel_id(product_id)
        except Exception:
            offer = {}
        if offer:
            return offer
    product_name = ggsel_order_product_name(order_id, fallback_product)
    return ggsel_client.seller_office_search_offer(product_name)


def digiseller_order_product_name(order_id: int, fallback: Any = "") -> str:
    info = cached_purchase_info(order_id)
    for key in ("product", "product_name", "goods_name", "name_goods", "name"):
        value = clean_text(info.get(key))
        if value and not looks_like_opaque_product_ref(value):
            return value
    product_info = info.get("product")
    if isinstance(product_info, dict):
        for key in ("name", "title"):
            value = clean_text(product_info.get(key))
            if value and not looks_like_opaque_product_ref(value):
                return value
    fallback_text = clean_text(fallback)
    if fallback_text and fallback_text != "Direct order lookup":
        return fallback_text
    return ""


def digiseller_order_product_id(order_id: int) -> int:
    info = cached_purchase_info(order_id)
    containers: list[dict[str, Any]] = []
    for value in (info, info.get("content"), info.get("product")):
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in ("id_goods", "item_id", "product_id", "id_good", "good_id", "id"):
            try:
                product_id = int(container.get(key) or 0)
            except (TypeError, ValueError):
                product_id = 0
            if product_id:
                return product_id
        product = container.get("product")
        if isinstance(product, dict):
            for key in ("id", "product_id", "id_goods", "item_id"):
                try:
                    product_id = int(product.get(key) or 0)
                except (TypeError, ValueError):
                    product_id = 0
                if product_id:
                    return product_id
    return 0


STOCK_FILE_URL_KEYS = ("download_url", "file_url", "url", "href", "link", "file", "path")
STOCK_TEXT_FILE_EXTENSIONS = (".txt", ".csv", ".log", ".md", ".json", ".yaml", ".yml")


def looks_like_stock_file_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path or url).lower()
    if path.endswith(STOCK_TEXT_FILE_EXTENSIONS):
        return True
    return any(part in path for part in ("/download", "/downloads", "/files/", "/uploads/", "/attachments/"))


def stock_file_url_from_value(value: Any, *, require_file_hint: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    href = re.search(r'''href=["']([^"']+)["']''', raw, flags=re.I)
    if href:
        candidate = html.unescape(href.group(1)).strip()
        if candidate and (not require_file_hint or looks_like_stock_file_url(candidate)):
            return candidate
    plain = clean_text(raw)
    is_url = re.match(r"https?://", plain, flags=re.I) or plain.startswith("//")
    is_relative = plain.startswith(("/api/", "/api_", "/uploads/", "/storage/", "/files/", "/download"))
    if (is_url or is_relative) and (not require_file_hint or looks_like_stock_file_url(plain)):
        return plain
    return ""


def stock_file_url_from_item(item: dict[str, Any]) -> str:
    for key in STOCK_FILE_URL_KEYS:
        value = item.get(key)
        require_file_hint = key not in {"download_url", "file_url", "file"}
        if isinstance(value, str):
            candidate = stock_file_url_from_value(value, require_file_hint=require_file_hint)
            if candidate:
                return candidate
        if isinstance(value, dict):
            candidate = stock_file_url_from_item(value)
            if candidate:
                return candidate
    attachments = item.get("attachments") or item.get("files")
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, dict):
                candidate = stock_file_url_from_item(attachment)
                if candidate:
                    return candidate
            elif isinstance(attachment, str):
                candidate = stock_file_url_from_value(attachment)
                if candidate:
                    return candidate
    return stock_file_url_from_value(item.get("value"))


def stock_item_message_text(offer_id: int, item: dict[str, Any]) -> str:
    file_url = stock_file_url_from_item(item)
    if file_url:
        file_text = ggsel_client.seller_office_fetch_text(file_url)
        if file_text:
            return file_text
    return clean_text(item.get("value"))


def seller_office_stock_for_offer(offer: dict[str, Any]) -> dict[str, Any]:
    offer_id = int(offer.get("id") or 0)
    if not offer_id:
        raise RuntimeError("Could not match this order to a seller-office offer")
    products_data = ggsel_client.seller_office_offer_products(offer_id, page=1, limit=20)
    products = products_data.get("data") if isinstance(products_data, dict) else []
    if not isinstance(products, list) or not products:
        raise RuntimeError("No stock items are available for this offer")
    stock_item: dict[str, Any] | None = None
    stock_message = ""
    for item in products:
        if not isinstance(item, dict):
            continue
        message = stock_item_message_text(offer_id, item)
        if message:
            stock_item = item
            stock_message = message
            break
    if not stock_item:
        raise RuntimeError("No usable stock item value is available for this offer")
    stock_item_id = int(stock_item.get("id") or 0)
    if not stock_item_id:
        raise RuntimeError("Stock item ID is missing")
    return {"offer_id": offer_id, "stock_item_id": stock_item_id, "message": stock_message, "product": clean_text(offer.get("title"))}


def funpay_offer_search_text(product_name: str) -> str:
    product = clean_text(product_name)
    parts = [part.strip() for part in product.split(",") if part.strip()]
    if len(parts) >= 3:
        product = ", ".join(parts[2:])
    product = re.sub(r",\s*(?:Free|[\d.]+\s*[A-Z$€₽]+),\s*\d+\s+pcs?\.?$", "", product, flags=re.I)
    product = re.sub(r",\s*\d+\s+pcs?\.?$", "", product, flags=re.I)
    return clean_text(product)


def stock_item_draft_for_chat(order_id: int, platform: str, fallback_product: Any = "") -> dict[str, Any]:
    normalized_platform = platform if platform in ("ggsel", "funpay") else "digiseller"
    if normalized_platform == "ggsel":
        offer = ggsel_seller_office_offer_for_order(order_id, fallback_product)
    elif normalized_platform == "funpay":
        product_name = funpay_client.chat_product(order_id) or clean_text(fallback_product)
        offer = ggsel_client.seller_office_search_offer(product_name)
        if not offer:
            offer = ggsel_client.seller_office_search_offer(funpay_offer_search_text(product_name))
    else:
        product_id = digiseller_order_product_id(order_id)
        if not product_id:
            raise RuntimeError("Could not find the Digiseller product ID for this order")
        item = first_cached_digiseller_stock(product_id)
        if not item:
            raise RuntimeError("No cached Digiseller stock is available for this product. Upload stock through the Stock page first.")
        stock_item_id = int(item.get("content_id") or 0)
        message = clean_text(item.get("value"))
        if not stock_item_id or not message:
            raise RuntimeError("Cached Digiseller stock item is missing content")
        return {
            "offer_id": product_id,
            "stock_item_id": stock_item_id,
            "message": message,
            "product": digiseller_order_product_name(order_id, fallback_product) or f"Product {product_id}",
            "platform": normalized_platform,
        }
    stock = seller_office_stock_for_offer(offer)
    message = stock["message"]
    offer_id = int(stock["offer_id"])
    stock_item_id = int(stock["stock_item_id"])
    return {
        "offer_id": offer_id,
        "stock_item_id": stock_item_id,
        "message": message,
        "product": stock["product"],
        "platform": normalized_platform,
    }


def delete_stock_item_after_reply(platform: str, offer_id: int, stock_item_id: int) -> None:
    if not offer_id or not stock_item_id:
        return
    try:
        if platform == "digiseller":
            client.delete_content(offer_id, stock_item_id)
            remove_cached_digiseller_stock(offer_id, stock_item_id)
        else:
            ggsel_client.seller_office_delete_product(offer_id, stock_item_id)
    except Exception as exc:
        raise RuntimeError(f"Reply was sent, but removing it from stock failed: {exc}") from exc


def send_stock_item_to_chat(order_id: int, platform: str, fallback_product: Any = "") -> dict[str, Any]:
    stock = stock_item_draft_for_chat(order_id, platform, fallback_product)
    message = stock["message"]
    offer_id = int(stock["offer_id"])
    stock_item_id = int(stock["stock_item_id"])
    normalized_platform = str(stock["platform"])
    if normalized_platform == "ggsel":
        ggsel_client.send_chat_message(order_id, message, [])
    elif normalized_platform == "funpay":
        funpay_client.send_chat_message(order_id, message, [])
    else:
        client.send_chat_message(order_id, message, [])
    delete_stock_item_after_reply(normalized_platform, offer_id, stock_item_id)
    return {"offer_id": offer_id, "stock_item_id": stock_item_id, "product": stock["product"], "platform": normalized_platform}


def ggsel_send_stock_item_to_chat(order_id: int, fallback_product: Any = "") -> dict[str, Any]:
    return send_stock_item_to_chat(order_id, "ggsel", fallback_product)


def ggsel_order_buyer_email_from_info(info: dict[str, Any]) -> str:
    content = info.get("content")
    content_buyer = content.get("buyer_info") if isinstance(content, dict) else None
    for value in (info.get("buyer_info"), content_buyer, content):
        if isinstance(value, dict):
            email = clean_text(value.get("email") or value.get("buyer_email"))
            if email:
                return email
    return clean_text(info.get("email") or info.get("buyer_email"))


def ggsel_order_buyer_email(order_id: int, fallback: Any = "") -> str:
    email = ggsel_order_buyer_email_from_info(cached_ggsel_order_info(order_id))
    if email:
        return email
    fallback_text = clean_text(fallback)
    return fallback_text or f"ggsel-{order_id}"


def ggsel_order_options_html(order_id: int, selected_chat: dict[str, Any]) -> str:
    options: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in (cached_ggsel_order_info(order_id), selected_chat):
        for item in purchase_options_from_info(source):
            if item in seen:
                continue
            seen.add(item)
            options.append(item)
    return order_options_block_html(options)


def new_phrase_id(text: str) -> str:
    seed = f"{time.time_ns()}:{text}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def phrase_stored_path(stored: str) -> Path | None:
    if not stored or "/" in stored or "\\" in stored:
        return None
    file_path = (COMMON_PHRASES_DIR / stored).resolve()
    if not str(file_path).startswith(str(COMMON_PHRASES_DIR.resolve())) or not file_path.exists():
        return None
    return file_path


def phrase_file_url(stored: str) -> str:
    path = phrase_stored_path(stored)
    version = f"?v={int(path.stat().st_mtime)}" if path else ""
    public_base = os.getenv("DIGISELLER_COMMON_PHRASE_PUBLIC_BASE_URL", "").strip()
    if public_base:
        return public_base.rstrip("/") + "/" + urllib.parse.quote(stored, safe="") + version
    return "/phrase-files/" + urllib.parse.quote(stored, safe="") + version


def digiseller_debate_image_url(image_id: str, filename: str, width: int = 360) -> str:
    ref = urllib.parse.quote(f"{image_id}/{filename}")
    return f"https://graph.digiseller.ru/img_deb.ashx?f={ref}&w={width}"


def phrase_file_reference(file: dict[str, Any]) -> tuple[str, str, str]:
    filename = str(file.get("filename") or file.get("name") or "")
    stored = str(file.get("stored") or "")
    legacy_file = str(file.get("file") or "")
    explicit_url = str(file.get("preview") or file.get("url") or file.get("src") or "")
    if not explicit_url and legacy_file and urllib.parse.urlparse(legacy_file).scheme:
        explicit_url = legacy_file
    if not stored and legacy_file and not urllib.parse.urlparse(legacy_file).scheme:
        stored = legacy_file
    if not filename and stored and looks_like_image_name(stored):
        filename = stored
    if stored and phrase_stored_path(stored):
        return filename or stored or "file", stored, phrase_file_url(stored)
    if not stored and filename:
        candidate = (COMMON_PHRASES_DIR / filename).resolve()
        if str(candidate).startswith(str(COMMON_PHRASES_DIR.resolve())) and candidate.exists():
            stored = filename
    if stored and phrase_stored_path(stored):
        return filename or stored or "file", stored, phrase_file_url(stored)
    file_url = explicit_url
    if not file_url and phrase_file_is_image(file, filename, ""):
        image_id = str(file.get("newid") or file.get("id") or file.get("file_id") or file.get("fileId") or "")
        if not image_id and legacy_file and not looks_like_image_name(legacy_file):
            image_id = legacy_file
        if image_id and filename:
            file_url = digiseller_debate_image_url(image_id, filename)
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


def funpay_boost_time_label(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else ""


def funpay_boost_duration_label(seconds: int | None) -> str:
    if seconds is None:
        return "未知"
    if seconds <= 0:
        return "已冷却"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时 {minutes}分钟"
    if minutes:
        return f"{minutes}分钟 {secs}秒"
    return f"{secs}秒"


def save_funpay_boost_history_locked() -> None:
    data = {str(node_id): item for node_id, item in sorted(FUNPAY_BOOST_HISTORY.items())}
    FUNPAY_BOOST_HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_funpay_boost_attempt(
    node_id: int,
    status: str,
    message: str,
    cooldown_until: float | None = None,
    product_name: str = "",
) -> None:
    checked_at = time.time()
    with FUNPAY_BOOST_LOCK:
        item = FUNPAY_BOOST_HISTORY.get(node_id, {"node_id": node_id})
        item.update(
            {
                "node_id": node_id,
                "status": status,
                "message": message,
                "last_checked_ts": checked_at,
                "last_checked_at": funpay_boost_time_label(checked_at),
            }
        )
        if product_name:
            item["product_name"] = product_name
        if cooldown_until is not None:
            item["cooldown_until_ts"] = cooldown_until
            item["cooldown_until"] = funpay_boost_time_label(cooldown_until)
            if cooldown_until > checked_at:
                FUNPAY_BOOST_COOLDOWNS[node_id] = cooldown_until
        elif status not in {"boosted", "cooling"}:
            FUNPAY_BOOST_COOLDOWNS.pop(node_id, None)
            item.pop("cooldown_until_ts", None)
            item.pop("cooldown_until", None)
        FUNPAY_BOOST_HISTORY[node_id] = item
        save_funpay_boost_history_locked()


def funpay_boost_history_rows() -> list[dict[str, Any]]:
    now = time.time()
    with FUNPAY_BOOST_LOCK:
        rows = []
        for node_id, item in FUNPAY_BOOST_HISTORY.items():
            row = item.copy()
            row["node_id"] = node_id
            cooldown_until = float(row.get("cooldown_until_ts") or 0)
            row["remaining_seconds"] = max(0, int(cooldown_until - now)) if cooldown_until else None
            row["cooling"] = bool(cooldown_until and cooldown_until > now)
            rows.append(row)
        rows.sort(key=lambda row: (row.get("status") == "failed", not row.get("cooling"), int(row.get("node_id") or 0)))
        return rows


def funpay_boost_snapshot() -> dict[str, Any]:
    with FUNPAY_BOOST_LOCK:
        data = FUNPAY_BOOST_STATUS.copy()
        now = time.time()
        history_items = list(FUNPAY_BOOST_HISTORY.values())
        data["history_count"] = len(history_items)
        data["failed_count"] = sum(1 for item in history_items if item.get("status") == "failed")
        data["cooling_count"] = sum(1 for item in history_items if float(item.get("cooldown_until_ts") or 0) > now)
        data["boosted_count"] = sum(1 for item in history_items if item.get("last_boost_ts"))
        return data


def clear_funpay_boost_failed_history() -> int:
    with FUNPAY_BOOST_LOCK:
        failed_node_ids = [
            node_id
            for node_id, item in FUNPAY_BOOST_HISTORY.items()
            if item.get("status") == "failed"
        ]
        for node_id in failed_node_ids:
            FUNPAY_BOOST_HISTORY.pop(node_id, None)
        if failed_node_ids:
            save_funpay_boost_history_locked()
        return len(failed_node_ids)


def sync_funpay_boost_active_categories(categories: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    active_categories = categories if categories is not None else funpay_client.active_offer_categories()
    synced_at = time.time()
    synced_at_label = funpay_boost_time_label(synced_at)
    added = 0
    updated = 0
    product_names: dict[int, str] = {}
    with FUNPAY_BOOST_LOCK:
        for category in active_categories:
            node_id = int(category.get("node_id") or 0)
            if not node_id:
                continue
            game_id = int(category.get("game_id") or 0)
            product_name = str(category.get("product_name") or "").strip()
            if product_name:
                product_names[node_id] = product_name
            existing = FUNPAY_BOOST_HISTORY.get(node_id)
            item = existing.copy() if existing else {"node_id": node_id}
            changed = existing is None
            if existing is None:
                added += 1
                item.update(
                    {
                        "status": "synced",
                        "message": "Active product synced",
                        "last_checked_ts": synced_at,
                        "last_checked_at": synced_at_label,
                    }
                )
            if product_name and item.get("product_name") != product_name:
                item["product_name"] = product_name
                changed = True
            if game_id and item.get("game_id") != game_id:
                item["game_id"] = game_id
                changed = True
            if changed:
                FUNPAY_BOOST_HISTORY[node_id] = item
                if existing is not None:
                    updated += 1
        if added or updated:
            save_funpay_boost_history_locked()
    return {
        "total": len(active_categories),
        "added": added,
        "updated": updated,
        "synced_at": synced_at_label,
        "product_names": product_names,
    }


def update_funpay_boost_status(**values: Any) -> None:
    with FUNPAY_BOOST_LOCK:
        FUNPAY_BOOST_STATUS.update(values)


def funpay_boost_cooldown_until(node_id: int) -> float:
    with FUNPAY_BOOST_LOCK:
        return float(FUNPAY_BOOST_COOLDOWNS.get(node_id) or 0)


def remember_funpay_boost_cooldown(
    node_ids: list[int],
    available_at: float,
    product_names: dict[int, str] | None = None,
) -> None:
    boosted_at = time.time()
    with FUNPAY_BOOST_LOCK:
        for node_id in node_ids:
            FUNPAY_BOOST_COOLDOWNS[node_id] = available_at
            item = FUNPAY_BOOST_HISTORY.get(node_id, {"node_id": node_id})
            item.update(
                {
                    "node_id": node_id,
                    "status": "boosted",
                    "message": "Offers boosted",
                    "last_checked_ts": boosted_at,
                    "last_checked_at": funpay_boost_time_label(boosted_at),
                    "last_boost_ts": boosted_at,
                    "last_boost_at": funpay_boost_time_label(boosted_at),
                    "cooldown_until_ts": available_at,
                    "cooldown_until": funpay_boost_time_label(available_at),
                }
            )
            product_name = (product_names or {}).get(node_id) or ""
            if product_name:
                item["product_name"] = product_name
            FUNPAY_BOOST_HISTORY[node_id] = item
        save_funpay_boost_history_locked()


def run_funpay_boost_once() -> dict[str, Any]:
    started_at = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    update_funpay_boost_status(running=True, last_run=started_at, last_error="")
    try:
        result = funpay_client.boost_all_active_offers()
    except Exception as exc:
        result = {
            "status": "failed",
            "total": 0,
            "success": 0,
            "skipped": 0,
            "failed": 1,
            "message": str(exc),
            "results": [],
        }
        update_funpay_boost_status(running=False, last_error=str(exc), last_result=result)
        raise
    update_funpay_boost_status(running=False, last_error="", last_result=result)
    return result


def schedule_next_funpay_boost_run() -> float:
    next_run_ts = time.time() + FUNPAY_BOOST_INTERVAL_SECONDS
    next_run = dt.datetime.utcfromtimestamp(next_run_ts).strftime("%Y-%m-%d %H:%M:%S UTC")
    update_funpay_boost_status(next_run=next_run)
    return next_run_ts


def funpay_boost_loop() -> None:
    deadline = schedule_next_funpay_boost_run()
    while funpay_boost_snapshot().get("enabled"):
        while time.time() < deadline:
            if not funpay_boost_snapshot().get("enabled"):
                update_funpay_boost_status(next_run="")
                return
            time.sleep(min(5, max(0.1, deadline - time.time())))
        if not funpay_boost_snapshot().get("enabled"):
            update_funpay_boost_status(next_run="")
            return
        try:
            run_funpay_boost_once()
        except Exception:
            pass
        deadline = schedule_next_funpay_boost_run()


def start_funpay_boost_scheduler() -> dict[str, Any]:
    global FUNPAY_BOOST_THREAD
    funpay_client.ensure_configured()
    with FUNPAY_BOOST_LOCK:
        already_enabled = bool(FUNPAY_BOOST_STATUS.get("enabled"))
        FUNPAY_BOOST_STATUS["enabled"] = True
        FUNPAY_BOOST_STATUS["last_error"] = ""
        FUNPAY_BOOST_STATUS["next_run"] = ""
    if not already_enabled:
        run_funpay_boost_once()
    if not funpay_boost_snapshot().get("enabled"):
        return funpay_boost_snapshot()
    schedule_next_funpay_boost_run()
    with FUNPAY_BOOST_LOCK:
        already_alive = FUNPAY_BOOST_THREAD is not None and FUNPAY_BOOST_THREAD.is_alive()
        if not already_alive:
            FUNPAY_BOOST_THREAD = threading.Thread(target=funpay_boost_loop, daemon=True)
            FUNPAY_BOOST_THREAD.start()
    return funpay_boost_snapshot()


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
        ggsel_enabled = os.getenv("GGSEL_KEEP_ONLINE", os.getenv("DIGISELLER_KEEP_ONLINE", "1")).strip().lower() not in {"0", "false", "no", "off"}
        if not client.configured() and not (ggsel_enabled and ggsel_client.configured()):
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
        if client.configured():
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
        ggsel_set_data: dict[str, Any] = {}
        ggsel_heartbeat_data: dict[str, Any] = {}
        ggsel_setting_data: dict[str, Any] = {}
        ggsel_status_data: dict[str, Any] = {}
        ggsel_set_error = ""
        ggsel_heartbeat_error = ""
        ggsel_setting_error = ""
        ggsel_verify_error = ""
        if ggsel_enabled and ggsel_client.configured():
            try:
                ggsel_set_data = ggsel_client.set_online()
            except Exception as exc:
                ggsel_set_error = str(exc)
            try:
                ggsel_heartbeat_data = ggsel_client.messenger_heartbeat()
            except Exception as exc:
                ggsel_heartbeat_error = str(exc)
            try:
                ggsel_setting_data = ggsel_client.online_setting()
            except Exception as exc:
                ggsel_setting_error = str(exc)
            try:
                ggsel_status_data = ggsel_client.seller_online_status()
            except Exception as exc:
                ggsel_verify_error = str(exc)
        status = status_data.get("status")
        try:
            status_value = int(status or 0)
        except (TypeError, ValueError):
            status_value = 0
        public_online = bool(public_data.get("online"))
        verified_online = status_value > 0 or public_online
        ggsel_status = ggsel_status_data.get("status")
        try:
            ggsel_status_value = int(ggsel_status or 0)
        except (TypeError, ValueError):
            ggsel_status_value = 0
        ggsel_verified_online = ggsel_status_value > 0
        keepalive_ok = not set_error or not heartbeat_error
        ggsel_keepalive_ok = not ggsel_enabled or not ggsel_client.configured() or not ggsel_set_error or not ggsel_heartbeat_error
        cycle_errors = []
        if not (verified_online or keepalive_ok):
            cycle_errors.extend(item for item in (set_error, heartbeat_error) if item)
        if not (ggsel_verified_online or ggsel_keepalive_ok):
            cycle_errors.extend(item for item in (ggsel_set_error, ggsel_heartbeat_error) if item)
        cycle_error = " | ".join(cycle_errors)
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
            ggsel_last_set=checked_at if not ggsel_set_error and ggsel_enabled and ggsel_client.configured() else ONLINE_KEEPALIVE_STATUS.get("ggsel_last_set", ""),
            ggsel_last_heartbeat=checked_at if not ggsel_heartbeat_error and ggsel_enabled and ggsel_client.configured() else ONLINE_KEEPALIVE_STATUS.get("ggsel_last_heartbeat", ""),
            ggsel_setting=ggsel_setting_data.get("setting", ONLINE_KEEPALIVE_STATUS.get("ggsel_setting")),
            ggsel_period=ggsel_setting_data.get("period", ONLINE_KEEPALIVE_STATUS.get("ggsel_period")),
            ggsel_status=ggsel_status,
            ggsel_verified_online=ggsel_verified_online,
            ggsel_set_error=ggsel_set_error,
            ggsel_heartbeat_error=ggsel_heartbeat_error,
            ggsel_setting_error=ggsel_setting_error,
            ggsel_verify_error=ggsel_verify_error,
            ggsel_set_response=ggsel_set_data,
            ggsel_heartbeat_response=ggsel_heartbeat_data,
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
    def send_html(self, title: str, body: str, status: int = 200, *, include_funpay_boost: bool = False) -> None:
        data = layout(title, body, include_funpay_boost=include_funpay_boost)
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

    def wants_json_response(self) -> bool:
        return self.headers.get("X-Requested-With") == "fetch" or "application/json" in self.headers.get("Accept", "")

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

    def reply_editor(self, order_id: int, target_lang: str, platform: str = "digiseller", email: str = "", product: str = "") -> str:
        editor_id = f"reply-{platform}-{order_id}"
        pending_buttons = (
            f"<button id='{editor_id}-pending-button' type='button' class='pending-status-btn' data-order-id='{order_id}' data-platform='{h(platform)}'>&#24453;&#22788;&#29702;</button>"
            f"<button id='{editor_id}-done-button' type='button' class='done-status-btn' data-order-id='{order_id}' data-platform='{h(platform)}'>&#24050;&#23436;&#25104;</button>"
        )
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
                f"<form class='common-phrase-card' method='post' action='/chats/send' data-pending-text='{h(text or fallback_label)}'>"
                f"<input type='hidden' name='order_id' value='{order_id}'>"
                f"<input type='hidden' name='platform' value='{h(platform)}'>"
                f"<input type='hidden' name='target_lang' value='{h(target_lang)}'>"
                f"<input type='hidden' name='email' value='{h(email)}'>"
                f"<input type='hidden' name='product' value='{h(product)}'>"
                f"<input type='hidden' name='phrase_id' value='{h(phrase['id'])}'>"
                f"{previews_html}"
                f"<button class='common-phrase-send' type='submit' title='{h(text or fallback_label)}'>"
                f"<span class='common-phrase-text'>{h(label)}</span>{file_note}</button></form>"
            )
        if phrase_forms:
            phrases_html = (
                f"<div id='{editor_id}-phrases' class='common-phrases' hidden><div class='common-phrase-title'>&#24120;&#29992;&#35821;&#65288;&#28857;&#20987;&#31435;&#21363;&#21457;&#36865;&#65289;</div>"
                f"<div class='common-phrase-buttons'>{''.join(phrase_forms)}</div></div>"
            )
        else:
            phrases_html = f"<div id='{editor_id}-phrases' class='common-phrases' hidden><div class='phrase-empty'>&#36824;&#27809;&#26377;&#24120;&#29992;&#35821;&#65292;<a href='/phrases'>&#21435;&#28155;&#21152;</a></div></div>"
        return f"""
        <form id="{editor_id}" class="reply-editor" method="post" action="/chats/send" enctype="multipart/form-data">
          <input type="hidden" name="order_id" value="{order_id}">
          <input type="hidden" name="platform" value="{h(platform)}">
          <input type="hidden" name="target_lang" value="{h(target_lang)}">
          <input type="hidden" name="email" value="{h(email)}">
          <input type="hidden" name="product" value="{h(product)}">

          <textarea id="{editor_id}-message" name="message" placeholder="&#22312;&#36825;&#37324;&#22238;&#22797;&#20250;&#21592;&#20449;&#24687;&#65292;&#21487;&#22635;&#20889;&#36134;&#21495;&#12289;&#23494;&#30721;&#12289;&#38142;&#25509;&#12289;&#20351;&#29992;&#35828;&#26126;&#31561;&#12290;"></textarea>
          <div class="reply-actions">
            <div id="{editor_id}-dropzone" class="reply-dropzone">
              <input id="{editor_id}-files" name="files" type="file" multiple accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.rtf,.zip,.rar,.7z">
              <span class="reply-dropzone-text">&#25302;&#25341;&#22270;&#29255;/&#38468;&#20214;&#21040;&#36825;&#37324;&#65292;&#25110;&#28857;&#20987;&#36873;&#25321;&#25991;&#20214;</span>
            </div>
            <button type="submit">&#21457;&#36865;&#22238;&#22797;</button>
            {pending_buttons}
            <button id="{editor_id}-phrase-toggle" type="button" aria-expanded="false" aria-controls="{editor_id}-phrases">&#24120;&#29992;&#35821;</button>
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
          const phrases = document.getElementById('{editor_id}-phrases');
          const phraseToggle = document.getElementById('{editor_id}-phrase-toggle');
          const pendingButton = document.getElementById('{editor_id}-pending-button');
          const doneButton = document.getElementById('{editor_id}-done-button');
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
          if (phraseToggle && phrases) {{
            phraseToggle.addEventListener('click', () => {{
              const show = phrases.hidden;
              phrases.hidden = !show;
              phraseToggle.setAttribute('aria-expanded', show ? 'true' : 'false');
              phraseToggle.textContent = show ? '\u9690\u85cf\u5e38\u7528\u8bed' : '\u5e38\u7528\u8bed';
            }});
          }}
          document.querySelectorAll('.common-phrase-preview').forEach((button) => {{
            if (button.dataset.previewReady) return;
            button.dataset.previewReady = '1';
            const img = button.querySelector('img');
            if (img) {{
              img.addEventListener('error', () => {{
                img.title = button.dataset.previewSrc || '';
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
          function getPendingOrders() {{
            try {{ return JSON.parse(localStorage.getItem('pendingOrders') || '{{}}'); }} catch {{ return {{}}; }}
          }}
          function savePendingOrders(obj) {{
            localStorage.setItem('pendingOrders', JSON.stringify(obj));
          }}
          function updatePendingDots() {{
            const pending = getPendingOrders();
            document.querySelectorAll('.conversation-item[data-order-id]').forEach(item => {{
              const key = (item.dataset.platform || 'digiseller') + '-' + item.dataset.orderId;
              const avatar = item.querySelector('.avatar');
              if (!avatar) return;
              avatar.classList.toggle('has-pending-dot', !!pending[key]);
            }});
          }}
          updatePendingDots();
          if (pendingButton) {{
            pendingButton.addEventListener('click', () => {{
              const orderId = pendingButton.dataset.orderId;
              const platform = pendingButton.dataset.platform || 'digiseller';
              const key = platform + '-' + orderId;
              const pending = getPendingOrders();
              pending[key] = true;
              savePendingOrders(pending);
              updatePendingDots();
            }});
          }}
          if (doneButton) {{
            doneButton.addEventListener('click', () => {{
              const orderId = doneButton.dataset.orderId;
              const platform = doneButton.dataset.platform || 'digiseller';
              const key = platform + '-' + orderId;
              const pending = getPendingOrders();
              delete pending[key];
              savePendingOrders(pending);
              updatePendingDots();
            }});
          }}
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
          async function submitChatForm(form, button, pendingText, clearEditor) {{
            const originalText = button ? button.textContent : '';
            if (button) {{
              button.disabled = true;
              button.textContent = '\u53d1\u9001\u4e2d...';
            }}
            const body = form.closest('.conversation-panel')?.querySelector('.conversation-body');
            const pending = document.createElement('div');
            pending.className = 'chat-row seller pending-send';
            const text = String(pendingText || '').trim();
            pending.innerHTML = '<div class="chat-meta"><span class="chat-author">nose1989 <span class="muted">\u53d1\u9001\u4e2d...</span></span></div><div class="chat-bubble"></div>';
            pending.querySelector('.chat-bubble').textContent = text || '\u9644\u4ef6\u6b63\u5728\u53d1\u9001...';
            if (body) {{
              body.appendChild(pending);
              body.scrollTop = body.scrollHeight;
            }}
            const formData = new FormData(form);
            let sent = false;
            try {{
              const res = await fetch(form.action, {{
                method: 'POST',
                body: formData,
                headers: {{'Accept': 'application/json', 'X-Requested-With': 'fetch'}}
              }});
              if (!res.ok) {{
                const contentType = res.headers.get('content-type') || '';
                let detail = '';
                if (contentType.includes('application/json')) {{
                  const data = await res.json().catch(() => ({{}}));
                  detail = data.error || '';
                }} else {{
                  const html = await res.text().catch(() => '');
                  const doc = new DOMParser().parseFromString(html, 'text/html');
                  detail = (doc.querySelector('pre.card, pre.code')?.textContent || doc.body.textContent || '')
                    .trim()
                    .replace(/\\s+/g, ' ')
                    .slice(0, 240);
                }}
                throw new Error(detail || `HTTP ${{res.status}}`);
              }}
              const data = await res.json().catch(() => ({{}}));
              if (!data.ok) throw new Error(data.error || 'Send failed');
              sent = true;
              if (clearEditor) {{
                textarea.value = '';
                selectedFiles = [];
                syncInputFiles();
                renderSelectedFiles();
              }}
              const params = new URLSearchParams({{
                platform: data.platform || formData.get('platform') || 'digiseller',
                order_id: String(data.order_id || formData.get('order_id') || ''),
                email: String(formData.get('email') || ''),
                product: String(formData.get('product') || '')
              }});
              const panelRes = await fetch('/api/chat-panel?' + params.toString(), {{cache: 'no-store'}});
              if (!panelRes.ok) throw new Error(`HTTP ${{panelRes.status}}`);
              const panelData = await panelRes.json();
              if (!panelData.ok) throw new Error(panelData.error || 'Refresh failed');
              const panel = root.closest('.conversation-panel');
              if (panel) {{
                panel.outerHTML = panelData.html;
                const newPanel = document.getElementById('chat-panel');
                if (newPanel) {{
                  newPanel.querySelectorAll('script').forEach((oldScript) => {{
                    const script = document.createElement('script');
                    script.textContent = oldScript.textContent;
                    document.body.appendChild(script);
                    script.remove();
                  }});
                  const newBody = newPanel.querySelector('.conversation-body');
                  if (newBody) newBody.scrollTop = newBody.scrollHeight;
                  if (window.loadDigisellerTranslations) window.loadDigisellerTranslations(newPanel);
                }}
              }}
              if (window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
            }} catch (error) {{
              const meta = pending.querySelector('.muted');
              const bubble = pending.querySelector('.chat-bubble');
              if (sent) {{
                if (meta) meta.textContent = '\u5df2\u53d1\u9001';
                if (bubble) bubble.textContent = text || '\u6d88\u606f\u5df2\u53d1\u9001\uff0c\u5237\u65b0\u5bf9\u8bdd\u5931\u8d25';
              }} else {{
                pending.classList.add('send-failed');
                if (meta) meta.textContent = '\u53d1\u9001\u5931\u8d25';
                if (bubble) bubble.textContent = `\u53d1\u9001\u5931\u8d25\uff1a${{error.message || error}}`;
              }}
              if (button) {{
                button.disabled = false;
                button.textContent = originalText;
              }}
            }}
          }}
          root.addEventListener('submit', async (event) => {{
            event.preventDefault();
            await submitChatForm(root, event.submitter || root.querySelector('button[type="submit"]'), textarea.value.trim(), true);
          }});
          if (phrases) {{
            phrases.querySelectorAll('form.common-phrase-card').forEach((form) => {{
              form.addEventListener('submit', async (event) => {{
                event.preventDefault();
                await submitChatForm(form, event.submitter || form.querySelector('button[type="submit"]'), form.dataset.pendingText || '', false);
              }});
            }});
          }}
        }})();
        </script>
        """
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                return self.home()
            if path == "/favicon.ico":
                return self.serve_logo()
            if path.startswith("/assets/"):
                return self.serve_asset(path)
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
            if path == "/coding-prompts":
                return self.coding_prompts_page()
            if path == "/product":
                return self.product()
            if path == "/stock":
                return self.stock()
            if path == "/ggsel":
                return self.ggsel()
            if path == "/ggsel-views":
                return self.ggsel_views()
            if path == "/funpay-boost":
                return self.funpay_boost_page()
            if path == "/unique-code":
                return self.unique_code_page()
            if path == "/download-images":
                return self.download_images()
            if path == "/api/unique-code":
                return self.api_unique_code()
            if path == "/api/unread-count":
                return self.api_unread_count()
            if path == "/api/sales-order-count":
                return self.api_sales_order_count()
            if path == "/api/online-keepalive":
                return self.api_online_keepalive()
            if path == "/api/funpay-boost-status":
                return self.api_funpay_boost_status()
            if path == "/api/funpay-boost-history":
                return self.api_funpay_boost_history()
            if path == "/api/ggsel-products":
                return self.api_ggsel_products()
            if path == "/api/version":
                return self.send_json({"version": APP_VERSION, "file": str(Path(__file__).resolve())})
            if path == "/api/chat-panel":
                return self.api_chat_panel()
            if path == "/api/chat-debug":
                return self.api_chat_debug()
            if path == "/api/m/conversations":
                return self.api_m_conversations()
            if path == "/api/m/messages":
                return self.api_m_messages()
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
            if path == "/chats/send-stock":
                return self.send_stock_reply()
            if path == "/api/stock-draft":
                return self.api_stock_draft()
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
            if path == "/api/funpay-boost-toggle":
                return self.api_funpay_boost_toggle()
            if path == "/api/funpay-boost-sync":
                return self.api_funpay_boost_sync()
            if path == "/api/funpay-boost-clear-errors":
                return self.api_funpay_boost_clear_errors()
            if path == "/api/m/send":
                return self.api_m_send()
            if path == "/api/m/translate":
                return self.api_m_translate()
            return self.send_html("Not found", "<div class='card bad'>Not found</div>", 404)
        except Exception as exc:
            if path == "/chats/send":
                return self.send_json({"ok": False, "error": str(exc)}, 500)
            if path.startswith("/api/m/"):
                return self.send_mobile_json({"ok": False, "error": str(exc)}, 500)
            self.send_html("Error", f"<div class='card bad'>Error</div><pre class='card code'>{h(exc)}</pre>", 500)

    def do_OPTIONS(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/m/"):
            return self.mobile_cors_preflight()
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1024 * 1024:
            raise RuntimeError("JSON body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def api_stock_draft(self) -> None:
        fields, _ = self.read_form()
        order_id = int(fields.get("order_id", "0") or 0)
        platform = fields.get("platform", "digiseller").strip() or "digiseller"
        product = fields.get("product", "").strip()
        if not order_id:
            return self.send_json({"ok": False, "error": "Order ID is missing"}, 400)
        try:
            stock = stock_item_draft_for_chat(order_id, platform, product)
        except Exception as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 500)
        return self.send_json({"ok": True, **stock})

    def send_chat_reply(self) -> None:
        fields, uploads = self.read_form()
        order_id = int(fields.get("order_id", "0") or 0)
        message = fields.get("message", "").strip()
        phrase_id = fields.get("phrase_id", "").strip()
        target_lang = fields.get("target_lang", "").strip() or "en"
        platform = fields.get("platform", "digiseller").strip() or "digiseller"
        stock_offer_id = int(fields.get("stock_offer_id", "0") or 0)
        stock_item_id = int(fields.get("stock_item_id", "0") or 0)
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
        if message and should_translate_outgoing_message(message, target_lang):
            original_message = message
            message, _ = google_translate(message, target_lang, "zh-CN")
            record_sent_original(message, original_message)
        stock_sent = bool(stock_offer_id and stock_item_id)
        sent_flag = "&stock_sent=1" if stock_sent else ""

        def finish(location: str, sent_platform: str) -> None:
            if self.wants_json_response():
                return self.send_json(
                    {
                        "ok": True,
                        "platform": sent_platform,
                        "order_id": order_id,
                        "target_lang": target_lang,
                        "stock_sent": stock_sent,
                    }
                )
            self.redirect(location)

        if platform == "ggsel":
            ggsel_client.send_chat_message(order_id, message, uploads)
            delete_stock_item_after_reply(platform, stock_offer_id, stock_item_id)
            return finish(f"/chats?platform=ggsel&order_id={order_id}&sent=1{sent_flag}&tl={urllib.parse.quote(target_lang)}", "ggsel")
        if platform == "funpay":
            funpay_client.send_chat_message(order_id, message, uploads)
            delete_stock_item_after_reply(platform, stock_offer_id, stock_item_id)
            return finish(f"/chats?platform=funpay&order_id={order_id}&sent=1{sent_flag}&tl={urllib.parse.quote(target_lang)}", "funpay")
        client.send_chat_message(order_id, message, uploads)
        delete_stock_item_after_reply("digiseller", stock_offer_id, stock_item_id)
        return finish(f"/chats?order_id={order_id}&sent=1{sent_flag}&tl={urllib.parse.quote(target_lang)}", "digiseller")

    def send_stock_reply(self) -> None:
        raise RuntimeError("Stock replenishment now fills the reply box first. Refresh the chat, click Replenish, then click Send Reply.")

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
        elif online.get("verified_online") or online.get("public_online") or online.get("ggsel_verified_online"):
            online_state = "<span class='ok'>verified online</span>"
        elif online.get("last_set") or online.get("last_heartbeat") or online.get("ggsel_last_set") or online.get("ggsel_last_heartbeat"):
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
            f"<br><b>GGSEL</b>: {h('verified online' if online.get('ggsel_verified_online') else 'heartbeat sent' if online.get('ggsel_last_set') or online.get('ggsel_last_heartbeat') else 'not active')}"
            f"<br>GGSEL last set: {h(online.get('ggsel_last_set') or '-')}"
            f"<br>GGSEL last chat heartbeat: {h(online.get('ggsel_last_heartbeat') or '-')}"
            f"<br>GGSEL buyer-visible status: {h(online.get('ggsel_status') if online.get('ggsel_status') is not None else '-')}"
            f"<br>GGSEL setting: {h(online.get('ggsel_setting') if online.get('ggsel_setting') is not None else '-')}"
            f" · period: {h(online.get('ggsel_period') if online.get('ggsel_period') is not None else '-')}"
            f"<br>Set error: {h(online.get('set_error') or '-')}"
            f"<br>Chat heartbeat error: {h(online.get('heartbeat_error') or '-')}"
            f"<br>Setting error: {h(online.get('setting_error') or '-')}"
            f"<br>Verify error: {h(online.get('verify_error') or '-')}"
            f"<br>GGSEL set error: {h(online.get('ggsel_set_error') or '-')}"
            f"<br>GGSEL chat heartbeat error: {h(online.get('ggsel_heartbeat_error') or '-')}"
            f"<br>GGSEL setting error: {h(online.get('ggsel_setting_error') or '-')}"
            f"<br>GGSEL verify error: {h(online.get('ggsel_verify_error') or '-')}"
            f"<br>Public verify error: {h(online.get('public_error') or '-')}"
            f"<br>Recovering: {h(online.get('recovery_error') or '-')}"
            f"<br>Error: {h(online.get('last_error') or '-')}</div>"
        )
        body = f"""
        <div class='card'><h2>Digiseller Local Admin</h2><p>Config: {status}</p><p class='muted'>API Key is read from <code>.env</code>; never paste it into code or chat.</p></div>
        <div class='grid'>{login_info}{online_info}<div class='stat'><b>Quick links</b><br><a href='/sales'>Recent sales</a><br><a href='/unread'>Unread messages</a><br><a href='/chats'>Buyer chats</a><br><a href='/ggsel'>GGSEL catalog</a></div></div>
        """
        self.send_html("Dashboard", body)

    def funpay_boost_page(self) -> None:
        sync_result: dict[str, Any] | None = None
        sync_error = ""
        product_names: dict[int, str] = {}
        if funpay_client.configured():
            try:
                sync_result = sync_funpay_boost_active_categories()
                product_names = {
                    int(node_id): product_name
                    for node_id, product_name in sync_result.get("product_names", {}).items()
                    if int(node_id)
                }
            except Exception as exc:
                sync_error = str(exc)
        status = funpay_boost_snapshot()
        rows = funpay_boost_history_rows()
        if rows:
            row_html = []
            for row in rows:
                node_id = int(row.get("node_id") or 0)
                product_name = str(row.get("product_name") or product_names.get(node_id) or "-")
                status_text = str(row.get("status") or "-")
                status_class = "ok" if status_text == "boosted" else "bad" if status_text == "failed" else "muted"
                remaining = funpay_boost_duration_label(row.get("remaining_seconds"))
                cooldown_until_ts = float(row.get("cooldown_until_ts") or 0)
                row_html.append(
                    "<tr>"
                    f"<td><a href='https://funpay.com/en/lots/{node_id}/trade' target='_blank' rel='noopener'>#{node_id}</a></td>"
                    f"<td>{h(short(product_name, 90))}</td>"
                    f"<td class='{status_class}'>{h(status_text)}</td>"
                    f"<td>{h(row.get('last_boost_at') or '-')}</td>"
                    f"<td class='cooldown-remaining' data-cooldown-until='{h(cooldown_until_ts)}'>{h(remaining)}</td>"
                    f"<td>{h(row.get('cooldown_until') or '-')}</td>"
                    f"<td>{h(row.get('last_checked_at') or '-')}</td>"
                    f"<td>{h(row.get('message') or '-')}</td>"
                    "</tr>"
                )
            rows_html = "".join(row_html)
        else:
            rows_html = "<tr><td colspan='8' class='muted'>还没有 FunPay Boost 记录。开启左下角按钮后，第一次检查会写入所有商品分类的状态。</td></tr>"
        last_result = status.get("last_result") if isinstance(status.get("last_result"), dict) else None
        last_result_rows_html = ""
        if status.get("running"):
            last_result_summary = "本次检测正在运行：正在检查冷却并尝试提升。"
            last_result_class = "muted"
        elif status.get("last_error"):
            last_result_summary = f"本次检测失败：{h(status.get('last_error'))}"
            last_result_class = "bad"
        elif last_result:
            total = int(last_result.get("total") or 0)
            success = int(last_result.get("success") or 0)
            skipped = int(last_result.get("skipped") or 0)
            failed = int(last_result.get("failed") or 0)
            if total:
                last_result_summary = f"本次检测完成：共检查 {total} 个分类，成功提升 {success} 个，冷却/跳过 {skipped} 个，失败 {failed} 个。"
            else:
                last_result_summary = "本次检测完成：没有找到可提升的 FunPay 商品分类。"
            last_result_class = "bad" if failed else "ok"
            result_items = last_result.get("results")
            if isinstance(result_items, list) and result_items:
                result_rows = []
                for item in result_items:
                    if not isinstance(item, dict):
                        continue
                    node_value = item.get("node_id") or "-"
                    try:
                        product_name = str(item.get("product_name") or product_names.get(int(item.get("node_id") or 0)) or "-")
                    except (TypeError, ValueError):
                        product_name = str(item.get("product_name") or "-")
                    item_status = str(item.get("status") or "-")
                    item_class = "ok" if item_status == "success" else "bad" if item_status == "failed" else "muted"
                    result_rows.append(
                        "<tr>"
                        f"<td>{h(node_value)}</td>"
                        f"<td>{h(short(product_name, 90))}</td>"
                        f"<td class='{item_class}'>{h(item_status)}</td>"
                        f"<td>{h(item.get('message') or '-')}</td>"
                        "</tr>"
                    )
                last_result_rows_html = "".join(result_rows)
        else:
            last_result_summary = "还没有执行检测。点击左下角 Start 后会立刻检测一次。"
            last_result_class = "muted"
        if not last_result_rows_html:
            last_result_rows_html = "<tr><td colspan='4' class='muted'>本次没有单个分类结果。</td></tr>"

        enabled = "开启" if status.get("enabled") else "关闭"
        running = "运行中" if status.get("running") else "空闲"
        if sync_error:
            sync_summary = f"<span class='bad'>同步失败：{h(sync_error)}</span>"
        elif sync_result:
            sync_summary = (
                f"<span class='ok'>已同步 FunPay 当前 {h(sync_result.get('total') or 0)} 个商品分类；"
                f"新增 {h(sync_result.get('added') or 0)} 个，更新 {h(sync_result.get('updated') or 0)} 个。</span>"
            )
        elif funpay_client.configured():
            sync_summary = "<span class='muted'>尚未同步。</span>"
        else:
            sync_summary = "<span class='muted'>未配置 FUNPAY_GOLDEN_KEY，不能同步。</span>"
        body = f"""
        <div class='card'>
          <h2>FunPay Boost 冷却记录</h2>
          <p class='muted'>这里集中记录每个 FunPay 商品分类的上次提升时间，以及从该时间推算出的冷却剩余时间。</p>
          <p>自动 Boost：<b>{h(enabled)}</b> · 当前状态：<b>{h(running)}</b> · 上次检查：{h(status.get('last_run') or '-')} · 下次12小时检查：{h(status.get('next_run') or '-')}</p>
          <p>冷却中：<b>{h(status.get('cooling_count') or 0)}</b> · 已提升记录：<b>{h(status.get('boosted_count') or 0)}</b> · 错误记录：<b class='bad'>{h(status.get('failed_count') or 0)}</b></p>
          <p>商品同步：{sync_summary}</p>
          <p><button id='funpay-boost-sync-products' type='button'>立即同步商品分类</button> <span id='funpay-boost-sync-message' class='muted'></span></p>
          <p>错误：<span class='bad'>{h(status.get('last_error') or '-')}</span></p>
        </div>
        <div class='card'>
          <h3>本次检测结果</h3>
          <p class='{last_result_class}'>{last_result_summary}</p>
          <table>
            <thead><tr><th>商品分类</th><th>商品名称</th><th>状态</th><th>结果</th></tr></thead>
            <tbody>{last_result_rows_html}</tbody>
          </table>
        </div>
        <div class='card'>
          <table>
            <thead><tr><th>商品分类</th><th>商品名称</th><th>状态</th><th>上次提升时间</th><th>剩余冷却</th><th>下次可提升时间</th><th>上次检测</th><th>结果</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        <script>
        (() => {{
          function formatRemaining(seconds) {{
            if (!Number.isFinite(seconds)) return '未知';
            if (seconds <= 0) return '已冷却';
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const secs = Math.floor(seconds % 60);
            if (hours > 0) return `${{hours}}小时 ${{minutes}}分钟`;
            if (minutes > 0) return `${{minutes}}分钟 ${{secs}}秒`;
            return `${{secs}}秒`;
          }}
          function tick() {{
            document.querySelectorAll('.cooldown-remaining').forEach((node) => {{
              const until = Number(node.dataset.cooldownUntil || 0);
              node.textContent = until ? formatRemaining(until - Date.now() / 1000) : '未知';
            }});
          }}
          tick();
          const cooldownTimer = setInterval(tick, 1000);
          if (window.pjaxOnCleanup) window.pjaxOnCleanup(() => clearInterval(cooldownTimer));
          const syncButton = document.getElementById('funpay-boost-sync-products');
          const syncMessage = document.getElementById('funpay-boost-sync-message');
          if (syncButton) syncButton.addEventListener('click', async () => {{
            syncButton.disabled = true;
            if (syncMessage) syncMessage.textContent = '正在同步...';
            try {{
              const res = await fetch('/api/funpay-boost-sync', {{method: 'POST'}});
              const data = await res.json();
              if (!res.ok) throw new Error(data.error || '同步失败');
              const sync = data.sync || {{}};
              if (syncMessage) syncMessage.textContent = `同步完成：当前 ${{sync.total || 0}} 个分类，新增 ${{sync.added || 0}} 个，更新 ${{sync.updated || 0}} 个。`;
              window.location.reload();
            }} catch (error) {{
              if (syncMessage) syncMessage.textContent = String(error.message || error);
            }} finally {{
              syncButton.disabled = false;
            }}
          }});
        }})();
        </script>
        """
        self.send_html("FunPay Boost", body, include_funpay_boost=True)

    def ggsel_product_rows(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("rows", "products", "goods", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                rows = self.ggsel_product_rows(value)
                if rows:
                    return rows
        return []

    def ggsel_product_id(self, row: dict[str, Any]) -> Any:
        for key in ("id_goods", "ggsel_id", "id", "product_id"):
            if row.get(key):
                return row.get(key)
        return ""

    def ggsel_product_name(self, row: dict[str, Any]) -> str:
        for key in ("name_goods", "name", "title", "product_name"):
            if row.get(key):
                return str(row.get(key))
        return ""

    def ggsel(self) -> None:
        configured = ggsel_client.configured()
        status = "<span class='ok'>configured</span>" if configured else "<span class='bad'>missing GGSEL_API_KEY or GGSEL_SELLER_ID</span>"
        count = min(max(int(self.q("count", "20") or "20"), 1), 100)
        sales_data: dict[str, Any] = {}
        chats_data: dict[str, Any] = {}
        reviews_data: dict[str, Any] = {}
        api_error = ""
        login_info = ""
        if configured:
            try:
                login_data = ggsel_client.login()
                login_info = f"<div class='stat'>API login: <span class='ok'>OK</span><br>seller_id: {h(login_data.get('seller_id'))}<br>valid_thru: {h(login_data.get('valid_thru'))}</div>"
                sales_data = ggsel_client.sales(count)
                chats_data = ggsel_client.chats(count)
                reviews_data = ggsel_client.reviews(count)
            except Exception as exc:
                api_error = str(exc)
        sales_rows = []
        for sale in sales_data.get("sales") or []:
            product = sale.get("product") if isinstance(sale.get("product"), dict) else {}
            product_id = product.get("id") or sale.get("product_id") or sale.get("id_goods")
            product_url = ggsel_client.product_url(product_id)
            invoice_id = sale.get("invoice_id")
            product_name = product.get("name") or sale.get("name") or f"GGSEL product {product_id}"
            amount, currency = ggsel_sale_order_amount(sale)
            chat_href = "/chats?" + urllib.parse.urlencode({"platform": "ggsel", "order_id": str(invoice_id or ""), "email": str(sale.get("email") or f"ggsel-{invoice_id}"), "product": str(product_name)})
            sales_rows.append([
                h(sale.get("date")),
                f"<a class='order-link' href='{h(chat_href)}'>{h(invoice_id)}</a>",
                f"<a href='{h(product_url)}' target='_blank'>{h(product_id)}</a>" if product_url else h(product_id),
                h(short(product_name, 100)),
                h(f"{amount} {currency}".strip()),
            ])
        chat_rows = []
        for chat in chats_data.get("items") or chats_data.get("chats") or []:
            invoice_id = chat.get("id_i") or chat.get("invoice_id")
            order_id = int(invoice_id or 0)
            raw_product = chat.get("product") or chat.get("id_goods")
            product_id = ggsel_order_product_id(order_id, raw_product) if order_id else clean_text(raw_product)
            product_url = ggsel_client.product_url(product_id)
            product_name = ggsel_order_product_name(order_id, raw_product) if order_id else "GGSEL order"
            email = ggsel_order_buyer_email(order_id, chat.get("email") or f"ggsel-{invoice_id}") if order_id else str(chat.get("email") or f"ggsel-{invoice_id}")
            chat_href = "/chats?" + urllib.parse.urlencode({"platform": "ggsel", "order_id": str(invoice_id or ""), "email": email, "product": product_name})
            chat_rows.append([
                h(chat.get("last_message") or chat.get("date")),
                f"<a class='order-link' href='{h(chat_href)}'>{h(invoice_id)}</a>",
                h(email),
                f"<a href='{h(product_url)}' target='_blank'>{h(product_id)}</a>" if product_url else h(product_id),
                h(chat.get("cnt_new") or ""),
            ])
        review_rows = []
        for review in reviews_data.get("reviews") or []:
            product_id = review.get("good") or review.get("product_id")
            product_url = ggsel_client.product_url(product_id)
            review_rows.append([
                h(review.get("date")),
                h(review.get("type")),
                h(review.get("invoice_id")),
                f"<a href='{h(product_url)}' target='_blank'>{h(product_id)}</a>" if product_url else h(product_id),
                h(short(review.get("name") or "", 90)),
                h(short(review.get("info") or review.get("comment") or "", 120)),
            ])
        form = f"""
        <form class='card'>
          <h2>GGSEL</h2>
          <p>Config: {status}</p>
          <p class='muted'>Reads <code>GGSEL_API_KEY</code> and <code>GGSEL_SELLER_ID</code> from <code>.env</code>. API keys are never stored in code.</p>
          <label>Rows <input name='count' value='{count}' size='4'></label>
          <button>Refresh</button>
          <a href='/ggsel-views'>View GGSEL product views</a>
          <p class='muted'>API: <code>{h(ggsel_client.api_base)}</code> · seller_id: <code>{h(ggsel_client.seller_id or '-')}</code></p>
        </form>
        """
        error_html = f"<div class='card bad'>GGSEL API error:<pre class='code'>{h(api_error)}</pre></div>" if api_error else ""
        body = (
            form
            + error_html
            + f"<div class='grid'>{login_info}<div class='stat'><b>Recent sales</b><br>{h(len(sales_rows))} rows</div><div class='stat'><b>Recent chats</b><br>{h(len(chat_rows))} rows</div><div class='stat'><b>Reviews</b><br>{h(len(review_rows))} rows</div></div>"
            + "<div class='card'><h3>Recent GGSEL sales</h3></div>"
            + table(["Date", "Invoice", "Product ID", "Product", "USD"], sales_rows)
            + "<div class='card'><h3>Recent GGSEL chats</h3></div>"
            + table(["Last message", "Invoice", "Email", "Product", "Unread"], chat_rows)
            + "<div class='card'><h3>Recent GGSEL reviews</h3></div>"
            + table(["Date", "Type", "Invoice", "Product ID", "Product", "Text"], review_rows)
        )
        self.send_html("GGSEL", body)

    def ggsel_views(self) -> None:
        today = dt.date.today()
        default_start = today - dt.timedelta(days=30)
        start_date = parse_ymd_date(self.q("date_start", ""), default_start)
        end_date = parse_ymd_date(self.q("date_end", ""), today)
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        raw_id = int_from_query(self.q("product_id", ""))
        id_type = clean_text(self.q("id_type", "product"))
        if id_type not in ("product", "offer"):
            id_type = "product"
        configured = ggsel_client.seller_office_configured()
        status = "<span class='ok'>configured</span>" if configured else "<span class='bad'>missing GGSEL_SELLER_COOKIE</span>"
        offer_id = raw_id if id_type == "offer" else None
        offer_title = ""
        lookup_note = ""
        api_error = ""
        dashboard: dict[str, Any] = {}
        if configured and raw_id and id_type == "product":
            try:
                offer = ggsel_client.seller_office_offer_by_ggsel_id(raw_id)
                offer_id = int_from_query(offer.get("id") or offer.get("offer_id")) or raw_id
                offer_title = clean_text(offer.get("title") or offer.get("name") or offer.get("name_goods"))
                if offer_id == raw_id and not offer:
                    lookup_note = "未找到商品 ID 到 Offer ID 的映射，已按 Offer ID 直接查询。"
            except Exception as exc:
                offer_id = raw_id
                lookup_note = f"商品 ID 映射失败，已按 Offer ID 直接查询：{short(exc, 160)}"
        if configured:
            try:
                offer_ids = [offer_id] if offer_id else None
                dashboard = ggsel_client.seller_office_dashboard(start_date, end_date, offer_ids)
            except Exception as exc:
                api_error = str(exc)
        statistic = dashboard.get("statistic") if isinstance(dashboard.get("statistic"), dict) else {}
        sales_chart = dashboard.get("sales") if isinstance(dashboard.get("sales"), dict) else {}
        views_value = stat_value(statistic, "views", "value") or sales_chart.get("views_count")
        revenue_value = stat_value(statistic, "revenue", "value")
        sales_value = stat_value(statistic, "sales", "value") or sales_chart.get("sales_count")
        conversion_value = stat_value(statistic, "conversion", "value")
        press_buy_value = sales_chart.get("press_buy_count")
        date_rows = []
        date_range = sales_chart.get("date_range") if isinstance(sales_chart.get("date_range"), list) else []
        for item in date_range:
            if not isinstance(item, dict):
                continue
            date_rows.append([
                h(item.get("date")),
                format_stat(item.get("views_count")),
                format_stat(item.get("press_buy_count")),
                format_stat(item.get("sales_count")),
            ])
        id_label = "GGSEL 商品 ID" if id_type == "product" else "Offer ID"
        title_html = f"<p class='muted'>当前商品：{h(offer_title)}</p>" if offer_title else ""
        note_html = f"<p class='muted'>{h(lookup_note)}</p>" if lookup_note else ""
        error_html = f"<div class='card bad'>GGSEL dashboard API error:<pre class='code'>{h(api_error)}</pre></div>" if api_error else ""
        form = f"""
        <form class='card'>
          <h2>GGSEL 浏览统计</h2>
          <p>Config: {status}</p>
          <p class='muted'>使用 GGSEL 卖家后台 dashboard 接口：<code>/api_seller_office/v1/dashboard</code>。接口返回的是商品页访问量 <code>views</code>，不是 Products 列表里的销量。</p>
          <label>开始日期 <input type='date' name='date_start' value='{h(start_date.isoformat())}'></label>
          <label>结束日期 <input type='date' name='date_end' value='{h(end_date.isoformat())}'></label>
          <label>类型
            <select name='id_type'>
              <option value='product' {'selected' if id_type == 'product' else ''}>GGSEL 商品 ID</option>
              <option value='offer' {'selected' if id_type == 'offer' else ''}>Offer ID</option>
            </select>
          </label>
          <label>{h(id_label)} <input name='product_id' value='{h(raw_id or "")}' placeholder='留空查看全部商品汇总'></label>
          <button>查询</button>
          <p class='muted'>需要 <code>GGSEL_SELLER_COOKIE</code>。查询单个商品时，会先用商品 ID 换算 Offer ID，再传给 <code>offer_ids[]</code>。</p>
          {title_html}
          {note_html}
        </form>
        """
        stats = f"""
        <div class='grid'>
          <div class='stat'><b>{format_stat(views_value)}</b><br>Views / 商品页访问量</div>
          <div class='stat'><b>{format_stat(press_buy_value)}</b><br>Payment transitions / 点击购买</div>
          <div class='stat'><b>{format_stat(sales_value)}</b><br>Sales / 订单数</div>
          <div class='stat'><b>{format_stat(revenue_value, ' USD')}</b><br>Revenue / 销售额</div>
          <div class='stat'><b>{format_stat(conversion_value, '%')}</b><br>Conversion / 转化率</div>
        </div>
        """
        body = (
            form
            + error_html
            + stats
            + "<div class='card'><h3>Daily detail</h3><p class='muted'>每日访问量、点击购买和订单数来自 dashboard 的 <code>sales.date_range</code>。</p></div>"
            + table(["Date", "Views", "Payment transitions", "Sales"], date_rows)
        )
        self.send_html("GGSEL 浏览统计", body)

    def api_ggsel_products(self) -> None:
        count = min(max(int(self.q("count", "20") or "20"), 1), 100)
        if not ggsel_client.configured():
            return self.send_json({"ok": False, "error": "GGSEL_API_KEY or GGSEL_SELLER_ID is missing"}, 400)
        try:
            self.send_json({
                "ok": True,
                "sales": ggsel_client.sales(count),
                "chats": ggsel_client.chats(count),
                "reviews": ggsel_client.reviews(count),
            })
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 502)

    def coding_prompts_page(self) -> None:
        source_rows = "".join(
            f"<li><a href='{h(url)}' target='_blank' rel='noreferrer'>{h(name)}</a>"
            f" <span class='muted'>{h(note)}</span></li>"
            for name, url, note in PROMPT_SOURCE_REPOS
        )
        prompt_cards = ""
        for item in CODING_PROMPT_TEMPLATES:
            prompt_cards += (
                "<div class='prompt-card'>"
                f"<div class='prompt-card-head'><h3>{h(item['title'])}</h3>"
                "<button class='prompt-copy' type='button'>复制</button></div>"
                f"<p class='muted'>{h(item['use'])}</p>"
                f"<pre class='code prompt-text'>{h(item['prompt'])}</pre>"
                "</div>"
            )
        body = f"""
        <style>
        .prompt-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}}
        .prompt-card{{border:1px solid #e2e8f0;border-radius:12px;background:#fff;padding:14px}}
        .prompt-card-head{{display:flex;align-items:center;justify-content:space-between;gap:10px}}
        .prompt-card-head h3{{margin:0}}
        .prompt-copy{{background:#e0ecff;color:#0f3b66;border-color:#b9d4ff}}
        .prompt-copy.copied{{background:#dcfce7;color:#166534;border-color:#bbf7d0}}
        .prompt-text{{white-space:pre-wrap;line-height:1.55}}
        .prompt-steps li{{margin:6px 0}}
        </style>
        <div class='card'>
          <h2>AI 编程提示词</h2>
          <p class='muted'>目标是让 AI 先理解项目，再用最小代码改动完成任务。这里没有直接复制第三方仓库内容，只整理可直接使用的通用提示词和参考链接。</p>
          <ol class='prompt-steps'>
            <li>先复制“只读检查”提示词，让 AI 只看代码并给最小方案。</li>
            <li>确认方案后，再复制“严格按最小改动实现”。</li>
            <li>每次只做一个小目标，避免把修 bug、重构、加功能混在一起。</li>
            <li>提交前用“提交前自查”，再运行项目已有检查命令。</li>
          </ol>
        </div>
        <div class='card'>
          <h3>GitHub 参考仓库</h3>
          <ul>{source_rows}</ul>
          <p class='muted'>如果要复制第三方仓库里的原文，请先检查对应 LICENSE；当前页面仅作为链接和改写后的模板库。</p>
        </div>
        <div class='prompt-grid'>{prompt_cards}</div>
        <script>
        (() => {{
          async function copyText(text) {{
            if (navigator.clipboard && window.isSecureContext) {{
              await navigator.clipboard.writeText(text);
              return;
            }}
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.left = '-9999px';
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            document.execCommand('copy');
            textarea.remove();
          }}
          document.querySelectorAll('.prompt-copy').forEach((button) => {{
            button.addEventListener('click', async () => {{
              const card = button.closest('.prompt-card');
              const text = card?.querySelector('.prompt-text')?.textContent || '';
              await copyText(text.trim());
              button.textContent = '已复制';
              button.classList.add('copied');
              setTimeout(() => {{
                button.textContent = '复制';
                button.classList.remove('copied');
              }}, 1200);
            }});
          }});
        }})();
        </script>
        """
        self.send_html("AI coding prompts", body)

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
                img.title = button.dataset.previewSrc || '';
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
        def parse_int(name: str, default: int) -> int:
            try:
                return int(self.q(name, str(default)) or default)
            except ValueError:
                return default

        days = RECENT_ORDER_DAYS
        rows_limit = min(max(parse_int("rows", 50), 1), 50)
        page = max(parse_int("page", 1), 1)
        errors: list[str] = []
        digiseller_data: dict[str, Any] = {"rows": [], "total_rows": 0, "pages": 1}
        try:
            digiseller_data = client.sales(days, rows_limit, page)
        except Exception as exc:
            errors.append(f"Digiseller: {exc}")

        digiseller_rows = [row for row in digiseller_data.get("rows", []) if isinstance(row, dict)][:rows_limit]
        if digiseller_rows:
            mark_sales_orders_seen(digiseller_rows)

        ggsel_rows: list[dict[str, Any]] = []
        if ggsel_client.configured():
            try:
                ggsel_data = ggsel_client.sales(100)
                for sale in ggsel_data.get("sales") or []:
                    if not isinstance(sale, dict):
                        continue
                    if not is_recent_time(sale.get("date"), days):
                        continue
                    invoice_id = sale.get("invoice_id")
                    product = sale.get("product") if isinstance(sale.get("product"), dict) else {}
                    product_id = product.get("id") or sale.get("product_id") or sale.get("id_goods")
                    amount, currency = ggsel_sale_order_amount(sale)
                    ggsel_rows.append(
                        {
                            "source": "GGSEL",
                            "date_pay": sale.get("date"),
                            "invoice_id": invoice_id,
                            "email": sale.get("email") or "",
                            "product_id": product_id,
                            "product_name": product.get("name") or sale.get("product_name") or sale.get("name") or f"GGSEL product {product_id}",
                            "amount_in": amount,
                            "amount_currency": currency,
                            "partner_id": sale.get("partner_id") or "-",
                            "referer": "ggsel.net",
                            "platform": "ggsel",
                        }
                    )
            except Exception as exc:
                errors.append(f"GGSEL: {exc}")
        else:
            errors.append("GGSEL: GGSEL_API_KEY or GGSEL_SELLER_ID missing")
        funpay_rows: list[dict[str, Any]] = []
        if funpay_client.configured():
            try:
                funpay_rows = funpay_client.recent_orders(order_days=days, chat_days=RECENT_CHAT_DAYS, limit=50)
            except Exception as exc:
                errors.append(f"FunPay: {exc}")
        else:
            errors.append("FunPay: FUNPAY_GOLDEN_KEY missing")

        for row in digiseller_rows:
            row.setdefault("source", "Digiseller")
            row.setdefault("platform", "digiseller")
        order_rows = sorted(digiseller_rows + ggsel_rows + funpay_rows, key=lambda row: sort_time(row.get("date_pay") or row.get("date")), reverse=True)[:rows_limit]
        totals: dict[str, float] = {}
        platform_counts: dict[str, int] = {}
        for row in order_rows:
            platform = str(row.get("source") or row.get("platform") or "?")
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
            currency = str(row.get("amount_currency") or "").strip() or "?"
            try:
                amount = float(str(row.get("amount_in") or 0).replace(",", "."))
            except ValueError:
                amount = 0.0
            totals[currency] = totals.get(currency, 0.0) + amount
        totals_text = ", ".join(f"{value:.2f} {currency}" for currency, value in sorted(totals.items())) or "-"
        platform_text = ", ".join(f"{name}: {count}" for name, count in sorted(platform_counts.items())) or "-"
        trs = []
        for row in order_rows:
            invoice_id = row.get("invoice_id")
            product_id = row.get("product_id")
            product_name = row.get("product_name")
            platform = str(row.get("platform") or "digiseller")
            source = "GGSEL" if platform == "ggsel" else "FunPay" if platform == "funpay" else "Digiseller"
            buyer_email = next((row.get(key) for key in ("email", "buyer_email", "user_email", "client_email") if row.get(key)), "")
            if platform == "ggsel":
                chat_href = "/chats?" + urllib.parse.urlencode({"platform": "ggsel", "order_id": str(invoice_id or ""), "email": str(buyer_email or f"ggsel-{invoice_id}"), "product": str(product_name or "GGSEL order")})
            elif platform == "funpay":
                chat_node_id = row.get("chat_node_id") or invoice_id
                chat_href = "/chats?" + urllib.parse.urlencode({"platform": "funpay", "order_id": str(chat_node_id or ""), "email": str(buyer_email or f"FunPay-{chat_node_id}"), "product": str(product_name or "FunPay order")})
            else:
                chat_href = order_chat_href(invoice_id, buyer_email, product_name)
            amount = f"{row.get('amount_in') or ''} {row.get('amount_currency') or ''}".strip()
            search_text = " ".join(str(value or "") for value in [source, invoice_id, product_id, product_name, buyer_email, amount, row.get("partner_id"), row.get("referer")]).lower()
            platform_class = "ggsel" if platform == "ggsel" else "funpay" if platform == "funpay" else "digiseller"
            trs.append(
                "<tr data-search='" + h(search_text) + "'>"
                + f"<td class='sales-source'><span class='platform-badge {platform_class}'>{h(source)}</span></td>"
                + f"<td>{h(row.get('date_pay') or row.get('date'))}</td>"
                + f"<td><a class='order-link' href='{h(chat_href)}'>{h(invoice_id)}</a></td>"
                + f"<td>{h(buyer_email or '-')}</td>"
                + f"<td>{h(product_id)}</td>"
                + f"<td class='sales-product'>{h(short(product_name, 110))}</td>"
                + f"<td>{h(amount)}</td>"
                + f"<td>{h(row.get('partner_id') or '-')}</td>"
                + f"<td>{h(short(row.get('referer'), 80) or '-')}</td>"
                + f"<td><a class='chat-action' href='{h(chat_href)}'>Chat</a></td>"
                + "</tr>"
            )
        head = "".join(f"<th>{h(value)}</th>" for value in ["Platform", "Paid", "Order", "Buyer", "Product ID", "Product", "Amount", "Partner", "Referer", "Action"])
        table_html = f"<table class='sales-table'><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table><div id='sales-empty-filter' class='sales-empty-filter'>No matching orders</div>"
        errors_html = "".join(f"<div class='notice bad-bg'>{h(error)}</div>" for error in errors)
        form = f"""
        <div class='card'>
          <h2>Orders</h2>
          <form class='sales-toolbar'>
            <label>Rows <input name='rows' value='{rows_limit}' size='4'></label>
            <label>Page <input name='page' value='{page}' size='4'></label>
            <input id='sales-search' class='sales-search' placeholder='Search platform, order, buyer, product, referer...' autocomplete='off'>
            <button>Refresh</button>
          </form>
          <p class='muted'>Showing paid orders from the last {RECENT_ORDER_DAYS} days; rows are capped at 50 per request and merged by paid time.</p>
          {errors_html}
        </div>
        <div class='sales-summary'>
          <div class='sales-stat'><b>{h(len(order_rows))}</b><span class='muted'>orders shown</span></div>
          <div class='sales-stat'><b>{h(platform_text)}</b><span class='muted'>platforms shown</span></div>
          <div class='sales-stat'><b>{h(totals_text)}</b><span class='muted'>shown amount</span></div>
          <div class='sales-stat'><b>{h(page)} / {h(digiseller_data.get('pages') or 1)}</b><span class='muted'>Digiseller page</span></div>
        </div>
        """
        script = """
        <script>
        (() => {
          const search = document.getElementById('sales-search');
          const rows = [...document.querySelectorAll('.sales-table tbody tr')];
          const empty = document.getElementById('sales-empty-filter');
          if (!search) return;
          function applyFilter() {
            const query = search.value.trim().toLowerCase();
            let visible = 0;
            rows.forEach((row) => {
              const show = !query || (row.dataset.search || row.textContent.toLowerCase()).includes(query);
              row.hidden = !show;
              if (show) visible += 1;
            });
            if (empty) empty.classList.toggle('visible', visible === 0);
          }
          search.addEventListener('input', applyFilter);
        })();
        </script>
        """
        self.send_html("Sales", form + table_html + script)

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
                f"<div class='conversation-header-side'>{options_html}</div>"
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
                    elif is_seller:
                        text_html = message_text_html(seller_bubble_text(text), allow_save=True)
                    else:
                        text_html = translate_incoming_html(text, msg.get("id"), should_translate=True)
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
            if not rows:
                rows.append("<div class='empty-state'>No order messages yet. Send a reply below to start the order chat.</div>")
            notice = ""
            if self.q("stock_sent") == "1":
                notice = "<div class='notice ok-bg'>&#24050;&#34917;&#36135;&#65292;&#24182;&#20174;&#24211;&#23384;&#20013;&#31227;&#38500;&#19968;&#26465;&#21830;&#21697;&#12290;</div>"
            elif self.q("sent") == "1":
                notice = "<div class='notice ok-bg'>&#22238;&#22797;&#24050;&#21457;&#36865;&#65292;&#27491;&#22312;&#26174;&#31034;&#26368;&#26032;&#23545;&#35805;&#12290;</div>"
            return f"<div id='chat-panel' class='conversation-panel' data-kind='order' data-order-id='{selected_order}' data-message-count='{len(selected_messages)}'><div class='conversation-header'>{header}</div><div class='conversation-body'>{notice}{''.join(rows)}</div>{self.reply_editor(selected_order, buyer_lang, email=str(selected_chat.get('email') or ''), product=str(selected_chat.get('product') or ''))}</div>"
        return "<div id='chat-panel' class='conversation-panel' data-kind='order'><div class='empty-state'>No chats found</div></div>"

    def loading_chat_panel_html(self, selected_kind: str, selected_platform: str, selected_order: int, selected_chat: dict[str, Any] | None, selected_guest_chat: dict[str, Any] | None) -> str:
        title = "加载中"
        preview = "正在加载订单消息"
        attrs = f"data-kind='{h(selected_kind)}'"
        if selected_kind == "guest" and selected_guest_chat:
            title = str(selected_guest_chat.get("Name") or "Guest")
            preview = str(selected_guest_chat.get("PurchaseName") or "Pre-order consultation")
        elif selected_order and selected_chat:
            title = str(selected_chat.get("email") or selected_chat.get("name") or f"order-{selected_order}").split("@", 1)[0]
            preview = str(selected_chat.get("product") or "Direct order lookup")
            attrs += f" data-platform='{h(selected_platform)}' data-order-id='{selected_order}'"
        return (
            f"<div id='chat-panel' class='conversation-panel loading' {attrs}>"
            "<div class='conversation-header'><div class='conversation-header-main'>"
            f"<div class='conversation-header-title'>{h(title)}</div>"
            f"<div class='muted'>{h(short(preview, 120))}</div></div></div>"
            "<div class='chat-loading'><div class='chat-loading-spinner'></div>"
            "<div class='chat-loading-title'>正在加载消息...</div>"
            "<div class='chat-loading-subtitle'>订单消息会异步加载，不刷新整个页面</div>"
            "<div class='loading-lines'><div class='loading-line'></div><div class='loading-line short'></div><div class='loading-line'></div></div>"
            "</div></div>"
        )

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
            elif is_seller:
                text_html = message_text_html(seller_bubble_text(text), allow_save=True)
            else:
                text_html = translate_incoming_html(text, msg.get("id"), should_translate=True)
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

    def ggsel_chat_panel_html(self, selected_order: int, selected_chat: dict[str, Any], selected_messages: list[dict[str, Any]]) -> str:
        buyer_email = ggsel_order_buyer_email(selected_order, selected_chat.get("email") or f"GGSEL-{selected_order}")
        buyer_name = buyer_email.split("@", 1)[0]
        product = ggsel_order_product_name(selected_order, selected_chat.get("product") or "GGSEL order")
        buyer_lang = detect_buyer_language(selected_messages)
        options_html = ggsel_order_options_html(selected_order, selected_chat)
        header = (
            f"<div class='conversation-header-main'><div class='conversation-header-title'>{h(buyer_name)}</div>"
            f"<div class='muted'>GGSEL Order {h(selected_order)} · {h(short(product, 110))} · Messages loaded: {len(selected_messages)} · Reply language: {h(lang_label(buyer_lang))}</div></div>"
            f"<div class='conversation-header-side'>{options_html}</div>"
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
                    text_html = attachment_html(msg, allow_guess_preview=True)
                elif is_seller:
                    text_html = message_text_html(seller_bubble_text(text), allow_save=True)
                else:
                    text_html = translate_incoming_html(text, msg.get("id"), should_translate=True)
            except Exception as exc:
                text_html = h(text or f"Message render error: {exc}")
            msg_no = f"#{idx}/{total_messages}"
            msg_id = f" · ID {h(msg.get('id'))}" if msg.get("id") else ""
            rows.append(
                f"<div class='chat-row {cls}'>"
                f"<div class='chat-meta'><span class='chat-author'>{h(author)} <span class='muted'>{msg_no}{msg_id}</span></span><span>{h(msg.get('date_written'))}</span></div>"
                f"<div class='chat-bubble'>{text_html}</div></div>"
            )
        if not rows:
            rows.append("<div class='empty-state'>No GGSEL messages loaded</div>")
        notice = ""
        if self.q("stock_sent") == "1":
            notice = "<div class='notice ok-bg'>&#24050;&#34917;&#36135;&#65292;&#24182;&#20174;&#24211;&#23384;&#20013;&#31227;&#38500;&#19968;&#26465;&#21830;&#21697;&#12290;</div>"
        elif self.q("sent") == "1":
            notice = "<div class='notice ok-bg'>&#22238;&#22797;&#24050;&#21457;&#36865;&#65292;&#27491;&#22312;&#26174;&#31034;&#26368;&#26032;&#23545;&#35805;&#12290;</div>"
        return f"<div id='chat-panel' class='conversation-panel' data-kind='order' data-platform='ggsel' data-order-id='{selected_order}' data-message-count='{len(selected_messages)}'><div class='conversation-header'>{header}</div><div class='conversation-body'>{notice}{''.join(rows)}</div>{self.reply_editor(selected_order, buyer_lang, platform='ggsel', email=buyer_email, product=product)}</div>"

    def funpay_chat_panel_html(self, node_id: int, chat: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        buyer_name = str(chat.get("name") or f"FunPay-{node_id}")
        product = clean_text(chat.get("product") or "FunPay chat")
        buyer_lang = detect_buyer_language(messages)
        header = (
            f"<div class='conversation-header-main'><div class='conversation-header-title'>{h(buyer_name)}</div>"
            f"<div class='muted'>FunPay chat #{node_id} · {h(short(product, 110))} · Messages loaded: {len(messages)} · Reply language: {h(lang_label(buyer_lang))}</div></div>"
            "<div class='conversation-header-side'><span class='platform-badge funpay'>FunPay</span></div>"
        )
        rows = []
        total_messages = len(messages)
        for idx, msg in enumerate(messages, 1):
            is_seller = msg.get("seller") == 1
            cls = "seller" if is_seller else "buyer"
            author = str(msg.get("author") or ("nose1989" if is_seller else buyer_name))
            text = clean_text(msg.get("message"))
            try:
                if is_seller:
                    text_html = message_text_html(seller_bubble_text(text), allow_save=True)
                else:
                    text_html = translate_incoming_html(text, msg.get("id"), should_translate=True)
            except Exception as exc:
                text_html = h(text or f"Message render error: {exc}")
            msg_no = f"#{idx}/{total_messages}"
            msg_id = f" · ID {h(msg.get('id'))}" if msg.get("id") else ""
            rows.append(
                f"<div class='chat-row {cls}'>"
                f"<div class='chat-meta'><span class='chat-author'>{h(author)} <span class='muted'>{msg_no}{msg_id}</span></span><span>{h(msg.get('date_written'))}</span></div>"
                f"<div class='chat-bubble'>{text_html}</div></div>"
            )
        if not rows:
            rows.append("<div class='empty-state'>No FunPay messages loaded</div>")
        notice = ""
        if self.q("sent") == "1":
            notice = "<div class='notice ok-bg'>&#22238;&#22797;&#24050;&#21457;&#36865;&#65292;&#27491;&#22312;&#26174;&#31034;&#26368;&#26032;&#23545;&#35805;&#12290;</div>"
        return f"<div id='chat-panel' class='conversation-panel' data-kind='order' data-platform='funpay' data-order-id='{node_id}' data-message-count='{len(messages)}'><div class='conversation-header'>{header}</div><div class='conversation-body'>{notice}{''.join(rows)}</div>{self.reply_editor(node_id, buyer_lang, platform='funpay', email=buyer_name, product=product)}</div>"

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
        platform_param = self.q("platform", "").strip()
        platform = platform_param or "digiseller"
        if platform == "funpay":
            messages = funpay_client.chat_messages(order_id)
            product = funpay_client.chat_product(order_id) or product
            selected_chat = {"node_id": order_id, "name": email if email != f"order-{order_id}" else f"FunPay-{order_id}", "product": product, "platform": "funpay"}
            return self.send_json({"ok": True, "platform": "funpay", "order_id": order_id, "email": selected_chat["name"], "product": product, "count": len(messages), "read": True, "html": self.funpay_chat_panel_html(order_id, selected_chat, messages)})
        ggsel_messages: list[dict[str, Any]] | None = None
        if not platform_param and ggsel_client.configured():
            try:
                ggsel_messages = ggsel_client.chat_messages(order_id)
                if ggsel_messages:
                    platform = "ggsel"
            except Exception:
                ggsel_messages = None
        if platform == "ggsel":
            messages = ggsel_messages if ggsel_messages is not None else ggsel_client.chat_messages(order_id)
            if messages:
                safe_mark_ggsel_chat_read(order_id)
            email = ggsel_order_buyer_email(order_id, email)
            product = ggsel_order_product_name(order_id, product)
            selected_chat = {"id_i": order_id, "email": email, "product": product, "platform": "ggsel"}
            return self.send_json({"ok": True, "platform": "ggsel", "order_id": order_id, "email": email, "product": product, "count": len(messages), "read": True, "html": self.ggsel_chat_panel_html(order_id, selected_chat, messages)})
        messages = client.all_chat_messages(order_id)
        if messages:
            safe_mark_chat_read(order_id)
        selected_chat = {"id_i": order_id, "email": email, "product": product, "platform": "digiseller"}
        self.send_json({"ok": True, "platform": "digiseller", "order_id": order_id, "email": email, "product": product, "count": len(messages), "read": True, "html": self.chat_panel_html(order_id, selected_chat, messages)})

    def api_translate_batch(self) -> None:
        payload = self.read_json_body()
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return self.send_json({"ok": False, "error": "messages must be a list"}, 400)
        pending_items = []
        for item in messages[:100]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            text = clean_text(item.get("text"))
            if not text:
                continue
            pending_items.append({"id": item_id, "text": text})

        def translate_item(item: dict[str, str]) -> dict[str, str]:
            translated, source_lang = google_translate(item["text"], "zh-CN")
            return {
                "id": item["id"],
                "text": item["text"],
                "translated": translated,
                "source_lang": source_lang,
                "label": lang_label(source_lang),
            }

        results: list[dict[str, str]] = []
        if pending_items:
            max_workers = min(8, len(pending_items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                for result in executor.map(translate_item, pending_items):
                    results.append(result)
        self.send_json({"ok": True, "results": results})

    def api_save_common_phrase(self) -> None:
        payload = self.read_json_body()
        text = clean_text(payload.get("text"))
        if not text:
            return self.send_json({"ok": False, "error": "text is required"}, 400)
        created, phrase_id = save_text_common_phrase(text)
        self.send_json({"ok": True, "created": created, "id": phrase_id})

    # ---- Mobile SPA data API -------------------------------------------------
    # These endpoints only return raw JSON data for the independent mobile
    # frontend under mobile/. They never render the PC admin UI and carry no
    # platform-source labels for display (the platform field is used solely to
    # route follow-up requests, and the mobile client never shows it).
    def _mobile_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_mobile_json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._mobile_cors()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def mobile_cors_preflight(self) -> None:
        self.send_response(204)
        self._mobile_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _mobile_initial(name: str) -> str:
        text = clean_text(name)
        return (text[:1] or "#").upper()

    @staticmethod
    def _mobile_time_label(when: str) -> str:
        text = clean_text(when)
        if len(text) >= 16 and "-" in text[:10]:
            return text[11:16]
        return text

    @staticmethod
    def _mobile_display_name(name: Any, conv_id: int = 0) -> str:
        """Strip platform-identifying fallback names so no source marker leaks."""
        text = clean_text(name)
        if not text:
            return ""
        lowered = text.lower()
        if lowered in {f"funpay-{conv_id}", f"ggsel-{conv_id}", f"order-{conv_id}", f"digiseller-{conv_id}"}:
            return ""
        if re.match(r"^(funpay|ggsel|digiseller)[-\s]", text, re.IGNORECASE):
            return ""
        return text

    @staticmethod
    def _mobile_avatar(product: Any, fallback: str) -> dict[str, Any]:
        """Structured avatar for the mobile list: brand logo image when the
        product matches a known brand, otherwise a generic mark or initial."""
        brand = product_brand(product)
        if brand:
            name, mark, logo, background, _ = brand
            return {"kind": "brand", "logo": logo, "background": background, "mark": mark, "name": name}
        product_text = clean_text(product)
        if product_text:
            words = re.findall(r"[A-Za-z0-9]+", product_text)
            label = words[0] if words else short(product_text, 8)
            mark = label[:2].upper() or fallback
            return {"kind": "generic", "mark": mark, "label": short(label, 8)}
        return {"kind": "initial", "initial": fallback}

    def api_m_conversations(self) -> None:
        entries: list[tuple[float, dict[str, Any]]] = []
        errors: list[str] = []
        try:
            for chat in client.chats(page_size=100):
                if not is_recent_time(chat.get("last_date"), RECENT_CHAT_DAYS):
                    continue
                order_id = int(chat.get("id_i") or 0)
                if not order_id:
                    continue
                email = str(chat.get("email") or "")
                name = (email.split("@", 1)[0] if email else "") or f"#{order_id}"
                when = str(chat.get("last_date") or "")
                entries.append((sort_time(when), {
                    "platform": "digiseller", "id": order_id,
                    "name": name, "email": email,
                    "preview": short(chat.get("product"), 80),
                    "product": clean_text(chat.get("product")),
                    "time": when, "time_label": self._mobile_time_label(when),
                    "unread": int(chat.get("cnt_new") or 0),
                    "initial": self._mobile_initial(name),
                    "avatar": self._mobile_avatar(chat.get("product"), self._mobile_initial(name)),
                }))
        except Exception as exc:
            errors.append(str(exc))
        try:
            if ggsel_client.configured():
                data = ggsel_client.chats(page_size=50)
                for chat in list(data.get("items") or data.get("chats") or []):
                    if not is_recent_time(ggsel_chat_last_date(chat), RECENT_CHAT_DAYS):
                        continue
                    order_id = ggsel_chat_order_id(chat)
                    if not order_id:
                        continue
                    product = ggsel_order_product_name(order_id, chat.get("product") or "")
                    email = ggsel_order_buyer_email(order_id, chat.get("email") or "")
                    name = (email.split("@", 1)[0] if email else "") or f"#{order_id}"
                    when = str(ggsel_chat_last_date(chat) or "")
                    entries.append((sort_time(when), {
                        "platform": "ggsel", "id": order_id,
                        "name": name, "email": email,
                        "preview": short(product, 80), "product": clean_text(product),
                        "time": when, "time_label": self._mobile_time_label(when),
                        "unread": int(chat.get("cnt_new") or 0),
                        "initial": self._mobile_initial(name),
                        "avatar": self._mobile_avatar(product, self._mobile_initial(name)),
                    }))
        except Exception as exc:
            errors.append(str(exc))
        try:
            if funpay_client.configured():
                for index, chat in enumerate(funpay_client.chats(limit=50)):
                    if not is_recent_time(chat.get("last_date"), RECENT_CHAT_DAYS):
                        continue
                    node_id = int(chat.get("node_id") or 0)
                    if not node_id:
                        continue
                    name = self._mobile_display_name(chat.get("name"), node_id) or "会员"
                    product = clean_text(chat.get("product") or chat.get("message") or "")
                    when = str(chat.get("last_date") or "")
                    sort_key = sort_time(chat.get("last_sort_date")) or sort_time(when) or (-1000000.0 - index)
                    entries.append((sort_key, {
                        "platform": "funpay", "id": node_id,
                        "name": name, "email": name,
                        "preview": short(product or chat.get("message"), 80),
                        "product": product,
                        "time": when, "time_label": self._mobile_time_label(when),
                        "unread": int(chat.get("cnt_new") or 0),
                        "initial": self._mobile_initial(name),
                        "avatar": self._mobile_avatar(product, self._mobile_initial(name)),
                    }))
        except Exception as exc:
            errors.append(str(exc))
        entries.sort(key=lambda item: item[0], reverse=True)
        conversations = [item for _, item in entries][:50]
        unread_total = sum(int(c.get("unread") or 0) for c in conversations)
        self.send_mobile_json({"ok": True, "conversations": conversations, "unread_total": unread_total, "errors": errors})

    @staticmethod
    def _mobile_attachment(msg: dict[str, Any]) -> dict[str, Any]:
        text = clean_text(msg.get("message") or msg.get("text") or "")
        filename = clean_text(msg.get("filename") or text)
        is_image = msg.get("is_img") == 1 or looks_like_image_name(filename)
        url = clean_text(msg.get("url") or "")
        preview = clean_text(msg.get("preview") or "")
        if is_image and not preview and msg.get("id"):
            preview = "https://graph.digiseller.ru/img_deb.ashx?f=" + urllib.parse.quote(f"{msg.get('id')}/{filename}") + "&w=360"
        return {"filename": filename or "file", "url": url or preview, "preview": preview, "is_image": bool(is_image)}

    def api_m_messages(self) -> None:
        platform = self.q("platform", "digiseller").strip() or "digiseller"
        conv_id = int(self.q("id", "0") or 0)
        if not conv_id:
            return self.send_mobile_json({"ok": False, "error": "id is required"}, 400)
        try:
            if platform == "funpay":
                raw = funpay_client.chat_messages(conv_id)
                product = funpay_client.chat_product(conv_id) or ""
                derived_name = next((clean_text(m.get("author")) for m in reversed(raw) if m.get("seller") != 1 and m.get("author")), "")
                name = self._mobile_display_name(self.q("name", ""), conv_id) or self._mobile_display_name(derived_name, conv_id)
            elif platform == "ggsel":
                raw = ggsel_client.chat_messages(conv_id)
                if raw:
                    safe_mark_ggsel_chat_read(conv_id)
                product = ggsel_order_product_name(conv_id, self.q("product", ""))
                email = ggsel_order_buyer_email(conv_id, "")
                name = self._mobile_display_name(self.q("name", ""), conv_id) or (email.split("@", 1)[0] if email else "")
            else:
                platform = "digiseller"
                raw = client.all_chat_messages(conv_id)
                if raw:
                    safe_mark_chat_read(conv_id)
                email = self.q("email", "")
                product = self.q("product", "")
                name = self._mobile_display_name(self.q("name", ""), conv_id) or (email.split("@", 1)[0] if email else "")
        except Exception as exc:
            return self.send_mobile_json({"ok": False, "error": str(exc)}, 502)
        target_lang = detect_buyer_language(raw)
        messages = []
        for idx, msg in enumerate(raw, 1):
            is_seller = msg.get("seller") == 1
            text = clean_text(msg.get("message") or msg.get("text") or "")
            entry: dict[str, Any] = {
                "id": str(msg.get("id") or f"{platform}-{conv_id}-{idx}"),
                "direction": "out" if is_seller else "in",
                "author": clean_text(msg.get("author") or ""),
                "date": clean_text(msg.get("date_written") or ""),
                "text": text,
                "translate": False,
                "translated": "",
                "lang": "",
            }
            if is_attachment_message(msg):
                entry["attachment"] = self._mobile_attachment(msg)
            elif not is_seller and should_translate_text(text) and heuristic_language(text) not in {"zh", "zh-CN"}:
                entry["translate"] = True
                cached = cached_translation(text, "zh-CN")
                if cached:
                    entry["translated"] = cached[0]
                    entry["lang"] = lang_label(cached[1])
            elif is_seller:
                entry["text"] = seller_bubble_text(text)
            messages.append(entry)
        buyer_name = name or "会员"
        self.send_mobile_json({
            "ok": True, "platform": platform, "id": conv_id,
            "name": buyer_name, "product": clean_text(product),
            "target_lang": target_lang, "messages": messages,
        })

    def api_m_translate(self) -> None:
        payload = self.read_json_body()
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            return self.send_mobile_json({"ok": False, "error": "messages must be a list"}, 400)
        pending = []
        for item in raw_messages[:100]:
            if not isinstance(item, dict):
                continue
            text = clean_text(item.get("text"))
            if not text:
                continue
            pending.append({"id": str(item.get("id") or ""), "text": text})

        def translate_item(item: dict[str, str]) -> dict[str, str]:
            translated, source_lang = google_translate(item["text"], "zh-CN")
            return {"id": item["id"], "text": item["text"], "translated": translated, "source_lang": source_lang, "label": lang_label(source_lang)}

        results: list[dict[str, str]] = []
        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pending))) as executor:
                results = list(executor.map(translate_item, pending))
        self.send_mobile_json({"ok": True, "results": results})

    def api_m_send(self) -> None:
        payload = self.read_json_body()
        platform = clean_text(payload.get("platform") or "digiseller") or "digiseller"
        conv_id = int(payload.get("id") or 0)
        message = clean_text(payload.get("message"))
        target_lang = clean_text(payload.get("target_lang")) or "en"
        if not conv_id:
            return self.send_mobile_json({"ok": False, "error": "id is required"}, 400)
        if not message:
            return self.send_mobile_json({"ok": False, "error": "message is required"}, 400)
        if should_translate_outgoing_message(message, target_lang):
            original_message = message
            message, _ = google_translate(message, target_lang, "zh-CN")
            record_sent_original(message, original_message)
        try:
            if platform == "ggsel":
                ggsel_client.send_chat_message(conv_id, message, [])
            elif platform == "funpay":
                funpay_client.send_chat_message(conv_id, message, [])
            else:
                client.send_chat_message(conv_id, message, [])
        except Exception as exc:
            return self.send_mobile_json({"ok": False, "error": str(exc)}, 502)
        self.send_mobile_json({"ok": True, "platform": platform, "id": conv_id, "sent_text": message})

    def chats(self) -> None:
        chat_error = ""
        try:
            chats = client.chats(page_size=100)
        except Exception as exc:
            chats = []
            chat_error = str(exc)
        chats = [chat for chat in chats if is_recent_time(chat.get("last_date"), RECENT_CHAT_DAYS)]
        guest_error = ""
        try:
            guest_chats = client.guest_chats(limit=10)
        except Exception as exc:
            guest_chats = []
            guest_error = str(exc)
        ggsel_error = ""
        try:
            ggsel_data = ggsel_client.chats(page_size=50) if ggsel_client.configured() else {}
            ggsel_chats = list(ggsel_data.get("items") or ggsel_data.get("chats") or [])
        except Exception as exc:
            ggsel_chats = []
            ggsel_error = str(exc)
        ggsel_chats = [chat for chat in ggsel_chats if is_recent_time(ggsel_chat_last_date(chat), RECENT_CHAT_DAYS)]
        funpay_error = ""
        try:
            funpay_chats = funpay_client.chats(limit=50) if funpay_client.configured() else []
        except Exception as exc:
            funpay_chats = []
            funpay_error = str(exc)
        funpay_chats = [chat for chat in funpay_chats if is_recent_time(chat.get("last_date"), RECENT_CHAT_DAYS)]
        try:
            funpay_order_dates = funpay_client.recent_order_dates(limit=100) if funpay_chats else {}
        except Exception:
            funpay_order_dates = {}
        for chat in funpay_chats:
            node_id = int(chat.get("node_id") or 0)
            if not node_id:
                continue
            try:
                page = funpay_client.chat_page(node_id)
                chat["product"] = funpay_client.chat_product_from_page(page) or chat.get("message") or "FunPay chat"
                messages = funpay_client.chat_messages_from_page(node_id, page)
                latest_message = messages[-1] if messages else {}
                latest_date = str(latest_message.get("date_written") or "")
                order_date = funpay_client.latest_order_date_for_messages(messages, funpay_order_dates)
                sort_date = funpay_client.message_sort_date(latest_date, order_date) if order_date else ""
                if sort_date and sort_time(sort_date):
                    chat["last_sort_date"] = sort_date
            except Exception:
                chat["product"] = chat.get("message") or "FunPay chat"
        selected_kind = self.q("kind", "order")
        selected_platform_param = self.q("platform", "").strip()
        selected_platform = selected_platform_param or "digiseller"
        selected_order = 0 if selected_kind == "guest" else int(self.q("order_id", "0") or 0)
        selected_corr_id = int(self.q("corr_id", "0") or 0) if selected_kind == "guest" else 0
        selected_corr_type = self.q("corr_type", "visitor")
        selected_ggsel_messages: list[dict[str, Any]] | None = None
        if selected_kind != "guest" and not selected_order:
            order_candidates: list[tuple[float, str, int]] = []
            for chat in chats:
                order_id = int(chat.get("id_i") or 0)
                if order_id:
                    order_candidates.append((sort_time(chat.get("last_date")), "digiseller", order_id))
            for chat in ggsel_chats:
                order_id = ggsel_chat_order_id(chat)
                if order_id:
                    order_candidates.append((sort_time(ggsel_chat_last_date(chat)), "ggsel", order_id))
            for funpay_index, chat in enumerate(funpay_chats):
                node_id = int(chat.get("node_id") or 0)
                if node_id:
                    order_candidates.append((sort_time(chat.get("last_sort_date")) or (-1000000.0 - funpay_index), "funpay", node_id))
            if order_candidates:
                _, selected_platform, selected_order = max(order_candidates, key=lambda item: item[0])
                selected_kind = "order"
        if (
            selected_kind == "order"
            and selected_order
            and not selected_platform_param
            and any(ggsel_chat_order_id(chat) == selected_order for chat in ggsel_chats)
        ):
            selected_platform = "ggsel"
        if selected_kind == "order" and selected_order and not selected_platform_param and selected_platform != "ggsel" and ggsel_client.configured():
            try:
                selected_ggsel_messages = ggsel_client.chat_messages(selected_order)
                if selected_ggsel_messages:
                    selected_platform = "ggsel"
            except Exception:
                selected_ggsel_messages = None
        if selected_kind != "guest" and not selected_order and not selected_corr_id and guest_chats:
            selected_kind = "guest"
            selected_corr_id = int(guest_chats[0].get("CorrID") or 0)
            selected_corr_type = str(guest_chats[0].get("CorrType") or "visitor")
        items = []
        order_items: list[tuple[float, str]] = []
        order_unread_total = 0
        guest_unread_total = 0
        selected_chat: dict[str, Any] | None = None
        selected_guest_chat: dict[str, Any] | None = None
        for chat in chats:
            order_id = int(chat.get("id_i") or 0)
            if selected_kind == "order" and selected_platform not in ("ggsel", "funpay") and order_id == selected_order:
                selected_chat = chat
            email = str(chat.get("email") or "unknown")
            name = email.split("@", 1)[0] or email
            initials = (name[:1] or "?").upper()
            raw_unread = int(chat.get("cnt_new") or 0)
            order_unread_total += raw_unread
            unread = 0 if selected_kind == "order" and selected_platform not in ("ggsel", "funpay") and order_id == selected_order else raw_unread
            active = " active" if selected_kind == "order" and selected_platform not in ("ggsel", "funpay") and order_id == selected_order else ""
            preview = short(chat.get("product"), 80)
            avatar = product_avatar_html(chat.get("product"), initials)
            when = str(chat.get("last_date") or "")
            short_when = when[11:16] if len(when) >= 16 else when
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            href = order_chat_href(order_id, email, chat.get("product"))
            search_text = " ".join([str(order_id), email, str(chat.get("product") or ""), name]).lower()
            order_items.append((
                sort_time(when),
                f"<a class='conversation-item{active}' data-kind='order' data-platform='digiseller' data-has-unread='{1 if raw_unread else 0}' data-search='{h(search_text)}' data-order-id='{order_id}' data-email='{h(email)}' data-product='{h(chat.get('product'))}' href='{h(href)}'>"
                f"{avatar}"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                f"<div class='conversation-time'>{h(short_when)}{badge}</div></a>"
            ))
        if chat_error:
            items.append(f"<div class='conversation-item'><div class='avatar'>!</div><div><div class='conversation-name'>Digiseller API error</div><div class='preview'>{h(short(chat_error, 100))}</div></div><div></div></div>")
        if ggsel_error:
            items.append(f"<div class='conversation-item'><div class='avatar'>!</div><div><div class='conversation-name'>GGSEL API error</div><div class='preview'>{h(short(ggsel_error, 100))}</div></div><div></div></div>")
        if funpay_error:
            items.append(f"<div class='conversation-item'><div class='avatar'>!</div><div><div class='conversation-name'>FunPay API error</div><div class='preview'>{h(short(funpay_error, 100))}</div></div><div></div></div>")
        for chat in ggsel_chats:
            order_id = ggsel_chat_order_id(chat)
            if not order_id:
                continue
            product = ggsel_order_product_name(order_id, chat.get("product") or "GGSEL order")
            email = ggsel_order_buyer_email(order_id, chat.get("email") or f"ggsel-{order_id}")
            if selected_kind == "order" and selected_platform == "ggsel" and order_id == selected_order:
                selected_chat = {"id_i": order_id, "email": email, "product": product, "platform": "ggsel"}
            name = email.split("@", 1)[0] or email
            initials = "GG"
            raw_unread = int(chat.get("cnt_new") or 0)
            unread = 0 if selected_kind == "order" and selected_platform == "ggsel" and order_id == selected_order else raw_unread
            active = " active" if selected_kind == "order" and selected_platform == "ggsel" and order_id == selected_order else ""
            preview = str(product or "")
            avatar = product_avatar_html(product, initials)
            when = str(ggsel_chat_last_date(chat) or "")
            short_when = when[11:16] if len(when) >= 16 and "-" in when[:10] else when[-5:]
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            href = "/chats?" + urllib.parse.urlencode({"platform": "ggsel", "order_id": str(order_id), "email": email, "product": str(product)})
            search_text = " ".join(["ggsel", str(order_id), email, str(product), name]).lower()
            order_items.append((
                sort_time(when),
                f"<a class='conversation-item{active}' data-kind='order' data-platform='ggsel' data-has-unread='{1 if raw_unread else 0}' data-search='{h(search_text)}' data-order-id='{order_id}' data-email='{h(email)}' data-product='{h(product)}' href='{h(href)}'>"
                f"{avatar}"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                f"<div class='conversation-time'>{h(short_when)}{badge}</div></a>"
            ))
        for funpay_index, chat in enumerate(funpay_chats):
            node_id = int(chat.get("node_id") or 0)
            if not node_id:
                continue
            product = str(chat.get("product") or "FunPay chat")
            if selected_kind == "order" and selected_platform == "funpay" and node_id == selected_order:
                selected_chat = {"node_id": node_id, "name": chat.get("name") or f"FunPay-{node_id}", "product": product, "platform": "funpay"}
            name = str(chat.get("name") or f"FunPay-{node_id}")
            raw_unread = int(chat.get("cnt_new") or 0)
            unread = 0 if selected_kind == "order" and selected_platform == "funpay" and node_id == selected_order else raw_unread
            active = " active" if selected_kind == "order" and selected_platform == "funpay" and node_id == selected_order else ""
            preview = str(chat.get("message") or "FunPay chat")
            when = str(chat.get("last_date") or "")
            sort_when = str(chat.get("last_sort_date") or when)
            short_when = sort_when[11:16] if len(sort_when) >= 16 and "-" in sort_when[:10] else when
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            href = "/chats?" + urllib.parse.urlencode({"platform": "funpay", "order_id": str(node_id), "email": name, "product": product})
            search_text = " ".join(["funpay", str(node_id), name, product, preview]).lower()
            order_items.append((
                sort_time(chat.get("last_sort_date")) or (-1000000.0 - funpay_index),
                f"<a class='conversation-item{active}' data-kind='order' data-platform='funpay' data-has-unread='{1 if raw_unread else 0}' data-search='{h(search_text)}' data-order-id='{node_id}' data-email='{h(name)}' data-product='{h(product)}' href='{h(href)}'>"
                f"{product_avatar_html(product, 'FP')}"
                f"<div><div class='conversation-name'>{h(short(product, 70))}</div>"
                f"<div class='preview'>{h(short(name + ': ' + preview, 70))}</div></div>"
                f"<div class='conversation-time' title='{h(sort_when)}'>{h(short_when)}{badge}</div></a>"
            ))
        CHATS_LIST_LIMIT = 30
        ordered_conversations = [html for _, html in sorted(order_items, key=lambda item: item[0], reverse=True)]
        shown_conversations = ordered_conversations[:CHATS_LIST_LIMIT]
        for html in ordered_conversations[CHATS_LIST_LIMIT:]:
            if "conversation-item active" in html:
                shown_conversations.append(html)
        items.extend(shown_conversations)

        if selected_kind == "order" and selected_order and selected_chat is None:
            fallback_email = self.q("email", f"order-{selected_order}")
            fallback_product = self.q("product", "Direct order lookup")
            if selected_platform == "ggsel":
                fallback_email = ggsel_order_buyer_email(selected_order, fallback_email)
                fallback_product = ggsel_order_product_name(selected_order, fallback_product)
            elif selected_platform == "funpay":
                fallback_email = self.q("email", f"FunPay-{selected_order}")
                fallback_product = "FunPay chat"
            selected_chat = {
                "id_i": selected_order,
                "email": fallback_email,
                "product": fallback_product,
                "platform": selected_platform,
            }
            email = str(selected_chat.get("email") or f"order-{selected_order}")
            name = email.split("@", 1)[0] or email
            initials = "GG" if selected_platform == "ggsel" else "FP" if selected_platform == "funpay" else (name[:1] or "?").upper()
            product = selected_chat.get("product")
            preview = short(product, 80)
            href = ("/chats?" + urllib.parse.urlencode({"platform": selected_platform, "order_id": str(selected_order), "email": email, "product": str(product or "")})) if selected_platform in ("ggsel", "funpay") else order_chat_href(selected_order, email, product)
            search_text = " ".join([selected_platform, str(selected_order), email, str(product or ""), name]).lower()
            items.insert(
                0,
                f"<a class='conversation-item active' data-kind='order' data-platform='{h(selected_platform)}' data-has-unread='0' data-search='{h(search_text)}' data-order-id='{selected_order}' data-email='{h(email)}' data-product='{h(product)}' href='{h(href)}'>"
                f"{product_avatar_html(product, initials)}"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                "<div class='conversation-time'>new</div></a>",
            )

        if guest_chats or guest_error:
            items.append("<div class='conversation-section' data-section='guest'>Guest consultations</div>")
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
            raw_unread = 1 if not int(chat.get("IsAuthor") or 0) and not int(chat.get("IsViewed") or 0) else 0
            guest_unread_total += raw_unread
            unread = 0 if is_selected_guest else raw_unread
            active = " active" if selected_kind == "guest" and corr_id == selected_corr_id and corr_type == selected_corr_type else ""
            preview = chat.get("Text") or chat.get("PurchaseName") or ""
            avatar = product_avatar_html(chat.get("PurchaseName"), initials)
            when = str(chat.get("DateWrite") or chat.get("DateWriteUtc") or "")
            short_when = when[11:16] if len(when) >= 16 and "-" in when[:10] else when[-5:]
            badge = f"<div class='badge'>{unread}</div>" if unread else ""
            href = f"/chats?kind=guest&corr_type={urllib.parse.quote(corr_type)}&corr_id={corr_id}&name={urllib.parse.quote(name)}&product={urllib.parse.quote(str(chat.get('PurchaseName') or ''))}"
            search_text = " ".join([str(corr_id), corr_type, name, str(chat.get("PurchaseName") or ""), str(chat.get("Text") or "")]).lower()
            items.append(
                f"<a class='conversation-item{active}' data-kind='guest' data-has-unread='{1 if raw_unread else 0}' data-search='{h(search_text)}' data-corr-id='{corr_id}' data-corr-type='{h(corr_type)}' data-name='{h(name)}' data-product='{h(chat.get('PurchaseName'))}' href='{h(href)}'>"
                f"{avatar}"
                f"<div><div class='conversation-name'>{h(name)}</div>"
                f"<div class='preview'>{h(short(preview, 70))}</div></div>"
                f"<div class='conversation-time'>{h(short_when)}{badge}</div></a>"
            )

        selected_messages: list[dict[str, Any]] = []
        defer_selected_panel = bool(selected_corr_id or selected_order)
        if not defer_selected_panel and selected_kind == "guest" and selected_corr_id:
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
        elif not defer_selected_panel and selected_order:
            if selected_platform == "funpay":
                try:
                    selected_messages = funpay_client.chat_messages(selected_order)
                    funpay_product = funpay_client.chat_product(selected_order) or self.q("product", "FunPay chat")
                    if selected_chat is None:
                        selected_chat = {"node_id": selected_order, "name": self.q("email", f"FunPay-{selected_order}"), "product": funpay_product, "platform": "funpay"}
                    else:
                        selected_chat["product"] = funpay_product
                except Exception as exc:
                    selected_chat = {"node_id": selected_order, "name": self.q("email", f"FunPay-{selected_order}"), "product": f"FunPay chat load failed: {exc}", "platform": "funpay"}
            elif selected_platform == "ggsel":
                try:
                    selected_messages = selected_ggsel_messages if selected_ggsel_messages is not None else ggsel_client.chat_messages(selected_order)
                    if selected_messages:
                        safe_mark_ggsel_chat_read(selected_order)
                    if selected_chat is None:
                        selected_chat = {"id_i": selected_order, "email": self.q("email", f"ggsel-{selected_order}"), "product": self.q("product", "GGSEL order"), "platform": "ggsel"}
                except Exception as exc:
                    selected_chat = {"id_i": selected_order, "email": self.q("email", f"ggsel-{selected_order}"), "product": f"GGSEL chat load failed: {exc}", "platform": "ggsel"}
            else:
                try:
                    selected_messages = client.all_chat_messages(selected_order)
                    if selected_messages:
                        safe_mark_chat_read(selected_order)
                    if selected_chat is None:
                        selected_chat = {"id_i": selected_order, "email": self.q("email", f"order-{selected_order}"), "product": self.q("product", "Direct order lookup"), "platform": "digiseller"}
                except Exception as exc:
                    selected_chat = {"id_i": selected_order, "email": self.q("email", f"order-{selected_order}"), "product": f"Chat load failed: {exc}", "platform": "digiseller"}

        if defer_selected_panel:
            panel = self.loading_chat_panel_html(selected_kind, selected_platform, selected_order, selected_chat, selected_guest_chat)
        elif selected_kind == "guest" and selected_guest_chat:
            panel = self.guest_chat_panel_html(selected_guest_chat, selected_messages)
        elif selected_platform == "funpay" and selected_chat:
            panel = self.funpay_chat_panel_html(selected_order, selected_chat, selected_messages)
        elif selected_platform == "ggsel" and selected_chat:
            panel = self.ggsel_chat_panel_html(selected_order, selected_chat, selected_messages)
        else:
            panel = self.chat_panel_html(selected_order, selected_chat, selected_messages)
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
            link.dataset.hasUnread = '1';
            const badge = conversationBadge(link);
            if (badge) badge.textContent = String(value);
          }
          function clearConversationBadge(link) {
            const badge = link?.querySelector('.badge');
            if (badge) badge.remove();
            if (link) {
              delete link.dataset.pendingUnread;
              link.dataset.hasUnread = '0';
            }
          }
          const searchInput = document.getElementById('conversation-search');
          const filterButtons = [...document.querySelectorAll('.conversation-filter')];
          const emptyFilter = document.getElementById('conversation-empty-filter');
          function applyConversationFilters() {
            const query = (searchInput?.value || '').trim().toLowerCase();
            const activeFilter = document.querySelector('.conversation-filter.active')?.dataset.filter || 'all';
            let visible = 0;
            list.querySelectorAll('.conversation-item').forEach((link) => {
              const kind = link.dataset.kind || 'order';
              const platform = link.dataset.platform || '';
              const hasUnread = link.dataset.hasUnread === '1';
              const text = link.dataset.search || link.textContent.toLowerCase();
              const matchesSearch = !query || text.includes(query);
              const matchesFilter =
                activeFilter === 'all' ||
                (activeFilter === 'unread' && hasUnread) ||
                (activeFilter === 'orders' && kind === 'order') ||
                (activeFilter === 'ggsel' && platform === 'ggsel') ||
                (activeFilter === 'guest' && kind === 'guest');
              const show = matchesSearch && matchesFilter;
              link.hidden = !show;
              if (show) visible += 1;
            });
            list.querySelectorAll('.conversation-section').forEach((section) => {
              const sectionName = section.dataset.section || 'guest';
              const selector = sectionName === 'ggsel'
                ? '.conversation-item[data-platform="ggsel"]:not([hidden])'
                : '.conversation-item[data-kind="guest"]:not([hidden])';
              section.hidden = !list.querySelector(selector) || (activeFilter === 'orders' && sectionName !== 'ggsel') || (activeFilter === 'guest' && sectionName !== 'guest');
            });
            if (emptyFilter) emptyFilter.classList.toggle('visible', visible === 0);
          }
          function paramsForLink(link) {
            const kind = link.dataset.kind || 'order';
            return kind === 'guest'
              ? new URLSearchParams({kind: 'guest', corr_id: link.dataset.corrId || '', corr_type: link.dataset.corrType || 'visitor', name: link.dataset.name || '', product: link.dataset.product || ''})
              : new URLSearchParams({platform: link.dataset.platform || 'digiseller', order_id: link.dataset.orderId || '', email: link.dataset.email || '', product: link.dataset.product || ''});
          }
          function escapeHtml(value) {
            return String(value).replace(/[&<>"']/g, (char) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
          }
          function shortText(value, maxLength = 70) {
            const text = String(value || '');
            return text.length <= maxLength ? text : `${text.slice(0, maxLength - 1)}…`;
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
          function showPanelError(panel, link, error) {
            const name = link.querySelector('.conversation-name')?.textContent?.trim() || '加载失败';
            const message = error?.message || String(error || 'Load failed');
            panel.className = 'conversation-panel';
            panel.innerHTML = `
              <div class="conversation-header">
                <div class="conversation-header-main">
                  <div class="conversation-header-title">${escapeHtml(name)}</div>
                  <div class="muted">消息加载失败</div>
                </div>
              </div>
              <div class="empty-state">加载消息失败：${escapeHtml(message)}</div>`;
          }
          let panelRequestSeq = 0;
          async function loadPanel(link, options = {}) {
            const requestSeq = ++panelRequestSeq;
            const panel = document.getElementById('chat-panel');
            if (!panel) { location.href = link.href; return; }
            if (!options.silent) showPanelLoading(panel, link);
            try {
              const res = await fetch('/api/chat-panel?' + paramsForLink(link).toString(), {cache: 'no-store'});
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || 'Load failed');
              if (requestSeq !== panelRequestSeq || !link.classList.contains('active')) return;
              if (data.email) {
                link.dataset.email = data.email;
                const name = link.querySelector('.conversation-name');
                if (name) name.textContent = String(data.email).split('@', 1)[0] || data.email;
              }
              if (data.product) {
                link.dataset.product = data.product;
                const preview = link.querySelector('.preview');
                if (preview) preview.textContent = shortText(data.product);
              }
              link.dataset.search = [link.dataset.platform || 'digiseller', link.dataset.orderId || '', link.dataset.email || '', link.dataset.product || ''].join(' ').toLowerCase();
              panel.outerHTML = data.html;
              const newPanel = document.getElementById('chat-panel');
              if (newPanel) runPanelScripts(newPanel);
              if (newPanel && window.loadDigisellerTranslations) window.loadDigisellerTranslations(newPanel);
              if (options.clearBadge) clearConversationBadge(link);
              if (options.pushHistory) history.pushState(null, '', link.href);
              if (options.refreshUnread && window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
            } catch (error) {
              if (requestSeq === panelRequestSeq && link.classList.contains('active')) showPanelError(panel, link, error);
            }
          }
          list.addEventListener('click', async (event) => {
            const link = event.target.closest('.conversation-item');
            if (!link) return;
            if (!link.dataset.orderId && !link.dataset.corrId) return;
            event.preventDefault();
            list.querySelectorAll('.conversation-item.active').forEach((item) => item.classList.remove('active'));
            link.classList.add('active');
            await loadPanel(link, {pushHistory: true, clearBadge: true, refreshUnread: true});
            applyConversationFilters();
          });
          searchInput?.addEventListener('input', applyConversationFilters);
          filterButtons.forEach((button) => {
            button.addEventListener('click', () => {
              filterButtons.forEach((item) => item.classList.remove('active'));
              button.classList.add('active');
              applyConversationFilters();
            });
          });
          const activeLink = list.querySelector('.conversation-item.active');
          const activePanel = document.getElementById('chat-panel');
          if (activeLink && activePanel?.classList.contains('loading')) {
            loadPanel(activeLink, {clearBadge: true, refreshUnread: true});
          }
          window.handleDigisellerUnreadData = (data) => {
            const buyerUnread = Array.isArray(data.buyer_unread) ? data.buyer_unread : [];
            const unreadByOrder = new Map();
            buyerUnread.forEach((item) => {
              const orderId = String(item.order_id || '');
              const platform = String(item.platform || 'digiseller');
              const count = Number(item.cnt_new || 0);
              if (orderId && count > 0) unreadByOrder.set(`${platform}:${orderId}`, count);
            });
            list.querySelectorAll('.conversation-item[data-kind="order"]').forEach((link) => {
              const platform = link.dataset.platform || 'digiseller';
              const count = unreadByOrder.get(`${platform}:${String(link.dataset.orderId || '')}`) || 0;
              if (count > 0) setConversationBadge(link, count);
              else if (!link.classList.contains('active')) clearConversationBadge(link);
            });
            applyConversationFilters();
            const active = list.querySelector('.conversation-item.active[data-kind="order"]');
            if (!active) return;
            const activePlatform = active.dataset.platform || 'digiseller';
            const activeCount = unreadByOrder.get(`${activePlatform}:${String(active.dataset.orderId || '')}`) || 0;
            if (activeCount <= 0 || refreshingActiveOrder) return;
            active.dataset.pendingUnread = String(activeCount);
            setConversationBadge(active, activeCount);
            refreshingActiveOrder = true;
            loadPanel(active, {silent: true}).finally(() => { refreshingActiveOrder = false; });
          };
          if (window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
          (function applyPendingDots() {
            try {
              const pending = JSON.parse(localStorage.getItem('pendingOrders') || '{}');
              document.querySelectorAll('.conversation-item[data-order-id]').forEach(item => {
                const key = (item.dataset.platform || 'digiseller') + '-' + item.dataset.orderId;
                const avatar = item.querySelector('.avatar');
                if (avatar) avatar.classList.toggle('has-pending-dot', !!pending[key]);
              });
            } catch(e) {}
          })();
          applyConversationFilters();
          const unreadPollTimer = setInterval(() => {
            if (!document.hidden && window.refreshDigisellerUnread) window.refreshDigisellerUnread(true);
          }, 15000);
          if (window.pjaxOnCleanup) window.pjaxOnCleanup(() => {
            clearInterval(unreadPollTimer);
            window.handleDigisellerUnreadData = null;
          });
        })();
        </script>
        """
        funpay_unread_total = sum(int(chat.get("cnt_new") or 0) for chat in funpay_chats)
        unread_total = order_unread_total + guest_unread_total + funpay_unread_total
        order_total = len(chats) + len(ggsel_chats) + len(funpay_chats)
        list_header = f"""
        <div class='conversation-list-header'>
          <div class='conversation-list-title'>
            <h2>Messages</h2>
            <div class='conversation-counts'>{order_total} chats · {len(guest_chats)} guests · {unread_total} unread</div>
          </div>
        </div>
        """
        body = f"<div class='messages-layout'><div id='conversation-list' class='conversation-list'>{list_header}{''.join(items)}</div>{panel}</div>{ajax}"
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
        try:
            buyer = [
                c
                for c in client.chats(only_unread=True)
                if int(c.get("cnt_new") or 0) > 0 and is_recent_time(c.get("last_date"), RECENT_CHAT_DAYS)
            ]
        except Exception:
            buyer = []
        ggsel_buyer: list[dict[str, Any]] = []
        if ggsel_client.configured():
            try:
                ggsel_data = ggsel_client.chats(page_size=50, only_unread=True)
                ggsel_rows = ggsel_data.get("items") or ggsel_data.get("chats") or []
                ggsel_buyer = [
                    c
                    for c in ggsel_rows
                    if isinstance(c, dict) and int(c.get("cnt_new") or 0) > 0 and is_recent_time(ggsel_chat_last_date(c), RECENT_CHAT_DAYS)
                ]
            except Exception:
                ggsel_buyer = []
        funpay_buyer: list[dict[str, Any]] = []
        if funpay_client.configured():
            try:
                funpay_buyer = [
                    c
                    for c in funpay_client.chats(limit=50)
                    if int(c.get("cnt_new") or 0) > 0 and is_recent_time(c.get("last_date"), RECENT_CHAT_DAYS)
                ]
            except Exception:
                funpay_buyer = []
        try:
            guest = [
                c
                for c in client.guest_chats(limit=10)
                if not int(c.get("IsAuthor") or 0) and not int(c.get("IsViewed") or 0)
            ]
        except Exception:
            guest = []
        admin: list[dict[str, Any]] = []  # Do not count historical admin notices as unread.
        b_rows = [["Digiseller", f"<a href='/chats?order_id={h(c.get('id_i'))}'>{h(c.get('id_i'))}</a>", h(c.get("last_date")), h(c.get("cnt_new")), h(c.get("email")), h(short(c.get("product"), 100))] for c in buyer]
        for c in ggsel_buyer:
            order_id = c.get("id_i")
            email = c.get("email") or f"ggsel-{order_id}"
            product = clean_text(c.get("product") or "GGSEL order")
            href = "/chats?" + urllib.parse.urlencode({"platform": "ggsel", "order_id": str(order_id or ""), "email": str(email), "product": product})
            b_rows.append(["GGSEL", f"<a href='{h(href)}'>{h(order_id)}</a>", h(c.get("last_message")), h(c.get("cnt_new")), h(email), h(short(product, 100))])
        for c in funpay_buyer:
            node_id = c.get("node_id")
            name = c.get("name") or f"FunPay-{node_id}"
            href = "/chats?" + urllib.parse.urlencode({"platform": "funpay", "order_id": str(node_id or ""), "email": str(name), "product": "FunPay chat"})
            b_rows.append(["FunPay", f"<a href='{h(href)}'>{h(node_id)}</a>", h(c.get("last_date")), h(c.get("cnt_new")), h(name), h(short(c.get("message"), 100))])
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
        body = f"<div class='card'><h2>Unread</h2><p>Buyer unread: {len(buyer) + len(ggsel_buyer) + len(funpay_buyer)} | Guest unread: {len(guest)} | Admin unread: {len(admin)}</p></div><h3>Buyer chats</h3>{table(['Platform','Order','Last','New','Email','Product'], b_rows)}<h3>Guest consultations</h3>{table(['Guest','Last','Type','Text','Product'], g_rows)}<h3>Admin messages</h3>{table(['Date','ID','Text'], a_rows)}"
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
        record_digiseller_stock_upload(product_id, items, response)
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

    def api_sales_order_count(self) -> None:
        force = self.q("force", "0") in {"1", "true", "yes"}
        self.send_json(sales_order_badge_summary(force=force))

    def api_online_keepalive(self) -> None:
        self.send_json(refresh_public_online_status(force=True))

    def api_funpay_boost_status(self) -> None:
        self.send_json(funpay_boost_snapshot())

    def api_funpay_boost_history(self) -> None:
        self.send_json({"status": funpay_boost_snapshot(), "rows": funpay_boost_history_rows()})

    def api_funpay_boost_sync(self) -> None:
        try:
            sync_result = sync_funpay_boost_active_categories()
        except Exception as exc:
            return self.send_json({"error": str(exc), "status": funpay_boost_snapshot()}, 400)
        self.send_json(
            {
                "sync": sync_result,
                "status": funpay_boost_snapshot(),
                "rows": funpay_boost_history_rows(),
            }
        )

    def api_funpay_boost_clear_errors(self) -> None:
        cleared = clear_funpay_boost_failed_history()
        self.send_json({"cleared": cleared, "status": funpay_boost_snapshot(), "rows": funpay_boost_history_rows()})

    def api_funpay_boost_toggle(self) -> None:
        data = self.read_json_body()
        enabled = bool(data.get("enabled"))
        if enabled:
            try:
                status = start_funpay_boost_scheduler()
            except Exception as exc:
                update_funpay_boost_status(enabled=False, running=False, next_run="", last_error=str(exc))
                return self.send_json(funpay_boost_snapshot(), 400)
        else:
            update_funpay_boost_status(enabled=False, next_run="")
            status = funpay_boost_snapshot()
        self.send_json(status)

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
        rel = urllib.parse.unquote(strip_path_prefix(path, "/downloads/"))
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

    def serve_asset(self, path: str) -> None:
        rel = urllib.parse.unquote(strip_path_prefix(path, "/assets/"))
        if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
            self.send_error(404)
            return
        file_path = (ASSET_DIR / rel).resolve()
        if not str(file_path).startswith(str(ASSET_DIR.resolve())) or not file_path.is_file():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_phrase_file(self, path: str) -> None:
        rel = urllib.parse.unquote(strip_path_prefix(path, "/phrase-files/"))
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
    if len(sys.argv) > 1 and sys.argv[1] in {"translate-phrases", "phrases-to-chinese"}:
        count, total = translate_common_phrases_to_chinese()
        print(f"Common phrases translated to Chinese: {count} changed of {total} with text.")
        return
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
    start_translation_cache_cleanup()
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
