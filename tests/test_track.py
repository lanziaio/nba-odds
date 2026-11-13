import unittest

from nba_odds import main


def row(home_won: bool, prop_picks=None, best_value=None):
    return {
        "event_id": "1", "tip": "2026-11-14T00:00:00+00:00", "preview": False, "home": "IND", "away": "NY",
        "p_home_final": 0.3, "pick": "NY", "best_value": best_value, "prop_picks": prop_picks or [],
        "result": {"home_score": 100 if home_won else 90, "away_score": 90 if home_won else 100, "home_won": home_won},
    }


def prop(side="yes", line=25, ask=0.50, strength="value"):
    return {"athlete_id": "7", "name": "P", "stat": "pts", "line": line, "side": side, "label": "x",
            "p": 0.6, "ask": ask, "edge": 0.06, "strength": strength, "team": "NY", "won": None}


class GradeTests(unittest.TestCase):
    def test_contract_pnl_includes_fee(self):
        self.assertAlmostEqual(main._contract_pnl(True, 0.50), 0.48)   # 1 - 0.50 - 0.02 fee
        self.assertAlmostEqual(main._contract_pnl(False, 0.50), -0.52)
        self.assertEqual(main._contract_pnl(None, 0.50), 0.0)           # void

    def test_winner_and_game_bet(self):
        h = row(home_won=False, best_value={"side": "away", "team": "NY", "ask": 0.70, "edge": 0.04})
        main.grade_row(h, None)
        self.assertTrue(h["model_pick_correct"])
        self.assertEqual(h["best_value"]["status"], "won")
        self.assertAlmostEqual(h["best_value"]["pnl"], 1 - 0.70 - 0.02)
        self.assertTrue(h["graded"])

    def test_props_won_lost_void_and_pending(self):
        picks = [prop("yes", 25), prop("no", 25), prop("yes", 25)]
        picks[2]["athlete_id"] = "8"
        h = row(True, picks)
        main.grade_row(h, None)  # box score not available yet
        self.assertEqual([p["status"] for p in h["prop_picks"]], ["pending"] * 3)
        self.assertFalse(h["graded"])
        box = {"T": [{"athlete_id": "7", "min": 30, "pts": 27}, {"athlete_id": "8", "dnp": True, "min": 0}]}
        main.grade_row(h, box)
        self.assertEqual([p["status"] for p in h["prop_picks"]], ["won", "lost", "void"])
        self.assertEqual(h["prop_picks"][2]["pnl"], 0.0)
        self.assertTrue(h["graded"])

    def test_prop_record_uses_graded_pnl(self):
        picks = [prop(), prop()]
        h = row(True, picks)
        main.grade_row(h, {"T": [{"athlete_id": "7", "min": 30, "pts": 30}]})
        rec = main._prop_record(h["prop_picks"])
        self.assertEqual(rec["value"]["n"], 2)
        self.assertEqual(rec["value"]["hit_rate"], 1.0)
        self.assertAlmostEqual(rec["value"]["pnl_per_contract"], 0.96)


if __name__ == "__main__":
    unittest.main()
