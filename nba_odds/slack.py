"""Post a report to Slack through an Incoming Webhook (SLACK_WEBHOOK_URL)."""
from __future__ import annotations

import json
import os
import urllib.request


def _pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p:.0%}"


def _ml(v: int | None) -> str:
    return "n/a" if v is None else (f"+{v}" if v > 0 else str(v))


def _avail_line(s: dict) -> str:
    a = s["availability"]
    head = f'*{s["abbr"]}* starters {a["starters_available"]}/{a["starters_total"]} available'
    parts = []
    for r in a["players"]:
        if r["miss_prob"] > 0:
            parts.append(f'{r["name"]} ({r["status"]}{", " + r["detail"] if r["detail"] else ""})')
    return head + (": " + "; ".join(parts) if parts else " (full strength)")


def _form_line(s: dict) -> str:
    f, st = s["form"], s["standing"]
    rec = f'{st.get("wins", 0)}-{st.get("losses", 0)}'
    l7 = f'{f["wins"]}-{f["losses"]} last 7 days' + (f' (avg {f["avg_margin"]:+.1f})' if f["avg_margin"] is not None else "")
    extra = " · back-to-back" if f["back_to_back"] else ""
    return f'*{s["abbr"]}* {rec} · streak {st.get("streak") or "-"} · {l7}{extra}'


def format_message(r: dict, dashboard_url: str | None = None) -> dict:
    h, a, m, k = r["home"], r["away"], r["model"], r["kalshi"]
    ph = m["p_home_final"]
    title = f'{a["abbr"]} @ {h["abbr"]} · {r["tip_et"]}'
    probs = (
        f'*Model:* {h["abbr"]} {_pct(ph)} · {a["abbr"]} {_pct(1 - ph)}   '
        f'(expected margin {h["abbr"]} {m["expected_home_margin"]:+.1f})\n'
        f'*Vegas:* {h["abbr"]} {_pct(r["market"]["p_home"])} '
        f'(ML {h["abbr"]} {_ml(r["market"]["home_ml"])} / {a["abbr"]} {_ml(r["market"]["away_ml"])}) · stats-only {_pct(m["p_home_stats"])}\n'
    )
    if k:
        probs += (f'*Kalshi:* {h["abbr"]} {_pct(k["p_home"])} '
                  f'(ask {h["abbr"]} {k["home"]["ask"] or "-"} / {a["abbr"]} {k["away"]["ask"] or "-"})')
    else:
        probs += "*Kalshi:* no market found"
    bv = r.get("best_value")
    value = (f':dart: *Value:* {bv["team"]} YES at {bv["ask"]:.2f} → edge {bv["edge"]*100:+.1f}¢ per $1 after fees'
             if bv else "No Kalshi side clears the 3¢ value threshold.")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"NBA pre-game: {title}"[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": probs}},
        {"type": "section", "text": {"type": "mrkdwn", "text": value}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _avail_line(a) + "\n" + _avail_line(h)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _form_line(a) + "\n" + _form_line(h)}},
    ]
    if r["flags"]:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(r["flags"])[:2900]}]})
    if dashboard_url:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Open dashboard"}, "url": dashboard_url}]})
    return {"text": f'NBA pre-game: {title} · {r["pick"]["team"]} {_pct(r["pick"]["prob"])}', "blocks": blocks}


def post(report: dict) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    payload = json.dumps(format_message(report, os.environ.get("DASHBOARD_URL"))).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status == 200
