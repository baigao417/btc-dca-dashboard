from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from decision_engine import build_decision
from market_data import demo_snapshot, fetch_market_snapshot
from notifier import notify
from scheduler import Scheduler
from storage import Store
from timezones import resolve_timezone

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
STORE: Store | None = None
SCHEDULER: Scheduler | None = None


def store() -> Store:
    if STORE is None:
        raise RuntimeError("Application not initialized")
    return STORE


def run_decision(scheduled_for: str | None = None, mode: str = "demo") -> dict:
    if mode not in {"demo", "live"}:
        raise ValueError("Invalid mode")
    if mode == "live" and os.environ.get("BTC_ALLOW_LIVE") != "true":
        raise ValueError("Live access disabled")
    db = store()
    started = time.monotonic()
    now = datetime.now(resolve_timezone(db.settings()["timezone"]))
    # Manual runs use unique timestamps; scheduled runs retain their daily key.
    scheduled_for = scheduled_for or now.isoformat()
    run_id = db.create_run(scheduled_for, now.isoformat())
    if run_id is None:
        return {"duplicate": True, "scheduled_for": scheduled_for}
    try:
        snapshot = demo_snapshot() if mode == "demo" else fetch_market_snapshot()
        snapshot_id = db.save_snapshot(snapshot)
        decision = build_decision(snapshot, float(db.settings()["monthly_budget"]), mode=mode)
        decision_id = db.save_decision(decision, snapshot_id)
        decision["id"] = decision_id
        notification = notify(decision) if mode == "live" else "disabled_demo"
        db.finish_run(run_id, "completed", datetime.now().astimezone().isoformat(),
                      time.monotonic() - started, decision_id=decision_id)
        db.add_event("run_completed", "Research draft recorded.",
                     {"run_id": run_id, "decision_id": decision_id, "notification": notification})
        return {"run_id": run_id, "decision": decision, "snapshot": snapshot, "notification": notification}
    except Exception as exc:
        error = type(exc).__name__
        db.finish_run(run_id, "failed", datetime.now().astimezone().isoformat(),
                      time.monotonic() - started, error=error)
        db.add_event("run_failed", "Draft generation failed.", {"run_id": run_id, "error_type": error})
        return {"run_id": run_id, "status": "failed", "error": error}


def scheduler_runner(scheduled_for: str) -> None:
    run_decision(scheduled_for=scheduled_for, mode=os.environ.get("BTC_RUN_MODE", "demo"))


def initialize() -> None:
    global STORE, SCHEDULER
    STORE = Store(ROOT / os.environ.get("BTC_DB_PATH", "data/app.db"))
    SCHEDULER = Scheduler(STORE, scheduler_runner)


def state_payload() -> dict:
    db = store()
    settings = db.settings()
    now = datetime.now(resolve_timezone(settings["timezone"]))
    hour, minute = [int(part) for part in settings["schedule_time"].split(":")]
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return {"now": now.isoformat(), "settings": settings, "next_run": next_run.isoformat(),
            "live_enabled": os.environ.get("BTC_ALLOW_LIVE") == "true",
            "scheduler": {"alive": bool(SCHEDULER and SCHEDULER.is_alive()),
                          "last_heartbeat": SCHEDULER.last_heartbeat.isoformat()
                          if SCHEDULER and SCHEDULER.last_heartbeat else None},
            "snapshot": db.latest_snapshot(), "decision": db.latest_decision(),
            "runs": db.recent_runs(), "events": db.recent_events()}


class Handler(BaseHTTPRequestHandler):
    server_version = "BTCDCA/0.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def _json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("JSON required")
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            raise ValueError("Cross-origin write rejected")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 16384:
            raise ValueError("Invalid body size")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("Object required")
        return body

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(state_payload())
        elif path == "/health":
            self._json({"ok": True, "scheduler_alive": bool(SCHEDULER and SCHEDULER.is_alive())})
        elif path in {"/", "/index.html"}:
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
        else:
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _serve_static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/run":
                self._json(run_decision(mode=body.get("mode", "demo")))
            elif path == "/api/settings":
                settings = store().update_settings(body)
                store().add_event("settings_updated", "Schedule or budget updated.")
                self._json({"settings": settings})
            elif match := re.fullmatch(r"/api/decisions/(\d+)/(confirm|skip)", path):
                decision_id = int(match[1])
                status = "confirmed" if match[2] == "confirm" else "skipped"
                updated = store().update_decision_status(decision_id, status)
                if updated:
                    store().add_event(status, "Research choice recorded.", {"decision_id": decision_id})
                self._json({"updated": updated, "decision_id": decision_id})
            else:
                self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, KeyError, UnicodeError):
            self._json({"error": "invalid_request"}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local research dashboard; offline demo by default.")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        initialize()
        if store().latest_snapshot() is None:
            run_decision(mode="demo")
        if os.environ.get("BTC_SCHEDULER_ENABLED") == "true":
            SCHEDULER.start()
        print(f"BTC research dashboard: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if SCHEDULER:
            SCHEDULER.stop()
            if SCHEDULER.is_alive():
                SCHEDULER.join(timeout=35)
        server.server_close()


if __name__ == "__main__":
    main()
