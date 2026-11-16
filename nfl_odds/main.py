"""NFL CLI entry point (same flow as the NBA job, data in docs/data/nfl).

  python -m nfl_odds run              # analyse games kicking off within the window (default 75 min)
  python -m nfl_odds run --all-week   # preview every remaining game this week (not counted in the record)
  python -m nfl_odds settle           # fill in final scores and grade past picks
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nba_odds import calib
from nba_odds import props as nba_props
from nba_odds.main import _brier, _contract_pnl, _history_row, _load, _prop_record, _save, _value_bets

from . import espn, props, report

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data" / "nfl"          # published with the dashboard
CACHE = ROOT / "data" / "cache" / "nfl_box"     # not published
LINES = ROOT / "data" / "lines" / "nfl"          # every priced Kalshi line, graded after the game (not published)
CALIB = DATA / "calibration.json"
ET = ZoneInfo("America/New_York")


def grade_row(h: dict, box: dict | None) -> None:
    """Stamp every pick in a settled row with its outcome and P&L (idempotent). Ties lose every side."""
    res = h["result"]
    tie = res["home_score"] == res["away_score"]
    winner = None if tie else (h["home"] if res["home_won"] else h["away"])
    h["model_pick_correct"] = h.get("pick") == winner
    bv = h.get("best_value")
    if bv:
        won = False if tie else (res["home_won"] if bv["side"] == "home" else not res["home_won"])
        bv["status"] = "won" if won else "lost"
        bv["pnl"] = _contract_pnl(won, bv["ask"])
    for pk in h.get("prop_picks", []):
        if box is None:
            pk["status"] = "pending"
            continue
        won = props.grade(pk, box)
        pk["won"] = won
        pk["status"] = "void" if won is None else ("won" if won else "lost")
        pk["pnl"] = _contract_pnl(won, pk["ask"])
    h["graded"] = box is not None or not h.get("prop_picks")


def settle(history: list[dict], now: datetime) -> int:
    pending = [h for h in history if h["result"] is None and datetime.fromisoformat(h["tip"]) < now - timedelta(hours=4)]
    dates = sorted({datetime.fromisoformat(h["tip"]).astimezone(ET).strftime("%Y%m%d") for h in pending})
    finals = {}
    for d in dates:
        games, _ = espn.scoreboard(d)
        finals.update({g["event_id"]: g for g in games if g["completed"]})
    n = 0
    for h in pending:
        g = finals.get(h["event_id"])
        if g and g["home"]["score"] is not None:
            h["result"] = {"home_score": g["home"]["score"], "away_score": g["away"]["score"],
                           "home_won": g["home"]["score"] > g["away"]["score"]}
            n += 1
    graded = calib.grade_settled(history, LINES, lambda eid: espn.box_lines(eid, CACHE), grade_row, props.grade)
    if graded or (LINES.exists() and not CALIB.exists()):
        calib.write_summary(LINES, CALIB, props.MARKET_WEIGHT, nba_props.PICK_MIN_EDGE)
    return n


def write_index(games: list[dict], history: list[dict], now: datetime, meta: dict) -> None:
    by_id = {h["event_id"]: h for h in history}
    prev_odds = {s["event_id"]: s.get("odds") for s in _load(DATA / "index.json", {}).get("slate", [])}
    slate = []
    for g in sorted(games, key=lambda g: g["tip"]):
        odds = g.get("odds") if g["state"] == "pre" else (prev_odds.get(g["event_id"]) or g.get("odds"))
        slate.append({
            "odds": odds,
            "event_id": g["event_id"], "tip": g["tip"], "state": g["state"], "status": g["status"],
            "home": {**{k: g["home"][k] for k in ("abbr", "name", "logo")}, "score": g["home"]["score"] if g["state"] == "post" else None},
            "away": {**{k: g["away"][k] for k in ("abbr", "name", "logo")}, "score": g["away"]["score"] if g["state"] == "post" else None},
            "has_report": (DATA / "reports" / f'{g["event_id"]}.json').exists(),
            "report_at": (datetime.fromisoformat(g["tip"]) - timedelta(minutes=meta["window_min"])).isoformat(),
            "summary": by_id.get(g["event_id"]),
        })
    settled = [h for h in history if h["result"] and not h["preview"]]
    body = {
        "sport": "nfl",
        "slate_label": f'Week {meta["week"]["number"]}, {meta["week"]["season"]}' if meta.get("week", {}).get("number") else meta.get("date"),
        "slate": slate,
        "track_record": {
            "games": len(settled),
            "model": _brier(settled, "p_home_final"),
            "vegas": _brier(settled, "p_home_market"),
            "stats_only": _brier(settled, "p_home_stats"),
            "kalshi": _brier(settled, "p_home_kalshi"),
            "value_bets": _value_bets([h for h in settled if h.get("best_value")]),
            "props": _prop_record([pk for h in settled for pk in h.get("prop_picks", [])]),
            "clv": calib.clv_record([h for h in history if not h["preview"]]),
            "calibration": _load(CALIB, None),
        },
        "notes": meta.get("notes", []),
    }
    prev = _load(DATA / "index.json", {})
    prev.pop("generated_at", None)
    if prev != json.loads(json.dumps(body)):
        _save(DATA / "index.json", {"generated_at": now.isoformat(timespec="seconds"), **body})


def cmd_run(args) -> int:
    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now).astimezone(timezone.utc)
    games, week = espn.scoreboard(args.date)  # default: the current NFL week
    state = _load(DATA / "state.json", {"reported": {}})
    history: list[dict] = _load(DATA / "history.json", [])
    window = timedelta(minutes=args.window)

    def is_due(g: dict) -> bool:
        if g["state"] != "pre":
            return False
        until = datetime.fromisoformat(g["tip"]) - now
        if args.all_week:
            return until > timedelta(0)
        return timedelta(0) < until <= window and g["event_id"] not in state["reported"]

    due = [g for g in games if is_due(g) or (args.game and g["event_id"] == args.game)]
    print(f"[{now:%Y-%m-%d %H:%M}Z] NFL week {week.get('number')}: {len(games)} games; {len(due)} due")

    failures = 0
    notes: list[str] = []
    if due:
        ctx = report.RunContext(CACHE)
        ctx.market_weight = calib.market_weight(CALIB, props.MARKET_WEIGHT)
        preview = args.all_week
        for g in due:
            label = f'{g["away"]["abbr"]}@{g["home"]["abbr"]}'
            try:
                r = report.build(g, ctx, preview=preview)
            except Exception:
                failures += 1
                print(f"  ! {label}: report failed\n{traceback.format_exc()}", file=sys.stderr)
                continue
            all_lines = (r.get("props") or {}).pop("all_lines", None)
            existing = _load(DATA / "reports" / f'{g["event_id"]}.json', None)
            if preview and existing and not existing.get("preview"):
                print(f"  {label}: final report already out, preview not saved")
                continue
            _save(DATA / "reports" / f'{g["event_id"]}.json', r)
            if all_lines and not preview:
                calib.save_lines(LINES, g["event_id"], g["tip"], all_lines)
            hist = {h["event_id"]: h for h in history}
            old = hist.get(g["event_id"])
            if old is None or old["preview"] or not preview:
                hist[g["event_id"]] = _history_row(r)
            history = sorted(hist.values(), key=lambda h: h["tip"])
            p = r["model"]["p_home_final"]
            n_picks = sum(len(v) for v in ((r.get("props") or {}).get("picks") or {}).values())
            print(f'  {label}: {r["home"]["abbr"]} {p:.1%} (stats {r["model"]["p_home_stats"]:.1%}, '
                  f'vegas {r["market"]["p_home"] or 0:.1%}, kalshi {(r["kalshi"] or {}).get("p_home") or 0:.1%})'
                  f' · {n_picks} prop picks{"  VALUE " + r["best_value"]["team"] if r["best_value"] else ""}')
            if not preview:
                state["reported"][g["event_id"]] = now.isoformat(timespec="seconds")
        notes = ctx.errors
        for e in ctx.errors:
            print(f"  warn: {e}", file=sys.stderr)

    try:
        c = calib.capture_close(history, DATA / "reports", now)
        if c:
            print(f"  closing prices recorded for {c} picks")
    except Exception as e:
        print(f"  warn: closing prices failed: {e}", file=sys.stderr)

    try:
        n = settle(history, now)
        if n:
            print(f"  settled {n} past games")
    except Exception as e:
        print(f"  warn: settle failed: {e}", file=sys.stderr)

    cutoff = (now - timedelta(days=21)).isoformat()
    state["reported"] = {k: v for k, v in state["reported"].items() if v >= cutoff}
    _save(DATA / "state.json", state)
    _save(DATA / "history.json", history)
    write_index(games, history, now, {"date": args.date, "week": week, "window_min": args.window, "notes": notes})
    return 1 if failures else 0


def cmd_settle(args) -> int:
    history = _load(DATA / "history.json", [])
    n = settle(history, datetime.now(timezone.utc))
    _save(DATA / "history.json", history)
    print(f"settled {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nfl_odds")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--window", type=int, default=75, help="minutes before kickoff to analyse a game")
    r.add_argument("--all-week", action="store_true", help="preview every remaining game this week")
    r.add_argument("--date", help="ET date YYYYMMDD (default: the current NFL week)")
    r.add_argument("--game", help="force one ESPN event id")
    r.add_argument("--now", help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("settle")
    s.set_defaults(fn=cmd_settle)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
