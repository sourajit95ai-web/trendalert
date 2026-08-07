/* TrendAlert — redesign shell (iteration 1).
 *
 * Presentation only. Every rule — grouping, zones, base status, scoring,
 * trend, staleness, sector mix, the daily summary — comes from window.TA
 * (frontend/v2/trendalert-core.js), unchanged. If a number here disagrees with
 * the shipped dashboard, this file is wrong, not the core.
 *
 * Deliberately NOT in this iteration, and tracked in the handoff notes: list
 * navigation, add ticker, manage lists, the settings modal, the detail drawer,
 * charts, and the profit-booked / re-entered marks. Anything that would WRITE
 * to the bucket is absent on purpose — this page is read-only until the rest
 * of the surface is rebuilt, so a reviewer opening it cannot alter what the
 * alerts fire on. It never POSTs, and it never calls TA.post.
 */
(function () {
  const T = window.TA;
  const app = document.getElementById("app");

  /* ---------------- state ---------------- */
  /* view is shared with the shipped dashboard through the same settings key,
     but a browser that has never chosen one lands on the table here — the
     redesign leads with the ledger, not the cards. */
  const storedView = (() => {
    try { return (JSON.parse(T.lsGet(T.keys.SET_KEY)) || {}).view; } catch (e) { return null; }
  })();

  const lists = T.loadLists();
  if (T.migrateLists(lists)) T.saveLists(lists);

  const s = {
    payload: null,
    mode: "demo",
    statusText: "Loading…",
    lists,
    settings: T.loadSettings(),
    theme: T.loadTheme(),
    dataUrl: T.lsGet(T.keys.DATA_KEY) || T.DEFAULT_DATA_URL,
    trendF: "all",
    view: storedView === "cards" ? "cards" : "table",
    sortKey: "trend",
    sortDir: -1,
    toast: "",
  };

  /* Index tape: the universe holds ETFs, not indexes, so each proxy is
     labelled with the index it tracks and keeps its ticker in the tooltip.
     Anything not in this map shows as its own symbol rather than being
     dropped — an index list is the user's to edit. */
  const PROXY = {
    SPY: "S&P 500", VOO: "S&P 500", QQQ: "Nasdaq 100", DIA: "Dow 30",
    IWM: "Russell 2000", IJR: "Small cap", SOXX: "Semis", "BTC/USD": "Bitcoin",
    IAUM: "Gold", IGV: "Software", IHAK: "Cybersecurity", SPGI: "S&P Global",
  };
  /* The tape reads left to right as the market is usually quoted, regardless
     of the order the symbols happen to sit in the Index list. Anything not
     named here queues behind them in list order. */
  const TAPE_ORDER = ["SPY", "VOO", "QQQ", "DIA", "IWM", "SOXX", "BTC/USD"];
  const TAPE_MAX = 4;

  /* ---------------- helpers ---------------- */
  const esc = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const pct = (v, d = 2) => (v >= 0 ? "+" : "−") + T.fmt(Math.abs(v || 0), d) + "%";
  const bySym = (sym) => (s.payload && (s.payload.symbols || []).find(d => d.symbol === sym)) || null;
  const NUMBERS = ["no", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve"];
  const words = (n) => NUMBERS[n] || String(n);
  const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  const listOf = (arr) => arr.length <= 1 ? (arr[0] || "")
    : arr.slice(0, -1).join(", ") + " and " + arr[arr.length - 1];

  /* the book this page reads: the Core portfolio, the same scope the summary
     alert and the sector mix already use. List switching returns with the
     list navigation. */
  const core = () => T.corePortfolio(s.lists);
  const coreRows = () => {
    const c = core();
    if (!c || !s.payload) return [];
    return c.symbols.map(bySym).filter(d => d && !d.pending);
  };

  let toastTimer = null;
  function flash(msg) {
    clearTimeout(toastTimer);
    s.toast = msg;
    render();
    toastTimer = setTimeout(() => { s.toast = ""; render(); }, 4000);
  }

  /* ---------------- data ---------------- */
  async function load() {
    const url = s.dataUrl;
    if (!url) {
      s.payload = T.demoData(s.lists); s.mode = "demo"; s.statusText = "Sample data";
      return render();
    }
    try {
      const r = await fetch(url + (url.includes("?") ? "&" : "?") + "t=" + Date.now());
      if (!r.ok) throw new Error("HTTP " + r.status);
      s.payload = await r.json();
      s.mode = "live"; s.statusText = "Live";
    } catch (e) {
      s.payload = T.demoData(s.lists); s.mode = "error"; s.statusText = "Fetch failed — sample data";
    }
    render();
  }

  /* ---------------- masthead ---------------- */
  function mastVals() {
    const idxSyms = [...new Set(s.lists.filter(l => l.type === "index").flatMap(l => l.symbols))];
    const rank = (sym) => {
      const i = TAPE_ORDER.indexOf(sym);
      return i === -1 ? TAPE_ORDER.length + idxSyms.indexOf(sym) : i;
    };
    /* SPY and VOO both track the S&P; whichever ranks first wins, so the tape
       never quotes the same index twice under two tickers. */
    const seen = new Set();
    const tape = idxSyms.map(bySym).filter(Boolean)
      .sort((a, b) => rank(a.symbol) - rank(b.symbol))
      .filter(d => {
        const label = PROXY[d.symbol] || d.symbol;
        if (seen.has(label)) return false;
        seen.add(label); return true;
      })
      .slice(0, TAPE_MAX).map(d => ({
      name: PROXY[d.symbol] || d.symbol.replace(/\/USD$/, ""),
      sym: d.symbol,
      close: T.fmt(d.close, d.close >= 1000 ? 2 : 2),
      chg: pct(d.change_pct, 1),
      ink: T.dirColor(d.change_pct),
    }));

    const gen = s.payload && s.payload.generated_at;
    const at = gen && gen !== "demo" ? new Date(gen) : null;
    const stamp = at && !isNaN(at) ? at.toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York",
    }) + " ET" : "";
    return {
      tape,
      dot: s.mode === "live" ? T.ink.up : s.mode === "error" ? T.ink.down : T.ink.warn,
      status: s.statusText + (stamp ? " · updated " + stamp : ""),
      stale: s.payload ? T.staleness(s.payload, s.dataUrl) : null,
    };
  }

  /* ---------------- the sentence at the top ---------------- */
  function hero() {
    const rows = coreRows();
    const total = rows.length;
    const bull = rows.filter(d => T.trendOf(d, s.settings) === "bull").length;
    const up = rows.filter(d => (d.change_pct || 0) > 0).length;

    const action = rows.filter(d => T.groupOf(d, s.settings) === 1);
    const reentry = rows.filter(d => T.groupOf(d, s.settings) === 3);

    if (!total) {
      return {
        eyebrow: "No data yet",
        headline: "Waiting on the pipeline.",
        standfirst: "The Core list is empty or the feed has not landed. The shipped dashboard can still edit lists.",
        action, reentry,
      };
    }

    /* lead sector: best average move among sectors holding more than one name,
       so a single flyer cannot "lead" the tape on its own */
    const mix = T.sectorMix(s.lists, s.payload).filter(m => m.count > 1);
    const lead = mix.slice().sort((a, b) => b.move - a.move)[0];
    const ratio = total ? up / total : 0;
    const tone = ratio >= 0.6 ? "firm" : ratio <= 0.4 ? "heavy" : "mixed";

    let decision;
    if (action.length && reentry.length) {
      decision = `${words(action.length)} in the exit review zone, ${words(reentry.length)} on re-entry watch.`;
    } else if (action.length) {
      decision = `${words(action.length)} ${action.length === 1 ? "name has" : "names have"} reached the exit review zone.`;
    } else if (reentry.length) {
      decision = `${words(reentry.length)} ${reentry.length === 1 ? "name is" : "names are"} on re-entry watch.`;
    } else {
      decision = "nothing needs a decision today.";
    }
    const headline = lead
      ? `${lead.name} leads a ${tone} tape — ${decision}`
      : cap(decision);

    /* standfirst: name the actual names, in the same order the cards below
       will show them */
    const byScore = (a, b) => T.scoreOf(b, s.settings) - T.scoreOf(a, s.settings);
    const parts = [];
    if (action.length) {
      const syms = action.slice().sort(byScore).slice(0, 3).map(d => d.symbol);
      parts.push(`${listOf(syms)} ${syms.length === 1 ? "sits" : "sit"} inside the ${T.fmt(s.settings.highZonePct, 0)}% high zone.`);
    }
    const confirmed = reentry.filter(d => d.base_status === "confirmed").map(d => d.symbol);
    if (confirmed.length) {
      parts.push(`${listOf(confirmed.slice(0, 3))} ${confirmed.length === 1 ? "has a confirmed base" : "have confirmed bases"} and ${confirmed.length === 1 ? "is" : "are"} worth a look for re-entry.`);
    } else if (reentry.length) {
      parts.push(`${words(reentry.length)} ${reentry.length === 1 ? "name sits" : "names sit"} near the 52-week low without a confirmed base — falling-knife risk until one forms.`);
    }
    if (!parts.length) parts.push("Every Core holding is between its zones — the rules are armed but silent.");

    return {
      eyebrow: `${bull} of ${total} symbols in uptrend`,
      headline, standfirst: cap(parts.join(" ")), action, reentry,
    };
  }

  /* ---------------- cards ---------------- */
  function cardHTML(d, kind) {
    const score = T.scoreOf(d, s.settings);
    const tag = T.zoneTag(d, s.settings);
    const act = T.action(d, s.settings);
    return `
      <button class="card" data-sym="${esc(d.symbol)}">
        <div class="card-top">
          <div>
            <div class="card-sym">${esc(d.symbol)}</div>
            <div class="card-sector">${esc(d.sector || "—")}</div>
          </div>
          <div class="card-score">
            <b class="num" style="color:${T.scoreColor(score)}">${T.fmt(score, 0)}</b>
            <span>score</span>
          </div>
        </div>
        <div class="card-price">
          <span class="px num">${esc(T.fmt(d.close))}</span>
          <span class="chg num" style="color:${T.dirColor(d.change_pct)}">${esc(pct(d.change_pct, 1))}</span>
          ${tag ? `<span class="tag" style="color:${tag.ink};border-color:${tag.border}">${esc(tag.text)}</span>` : ""}
        </div>
        <p class="why">${esc(T.why(d, s.settings))}</p>
        ${kind === "action" && act ? `<div class="do"><i>↳</i><span style="color:${act.ink}">${esc(act.text)}</span></div>` : ""}
      </button>`;
  }

  function miniHTML(d) {
    const score = T.scoreOf(d, s.settings);
    return `
      <button class="card mini" data-sym="${esc(d.symbol)}">
        <div class="card-top">
          <div>
            <div class="card-sym">${esc(d.symbol)}</div>
            <div class="card-sector">${esc(d.sector || "—")}</div>
          </div>
          <div class="card-score">
            <b class="num" style="color:${T.scoreColor(score)}">${T.fmt(score, 0)}</b>
            <span>score</span>
          </div>
        </div>
        <div class="card-price">
          <span class="px num">${esc(T.fmt(d.close))}</span>
          <span class="chg num" style="color:${T.dirColor(d.change_pct)}">${esc(pct(d.change_pct, 1))}</span>
        </div>
      </button>`;
  }

  /* ---------------- all trends ---------------- */
  const COLS = [
    { key: "symbol", lbl: "Symbol", l: true },
    { key: "sector", lbl: "Sector", l: true, hide: true },
    { key: "score", lbl: "Score" },
    { key: "close", lbl: "Close" },
    { key: "change_pct", lbl: "Change" },
    { key: "trend", lbl: "Trend" },
  ];
  const TREND_GLYPH = { bull: "▲", bear: "▼", mixed: "◆" };
  /* The filter chips say Bullish / Bearish because they name a set; the column
     says Uptrend / Downtrend because it describes one name's state. Same
     TA.trendOf underneath — only the wording differs. */
  const TREND_STATE = { bull: "Uptrend", bear: "Downtrend", mixed: "Mixed" };
  const trendInk = (t) => t === "bull" ? T.ink.up : t === "bear" ? T.ink.down : T.ink.muted;

  function trendRows() {
    const rows = coreRows().slice();
    const filtered = s.trendF === "all" ? rows
      : rows.filter(d => T.trendOf(d, s.settings) === s.trendF);
    const dir = s.sortDir;
    return filtered.sort((a, b) => {
      if (s.sortKey === "symbol") return a.symbol.localeCompare(b.symbol) * dir;
      if (s.sortKey === "sector") return String(a.sector || "").localeCompare(String(b.sector || "")) * dir;
      let av, bv;
      if (s.sortKey === "trend") { av = T.trendRank(T.trendOf(a, s.settings)); bv = T.trendRank(T.trendOf(b, s.settings)); }
      else if (s.sortKey === "score") { av = T.scoreOf(a, s.settings); bv = T.scoreOf(b, s.settings); }
      else { av = a[s.sortKey]; bv = b[s.sortKey]; }
      return ((av == null ? -1e9 : av) - (bv == null ? -1e9 : bv)) * dir;
    });
  }

  function tableHTML(rows) {
    if (!rows.length) return `<p class="empty">No Core holding matches this filter.</p>`;
    const head = COLS.map(c => {
      const on = s.sortKey === c.key;
      const arrow = on ? (s.sortDir < 0 ? " ↓" : " ↑") : "";
      return `<th class="${c.l ? "l" : ""}${c.hide ? " hide-s" : ""}" data-sorted="${on}">
        <button data-sort="${c.key}">${esc(c.lbl)}${arrow}</button></th>`;
    }).join("");
    const body = rows.map(d => {
      const score = T.scoreOf(d, s.settings), tr = T.trendOf(d, s.settings);
      return `<tr data-sym="${esc(d.symbol)}">
        <td class="sym">${esc(d.symbol)}</td>
        <td class="sector hide-s" style="text-align:left">${esc(d.sector || "—")}</td>
        <td class="num" style="color:${T.scoreColor(score)}">${T.fmt(score, 0)}</td>
        <td class="num">${esc(T.fmt(d.close))}</td>
        <td class="num" style="color:${T.dirColor(d.change_pct)}">${esc(pct(d.change_pct, 1))}</td>
        <td><span class="trend" style="color:${trendInk(tr)}">${TREND_GLYPH[tr]} ${esc(TREND_STATE[tr])}</span></td>
      </tr>`;
    }).join("");
    return `<table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  /* ---------------- rail ---------------- */
  function railHTML() {
    const rows = coreRows();
    const withChg = rows.filter(d => d.change_pct != null);
    const best = withChg.slice().sort((a, b) => b.change_pct - a.change_pct)[0];
    const worst = withChg.slice().sort((a, b) => a.change_pct - b.change_pct)[0];
    const up = withChg.filter(d => d.change_pct > 0).length;

    const summary = `
      <div>
        <div class="rail-label">Session summary</div>
        <div class="panel">
          <div class="kv"><span class="k">Top up</span><span class="v num">${best ? esc(best.symbol) + " " + esc(pct(best.change_pct, 1)) : "—"}</span></div>
          <div class="kv"><span class="k">Top down</span><span class="v num">${worst ? esc(worst.symbol) + " " + esc(pct(worst.change_pct, 1)) : "—"}</span></div>
          <div class="kv"><span class="k">Breadth</span><span class="v num">${withChg.length ? up + " / " + withChg.length + " up" : "—"}</span></div>
        </div>
      </div>`;

    /* data.json carries only crosses detected on the LATEST bar and keeps no
       history, so there is nothing to age these by — every row is today's.
       An alert history file is a backend change, noted in the handoff. */
    const al = (s.payload && s.payload.alerts) || [];
    const alerts = `
      <div>
        <div class="rail-label">Recent alerts</div>
        ${al.length ? al.map(a => `
          <button class="alert-row" data-sym="${esc(a.symbol)}">
            <span class="arrow" style="color:${a.dir === "bull" ? T.ink.up : T.ink.down}">${a.dir === "bull" ? "↑" : "↓"}</span>
            <span class="txt"><b>${esc(a.symbol)}</b> ${esc(a.type.replace(/^EMA /, "EMA "))}${a.detail ? " — " + esc(a.detail.toLowerCase()) : ""}</span>
            <span class="age">today</span>
          </button>`).join("")
        : `<p class="rail-note">No EMA 50 / 150 crosses on the latest bar.</p>`}
      </div>`;

    const mix = s.payload ? T.sectorMix(s.lists, s.payload) : [];
    const sectors = `
      <div>
        <div class="rail-label">Portfolio sectors</div>
        ${mix.length ? mix.map(m => `
          <div class="sect-row" title="${esc(m.count)} held">
            <div class="sect-top">
              <span>${esc(m.name)}</span>
              <span class="num"><span style="color:var(--color-neutral-500)">${esc(m.pctLabel)}</span>
                <span class="sect-move" style="color:${m.moveInk}"> · ${esc(m.moveLabel)}</span></span>
            </div>
            <div class="sect-bar"><i style="width:${m.pct.toFixed(1)}%;background:${m.barColor}"></i></div>
          </div>`).join("")
        : `<p class="rail-note">No portfolio holdings with data yet.</p>`}
      </div>`;

    const w = s.settings.weights;
    const note = `<p class="rail-note">Signal score is a 0–100 cross-sectional rank
      (trend ${w.trend} · momentum ${w.momentum} · volume ${w.participation} ·
      relative strength ${w.relStrength} · risk ${w.risk}). A scanning aid, not investment
      advice. Lists are saved in this browser.</p>`;

    return summary + alerts + sectors + note;
  }

  /* ---------------- render ---------------- */
  function render() {
    document.documentElement.dataset.theme = s.theme;
    const m = mastVals();
    const h = hero();
    const byScore = (a, b) => T.scoreOf(b, s.settings) - T.scoreOf(a, s.settings);
    const action = h.action.slice().sort(byScore);
    const reentry = h.reentry.slice().sort(byScore);
    const rows = trendRows();

    const seg = (opts, cur, attr) => `<div class="seg">${opts.map(o =>
      `<button ${attr}="${o[0]}" aria-pressed="${cur === o[0]}">${esc(o[1])}</button>`).join("")}</div>`;

    app.innerHTML = `
      <header class="mast">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M3 13h4l3 7 4-16 3 9h4"/></svg>
          </span>
          <span class="brand-name">TrendAlert</span>
        </div>
        <span class="chip-scope">US market</span>
        <div class="tape">
          ${m.tape.map(t => `<span class="tape-item" title="${esc(t.sym)}">
            <span class="tape-name">${esc(t.name)}</span>
            <span class="tape-val num">${esc(t.close)}</span>
            <span class="tape-chg num" style="color:${t.ink}">${esc(t.chg)}</span>
          </span>`).join("")}
        </div>
        <div class="mast-right">
          ${m.stale ? `<span class="stale" style="color:${m.stale.level === "bad" ? T.ink.down : T.ink.warn}">${esc(m.stale.text)}</span>` : ""}
          <span class="status"><i class="status-dot" style="background:${m.dot}"></i>${esc(m.status)}</span>
          <button class="gear" data-act="settings">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.2.61.77 1.03 1.42 1.03H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            Settings
          </button>
        </div>
      </header>

      <div class="page">
        <div class="col">
          <p class="eyebrow">${esc(h.eyebrow)}</p>
          <h1 class="headline">${esc(h.headline)}</h1>
          <p class="standfirst">${esc(h.standfirst)}</p>

          ${action.length ? `
            <section class="sec">
              <div class="sec-head"><h2 class="sec-title">Action required</h2><span class="count">${action.length}</span></div>
              <div class="grid-2">${action.map(d => cardHTML(d, "action")).join("")}</div>
            </section>` : ""}

          ${reentry.length ? `
            <section class="sec">
              <div class="sec-head"><h2 class="sec-title">Re-entry watch</h2><span class="count">${reentry.length}</span></div>
              <div class="grid-3">${reentry.map(d => cardHTML(d, "reentry")).join("")}</div>
            </section>` : ""}

          <section class="sec">
            <div class="sec-head">
              <h2 class="sec-title">All trends</h2>
              <div class="sec-tools">
                ${seg([["all", "All"], ["bull", "Bullish"], ["bear", "Bearish"], ["mixed", "Mixed"]], s.trendF, "data-trend")}
                <div class="seg icons">
                  <button data-view="cards" aria-pressed="${s.view === "cards"}" title="Cards">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>
                      <rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
                  </button>
                  <button data-view="table" aria-pressed="${s.view === "table"}" title="Table">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18"/></svg>
                  </button>
                </div>
              </div>
            </div>
            ${s.view === "table" ? tableHTML(rows)
              : (rows.length ? `<div class="grid-3">${rows.map(miniHTML).join("")}</div>`
                : `<p class="empty">No Core holding matches this filter.</p>`)}
          </section>
        </div>

        <aside class="col rail">${railHTML()}</aside>
      </div>

      <footer class="foot">
        Symbols can belong to several portfolios, watchlists and indexes.
        ${s.mode === "live"
          ? "Showing the live pipeline feed."
          : "Sample data shown — the live feed did not load."}
      </footer>
      ${s.toast ? `<div class="toast">${esc(s.toast)}</div>` : ""}`;
  }

  /* ---------------- events ---------------- */
  /* One delegated listener: render() replaces the whole tree, so per-node
     handlers would have to be re-bound on every paint. */
  app.addEventListener("click", (e) => {
    const trend = e.target.closest("[data-trend]");
    if (trend) { s.trendF = trend.dataset.trend; return render(); }

    const view = e.target.closest("[data-view]");
    if (view) {
      s.view = view.dataset.view;
      const settings = { ...s.settings, view: s.view };
      T.saveSettings(settings); s.settings = settings;
      return render();
    }

    const sort = e.target.closest("[data-sort]");
    if (sort) {
      const k = sort.dataset.sort;
      if (s.sortKey === k) s.sortDir *= -1;
      else { s.sortKey = k; s.sortDir = (k === "symbol" || k === "sector") ? 1 : -1; }
      return render();
    }

    if (e.target.closest('[data-act="settings"]')) {
      return flash("Settings, lists and charts are still on the current dashboard — they land in the next iterations.");
    }

    const sym = e.target.closest("[data-sym]");
    if (sym) return flash(sym.dataset.sym + " — the detail drawer and chart land in a later iteration.");
  });

  /* ---------------- boot ---------------- */
  render();
  load();
  setInterval(load, 300000);
  /* the staleness pill is time-based, so the page has to repaint without new
     data for it to ever appear */
  setInterval(render, 60000);
})();
