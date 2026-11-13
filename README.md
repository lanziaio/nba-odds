# NBA Win Odds

Every NBA game day, about 1 hour before each tip-off, this job calculates each team's chance of winning, checks whether the regular starters and main rotation players are available, compares the result with Vegas and Kalshi, posts to Slack and updates a dashboard.

## What goes into each game

| Input | Source | Cost |
|---|---|---|
| Schedule, tip-off times, final scores | ESPN public API | free |
| Vegas moneyline / spread / total (DraftKings), and how it moved since open | ESPN public API | free |
| Multi-book consensus (optional, replaces the DraftKings line when set) | The Odds API (`ODDS_API_KEY`) | free tier: 500 credits/mo, 1 credit per run with games due |
| Season record, point differential, streak | ESPN standings | free |
| Last 7 days results, back-to-backs | ESPN team schedules | free |
| Regular starters and rotation (who started and who played most in the last 12 games) | ESPN box scores (cached in `data/cache/box`) | free |
| Injury report (Out / Doubtful / Questionable / Day-To-Day) | ESPN injuries | free |
| Kalshi prices (bid/ask/last, both sides) | Kalshi public market API, series `KXNBAGAME` | free, no key |

## How the probability is built

1. **Vegas:** moneylines converted to probabilities with the bookmaker margin removed.
2. **Stats model:** expected home margin = season point differential (regressed early in the season) + home court (2 pts) + last-7-days form + back-to-back penalty (1.5 pts) − injuries. A player's injury cost = his value above replacement (Game Score per game, in points) × chance he sits (Out 100 %, Doubtful 85 %, Questionable 50 %, Day-To-Day 35 %) × share of recent games he played (if he has already been missing games, the recent results already show it). Margin → probability with a normal curve (SD 12 points).
3. **Final:** 70 % Vegas + 30 % stats model, blended in log-odds. Vegas is the strongest single predictor; the stats model nudges it.
4. **Kalshi edge:** final probability − Kalshi YES ask − Kalshi fee (≈ 0.07·P·(1−P)). A side is flagged as **value** at 3 ¢ or more per $1 contract.

Every tunable number lives at the top of `nba_odds/model.py`, `nba_odds/players.py` and `nba_odds/report.py`.

The dashboard keeps a **track record** (accuracy and Brier score for the model, Vegas, Kalshi and stats-only, plus the P&L of the value picks at $1 per contract). Let it build a few weeks of history before trusting any edge: beating the closing line is hard, and the stats model is deliberately simple.

Note: 1 hour before tip, starting lineups are usually not confirmed yet (teams announce them about 30 min before). "Starters" here means each team's regular starters, checked against the injury report.

## Run it locally

Python 3.10+ only, no dependencies.

```bash
python3 -m nba_odds run --all-today      # preview every remaining game today (no Slack)
python3 -m nba_odds run --no-slack       # what the scheduled job does, without posting
python3 -m nba_odds settle               # record final scores
python3 -m unittest discover -s tests    # tests
cd docs && python3 -m http.server 8765   # dashboard at http://localhost:8765
```

## Schedule (GitHub Actions)

`.github/workflows/pregame.yml` runs every 15 minutes from 14:00 to 03:59 UTC (all NBA tip-off times in EST and EDT), plus an 11:00 UTC sweep for overnight scores. Each run reports every game tipping off within the next 75 minutes that hasn't been reported yet (`docs/data/state.json`), so each game is posted once, roughly 45 to 75 minutes before tip-off (GitHub can start scheduled runs a few minutes late). Results are committed back to the repo.

Minutes: about 56 runs/day of under a minute. That's free on a public repo, or about 1,700 of the 2,000 free minutes/month on a private repo.

### Setup

1. Push this repo to GitHub.
2. **Settings → Secrets and variables → Actions:**
   - Secret `SLACK_WEBHOOK_URL`: a Slack Incoming Webhook for the channel you want (Slack → Apps → Incoming Webhooks).
   - Secret `ODDS_API_KEY` (optional): from the-odds-api.com.
   - Variable `DASHBOARD_URL`: the Pages URL below (adds an "Open dashboard" button in Slack).
3. **Settings → Pages:** deploy from branch `main`, folder `/docs`. Pages on a private repo needs a paid GitHub plan; otherwise make the repo public or host `docs/` on Vercel/Netlify.
4. **Actions → pregame → Run workflow → preview-all** to fill today's dashboard right away.

## Files

```
nba_odds/espn.py      schedule, lines, standings, injuries, box scores
nba_odds/kalshi.py    Kalshi prices (ESPN→Kalshi team codes)
nba_odds/odds_api.py  optional multi-book consensus
nba_odds/players.py   regular starters / rotation, availability, injury cost
nba_odds/model.py     probability math (pure functions)
nba_odds/report.py    builds one game report + flags
nba_odds/slack.py     Slack message
nba_odds/main.py      CLI, the T-60 window, history, track record
docs/index.html       dashboard (reads docs/data/*.json)
```

For information and research only. Not betting advice.
