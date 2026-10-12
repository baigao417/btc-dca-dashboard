from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from urllib.request import Request, urlopen


class MarketDataError(RuntimeError):
    pass


def _get_json(url: str, timeout: int = 12) -> dict | list:
    request = Request(url, headers={"User-Agent": "btc-ahr999-bot/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _stats(closes: list[float]) -> dict[str, float]:
    if len(closes) < 365:
        raise MarketDataError("At least 365 completed daily samples required")
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        raise MarketDataError("Prices must be positive and finite")
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 30, len(closes))]
    high = max(closes[-365:])
    return {"price": closes[-1], "sma200": statistics.mean(closes[-200:]),
            "volatility30": statistics.pstdev(returns) * math.sqrt(365),
            "high365": high, "drawdown365": closes[-1] / high - 1}


def fetch_market_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    errors = []
    for provider in ("binance", "coingecko"):
        try:
            if provider == "binance":
                rows = _get_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=366")
                closes = [float(row[4]) for row in rows if float(row[6]) < now.timestamp() * 1000]
            else:
                payload = _get_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=366&interval=daily")
                daily = {}
                for stamp, value in sorted(payload["prices"]):
                    day = datetime.fromtimestamp(float(stamp) / 1000, timezone.utc).date()
                    if day < now.date():
                        daily[day] = float(value)
                closes = list(daily.values())
            return {**_stats(closes), "observed_at": now.isoformat(), "source": provider + "_public_daily",
                    "status": "ok", "meta": {"samples": len(closes), "method": "completed_daily_samples"}}
        except (OSError, ValueError, KeyError, TypeError, IndexError, MarketDataError):
            errors.append(provider + ": unavailable or insufficient data")
    raise MarketDataError("; ".join(errors))


def demo_snapshot() -> dict:
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "source": "synthetic_demo",
            "status": "demo", "price": 100.0, "sma200": 110.0, "volatility30": 0.4,
            "high365": 160.0, "drawdown365": 100.0 / 160.0 - 1,
            "meta": {"samples": 365, "method": "fictional_values_not_market_data"}}
