/* TrendAlert — pure logic core. No DOM, no markup.
   Ported verbatim from the production dashboard so behaviour is identical:
   same localStorage keys, same scoring, same zone/base/booking rules, same
   indicator math, same demo fallback. The DC owns all presentation. */
(function () {
  const TA = {};

  /* ---------- formatting ---------- */
  const fmt = TA.fmt = (n, d = 2) =>
    (n == null || isNaN(n)) ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  TA.signed = n => (n >= 0 ? "+" : "") + fmt(n);
  const clamp01 = x => Math.max(0, Math.min(1, x));

  /* ---------- storage keys (unchanged from production) ---------- */
  const LS_KEY = "trendalert_lists_v1";
  const DATA_KEY = "trendalert_data_url";
  const SET_KEY = "trendalert_settings_v1";
  const SYNC_KEY = "trendalert_sync_url";
  const PBRE_KEY = "trendalert_pbre_v1";
  const CH_KEY = "trendalert_chartcfg_v1";
  const GROUPS_KEY = "trendalert_groups_v1";
  const TOKEN_KEY = "trendalert_admin_token_v1";
  TA.keys = { LS_KEY, DATA_KEY, SET_KEY, SYNC_KEY, PBRE_KEY, CH_KEY, GROUPS_KEY, TOKEN_KEY };

  const DEFAULT_DATA_URL = "https://storage.googleapis.com/trendalert-data-rattle/data.json";
  const DEFAULT_SYNC_URL = "https://us-central1-trendalert-prod.cloudfunctions.net/notes";
  TA.DEFAULT_DATA_URL = DEFAULT_DATA_URL;
  TA.DEFAULT_SYNC_URL = DEFAULT_SYNC_URL;

  const CORE_SYMBOLS = ["META", "MELI", "MSFT", "GOOGL", "AMZN", "MA", "NFLX", "NVDA", "AMD", "NOW",
    "V", "UNH", "COIN", "VEEV", "HOOD", "TSM", "CRWD", "PANW", "AXON", "LLY",
    "TMO", "NBIS", "AVGO", "ISRG", "PLTR", "RKLB", "IONQ", "NVO"];
  const INDEX_SYMBOLS = ["QQQ", "SOXX", "SPY", "IWM", "BTC/USD", "IAUM", "IJR", "SPGI", "IHAK", "IGV", "VOO"];
  TA.CORE_SYMBOLS = CORE_SYMBOLS; TA.INDEX_SYMBOLS = INDEX_SYMBOLS;

  TA.TYPE_LABEL = { portfolio: "Portfolio", watchlist: "Watchlist", index: "Index" };
  TA.SECTORS = ["all", "Technology", "Finance", "Healthcare", "Energy", "Consumer", "Industrial", "Index", "Crypto"];
  TA.COLS = [
    { key: "symbol", lbl: "Symbol" }, { key: "score", lbl: "Score" }, { key: "close", lbl: "Close" },
    { key: "change_pct", lbl: "Chg%" }, { key: "pos52", lbl: "Off 52w high" }, { key: "trend", lbl: "Trend" },
  ];

  const uid = TA.uid = () => Math.random().toString(36).slice(2, 9);
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { } };
  TA.lsGet = lsGet; TA.lsSet = lsSet;

  /* ---------- lists ---------- */
  TA.defaultLists = () => [
    { id: uid(), name: "Core", type: "portfolio", symbols: CORE_SYMBOLS.slice() },
    { id: uid(), name: "Watchlist", type: "watchlist", symbols: ["NVDA", "CAT", "UNH", "JPM", "XOM"] },
    { id: uid(), name: "Index", type: "index", symbols: INDEX_SYMBOLS.slice() },
  ];
  TA.loadLists = () => {
    try { const r = lsGet(LS_KEY); if (r) return JSON.parse(r); } catch (e) { }
    return TA.defaultLists();
  };
  /* the production migrations, run once each, so an existing browser lands in
     exactly the state the old dashboard left it in */
  TA.migrateLists = (lists) => {
    let changed = false;
    if (!lists.some(l => l.type === "index")) {
      lists.push({ id: uid(), name: "Index", type: "index", symbols: ["QQQ", "SOXX", "SPY", "IWM", "BTC/USD"] });
      changed = true;
    }
    if (!lsGet("trendalert_core_v2")) {
      const core = lists.find(l => l.type === "portfolio" && l.name === "Core") || lists.find(l => l.type === "portfolio");
      if (core) { core.symbols = CORE_SYMBOLS.slice(); changed = true; }
      lsSet("trendalert_core_v2", "1");
    }
    if (!lsGet("trendalert_index_v2")) {
      const idx = lists.find(l => l.type === "index");
      if (idx) { INDEX_SYMBOLS.forEach(s => { if (!idx.symbols.includes(s)) { idx.symbols.push(s); changed = true; } }); }
      lsSet("trendalert_index_v2", "1");
    }
    if (!lsGet("trendalert_spy_index")) {
      const idx = lists.find(l => l.type === "index");
      if (idx && !idx.symbols.includes("SPY")) { idx.symbols.push("SPY"); changed = true; }
      lsSet("trendalert_spy_index", "1");
    }
    return changed;
  };
  TA.saveLists = (lists) => lsSet(LS_KEY, JSON.stringify(lists));
  TA.corePortfolio = (lists) => lists.find(l => l.type === "portfolio" && l.name === "Core")
    || lists.find(l => l.type === "portfolio") || null;

  /* ---------- settings ---------- */
  /* Every alert the backend sends, in the order the trading day fires them.
     Mirrors ALERT_KINDS in alerts_email.py — the keys are the contract. The
     bloodbath alarm and the end-of-day rule signals were retired, so they are
     absent here AND refused server-side (RETIRED_ALERTS). */
  const ALERT_KINDS = [
    { key: "summary_premkt", label: "Pre-market summary", when: "8:35 ET · the chart" },
    { key: "brief", label: "Morning brief", when: "9:50 ET" },
    { key: "summary_close", label: "Market-close summary", when: "16:50 ET · the chart" },
  ];
  TA.ALERT_KINDS = ALERT_KINDS;
  const ALL_ALERTS = () => Object.fromEntries(ALERT_KINDS.map(a => [a.key, true]));
  const SETTINGS_VERSION = 3;   /* bump to migrate stored settings; see loadSettings */
  /* trendFast/trendSlow default to EMA 50 / EMA 150 — the SAME pair main.py
     calls a golden or death cross. One trend definition across the product:
     what the table calls an uptrend is what the alerts call a golden cross. */
  /* captionText: the summary alert's written signals under the poster. OFF by
     default — the poster already says it, so the text is opt-in (and the
     backend's caption_text_on agrees: absent means off, unlike the alert
     switches, which fail open). alertEmails: extra recipients on top of the
     configured inbox, capped and re-validated server-side. */
  const MAX_EXTRA_EMAILS = 5;
  TA.MAX_EXTRA_EMAILS = MAX_EXTRA_EMAILS;
  /* Mirrors clean_emails in alerts_email.py — deliberately loose, its job is
     to refuse anything that could read as a second recipient or header. */
  TA.cleanEmails = (v) => {
    const out = [];
    const list = Array.isArray(v) ? v : String(v == null ? "" : v).split(/[,\s]+/);
    for (const raw of list) {
      const a = String(raw == null ? "" : raw).trim().toLowerCase();
      const at = a.lastIndexOf("@");
      const dom = at > 0 ? a.slice(at + 1) : "";
      if (a && a.length <= 254 && (a.match(/@/g) || []).length === 1 && dom.includes(".")
        && !dom.startsWith(".") && !/[\s,;<>"]/.test(a) && !out.includes(a)) out.push(a);
      if (out.length >= MAX_EXTRA_EMAILS) break;
    }
    return out;
  };
  const DEFAULT_SETTINGS = {
    gainPct: 20, highZonePct: 2, lowZonePct: 10, horizon: "long", reEntryMode: "base", view: "cards",
    trendFast: 50, trendSlow: 150, alertChannel: "both", alertTypes: ALL_ALERTS(),
    captionText: false, alertEmails: [],
    sv: SETTINGS_VERSION,
    weights: { trend: 30, momentum: 20, participation: 20, relStrength: 20, risk: 10 }
  };
  TA.DEFAULT_SETTINGS = DEFAULT_SETTINGS;
  TA.HORIZON_PRESETS = { long: { gain: 20, high: 2, low: 10, span: "6M" }, swing: { gain: 10, high: 3, low: 8, span: "3M" } };
  TA.loadSettings = () => {
    try {
      const v = JSON.parse(lsGet(SET_KEY));
      if (!v) return JSON.parse(JSON.stringify(DEFAULT_SETTINGS));
      const s = {
        ...DEFAULT_SETTINGS, ...v,
        alertTypes: { ...ALL_ALERTS(), ...(v.alertTypes || {}) },
        weights: { ...DEFAULT_SETTINGS.weights, ...(v.weights || {}) }
      };
      /* v2 -> v3: the trend pair briefly defaulted to EMA 50 / EMA 200 before
         being put back to 50/150 for consistency with the cross alerts.
         Settings live only in localStorage, so a browser that loaded the v2
         build has 200 stored and would keep it silently. Only the untouched v2
         default is moved back — a pair chosen by hand is left alone. */
      if (v.sv === 2 && +v.trendFast === 50 && +v.trendSlow === 200) s.trendSlow = 150;
      s.sv = SETTINGS_VERSION;
      return s;
    } catch (e) { return JSON.parse(JSON.stringify(DEFAULT_SETTINGS)); }
  };
  TA.saveSettings = (s) => lsSet(SET_KEY, JSON.stringify(s));

  TA.loadPbre = () => { try { return JSON.parse(lsGet(PBRE_KEY)) || {}; } catch (e) { return {}; } };
  TA.savePbre = (p) => lsSet(PBRE_KEY, JSON.stringify(p));
  TA.loadGroups = () => {
    const d = { g0: true, g1: true, g3: true, g2: false };
    try { return Object.assign(d, JSON.parse(lsGet(GROUPS_KEY)) || {}); } catch (e) { return d; }
  };
  TA.saveGroups = (g) => lsSet(GROUPS_KEY, JSON.stringify(g));

  /* ---------- sync (fire and forget, same endpoints) ---------- */
  /* The write token. Held only in this browser's localStorage and sent as a
     header on every POST; the backend is what actually enforces it, so a
     viewer clearing this check in devtools still cannot write. Without a
     token the dashboard is read-only: edits stay in the viewer's own
     localStorage and never reach the bucket. */
  TA.loadToken = () => lsGet(TOKEN_KEY) || "";
  TA.saveToken = (t) => lsSet(TOKEN_KEY, String(t || "").trim());
  TA.canWrite = () => !!TA.loadToken();

  TA.post = (syncUrl, kind, data) => {
    if (!syncUrl) return;
    const token = TA.loadToken();
    /* Skip rather than fire-and-fail. publish() runs 3s after ANY page load,
       so without this every viewer would pile up 401s in their console on
       every visit and make a read-only dashboard look broken. */
    if (!token) return;
    fetch(syncUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-TA-Token": token },
      body: JSON.stringify({ kind, data })
    }).catch(() => { });
  };
  /* Bars live one object per symbol so a page load fetches none of them.
     Must match chart_backend.chart_object_name exactly -- BTC/USD would
     otherwise become a nested path the URL cannot address. */
  TA.chartObject = (sym) => "chart/" + String(sym).replace(/\//g, "-") + ".json";

  TA.pull = async (syncUrl, kind) => {
    if (!syncUrl) return null;
    try {
      const r = await fetch(syncUrl + "?kind=" + kind + "&t=" + Date.now());
      if (!r.ok) return null;
      const j = await r.json();
      return j[kind] && Object.keys(j[kind]).length ? j[kind] : null;
    } catch (e) { return null; }
  };

  /* ---------- scoring ---------- */
  TA.clientScore = (d, settings) => {
    const al = [];
    if (d.ema20 != null) al.push(d.close > d.ema20);
    if (d.ema20 != null && d.ema50 != null) al.push(d.ema20 > d.ema50);
    if (d.ema50 != null && d.ema200 != null) al.push(d.ema50 > d.ema200);
    if (d.close != null && d.ema200 != null) al.push(d.close > d.ema200);
    const trend = al.length ? al.filter(Boolean).length / al.length * 100 : 50;
    const hist = d.macd_hist || 0;
    const macdComp = clamp01((hist / (0.01 * (d.close || 1)) + 1) / 2) * 100;
    const rsi = d.rsi14 != null ? d.rsi14 : 50;
    const rsiComp = (rsi <= 70 ? clamp01((rsi - 30) / 40) : clamp01(1 - (rsi - 70) / 30)) * 100;
    const momentum = 0.6 * macdComp + 0.4 * rsiComp;
    const participation = d.rel_volume != null ? clamp01((d.rel_volume - 0.5) / 2) * 100 : 50;
    const relStrength = d.rs_score != null ? d.rs_score : 50;
    const atrp = (d.atr != null && d.close) ? d.atr / d.close : null;
    const risk = atrp != null ? clamp01(1 - (atrp - 0.01) / 0.04) * 100 : 50;
    const W = settings.weights;
    const s = (W.trend * trend + W.momentum * momentum + W.participation * participation
      + W.relStrength * relStrength + W.risk * risk) / 100;
    return Math.max(0, Math.min(100, s));
  };
  TA.scoreOf = (d, settings) => d.score != null ? d.score : TA.clientScore(d, settings);

  TA.pctFromHigh = d => (d.high_252 != null && d.close != null && d.high_252 > 0) ? (d.close - d.high_252) / d.high_252 * 100 : null;
  TA.pctFromLow = d => (d.low_252 != null && d.close != null && d.low_252 > 0) ? (d.close - d.low_252) / d.low_252 * 100 : null;
  TA.pos52 = d => (d.high_252 != null && d.low_252 != null && d.high_252 > d.low_252)
    ? clamp01((d.close - d.low_252) / (d.high_252 - d.low_252)) * 100 : null;
  TA.zoneOf = (d, settings) => {
    const ph = TA.pctFromHigh(d), pl = TA.pctFromLow(d);
    if (ph == null) return null;
    if (ph >= -settings.highZonePct) return "high";
    if (pl != null && pl <= settings.lowZonePct) return "low";
    return null;
  };
  /* Uptrend / downtrend is a two-EMA comparison, defaulting to the same
     EMA50 vs EMA150 pair the pipeline already calls a golden / death cross.
     Which pair is used is a setting, so this is configurable without a deploy.

     The previous rule demanded a full four-level stack
     (close > EMA20 > EMA50 > EMA200) and dropped anything with one link out of
     order into "mixed" -- which was 19 of 28 Core names, and meant a name like
     TMO (price 12% above its own EMA50, but EMA50 $0.90 under EMA200) showed
     under every filter state because mixed was never filtered. */
  TA.TREND_EMAS = [20, 50, 150, 200];
  TA.trendOf = (d, settings) => {
    const fast = d["ema" + ((settings && settings.trendFast) || 50)];
    const slow = d["ema" + ((settings && settings.trendSlow) || 150)];
    if (fast == null || slow == null) return "mixed";   // not enough history yet
    return fast > slow ? "bull" : fast < slow ? "bear" : "mixed";
  };
  TA.trendRank = t => ({ bull: 2, mixed: 1, bear: 0 })[t];
  TA.trendLabel = t => ({ bull: "Bullish", bear: "Bearish", mixed: "Mixed" })[t];
  TA.rsiZone = v => v >= 70 ? "Overbought" : v <= 30 ? "Oversold" : "Neutral";

  /* ---------- Nocturne-derived semantic inks ----------
     Custom properties rather than oklch() literals, so switching theme is pure
     CSS and this logic layer stays DOM-free and theme-unaware. The values for
     both themes live in theme.css. */
  const UP = "var(--ta-up)", DOWN = "var(--ta-down)", WARN = "var(--ta-warn)";
  TA.ink = { up: UP, down: DOWN, warn: WARN, accent: "var(--color-accent)", accentSoft: "var(--color-accent-300)", muted: "var(--color-neutral-500)", faint: "var(--color-neutral-600)", text: "var(--color-text)" };
  TA.scoreColor = s => s >= 66 ? UP : s <= 33 ? DOWN : WARN;
  TA.dirColor = v => (v >= 0 ? UP : DOWN);


  /* group 0 awaiting data · 1 action required · 3 re-entry watch · 2 holding steady */
  TA.groupOf = (d, settings) => {
    if (d.pending) return 0;
    if (d.limited_history) return 2;
    const z = TA.zoneOf(d, settings);
    if (z === "high") return 1;
    if (z === "low") return 3;
    return 2;
  };

  /* the "why" sentence — plain text (the DC styles the emphasis) */
  TA.why = (d, settings) => {
    if (d.limited_history)
      return `Listed only ${d.history_bars} trading days ago — no true 52-week range yet, so booking and base rules stay disarmed (needs 252).`;
    const zn = TA.zoneOf(d, settings);
    if (zn === "high") return `${fmt(Math.abs(TA.pctFromHigh(d) || 0), 1)}% from the 52-week high — exit review zone.`;
    if (zn === "low") {
      if (settings.reEntryMode === "near_low") return `In the basing zone — re-entry watch (near-low rule).`;
      if (d.base_status === "confirmed") return `${d.days_since_low != null ? d.days_since_low + " days off the low — " : ""}base confirmed — re-entry candidate.`;
      if (d.base_status === "forming") return `Base forming (${d.base_score != null ? d.base_score + "/5" : "—"}) — hasn't cleared all checks. Wait.`;
      return `Near the low, still falling — no base. The detector says don't.`;
    }
    const c = d.components;
    if (c) {
      const weakest = [["trend", c.trend], ["momentum", c.momentum], ["volume", c.participation], ["RS", c.rel_strength]]
        .filter(x => x[1] != null).sort((a, b) => a[1] - b[1])[0];
      if (weakest) return `Trend intact, nothing to do — weakest bucket: ${weakest[0]} ${Math.round(weakest[1])}, the number to watch.`;
    }
    return `Quietly compounding — nothing to do.`;
  };

  /* the sizing instruction — {text, ink} or null */
  TA.action = (d, settings) => {
    const grp = TA.groupOf(d, settings);
    if (grp !== 1 && grp !== 3) return null;
    const zn = TA.zoneOf(d, settings);
    if (zn === "high") return { text: "If holding: book ⅓ (33%) — exit review zone.", ink: WARN };
    if (zn === "low") {
      if (settings.reEntryMode === "near_low" || d.base_status === "confirmed")
        return { text: "Re-enter with a ⅓ (33%) tranche — scale in, never all at once.", ink: "var(--color-accent-300)" };
      if (d.base_status === "forming") return { text: `Re-entry size 0% until the base confirms (${d.base_score != null ? d.base_score + "/5" : "—"}).`, ink: "var(--color-neutral-500)" };
      return { text: "Re-entry size 0% — still falling, no base.", ink: "var(--color-neutral-500)" };
    }
    return null;
  };

  /* short status pill next to the symbol */
  TA.zoneTag = (d, settings) => {
    if (d.limited_history) return { text: `New listing · ${d.history_bars}d`, ink: "var(--color-neutral-400)", border: "var(--color-neutral-800)" };
    const zn = TA.zoneOf(d, settings);
    const ph = TA.pctFromHigh(d);
    if (zn === "high") return { text: `${fmt(Math.abs(ph || 0), 1)}% from 52w high`, ink: WARN, border: "color-mix(in srgb, var(--ta-warn) 45%, transparent)" };
    if (zn === "low") {
      if (settings.reEntryMode === "near_low") return { text: "Basing zone", ink: "var(--color-accent-300)", border: "var(--color-accent-700)" };
      if (d.base_status === "confirmed") return { text: `Base confirmed 5/5`, ink: "var(--color-accent-300)", border: "var(--color-accent-700)" };
      if (d.base_status === "forming") return { text: `Base forming ${d.base_score != null ? d.base_score + "/5" : ""}`, ink: "var(--color-neutral-400)", border: "var(--color-neutral-800)" };
      return { text: "Falling knife", ink: DOWN, border: "color-mix(in srgb, var(--ta-down) 45%, transparent)" };
    }
    return null;
  };
  /* 52-week extreme sticker — the fixed 2% rule, shared everywhere */
  TA.at52 = d => {
    const ph = TA.pctFromHigh(d), pl = TA.pctFromLow(d), r1 = v => Math.round(v * 10) / 10;
    if (ph != null && r1(ph) >= -2) return "high";
    if (pl != null && r1(pl) <= 2) return "low";
    return null;
  };

  /* ---------- filtering & sorting ---------- */
  TA.activeRows = (list, payload, st) => {
    if (!list) return [];
    const bySym = s => (payload.symbols || []).find(d => d.symbol === s);
    let rows = list.symbols.map(bySym).filter(Boolean);
    if (st.sector !== "all") rows = rows.filter(d => !d.sector || d.sector === st.sector);
    if (st.q) rows = rows.filter(d => d.symbol.toLowerCase().includes(st.q));
    if (st.trendF !== "all") rows = rows.filter(d => TA.trendOf(d, st.settings) === st.trendF);
    rows = rows.filter(d => { const t = TA.trendOf(d, st.settings); return t === "mixed" ? true : st.trendToggles[t] !== false; });
    rows.sort((a, b) => {
      let av, bv;
      if (st.sortKey === "trend") { av = TA.trendRank(TA.trendOf(a, st.settings)); bv = TA.trendRank(TA.trendOf(b, st.settings)); }
      else if (st.sortKey === "score") { av = TA.scoreOf(a, st.settings); bv = TA.scoreOf(b, st.settings); }
      else if (st.sortKey === "pos52") { av = TA.pos52(a); bv = TA.pos52(b); }
      else if (st.sortKey === "symbol") { return a.symbol.localeCompare(b.symbol) * st.sortDir; }
      else { av = a[st.sortKey]; bv = b[st.sortKey]; }
      return ((av ?? -1e9) - (bv ?? -1e9)) * st.sortDir;
    });
    let pending = list.symbols.filter(s => !bySym(s)).map(s => ({ symbol: s, pending: true }));
    if (st.q) pending = pending.filter(d => d.symbol.toLowerCase().includes(st.q));
    return rows.concat(pending);
  };

  /* ---------- signals & levels ---------- */
  TA.signalChips = (d, settings) => {
    const px = d.close, t = TA.trendOf(d, settings), rz = TA.rsiZone(d.rsi14), zn = TA.zoneOf(d, settings);
    const ph = TA.pctFromHigh(d), mb = d.macd_hist >= 0;
    const ok = { ink: UP, border: "color-mix(in srgb, var(--ta-up) 40%, transparent)" };
    const bad = { ink: DOWN, border: "color-mix(in srgb, var(--ta-down) 40%, transparent)" };
    const warn = { ink: WARN, border: "color-mix(in srgb, var(--ta-warn) 40%, transparent)" };
    const flat = { ink: "var(--color-neutral-400)", border: "var(--color-neutral-800)" };
    return [
      Object.assign({ text: { bull: "Uptrend", bear: "Downtrend", mixed: "Mixed trend" }[t] }, t === "bull" ? ok : t === "bear" ? bad : warn),
      Object.assign({ text: (px >= d.ema50 ? "Above" : "Below") + " EMA50" }, px >= d.ema50 ? ok : bad),
      Object.assign({ text: "RSI " + fmt(d.rsi14, 0) + " · " + rz.toLowerCase() }, rz === "Overbought" ? bad : rz === "Oversold" ? ok : flat),
      Object.assign({ text: "MACD " + (mb ? "bullish" : "bearish") }, mb ? ok : bad),
      zn === "high" ? Object.assign({ text: "Near 52w high" }, warn)
        : zn === "low" ? Object.assign({ text: "Near 52w low" }, bad)
          : Object.assign({ text: fmt(Math.abs(ph || 0), 1) + "% below high" }, flat),
    ];
  };
  TA.ladder = (d) => {
    const px = d.close;
    const lv = [{ n: "52w high", v: d.high_252 }, { n: "EMA 20", v: d.ema20 }, { n: "EMA 50", v: d.ema50 },
    { n: "EMA 200", v: d.ema200 }, { n: "52w low", v: d.low_252 }]
      .filter(l => l.v != null).map(l => ({ n: l.n, v: l.v, dist: (l.v - px) / px * 100 }));
    lv.push({ n: "Current price", v: px, cur: true });
    lv.sort((a, b) => b.v - a.v);
    const ci = lv.findIndex(r => r.cur);
    lv.forEach((r, i) => { if (i === ci - 1) r.res = true; if (i === ci + 1) r.sup = true; });
    return lv.map(r => ({
      name: r.n, cur: !!r.cur, tag: r.res ? "resistance" : r.sup ? "support" : "",
      tagInk: r.res ? WARN : "var(--color-accent-300)",
      value: "$" + fmt(r.v),
      dist: r.cur ? "" : (r.dist > 0 ? "+" : "") + fmt(r.dist, 1) + "%",
    }));
  };

  /* ---------- daily summary ---------- */
  TA.summary = (lists, payload) => {
    const bySym = s => (payload.symbols || []).find(d => d.symbol === s);
    const core = TA.corePortfolio(lists);
    const held = (core ? core.symbols : []).map(bySym)
      .filter(d => d && !d.pending && d.change_pct != null && d.sector !== "Index" && d.sector !== "Crypto");
    if (held.length < 3) return null;
    const byChg = (a, b) => (b.change_pct || 0) - (a.change_pct || 0);
    const eq = held.slice().sort(byChg);
    const PINNED = ["QQQ", "SPY", "BTC/USD"];
    const pinned = PINNED.map(bySym).filter(d => d && !d.pending && d.change_pct != null);
    const pinnedSet = new Set(pinned.map(d => d.symbol));
    const pool = [...new Set(lists.flatMap(l => l.symbols))].map(bySym)
      .filter(d => d && !d.pending && d.change_pct != null && d.sector === "Index" && !pinnedSet.has(d.symbol))
      .sort(byChg);
    const extras = pool.length ? (pool.length > 1 ? [pool[0], pool[pool.length - 1]] : [pool[0]]) : [];
    const up = eq.slice(0, 5), upSet = new Set(up.map(d => d.symbol));
    const down = eq.slice().reverse().filter(d => !upSet.has(d.symbol)).slice(0, 5);
    const upN = held.filter(d => (d.change_pct || 0) > 0).length;
    const downN = held.filter(d => (d.change_pct || 0) < 0).length;
    const ext = held.filter(d => TA.at52(d)).length;
    const raw = payload.expected_last_trading_day
      || (payload.generated_at && payload.generated_at !== "demo" ? payload.generated_at.slice(0, 10) : null);
    return {
      listName: core.name, up, down, index: pinned.concat(extras),
      breadth: `${upN} up · ${downN} down` + (ext ? ` · ${ext} at 52-week extremes` : ""),
      date: raw ? new Date(raw + "T00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " · close" : "demo",
    };
  };

  /* ---------- sector mix (donut data, rendered as bars) ---------- */
  TA.sectorMix = (lists, payload) => {
    const bySym = s => (payload.symbols || []).find(d => d.symbol === s);
    const held = [...new Set(lists.filter(l => l.type === "portfolio").flatMap(l => l.symbols))]
      .map(bySym).filter(d => d && !d.pending);
    const by = {};
    held.forEach(d => { const s = d.sector || "Other"; (by[s] = by[s] || []).push(d); });
    const secs = Object.entries(by).map(([n, arr]) => ({
      n, arr, cnt: arr.length, avg: arr.reduce((x, y) => x + (y.change_pct || 0), 0) / arr.length
    })).sort((a, b) => b.cnt - a.cnt);
    if (!secs.length) return [];
    const total = secs.reduce((s, x) => s + x.cnt, 0);
    return secs.map((s, i) => ({
      name: s.n, count: s.cnt, pct: s.cnt / total * 100, pctLabel: fmt(s.cnt / total * 100, 1) + "%",
      barColor: ["var(--color-accent)", "var(--color-accent-600)", "var(--color-accent-600)",
        "var(--color-accent-700)", "var(--color-accent-700)", "var(--color-accent-800)"][Math.min(i, 5)],
      move: s.avg, moveLabel: (s.avg < 0 ? "−" : "+") + fmt(Math.abs(s.avg), 1) + "%",
      moveInk: s.avg < 0 ? DOWN : UP,
      stocks: s.arr.slice().sort((a, b) => (a.change_pct || 0) - (b.change_pct || 0)),
    }));
  };

  /* ---------- staleness ---------- */
  TA.staleness = (payload, dataUrl) => {
    if (!payload || payload.generated_at === "demo" || !dataUrl) return null;
    const gen = new Date(payload.generated_at).getTime();
    if (isNaN(gen)) return null;
    const ageH = (Date.now() - gen) / 3.6e6;
    const day = new Date().getDay(), weekday = day >= 1 && day <= 5;
    if (ageH > 26) return { level: "bad", text: "data " + Math.round(ageH) + "h old — pipeline may be down" };
    if (weekday && ageH > 2) return { level: "bad", text: "data " + ageH.toFixed(1) + "h old" };
    if (weekday && ageH > 1) return { level: "warn", text: "data " + ageH.toFixed(1) + "h old" };
    return null;
  };

  /* ---------- indicator math (mirrors the server) ---------- */
  TA.emaArr = (vals, period) => {
    const k = 2 / (period + 1); const out = new Array(vals.length).fill(null); let prev = null;
    for (let i = 0; i < vals.length; i++) { const v = vals[i]; prev = prev == null ? v : v * k + prev * (1 - k); if (i >= period - 1) out[i] = prev; }
    return out;
  };
  TA.rsiArr = (c, period) => {
    const out = new Array(c.length).fill(null); let g = 0, l = 0;
    for (let i = 1; i < c.length; i++) {
      const ch = c[i] - c[i - 1], up = Math.max(ch, 0), dn = Math.max(-ch, 0);
      if (i <= period) { g += up; l += dn; if (i === period) { g /= period; l /= period; out[i] = 100 - 100 / (1 + (l === 0 ? 100 : g / l)); } }
      else { g = (g * (period - 1) + up) / period; l = (l * (period - 1) + dn) / period; out[i] = 100 - 100 / (1 + (l === 0 ? 100 : g / l)); }
    }
    return out;
  };
  TA.macdArr = (c, f, s, sig) => {
    const ef = TA.emaArr(c, f), es = TA.emaArr(c, s);
    const line = c.map((_, i) => (ef[i] != null && es[i] != null) ? ef[i] - es[i] : null);
    const fi = line.findIndex(v => v != null);
    const dense = line.slice(fi).map(v => v == null ? 0 : v);
    const sd = TA.emaArr(dense, sig);
    const signal = new Array(c.length).fill(null);
    for (let i = 0; i < sd.length; i++) signal[fi + i] = sd[i];
    const hist = c.map((_, i) => (line[i] != null && signal[i] != null) ? line[i] - signal[i] : null);
    return { line, signal, hist };
  };
  TA.autoSR = (bars, lookback = 5, maxLevels = 4) => {
    const piv = [];
    for (let i = lookback; i < bars.length - lookback; i++) {
      let hi = true, lo = true;
      for (let j = i - lookback; j <= i + lookback; j++) { if (bars[j].high > bars[i].high) hi = false; if (bars[j].low < bars[i].low) lo = false; }
      if (hi) piv.push(bars[i].high); if (lo) piv.push(bars[i].low);
    }
    if (!piv.length) return [];
    const last = bars[bars.length - 1].close, tol = last * 0.012, cl = [];
    piv.sort((a, b) => a - b).forEach(p => {
      const c = cl.find(c => Math.abs(c.price - p) < tol);
      if (c) { c.price = (c.price * c.n + p) / (c.n + 1); c.n++; } else cl.push({ price: p, n: 1 });
    });
    cl.forEach(c => { c.kind = c.price >= last ? "resistance" : "support"; });
    cl.sort((a, b) => b.n - a.n);
    return cl.slice(0, maxLevels);
  };
  /* Log-regression channel — the long-horizon entry rule.
   *
   * A least-squares line through log10(close) with rails at ±k standard
   * deviations of the residuals. Log price is what makes it honest over years:
   * on a linear axis the same percentage pullback looks bigger the higher the
   * price got, so a straight channel would fan out and the early years would
   * read as permanently cheap.
   *
   * The channel is fitted to the bars it is HANDED, not to the whole history —
   * the caller passes the visible span, so a 3Y and a Max channel are genuinely
   * different descriptions of the same stock. `pos` is the reading that matters:
   * 0 at the lower rail, 1 at the upper, and comparable across symbols because
   * the width is each symbol's own dispersion rather than a fixed percentage.
   */
  TA.regChannel = (bars, opts) => {
    const o = Object.assign({ k: 1.8, entryPct: 18, cooldown: 60 }, opts || {});
    if (!bars || bars.length < 30) return null;
    const n = bars.length;
    const ys = bars.map(b => Math.log10(Math.max(b.close, 1e-6)));
    const mx = (n - 1) / 2, my = ys.reduce((a, v) => a + v, 0) / n;
    let sxx = 0, sxy = 0;
    for (let i = 0; i < n; i++) { const dx = i - mx; sxx += dx * dx; sxy += dx * (ys[i] - my); }
    const slope = sxx ? sxy / sxx : 0, icpt = my - slope * mx;
    const fit = new Array(n), res = new Array(n);
    let ss = 0;
    for (let i = 0; i < n; i++) { fit[i] = slope * i + icpt; res[i] = ys[i] - fit[i]; ss += res[i] * res[i]; }
    const sd = Math.sqrt(ss / n);
    if (!(sd > 0)) return null;
    const half = o.k * sd;
    const at = (i, off) => +Math.pow(10, fit[i] + off).toFixed(4);
    const line = (off) => bars.map((b, i) => ({ time: b.time, value: at(i, off) }));
    /* 0 at the lower rail, 1 at the upper. Deliberately NOT clamped: price
       breaking out above the channel reads as 1.2, which is information. */
    const pos = res.map(r => (r + half) / (2 * half));
    const rising = slope > 0;
    const entries = [];
    let cool = -1e9;
    for (let i = 0; i < n; i++) {
      if (rising && pos[i] < o.entryPct / 100 && i - cool >= o.cooldown) {
        entries.push({ time: bars[i].time, price: bars[i].close, pos: pos[i] });
        cool = i;
      }
    }
    return {
      slope, sd, rising, pos, entries, k: o.k,
      mid: line(0), upper: line(half), lower: line(-half),
      posNow: pos[n - 1],
      lowerNow: at(n - 1, -half), midNow: at(n - 1, 0), upperNow: at(n - 1, half),
      /* compounded over 252 trading days, so it reads as a trend rate rather
         than a slope in log units nobody can picture */
      annualPct: (Math.pow(10, slope * 252) - 1) * 100,
      bars: n, from: bars[0].time, to: bars[n - 1].time,
    };
  };
  /* How a channel position should be read. The bands are the same ones the
     entry rule uses, so the words and the ring can never disagree. */
  TA.regZone = (ch) => {
    if (!ch) return null;
    const p = ch.posNow;
    if (!ch.rising) return { label: "Channel falling — no entry", ink: TA.ink.down, act: false };
    if (p < 0) return { label: "Below the channel", ink: TA.ink.warn, act: false };
    if (p < 0.18) return { label: "Lower rail — entry zone", ink: TA.ink.up, act: true };
    if (p < 0.45) return { label: "Below mid-channel", ink: "var(--color-accent-300)", act: false };
    if (p <= 0.82) return { label: "Upper half of channel", ink: "var(--color-neutral-500)", act: false };
    if (p <= 1) return { label: "Upper rail — stretched", ink: TA.ink.warn, act: false };
    return { label: "Above the channel", ink: TA.ink.down, act: false };
  };
  TA.genBars = (seedStr, start) => {
    let s = 0; for (const ch of seedStr) s = (s * 31 + ch.charCodeAt(0)) % 233280;
    const rnd = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
    const n = 420, dates = [], d = new Date();
    while (dates.length < n) { const dow = d.getDay(); if (dow !== 0 && dow !== 6) dates.unshift(d.toISOString().slice(0, 10)); d.setDate(d.getDate() - 1); }
    const bars = []; let px = start || 100; const drift = (rnd() - 0.45) * 0.0011;
    for (const t of dates) {
      const vol = 0.012 + rnd() * 0.012, ch = drift + (rnd() - 0.5) * vol * 2, o = px;
      px = Math.max(1, px * (1 + ch));
      const hi = Math.max(o, px) * (1 + rnd() * 0.008), lo = Math.min(o, px) * (1 - rnd() * 0.008);
      bars.push({ time: t, open: +o.toFixed(2), high: +hi.toFixed(2), low: +lo.toFixed(2), close: +px.toFixed(2), volume: Math.round(6e5 + rnd() * 5e6) });
    }
    return bars;
  };

  /* ---------- chart config ---------- */
  TA.DEFAULT_CHCFG = {
    span: "6M",
    ema: [
      { key: "ema20", period: 20, color: "#b5abfc", label: "EMA 20" },
      { key: "ema50", period: 50, color: "#9184d9", label: "EMA 50" },
      { key: "ema150", period: 150, color: "#5d5294", label: "EMA 150" },
      { key: "ema200", period: 200, color: "#75798c", label: "EMA 200" },
    ],
    rsi: { period: 14, color: "#e9e9ed", ob: 70, os: 30 },
    /* k: rail width in standard deviations of the fit's residuals.
       entryPct: how far down the channel counts as the entry zone, in percent.
       cooldown: bars before the rule may speak again, so one long trough at the
       lower rail is one entry rather than forty. */
    regch: { k: 1.8, entryPct: 18, cooldown: 60 },
    macd: { fast: 12, slow: 26, signal: 9, macdColor: "#9184d9", signalColor: "#b2b6ca" },
  };
  /* default chart: the two EMAs that drive the rule, their crossovers, and the last
     price. Candles, the other EMAs, S/R and the 52w bands stay off until asked for. */
  TA.DEFAULT_VIS = { candles: false, ema20: false, ema50: true, ema150: true, ema200: false, rsi: true, macd: true, sr: false, w52: false, cross: true, chart: true, pline: true, regch: true };
  /* 3Y and 5Y exist for the regression channel: it needs years to describe a
     trend, and 1Y was the longest window on offer. */
  TA.SPANS = ["1M", "3M", "6M", "1Y", "3Y", "5Y", "Max"];
  TA.SPAN_DAYS = { "1M": 21, "3M": 63, "6M": 126, "1Y": 252, "3Y": 756, "5Y": 1260 };
  /* The single source for span -> bar count. Both dashboards used to carry their
     own copy of this map inline, so adding a span here would have left the other
     one resolving `undefined` days and indexing bars[NaN]. */
  TA.spanDays = (span, n) => span === "Max" ? n : Math.min(n, TA.SPAN_DAYS[span] || TA.SPAN_DAYS["6M"]);
  TA.EMA_KEYS = ["ema20", "ema50", "ema150", "ema200"];
  TA.loadCh = (k, d) => {
    try { const v = JSON.parse(lsGet(k)); return v ? Object.assign(JSON.parse(JSON.stringify(d)), v) : JSON.parse(JSON.stringify(d)); }
    catch (e) { return JSON.parse(JSON.stringify(d)); }
  };
  TA.saveCh = (cfg, vis) => { lsSet(CH_KEY, JSON.stringify(cfg)); lsSet(CH_KEY + "_vis_v2", JSON.stringify(vis)); };
  /* ---------- theme ----------
     Lightweight Charts is canvas-drawn: it cannot read CSS custom properties,
     so unlike the rest of the UI its palette has to be handed over as literal
     colours and re-applied when the theme changes. */
  const THEME_KEY = "trendalert_theme_v1";
  TA.keys.THEME_KEY = THEME_KEY;
  TA.THEMES = ["dark", "light"];
  TA.loadTheme = () => {
    const t = lsGet(THEME_KEY);
    /* the retired Aurora skin names all collapse to dark */
    return TA.THEMES.includes(t) ? t : "dark";
  };
  TA.saveTheme = (t) => lsSet(THEME_KEY, TA.THEMES.includes(t) ? t : "dark");

  /* chart chrome on the Nocturne ground */
  TA.chartThemes = {
    dark: {
      text: "#9397ab", grid: "rgba(233,233,237,.05)", border: "#3f424d", cross: "#595d6c",
      accent: "#9184d9", up: "#63c69b", down: "#e0798a", upA: "rgba(99,198,155,.5)", downA: "rgba(224,121,138,.5)",
      histUp: "rgba(99,198,155,.55)", histDown: "rgba(224,121,138,.55)", sr: "#b2b6ca", gold: "#d9c58a",
      chan: "#8a7fd0",
      obZone: "rgba(224,121,138,.10)", osZone: "rgba(99,198,155,.10)",
    },
    light: {
      text: "#585d70", grid: "rgba(20,22,29,.07)", border: "#d5d8e6", cross: "#a9adbe",
      accent: "#5b4fa0", up: "#1d7d55", down: "#b03b52", upA: "rgba(29,125,85,.5)", downA: "rgba(176,59,82,.5)",
      histUp: "rgba(29,125,85,.55)", histDown: "rgba(176,59,82,.55)", sr: "#343845", gold: "#8a6d1f",
      chan: "#6b5fc0",
      obZone: "rgba(176,59,82,.09)", osZone: "rgba(29,125,85,.09)",
    },
  };
  /* Kept as a property so the two `const CT = T.chartTheme` call sites in the
     template pick up the active theme at render time without changing. */
  Object.defineProperty(TA, "chartTheme", {
    get() { return TA.chartThemes[TA.themeName] || TA.chartThemes.dark; },
  });
  TA.themeName = TA.loadTheme();

  /* ---------- demo payload (identical universe & shape) ---------- */
  const DEMO_COMPS = {
    QQQ: { trend: 88, momentum: 66, participation: 58, rel_strength: 75, risk_adj: 80 },
    SOXX: { trend: 69, momentum: 61, participation: 66, rel_strength: 71, risk_adj: 52 },
    SPY: { trend: 84, momentum: 60, participation: 52, rel_strength: 60, risk_adj: 86 },
    IWM: { trend: 44, momentum: 48, participation: 47, rel_strength: 38, risk_adj: 63 },
    "BTC/USD": { trend: 57, momentum: 72, participation: 83, rel_strength: 64, risk_adj: 30 },
    RDDT: { trend: 74, momentum: 68, participation: 71, rel_strength: 66, risk_adj: 38 },
    AAPL: { trend: 92, momentum: 71, participation: 64, rel_strength: 88, risk_adj: 74 },
    MSFT: { trend: 78, momentum: 58, participation: 49, rel_strength: 66, risk_adj: 81 },
    NVDA: { trend: 95, momentum: 83, participation: 88, rel_strength: 97, risk_adj: 41 },
    JPM: { trend: 64, momentum: 52, participation: 44, rel_strength: 55, risk_adj: 77 },
    XOM: { trend: 38, momentum: 44, participation: 57, rel_strength: 33, risk_adj: 69 },
    UNH: { trend: 22, momentum: 31, participation: 35, rel_strength: 11, risk_adj: 58 },
    TSLA: { trend: 47, momentum: 66, participation: 79, rel_strength: 62, risk_adj: 35 },
    CAT: { trend: 71, momentum: 49, participation: 41, rel_strength: 59, risk_adj: 72 }
  };
  const DEMO_SCORES = Object.fromEntries(Object.entries(DEMO_COMPS).map(([s, c]) =>
    [s, Math.round(0.30 * c.trend + 0.20 * c.momentum + 0.20 * c.participation + 0.20 * c.rel_strength + 0.10 * c.risk_adj)]));

  TA.demoData = (lists) => {
    const rows = [["AAPL", 228, "Technology", 1.012, 0.74], ["MSFT", 430, "Technology", 1.09, 0.78], ["NVDA", 128, "Technology", 1.018, 0.55],
    ["JPM", 215, "Finance", 1.14, 0.82], ["XOM", 112, "Energy", 1.22, 0.93], ["UNH", 512, "Healthcare", 1.35, 0.94],
    ["TSLA", 250, "Consumer", 1.42, 0.93], ["CAT", 355, "Industrial", 1.07, 0.71],
    ["QQQ", 545, "Index", 1.03, 0.79], ["SOXX", 232, "Index", 1.12, 0.75], ["SPY", 560, "Index", 1.02, 0.82],
    ["IWM", 215, "Index", 1.16, 0.90], ["BTC/USD", 67500, "Crypto", 1.18, 0.62],
    ["RDDT", 92, "Technology", 1.05, 0.60]];
    const syms = rows.map(([s, p, sec, hiM, loM]) => {
      const rsi = 20 + Math.random() * 60, hist = (Math.random() - .45) * 2;
      const e20 = p * (1 + (Math.random() - .5) * .03), e50 = p * (1 + (Math.random() - .5) * .06),
        e150 = p * (1 + (Math.random() - .5) * .10), e200 = p * (1 + (Math.random() - .5) * .14);
      return {
        symbol: s, sector: sec, as_of_date: "2026-06-12", close: p, change: p * (Math.random() - .5) * .02,
        change_pct: (Math.random() - .5) * 3, rsi14: +rsi.toFixed(2), macd: +(hist + Math.random()).toFixed(3),
        macd_signal: +Math.random().toFixed(3), macd_hist: +hist.toFixed(3),
        ema20: +e20.toFixed(2), ema50: +e50.toFixed(2), ema150: +e150.toFixed(2), ema200: +e200.toFixed(2),
        high_252: s === "RDDT" ? null : +(p * hiM).toFixed(2), low_252: s === "RDDT" ? null : +(p * loM).toFixed(2),
        history_bars: s === "RDDT" ? 96 : 300, limited_history: s === "RDDT",
        base_status: s === "RDDT" ? "insufficient_history" : (loM >= 0.9 ? (s === "TSLA" ? "confirmed" : s === "XOM" ? "forming" : "none") : "none"),
        base_score: s === "TSLA" ? 5 : s === "XOM" ? 3 : 0, days_since_low: s === "TSLA" ? 42 : s === "XOM" ? 19 : null,
        components: DEMO_COMPS[s], score: DEMO_SCORES[s], rs_score: DEMO_COMPS[s].rel_strength,
        rel_volume: +(0.6 + Math.random() * 1.6).toFixed(2), atr: +(p * 0.02).toFixed(2)
      };
    });
    const have = new Set(syms.map(d => d.symbol));
    const wanted = [...new Set(lists.flatMap(l => l.symbols))];
    wanted.filter(s2 => !have.has(s2)).forEach(s2 => {
      let sd = 0; for (const ch of s2) sd = (sd * 31 + ch.charCodeAt(0)) % 233280;
      const rnd = () => { sd = (sd * 9301 + 49297) % 233280; return sd / 233280; };
      const p = +(15 + rnd() * 400).toFixed(2);
      const comp = {
        trend: Math.round(20 + rnd() * 75), momentum: Math.round(20 + rnd() * 70),
        participation: Math.round(20 + rnd() * 70), rel_strength: Math.round(15 + rnd() * 80), risk_adj: Math.round(25 + rnd() * 65)
      };
      const hiM = 1.01 + rnd() * 0.3, loM = 0.6 + rnd() * 0.33;
      syms.push({
        symbol: s2, sector: "Other", as_of_date: new Date().toISOString().slice(0, 10),
        close: p, change: +(p * (rnd() - .5) * .02).toFixed(2), change_pct: +((rnd() - .5) * 3).toFixed(2),
        rsi14: +(25 + rnd() * 50).toFixed(2), macd: +((rnd() - .4)).toFixed(3), macd_signal: +(rnd() * .5).toFixed(3),
        macd_hist: +((rnd() - .5)).toFixed(3),
        ema20: +(p * (1 + (rnd() - .5) * .03)).toFixed(2), ema50: +(p * (1 + (rnd() - .5) * .06)).toFixed(2),
        ema150: +(p * (1 + (rnd() - .5) * .1)).toFixed(2), ema200: +(p * (1 + (rnd() - .5) * .14)).toFixed(2),
        high_252: +(p * hiM).toFixed(2), low_252: +(p * loM).toFixed(2),
        history_bars: 300, limited_history: false, base_status: "none", base_score: 0, days_since_low: null,
        components: comp, score: Math.round(.3 * comp.trend + .2 * comp.momentum + .2 * comp.participation + .2 * comp.rel_strength + .1 * comp.risk_adj),
        rs_score: comp.rel_strength, rel_volume: +(0.6 + rnd() * 1.6).toFixed(2), atr: +(p * 0.02).toFixed(2)
      });
    });
    return {
      generated_at: "demo", timeframe: "1Day", symbols: syms,
      alerts: [{ symbol: "AAPL", type: "EMA 50 crossed above EMA 150", detail: "Golden cross", dir: "bull" },
      { symbol: "TSLA", type: "EMA 150 crossed below EMA 50", detail: "Death cross", dir: "bear" }]
    };
  };

  window.TA = TA;
})();
