"""NFL win probability. Pure functions; the probability helpers come from the NBA model."""
from __future__ import annotations

from dataclasses import dataclass, field

from nba_odds.model import (edge_vs_price, expit, kalshi_fee, logit, market_prob_from_moneylines,  # noqa: F401
                            norm_cdf)

MARGIN_SD = 13.5          # std-dev of NFL final margin around expectation, in points
HOME_FIELD = 1.5          # points; home-field edge has shrunk to ~1.5 in recent seasons
RATING_SHRINK_GAMES = 4   # point differential per game is regressed toward 0 (8 games -> 2/3 weight)
FORM_GAMES = 3            # "recent form" = last 3 games (the NFL plays once a week)
FORM_WEIGHT = 0.15        # NFL margins are noisy: recent form moves the rating only a little
BYE_EDGE = 0.5            # points for coming off a bye (12+ days of rest) when the opponent isn't
MARKET_WEIGHT = 0.75      # log-odds weight of the de-vigged Vegas line (the NFL market is very efficient)


def spread_to_prob(home_spread: float | None) -> float | None:
    if home_spread is None:
        return None
    return norm_cdf(-float(home_spread) / MARGIN_SD)


def team_rating(diff_pg: float, gp: int) -> float:
    return diff_pg * gp / (gp + RATING_SHRINK_GAMES) if gp else 0.0


def form_adjustment(recent_margin: float | None, n: int, season_diff_pg: float) -> float:
    if not n or recent_margin is None:
        return 0.0
    return FORM_WEIGHT * (recent_margin - season_diff_pg) * n / (n + 3)


@dataclass
class TeamInputs:
    diff_pg: float
    gp: int
    recent_margin: float | None
    recent_games: int
    off_bye: bool
    injury_margin_loss: float


@dataclass
class Prediction:
    p_home_stats: float
    expected_home_margin: float
    p_home_market: float | None
    p_home_final: float
    components: dict = field(default_factory=dict)


def predict(home: TeamInputs, away: TeamInputs, p_home_market: float | None,
            neutral: bool = False, market_weight: float = MARKET_WEIGHT) -> Prediction:
    rh, ra = team_rating(home.diff_pg, home.gp), team_rating(away.diff_pg, away.gp)
    fh = form_adjustment(home.recent_margin, home.recent_games, home.diff_pg)
    fa = form_adjustment(away.recent_margin, away.recent_games, away.diff_pg)
    hfa = 0.0 if neutral else HOME_FIELD
    rest = (BYE_EDGE if home.off_bye else 0.0) - (BYE_EDGE if away.off_bye else 0.0)
    inj = -home.injury_margin_loss + away.injury_margin_loss
    margin = (rh - ra) + hfa + (fh - fa) + rest + inj
    p_stats = norm_cdf(margin / MARGIN_SD)
    p_final = (expit(market_weight * logit(p_home_market) + (1 - market_weight) * logit(p_stats))
               if p_home_market is not None else p_stats)
    return Prediction(
        p_home_stats=p_stats, expected_home_margin=margin, p_home_market=p_home_market, p_home_final=p_final,
        components={"season_rating_edge": round(rh - ra, 2), "home_field": hfa, "form_edge": round(fh - fa, 2),
                    "rest_edge": rest, "injury_edge": round(inj, 2)},
    )
