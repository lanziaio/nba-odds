// Track record tab: every pick the app has made, graded, with P&L after Kalshi fees.
// Data: data/history.json (NBA) or data/nfl/history.json (NFL), one row per game, written by the scheduled job.
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const pct = p => p == null ? "–" : Math.round(p * 100) + "%";
  const money = v => v == null ? "–" : (v >= 0 ? "+$" : "−$") + Math.abs(v).toFixed(2);
  const etDate = iso => new Date(iso).toLocaleDateString("en-CA", {timeZone: "America/Phoenix"}); // YYYY-MM-DD
  const shortDate = d => { const [y, m, dd] = d.split("-"); return `${+m}/${+dd}`; };
  const STATS = {
    nba: {pts: "Points", reb: "Rebounds", ast: "Assists", tpm: "Threes", pra: "Pts+Reb+Ast"},
    nfl: {pass_yds: "Passing yds", pass_td: "Passing TDs", rush_yds: "Rushing yds", rec_yds: "Receiving yds",
          rec: "Receptions", td: "Touchdowns"},
  };
  let STAT = STATS.nba;
  const SERIES = [
    {key: "props-value", label: "Props · value"},
    {key: "props-lean", label: "Props · lean"},
    {key: "game", label: "Game winner bets"},
  ];
  const state = {period: "all", kind: "all", logLimit: 50, rows: [], picks: []};

  // ---------- flatten history into one list of picks ----------
  function flatten(history) {
    const out = [];
    for (const h of history) {
      if (h.preview) continue; // only picks that went out at T-60 count
      const date = etDate(h.tip), game = `${h.away} @ ${h.home}`, settled = !!h.result;
      const pHome = h.p_home_final;
      const pickHome = h.pick === h.home;
      out.push({
        kind: "winner", date, game, label: `${h.pick} to win`, p: pickHome ? pHome : 1 - pHome,
        price: h.p_home_kalshi == null ? null : (pickHome ? h.p_home_kalshi : 1 - h.p_home_kalshi),
        status: !settled ? "pending" : h.model_pick_correct ? "won" : "lost", pnl: null, edge: null,
        vegasFavWon: settled && h.p_home_market != null ? (h.p_home_market >= 0.5) === h.result.home_won : null,
        kalshiFavWon: settled && h.p_home_kalshi != null ? (h.p_home_kalshi >= 0.5) === h.result.home_won : null,
        briers: settled ? {model: h.p_home_final, vegas: h.p_home_market, kalshi: h.p_home_kalshi, stats: h.p_home_stats} : null,
        homeWon: settled ? h.result.home_won : null,
      });
      const bv = h.best_value;
      if (bv) {
        const pSide = bv.side === "home" ? pHome : 1 - pHome;
        out.push({kind: "game", date, game, label: `${bv.team} to win (Kalshi YES)`, p: pSide, price: bv.ask, edge: bv.edge,
                  status: bv.status || (settled ? "pending" : "pending"), pnl: bv.pnl ?? null});
      }
      for (const pk of h.prop_picks || []) {
        out.push({kind: `props-${pk.strength}`, date, game, label: `${pk.team} · ${pk.label}`, p: pk.p, price: pk.ask, edge: pk.edge,
                  stat: pk.stat, side: pk.side, status: pk.status || "pending", pnl: pk.pnl ?? null});
      }
    }
    return out;
  }

  function inPeriod(p) {
    if (state.period === "all") return true;
    const days = +state.period;
    const cutoff = new Date(Date.now() - days * 864e5).toLocaleDateString("en-CA", {timeZone: "America/Phoenix"});
    return p.date >= cutoff;
  }
  const isBet = p => p.kind !== "winner";
  const graded = p => p.status === "won" || p.status === "lost";

  function summarize(list) {
    const g = list.filter(graded);
    const wins = g.filter(p => p.status === "won").length;
    const pnl = g.reduce((s, p) => s + (p.pnl || 0), 0);
    const staked = g.reduce((s, p) => s + (p.price || 0), 0);
    return {n: g.length, wins, hit: g.length ? wins / g.length : null,
            expected: g.length ? g.reduce((s, p) => s + p.p, 0) / g.length : null,
            pnl, roi: staked ? pnl / staked : null,
            pending: list.filter(p => p.status === "pending").length,
            void: list.filter(p => p.status === "void").length};
  }

  // ---------- KPI tiles ----------
  function tiles(bets, winners) {
    const s = summarize(bets), w = winners.filter(graded);
    const wAcc = w.length ? w.filter(p => p.status === "won").length / w.length : null;
    const vegas = w.filter(p => p.vegasFavWon != null), vAcc = vegas.length ? vegas.filter(p => p.vegasFavWon).length / vegas.length : null;
    const tile = (k, v, d, cls = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="d">${d}</div></div>`;
    return `<div class="record">
      ${tile("Bets graded", s.n, `${s.pending} pending · ${s.void} void`)}
      ${tile("Hit rate", pct(s.hit), `model expected ${pct(s.expected)}`)}
      ${tile("P&L ($1 contracts)", money(s.n ? s.pnl : null), "after Kalshi fees", s.pnl > 0 ? "pos" : s.pnl < 0 ? "neg" : "")}
      ${tile("ROI", s.roi == null ? "–" : (s.roi * 100).toFixed(1) + "%", "profit ÷ amount staked")}
      ${tile("Winner picks", pct(wAcc), `n=${w.length} · Vegas favourite ${pct(vAcc)}`)}
    </div>`;
  }

  // ---------- cumulative P&L chart (SVG, one y-axis) ----------
  function chart(bets) {
    const g = bets.filter(graded);
    const dates = [...new Set(g.map(p => p.date))].sort();
    if (dates.length < 1) return `<div class="empty">The P&L chart appears once the first picks are graded (each morning, after the night's games are final).</div>`;
    const series = SERIES.map((s, i) => {
      let cum = 0;
      const pts = dates.map(d => { cum += g.filter(p => p.kind === s.key && p.date === d).reduce((a, p) => a + p.pnl, 0); return cum; });
      return {...s, i, pts, used: g.some(p => p.kind === s.key)};
    }).filter(s => s.used);
    const W = 780, H = 260, L = 48, R = 165, T = 14, B = 28;
    const all = series.flatMap(s => s.pts).concat([0]);
    let lo = Math.min(...all), hi = Math.max(...all);
    if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
    const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
    const x = i => L + (dates.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (dates.length - 1));
    const y = v => T + (hi - v) * (H - T - B) / (hi - lo);
    const step = niceStep((hi - lo) / 4);
    const ticks = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(+v.toFixed(2));
    const grid = ticks.map(v => `<line class="grid${v === 0 ? " zero" : ""}" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>
      <text class="tick" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v === 0 ? "$0" : money(v).replace(".00", "")}</text>`).join("");
    const every = Math.ceil(dates.length / 8);
    const xt = dates.map((d, i) => i % every === 0 || i === dates.length - 1
      ? `<text class="tick" x="${x(i)}" y="${H - 8}" text-anchor="middle">${shortDate(d)}</text>` : "").join("");
    // end labels, nudged apart so they never collide
    const ends = series.map(s => ({s, y: y(s.pts[s.pts.length - 1])})).sort((a, b) => a.y - b.y);
    for (let k = 1; k < ends.length; k++) if (ends[k].y - ends[k - 1].y < 14) ends[k].y = ends[k - 1].y + 14;
    const lines = series.map(s => {
      const d = s.pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
      const dot = dates.length === 1 ? `<circle cx="${x(0)}" cy="${y(s.pts[0])}" r="4.5" class="s${s.i + 1} fill"/>` : "";
      return `<path d="${d}" class="line s${s.i + 1}"/>${dot}`;
    }).join("");
    const labels = ends.map(e => `<text class="end" x="${W - R + 8}" y="${e.y + 4}">${esc(e.s.label)} ${money(e.s.pts[e.s.pts.length - 1])}</text>`).join("");
    const legend = series.map(s => `<span><i class="sw s${s.i + 1}"></i>${esc(s.label)}</span>`).join("");
    state.chartData = {dates, series, x, y, L, R, W, H, T, B};
    return `<div class="chart-head"><b>Cumulative P&L</b> <span class="muted">· $1 per pick, after fees</span><div class="legend2">${legend}</div></div>
      <div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" class="pnl" role="img" aria-label="Cumulative profit and loss by day">
        ${grid}${xt}${lines}${labels}
        <line class="cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>
        <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/>
      </svg><div class="tip" hidden></div></div>`;
  }
  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const m = raw / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function wireChart(root) {
    const c = state.chartData, svg = root.querySelector("svg.pnl");
    if (!c || !svg) return;
    const tip = root.querySelector(".chart-wrap .tip"), cross = svg.querySelector(".cross");
    svg.querySelector(".hit").addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * c.W / r.width;
      let i = 0, best = Infinity;
      c.dates.forEach((_, k) => { const dx = Math.abs(c.x(k) - px); if (dx < best) { best = dx; i = k; } });
      cross.setAttribute("x1", c.x(i)); cross.setAttribute("x2", c.x(i)); cross.setAttribute("visibility", "visible");
      tip.innerHTML = `<b>${shortDate(c.dates[i])}</b>` + c.series.map(s =>
        `<div><i class="sw s${s.i + 1}"></i>${esc(s.label)} <b>${money(s.pts[i])}</b></div>`).join("");
      tip.hidden = false;
      const left = (c.x(i) / c.W) * r.width;
      tip.style.left = Math.min(left + 12, r.width - 190) + "px";
    });
    svg.querySelector(".hit").addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  }

  // ---------- breakdown tables ----------
  function breakdown(title, list, keyFn, order) {
    const groups = {};
    for (const p of list) { const k = keyFn(p); if (k == null) continue; (groups[k] = groups[k] || []).push(p); }
    const keys = order ? order.filter(k => groups[k]) : Object.keys(groups).sort();
    if (!keys.length) return "";
    const rows = keys.map(k => {
      const s = summarize(groups[k]);
      return `<tr><td>${esc(k)}</td><td>${s.n}</td><td>${pct(s.hit)}</td><td>${pct(s.expected)}</td>
        <td class="${s.pnl > 0 ? "pos" : s.pnl < 0 ? "neg" : ""}">${s.n ? money(s.pnl) : "–"}</td></tr>`;
    }).join("");
    return `<div class="card bd"><h3>${esc(title)}</h3><table class="bdt"><tr><th></th><th>Graded</th><th>Hit</th><th>Expected</th><th>P&L</th></tr>${rows}</table></div>`;
  }
  const edgeBucket = p => p.edge == null ? null : p.edge < 0.05 ? "2–5¢" : p.edge < 0.08 ? "5–8¢" : "8¢+";
  const probBucket = p => p.p < 0.4 ? "< 40%" : p.p < 0.5 ? "40–50%" : p.p < 0.6 ? "50–60%" : p.p < 0.7 ? "60–70%" : "70%+";

  function winnerTable(winners) {
    const w = winners.filter(p => p.briers);
    if (!w.length) return "";
    const src = [["Model (final)", "model"], ["Vegas (no-vig)", "vegas"], ["Kalshi", "kalshi"], ["Stats only", "stats"]];
    const rows = src.map(([label, k]) => {
      const pts = w.filter(p => p.briers[k] != null);
      if (!pts.length) return "";
      const acc = pts.filter(p => (p.briers[k] >= 0.5) === p.homeWon).length / pts.length;
      const brier = pts.reduce((s, p) => s + (p.briers[k] - (p.homeWon ? 1 : 0)) ** 2, 0) / pts.length;
      return `<tr><td>${label}</td><td>${pts.length}</td><td>${pct(acc)}</td><td>${brier.toFixed(3)}</td></tr>`;
    }).join("");
    return `<div class="card bd"><h3>Game winners: who called it best</h3><table class="bdt"><tr><th></th><th>Games</th><th>Picked winner</th><th>Brier ↓</th></tr>${rows}</table>
      <div class="muted small">Brier = average squared error of the probability; lower is better calibrated.</div></div>`;
  }

  // ---------- pick log ----------
  function log(list) {
    const shown = list.slice().sort((a, b) => b.date.localeCompare(a.date)).slice(0, state.logLimit);
    const kindLabel = {"winner": "Winner pick", "game": "Game bet", "props-value": "Prop · value", "props-lean": "Prop · lean"};
    const rows = shown.map(p => `<tr>
      <td>${shortDate(p.date)}</td><td>${esc(p.game)}</td><td class="lbl">${esc(p.label)}</td><td>${kindLabel[p.kind]}</td>
      <td>${pct(p.p)}</td><td>${p.price == null ? "–" : p.price.toFixed(2)}</td>
      <td>${p.edge == null ? "–" : "+" + (p.edge * 100).toFixed(1) + "¢"}</td>
      <td><span class="res ${p.status}">${p.status}</span></td>
      <td class="${p.pnl > 0 ? "pos" : p.pnl < 0 ? "neg" : ""}">${p.pnl == null ? "–" : money(p.pnl)}</td></tr>`).join("");
    const more = list.length > state.logLimit ? `<button class="more" data-more>Show more (${list.length - state.logLimit} older)</button>` : "";
    return `<div class="card"><h3>Pick log <span class="muted">· ${list.length} picks</span></h3>
      <div class="log-wrap"><table class="log"><tr><th>Date</th><th>Game</th><th>Pick</th><th>Type</th><th>Model</th><th>Price</th><th>Edge</th><th>Result</th><th>P&L</th></tr>${rows ||
        '<tr><td colspan="9" class="muted">No picks yet.</td></tr>'}</table></div>${more}</div>`;
  }

  // ---------- render ----------
  function filters() {
    const seg = (name, opts) => `<div class="seg" data-filter="${name}">${opts.map(([v, l]) =>
      `<button data-v="${v}" class="${state[name] === v ? "on" : ""}">${l}</button>`).join("")}</div>`;
    return `<div class="filters">
      ${seg("period", [["7", "7 days"], ["30", "30 days"], ["all", "Season"]])}
      ${seg("kind", [["all", "All bets"], ["props-value", "Props · value"], ["props-lean", "Props · lean"], ["game", "Game bets"]])}
    </div>`;
  }

  function render() {
    const root = document.getElementById("tab-track");
    const scoped = state.picks.filter(inPeriod);
    const winners = scoped.filter(p => p.kind === "winner");
    const bets = scoped.filter(isBet).filter(p => state.kind === "all" || p.kind === state.kind);
    const props = bets.filter(p => p.kind.startsWith("props"));
    root.innerHTML = filters() + tiles(bets, winners) +
      `<div class="card chart-card">${chart(bets)}</div>` +
      `<div class="bd-grid">
        ${breakdown("Props by stat", props, p => STAT[p.stat], Object.values(STAT))}
        ${breakdown("Props: over vs under", props, p => p.side === "yes" ? "Over (YES)" : "Under (NO)", ["Over (YES)", "Under (NO)"])}
        ${breakdown("By edge at pick time", bets, edgeBucket, ["2–5¢", "5–8¢", "8¢+"])}
        ${breakdown("Calibration: model probability vs hit rate", bets, probBucket, ["< 40%", "40–50%", "50–60%", "60–70%", "70%+"])}
        ${winnerTable(winners)}
      </div>` +
      log(scoped.filter(p => state.kind === "all" || p.kind === state.kind || (state.kind === "all" && p.kind === "winner")));
    wireChart(root);
    root.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
      state[b.parentElement.dataset.filter] = b.dataset.v; state.logLimit = 50; render();
    }));
    const m = root.querySelector("[data-more]");
    if (m) m.addEventListener("click", () => { state.logLimit += 100; render(); });
  }

  // base: "data/" (NBA) or "data/nfl/" (NFL); both hold the same history.json layout.
  async function load(base = "data/", sport = "nba") {
    const token = state.token = (state.token || 0) + 1;
    let picks = [];
    try {
      const history = await (await fetch(base + "history.json?t=" + Date.now())).json();
      picks = flatten(history);
    } catch (e) {
      picks = [];
    }
    if (token !== state.token) return;  // a newer load (other league) won
    STAT = STATS[sport] || STATS.nba;
    state.picks = picks;
    render();
  }

  window.TrackTab = {load};
})();
