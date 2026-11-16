import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location("next_wake", Path(__file__).parent.parent / "scripts/next_wake.py")
nw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nw)

NOW = datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc)


def game(tip, state="pre", has_report=False, preview=False):
    return {"tip": tip.isoformat(), "report_at": (tip - timedelta(minutes=75)).isoformat(), "state": state,
            "has_report": has_report, "summary": {"preview": preview} if has_report else None}


class NextWakeTests(unittest.TestCase):
    def test_wakes_just_after_window_opens(self):
        tip = NOW + timedelta(hours=2)
        self.assertEqual(nw.next_wake(NOW, [[game(tip)]]), tip - timedelta(minutes=73))

    def test_overdue_report_runs_now(self):
        self.assertEqual(nw.next_wake(NOW, [[game(NOW + timedelta(minutes=30))]]), NOW)

    def test_preview_still_owes_the_real_report(self):
        tip = NOW + timedelta(hours=3)
        self.assertEqual(nw.next_wake(NOW, [[game(tip, has_report=True, preview=True)]]), tip - timedelta(minutes=73))

    def test_live_games_refresh_and_finished_slate_stops(self):
        self.assertEqual(nw.next_wake(NOW, [[game(NOW, state="in")]]), NOW + nw.LIVE_REFRESH)
        self.assertIsNone(nw.next_wake(NOW, [[game(NOW - timedelta(hours=4), state="post", has_report=True)], []]))

    def test_reported_game_wakes_for_closing_price_then_kickoff(self):
        tip = NOW + timedelta(hours=1)
        self.assertEqual(nw.next_wake(NOW, [[game(tip, has_report=True)]]), tip - nw.CLOSE_LEAD)
        self.assertEqual(nw.next_wake(tip - timedelta(minutes=2), [[game(tip, has_report=True)]]), tip + timedelta(minutes=5))

    def test_earliest_across_leagues(self):
        nba, nfl = NOW + timedelta(hours=5), NOW + timedelta(hours=2)
        self.assertEqual(nw.next_wake(NOW, [[game(nba)], [game(nfl)]]), nfl - timedelta(minutes=73))


if __name__ == "__main__":
    unittest.main()
