"""How long the pregame job should sleep before its next run.

GitHub's scheduled cron is best-effort: on busy days most */15 runs are simply
dropped (2026-11-15: 3 of ~40 fired), so games slipped past their T-60 window
with no report. The workflow therefore keeps itself awake: after each run it
asks this script when the next report is due (or when live scores need a
refresh) and sleeps until then.

Prints whole seconds to wait (0 = run now), or "none" when nothing is pending
in either league's slate. Reads only the committed index files.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEXES = [ROOT / "docs/data/index.json", ROOT / "docs/data/nfl/index.json"]
LIVE_REFRESH = timedelta(minutes=30)   # update scores while games are on
AFTER_OPEN = timedelta(minutes=2)      # wake just after the window opens


def next_wake(now: datetime, slates: list[list[dict]]) -> datetime | None:
    wakes: list[datetime] = []
    for slate in slates:
        for g in slate:
            if g.get("state") == "in":
                wakes.append(now + LIVE_REFRESH)
            elif g.get("state") == "pre" and (not g.get("has_report") or (g.get("summary") or {}).get("preview")):
                # No report yet, or only a preview: the real T-60 report is still owed.
                tip = datetime.fromisoformat(g["tip"])
                if tip <= now:
                    continue  # ESPN hasn't flipped the state yet; nothing to report
                wakes.append(max(now, datetime.fromisoformat(g["report_at"]) + AFTER_OPEN))
            elif g.get("state") == "pre":
                # Reported (or previewed); wake at kickoff to flip it to live.
                tip = datetime.fromisoformat(g["tip"])
                if tip > now:
                    wakes.append(tip + timedelta(minutes=5))
    return min(wakes) if wakes else None


def main() -> int:
    now = datetime.now(timezone.utc)
    slates = []
    for p in INDEXES:
        try:
            slates.append(json.loads(p.read_text()).get("slate", []))
        except (OSError, ValueError):
            continue
    w = next_wake(now, slates)
    print("none" if w is None else max(0, int((w - now).total_seconds())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
