"""CLI entry point.

  python -m nba_odds run            # analyse games tipping off within the window (default 75 min), post to Slack
  python -m nba_odds run --all-today --no-slack   # preview every remaining game today (nothing is posted)
  python -m nba_odds settle         # fill in final scores for past predictions
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import espn, report, slack

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"           # published with the dashboard
CACHE = ROOT / "data" / "cache" / "box"  # not published
ET = ZoneInfo("America/New_York")


def _load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=False) + "\n")


def _history_row(r: dict) -> dict:
    k = r.get("kalshi") or {}
    return {
        "event_id": r["event_id"],
        "tip": r["tip"],
        "preview": r["preview"],
        "home": r["home"]["abbr"],
        "away": r["away"]["abbr"],
        "p_home_final": r["model"]["p_home_final"],
        "p_home_stats": r["model"]["p_home_stats"],
        "p_home_market": r["market"]["p_home"],
        "p_home_kalshi": k.get("p_home"),
        "pick": r["pick"]["team"],
        "best_value": r.get("best_value"),
        "result": None,
    }


def _brier(rows: list[dict], key: str) -> dict:
    pts = [(r[key], r["result"]["home_won"]) for r in rows if r.get(key) is not None]
    if not pts:
        return {"n": 0, "brier": None, "accuracy": None}
    brier = sum((p - (1 if won else 0)) ** 2 for p, won in pts) / len(pts)
    acc = sum(1 for p, won in pts if (p >= 0.5) == won) / len(pts)
    return {"n": len(pts), "brier": round(brier, 4), "accuracy": round(acc, 4)}


def _value_bets(rows: list[dict]) -> dict:
    bets = [r for r in rows if r.get("best_value")]
    pnl = 0.0
    for r in bets:
        bv = r["best_value"]
        won = r["result"]["home_won"] if bv["side"] == "home" else not r["result"]["home_won"]
        pnl += (1 - bv["ask"]) if won else -bv["ask"]
    return {"n": len(bets), "pnl_per_contract": round(pnl, 2)}


def settle(history: list[dict], now: datetime) -> int:
    pending = [h for h in history if h["result"] is None and datetime.fromisoformat(h["tip"]) < now - timedelta(hours=3)]
    dates = sorted({datetime.fromisoformat(h["tip"]).astimezone(ET).strftime("%Y%m%d") for h in pending})
    finals = {}
    for d in dates:
        for g in espn.scoreboard(d):
            if g["completed"]:
                finals[g["event_id"]] = g
    n = 0
    for h in pending:
        g = finals.get(h["event_id"])
        if g and g["home"]["score"] is not None:
            h["result"] = {"home_score": g["home"]["score"], "away_score": g["away"]["score"],
                           "home_won": g["home"]["score"] > g["away"]["score"]}
            n += 1
    return n


def write_index(games: list[dict], history: list[dict], now: datetime, meta: dict) -> None:
    by_id = {h["event_id"]: h for h in history}
    slate = []
    for g in sorted(games, key=lambda g: g["tip"]):
        h = by_id.get(g["event_id"])
        slate.append({
            "event_id": g["event_id"], "tip": g["tip"], "state": g["state"], "status": g["status"],
            "home": {**{k: g["home"][k] for k in ("abbr", "name", "logo")}, "score": g["home"]["score"] if g["state"] == "post" else None},
            "away": {**{k: g["away"][k] for k in ("abbr", "name", "logo")}, "score": g["away"]["score"] if g["state"] == "post" else None},
            "has_report": (DATA / "reports" / f'{g["event_id"]}.json').exists(),
            "report_at": (datetime.fromisoformat(g["tip"]) - timedelta(minutes=meta["window_min"])).isoformat(),
            "summary": h,
        })
    settled = [h for h in history if h["result"] and not h["preview"]]
    body = {
        "slate_date_et": meta["date"],
        "slate": slate,
        "track_record": {
            "games": len(settled),
            "model": _brier(settled, "p_home_final"),
            "vegas": _brier(settled, "p_home_market"),
            "stats_only": _brier(settled, "p_home_stats"),
            "kalshi": _brier(settled, "p_home_kalshi"),
            "value_bets": _value_bets([h for h in settled if h.get("best_value")]),
        },
        "notes": meta.get("notes", []),
    }
    # Only rewrite when something besides the timestamp changed (keeps the git history quiet).
    prev = _load(DATA / "index.json", {})
    prev.pop("generated_at", None)
    if prev != json.loads(json.dumps(body)):
        _save(DATA / "index.json", {"generated_at": now.isoformat(timespec="seconds"), **body})


def cmd_run(args) -> int:
    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now).astimezone(timezone.utc)
    date = args.date or now.astimezone(ET).strftime("%Y%m%d")
    games = espn.scoreboard(date)

    state = _load(DATA / "state.json", {"reported": {}})
    history: list[dict] = _load(DATA / "history.json", [])
    window = timedelta(minutes=args.window)

    def is_due(g: dict) -> bool:
        if g["state"] != "pre":
            return False
        until = datetime.fromisoformat(g["tip"]) - now
        if args.all_today:
            return until > timedelta(0)
        return timedelta(0) < until <= window and g["event_id"] not in state["reported"]

    due = [g for g in games if is_due(g) or (args.game and g["event_id"] == args.game)]
    print(f"[{now:%Y-%m-%d %H:%M}Z] {len(games)} games on {date}; {len(due)} due")

    failures = 0
    notes: list[str] = []
    if due:
        ctx = report.RunContext(CACHE)
        if ctx.odds_api_remaining is not None:
            print(f"  The Odds API credits remaining: {ctx.odds_api_remaining}")
        preview = args.all_today
        for g in due:
            label = f'{g["away"]["abbr"]}@{g["home"]["abbr"]}'
            try:
                r = report.build(g, ctx, preview=preview)
            except Exception:
                failures += 1
                print(f"  ! {label}: report failed\n{traceback.format_exc()}", file=sys.stderr)
                continue
            _save(DATA / "reports" / f'{g["event_id"]}.json', r)
            hist = {h["event_id"]: h for h in history}
            old = hist.get(g["event_id"])
            if old is None or old["preview"] or not preview:
                hist[g["event_id"]] = _history_row(r)
            history = sorted(hist.values(), key=lambda h: h["tip"])
            p = r["model"]["p_home_final"]
            print(f'  {label}: {r["home"]["abbr"]} {p:.1%} (stats {r["model"]["p_home_stats"]:.1%}, '
                  f'vegas {r["market"]["p_home"] or 0:.1%}, kalshi {(r["kalshi"] or {}).get("p_home") or 0:.1%})'
                  f'{"  VALUE " + r["best_value"]["team"] if r["best_value"] else ""}')
            if preview or args.no_slack:
                continue
            try:
                posted = slack.post(r)
                print(f"    slack: {'posted' if posted else 'skipped (no SLACK_WEBHOOK_URL)'}")
            except Exception as e:
                failures += 1
                print(f"    ! slack failed: {e}", file=sys.stderr)
                continue  # not marked reported -> retried next run while still before tip
            state["reported"][g["event_id"]] = now.isoformat(timespec="seconds")
        notes = ctx.errors
        for e in ctx.errors:
            print(f"  warn: {e}", file=sys.stderr)

    try:
        n = settle(history, now)
        if n:
            print(f"  settled {n} past games")
    except Exception as e:
        print(f"  warn: settle failed: {e}", file=sys.stderr)

    cutoff = (now - timedelta(days=14)).isoformat()
    state["reported"] = {k: v for k, v in state["reported"].items() if v >= cutoff}
    _save(DATA / "state.json", state)
    _save(DATA / "history.json", history)
    write_index(games, history, now, {"date": date, "window_min": args.window, "notes": notes})
    return 1 if failures else 0


def cmd_settle(args) -> int:
    now = datetime.now(timezone.utc)
    history = _load(DATA / "history.json", [])
    n = settle(history, now)
    _save(DATA / "history.json", history)
    print(f"settled {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nba_odds")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--window", type=int, default=75, help="minutes before tip-off to analyse a game")
    r.add_argument("--all-today", action="store_true", help="preview every remaining game today (no Slack)")
    r.add_argument("--no-slack", action="store_true")
    r.add_argument("--date", help="ET date YYYYMMDD (default: today)")
    r.add_argument("--game", help="force one ESPN event id")
    r.add_argument("--now", help=argparse.SUPPRESS)  # testing: pretend current time (ISO)
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("settle")
    s.set_defaults(fn=cmd_settle)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
