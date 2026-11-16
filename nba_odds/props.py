"""Player props: project each player's points / rebounds / assists / threes / PRA,
price every Kalshi line, and pick the best 1-2 per team.

Projection for player i and stat s:
  minutes_i  = recent weighted minutes, + minutes freed by injured teammates, normalised to 240 per team
  rate_i,s   = recent weighted per-minute production
  raw_i,s    = minutes_i * rate_i,s
  team total = Vegas implied team points (pts), or the team's recent average x opponent's
               "allowed" factor for that stat x game-pace factor (reb / ast / threes)
  mean_i,s   = raw_i,s scaled so the team's players add up to the team total
Distribution: normal for points and PRA, negative binomial for rebounds / assists / threes,
with variance from the player's own games, shrunk toward a typical spread.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from pathlib import Path

from .http import get_json
from .kalshi import BASE, ET, code
from .model import expit, kalshi_fee, logit, norm_cdf

STATS = ("pts", "reb", "ast", "tpm", "pra")
STAT_LABEL = {"pts": "points", "reb": "rebounds", "ast": "assists", "tpm": "threes", "pra": "pts+reb+ast"}
KALSHI_SERIES = {"KXNBAPTS": "pts", "KXNBAREB": "reb", "KXNBAAST": "ast", "KXNBA3PT": "tpm", "KXNBAPRA": "pra"}

RECENCY = 0.90          # weight of game i back = RECENCY ** i (production rates)
MIN_RECENCY = 0.75      # minutes react faster to role changes
PRIOR_GAMES = 8         # last season's per-minute rates count as this many extra games
MARKET_WEIGHT = 0.60    # log-odds weight of Kalshi's own price (default until calib has 40 graded games).
                        # Every-line check 2026-11-15 (2,380 lines, 11 games): model buckets calibrated; the pick
                        # rule on all lines +6% ROI in both halves at 0.60, ~no bets left at 0.80+. Kept.
DEFENSE_SHRINK = 10     # games of pseudo-sample pulling a team's "allowed" factor toward 1.0
VAR_SHRINK = 8          # games of pseudo-sample pulling a player's variance toward the prior
TEAM_MINUTES = 240.0
BENCH_TRIM = 0.0        # share of bench minutes trimmed FIRST when the rotation overflows 240.
                        # Backtest on ~1,000 player-games (2026-11-13): 0.0 -> starters +0.6 / bench -0.1 min bias;
                        # 0.3 -> starters +3.0 / bench -2.8 and a worse MAE. Keep 0 unless a new backtest says otherwise.
MAX_MINUTES = 40.0
FREED_EVEN = 1.0         # 0 = freed minutes shared in proportion to minutes, 1 = split evenly across the rotation.
                         # Backtest 2026-11-13, games with 40+ freed minutes: proportional = starters +1.07 / bench -1.84 min,
                         # even = +0.51 / -0.86 (MAE 4.87 -> 4.66); all games: even = -0.04 / +0.34, MAE also lower.
                         # EXTRA_MINUTES_CAP 4 beat no cap (starters bias +0.64 -> +0.41, MAE 4.49 -> 4.41).
FREED_TO_ROTATION = 1.0  # share of an injured player's minutes that go to the current rotation
MAX_SCALE_UP = 1.0       # never stretch the rotation above its own minutes to fill 240 (backtest: equal or better)
EXTRA_MINUTES_CAP = 4.0  # max minutes above his normal load when teammates are out
PACE_EXP = {"reb": 0.5, "ast": 0.7, "tpm": 0.6}

PICK_MIN_EDGE = 0.05    # "value": 5 cents per $1 after fees (props are noisier than game lines)
LEAN_MIN_EDGE = 0.02    # "lean": fills the 1-2 picks per team when nothing reaches value
PICK_PRICE_RANGE = (0.20, 0.85)
PICK_MAX_SPREAD = 0.06
PICKS_PER_TEAM = 2


# ---------- helpers ----------

def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return " ".join(s.split())


def stat_of(row: dict, s: str) -> float:
    if s == "pra":
        return row.get("pts", 0) + row.get("reb", 0) + row.get("ast", 0)
    return row.get(s, 0)


def team_totals(rows: list[dict]) -> dict:
    return {s: sum(stat_of(r, s) for r in rows) for s in STATS}


def p_at_least(k: float, mean: float, var: float, stat: str) -> float:
    """P(X >= k) for an integer threshold k."""
    if mean <= 0:
        return 0.0
    if stat in ("pts", "pra"):
        sd = math.sqrt(max(var, 1.0))
        return 1.0 - norm_cdf((k - 0.5 - mean) / sd)
    # negative binomial (Poisson when not over-dispersed)
    if var <= mean * 1.01:
        cdf, term = 0.0, math.exp(-mean)
        for x in range(int(k)):
            cdf += term
            term *= mean / (x + 1)
        return max(0.0, 1.0 - cdf)
    r = mean * mean / (var - mean)
    p = r / (r + mean)
    cdf = 0.0
    for x in range(int(k)):
        cdf += math.exp(math.lgamma(x + r) - math.lgamma(r) - math.lgamma(x + 1) + r * math.log(p) + x * math.log(1 - p))
    return max(0.0, 1.0 - cdf)


def _prior_var(stat: str, mean: float) -> float:
    if stat == "pts":
        return (0.30 * mean + 2.5) ** 2
    if stat == "pra":
        return (0.25 * mean + 3.0) ** 2
    return {"reb": 1.5, "ast": 1.6, "tpm": 1.35}[stat] * max(mean, 0.3)


# ---------- league / opponent context ----------

def league_baseline(cache_dir: Path) -> dict:
    """Average team totals per game across every cached box score."""
    import json

    sums = {s: 0.0 for s in STATS}
    n = 0
    for f in cache_dir.glob("*.json"):
        box = json.loads(f.read_text())
        if len(box) != 2:
            continue
        for rows in box.values():
            t = team_totals(rows)
            if t["pts"] < 60:  # incomplete box
                continue
            for s in STATS:
                sums[s] += t[s]
            n += 1
    if not n:
        return {"pts": 114.0, "reb": 44.0, "ast": 26.0, "tpm": 13.0, "pra": 184.0, "games": 0}
    out = {s: sums[s] / n for s in STATS}
    out["games"] = n
    return out


def allowed_factors(team_id: str, boxes: list[dict], league: dict) -> dict:
    """How much of each stat this team ALLOWS opponents vs league average (shrunk toward 1)."""
    allowed = {s: [] for s in STATS}
    for box in boxes:
        opp = [tid for tid in box if tid != team_id]
        if team_id not in box or not opp:
            continue
        t = team_totals(box[opp[0]])
        if t["pts"] < 60:
            continue
        for s in STATS:
            allowed[s].append(t[s])
    out = {}
    for s in STATS:
        n = len(allowed[s])
        if not n or not league.get(s):
            out[s] = 1.0
            continue
        ratio = (sum(allowed[s]) / n) / league[s]
        out[s] = round(1 + (ratio - 1) * n / (n + DEFENSE_SHRINK), 3)
    out["games"] = len(allowed["pts"])
    return out


# ---------- projections ----------

def project_team(team_id: str, games: list[list[dict]], injuries_by_pid: dict[str, float],
                 opp_allowed: dict, league: dict, implied_pts: float | None, game_total: float | None,
                 seasons: dict[str, dict] | None = None) -> dict:
    """games: this team's player rows per recent game (most recent first)."""
    players: dict[str, dict] = {}
    for i, rows in enumerate(games):
        w = RECENCY ** i
        for r in rows:
            p = players.setdefault(r["athlete_id"], {"name": r.get("name"), "logs": [], "w": 0.0, "wmin": 0.0,
                                                     "wstat": {s: 0.0 for s in STATS}, "last3": False,
                                                     "mw": 0.0, "mmin": 0.0, "by_idx": {}})
            if r.get("dnp") or r.get("min", 0) <= 0:
                continue
            p["logs"].append(r)
            p["by_idx"][i] = r["min"]
            p["w"] += w
            p["wmin"] += w * r["min"]
            for s in STATS:
                p["wstat"][s] += w * stat_of(r, s)
            p["mw"] += MIN_RECENCY ** i
            p["mmin"] += MIN_RECENCY ** i * r["min"]
            if i < 3:
                p["last3"] = True

    # recent team totals (weighted)
    tw, tsum = 0.0, {s: 0.0 for s in STATS}
    for i, rows in enumerate(games):
        t = team_totals(rows)
        if t["pts"] < 60:
            continue
        w = RECENCY ** i
        tw += w
        for s in STATS:
            tsum[s] += w * t[s]
    team_avg = {s: (tsum[s] / tw if tw else league.get(s, 0)) for s in STATS}

    # Active rotation = played at least once in the last 3 games and ~8+ minutes when playing.
    cands = {pid: p for pid, p in players.items()
             if len(p["logs"]) >= 2 and p["last3"] and p["mmin"] / p["mw"] >= 8}
    # Minutes the rest of the roster (deep bench, garbage time) takes per game.
    bw, bsum = 0.0, 0.0
    bstat = {s: 0.0 for s in STATS}
    for i, rows in enumerate(games):
        w = MIN_RECENCY ** i
        bw += w
        others = [r for r in rows if r["athlete_id"] not in cands and not r.get("dnp")]
        bsum += w * sum(r.get("min", 0) for r in others)
        for s in STATS:
            bstat[s] += w * sum(stat_of(r, s) for r in others)
    bench = bsum / bw if bw else 0.0
    bench_stat = {s: (bstat[s] / bw if bw else 0.0) for s in STATS}
    def expected_minutes(p: dict) -> float:
        idx = p["by_idx"]
        first = max(idx)
        recent = sum(1 for i in (0, 1, 2) if i in idx)
        if 0 in idx or recent >= 2 or len(idx) / (first + 1) >= 0.6:
            # regular (or back from an absence): his last 5 appearances
            last = sorted(idx)[:5]
            ws = [MIN_RECENCY ** k for k in range(len(last))]
            return sum(w * idx[i] for w, i in zip(ws, last)) / sum(ws)
        span = range(first + 1)  # in and out of the rotation: count the games he sat
        ws = [MIN_RECENCY ** i for i in span]
        return sum(w * idx.get(i, 0.0) for w, i in zip(ws, span)) / sum(ws)

    mins, cap = {}, {}
    freed = 0.0
    for pid, p in cands.items():
        mpg = expected_minutes(p)
        miss = injuries_by_pid.get(pid, 0.0)
        freed += mpg * miss
        mins[pid] = mpg * (1 - miss)
        # nobody plays far above his normal load just because teammates are out
        cap[pid] = min(MAX_MINUTES, mpg + EXTRA_MINUTES_CAP, max(p["by_idx"].values()) + 2)
    avail_total = sum(mins.values()) or 1.0
    n_avail = sum(1 for v in mins.values() if v > 0) or 1
    for pid in mins:
        if mins[pid] <= 0:
            continue
        # blend of "even split" and "in proportion to his minutes" (stars are near their ceiling)
        share = FREED_EVEN * (1 / n_avail) + (1 - FREED_EVEN) * mins[pid] / avail_total
        mins[pid] += FREED_TO_ROTATION * freed * share
    # Part of the freed minutes goes to deep-bench players who haven't been in the rotation.
    room = max(TEAM_MINUTES - bench - freed * (1 - FREED_TO_ROTATION), 180.0)
    excess = sum(mins.values()) - room
    if excess > 0:  # too many minutes: trim the bench first (up to 30%), starters' minutes are stable
        top5 = set(sorted(mins, key=lambda k: -mins[k])[:5])
        pool = sum(v for k, v in mins.items() if k not in top5)
        cut = min(excess, BENCH_TRIM * pool)
        for k in mins:
            if k not in top5 and pool:
                mins[k] -= cut * mins[k] / pool
    for _ in range(3):  # then scale to fit 240, respecting each player's cap
        tot = sum(mins.values()) or 1.0
        # scaling DOWN is unlimited; scaling UP is limited (short rotations hand minutes to the deep bench)
        f = min(room / tot, MAX_SCALE_UP)
        for pid in mins:
            mins[pid] = min(cap[pid], mins[pid] * f)

    # Per-minute rates: recent games + last season as PRIOR_GAMES pseudo-games.
    seasons = seasons or {}
    rates = {}
    for pid, p in cands.items():
        prior = (seasons.get(pid) or {}).get("prior")
        rates[pid] = {}
        for s in STATS:
            num, den = p["wstat"][s], p["wmin"]
            if prior and prior.get("min", 0) >= 5 and prior.get("gp", 0) >= 10:
                pv = prior["pts"] + prior["reb"] + prior["ast"] if s == "pra" else prior[s]
                k = PRIOR_GAMES * min(1.0, prior["gp"] / 40)
                m_bar = p["wmin"] / p["w"] if p["w"] else prior["min"]
                num += k * m_bar * pv / prior["min"]
                den += k * m_bar
            rates[pid][s] = num / den if den else 0.0

    raw = {pid: {s: mins[pid] * rates[pid][s] for s in STATS} for pid in cands}

    league_total = 2 * league.get("pts", 114.0)
    pace = (game_total / league_total) if game_total else 1.0
    target = {}
    for s in ("reb", "ast", "tpm"):
        target[s] = team_avg[s] * opp_allowed.get(s, 1.0) * pace ** PACE_EXP[s]
    target["pts"] = implied_pts if implied_pts else team_avg["pts"] * opp_allowed.get("pts", 1.0)
    scale = {}
    for s in ("pts", "reb", "ast", "tpm"):
        # the deep bench produces some of the team total too
        tot = (sum(raw[pid][s] for pid in raw) + bench_stat[s]) or 1.0
        scale[s] = max(0.85, min(1.20, target[s] / tot))

    out = {}
    for pid, p in cands.items():
        miss = injuries_by_pid.get(pid, 0.0)
        if miss >= 1.0:
            continue  # ruled out
        # His own props are priced as if he plays (Kalshi settles at a fair price if he doesn't).
        if miss > 0:
            mins[pid] = min(cap[pid], mins[pid] / (1 - miss))
            raw[pid] = {s: mins[pid] * rates[pid][s] for s in STATS}
        if mins[pid] < 1:
            continue
        proj, var, avg = {}, {}, {}
        n = len(p["logs"])
        for s in ("pts", "reb", "ast", "tpm"):
            proj[s] = raw[pid][s] * scale[s]
        proj["pra"] = proj["pts"] + proj["reb"] + proj["ast"]
        cur = (seasons.get(pid) or {}).get("current")
        for s in STATS:
            vals = [stat_of(r, s) for r in p["logs"]]
            mu = sum(vals) / n
            if cur and cur.get("gp"):
                avg[s] = round(cur["pts"] + cur["reb"] + cur["ast"], 1) if s == "pra" else round(cur[s], 1)
            else:
                avg[s] = round(mu, 1)
            s2 = sum((v - mu) ** 2 for v in vals) / (n - 1) if n > 1 else _prior_var(s, proj[s])
            # scale the sample spread to the projected level, then shrink toward a typical spread
            ratio = proj[s] / mu if mu > 0 else 1.0
            s2 *= ratio ** 2 if s in ("pts", "pra") else ratio
            var[s] = (n * s2 + VAR_SHRINK * _prior_var(s, proj[s])) / (n + VAR_SHRINK)
        out[pid] = {
            "athlete_id": pid, "name": p["name"], "games": n,
            "minutes": round(mins[pid], 1),
            "avg": avg,
            "has_prior": bool((seasons.get(pid) or {}).get("prior")),
            "proj": {s: round(proj[s], 1) for s in STATS},
            "_var": var,
            # his last 10 games for each stat, most recent first (for hit rates on a line)
            "_recent": {s: [stat_of(r, s) for r in p["logs"][:10]] for s in STATS},
            "miss_prob": injuries_by_pid.get(pid, 0.0),
        }
    return {"players": out, "scale": {k: round(v, 3) for k, v in scale.items()}, "team_avg": {s: round(team_avg[s], 1) for s in STATS},
            "target": {s: round(target[s], 1) for s in target}, "pace": round(pace, 3)}


def hit_average(proj_player: dict) -> dict:
    """P(player reaches his recent average) for pts / reb / ast / threes."""
    out = {}
    for s in ("pts", "reb", "ast", "tpm"):
        avg = proj_player["avg"][s]
        k = max(1, math.ceil(avg - 1e-9))
        out[s] = {"line": k, "p": round(p_at_least(k, proj_player["proj"][s], proj_player["_var"][s], s), 3)}
    return out


# ---------- Kalshi props ----------

def kalshi_props(away_abbr: str, home_abbr: str, tip, series_map: dict | None = None, code_fn=code) -> list[dict]:
    """Every open Kalshi prop line for one game. `series_map` / `code_fn` let other leagues (NFL) reuse this."""
    d = tip.astimezone(ET)
    suffix = f"{d:%y}{d:%b}{d:%d}".upper() + f"{code_fn(away_abbr)}{code_fn(home_abbr)}"
    out = []
    for series, stat in (series_map or KALSHI_SERIES).items():
        time.sleep(0.3)  # stay well under Kalshi's public rate limit
        data = get_json(f"{BASE}/markets", {"event_ticker": f"{series}-{suffix}", "limit": 1000})
        for m in data.get("markets") or []:
            if m.get("status") not in ("active", "open"):
                continue
            try:
                strike = float(m["floor_strike"])
            except (KeyError, TypeError, ValueError):
                continue
            seg = m["ticker"].split("-")[2] if m["ticker"].count("-") >= 3 else ""
            team_code = next((c for c in sorted((code_fn(away_abbr), code_fn(home_abbr)), key=len, reverse=True) if seg.startswith(c)), None)

            def f(k):
                try:
                    v = float(m.get(k))
                except (TypeError, ValueError):
                    return None
                return v if 0 < v < 1 else None

            out.append({
                "ticker": m["ticker"], "stat": stat, "player": (m.get("yes_sub_title") or "").split(":")[0].strip(),
                "team_code": team_code, "line": math.floor(strike) + 1,  # "15+" = strike 14.5
                "yes_bid": f("yes_bid_dollars"), "yes_ask": f("yes_ask_dollars"),
                "no_bid": f("no_bid_dollars"), "no_ask": f("no_ask_dollars"),
                "volume": float(m.get("volume_fp") or 0),
            })
    return out


def price_markets(markets: list[dict], side_players: dict[str, dict], team_code: str,
                  w: float = MARKET_WEIGHT) -> list[dict]:
    """Attach model probability + edge to each Kalshi line for one team's players. `w` = Kalshi's
    log-odds weight in the blend (the fitted one from calib once there's enough evidence)."""
    by_name = {norm_name(p["name"]): p for p in side_players.values()}
    priced = []
    for m in markets:
        if m["team_code"] != team_code:
            continue
        p = by_name.get(norm_name(m["player"]))
        if not p:
            continue
        p_model = p_at_least(m["line"], p["proj"][m["stat"]], p["_var"][m["stat"]], m["stat"])
        p_mkt = (m["yes_bid"] + m["yes_ask"]) / 2 if m["yes_bid"] and m["yes_ask"] else None
        prob = expit(w * logit(p_mkt) + (1 - w) * logit(p_model)) if p_mkt else p_model
        row = {**m, "athlete_id": p["athlete_id"], "name": p["name"], "proj": p["proj"][m["stat"]],
               "recent": p["_recent"][m["stat"]],
               "avg": p["avg"][m["stat"]], "p_model": round(p_model, 4), "p_market": round(p_mkt, 4) if p_mkt else None,
               "p_yes": round(prob, 4), "miss_prob": p["miss_prob"]}
        for side, pr, ask, bid in (("yes", prob, m["yes_ask"], m["yes_bid"]), ("no", 1 - prob, m["no_ask"], m["no_bid"])):
            row[f"edge_{side}"] = round(pr - ask - kalshi_fee(ask), 4) if ask else None
        priced.append(row)
    return priced


def pick(priced: list[dict], n: int = PICKS_PER_TEAM, labels: dict | None = None) -> list[dict]:
    labels = labels or STAT_LABEL
    cands = []
    for r in priced:
        if r["miss_prob"] > 0:
            continue  # only players with no injury tag
        for side in ("yes", "no"):
            ask, bid = r[f"{side}_ask"], r[f"{side}_bid"]
            edge = r[f"edge_{side}"]
            if edge is None or ask is None or bid is None:
                continue
            if not (PICK_PRICE_RANGE[0] <= ask <= PICK_PRICE_RANGE[1]) or ask - bid > PICK_MAX_SPREAD:
                continue
            if edge < LEAN_MIN_EDGE:
                continue
            p_side = r["p_yes"] if side == "yes" else 1 - r["p_yes"]
            recent = r.get("recent") or []
            cands.append({
                "athlete_id": r["athlete_id"], "name": r["name"], "stat": r["stat"], "line": r["line"],
                "side": side,
                "label": (f'{r["name"]} {r["line"]}+ {labels[r["stat"]]}' if side == "yes"
                          else f'{r["name"]} under {r["line"]} {labels[r["stat"]]}'),
                "proj": r["proj"], "avg": r["avg"], "p": round(p_side, 4), "ask": ask, "edge": r[f"edge_{side}"],
                "p_model": r["p_model"] if side == "yes" else round(1 - r["p_model"], 4),
                "p_market": (r["p_market"] if side == "yes" else round(1 - r["p_market"], 4)) if r["p_market"] else None,
                "strength": "value" if edge >= PICK_MIN_EDGE else "lean",
                "last5_avg": round(sum(recent[:5]) / len(recent[:5]), 1) if recent else None,
                "last10": recent,
                # games out of his last 10 where this side would have won
                "hits": sum(1 for v in recent if (v >= r["line"]) == (side == "yes")),
                "games": len(recent),
                "ticker": r["ticker"],
            })
    cands.sort(key=lambda c: -c["edge"])
    chosen, used = [], set()
    for c in cands:
        if c["athlete_id"] in used:
            continue
        chosen.append(c)
        used.add(c["athlete_id"])
        if len(chosen) == n:
            break
    return chosen


def grade(pick_row: dict, box: dict) -> bool | None:
    """True/False once the game is final; None if the player didn't play (Kalshi settles at fair price)."""
    for rows in box.values():
        for r in rows:
            if r["athlete_id"] == pick_row["athlete_id"]:
                if r.get("dnp") or r.get("min", 0) <= 0:
                    return None
                hit = stat_of(r, pick_row["stat"]) >= pick_row["line"]
                return hit if pick_row["side"] == "yes" else not hit
    return None
