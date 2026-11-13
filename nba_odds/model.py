"""Probability math. Everything here is pure (no I/O) so it is easy to test."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MARGIN_SD = 12.0          # std-dev of NBA final margin around expectation, in points
HOME_COURT = 2.0          # points; recent seasons sit around 2-2.5
B2B_PENALTY = 1.5         # points lost on the second night of a back-to-back
RATING_SHRINK_GAMES = 10  # early-season point differential is regressed toward 0
FORM_WEIGHT = 0.30        # how much last-7-days margin vs season margin moves the rating
MARKET_WEIGHT = 0.70      # log-odds weight of the de-vigged Vegas line in the final blend


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def american_to_prob(ml: int | None) -> float | None:
    if ml is None or ml == 0:
        return None
    return (-ml) / (-ml + 100.0) if ml < 0 else 100.0 / (ml + 100.0)


def devig(p_home_raw: float, p_away_raw: float) -> float:
    """Remove the bookmaker margin (proportional method). Returns P(home)."""
    return p_home_raw / (p_home_raw + p_away_raw)


def market_prob_from_moneylines(home_ml: int | None, away_ml: int | None) -> float | None:
    ph, pa = american_to_prob(home_ml), american_to_prob(away_ml)
    if ph is None or pa is None:
        return None
    return devig(ph, pa)


def spread_to_prob(home_spread: float | None) -> float | None:
    """home_spread uses the betting convention: -6.5 means home favoured by 6.5."""
    if home_spread is None:
        return None
    return norm_cdf(-float(home_spread) / MARGIN_SD)


def team_rating(diff_pg: float, gp: int) -> float:
    return diff_pg * gp / (gp + RATING_SHRINK_GAMES) if gp else 0.0


def form_adjustment(last7_margin: float | None, n7: int, season_diff_pg: float) -> float:
    if not n7 or last7_margin is None:
        return 0.0
    return FORM_WEIGHT * (last7_margin - season_diff_pg) * n7 / (n7 + 3)


@dataclass
class TeamInputs:
    diff_pg: float
    gp: int
    last7_margin: float | None
    last7_games: int
    back_to_back: bool
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
    rh = team_rating(home.diff_pg, home.gp)
    ra = team_rating(away.diff_pg, away.gp)
    fh = form_adjustment(home.last7_margin, home.last7_games, home.diff_pg)
    fa = form_adjustment(away.last7_margin, away.last7_games, away.diff_pg)
    hca = 0.0 if neutral else HOME_COURT
    rest = (-B2B_PENALTY if home.back_to_back else 0.0) - (-B2B_PENALTY if away.back_to_back else 0.0)
    inj = -home.injury_margin_loss + away.injury_margin_loss

    margin = (rh - ra) + hca + (fh - fa) + rest + inj
    p_stats = norm_cdf(margin / MARGIN_SD)

    if p_home_market is not None:
        p_final = expit(market_weight * logit(p_home_market) + (1 - market_weight) * logit(p_stats))
    else:
        p_final = p_stats

    return Prediction(
        p_home_stats=p_stats,
        expected_home_margin=margin,
        p_home_market=p_home_market,
        p_home_final=p_final,
        components={
            "season_rating_edge": round(rh - ra, 2),
            "home_court": hca,
            "form_edge": round(fh - fa, 2),
            "rest_edge": rest,
            "injury_edge": round(inj, 2),
        },
    )


def kalshi_fee(price: float) -> float:
    """Approx Kalshi taker fee per $1 contract: 0.07 * P * (1 - P), rounded up to the cent."""
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def edge_vs_price(p_true: float, ask: float | None) -> float | None:
    """Expected profit per $1 contract when buying YES at `ask`, after fees."""
    if ask is None or not (0 < ask < 1):
        return None
    return p_true - ask - kalshi_fee(ask)
