import http.client
import json
import math
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import app
import market_data
import notifier
from decision_engine import build_decision
from scheduler import Scheduler
from storage import Store

ROOT = Path(__file__).resolve().parents[1]


class LocalCase(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.network = patch("market_data.urlopen", side_effect=AssertionError("External network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.webhook = patch("notifier.urlopen", side_effect=AssertionError("External network forbidden"))
        self.webhook.start()
        self.addCleanup(self.webhook.stop)
        self.temp = tempfile.TemporaryDirectory(prefix=".test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.db = Store(Path(self.temp.name) / "test.db")
        self.previous_store, self.previous_scheduler = app.STORE, app.SCHEDULER
        app.STORE, app.SCHEDULER = self.db, Scheduler(self.db, app.scheduler_runner)
        self.addCleanup(self.restore_app)

    def restore_app(self):
        app.STORE, app.SCHEDULER = self.previous_store, self.previous_scheduler


class DecisionTests(LocalCase):
    def test_synthetic_draft_has_no_real_budget(self):
        decision = build_decision(market_data.demo_snapshot(), 0)
        self.assertEqual((decision["status"], decision["multiplier"], decision["planned_amount"]), ("pending", 1.5, 0))

    def test_unavailable_data_blocks(self):
        self.assertEqual(build_decision({"status": "failed"}, 0)["status"], "blocked")

    def test_volatility_caps_multiplier(self):
        snapshot = {**market_data.demo_snapshot(), "volatility30": 0.9}
        self.assertEqual(build_decision(snapshot, 400)["planned_amount"], 75)

    def test_rule_branches(self):
        for drawdown, price, expected in [(-0.25, 100, 1.25), (-0.15, 100, 1), (-0.05, 120, 0.75)]:
            with self.subTest(drawdown=drawdown):
                snapshot = {**market_data.demo_snapshot(), "drawdown365": drawdown, "price": price}
                self.assertEqual(build_decision(snapshot, 400)["multiplier"], expected)

    def test_invalid_inputs_rejected(self):
        for budget in (-1, math.nan, math.inf):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                build_decision(market_data.demo_snapshot(), budget)


class MarketTests(LocalCase):
    def test_constant_samples(self):
        stats = market_data._stats([100.0] * 365)
        self.assertEqual((stats["price"], stats["sma200"], stats["drawdown365"], stats["volatility30"]), (100, 100, 0, 0))

    def test_short_and_bad_data_rejected(self):
        for values in ([100.0] * 30, [100.0] * 364 + [0], [100.0] * 364 + [math.nan]):
            with self.assertRaises(market_data.MarketDataError):
                market_data._stats(values)

    def test_binance_omits_unfinished_candle(self):
        now = datetime.now(timezone.utc).timestamp() * 1000
        rows = [[0, 0, 0, 0, 100, 0, now - 1000] for _ in range(365)]
        rows.append([0, 0, 0, 0, 999, 0, now + 100000])
        with patch("market_data._get_json", return_value=rows):
            snapshot = market_data.fetch_market_snapshot()
        self.assertEqual(snapshot["price"], 100)
        self.assertEqual(snapshot["meta"]["samples"], 365)

    def test_fallback_uses_past_daily_samples(self):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        prices = [[(today - timedelta(days=day)).timestamp() * 1000, 100] for day in range(365, 0, -1)]
        prices.append([today.timestamp() * 1000, 999])
        with patch("market_data._get_json", side_effect=[OSError("offline"), {"prices": prices}]):
            snapshot = market_data.fetch_market_snapshot()
        self.assertEqual(snapshot["source"], "coingecko_public_daily")
        self.assertEqual(snapshot["price"], 100)

    def test_provider_failure_is_generic(self):
        with patch("market_data._get_json", side_effect=OSError("private detail")):
            with self.assertRaises(market_data.MarketDataError) as caught:
                market_data.fetch_market_snapshot()
        self.assertNotIn("private detail", str(caught.exception))


class StoreTests(LocalCase):
    def test_settings_default_and_allowlist(self):
        self.assertEqual(self.db.settings()["monthly_budget"], "0")
        self.db.update_settings({"monthly_budget": 400, "BTC_WEBHOOK_TOKEN": "fake"})
        self.assertNotIn("BTC_WEBHOOK_TOKEN", self.db.settings())

    def test_settings_validate_before_write(self):
        for values in ({"schedule_time": "25:00"}, {"monthly_budget": -1}, {"monthly_budget": "NaN"},
                       {"max_misfire_minutes": -1}, {"timezone": "invalid/zone"}):
            with self.assertRaises((ValueError, KeyError)):
                self.db.update_settings(values)
        self.assertEqual(self.db.settings()["monthly_budget"], "0")

    def test_amount_is_persisted(self):
        self.db.update_settings({"monthly_budget": 400})
        app.run_decision()
        self.assertEqual(self.db.latest_decision()["planned_amount"], 150)

    def test_terminal_choice_cannot_be_overwritten(self):
        result = app.run_decision()
        ident = result["decision"]["id"]
        self.assertTrue(self.db.update_decision_status(ident, "confirmed"))
        self.assertFalse(self.db.update_decision_status(ident, "skipped"))
        self.assertEqual(self.db.latest_decision()["status"], "confirmed")

    def test_missed_key_is_unique(self):
        self.assertTrue(self.db.mark_missed("synthetic-slot", "Missed"))
        self.assertFalse(self.db.mark_missed("synthetic-slot", "Missed"))
        self.assertEqual(len(self.db.recent_runs()), 1)


class RunnerTests(LocalCase):
    def test_demo_never_fetches_or_notifies(self):
        with patch("app.fetch_market_snapshot") as fetch, patch("app.notify") as notify:
            result = app.run_decision()
        fetch.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(result["notification"], "disabled_demo")

    def test_live_and_unknown_mode_disabled(self):
        for mode in ("live", "unknown"):
            with self.assertRaises(ValueError):
                app.run_decision(mode=mode)
        self.assertEqual(self.db.recent_runs(), [])

    def test_scheduled_key_is_idempotent(self):
        app.run_decision("synthetic-slot")
        self.assertTrue(app.run_decision("synthetic-slot")["duplicate"])
        self.assertEqual(len(self.db.recent_runs()), 1)

    def test_failure_does_not_persist_exception_message(self):
        os.environ["BTC_ALLOW_LIVE"] = "true"
        with patch("app.fetch_market_snapshot", side_effect=RuntimeError("private detail")):
            result = app.run_decision(mode="live")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private detail", json.dumps(self.db.recent_events()))

    def test_mock_live_notification_failure_preserves_draft(self):
        os.environ["BTC_ALLOW_LIVE"] = "true"
        with patch("app.fetch_market_snapshot", return_value={**market_data.demo_snapshot(), "status": "ok"}), patch("app.notify", return_value="failed"):
            result = app.run_decision(mode="live")
        self.assertEqual(result["notification"], "failed")
        self.assertEqual(self.db.recent_runs()[0]["status"], "completed")


class SchedulerTests(LocalCase):
    def test_before_and_during_grace_window(self):
        scheduler = app.SCHEDULER
        scheduler.tick(datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc))
        self.assertEqual(self.db.recent_runs(), [])
        scheduler.tick(datetime(2026, 1, 1, 9, 1, tzinfo=timezone.utc))
        scheduler.tick(datetime(2026, 1, 1, 9, 2, tzinfo=timezone.utc))
        self.assertEqual(len(self.db.recent_runs()), 1)

    def test_after_grace_records_missed_once(self):
        for _ in range(2):
            app.SCHEDULER.tick(datetime(2026, 1, 1, 9, 16, tzinfo=timezone.utc))
        self.assertEqual(self.db.recent_runs()[0]["status"], "missed")
        self.assertEqual(len(self.db.recent_events()), 1)


class WebhookTests(LocalCase):
    def test_unconfigured_is_disabled(self):
        self.assertEqual(notifier.notify({}), "disabled")

    def test_plain_http_is_rejected(self):
        os.environ["BTC_WEBHOOK_URL"] = "http://example.invalid/receiver"
        self.assertEqual(notifier.notify({}), "invalid_configuration")

    def test_mock_receiver_gets_minimal_payload(self):
        os.environ["BTC_WEBHOOK_URL"] = "https://example.invalid/receiver"
        os.environ["BTC_WEBHOOK_TOKEN"] = "fake"
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("notifier.urlopen", return_value=response) as send:
            result = notifier.notify({"id": 1, "action": "base_dca", "multiplier": 1,
                                      "status": "pending", "planned_amount": 100})
        request = send.call_args.args[0]
        self.assertEqual(result, "sent")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake")
        self.assertNotIn("planned_amount", json.loads(request.data))

    def test_error_details_are_not_returned(self):
        os.environ["BTC_WEBHOOK_URL"] = "https://example.invalid/receiver"
        with patch("notifier.urlopen", side_effect=OSError("private detail")):
            self.assertEqual(notifier.notify({"id": 1, "action": "base_dca", "multiplier": 1, "status": "pending"}), "failed")

    def test_redirect_does_not_forward_credentials(self):
        from http.server import BaseHTTPRequestHandler
        seen = []

        class RedirectReceiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectReceiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/receiver", headers={"Authorization": "Bearer fake"})
            with self.assertRaises(HTTPError):
                build_opener(notifier.NoRedirect()).open(request, timeout=3)
            self.assertEqual(seen, ["/receiver"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class HTTPTests(LocalCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body, headers or {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_health(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["scheduler_alive"])

    def test_static_assets_and_no_corrupt_text(self):
        for path in ("/", "/static/app.js", "/static/styles.css"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertNotIn("\ufffd", body.decode("utf-8"))

    def test_demo_settings_and_confirmation_readback(self):
        status, _ = self.request("POST", "/api/settings", '{"monthly_budget":400}')
        self.assertEqual(status, 200)
        status, body = self.request("POST", "/api/run", '{"mode":"demo"}')
        self.assertEqual(status, 200)
        ident = json.loads(body)["decision"]["id"]
        self.request("POST", f"/api/decisions/{ident}/confirm", "{}")
        _, state = self.request("GET", "/api/state")
        decision = json.loads(state)["decision"]
        self.assertEqual((decision["status"], decision["planned_amount"]), ("confirmed", 150))

    def test_malformed_json_and_wrong_shape(self):
        for body in ("{", "[]", "null"):
            self.assertEqual(self.request("POST", "/api/run", body)[0], 400)

    def test_live_disabled_via_http(self):
        self.assertEqual(self.request("POST", "/api/run", '{"mode":"live"}')[0], 400)

    def test_invalid_settings(self):
        self.assertEqual(self.request("POST", "/api/settings", '{"schedule_time":"25:00"}')[0], 400)

    def test_cross_origin_and_non_json_write_rejected(self):
        headers = {"Content-Type": "application/json", "Origin": "https://example.invalid"}
        self.assertEqual(self.request("POST", "/api/run", "{}", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/run", "{}", {"Content-Type": "text/plain"})[0], 400)

    def test_static_traversal_and_unknown_route(self):
        for path in ("/static/../storage.py", "/static/", "/unknown"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/decisions/1", "{}")[0], 404)


if __name__ == "__main__":
    unittest.main()
