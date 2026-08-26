#!/usr/bin/env python3
"""Local dashboard for remaining usage on Claude, Codex, and Cursor accounts.

Zero dependencies — run with:  python3 server.py
Then open http://127.0.0.1:8899
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from dispatcher import launch as dispatcher_launch
from providers import claude, codex, cursor

HOST = "127.0.0.1"
PORT = int(os.environ.get("USAGE_TRACKER_PORT", "8899"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

PROVIDERS = {"claude": claude.fetch, "codex": codex.fetch, "cursor": cursor.fetch}
CACHE_TTL_S = {"claude": 60, "codex": 120, "cursor": 120}

_cache = {}
_cache_lock = threading.Lock()


def _get_provider(name, force):
    fetcher = PROVIDERS[name]
    now = time.time()
    with _cache_lock:
        entry = _cache.get(name)
        if entry and not force and now - entry["ts"] < CACHE_TTL_S[name]:
            return entry["data"]
    try:
        data = fetcher(force=force)
    except Exception as e:
        data = {"ok": False, "error": f"provider crashed: {e}"}
    with _cache_lock:
        _cache[name] = {"ts": now, "data": data}
    return data


def _get_all(force):
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(_get_provider, name, force) for name in PROVIDERS}
        return {name: fut.result() for name, fut in futures.items()}


def _dispatch_stats(records):
    now = time.time()
    counts = {}
    for r in records:
        agent = r.get("agent", "?")
        c = counts.setdefault(agent, {"total": 0, "last_24h": 0, "last_7d": 0})
        c["total"] += 1
        age = now - r.get("ts", 0)
        if age < 86400:
            c["last_24h"] += 1
        if age < 7 * 86400:
            c["last_7d"] += 1
    return counts


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/usage":
            force = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"
            payload = {"fetched_at": int(time.time()), "services": _get_all(force)}
            body = json.dumps(payload).encode()
            self._respond(200, "application/json", body)
        elif parsed.path == "/api/dispatches":
            records = dispatcher_launch.read_dispatch_log(limit=100)
            payload = {"records": records, "stats": _dispatch_stats(records)}
            self._respond(200, "application/json", json.dumps(payload).encode())
        elif parsed.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                self._respond(200, "text/html; charset=utf-8", f.read())
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Usage tracker running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
