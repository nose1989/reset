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
"""
from __future__ import annotations

import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIST = (Path(__file__).resolve().parent / "dist").resolve()
BACKEND = (os.environ.get("DIGISELLER_ADMIN_ORIGIN") or "http://127.0.0.1:8765").rstrip("/")
PORT = int(os.environ.get("MOBILE_PORT") or 8080)
HOST = os.environ.get("MOBILE_HOST") or "0.0.0.0"
PROXY_PREFIXES = ("/api/", "/assets/")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MobileServe/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(PROXY_PREFIXES):
            self._proxy(method)
        else:
            self._serve_static(path)

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = BACKEND + self.path
        req = urllib.request.Request(url, data=body, method=method)
        ctype = self.headers.get("Content-Type")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
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
