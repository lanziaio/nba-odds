"""Who matters on each team: regular starters and main rotation, from recent box scores."""
from __future__ import annotations

from collections import defaultdict

# Hollinger-style Game Score minus a replacement-level baseline, converted to points of margin.
REPLACEMENT_GS_PER_MIN = 0.20
GS_TO_MARGIN = 0.40
MAX_PLAYER_IMPACT = 9.0

# How much of a player's impact we remove for each listed status (probability he sits).
STATUS_WEIGHT = {
    "out": 1.0,
    "suspension": 1.0,
    "doubtful": 0.85,
    "questionable": 0.5,
    "day-to-day": 0.35,
    "gtd": 0.5,
    "probable": 0.1,
}


def game_score(r: dict) -> float:
    return (
        r.get("pts", 0)
        + 0.4 * r.get("fgm", 0)
        - 0.7 * r.get("fga", 0)
        - 0.4 * (r.get("fta", 0) - r.get("ftm", 0))
        + 0.7 * r.get("oreb", 0)
        + 0.3 * r.get("dreb", 0)
        + r.get("stl", 0)
        + 0.7 * r.get("ast", 0)
        + 0.7 * r.get("blk", 0)
        - 0.4 * r.get("pf", 0)
        - r.get("to", 0)
    )


def core_players(games: list[list[dict]], n_starters: int = 5, n_rotation: int = 8) -> dict:
    """games: per recent game, the team's player rows (most recent first).

    Starters = the players who started most of those games (ties -> more minutes).
    Rotation = top players by minutes per game across the sample.
    """
    agg: dict[str, dict] = defaultdict(lambda: {"starts": 0, "gp": 0, "min": 0.0, "gs": 0.0, "pts": 0.0, "name": ""})
    for rows in games:
        for r in rows:
            p = agg[r["athlete_id"]]
            p["name"] = r.get("name") or p["name"]
            if r.get("dnp") or r.get("min", 0) <= 0:
                continue
            p["gp"] += 1
            p["starts"] += 1 if r.get("starter") else 0
            p["min"] += r.get("min", 0)
            p["gs"] += game_score(r)
            p["pts"] += r.get("pts", 0)

    n_games = max(len(games), 1)
    players = {}
    for pid, p in agg.items():
        if p["gp"] == 0:
            continue
        mpg = p["min"] / p["gp"]
        gs_pg = p["gs"] / p["gp"]
        impact = max(0.0, min(MAX_PLAYER_IMPACT, GS_TO_MARGIN * (gs_pg - REPLACEMENT_GS_PER_MIN * mpg)))
        players[pid] = {
            "athlete_id": pid,
            "name": p["name"],
            "gp": p["gp"],
            "starts": p["starts"],
            # minutes over ALL sampled games, so a player who sat half of them ranks lower
            "mpg": round(mpg, 1),
            "mpg_all": round(p["min"] / n_games, 1),
            "ppg": round(p["pts"] / p["gp"], 1),
            "gs_pg": round(gs_pg, 1),
            "impact": round(impact, 2),
            # share of sampled games he played: if he has already been missing games,
            # the team's recent results partly price his absence in (avoid double counting)
            "played_share": round(p["gp"] / n_games, 2),
        }

    by_starts = sorted(players.values(), key=lambda x: (-x["starts"], -x["mpg_all"]))
    starters = [p["athlete_id"] for p in by_starts[:n_starters] if p["starts"] > 0]
    by_minutes = sorted(players.values(), key=lambda x: -x["mpg_all"])
    rotation = [p["athlete_id"] for p in by_minutes if p["athlete_id"] not in starters][: max(0, n_rotation - len(starters))]
    return {"games_sampled": len(games), "starters": starters, "rotation": rotation, "players": players}


def status_weight(inj: dict) -> float:
    for key in (inj.get("status"), inj.get("fantasy")):
        if key and key.strip().lower() in STATUS_WEIGHT:
            return STATUS_WEIGHT[key.strip().lower()]
    return 0.0


def availability(core: dict, injuries: list[dict]) -> dict:
    """Cross the core players with today's injury report."""
    by_id = {i["athlete_id"]: i for i in injuries if i.get("athlete_id")}
    by_name = {(i.get("name") or "").lower(): i for i in injuries}

    def row(pid: str, role: str) -> dict:
        p = core["players"][pid]
        inj = by_id.get(pid) or by_name.get(p["name"].lower())
        w = status_weight(inj) if inj else 0.0
        return {
            "athlete_id": pid,
            "name": p["name"],
            "role": role,
            "mpg": p["mpg_all"],
            "ppg": p["ppg"],
            "impact": p["impact"],
            "status": (inj or {}).get("status") or "Available",
            "detail": (inj or {}).get("detail"),
            "comment": (inj or {}).get("comment"),
            "miss_prob": w,
            "played_share": p["played_share"],
            "expected_loss": round(w * p["impact"] * p["played_share"], 2),
        }

    rows = [row(pid, "starter") for pid in core["starters"]] + [row(pid, "rotation") for pid in core["rotation"]]
    starters = [r for r in rows if r["role"] == "starter"]
    out_or_doubtful = [r for r in rows if r["miss_prob"] >= 0.85]
    uncertain = [r for r in rows if 0 < r["miss_prob"] < 0.85]
    return {
        "players": rows,
        "starters_available": sum(1 for r in starters if r["miss_prob"] == 0),
        "starters_total": len(starters),
        "all_starters_available": all(r["miss_prob"] == 0 for r in starters) and len(starters) == 5,
        "all_core_available": all(r["miss_prob"] == 0 for r in rows),
        "missing": [r["name"] for r in out_or_doubtful],
        "uncertain": [f'{r["name"]} ({r["status"]})' for r in uncertain],
        "injury_margin_loss": round(sum(r["expected_loss"] for r in rows), 2),
    }
