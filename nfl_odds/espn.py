"""ESPN public API for the NFL: schedule + lines, standings, injuries, depth charts, box scores."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nba_odds.espn import _athlete_id, _num, _odds, _team, parse_iso
from nba_odds.http import get_json

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
STANDINGS = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"


def _game(e: dict) -> dict:
    comp = e["competitions"][0]
    st = comp.get("status", {}).get("type", {})
    teams = {c["homeAway"]: _team(c) for c in comp["competitors"]}
    return {
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
        "season_type": (e.get("season") or {}).get("type"),
    }


def scoreboard(date_yyyymmdd: str | None = None) -> tuple[list[dict], dict]:
    """Games for one ET date, or the current NFL week when no date is given."""
    params = {"limit": 50}
    if date_yyyymmdd:
        params["dates"] = date_yyyymmdd
    data = get_json(f"{BASE}/scoreboard", params)
    week = {"number": (data.get("week") or {}).get("number"), "season": (data.get("season") or {}).get("year")}
    return [_game(e) for e in data.get("events", [])], week


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

            w, l, t = int(num("wins")), int(num("losses")), int(num("ties"))
            gp = w + l + t
            out[tid] = {
                "abbr": entry["team"].get("abbreviation"),
                "wins": w, "losses": l, "ties": t, "gp": gp,
                "diff_pg": round(num("pointDifferential") / gp, 2) if gp else 0.0,
                "pf_pg": round(num("pointsFor") / gp, 1) if gp else None,
                "pa_pg": round(num("pointsAgainst") / gp, 1) if gp else None,
                "streak": stats.get("streak", {}).get("displayValue"),
                "home_record": (by_label.get("Home") or {}).get("displayValue"),
                "road_record": (by_label.get("Road") or {}).get("displayValue"),
                "seed": int(num("playoffSeed")) or None,
                "conference": conf.get("abbreviation") or conf.get("name"),
            }
    return out


def injuries() -> dict[str, list[dict]]:
    """League-wide injury report keyed by ESPN team id."""
    data = get_json(f"{BASE}/injuries")
    out: dict[str, list[dict]] = {}
    for team in data.get("injuries", []):
        rows = []
        for inj in team.get("injuries", []):
            a = inj.get("athlete", {})
            details = inj.get("details") or {}
            rows.append({
                "athlete_id": _athlete_id(a),
                "name": a.get("displayName"),
                "pos": (a.get("position") or {}).get("abbreviation"),
                "status": inj.get("status"),  # Out | Doubtful | Questionable | Injured Reserve | Active ...
                "fantasy": (details.get("fantasyStatus") or {}).get("abbreviation"),
                "detail": " ".join(x for x in (details.get("type"), details.get("detail")) if x) or None,
                "comment": inj.get("shortComment"),
                "updated": inj.get("date"),
            })
        out[str(team.get("id"))] = rows
    return out


def depth_chart(team_id: str) -> list[dict]:
    """Every depth-chart slot: position key, the starter and his backup (with ESPN's injury tags)."""
    data = get_json(f"{BASE}/teams/{team_id}/depthcharts")
    slots = []
    for formation in data.get("depthchart", []):
        for key, v in (formation.get("positions") or {}).items():
            people = []
            for a in (v.get("athletes") or [])[:2]:
                inj = (a.get("injuries") or [{}])[0]
                people.append({"athlete_id": str(a.get("id")), "name": a.get("displayName"),
                               "status": inj.get("status") if inj else None})
            if people:
                slots.append({"slot": key.lower(), "pos": ((v.get("position") or {}).get("abbreviation") or key).upper(),
                              "formation": formation.get("name"), "starter": people[0],
                              "backup": people[1] if len(people) > 1 else None})
    return slots


def team_schedule(team_id: str) -> list[dict]:
    data = get_json(f"{BASE}/teams/{team_id}/schedule")
    return [_game(e) for e in data.get("events", [])]


# box score block -> {label: our key}
_BLOCKS = {
    "passing": {"YDS": "pass_yds", "TD": "pass_td", "INT": "int"},
    "rushing": {"CAR": "rush_att", "YDS": "rush_yds", "TD": "rush_td"},
    "receiving": {"REC": "rec", "YDS": "rec_yds", "TD": "rec_td", "TGTS": "tgt"},
    "defensive": {"TOT": "tkl", "SACKS": "sacks", "TD": "def_td"},
    "interceptions": {"TD": "int_td"},
    "kickReturns": {"TD": "kr_td"},
    "puntReturns": {"TD": "pr_td"},
    "kicking": {"PTS": "k_pts"},
}


def box_lines(event_id: str, cache_dir: Path) -> dict[str, list[dict]]:
    """Player lines for a FINISHED game, keyed by team id. Cached on disk (immutable).

    A player appears only if he recorded a stat in some block (a receiver with no target is absent).
    `td` = touchdowns he scored (rushing + receiving + returns), the way Kalshi's "touchdowns" market counts.
    """
    cache = cache_dir / f"{event_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    data = get_json(f"{BASE}/summary", {"event": event_id})
    out: dict[str, list[dict]] = {}
    for team in data.get("boxscore", {}).get("players", []):
        tid = str(team["team"]["id"])
        rows: dict[str, dict[str, Any]] = {}
        for block in team.get("statistics", []):
            keys = _BLOCKS.get(block.get("name"))
            labels = block.get("labels") or []
            for a in block.get("athletes", []):
                ath = a.get("athlete", {})
                pid = str(ath.get("id"))
                row = rows.setdefault(pid, {"athlete_id": pid, "name": ath.get("displayName"),
                                            "pos": (ath.get("position") or {}).get("abbreviation")})
                vals = dict(zip(labels, a.get("stats") or []))
                if block.get("name") == "passing" and "/" in str(vals.get("C/ATT", "")):
                    c, att = str(vals["C/ATT"]).split("/", 1)
                    row["pass_cmp"], row["pass_att"] = _num(c), _num(att)
                for label, key in (keys or {}).items():
                    if label in vals:
                        row[key] = _num(vals[label])
        for r in rows.values():
            r["td"] = sum(r.get(k, 0) for k in ("rush_td", "rec_td", "kr_td", "pr_td", "int_td", "def_td"))
        out[tid] = list(rows.values())
    if out:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, separators=(",", ":")))
    return out
