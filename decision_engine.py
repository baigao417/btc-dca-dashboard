from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def build_decision(snapshot: dict[str, Any], monthly_budget: float, mode: str = "research") -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    if snapshot.get("status") not in {"ok", "demo"}:
        return {"created_at": created_at, "mode": mode, "action": "data_insufficient",
                "multiplier": 0.0, "status": "blocked", "planned_amount": 0.0,
                "rationale": ["Market data unavailable; no actionable draft."]}
    price, sma200, drawdown, volatility = [float(snapshot[key]) for key in
                                         ("price", "sma200", "drawdown365", "volatility30")]
    if not all(math.isfinite(value) for value in (price, sma200, drawdown, volatility, monthly_budget)):
        raise ValueError("Non-finite input")
    if price <= 0 or sma200 <= 0 or monthly_budget < 0 or not -1 <= drawdown <= 0 or volatility < 0:
        raise ValueError("Invalid decision input")
    trend_ok = price >= sma200
    rationale = [f"Price: {price:.2f}; 200-day average: {sma200:.2f}.",
                 f"365-day drawdown: {drawdown:.1%}; annualized 30-day volatility: {volatility:.1%}."]
    if drawdown <= -0.35 and not trend_ok:
        action, multiplier = "increase_dca", 1.50
    elif drawdown <= -0.20:
        action, multiplier = "increase_dca", 1.25
    elif trend_ok and drawdown > -0.10:
        action, multiplier = "slow_dca", 0.75
    else:
        action, multiplier = "base_dca", 1.00
    if volatility >= 0.85:
        multiplier = min(multiplier, 0.75)
        rationale.append("High volatility caps the research multiplier.")
    rationale.append("Research only; confirmation records a choice, not a trade.")
    return {"created_at": created_at, "mode": mode, "action": action,
            "multiplier": multiplier, "status": "pending", "rationale": rationale,
            "planned_amount": round(monthly_budget / 4 * multiplier, 2)}
