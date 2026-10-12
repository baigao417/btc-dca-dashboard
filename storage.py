from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from timezones import resolve_timezone


DEFAULT_SETTINGS = {
    "schedule_time": "09:00",
    "timezone": "UTC",
    "monthly_budget": "0",
    "max_misfire_minutes": "15",
}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    price REAL,
                    sma200 REAL,
                    volatility30 REAL,
                    high365 REAL,
                    drawdown365 REAL,
                    raw_meta TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    action TEXT NOT NULL,
                    multiplier REAL NOT NULL,
                    planned_amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    snapshot_id INTEGER,
                    FOREIGN KEY(snapshot_id) REFERENCES market_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scheduled_for TEXT NOT NULL UNIQUE,
                    started_at TEXT,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    latency_seconds REAL,
                    error TEXT,
                    decision_id INTEGER,
                    FOREIGN KEY(decision_id) REFERENCES decisions(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
            if "planned_amount" not in columns:
                conn.execute("ALTER TABLE decisions ADD COLUMN planned_amount REAL NOT NULL DEFAULT 0")
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, value),
                )

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def settings(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        values = DEFAULT_SETTINGS.copy()
        values.update({row["key"]: row["value"] for row in rows})
        return values

    def update_settings(self, values: dict[str, Any]) -> dict[str, str]:
        allowed = set(DEFAULT_SETTINGS)
        if "monthly_budget" in values:
            budget = float(values["monthly_budget"])
            if not math.isfinite(budget) or not 0 <= budget <= 1_000_000:
                raise ValueError("Invalid monthly budget")
        if "timezone" in values:
            resolve_timezone(str(values["timezone"]))
        if "schedule_time" in values:
            value = str(values["schedule_time"])
            hour, minute = [int(part) for part in value.split(":")]
            if len(value) != 5 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("Invalid schedule time")
        if "max_misfire_minutes" in values and not 0 <= int(values["max_misfire_minutes"]) <= 1440:
            raise ValueError("Invalid grace period")
        with self._connect() as conn:
            for key, value in values.items():
                if key in allowed:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)),
                    )
        return self.settings()

    def add_event(self, kind: str, message: str, payload: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(created_at, kind, message, payload) VALUES (?, ?, ?, ?)",
                (self.now_iso(), kind, message, json.dumps(payload or {}, ensure_ascii=False)),
            )

    def save_snapshot(self, snapshot: dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO market_snapshots
                (observed_at, source, status, price, sma200, volatility30, high365, drawdown365, raw_meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot["observed_at"], snapshot["source"], snapshot["status"],
                    snapshot.get("price"), snapshot.get("sma200"), snapshot.get("volatility30"),
                    snapshot.get("high365"), snapshot.get("drawdown365"),
                    json.dumps(snapshot.get("meta", {}), ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def save_decision(self, decision: dict[str, Any], snapshot_id: int | None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO decisions
                (created_at, mode, action, multiplier, planned_amount, status, rationale, snapshot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision["created_at"], decision["mode"], decision["action"],
                    decision["multiplier"], decision.get("planned_amount", 0.0), decision["status"],
                    json.dumps(decision.get("rationale", []), ensure_ascii=False), snapshot_id,
                ),
            )
            return int(cursor.lastrowid)

    def create_run(self, scheduled_for: str, started_at: str) -> int | None:
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO runs(scheduled_for, started_at, status) VALUES (?, ?, 'running')",
                    (scheduled_for, started_at),
                )
            except sqlite3.IntegrityError:
                return None
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        completed_at: str,
        latency_seconds: float,
        error: str | None = None,
        decision_id: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE runs SET status=?, completed_at=?, latency_seconds=?, error=?, decision_id=?
                WHERE id=?""",
                (status, completed_at, latency_seconds, error, decision_id, run_id),
            )

    def mark_missed(self, scheduled_for: str, message: str) -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs(scheduled_for, status, error) VALUES (?, 'missed', ?)",
                    (scheduled_for, message),
                )
            except sqlite3.IntegrityError:
                return False
        self.add_event("missed", message, {"scheduled_for": scheduled_for})
        return True

    def update_decision_status(self, decision_id: int, status: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE decisions SET status=? WHERE id=? AND status='pending'",
                (status, decision_id),
            )
            return cursor.rowcount == 1

    def _row(self, query: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(query, args).fetchone()
        return dict(row) if row else None

    def latest_snapshot(self) -> dict[str, Any] | None:
        row = self._row("SELECT * FROM market_snapshots ORDER BY id DESC LIMIT 1")
        if row:
            row["meta"] = json.loads(row.pop("raw_meta") or "{}")
        return row

    def latest_decision(self) -> dict[str, Any] | None:
        row = self._row("SELECT * FROM decisions ORDER BY id DESC LIMIT 1")
        if row:
            row["rationale"] = json.loads(row["rationale"] or "[]")
        return row

    def recent_runs(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row["payload"] or "{}")
        return rows
