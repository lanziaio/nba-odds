# NBA & NFL Win Odds

Every NBA and NFL game day, about 1 hour before each tip-off, this job calculates each team's chance of winning, checks whether the regular starters and main rotation players are available, compares the result with Vegas and Kalshi, posts to Slack and updates a dashboard. The NFL works the same way (see [NFL](#nfl)); the dashboard has an NBA / NFL switch.

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

## Player props (Kalshi)

Kalshi lists NBA player props as ladders (e.g. "Brunson 25+ points", "Zubac 10+ rebounds") in the series `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT` and `KXNBAPRA`, readable without a key. For each game the job:

1. **Projects minutes** for every rotation player: his recent minutes. A player tagged Out frees his minutes for teammates. The rotation is then fitted to 240, trimming the bench first.
2. **Projects production**: recent per-minute points / rebounds / assists / threes, with last season's rates counted as 8 extra games (ESPN player stats), times projected minutes.
3. **Adjusts for the matchup**: the team's points are scaled to the Vegas implied team score; rebounds, assists and threes are scaled by what the opponent allows vs league average (from box scores) and by the game's pace (Vegas total).
4. **Prices every Kalshi line**: normal distribution for points and PRA, negative binomial for rebounds / assists / threes. The result is blended 60/40 with Kalshi's own price, since 12 games of data can't beat a market that knows careers. Edge = probability − ask − Kalshi fee, for both YES (over) and NO (under).
5. **Picks 1-2 per team**, only for players with no injury tag and a tight market (bid/ask ≤ 6¢, price 0.20-0.85): **value** = 5¢+ edge, **lean** = 2-5¢.

Props are priced as if the player plays: Kalshi settles a prop at a fair price when the player doesn't take the court. Each card also shows the starters' season average, the projection and the chance to reach the average. Picks are graded from the final box score and tracked on the dashboard (hit rate vs expected, and P&L at $1 per contract).

Sportsbook props (DraftKings, FanDuel…) are available through The Odds API, but each game costs ~4 credits per market group, about 1,000 credits a month. That's beyond the free tier, so it's not wired in yet.

## Track record

Every pick that goes out at T-60 is kept in `docs/data/history.json` and graded once the game is final (any run at least 3 hours after tip-off, plus the 11:00 UTC morning sweep):

- **Winner picks:** the model's favourite in every game, scored against Vegas's and Kalshi's favourites (accuracy and Brier score).
- **Game bets:** the Kalshi YES flagged as value on a game winner.
- **Prop picks:** value and lean, graded from the final box score. A player who didn't play counts as **void**, because Kalshi settles those at a fair price.

P&L assumes one $1 Kalshi contract per pick, bought at the ask shown when the pick was made, minus Kalshi's taker fee. The dashboard's **Track record** tab (`#track`) shows:
- hit rate vs what the model expected
- cumulative P&L, P&L and ROI
- breakdowns by stat, over/under, edge size and probability bucket (calibration)
- the full pick log, filterable by 7 / 30 days / season and by type

Preview reports (`--all-today`) never count.

Every tunable number lives at the top of `nba_odds/model.py`, `nba_odds/players.py` and `nba_odds/report.py`.

The dashboard keeps a **track record** (accuracy and Brier score for the model, Vegas, Kalshi and stats-only, plus the P&L of the value picks at $1 per contract). Let it build a few weeks of history before trusting any edge: beating the closing line is hard, and the stats model is deliberately simple.

Note: 1 hour before tip, starting lineups are usually not confirmed yet (teams announce them about 30 min before). "Starters" here means each team's regular starters, checked against the injury report.

## NFL

Same job, same schedule, data in `docs/data/nfl` (dashboard: `#nfl`, track record `#nfl/track`). What changes:

| | NFL |
|---|---|
| Schedule and lines | ESPN `football/nfl` scoreboard for the current week (DraftKings) |
| Starters | ESPN depth chart (`teams/{id}/depthcharts`): the first name at each slot, with ESPN's injury tag and the backup |
| Injury cost | points of margin by slot when the starter sits: QB 4.5, WR1 1.0, LT 0.8, edge 0.6, CB 0.55, WR2 0.6, TE 0.5, other line/defense 0.25-0.4 · chance he sits (Out 100 %, Doubtful 80 %, Questionable 25 %) · counted half if he hasn't played lately |
| Stats model | point differential per game shrunk over 4 games, home field 1.5, last-3-games form (weight 0.15), +0.5 off a bye, SD 13.5 points |
| Final | 75 % Vegas + 25 % stats, in log-odds |
| Kalshi | `KXNFLGAME`; team codes differ from ESPN for JAX (JAC) and WSH (WAS) |
| Props | `KXNFLPASSYDS`, `KXNFLPASSTDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLREC`, `KXNFLTD` |

**Props model.**
- Each player's recency-weighted average in the games he played this season. An injured teammate's share (if he was playing lately) goes to the healthy players, capped at +40 % each.
- Game script from Vegas (implied team points; favourites run a bit more) times the square root of what the opponent allows. The Vegas total already knows the defense, so the full factor would count it twice.
- Passing goes to the expected starting QB only; a backup starting gets the team's passing average minus 10 %.
- Touchdowns use a TD rate per carry + target, shrunk toward the league rate, times expected opportunities and implied points.
- Distributions: gamma for yards, negative binomial for receptions, Poisson for TDs. Blended 70/30 with Kalshi's price.
- Picks follow the NBA rules, plus two guardrails: the player has an established role (4+ games, 2 of the last 3), and the model and Kalshi differ by no more than 25 points of probability (a bigger gap is usually news the model can't see).
- Grading: a player missing from the box score counts as 0, since ESPN lists only players with a stat. A QB missing on a passing prop counts as void.

Ties: every game bet loses and the winner pick counts as wrong.

Tunable numbers live at the top of `nfl_odds/model.py`, `nfl_odds/players.py` and `nfl_odds/props.py`.

## Run it locally

Python 3.10+ only, no dependencies.

```bash
python3 -m nba_odds run --all-today      # preview every remaining game today (no Slack)
python3 -m nba_odds run --no-slack       # what the scheduled job does, without posting
python3 -m nba_odds settle               # record final scores
python3 -m nfl_odds run --all-week       # NFL: preview every remaining game this week
python3 -m nfl_odds run                  # NFL: what the scheduled job does
python3 -m unittest discover -s tests    # tests
cd docs && python3 -m http.server 8765   # dashboard at http://localhost:8765
```

## Calibration and closing-line value

Four picks a game is far too few to tell skill from luck, so two faster checks run alongside the record:

- **Every line, graded.** Each report saves every Kalshi prop line it priced (~250 per NBA game, ~300 per NFL game) to `data/lines/<sport>/<event>.json` (not published). After the game they are graded like picks, and `nba_odds/calib.py` writes `docs/data[/nfl]/calibration.json`: model vs Kalshi vs blend (Brier, log loss), a calibration table, what the pick rule would have done on every line, and the Kalshi weight that would have predicted best. Once a league has 40 graded games (and 4,000 lines), that fitted weight (kept within 50-95%) replaces the default automatically. Lines on one player's ladder move together, so the real sample is closer to player-games than to lines.
- **Closing-line value.** A few minutes before tip-off the job records each pick's Kalshi mid (the run wakes at T-4 min for it). CLV = closing mid on our side minus the ask we took; beating the close consistently is the standard early sign of a real edge.

Starting weights (from 2026-11-13 to 15, rebuilt with `scripts/backfill_lines.py`):

| | Lines | Model log loss | Kalshi log loss | Pick rule on every line | Kalshi weight |
|---|---|---|---|---|---|
| NBA | 2,380 (11 games) | 0.506 | 0.489 | +6% ROI (237 bets), positive in both halves | 60% (kept) |
| NFL | 2,229 (7 games) | 0.523 | 0.470 | loses at every weight; model says 85%, hits 70% | 85% (was 70%) |

Both tabs show this under **Track record → Model vs Kalshi on every line**.

## Schedule (GitHub Actions)

`.github/workflows/pregame.yml` does not rely on a tight cron: GitHub drops most `*/15` scheduled runs on busy days (on 2026-11-15 only 3 of ~40 fired and 9 games got no report). Instead, each run does a pass, then asks `scripts/next_wake.py` when the next report is due across both leagues (75 minutes before tip-off, or every 30 minutes while a game is live for scores), sleeps until then and runs again. Before GitHub's 6-hour job limit it hands off to a fresh run of itself. The crons (11:00 UTC to settle overnight games and load the slate, backups at 14, 17, 20 and 23 UTC, and 12:00 UTC on Sundays) only need to start that chain once a day. Each game is reported once (`state.json`), 73 to 75 minutes before tip-off. Results are committed back to the repo.

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
nfl_odds/             the NFL version (espn, kalshi, players = depth chart, model, props, report, main)
docs/index.html       dashboard (reads docs/data/*.json)
```

For information and research only. Not betting advice.
