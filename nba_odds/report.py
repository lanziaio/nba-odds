"""Build one pre-game report: gather inputs, run the model, flag what matters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import espn, kalshi, model, odds_api, players, props

ET = ZoneInfo("America/New_York")
BOX_GAMES = 12        # recent games used to find regular starters / rotation
FORM_DAYS = 7
VALUE_EDGE = 0.03     # flag a Kalshi side when expected profit >= 3 cents per $1 contract
DISAGREE = 0.08       # flag when stats model and market differ by 8+ points of probability


class RunContext:
    """Data shared by every game in one run (fetched once)."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.standings = espn.standings()
        self.injuries = espn.injuries()
        self.consensus, self.odds_api_remaining = [], None
        self.errors: list[str] = []
        try:
            self.consensus, self.odds_api_remaining = odds_api.fetch_consensus()
        except Exception as e:  # optional source: never block the report
            self.errors.append(f"The Odds API: {e}")
        self._schedules: dict[str, list[dict]] = {}
        self._league: dict | None = None

    @property
    def league(self) -> dict:
        if self._league is None:
            self._league = props.league_baseline(self.cache_dir)
        return self._league

    def schedule(self, team_id: str) -> list[dict]:
        if team_id not in self._schedules:
            self._schedules[team_id] = espn.team_schedule(team_id)
        return self._schedules[team_id]


def _form(team_id: str, schedule: list[dict], tip: datetime) -> dict:
    past = [g for g in schedule if g["completed"] and datetime.fromisoformat(g["tip"]) < tip]
    past.sort(key=lambda g: g["tip"], reverse=True)
    recent = [g for g in past if tip - datetime.fromisoformat(g["tip"]) <= timedelta(days=FORM_DAYS)]
    results, margins = [], []
    for g in recent:
        me, opp = (g["home"], g["away"]) if g["home"]["id"] == team_id else (g["away"], g["home"])
        if me["score"] is None or opp["score"] is None:
            continue
        m = me["score"] - opp["score"]
        margins.append(m)
        results.append({"date": g["tip"], "opp": opp["abbr"], "home": g["home"]["id"] == team_id,
                        "score": f'{me["score"]}-{opp["score"]}', "won": m > 0})
    last_tip = datetime.fromisoformat(past[0]["tip"]) if past else None
    b2b = bool(last_tip and (tip.astimezone(ET).date() - last_tip.astimezone(ET).date()).days == 1)
    rest_days = (tip.astimezone(ET).date() - last_tip.astimezone(ET).date()).days - 1 if last_tip else None
    return {
        "games": len(margins),
        "wins": sum(1 for r in results if r["won"]),
        "losses": sum(1 for r in results if not r["won"]),
        "avg_margin": round(sum(margins) / len(margins), 1) if margins else None,
        "results": results,
        "back_to_back": b2b,
        "rest_days": rest_days,
        "recent_event_ids": [g["event_id"] for g in past[:BOX_GAMES]],
    }


def _boxes(event_ids: list[str], cache_dir: Path) -> list[dict]:
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(lambda eid: espn.box_lines(eid, cache_dir), event_ids))


def _streak_len(streak: str | None) -> tuple[str | None, int]:
    if not streak or len(streak) < 2 or not streak[1:].isdigit():
        return None, 0
    return streak[0], int(streak[1:])


def build(game: dict, ctx: RunContext, preview: bool = False) -> dict:
    tip = datetime.fromisoformat(game["tip"])
    sides = {}
    for side in ("home", "away"):
        t = game[side]
        st = ctx.standings.get(t["id"], {"wins": 0, "losses": 0, "gp": 0, "diff_pg": 0.0})
        form = _form(t["id"], ctx.schedule(t["id"]), tip)
        boxes = _boxes(form.pop("recent_event_ids"), ctx.cache_dir)
        games = [b[t["id"]] for b in boxes if b.get(t["id"])]
        core = players.core_players(games)
        avail = players.availability(core, ctx.injuries.get(t["id"], []))
        sides[side] = {**t, "standing": st, "form": form, "availability": avail, "core_games_sampled": core["games_sampled"],
                       "_boxes": boxes, "_games": games, "_core": core}

    # --- market (Vegas) ---
    odds = game.get("odds") or {}
    cons = odds_api.match(ctx.consensus, game["home"]["nickname"], game["away"]["nickname"], tip)
    if cons:
        p_mkt, src = cons["p_home"], f'Consensus of {len(cons["books"])} US books (The Odds API)'
    else:
        p_mkt = model.market_prob_from_moneylines(odds.get("home_ml"), odds.get("away_ml"))
        src = f'{odds.get("provider", "ESPN")} moneyline via ESPN'
        if p_mkt is None:
            p_mkt = model.spread_to_prob(odds.get("home_spread"))
            src = f'{odds.get("provider", "ESPN")} spread via ESPN' if p_mkt is not None else None
    p_open = model.market_prob_from_moneylines(odds.get("home_ml_open"), odds.get("away_ml_open"))

    def inputs(s: dict) -> model.TeamInputs:
        return model.TeamInputs(
            diff_pg=s["standing"].get("diff_pg", 0.0), gp=s["standing"].get("gp", 0),
            last7_margin=s["form"]["avg_margin"], last7_games=s["form"]["games"],
            back_to_back=s["form"]["back_to_back"], injury_margin_loss=s["availability"]["injury_margin_loss"],
        )

    pred = model.predict(inputs(sides["home"]), inputs(sides["away"]), p_mkt, neutral=game.get("neutral", False))

    # --- Kalshi ---
    k = None
    try:
        k = kalshi.game_prices(game["away"]["abbr"], game["home"]["abbr"], tip)
    except Exception as e:
        ctx.errors.append(f"Kalshi {game['away']['abbr']}@{game['home']['abbr']}: {e}")
    edges = {}
    if k:
        edges = {
            "home": model.edge_vs_price(pred.p_home_final, k["home"]["ask"]),
            "away": model.edge_vs_price(1 - pred.p_home_final, k["away"]["ask"]),
        }

    p = pred.p_home_final
    pick_side = "home" if p >= 0.5 else "away"
    best = None
    for side, e in edges.items():
        if e is not None and e >= VALUE_EDGE and (best is None or e > best["edge"]):
            best = {"side": side, "team": sides[side]["abbr"], "edge": round(e, 4), "ask": k[side]["ask"]}

    flags = []
    for side in ("away", "home"):
        s = sides[side]
        a = s["availability"]
        if a["missing"]:
            flags.append(f'{s["abbr"]} without: {", ".join(a["missing"])}')
        if a["uncertain"]:
            flags.append(f'{s["abbr"]} questionable: {", ".join(a["uncertain"])}')
        if a["starters_total"] == 5 and a["all_core_available"]:
            flags.append(f'{s["abbr"]} full strength (all 5 regular starters + rotation available)')
        kind, n = _streak_len(s["standing"].get("streak"))
        if n >= 3:
            flags.append(f'{s["abbr"]} on a {n}-game {"winning" if kind == "W" else "losing"} streak')
        if s["form"]["back_to_back"]:
            flags.append(f'{s["abbr"]} on the 2nd night of a back-to-back')
    if p_mkt is not None and p_open is not None and abs(p_mkt - p_open) >= 0.05:
        mover = sides["home"]["abbr"] if p_mkt > p_open else sides["away"]["abbr"]
        flags.append(f"Line moved toward {mover}: P(home) {p_open:.0%} at open -> {p_mkt:.0%} now")
    if p_mkt is not None and abs(pred.p_home_stats - p_mkt) >= DISAGREE:
        flags.append(f"Stats model ({pred.p_home_stats:.0%}) and Vegas ({p_mkt:.0%}) disagree on P(home)")
    if k and k["illiquid"]:
        flags.append("Kalshi book is thin (wide bid/ask): treat its price with care")
    if not k:
        flags.append("No Kalshi market found for this game")

    prop_section = None
    try:
        prop_section = _props(game, sides, odds, tip, ctx)
    except Exception as e:
        ctx.errors.append(f"Props {game['away']['abbr']}@{game['home']['abbr']}: {e}")
    for side in ("home", "away"):
        for tmp_key in ("_boxes", "_games", "_core"):
            sides[side].pop(tmp_key, None)

    return {
        "event_id": game["event_id"],
        "props": prop_section,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preview": preview,
        "tip": game["tip"],
        "tip_et": tip.astimezone(ET).strftime("%a %b %-d, %-I:%M %p ET"),
        "home": sides["home"],
        "away": sides["away"],
        "market": {
            "source": src, "p_home": p_mkt, "p_home_open": p_open,
            "home_ml": odds.get("home_ml"), "away_ml": odds.get("away_ml"),
            "home_ml_open": odds.get("home_ml_open"), "away_ml_open": odds.get("away_ml_open"),
            "home_spread": odds.get("home_spread"), "total": odds.get("total"),
            "books": cons["books"] if cons else None,
            "range": [cons["p_home_min"], cons["p_home_max"]] if cons else None,
        },
        "kalshi": k,
        "model": {
            "p_home_stats": round(pred.p_home_stats, 4),
            "expected_home_margin": round(pred.expected_home_margin, 1),
            "components": pred.components,
            "p_home_final": round(p, 4),
            "market_weight": model.MARKET_WEIGHT,
        },
        "pick": {"side": pick_side, "team": sides[pick_side]["abbr"], "prob": round(max(p, 1 - p), 4)},
        "edges": {s: (round(e, 4) if e is not None else None) for s, e in edges.items()},
        "best_value": best,
        "flags": flags,
    }


def _props(game: dict, sides: dict, odds: dict, tip: datetime, ctx: RunContext) -> dict:
    spread, total = odds.get("home_spread"), odds.get("total")
    implied = {"home": None, "away": None}
    if spread is not None and total:
        implied = {"home": (total - spread) / 2, "away": (total + spread) / 2}
    markets: list[dict] = []
    try:
        markets = props.kalshi_props(game["away"]["abbr"], game["home"]["abbr"], tip)
    except Exception as e:
        ctx.errors.append(f"Kalshi props {game['away']['abbr']}@{game['home']['abbr']}: {e}")

    out = {"source": "Kalshi player props" if markets else None, "markets_found": len(markets),
           "picks": {}, "players": {}, "defense": {}, "team_target": {}}
    for side, other in (("home", "away"), ("away", "home")):
        s, o = sides[side], sides[other]
        inj = {r["athlete_id"]: players.status_weight(r)
               for r in ctx.injuries.get(s["id"], []) if r.get("athlete_id")}
        allowed = props.allowed_factors(o["id"], o["_boxes"], ctx.league)
        recent_ids = {row["athlete_id"] for g in s["_games"][:3] for row in g if not row.get("dnp")}
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = sorted(recent_ids)
            seasons = dict(zip(ids, pool.map(lambda a: _season_safe(a, ctx), ids)))
        proj = props.project_team(s["id"], s["_games"], inj, allowed, ctx.league, implied[side], total, seasons)
        priced = props.price_markets(markets, proj["players"], kalshi.code(s["abbr"]))
        out["picks"][side] = props.pick(priced)
        out["defense"][other] = allowed  # what `other` allows, used for `side`'s players
        out["team_target"][side] = proj["target"]
        core_ids = s["_core"]["starters"] + s["_core"]["rotation"]
        rows = []
        for pid in core_ids:
            p = proj["players"].get(pid)
            if not p:
                continue
            lines = sorted([m for m in priced if m["athlete_id"] == pid], key=lambda m: (m["stat"], m["line"]))
            rows.append({
                "athlete_id": pid, "name": p["name"], "role": "starter" if pid in s["_core"]["starters"] else "rotation",
                "minutes": p["minutes"], "avg": p["avg"], "proj": p["proj"], "miss_prob": p["miss_prob"],
                "hit_avg": props.hit_average(p),
                "lines": [{k: m[k] for k in ("stat", "line", "p_yes", "yes_ask", "no_ask", "edge_yes", "edge_no")} for m in lines],
            })
        out["players"][side] = rows
    return out


def _season_safe(athlete_id: str, ctx: RunContext) -> dict:
    try:
        return espn.athlete_seasons(athlete_id, ctx.cache_dir.parent / "athletes")
    except Exception:
        return {}
