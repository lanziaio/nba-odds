"""One-off: rebuild data/lines/<sport>/ from reports written before every priced line was saved
(2026-11-13 to 2026-11-15), then grade them from the cached box scores.

Reports kept each shown player's lines as p_yes (the blend), yes_ask and no_ask. On Kalshi
yes_bid = 1 - no_ask, so the market mid is recoverable, and the model's own probability comes
from inverting the blend with the weight in force then (NBA 0.60, NFL 0.70).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nba_odds import calib, props as nba_props  # noqa: E402
from nba_odds.model import expit, logit  # noqa: E402
from nfl_odds import props as nfl_props  # noqa: E402

SPORTS = {
    "nba": (ROOT / "docs/data/reports", ROOT / "data/cache/box", 0.60, nba_props.grade, ROOT / "docs/data/history.json"),
    "nfl": (ROOT / "docs/data/nfl/reports", ROOT / "data/cache/nfl_box", 0.70, nfl_props.grade, ROOT / "docs/data/nfl/history.json"),
}


def main() -> None:
    for sport, (reports, boxes, w, grade_fn, hist_path) in SPORTS.items():
        out = ROOT / "data/lines" / sport
        settled = {h["event_id"] for h in json.loads(hist_path.read_text()) if h.get("result")}
        n_games = n_lines = 0
        for rp in sorted(reports.glob("*.json")):
            r = json.loads(rp.read_text())
            if r.get("preview") or calib.has_lines(out, r["event_id"]):
                continue
            lines = []
            for side in ("away", "home"):
                for p in ((r.get("props") or {}).get("players") or {}).get(side, []):
                    for ln in p.get("lines", []):
                        ya, na = ln.get("yes_ask"), ln.get("no_ask")
                        yb = round(1 - na, 4) if na else None
                        nb = round(1 - ya, 4) if ya else None
                        mkt = (yb + ya) / 2 if ya and yb else None
                        py = min(0.9999, max(0.0001, ln["p_yes"]))
                        pm = expit((logit(py) - w * logit(mkt)) / (1 - w)) if mkt else py
                        lines.append({"athlete_id": p["athlete_id"], "name": p["name"], "team": r[side]["abbr"],
                                      "stat": ln["stat"], "line": ln["line"], "p_model": round(pm, 4),
                                      "p_market": round(mkt, 4) if mkt else None, "yes_bid": yb, "yes_ask": ya,
                                      "no_bid": nb, "no_ask": na, "miss_prob": p.get("miss_prob"),
                                      "ticker": None, "backfilled": True})
            calib.save_lines(out, r["event_id"], r["tip"], lines)
            box_file = boxes / f'{r["event_id"]}.json'
            if r["event_id"] in settled and box_file.exists():
                calib.grade_lines(out, r["event_id"], json.loads(box_file.read_text()), grade_fn)
            n_games += 1
            n_lines += len(lines)
        print(f"{sport}: {n_games} games, {n_lines} lines")


if __name__ == "__main__":
    main()
