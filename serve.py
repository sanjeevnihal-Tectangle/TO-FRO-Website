"""Serve the TO&FRO site and the courier tracking API."""

import json
import os
import sys
import time
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import tracking

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "5510"))
_HITS = {}
_HITS_LOCK = threading.Lock()


def _allowed(ip):
    now = time.monotonic()
    with _HITS_LOCK:
        recent = [stamp for stamp in _HITS.get(ip, []) if now - stamp < 60]
        if len(recent) >= 30:
            _HITS[ip] = recent
            return False
        recent.append(now)
        _HITS[ip] = recent
        return True


class SiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/track":
            awb = (parse_qs(parsed.query).get("awb") or [""])[0]
            self._track(awb)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/staff":
            self._staff()
            return
        if parsed.path != "/api/track":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(max(length, 0), 4000))
        try:
            awb = json.loads(raw.decode("utf-8") or "{}").get("awb", "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            awb = ""
        self._track(awb)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(max(length, 0), 8000))
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _staff(self):
        if not _allowed(self.client_address[0]):
            self._json(429, {"ok": False, "code": "busy", "message": "Too many requests. Please wait a moment."})
            return
        body = self._body()
        action = str(body.get("action") or "")
        if action == "login":
            self._json(200, tracking.staff_login(body.get("email", ""), body.get("password", "")))
            return
        if action == "track":
            if not tracking.staff_authorized(str(body.get("token") or "")):
                self._json(200, {"ok": False, "code": "denied", "message": "Please sign in again."})
                return
            self._json(200, tracking.staff_track(body.get("awb", "")))
            return
        self._json(404, {"ok": False, "code": "invalid", "message": "Unknown request."})

    def _track(self, awb):
        ip = self.client_address[0]
        if not _allowed(ip):
            self._json(429, {"ok": False, "code": "busy", "message": "Too many tracking requests. Please wait a moment."})
            return
        self._json(200, tracking.track_awb(awb))

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    tracking.load_env()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    with ThreadingHTTPServer(("", port), SiteHandler) as server:
        print(f"Serving {ROOT} at http://localhost:{port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
