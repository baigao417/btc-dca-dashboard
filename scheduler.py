from __future__ import annotations

import threading
from datetime import datetime, timedelta
from timezones import resolve_timezone


class Scheduler(threading.Thread):
    def __init__(self, store, runner, interval_seconds: int = 5):
        super().__init__(daemon=True)
        self.store = store
        self.runner = runner
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.last_heartbeat: datetime | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.store.add_event("scheduler_error", "Scheduler tick failed.", {"error_type": type(exc).__name__})
            self.stop_event.wait(self.interval_seconds)

    def tick(self, now: datetime | None = None) -> None:
        settings = self.store.settings()
        tz = resolve_timezone(settings.get("timezone", "UTC"))
        current = now.astimezone(tz) if now else datetime.now(tz)
        self.last_heartbeat = current
        hour, minute = [int(part) for part in settings["schedule_time"].split(":")]
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < scheduled:
            return
        if current <= scheduled + timedelta(minutes=int(settings["max_misfire_minutes"])):
            self.runner(scheduled.isoformat())
        else:
            self.store.mark_missed(scheduled.isoformat(), "Outside grace period; no automatic catch-up.")
