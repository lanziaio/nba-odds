"""Who starts for each NFL team (ESPN depth chart) and what their absence costs.

The depth chart's first name at each slot is the regular starter; ESPN tags him with the injury
status. A starter's absence costs a fixed number of points of margin by position (starter -> backup),
times the chance he sits, times how much of the recent stretch he actually played (if he has already
been out for weeks, the team's recent results already show it).
"""
from __future__ import annotations

# Points of margin lost when the regular starter at this depth-chart slot is replaced by his backup.
# Rough consensus of public NFL value-over-replacement estimates: the quarterback dwarfs everything else.
SLOT_VALUE = {
    "qb": 4.5,  # starter -> typical backup; markets usually move 3-6 points for a QB change
    "wr1": 1.0, "wr2": 0.6, "wr3": 0.3, "te": 0.5, "rb": 0.4,
    "lt": 0.8, "rt": 0.5, "lg": 0.35, "rg": 0.35, "c": 0.4,
    "lde": 0.6, "rde": 0.6, "de": 0.6, "edge": 0.6,
    "ldt": 0.35, "rdt": 0.35, "dt": 0.35, "nt": 0.35,
    "wlb": 0.3, "mlb": 0.3, "slb": 0.3, "lilb": 0.3, "rilb": 0.3, "lb": 0.3, "olb": 0.3,
    "lcb": 0.55, "rcb": 0.55, "cb": 0.55, "nb": 0.25,
    "ss": 0.35, "fs": 0.35, "s": 0.35,
    "pk": 0.4,
}
OFFENSE_SHOWN = ("qb", "rb", "wr1", "wr2", "wr3", "te")   # always listed on the card
NO_BOX_FOOTPRINT = {"lt", "rt", "lg", "rg", "c", "pk"}  # linemen never appear in the box score
MAX_TEAM_LOSS = 9.0

# Probability the player sits, by listed status. Most NFL "Questionable" players do play.
STATUS_WEIGHT = {
    "out": 1.0, "injured reserve": 1.0, "ir": 1.0, "suspension": 1.0, "pup": 1.0,
    "physically unable to perform": 1.0, "non-football injury": 1.0,
    "doubtful": 0.8, "questionable": 0.25, "day-to-day": 0.25, "probable": 0.05,
}


def status_weight(status: str | None) -> float:
    return STATUS_WEIGHT.get((status or "").strip().lower(), 0.0)


def starters(depth: list[dict]) -> list[dict]:
    """One row per valued slot (the first formation that lists it wins)."""
    seen, out = set(), []
    for s in depth:
        if s["slot"] in SLOT_VALUE and s["slot"] not in seen:
            seen.add(s["slot"])
            out.append(s)
    return out


def availability(depth: list[dict], injuries: list[dict], games: list[list[dict]]) -> dict:
    """games: the team's recent box-score rows per game, most recent first."""
    inj_by_id = {i["athlete_id"]: i for i in injuries if i.get("athlete_id")}
    recent = games[:4]
    rows = []
    for s in starters(depth):
        st = s["starter"]
        inj = inj_by_id.get(st["athlete_id"]) or {}
        status = inj.get("status") or st.get("status")
        w = status_weight(status)
        if s["slot"] in NO_BOX_FOOTPRINT or not recent:
            played_share = 1.0
        else:
            played_share = sum(1 for g in recent if any(r["athlete_id"] == st["athlete_id"] for r in g)) / len(recent)
        value = SLOT_VALUE[s["slot"]]
        rows.append({
            "athlete_id": st["athlete_id"], "name": st["name"], "slot": s["slot"], "pos": s["pos"],
            "side": "offense" if s["slot"] in OFFENSE_SHOWN or s["slot"] in ("lt", "rt", "lg", "rg", "c") else
                    ("special" if s["slot"] == "pk" else "defense"),
            "status": status if w > 0 else "Available",
            "detail": inj.get("detail"), "comment": inj.get("comment"),
            "miss_prob": w, "impact": value, "played_share": round(played_share, 2),
            # a starter who hasn't played lately is already priced into recent results: count half
            "expected_loss": round(w * value * (0.5 + 0.5 * played_share), 2),
            "backup": (s.get("backup") or {}).get("name"),
            "backup_id": (s.get("backup") or {}).get("athlete_id"),
        })
    qb = next((r for r in rows if r["slot"] == "qb"), None)
    out_rows = [r for r in rows if r["miss_prob"] >= 0.8]
    unsure = [r for r in rows if 0 < r["miss_prob"] < 0.8]
    return {
        "players": rows,
        "starters_available": sum(1 for r in rows if r["miss_prob"] == 0),
        "starters_total": len(rows),
        "all_starters_available": all(r["miss_prob"] == 0 for r in rows),
        "all_core_available": all(r["miss_prob"] == 0 for r in rows),
        "qb": {"name": qb["name"], "status": qb["status"], "miss_prob": qb["miss_prob"], "backup": qb["backup"],
               "backup_id": qb["backup_id"], "athlete_id": qb["athlete_id"]} if qb else None,
        "missing": [f'{r["name"]} ({r["pos"]})' for r in out_rows],
        "uncertain": [f'{r["name"]} ({r["pos"]}, {r["status"]})' for r in unsure],
        "injury_margin_loss": round(min(MAX_TEAM_LOSS, sum(r["expected_loss"] for r in rows)), 2),
    }
