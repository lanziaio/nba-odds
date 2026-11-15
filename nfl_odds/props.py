"""NFL player props: project passing / rushing / receiving yards, receptions, passing TDs and
touchdowns, price every Kalshi line, pick the best 1-2 per team.

Projection for player i and stat s (the NFL has no minutes, so it works on usage):
  base_i,s   = his recency-weighted average in the games he played this season
  + freed    = production of injured teammates (who were playing lately), handed to the healthy
               players in proportion to their own share, capped at +40% of their base
  x factors  = game script from Vegas (implied team points; favourites run more, underdogs pass more)
               x the square root of what the opponent allows vs league average (the Vegas total already
               knows the defense, so the full factor would count it twice)
Touchdowns: a TD rate per opportunity (carries + targets), shrunk toward the league rate, times his
expected opportunities and the Vegas implied points (raw TD counts over 9 games are mostly noise).
Passing yards / TDs go to the expected starting QB only (depth chart, injury report); a backup
starting gets the team's passing average with a 10% haircut.
Distribution: gamma for yards (right-skewed), negative binomial for receptions, Poisson for TDs,
with spread from the player's own games shrunk toward typical NFL spreads.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from nba_odds import props as nba_props
from nba_odds.model import expit, kalshi_fee, logit

from .kalshi import code

STATS = ("pass_yds", "pass_td", "rush_yds", "rec_yds", "rec", "td")
STAT_LABEL = {"pass_yds": "passing yds", "pass_td": "passing TDs", "rush_yds": "rushing yds",
              "rec_yds": "receiving yds", "rec": "receptions", "td": "touchdowns"}
KALSHI_SERIES = {"KXNFLPASSYDS": "pass_yds", "KXNFLPASSTDS": "pass_td", "KXNFLRSHYDS": "rush_yds",
                 "KXNFLRECYDS": "rec_yds", "KXNFLREC": "rec", "KXNFLTD": "td"}

RECENCY = 0.85          # weight of game i back = RECENCY ** i
MARKET_WEIGHT = 0.70    # log-odds weight of Kalshi's own price: NFL samples are ~9 games, roles change weekly
DEFENSE_SHRINK = 4      # games of pseudo-sample pulling an "allowed" factor toward 1.0
VAR_SHRINK = 6          # games of pseudo-sample pulling a player's spread toward the typical one
FREED_CAP = 0.40        # a healthy player gains at most +40% of his own base from injured teammates
BACKUP_QB_HAIRCUT = 0.90
CV = {"pass_yds": 0.30, "rush_yds": 0.60, "rec_yds": 0.70}   # typical sd / mean for yards
REC_DISPERSION = 1.15   # receptions: variance = 1.15 x mean (close to Poisson)
LEAGUE_FALLBACK = {"pass_yds": 215.0, "rush_yds": 118.0, "rec": 21.0, "rec_yds": 215.0, "td": 2.5, "pass_td": 1.45,
                   "opps": 58.0, "pts": 22.5}
TD_PRIOR_OPPS = 40.0    # the league TD rate counts as this many carries + targets of evidence
FACTOR_RANGE = (0.75, 1.30)
ROLE_CV = 0.25          # extra spread for usage that can change week to week (role, game flow)
# Pick guardrails: the market sees news this model can't (role changes, practice reports).
PICK_MIN_GAMES = 4
PICK_MAX_DISAGREE = 0.25  # skip a line when the model and Kalshi differ by more than 25 points of probability


# ---------- distributions ----------

def _gamma_p(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x)."""
    if x <= 0:
        return 0.0
    gln = math.lgamma(a)
    if x < a + 1:  # series
        term = total = 1.0 / a
        ap = a
        for _ in range(500):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-12:
                break
        return total * math.exp(-x + a * math.log(x) - gln)
    # continued fraction (Lentz)
    b, c, d = x + 1 - a, 1e300, 1.0 / (x + 1 - a)
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return 1.0 - math.exp(-x + a * math.log(x) - gln) * h


def p_at_least(k: float, mean: float, var: float, stat: str) -> float:
    """P(X >= k) for an integer line k."""
    if mean <= 0:
        return 0.0
    if stat in CV:  # yards: gamma, continuity-corrected
        var = max(var, 1.0)
        shape, scale = mean * mean / var, var / mean
        return max(0.0, 1.0 - _gamma_p(shape, max(k - 0.5, 0.0) / scale))
    if stat == "rec":
        return nba_props.p_at_least(k, mean, max(var, mean * 1.02), "reb")  # negative binomial
    return nba_props.p_at_least(k, mean, mean, "ast")  # Poisson (var == mean)


def _prior_var(stat: str, mean: float) -> float:
    if stat in CV:
        return (CV[stat] * mean + 8.0) ** 2
    if stat == "rec":
        return REC_DISPERSION * max(mean, 0.3) + (ROLE_CV * mean) ** 2
    return max(mean, 0.05)


# ---------- league / opponent context ----------

def team_sum(rows: list[dict], stat: str) -> float:
    if stat == "opps":
        return sum(r.get("rush_att", 0) + r.get("tgt", 0) for r in rows)
    return sum(r.get(stat, 0) for r in rows)


def league_baseline(cache_dir: Path) -> dict:
    """Average team totals per game across every cached NFL box score."""
    keys = STATS + ("opps",)
    tot = {s: 0.0 for s in keys}
    n = 0
    for f in cache_dir.glob("*.json") if cache_dir.exists() else []:
        try:
            box = json.loads(f.read_text())
        except Exception:
            continue
        for rows in box.values():
            n += 1
            for s in keys:
                tot[s] += team_sum(rows, s)
    if n < 20:
        return {**LEAGUE_FALLBACK, "team_games": n}
    out = {s: tot[s] / n for s in keys}
    out["rec_yds"] = out["pass_yds"] = (out["pass_yds"] + out["rec_yds"]) / 2  # gross passing ~ receiving yards
    return {**out, "pts": LEAGUE_FALLBACK["pts"], "team_games": n}


def allowed_factors(team_id: str, boxes: list[dict], league: dict) -> dict:
    """What `team_id`'s defense allows (opponents' production) vs league average, shrunk toward 1."""
    out = {}
    w_tot, sums = 0.0, {s: 0.0 for s in ("pass_yds", "rush_yds", "rec", "td")}
    for i, box in enumerate(boxes):
        opp = [rows for tid, rows in box.items() if tid != team_id]
        if not opp or team_id not in box:
            continue
        w = RECENCY ** i
        w_tot += w
        for s in sums:
            sums[s] += w * team_sum(opp[0], s)
    n = sum(1 for b in boxes if team_id in b)
    for s in sums:
        if not w_tot or not league.get(s):
            out[s] = 1.0
            continue
        raw = (sums[s] / w_tot) / league[s]
        out[s] = round(1 + (raw - 1) * n / (n + DEFENSE_SHRINK), 3)
    out["games"] = n
    return out


# ---------- projection ----------

def project_team(games: list[list[dict]], team_pts: list[float | None], injuries_by_pid: dict[str, float],
                 opp_allowed: dict, league: dict, implied_pts: float | None, team_spread: float | None,
                 qb: dict | None) -> dict:
    """games: the team's player rows for each game this season, most recent first (team_pts aligned)."""
    weights = [RECENCY ** i for i in range(len(games))]
    wsum = sum(weights) or 1.0
    team_avg = {s: sum(w * team_sum(g, s) for w, g in zip(weights, games)) / wsum for s in STATS}
    pts_vals = [(w, p) for w, p in zip(weights, team_pts) if p is not None]
    team_pts_avg = (sum(w * p for w, p in pts_vals) / sum(w for w, _ in pts_vals)) if pts_vals else league["pts"]
    team_pts_avg = (len(pts_vals) * team_pts_avg + 4 * league["pts"]) / (len(pts_vals) + 4)  # shrink

    # --- factors: opponent + game script ---
    script = 1.0
    td_factor = opp_allowed.get("td", 1.0)
    if implied_pts:
        script = min(1.2, max(0.85, (implied_pts / team_pts_avg) ** 0.35))
        td_factor = min(1.6, max(0.6, implied_pts / team_pts_avg))
    tilt = 0.0 if team_spread is None else -team_spread  # + = favourite by that many points
    run_tilt, pass_tilt = 1 + 0.006 * tilt, 1 - 0.003 * tilt
    clamp = lambda x: min(FACTOR_RANGE[1], max(FACTOR_RANGE[0], x))  # noqa: E731
    def opp(k: str) -> float:  # square root: the Vegas total already prices most of the defense
        return opp_allowed.get(k, 1.0) ** (0.5 if implied_pts else 1.0)
    factor = {
        "pass_yds": clamp(opp("pass_yds") * script * pass_tilt),
        "rec_yds": clamp(opp("pass_yds") * script * pass_tilt),
        "rec": clamp(opp("rec") * script * pass_tilt),
        "rush_yds": clamp(opp("rush_yds") * script * run_tilt),
        "td": td_factor, "pass_td": td_factor,
    }
    league_td_rate = league.get("td", LEAGUE_FALLBACK["td"]) / league.get("opps", LEAGUE_FALLBACK["opps"])

    # --- per-player logs ---
    players: dict[str, dict] = {}
    for i, rows in enumerate(games):
        for r in rows:
            p = players.setdefault(r["athlete_id"], {"name": r.get("name"), "logs": [], "w": 0.0, "idx": []})
            p["logs"].append(r)
            p["idx"].append(i)
            p["w"] += weights[i]
    recent_ids = {r["athlete_id"] for g in games[:3] for r in g}

    def val(r: dict, s: str) -> float:
        return r.get("rush_att", 0) + r.get("tgt", 0) if s == "opps" else r.get(s, 0)

    def base(p: dict, s: str) -> float:
        return sum(weights[i] * val(r, s) for i, r in zip(p["idx"], p["logs"])) / p["w"] if p["w"] else 0.0

    bases = {pid: {s: base(p, s) for s in STATS + ("opps",)} for pid, p in players.items()}
    # TD rate per opportunity, shrunk toward the league rate
    td_rate = {}
    for pid, p in players.items():
        w_td = sum(weights[i] * r.get("td", 0) for i, r in zip(p["idx"], p["logs"]))
        w_op = sum(weights[i] * val(r, "opps") for i, r in zip(p["idx"], p["logs"]))
        td_rate[pid] = (w_td + TD_PRIOR_OPPS * league_td_rate) / (w_op + TD_PRIOR_OPPS)

    # --- injured teammates' production goes to the healthy players ---
    boost = {pid: {s: 0.0 for s in STATS + ("opps",)} for pid in players}
    for s in ("rush_yds", "rec_yds", "rec", "opps"):
        freed = 0.0
        for pid, p in players.items():
            miss = injuries_by_pid.get(pid, 0.0)
            if miss and pid in recent_ids:
                share_recent = sum(1 for g in games[:3] if any(r["athlete_id"] == pid for r in g)) / max(1, min(3, len(games)))
                freed += miss * bases[pid][s] * share_recent
        takers = {pid: bases[pid][s] for pid in players if pid in recent_ids and not injuries_by_pid.get(pid)
                  and bases[pid][s] > 0 and pid != (qb or {}).get("starter_id")}
        tot = sum(takers.values())
        for pid, b in takers.items():
            boost[pid][s] = min(FREED_CAP * b, freed * b / tot) if tot else 0.0

    out = {}
    starter_qb = (qb or {}).get("starter_id")
    for pid, p in players.items():
        if pid not in recent_ids and pid != starter_qb:
            continue
        proj, var, avg, recent = {}, {}, {}, {}
        n = len(p["logs"])
        for s in STATS:
            vals = [r.get(s, 0) for r in p["logs"]]
            avg[s] = round(sum(vals) / n, 1)
            recent[s] = vals[:10]
            if s in ("pass_yds", "pass_td"):
                if pid != starter_qb:
                    proj[s] = 0.0
                    continue
                mu = team_avg[s] * (BACKUP_QB_HAIRCUT if qb.get("backup_starting") else 1.0)
            elif s == "td":
                mu = td_rate[pid] * (bases[pid]["opps"] + boost[pid]["opps"])
            else:
                mu = bases[pid][s] + boost[pid][s]
            proj[s] = mu * factor[s]
            m = sum(vals) / n
            s2 = sum((v - m) ** 2 for v in vals) / (n - 1) if n > 1 else _prior_var(s, proj[s])
            if m > 0:
                ratio = proj[s] / m
                s2 *= ratio ** 2 if s in CV else ratio
            var[s] = (min(n, 10) * s2 + VAR_SHRINK * _prior_var(s, proj[s])) / (min(n, 10) + VAR_SHRINK)
        out[pid] = {"athlete_id": pid, "name": p["name"], "games": n, "avg": avg,
                    "recent_games": sum(1 for i in p["idx"] if i < 3),
                    "proj": {s: round(proj[s], 2 if s in ("td", "pass_td") else 1) for s in STATS},
                    "_var": var, "_recent": recent, "miss_prob": injuries_by_pid.get(pid, 0.0)}

    # A starting QB with no snaps this season (backup forced in): passing from the team average.
    if starter_qb and starter_qb not in out:
        mu = {s: team_avg[s] * BACKUP_QB_HAIRCUT * factor[s] for s in ("pass_yds", "pass_td")}
        out[starter_qb] = {
            "athlete_id": starter_qb, "name": qb.get("starter_name"), "games": 0, "recent_games": 0,
            "avg": {s: None for s in STATS},
            "proj": {**{s: 0.0 for s in STATS}, "pass_yds": round(mu["pass_yds"], 1), "pass_td": round(mu["pass_td"], 2)},
            "_var": {s: _prior_var(s, mu.get(s, 0.0)) for s in STATS}, "_recent": {s: [] for s in STATS},
            "miss_prob": 0.0}
    return {"players": out, "team_avg": {s: round(v, 1) for s, v in team_avg.items()},
            "factor": {s: round(v, 3) for s, v in factor.items()}, "team_pts_avg": round(team_pts_avg, 1)}


def hit_average(p: dict) -> dict:
    out = {}
    for s in STATS:
        avg = p["avg"].get(s)
        if not avg or p["proj"][s] <= 0:
            continue
        k = max(1, math.ceil(avg - 1e-9))
        out[s] = {"line": k, "p": round(p_at_least(k, p["proj"][s], p["_var"][s], s), 3)}
    return out


# ---------- Kalshi ----------

def kalshi_props(away_abbr: str, home_abbr: str, tip) -> list[dict]:
    rows = nba_props.kalshi_props(away_abbr, home_abbr, tip, series_map=KALSHI_SERIES, code_fn=code)
    return [m for m in rows if "D/ST" not in m["player"]]


def price_markets(markets: list[dict], side_players: dict[str, dict], team_code: str) -> list[dict]:
    by_name = {nba_props.norm_name(p["name"]): p for p in side_players.values() if p.get("name")}
    priced = []
    for m in markets:
        if m["team_code"] != team_code:
            continue
        p = by_name.get(nba_props.norm_name(m["player"]))
        if not p or p["proj"][m["stat"]] <= 0:
            continue
        p_model = p_at_least(m["line"], p["proj"][m["stat"]], p["_var"][m["stat"]], m["stat"])
        p_mkt = (m["yes_bid"] + m["yes_ask"]) / 2 if m["yes_bid"] and m["yes_ask"] else None
        prob = expit(MARKET_WEIGHT * logit(p_mkt) + (1 - MARKET_WEIGHT) * logit(p_model)) if p_mkt else p_model
        row = {**m, "athlete_id": p["athlete_id"], "name": p["name"], "proj": p["proj"][m["stat"]],
               "recent": p["_recent"][m["stat"]], "avg": p["avg"][m["stat"]],
               "p_model": round(p_model, 4), "p_market": round(p_mkt, 4) if p_mkt else None,
               "p_yes": round(prob, 4), "miss_prob": p["miss_prob"],
               "games": p["games"], "recent_games": p["recent_games"]}
        for side, pr, ask in (("yes", prob, m["yes_ask"]), ("no", 1 - prob, m["no_ask"])):
            row[f"edge_{side}"] = round(pr - ask - kalshi_fee(ask), 4) if ask else None
        priced.append(row)
    return priced


def pick(priced: list[dict]) -> list[dict]:
    """Same rules as the NBA (value 5c+, lean 2-5c, 1-2 per team), plus NFL guardrails: an established
    role (4+ games, 2 of the last 3) and no huge model-vs-market gap (that gap is usually news we lack)."""
    ok = [r for r in priced
          if r["games"] >= PICK_MIN_GAMES and r["recent_games"] >= 2
          and (r["p_market"] is None or abs(r["p_model"] - r["p_market"]) <= PICK_MAX_DISAGREE)]
    return nba_props.pick(ok, labels=STAT_LABEL)


def grade(pick_row: dict, box: dict) -> bool | None:
    """True/False once final. A player missing from the box score: QB = didn't play (void, Kalshi
    settles those at a fair price); anyone else counts as 0 (box scores only list players with a stat)."""
    for rows in box.values():
        for r in rows:
            if r["athlete_id"] == pick_row["athlete_id"]:
                hit = r.get(pick_row["stat"], 0) >= pick_row["line"]
                return hit if pick_row["side"] == "yes" else not hit
    if pick_row["stat"] in ("pass_yds", "pass_td"):
        return None
    hit = 0 >= pick_row["line"]
    return hit if pick_row["side"] == "yes" else not hit
