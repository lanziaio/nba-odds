import math
import unittest

from nfl_odds import kalshi, main, model, players, props


class NflModelTests(unittest.TestCase):
    def test_kalshi_codes(self):
        self.assertEqual(kalshi.code("JAX"), "JAC")
        self.assertEqual(kalshi.code("WSH"), "WAS")
        self.assertEqual(kalshi.code("LAR"), "LAR")

    def test_home_field_only_when_even(self):
        t = model.TeamInputs(0.0, 8, None, 0, False, 0.0)
        p = model.predict(t, t, None)
        self.assertAlmostEqual(p.expected_home_margin, model.HOME_FIELD)
        self.assertGreater(p.p_home_stats, 0.5)
        self.assertAlmostEqual(model.predict(t, t, None, neutral=True).p_home_stats, 0.5)

    def test_market_dominates_blend(self):
        t = model.TeamInputs(0.0, 8, None, 0, False, 0.0)
        p = model.predict(t, t, 0.80)
        self.assertGreater(p.p_home_final, 0.70)

    def test_gamma_yards_probability(self):
        # mean 60, sd 30: P(>= 60) a bit under 50% (right-skewed), P(>= 1) ~ 1, monotone
        p60 = props.p_at_least(60, 60.0, 900.0, "rush_yds")
        self.assertTrue(0.40 < p60 < 0.52)
        self.assertGreater(props.p_at_least(1, 60.0, 900.0, "rush_yds"), 0.97)
        self.assertGreater(props.p_at_least(40, 60.0, 900.0, "rush_yds"), p60)

    def test_td_poisson(self):
        self.assertAlmostEqual(props.p_at_least(1, 0.5, 0.5, "td"), 1 - math.exp(-0.5), places=4)


class NflPlayersTests(unittest.TestCase):
    def depth(self):
        return [
            {"slot": "qb", "pos": "QB", "starter": {"athlete_id": "1", "name": "Starter QB", "status": "Out"},
             "backup": {"athlete_id": "2", "name": "Backup QB"}},
            {"slot": "wr1", "pos": "WR", "starter": {"athlete_id": "3", "name": "WR One", "status": None}, "backup": None},
            {"slot": "lt", "pos": "LT", "starter": {"athlete_id": "4", "name": "Tackle", "status": "Questionable"}, "backup": None},
            {"slot": "h", "pos": "H", "starter": {"athlete_id": "5", "name": "Holder", "status": None}, "backup": None},
        ]

    def test_availability_counts_qb_and_skips_unvalued_slots(self):
        games = [[{"athlete_id": "1"}, {"athlete_id": "3"}]] * 4
        a = players.availability(self.depth(), [], games)
        self.assertEqual(a["starters_total"], 3)  # holder slot ignored
        self.assertEqual(a["qb"]["backup"], "Backup QB")
        qb_loss = players.SLOT_VALUE["qb"]
        lt_loss = 0.25 * players.SLOT_VALUE["lt"]
        self.assertAlmostEqual(a["injury_margin_loss"], round(qb_loss + lt_loss, 2), places=2)
        self.assertIn("Starter QB (QB)", a["missing"])

    def test_long_absence_counts_half(self):
        games = [[{"athlete_id": "3"}]] * 4  # QB hasn't played in 4 games
        a = players.availability(self.depth(), [], games)
        qb = next(r for r in a["players"] if r["slot"] == "qb")
        self.assertAlmostEqual(qb["expected_loss"], 0.5 * players.SLOT_VALUE["qb"])


class NflPropsTests(unittest.TestCase):
    def test_injured_receiver_share_goes_to_teammates(self):
        game = [{"athlete_id": "a", "name": "A", "rec": 6, "rec_yds": 80, "tgt": 8},
                {"athlete_id": "b", "name": "B", "rec": 4, "rec_yds": 40, "tgt": 6}]
        games = [game] * 6
        league = dict(props.LEAGUE_FALLBACK)
        base = props.project_team(games, [24] * 6, {}, {}, league, None, None, None)
        hurt = props.project_team(games, [24] * 6, {"a": 1.0}, {}, league, None, None, None)
        b0, b1 = base["players"]["b"]["proj"]["rec_yds"], hurt["players"]["b"]["proj"]["rec_yds"]
        self.assertGreater(b1, b0)
        self.assertLessEqual(b1, b0 * (1 + props.FREED_CAP) + 0.1)  # capped

    def test_backup_qb_gets_team_passing_with_haircut(self):
        game = [{"athlete_id": "q1", "name": "QB1", "pass_yds": 250, "pass_td": 2, "pass_att": 35}]
        qb = {"starter_id": "q2", "starter_name": "QB2", "backup_starting": True}
        out = props.project_team([game] * 5, [21] * 5, {"q1": 1.0}, {}, dict(props.LEAGUE_FALLBACK), None, None, qb)
        self.assertAlmostEqual(out["players"]["q2"]["proj"]["pass_yds"], 250 * props.BACKUP_QB_HAIRCUT, places=0)
        self.assertEqual(out["players"]["q1"]["proj"]["pass_yds"], 0.0)

    def test_pick_guardrails(self):
        row = {"athlete_id": "x", "name": "X", "stat": "rec", "line": 4, "proj": 5, "avg": 5, "recent": [5, 5, 5],
               "p_model": 0.70, "p_market": 0.50, "p_yes": 0.62, "miss_prob": 0.0, "games": 6, "recent_games": 3,
               "yes_ask": 0.51, "yes_bid": 0.49, "no_ask": 0.51, "no_bid": 0.49, "edge_yes": 0.09, "edge_no": -0.2, "ticker": "T"}
        self.assertEqual(len(props.pick([row])), 1)
        self.assertEqual(props.pick([{**row, "games": 3}]), [])           # role not established
        self.assertEqual(props.pick([{**row, "p_model": 0.80}]), [])      # 30-point gap with the market

    def test_grade(self):
        box = {"T": [{"athlete_id": "7", "rec_yds": 55, "td": 1}]}
        self.assertTrue(props.grade({"athlete_id": "7", "stat": "rec_yds", "line": 50, "side": "yes"}, box))
        self.assertTrue(props.grade({"athlete_id": "7", "stat": "td", "line": 2, "side": "no"}, box))
        self.assertFalse(props.grade({"athlete_id": "9", "stat": "rec", "line": 1, "side": "yes"}, box))  # absent = 0
        self.assertIsNone(props.grade({"athlete_id": "9", "stat": "pass_yds", "line": 200, "side": "yes"}, box))

    def test_tie_loses_every_side(self):
        h = {"home": "A", "away": "B", "pick": "A", "best_value": {"side": "away", "ask": 0.4, "edge": 0.05},
             "prop_picks": [], "result": {"home_score": 20, "away_score": 20, "home_won": False}}
        main.grade_row(h, None)
        self.assertFalse(h["model_pick_correct"])
        self.assertEqual(h["best_value"]["status"], "lost")


if __name__ == "__main__":
    unittest.main()
