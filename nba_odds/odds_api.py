"""Optional: The Odds API (the-odds-api.com) consensus across US sportsbooks.

Only used when ODDS_API_KEY is set. One call (1 credit with markets=h2h, regions=us)
returns every upcoming NBA game, so we call it at most once per run.
"""
from __future__ import annotations

import os
import statistics
from datetime import datetime

from .http import get_json
from .model import american_to_prob, devig

URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


def fetch_consensus(sport: str = "basketball_nba") -> tuple[list[dict], str | None]:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return [], None
    body, headers = get_json(
        URL.format(sport=sport),
        {"apiKey": key, "regions": "us", "markets": "h2h", "oddsFormat": "american"},
        with_headers=True,
    )
    remaining = headers.get("x-requests-remaining") or headers.get("X-Requests-Remaining")
    games = []
    for ev in body:
        probs, books = [], []
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in mk.get("outcomes", [])}
                ph = american_to_prob(prices.get(ev["home_team"]))
                pa = american_to_prob(prices.get(ev["away_team"]))
                if ph and pa:
                    probs.append(devig(ph, pa))
                    books.append(bk.get("title") or bk.get("key"))
        if probs:
            games.append(
                {
                    "home_team": ev["home_team"],
                    "away_team": ev["away_team"],
                    "commence_time": ev["commence_time"],
                    "p_home": statistics.median(probs),
                    "p_home_min": min(probs),
                    "p_home_max": max(probs),
                    "books": books,
                }
            )
    return games, remaining


def match(consensus: list[dict], home_nickname: str, away_nickname: str, tip: datetime) -> dict | None:
    for g in consensus:
        if not (g["home_team"].endswith(home_nickname) and g["away_team"].endswith(away_nickname)):
            continue
        ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        if abs((ct - tip).total_seconds()) <= 6 * 3600:
            return g
    return None
