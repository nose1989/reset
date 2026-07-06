#!/usr/bin/env python3
"""Standalone server for the mobile client.

Runs the mobile app on its own port, completely separate from the PC admin.
It serves the pre-built static files in ``dist/`` (committed to the repo, so no
``npm`` build/install is needed after a pull) and transparently proxies
``/api`` and ``/assets`` to the PC admin backend. Because everything is served
from this one origin, the browser stays same-origin and no CORS is involved.

Usage:
    python3 mobile/serve.py

Environment variables:
    MOBILE_PORT               port to listen on (default 8080)
    MOBILE_HOST               interface to bind (default 0.0.0.0, i.e. LAN)
    DIGISELLER_ADMIN_ORIGIN   backend origin to proxy to (default
                              http://127.0.0.1:8765)
    DEVICE_ACTIVATION_KEY     when set, only activated devices may access the
                              app. A new device is activated once by opening
                              /activate?key=<DEVICE_ACTIVATION_KEY>; its
                              generated device id is stored in the allowlist
                              file and in a long-lived cookie. Unknown devices
                              get 403. When unset, access is open (no gating).
    DEVICE_ALLOWLIST_FILE     path of the allowlist JSON file (default
                              <repo>/allowed_devices.json)
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIST = (Path(__file__).resolve().parent / "dist").resolve()
BACKEND = (os.environ.get("DIGISELLER_ADMIN_ORIGIN") or "http://127.0.0.1:8765").rstrip("/")
PORT = int(os.environ.get("MOBILE_PORT") or 8080)
HOST = os.environ.get("MOBILE_HOST") or "0.0.0.0"
PROXY_PREFIXES = ("/api/", "/assets/", "/phrase-files/")
# Talk to the backend directly, ignoring any system/env HTTP proxy. Without this,
# urllib may route even 127.0.0.1 through a configured proxy/VPN and fail (502).
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}

ACTIVATION_KEY = (os.environ.get("DEVICE_ACTIVATION_KEY") or "").strip()
ALLOWLIST_FILE = Path(
    os.environ.get("DEVICE_ALLOWLIST_FILE")
    or Path(__file__).resolve().parent.parent / "allowed_devices.json"
)
DEVICE_COOKIE = "reset_device_id"
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600
_ALLOWLIST_LOCK = threading.Lock()


def _load_allowlist() -> dict[str, dict]:
    try:
        data = json.loads(ALLOWLIST_FILE.read_text("utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_allowlist(allow: dict[str, dict]) -> None:
    tmp = ALLOWLIST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(allow, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(ALLOWLIST_FILE)


FORBIDDEN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>\u8bbe\u5907\u672a\u6388\u6743</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f6f8;color:#333}main{text-align:center;padding:24px}h1{font-size:20px}p{color:#777;font-size:14px}</style>
</head><body><main><h1>\u8bbe\u5907\u672a\u6388\u6743</h1>
<p>\u6b64\u8bbe\u5907\u6ca1\u6709\u8bbf\u95ee\u6743\u9650\u3002\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u83b7\u53d6\u6fc0\u6d3b\u94fe\u63a5\u3002</p>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MobileServe/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def _fingerprint(self) -> str:
        ua = self.headers.get("User-Agent", "")
        return hashlib.sha256(ua.encode("utf-8")).hexdigest()

    def _device_id(self) -> str | None:
        jar = http_cookies.SimpleCookie(self.headers.get("Cookie") or "")
        morsel = jar.get(DEVICE_COOKIE)
        return morsel.value if morsel else None

    def _set_device_cookie(self, device_id: str) -> None:
        self.send_header(
            "Set-Cookie",
            f"{DEVICE_COOKIE}={device_id}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax",
        )

    def _activate(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        key = (params.get("key") or [""])[0]
        if not ACTIVATION_KEY or not secrets.compare_digest(key, ACTIVATION_KEY):
            self._forbidden()
            return
        device_id = self._device_id() or secrets.token_urlsafe(24)
        with _ALLOWLIST_LOCK:
            allow = _load_allowlist()
            entry = allow.get(device_id)
            if not isinstance(entry, dict) or entry.get("fp") != self._fingerprint():
                allow[device_id] = {
                    "ua": self.headers.get("User-Agent", "")[:200],
                    "fp": self._fingerprint(),
                }
                _save_allowlist(allow)
        self.send_response(302)
        self.send_header("Location", "/")
        self._set_device_cookie(device_id)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _forbidden(self) -> None:
        body = FORBIDDEN_HTML.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not ACTIVATION_KEY:
            return True
        device_id = self._device_id()
        if not device_id:
            return False
        entry = _load_allowlist().get(device_id)
        # The cookie is bound to the browser fingerprint captured at activation;
        # a cookie copied to a different device/browser is rejected.
        return isinstance(entry, dict) and entry.get("fp") == self._fingerprint()

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if ACTIVATION_KEY and path == "/activate":
                self._activate(parsed.query)
                return
            if not self._authorized():
                self._forbidden()
                return
            if path.startswith(PROXY_PREFIXES):
                self._proxy(method)
            else:
                self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            # Client navigated away / closed the tab before we finished writing.
            # Harmless — just drop this request instead of dumping a traceback.
            return

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = BACKEND + self.path
        req = urllib.request.Request(url, data=body, method=method)
        ctype = self.headers.get("Content-Type")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with OPENER.open(req, timeout=60) as resp:
                data = resp.read()
                status = resp.status
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            data = exc.read()
            status = exc.code
            headers = exc.headers
        except Exception as exc:  # backend unreachable
            msg = f"Backend unreachable at {BACKEND}: {exc}".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(status)
        for key, value in (headers or {}).items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        index = DIST / "index.html"
        if not index.is_file():
            self._plain(
                503,
                "Mobile build not found. Run: cd mobile && npm install && npm run build",
            )
            return
        rel = urllib.parse.unquote(path).lstrip("/")
        if rel and ".." not in rel.split("/") and "\\" not in rel:
            candidate = (DIST / rel).resolve()
            if str(candidate).startswith(str(DIST)) and candidate.is_file():
                self._send_file(candidate, cache=rel.startswith("static/"))
                return
        # SPA fallback for client-side routes (e.g. /c/:platform/:id)
        self._send_file(index, cache=False)

    def _send_file(self, file_path: Path, cache: bool) -> None:
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
        )
        self.send_header(
            "Cache-Control",
            "public, max-age=31536000, immutable" if cache else "no-store",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _plain(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        return


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Mobile client running at http://127.0.0.1:{PORT}/ (proxying /api -> {BACKEND})")
    print("On your phone open http://<this-computer-LAN-IP>:%d/" % PORT)
    print("Press Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
