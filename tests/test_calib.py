import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nba_odds import calib
from nba_odds.model import expit, logit


def line(p_model, p_market, y, **kw):
    return {"athlete_id": "1", "name": "A", "team": "T", "stat": "pts", "line": 20, "p_model": p_model,
            "p_market": p_market, "yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None,
            "miss_prob": 0.0, "ticker": None, "y": y, **kw}


class CalibTests(unittest.TestCase):
    def test_fit_weight_prefers_the_better_source(self):
        rng = random.Random(7)
        truth_follows_market, truth_follows_model = [], []
        for _ in range(3000):
            a, b = rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9)
            truth_follows_market.append(line(a, b, int(rng.random() < b)))
            truth_follows_model.append(line(a, b, int(rng.random() < a)))
        self.assertGreaterEqual(calib.fit_weight(truth_follows_market), 0.9)
        self.assertLessEqual(calib.fit_weight(truth_follows_model), 0.1)

    def test_save_grade_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lines = [line(0.6, 0.5, None, athlete_id="7", line=20), line(0.3, 0.4, None, athlete_id="8", line=10)]
            calib.save_lines(d, "e1", "2026-11-15T00:00:00+00:00", lines)
            box = {"T": [{"athlete_id": "7", "pts": 25}, {"athlete_id": "8", "pts": 4}]}
            grade_fn = lambda pk, b: next((r["pts"] >= pk["line"] for rows in b.values() for r in rows
                                           if r["athlete_id"] == pk["athlete_id"]), None)
            self.assertEqual(calib.grade_lines(d, "e1", box, grade_fn), 2)
            self.assertEqual(calib.grade_lines(d, "e1", box, grade_fn), 0)  # idempotent
            got = {ln["athlete_id"]: ln["y"] for ln in calib.load_graded(d)}
            self.assertEqual(got, {"7": 1, "8": 0})
            s = calib.summary(d, 0.6, 0.05)
            self.assertEqual((s["lines"], s["games"], s["weight_source"]), (2, 1, "default"))
            self.assertEqual(calib.market_weight(Path(d) / "missing.json", 0.6), 0.6)

    def test_fitted_weight_used_only_with_enough_games(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"weight_source": "default", "market_weight_in_use": 0.6}))
            self.assertEqual(calib.market_weight(p, 0.85), 0.85)
            p.write_text(json.dumps({"weight_source": "fitted", "market_weight_in_use": 0.9}))
            self.assertEqual(calib.market_weight(p, 0.85), 0.9)

    def test_paper_bets_respects_edge_and_price_rules(self):
        ok = line(0.80, 0.60, 1, yes_bid=0.58, yes_ask=0.60, no_bid=0.40, no_ask=0.42)
        wide = {**ok, "yes_bid": 0.50}            # 10c spread: skipped
        injured = {**ok, "miss_prob": 0.25}       # injury tag: skipped
        r = calib.paper_bets([ok, wide, injured], w=0.5, min_edge=0.02)
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["hit_rate"], 1.0)
        self.assertEqual(calib.paper_bets([ok], w=1.0, min_edge=0.02)["n"], 0)  # market-only: no edge

    def test_capture_close_and_clv(self):
        tip = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "g1.json").write_text(json.dumps({"props": {"picks": {"away": [], "home": [
                {"athlete_id": "7", "stat": "rec", "line": 4, "side": "no", "ticker": "PROP-T"}]}},
                "kalshi": {"home": {"ticker": "GAME-H"}, "away": {"ticker": "GAME-A"}}}))
            h = {"event_id": "g1", "tip": tip.isoformat(), "preview": False, "result": None,
                 "best_value": {"side": "home", "ask": 0.40},
                 "prop_picks": [{"athlete_id": "7", "stat": "rec", "line": 4, "side": "no", "ask": 0.55}]}
            mids = {"PROP-T": 0.40, "GAME-H": 0.43}
            self.assertEqual(calib.capture_close([h], d, tip - timedelta(hours=1), mids.get), 0)  # too early
            self.assertEqual(calib.capture_close([h], d, tip - timedelta(minutes=4), mids.get), 2)
            self.assertAlmostEqual(h["prop_picks"][0]["clv"], 0.05)   # NO side closes at 0.60, we paid 0.55
            self.assertAlmostEqual(h["best_value"]["clv"], 0.03)
            self.assertEqual(calib.capture_close([h], d, tip - timedelta(minutes=2), mids.get), 0)  # once
            rec = calib.clv_record([h])
            self.assertEqual((rec["n"], rec["beat_close"]), (2, 1.0))


if __name__ == "__main__":
    unittest.main()
