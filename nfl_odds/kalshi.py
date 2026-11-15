"""Kalshi NFL game markets (series KXNFLGAME), read without a key."""
from __future__ import annotations

from datetime import datetime

from nba_odds import kalshi as _k

SERIES = "KXNFLGAME"
ESPN_TO_KALSHI = {"JAX": "JAC", "WSH": "WAS"}


def code(espn_abbr: str) -> str:
    return ESPN_TO_KALSHI.get(espn_abbr, espn_abbr)


def game_prices(away_abbr: str, home_abbr: str, tip_utc: datetime) -> dict | None:
    return _k.game_prices(away_abbr, home_abbr, tip_utc, series=SERIES, code_fn=code)
