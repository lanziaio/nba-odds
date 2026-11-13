import unittest
from datetime import datetime, timezone

from nba_odds import kalshi, model, players


class ModelTests(unittest.TestCase):
    def test_american_to_prob(self):
        self.assertAlmostEqual(model.american_to_prob(-200), 2 / 3, places=4)
        self.assertAlmostEqual(model.american_to_prob(200), 1 / 3, places=4)
        self.assertIsNone(model.american_to_prob(None))

    def test_devig_sums_to_one(self):
        p = model.market_prob_from_moneylines(-150, 130)
        self.assertTrue(0.55 < p < 0.6)
        self.assertAlmostEqual(model.market_prob_from_moneylines(-110, -110), 0.5)

    def test_spread_sign(self):
        self.assertGreater(model.spread_to_prob(-6.5), 0.5)  # home favoured
        self.assertLess(model.spread_to_prob(6.5), 0.5)

    def test_even_teams_home_court(self):
        t = model.TeamInputs(0.0, 10, None, 0, False, 0.0)
        pred = model.predict(t, t, None)
        self.assertAlmostEqual(pred.expected_home_margin, model.HOME_COURT)
        self.assertGreater(pred.p_home_stats, 0.5)
        neutral = model.predict(t, t, None, neutral=True)
        self.assertAlmostEqual(neutral.p_home_stats, 0.5)

    def test_injury_and_b2b_move_margin(self):
        base = model.TeamInputs(0.0, 10, None, 0, False, 0.0)
        hurt = model.TeamInputs(0.0, 10, None, 0, True, 4.0)
        pred = model.predict(hurt, base, None, neutral=True)
        self.assertAlmostEqual(pred.expected_home_margin, -4.0 - model.B2B_PENALTY)

    def test_blend_stays_between(self):
        t = model.TeamInputs(5.0, 20, None, 0, False, 0.0)
        o = model.TeamInputs(-5.0, 20, None, 0, False, 0.0)
        pred = model.predict(t, o, 0.6)
        self.assertTrue(0.6 < pred.p_home_final < pred.p_home_stats)

    def test_kalshi_fee_and_edge(self):
        self.assertEqual(model.kalshi_fee(0.5), 0.02)
        self.assertAlmostEqual(model.edge_vs_price(0.6, 0.5), 0.08)
        self.assertIsNone(model.edge_vs_price(0.6, None))


class KalshiTests(unittest.TestCase):
    def test_event_ticker_uses_eastern_date_and_codes(self):
        tip = datetime(2026, 11, 14, 3, 30, tzinfo=timezone.utc)  # 10:30 PM ET on Nov 13
        self.assertEqual(kalshi.event_ticker("OKC", "LAC", tip), "KXNBAGAME-26NOV13OKCLAC")
        self.assertEqual(kalshi.event_ticker("GS", "SA", tip), "KXNBAGAME-26NOV13GSWSAS")
        self.assertEqual(kalshi.event_ticker("NY", "WSH", tip), "KXNBAGAME-26NOV13NYKWAS")


def line(pid, mins, starter, pts=10, dnp=False):
    return {"athlete_id": pid, "name": f"P{pid}", "starter": starter, "dnp": dnp, "min": mins, "pts": pts,
            "fgm": 4, "fga": 9, "ftm": 2, "fta": 2, "oreb": 1, "dreb": 4, "ast": 3, "stl": 1, "blk": 0, "to": 1, "pf": 2}


class PlayerTests(unittest.TestCase):
    def setUp(self):
        games = []
        for _ in range(10):
            rows = [line(str(i), 34 - i, i <= 5, pts=25 - i * 2) for i in range(1, 6)]
            rows += [line(str(i), 22 - i, False) for i in range(6, 11)]
            games.append(rows)
        self.core = players.core_players(games)

    def test_starters_and_rotation(self):
        self.assertEqual(self.core["starters"], ["1", "2", "3", "4", "5"])
        self.assertEqual(len(self.core["rotation"]), 3)
        self.assertEqual(self.core["rotation"][0], "6")

    def test_availability(self):
        inj = [{"athlete_id": "1", "name": "P1", "status": "Out", "fantasy": "OUT", "detail": "Knee"},
               {"athlete_id": "7", "name": "P7", "status": "Day-To-Day", "fantasy": "GTD", "detail": None}]
        a = players.availability(self.core, inj)
        self.assertEqual(a["starters_available"], 4)
        self.assertFalse(a["all_starters_available"])
        self.assertEqual(a["missing"], ["P1"])
        self.assertEqual(len(a["uncertain"]), 1)
        self.assertGreater(a["injury_margin_loss"], 0)

    def test_full_strength(self):
        a = players.availability(self.core, [])
        self.assertTrue(a["all_starters_available"])
        self.assertEqual(a["injury_margin_loss"], 0)


if __name__ == "__main__":
    unittest.main()
