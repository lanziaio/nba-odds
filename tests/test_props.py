import unittest

from nba_odds import props


def row(pid, mins, pts, reb=5, ast=3, tpm=1, starter=True):
    return {"athlete_id": pid, "name": f"Player {pid}", "starter": starter, "dnp": False, "min": mins,
            "pts": pts, "reb": reb, "ast": ast, "tpm": tpm}


def team_games(n=10, star_out_recent=0):
    games = []
    for i in range(n):
        rows = []
        for p in range(1, 9):
            if p == 1 and i < star_out_recent:
                continue
            mins = 34 if p <= 5 else 22
            rows.append(row(str(p), mins, pts=28 if p == 1 else 12, starter=p <= 5))
        games.append(rows)
    return games


LEAGUE = {"pts": 114.0, "reb": 44.0, "ast": 26.0, "tpm": 13.0, "pra": 184.0}


class ProbabilityTests(unittest.TestCase):
    def test_monotonic_in_line(self):
        a = props.p_at_least(20, 22.0, 30.0, "pts")
        b = props.p_at_least(25, 22.0, 30.0, "pts")
        self.assertGreater(a, b)
        self.assertTrue(0.5 < a < 1)

    def test_count_stats_poisson_and_nb(self):
        self.assertAlmostEqual(props.p_at_least(1, 1.0, 1.0, "tpm"), 1 - 2.718281828 ** -1, places=3)
        nb = props.p_at_least(1, 1.0, 2.0, "tpm")  # over-dispersed: more zeros
        self.assertLess(nb, props.p_at_least(1, 1.0, 1.0, "tpm"))

    def test_name_normalisation(self):
        self.assertEqual(props.norm_name("Kelly Oubre Jr."), props.norm_name("Kelly Oubre"))
        self.assertEqual(props.norm_name("Nikola Jokić"), "nikola jokic")


class ProjectionTests(unittest.TestCase):
    def test_minutes_fit_240_and_star_projection(self):
        proj = props.project_team("T", team_games(), {}, {}, LEAGUE, None, None)
        total = sum(p["minutes"] for p in proj["players"].values())
        # never above 240; a short rotation is not stretched (the deep bench takes the rest)
        self.assertLessEqual(total, 240.5)
        self.assertAlmostEqual(total, 5 * 34 + 3 * 22, delta=1)
        self.assertAlmostEqual(proj["players"]["1"]["proj"]["pts"], 28, delta=3)

    def test_injured_star_frees_minutes_and_shots(self):
        healthy = props.project_team("T", team_games(), {}, {}, LEAGUE, None, None)
        hurt = props.project_team("T", team_games(), {"1": 1.0}, {}, LEAGUE, None, None)
        self.assertNotIn("1", hurt["players"])
        self.assertGreater(hurt["players"]["2"]["minutes"], healthy["players"]["2"]["minutes"])
        self.assertGreater(hurt["players"]["2"]["proj"]["pts"], healthy["players"]["2"]["proj"]["pts"])

    def test_questionable_player_priced_as_if_he_plays(self):
        p = props.project_team("T", team_games(), {"1": 0.5}, {}, LEAGUE, None, None)["players"]["1"]
        self.assertGreater(p["minutes"], 30)

    def test_soft_rebounding_defense_raises_rebounds(self):
        base = props.project_team("T", team_games(), {}, {}, LEAGUE, None, None)
        soft = props.project_team("T", team_games(), {}, {"reb": 1.1}, LEAGUE, None, None)
        self.assertGreater(soft["players"]["1"]["proj"]["reb"], base["players"]["1"]["proj"]["reb"])


class PickTests(unittest.TestCase):
    def priced(self, p_yes, yes_bid, yes_ask, miss=0.0):
        return {"athlete_id": "1", "name": "P1", "stat": "pts", "line": 25, "proj": 27, "avg": 26,
                "p_yes": p_yes, "p_model": p_yes, "p_market": (yes_bid + yes_ask) / 2, "miss_prob": miss,
                "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": round(1 - yes_ask, 2), "no_ask": round(1 - yes_bid, 2),
                "edge_yes": round(p_yes - yes_ask - 0.02, 4), "edge_no": round((1 - p_yes) - (1 - yes_bid) - 0.02, 4),
                "ticker": "X"}

    def test_value_pick(self):
        picks = props.pick([self.priced(0.62, 0.52, 0.54)])
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["side"], "yes")
        self.assertEqual(picks[0]["strength"], "value")

    def test_recent_hits_counted_for_the_pick_side(self):
        r = self.priced(0.62, 0.52, 0.54)
        r["recent"] = [30, 20, 26, 25, 18, 27]
        pk = props.pick([r])[0]
        self.assertEqual((pk["hits"], pk["games"]), (4, 6))  # 25+ in 4 of 6
        self.assertEqual(pk["last5_avg"], 23.8)
        r2 = self.priced(0.30, 0.52, 0.54)  # model likes the under
        r2["recent"] = [30, 20, 26, 25, 18, 27]
        pk2 = props.pick([r2])[0]
        self.assertEqual((pk2["side"], pk2["hits"]), ("no", 2))

    def test_injured_player_and_wide_market_skipped(self):
        self.assertEqual(props.pick([self.priced(0.62, 0.52, 0.54, miss=0.35)]), [])
        self.assertEqual(props.pick([self.priced(0.70, 0.40, 0.54)]), [])

    def test_grade(self):
        box = {"T": [{"athlete_id": "1", "min": 30, "pts": 26}]}
        self.assertTrue(props.grade({"athlete_id": "1", "stat": "pts", "line": 25, "side": "yes"}, box))
        self.assertFalse(props.grade({"athlete_id": "1", "stat": "pts", "line": 25, "side": "no"}, box))
        self.assertIsNone(props.grade({"athlete_id": "9", "stat": "pts", "line": 25, "side": "yes"}, box))


if __name__ == "__main__":
    unittest.main()
