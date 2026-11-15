"""Kalshi public market data (no API key needed to read prices)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .http import get_json

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXNBAGAME"
ET = ZoneInfo("America/New_York")

# ESPN abbreviation -> Kalshi team code (only the ones that differ)
ESPN_TO_KALSHI = {"GS": "GSW", "SA": "SAS", "NO": "NOP", "NY": "NYK", "UTAH": "UTA", "WSH": "WAS"}


def code(espn_abbr: str) -> str:
    return ESPN_TO_KALSHI.get(espn_abbr, espn_abbr)


def event_ticker(away_abbr: str, home_abbr: str, tip_utc: datetime, series: str = SERIES, code_fn=code) -> str:
    d = tip_utc.astimezone(ET)
    return f"{series}-{d:%y}{d:%b}{d:%d}".upper() + f"{code_fn(away_abbr)}{code_fn(home_abbr)}"


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def game_prices(away_abbr: str, home_abbr: str, tip_utc: datetime, series: str = SERIES, code_fn=code) -> dict | None:
    """Kalshi game-winner prices. `series` / `code_fn` let other leagues (NFL) reuse this."""
    ticker = event_ticker(away_abbr, home_abbr, tip_utc, series, code_fn)
    data = get_json(f"{BASE}/markets", {"event_ticker": ticker, "limit": 10})
    markets = data.get("markets") or []
    if not markets:
        return None
    sides: dict[str, dict] = {}
    for m in markets:
        team_code = m["ticker"].rsplit("-", 1)[-1]
        bid, ask, last = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")), _f(m.get("last_price_dollars"))
        if bid is not None and bid <= 0:
            bid = None
        if ask is not None and ask >= 1:
            ask = None
        mid = (bid + ask) / 2 if bid is not None and ask is not None else (last or None)
        sides[team_code] = {
            "ticker": m["ticker"],
            "bid": bid,
            "ask": ask,
            "last": last if last else None,
            "mid": mid,
            "spread": round(ask - bid, 4) if bid is not None and ask is not None else None,
            "volume": _f(m.get("volume_fp")),
            "status": m.get("status"),
        }
    home, away = sides.get(code_fn(home_abbr)), sides.get(code_fn(away_abbr))
    if not home or not away:
        return None
    # Implied P(home) from both books (home YES and 1 - away YES), averaged.
    probs = [x for x in (home["mid"], (1 - away["mid"]) if away["mid"] is not None else None) if x is not None]
    p_home = sum(probs) / len(probs) if probs else None
    wide = any(s["spread"] is None or s["spread"] > 0.06 for s in (home, away))
    return {
        "event_ticker": ticker,
        "url": f"https://kalshi.com/markets/{series.lower()}",
        "home": home,
        "away": away,
        "p_home": round(p_home, 4) if p_home is not None else None,
        "illiquid": wide,
        "volume": (home["volume"] or 0) + (away["volume"] or 0),
    }
