"""Calibration and closing-line value, shared by the NBA and NFL jobs.

Two questions the pick record alone answers far too slowly (~4 picks a game):

1. Is the props model calibrated, and how much should it count next to Kalshi's own price?
   Every Kalshi line we price (~250 per NBA game, ~300 per NFL game) is saved at report time
   (data/lines/<sport>/<event>.json, not published) and graded after the game like a pick.
   From those, `summary` compares model vs market vs blend and fits the log-odds weight of the
   market that would have predicted best. Once there is enough evidence, that fitted weight
   replaces the default (`market_weight`).
   Caveat: lines of one player's ladder are strongly correlated, so the real sample is closer
   to "player-games" than to lines; the thresholds below are set with that in mind.

2. Are the picks beating the market? `capture_close` records each pick's Kalshi price a few
   minutes before kickoff. Closing-line value (closing mid on our side minus the ask we took)
   is the standard early signal of skill: it needs dozens of bets, not thousands.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .http import get_json
from .kalshi import BASE
from .model import expit, kalshi_fee, logit

LINE_KEYS = ("athlete_id", "name", "team", "stat", "line", "p_model", "p_market",
             "yes_bid", "yes_ask", "no_bid", "no_ask", "miss_prob", "ticker")
MIN_FIT_GAMES = 40        # games with graded lines before the fitted weight replaces the default
MIN_FIT_LINES = 4000
WEIGHT_RANGE = (0.50, 0.95)
CLOSE_BEFORE = timedelta(minutes=15)  # closing price = the last run in the 15 minutes before tip-off
EPS = 0.005


# ---------- saving and grading every priced line ----------

def compact_lines(priced: list[dict], team: str) -> list[dict]:
    return [{**{k: m.get(k) for k in LINE_KEYS}, "team": team} for m in priced]


def save_lines(lines_dir: Path, event_id: str, tip: str, lines: list[dict]) -> None:
    lines_dir.mkdir(parents=True, exist_ok=True)
    (lines_dir / f"{event_id}.json").write_text(
        json.dumps({"event_id": event_id, "tip": tip, "graded": False, "lines": lines}, separators=(",", ":")) + "\n")


def has_lines(lines_dir: Path, event_id: str) -> bool:
    return (lines_dir / f"{event_id}.json").exists()


def grade_lines(lines_dir: Path, event_id: str, box: dict, grade_fn: Callable[[dict, dict], bool | None]) -> int:
    """Stamp each saved line with y = 1 (YES won), 0, or None (void). Returns lines graded."""
    path = lines_dir / f"{event_id}.json"
    if not path.exists():
        return 0
    doc = json.loads(path.read_text())
    if doc.get("graded"):
        return 0
    for ln in doc["lines"]:
        won = grade_fn({**ln, "side": "yes"}, box)
        ln["y"] = None if won is None else int(won)
    doc["graded"] = True
    path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    return len(doc["lines"])


def grade_settled(history: list[dict], lines_dir: Path, box_fn: Callable[[str], dict],
                  grade_row_fn: Callable[[dict, dict | None], None],
                  grade_fn: Callable[[dict, dict], bool | None]) -> int:
    """Grade the picks (grade_row_fn) and every saved line of each settled game, fetching the box
    score once. Returns how many lines were graded (so the caller knows to refresh the summary)."""
    graded = 0
    for h in history:
        if h["result"] is None:
            continue
        lines_due = not h.get("lines_graded") and has_lines(lines_dir, h["event_id"])
        if h.get("graded") and not lines_due:
            h["lines_graded"] = True
            continue
        box = None
        if h.get("prop_picks") or lines_due:
            try:
                box = box_fn(h["event_id"])
            except Exception as e:
                print(f"  warn: box score for {h['event_id']} not available yet: {e}", file=sys.stderr)
        if not h.get("graded"):
            grade_row_fn(h, box)
        if box is not None and lines_due:
            graded += grade_lines(lines_dir, h["event_id"], box, grade_fn)
            h["lines_graded"] = True
    return graded


def load_graded(lines_dir: Path) -> list[dict]:
    out = []
    for p in sorted(lines_dir.glob("*.json")) if lines_dir.exists() else []:
        doc = json.loads(p.read_text())
        if doc.get("graded"):
            out += [{**ln, "event_id": doc["event_id"]} for ln in doc["lines"]
                    if ln.get("y") is not None and ln.get("p_model") is not None]
    return out


# ---------- scoring ----------

def blend(p_model: float, p_market: float | None, w: float) -> float:
    return p_model if p_market is None else expit(w * logit(p_market) + (1 - w) * logit(p_model))


def _clip(p: float) -> float:
    return min(1 - EPS, max(EPS, p))


def _scores(pairs: list[tuple[float, int]]) -> dict:
    if not pairs:
        return {"brier": None, "log_loss": None}
    n = len(pairs)
    return {"brier": round(sum((p - y) ** 2 for p, y in pairs) / n, 4),
            "log_loss": round(-sum(math.log(_clip(p)) if y else math.log(1 - _clip(p)) for p, y in pairs) / n, 4)}


def fit_weight(lines: list[dict]) -> float | None:
    """Market log-odds weight (0..1, step 0.05) with the lowest log loss over graded lines."""
    rows = [(logit(_clip(ln["p_model"])), logit(_clip(ln["p_market"])), ln["y"]) for ln in lines if ln.get("p_market")]
    if not rows:
        return None
    best = None
    for i in range(21):
        w = i / 20
        ll = 0.0
        for lm, lk, y in rows:
            p = _clip(expit(w * lk + (1 - w) * lm))
            ll -= math.log(p) if y else math.log(1 - p)
        if best is None or ll < best[1]:
            best = (w, ll)
    return best[0]


def _buckets(pairs: list[tuple[float, int]], edges=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        b = [(p, y) for p, y in pairs if lo <= p < hi]
        if b:
            out.append({"from": lo, "to": min(hi, 1.0), "n": len(b),
                        "predicted": round(sum(p for p, _ in b) / len(b), 3),
                        "actual": round(sum(y for _, y in b) / len(b), 3)})
    return out


def paper_bets(lines: list[dict], w: float, min_edge: float, price_range=(0.20, 0.85), max_spread=0.06) -> dict:
    """Every side of every graded line the current pick rule would have bet (not only the 1-2 shown)."""
    n = wins = 0
    pnl = stake = 0.0
    for ln in lines:
        if ln.get("miss_prob"):
            continue
        p_yes = blend(ln["p_model"], ln.get("p_market"), w)
        for side, p, ask, bid, won in (("yes", p_yes, ln.get("yes_ask"), ln.get("yes_bid"), ln["y"] == 1),
                                       ("no", 1 - p_yes, ln.get("no_ask"), ln.get("no_bid"), ln["y"] == 0)):
            if not ask or not bid or not (price_range[0] <= ask <= price_range[1]) or ask - bid > max_spread:
                continue
            if p - ask - kalshi_fee(ask) < min_edge:
                continue
            fee = kalshi_fee(ask)
            n += 1
            wins += won
            stake += ask
            pnl += (1 - ask - fee) if won else (-ask - fee)
    return {"n": n, "hit_rate": round(wins / n, 4) if n else None,
            "pnl_per_contract": round(pnl, 2), "roi": round(pnl / stake, 4) if stake else None}


def summary(lines_dir: Path, default_w: float, min_edge: float) -> dict:
    lines = load_graded(lines_dir)
    games = len({ln["event_id"] for ln in lines})
    fitted = fit_weight(lines)
    enough = games >= MIN_FIT_GAMES and len(lines) >= MIN_FIT_LINES and fitted is not None
    w_used = min(WEIGHT_RANGE[1], max(WEIGHT_RANGE[0], fitted)) if enough else default_w
    with_mkt = [ln for ln in lines if ln.get("p_market")]
    model = [(ln["p_model"], ln["y"]) for ln in with_mkt]
    market = [(ln["p_market"], ln["y"]) for ln in with_mkt]
    blended = [(blend(ln["p_model"], ln["p_market"], w_used), ln["y"]) for ln in with_mkt]
    return {
        "lines": len(lines), "games": games,
        "model": _scores(model), "market": _scores(market), "blend": _scores(blended),
        "fitted_market_weight": fitted, "market_weight_in_use": w_used, "default_market_weight": default_w,
        "weight_source": "fitted" if enough else "default",
        "fit_needs": {"games": MIN_FIT_GAMES, "lines": MIN_FIT_LINES},
        "buckets": {"model": _buckets(model), "market": _buckets(market), "blend": _buckets(blended)},
        "paper_bets": {"value": paper_bets(lines, w_used, min_edge), "lean_or_better": paper_bets(lines, w_used, 0.02)},
    }


def write_summary(lines_dir: Path, out_path: Path, default_w: float, min_edge: float) -> dict:
    s = summary(lines_dir, default_w, min_edge)
    out_path.write_text(json.dumps(s, indent=1) + "\n")
    return s


def market_weight(summary_path: Path, default_w: float) -> float:
    """Weight the props blend should use now: the fitted one once there's enough evidence."""
    try:
        s = json.loads(summary_path.read_text())
        return float(s["market_weight_in_use"]) if s.get("weight_source") == "fitted" else default_w
    except (OSError, ValueError, KeyError, TypeError):
        return default_w


# ---------- closing-line value ----------

def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0 < x < 1 else None


def market_mid(ticker: str) -> float | None:
    """Current mid of a Kalshi market's YES side, or None if it isn't quoted on both sides."""
    m = (get_json(f"{BASE}/markets/{ticker}", {}) or {}).get("market") or {}
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    return round((bid + ask) / 2, 4) if bid and ask else None


def capture_close(history: list[dict], reports_dir: Path, now: datetime,
                  mid_fn: Callable[[str], float | None] = market_mid) -> int:
    """For games starting within CLOSE_BEFORE, record the closing Kalshi mid of every pick and the
    game value bet. CLV = closing mid on our side - the ask we took (positive = we beat the close)."""
    n = 0
    for h in history:
        if h.get("preview") or h.get("close_at") or h.get("result"):
            continue
        tip = datetime.fromisoformat(h["tip"])
        if not (tip - CLOSE_BEFORE <= now <= tip):
            continue
        try:
            r = json.loads((reports_dir / f'{h["event_id"]}.json').read_text())
        except (OSError, ValueError):
            continue
        tickers = {(pk["athlete_id"], pk["stat"], pk["line"], pk["side"]): pk.get("ticker")
                   for side in ("away", "home") for pk in ((r.get("props") or {}).get("picks") or {}).get(side, [])}
        targets = []
        for pk in h.get("prop_picks", []):
            t = pk.get("ticker") or tickers.get((pk["athlete_id"], pk["stat"], pk["line"], pk["side"]))
            if t:
                targets.append((pk, t, pk["side"] == "yes"))
        bv = h.get("best_value")
        if bv:
            t = ((r.get("kalshi") or {}).get(bv["side"]) or {}).get("ticker")
            if t:
                targets.append((bv, t, True))  # the game market's YES is "this team wins"
        for obj, ticker, yes_side in targets:
            try:
                mid = mid_fn(ticker)
            except Exception:
                mid = None
            if mid is None:
                continue
            close = mid if yes_side else round(1 - mid, 4)
            obj["close"] = close
            obj["clv"] = round(close - obj["ask"], 4)
            n += 1
        h["close_at"] = now.isoformat(timespec="seconds")
    return n


def clv_record(rows: list[dict]) -> dict:
    """Closing-line value of every pick and value bet that has a closing price."""
    bets = [pk for h in rows for pk in h.get("prop_picks", []) if pk.get("clv") is not None]
    bets += [h["best_value"] for h in rows if (h.get("best_value") or {}).get("clv") is not None]
    if not bets:
        return {"n": 0, "avg_clv": None, "beat_close": None}
    return {"n": len(bets), "avg_clv": round(sum(b["clv"] for b in bets) / len(bets), 4),
            "beat_close": round(sum(1 for b in bets if b["clv"] > 0) / len(bets), 4)}
