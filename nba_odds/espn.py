"""ESPN public site API: schedule, lines (DraftKings), standings, injuries, box scores."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .http import get_json

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
STANDINGS = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"


def parse_iso(s: str) -> datetime:
    # ESPN uses "2026-11-14T00:00Z"
    s = s.replace("Z", "+00:00")
    if re.match(r".*T\d\d:\d\d\+", s):
        s = s.replace("+", ":00+", 1)
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def american(s: Any) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 100
    try:
        return int(s.replace("+", ""))
    except ValueError:
        return None


def _team(c: dict) -> dict:
    t = c["team"]
    score = c.get("score")
    if isinstance(score, dict):
        score = score.get("displayValue")
    return {
        "id": str(t["id"]),
        "abbr": t.get("abbreviation", ""),
        "name": t.get("displayName", ""),
        "nickname": t.get("name") or t.get("shortDisplayName", ""),
        "logo": t.get("logo") or (t.get("logos") or [{}])[0].get("href"),
        "score": int(score) if score not in (None, "") else None,
        "winner": c.get("winner"),
    }


def _odds(comp: dict) -> dict | None:
    odds = comp.get("odds") or []
    if not odds:
        return None
    o = odds[0]
    ml = o.get("moneyline") or {}

    def ml_side(side: str, which: str) -> int | None:
        return american(((ml.get(side) or {}).get(which) or {}).get("odds"))

    out = {
        "provider": (o.get("provider") or {}).get("name", "ESPN"),
        "details": o.get("details"),
        "home_spread": o.get("spread"),  # ESPN reports the line from the home side
        "total": o.get("overUnder"),
        "home_ml": ml_side("home", "close"),
        "away_ml": ml_side("away", "close"),
        "home_ml_open": ml_side("home", "open"),
        "away_ml_open": ml_side("away", "open"),
    }
    # Older payload shape (pickcenter style)
    if out["home_ml"] is None:
        out["home_ml"] = american((o.get("homeTeamOdds") or {}).get("moneyLine"))
        out["away_ml"] = american((o.get("awayTeamOdds") or {}).get("moneyLine"))
    return out


def scoreboard(date_yyyymmdd: str) -> list[dict]:
    data = get_json(f"{BASE}/scoreboard", {"dates": date_yyyymmdd, "limit": 50})
    games = []
    for e in data.get("events", []):
        comp = e["competitions"][0]
        st = comp.get("status", {}).get("type", {})
        teams = {c["homeAway"]: _team(c) for c in comp["competitors"]}
        games.append(
            {
                "event_id": str(e["id"]),
                "tip": parse_iso(e["date"]).isoformat(),
                "name": e.get("name"),
                "state": st.get("state"),  # pre | in | post
                "status": st.get("name"),
                "completed": bool(st.get("completed")),
                "home": teams["home"],
                "away": teams["away"],
                "odds": _odds(comp),
                "neutral": bool(comp.get("neutralSite")),
            }
        )
    return games


def standings() -> dict[str, dict]:
    data = get_json(STANDINGS)
    out: dict[str, dict] = {}
    for conf in data.get("children", []):
        for entry in conf["standings"]["entries"]:
            tid = str(entry["team"]["id"])
            stats = {s["name"]: s for s in entry["stats"]}
            by_label = {s.get("displayName") or s.get("shortDisplayName") or s["name"]: s for s in entry["stats"]}

            def num(k: str) -> float:
                v = stats.get(k, {}).get("value")
                return float(v) if v is not None else 0.0

            w, l = int(num("wins")), int(num("losses"))
            gp = w + l
            out[tid] = {
                "abbr": entry["team"].get("abbreviation"),
                "wins": w,
                "losses": l,
                "gp": gp,
                "diff_pg": round(num("pointDifferential") / gp, 2) if gp else 0.0,
                "streak": stats.get("streak", {}).get("displayValue"),
                "last10": (by_label.get("Last Ten Games") or {}).get("displayValue"),
                "home_record": (by_label.get("Home") or {}).get("displayValue"),
                "road_record": (by_label.get("Road") or {}).get("displayValue"),
                "seed": int(num("playoffSeed")) or None,
                "conference": conf.get("abbreviation") or conf.get("name"),
            }
    return out


def _athlete_id(athlete: dict) -> str | None:
    if athlete.get("id"):
        return str(athlete["id"])
    for link in athlete.get("links", []):
        m = re.search(r"/id/(\d+)", link.get("href", ""))
        if m:
            return m.group(1)
    return None


def injuries() -> dict[str, list[dict]]:
    """League-wide injury report keyed by ESPN team id."""
    data = get_json(f"{BASE}/injuries")
    out: dict[str, list[dict]] = {}
    for team in data.get("injuries", []):
        tid = str(team.get("id"))
        rows = []
        for inj in team.get("injuries", []):
            a = inj.get("athlete", {})
            details = inj.get("details") or {}
            rows.append(
                {
                    "athlete_id": _athlete_id(a),
                    "name": a.get("displayName"),
                    "status": inj.get("status"),  # Out | Day-To-Day | Questionable | Doubtful | Probable | Suspension
                    "fantasy": (details.get("fantasyStatus") or {}).get("abbreviation"),  # OUT | GTD ...
                    "detail": " ".join(x for x in (details.get("type"), details.get("detail")) if x) or None,
                    "return_date": details.get("returnDate"),
                    "comment": inj.get("shortComment"),
                    "updated": inj.get("date"),
                }
            )
        out[tid] = rows
    return out


def team_schedule(team_id: str) -> list[dict]:
    data = get_json(f"{BASE}/teams/{team_id}/schedule")
    out = []
    for e in data.get("events", []):
        comp = e["competitions"][0]
        st = comp.get("status", {}).get("type", {})
        teams = {c["homeAway"]: _team(c) for c in comp["competitors"]}
        out.append(
            {
                "event_id": str(e["id"]),
                "tip": parse_iso(e["date"]).isoformat(),
                "completed": bool(st.get("completed")),
                "home": teams.get("home"),
                "away": teams.get("away"),
            }
        )
    return out


_STAT_KEYS = {"MIN": "min", "PTS": "pts", "REB": "reb", "AST": "ast", "TO": "to", "STL": "stl",
              "BLK": "blk", "OREB": "oreb", "DREB": "dreb", "PF": "pf", "+/-": "pm"}
_SPLIT_KEYS = {"FG": ("fgm", "fga"), "FT": ("ftm", "fta"), "3PT": ("tpm", "tpa")}


def _num(v: str) -> float:
    try:
        return float(str(v).replace("+", ""))
    except ValueError:
        return 0.0


def box_lines(event_id: str, cache_dir: Path) -> dict[str, list[dict]]:
    """Player box lines for a FINISHED game, keyed by team id. Cached on disk (immutable)."""
    cache = cache_dir / f"{event_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    data = get_json(f"{BASE}/summary", {"event": event_id})
    out: dict[str, list[dict]] = {}
    for team in data.get("boxscore", {}).get("players", []):
        tid = str(team["team"]["id"])
        rows: list[dict] = []
        for block in team.get("statistics", []):
            labels = block.get("labels") or block.get("names") or []
            for a in block.get("athletes", []):
                ath = a.get("athlete", {})
                row: dict[str, Any] = {
                    "athlete_id": str(ath.get("id")),
                    "name": ath.get("displayName"),
                    "starter": bool(a.get("starter")),
                    "dnp": bool(a.get("didNotPlay")) or not a.get("stats"),
                }
                for label, val in zip(labels, a.get("stats") or []):
                    if label in _STAT_KEYS:
                        row[_STAT_KEYS[label]] = _num(val)
                    elif label in _SPLIT_KEYS and "-" in str(val):
                        m, att = str(val).split("-", 1)
                        row[_SPLIT_KEYS[label][0]] = _num(m)
                        row[_SPLIT_KEYS[label][1]] = _num(att)
                rows.append(row)
        out[tid] = rows
    if out:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, separators=(",", ":")))
    return out
