#!/usr/bin/env python3
"""Local dashboard for remaining usage on Claude, Codex, and Cursor accounts.

Zero dependencies — run with:  python3 server.py
Then open http://127.0.0.1:8899
"""

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import gateway
import history
from dispatcher import launch as dispatcher_launch
from dispatcher.config import load_config
from providers import claude, codex, cursor, gemini

HOST = "127.0.0.1"
PORT = int(os.environ.get("USAGE_TRACKER_PORT", "8899"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

PROVIDERS = {"claude": claude.fetch, "codex": codex.fetch,
             "gemini": gemini.fetch, "cursor": cursor.fetch}
CACHE_TTL_S = {"claude": 60, "codex": 120, "gemini": 60, "cursor": 120}

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
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(_get_provider, name, force) for name in PROVIDERS}
        services = {name: fut.result() for name, fut in futures.items()}
    history.append_snapshot(history.snapshot_from_services(services))
    return services


SAMPLE_INTERVAL_S = int(os.environ.get("USAGE_TRACKER_SAMPLE_S", "600"))


def _sampler():
    """Record usage history even when nobody has the dashboard open."""
    while True:
        try:
            _get_all(force=False)
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL_S)


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
            records, active = dispatcher_launch.read_dispatch_log(limit=100)
            payload = {"records": records, "active": active,
                       "stats": _dispatch_stats(records + active),
                       "server_time": int(time.time())}
            self._respond(200, "application/json", json.dumps(payload).encode())
        elif parsed.path == "/api/history":
            try:
                days = min(90, max(1, int(parse_qs(parsed.query).get("days", ["14"])[0])))
            except ValueError:
                days = 14
            records, active = dispatcher_launch.read_dispatch_log(limit=10000)
            series = history.daily_series(
                history.read_history(max_age_days=days),
                records + active,
                days=days)
            self._respond(200, "application/json", json.dumps({"days": series}).encode())
        elif parsed.path == "/api/insight":
            from insight import snapshot
            config = load_config()
            services = _get_all(force=False)
            records, _active = dispatcher_launch.read_dispatch_log(limit=5000)
            try:
                days = min(90, max(1, int(parse_qs(parsed.query).get("days", ["14"])[0])))
            except ValueError:
                days = 14
            series = history.daily_series(
                history.read_history(max_age_days=days), records, days=days)
            payload = snapshot(
                services, records, series, config,
                dispatcher_launch.installed_agents())
            self._respond(200, "application/json", json.dumps(payload).encode())
        elif parsed.path == "/v1/models":
            payload = gateway.list_models(load_config())
            self._respond(200, "application/json", json.dumps(payload).encode())
        elif parsed.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                self._respond(200, "text/html; charset=utf-8", f.read())
        else:
            self._respond(404, "text/plain", b"not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/v1/chat/completions":
            self._respond(404, "text/plain", b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._respond(400, "application/json",
                          json.dumps({"error": {"message": "invalid JSON"}}).encode())
            return
        if not isinstance(body, dict):
            self._respond(400, "application/json",
                          json.dumps({"error": {"message": "JSON object required"}}).encode())
            return
        config = load_config()
        if not ((config.get("gateway") or {}).get("enabled", True)):
            self._respond(404, "application/json",
                          json.dumps({"error": {"message": "gateway disabled"}}).encode())
            return
        services = _get_all(force=False)
        started = time.time()
        status, payload = gateway.chat_completions(body, services, config)
        task = ""
        messages = body.get("messages") or []
        if messages:
            last = messages[-1]
            if isinstance(last, dict):
                task = str(last.get("content") or "")[:140]
        backend = ((payload.get("router") or {}).get("backend")
                   if isinstance(payload, dict) else None)
        reason = ((payload.get("router") or {}).get("reason")
                  if isinstance(payload, dict) else None)
        run_id = uuid.uuid4().hex
        dispatcher_launch.log_dispatch_start(
            run_id, backend or "gateway", reason or "gateway",
            task or "(chat completion)", "gateway", False, os.getpid())
        dispatcher_launch.log_dispatch_end(
            run_id, backend or "gateway",
            0 if status == 200 else status,
            time.time() - started)
        if status == 200 and body.get("stream"):
            sse = gateway.sse_chunks(payload).encode()
            self._respond(200, "text/event-stream", sse)
            return
        self._respond(status, "application/json", json.dumps(payload).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=_sampler, daemon=True).start()
    print(f"Usage tracker running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
