/* TrendAlert — redesign shell.
 *
 * Presentation only. Every rule — grouping, zones, base status, scoring,
 * trend, staleness, sector mix, indicator math, the demo fallback — comes from
 * window.TA (frontend/v2/trendalert-core.js), unchanged. If a number here
 * disagrees with the shipped dashboard, this file is wrong, not the core.
 *
 * Two deliberate differences from the shipped dashboard, both about writes:
 *
 *   1. It does NOT publish on page load. The live dashboard POSTs universe and
 *      core 3s after every load, which is why its URL cannot be handed out.
 *      Here a POST only happens when the viewer actually edits something, so
 *      opening the link changes nothing.
 *   2. Writes still need the admin password (TA.post skips without a token),
 *      so a reader without it edits only their own localStorage.
 *
 * Rendering is two roots and one delegated listener. #app is the page, #layer
 * is the drawer and the modals; both are replaced wholesale on every paint,
 * which is why nothing binds handlers per node. The three chart containers are
 * the exception — they are created once and MOVED into the fresh markup, so
 * Lightweight Charts keeps its canvases across a repaint.
 */
(function () {
  const T = window.TA;
  const app = document.getElementById("app");
  const layer = document.getElementById("layer");

  /* ---------------- state ---------------- */
  const lists = T.loadLists();
  if (T.migrateLists(lists)) T.saveLists(lists);

  /* view is shared with the shipped dashboard through the same settings key,
     but a browser that has never chosen one lands on the table here — the
     redesign leads with the ledger, not the cards. */
  const storedView = (() => {
    try { return (JSON.parse(T.lsGet(T.keys.SET_KEY)) || {}).view; } catch (e) { return null; }
  })();

  const s = {
    payload: null, mode: "demo", statusText: "Loading…",
    lists, activeType: "portfolio", activeListId: null,
    settings: T.loadSettings(), pbre: T.loadPbre(),
    theme: T.loadTheme(),
    dataUrl: T.lsGet(T.keys.DATA_KEY) || T.DEFAULT_DATA_URL,
    syncUrl: T.lsGet(T.keys.SYNC_KEY) || T.DEFAULT_SYNC_URL,
    chCfg: T.loadCh(T.keys.CH_KEY, T.DEFAULT_CHCFG),
    vis: T.loadCh(T.keys.CH_KEY + "_vis_v2", T.DEFAULT_VIS),
    chartStore: null,
    q: "", sector: "all", trendF: "all",
    view: storedView === "cards" ? "cards" : "table",
    sortKey: "trend", sortDir: -1,
    selected: null, drawerTab: "detail", drawerOpen: false, chartCfgOpen: false,
    settingsOpen: false, form: null, confirmClose: null,
    listsOpen: false, newListName: "", newListType: "portfolio",
    addDraft: "", addNote: "", addModal: null, confirm: null, pw: null,
    toast: "",
  };
  /* keep the two copies of `view` in step from the first paint: loadSettings
     fills in the shipped dashboard's default ("cards") for a browser that has
     never chosen, and saving Settings would otherwise write that back and
     silently undo the table default above */
  s.settings.view = s.view;
  if (!T.SPANS.includes(s.chCfg.span)) s.chCfg.span = "6M";
  let emaOrder = T.EMA_KEYS.filter(k => s.vis[k]);

  /* The tape reads left to right as the market is usually quoted, regardless
     of the order the symbols sit in the Index list. Anything not named here
     queues behind them in list order. */
  const PROXY = {
    SPY: "S&P 500", VOO: "S&P 500", QQQ: "Nasdaq 100", DIA: "Dow 30",
    IWM: "Russell 2000", IJR: "Small cap", SOXX: "Semis", "BTC/USD": "Bitcoin",
    IAUM: "Gold", IGV: "Software", IHAK: "Cybersecurity", SPGI: "S&P Global",
  };
  const TAPE_ORDER = ["SPY", "VOO", "QQQ", "DIA", "IWM", "SOXX", "BTC/USD"];
  const TYPES = [["portfolio", "Portfolios"], ["watchlist", "Watchlists"], ["index", "Indexes"]];

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
  const listOf = (a) => a.length <= 1 ? (a[0] || "") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1];
  const on = (o, k) => (o || {})[k] !== false;

  const listsOfType = (t) => s.lists.filter(l => l.type === t);
  const activeList = () => {
    const of = listsOfType(s.activeType);
    return of.find(l => l.id === s.activeListId) || of[0] || null;
  };
  /* the same filter/sort pipeline the shipped dashboard runs */
  const rows = () => s.payload ? T.activeRows(activeList(), s.payload, {
    sector: s.sector, q: s.q, trendF: s.trendF, trendToggles: { bull: true, bear: true },
    sortKey: s.sortKey, sortDir: s.sortDir, settings: s.settings,
  }) : [];
  const live = () => rows().filter(d => !d.pending);
  const byScore = (a, b) => T.scoreOf(b, s.settings) - T.scoreOf(a, s.settings);

  let toastTimer = null;
  function flash(msg) {
    clearTimeout(toastTimer);
    s.toast = msg; render();
    toastTimer = setTimeout(() => { s.toast = ""; render(); }, 5000);
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
    /* bars are dropped rather than re-pulled: the next chart opened re-fetches
       the one symbol it needs (see ensureChart) */
    chartPending = {};
    s.chartStore = null;
    chartedSym = null;
    render();
  }

  /* Unlike the shipped dashboard this is NOT called on load — only after an
     edit, so that merely opening the page cannot republish the universe. */
  function publishPosts(lists, url, token) {
    const core = T.corePortfolio(lists);
    return Promise.all([
      T.postAuth(url, "universe", [...new Set(lists.flatMap(l => l.symbols))], token),
      T.postAuth(url, "core", core ? core.symbols : [], token),
    ]).then((rs) => rs.find((r) => !r.ok) || rs[0]);
  }

  /* --- write authorisation -------------------------------------------------
     Asked ONCE per session, then held in memory until the tab closes.

     There is no verify endpoint, so THE WRITE ITSELF is the check: the save is
     attempted with the candidate password and a 401 rolls it back. That is what
     keeps the screen honest — what you see always matches what the server
     accepted, which is exactly what silently-skipped POSTs used to break.

     The 3-attempt limit catches typos. It is NOT a security control: the
     backend has no rate limiting and a reload resets the count. The real gate
     is compare_digest in notes_function. */
  const MAX_PW_TRIES = 3;
  let sessionToken = null;

  function pwError(res) {
    if (!res) return "";
    if (res.offline) return "Could not reach the server. Nothing was saved.";
    if (res.status === 503) return "Writes are disabled: the server has no password configured.";
    if (res.status === 401) return "Password incorrect.";
    return `The server refused the save (HTTP ${res.status}). Nothing was saved.`;
  }

  /* apply() mutates state and localStorage and returns its own revert().
     post(token) resolves {ok, status, offline}. done() runs only on success. */
  function gatedSave(what, apply, post, done, candidate) {
    const tryWith = (token, onFail) => {
      const revert = apply();
      render();
      post(token).then((res) => {
        if (!res.ok) { revert(); render(); return onFail(res); }
        sessionToken = token;
        T.saveToken(token);          // remembered so next session pre-fills
        s.pw = null;
        render();
        if (done) done();
      });
    };

    const ask = (res, prefill) => {
      const st = { what, value: prefill || "", tries: 0, error: pwError(res), busy: false };
      st.submit = () => {
        const pw = String(st.value || "").trim();
        if (!pw) { st.error = "Enter the write password."; return render(); }
        st.busy = true; render();
        tryWith(pw, (r) => {
          st.busy = false;
          st.tries += 1;
          const left = MAX_PW_TRIES - st.tries;
          if (r.status === 401 && left > 0) {
            st.value = "";
            st.error = `Password incorrect. ${left} ${left === 1 ? "try" : "tries"} left.`;
            return render();
          }
          s.pw = null; render();
          flash(r.status === 401
            ? `${what} was discarded — the password was rejected ${MAX_PW_TRIES} times.`
            : `${what} was discarded — ${pwError(r).toLowerCase()}`);
        });
      };
      s.pw = st;
      render();
    };

    const first = String(candidate || "").trim() || sessionToken;
    /* a session token can go stale if the password is rotated mid-session, so
       a rejection falls back to asking rather than silently losing the edit */
    if (first) return tryWith(first, (res) => { sessionToken = null; ask(res, ""); });
    ask(null, T.loadToken());
  }
  /* A write that cannot reach the bucket must FAIL LOUDLY, not pretend. Every
     edit is also kept in localStorage, so an unguarded save repaints as though
     it worked and the change is quietly lost everywhere else -- which is how
     universe.json sat eleven days stale while the dashboard looked fine.
     TA.post's silent skip is inherited from the OLD dashboard, which POSTed 3s
     after every page load and would have spammed viewers with 401s. This one
     only ever posts on a deliberate edit, so silence is the wrong default here. */
  /* `note` rides along inside apply/revert on purpose. Setting it before the
     save meant a rolled-back edit left "Added ORCL to Core" on screen — the UI
     still claiming the add happened, which is the exact lie this whole gate
     exists to stop. It has to appear and disappear WITH the change. */
  function saveLists(next, note) {
    const prev = s.lists, prevNote = s.addNote, url = s.syncUrl;
    gatedSave("Your list change",
      () => {
        s.lists = next; T.saveLists(next);
        if (note !== undefined) s.addNote = note;
        return () => {
          s.lists = prev; T.saveLists(prev);
          if (note !== undefined) s.addNote = prevNote;
        };
      },
      (token) => publishPosts(next, url, token));
  }

  /* ---------------- lists ---------------- */
  function toggleMembership(sym, listId) {
    saveLists(s.lists.map(l => l.id !== listId ? l : {
      ...l, symbols: l.symbols.includes(sym) ? l.symbols.filter(x => x !== sym) : l.symbols.concat(sym),
    }));
  }
  function removeFrom(listId, sym) {
    saveLists(s.lists.map(l => l.id === listId ? { ...l, symbols: l.symbols.filter(x => x !== sym) } : l));
  }
  function submitAdd() {
    const v = String(s.addDraft || "").trim().toUpperCase();
    if (!v) return;
    if (!s.lists.length) { s.addNote = "Create a list first — Manage lists."; return render(); }
    const act = activeList();
    const first = s.lists.find(l => !l.symbols.includes(v));
    const target = (act && !act.symbols.includes(v)) ? act.id : (first ? first.id : null);
    if (!target) { s.addNote = `${v} is already in every list.`; s.addDraft = ""; return render(); }
    s.addModal = { sym: v, target }; render();
  }
  function confirmAdd() {
    const m = s.addModal;
    if (!m) return;
    const target = s.lists.find(l => l.id === m.target);
    s.addModal = null; s.addDraft = "";
    const note = bySym(m.sym) ? "" :
      `Added ${m.sym} to “${target ? target.name : ""}” — it shows as awaiting data until the next pipeline run.`;
    saveLists(s.lists.map(l => l.id === m.target && !l.symbols.includes(m.sym)
      ? { ...l, symbols: l.symbols.concat(m.sym) } : l), note);
  }
  function addList() {
    const name = String(s.newListName || "").trim();
    if (!name) return flash("Give the list a name first.");
    s.newListName = "";
    saveLists(s.lists.concat({ id: T.uid(), name, type: s.newListType, symbols: [] }));
  }
  function deleteList(id) {
    const l = s.lists.find(x => x.id === id);
    if (!l) return;
    s.confirm = {
      title: "Delete list — " + l.name,
      msg: `Delete “${l.name}” and its ${l.symbols.length} ${l.symbols.length === 1 ? "symbol" : "symbols"}? The symbols stay in any other list that holds them.`,
      ok: () => {
        if (s.activeListId === id) s.activeListId = null;
        saveLists(s.lists.filter(x => x.id !== id));
      },
    };
    render();
  }

  /* ---------------- PB / RE ---------------- */
  /* pb is a COUNT of open bookings, not a flag: booking a third tranche makes it
     3, and each re-entry gives one back. The count reaching zero is what ends
     the cycle and clears the pair, which is the same moment the old flag pair
     used to reset on.
     RE keeps its old standalone meaning when there is nothing to give back —
     re-entering a name you exited before you ever marked a booking still just
     highlights RE — so the two rules are spelled out in the dialog rather than
     left for the reader to infer from the button. */
  const pbCount = (sym) => Math.max(0, +((s.pbre[sym] || {}).pb) || 0);
  const reCount = (sym) => Math.max(0, +((s.pbre[sym] || {}).re) || 0);
  const hasMarks = (sym) => pbCount(sym) > 0 || reCount(sym) > 0;

  function askPbre(sym, k) {
    const pb = pbCount(sym), re = reCount(sym);
    let msg;
    if (k === "pb") {
      msg = pb
        ? `Book another tranche of ${sym}? The PB counter goes ${pb} → ${pb + 1}.`
        : `Have you booked profit on ${sym}? OK highlights PB with a count of 1.`;
    } else if (pb > 0) {
      msg = pb === 1
        ? `Re-entered ${sym}? That gives back the last booking — the counter goes 1 → 0 and the cycle closes.`
        : `Re-entered ${sym}? The PB counter goes ${pb} → ${pb - 1}.`;
    } else {
      msg = re
        ? `Remove the RE (re-entered) mark on ${sym}?`
        : `Have you re-entered ${sym}? There is no open booking to give back, so OK just highlights RE.`;
    }
    s.confirm = {
      title: (k === "pb" ? "Profit booking" : "Re-entry") + " — " + sym,
      msg,
      ok: () => applyPbre(sym, k),
    };
    render();
  }
  function applyPbre(sym, k) {
    const pbre = { ...s.pbre };
    const pb = pbCount(sym), re = reCount(sym);
    let msg = "";
    if (k === "pb") {
      pbre[sym] = { pb: pb + 1, re };
      msg = `${sym}: booking ${pb + 1} recorded.`;
    } else if (pb > 0) {
      if (pb === 1) { delete pbre[sym]; msg = `${sym}: profit booked → re-entered — cycle complete, PB/RE reset.`; }
      else { pbre[sym] = { pb: pb - 1, re: re + 1 }; msg = `${sym}: re-entered — ${pb - 1} booking${pb - 1 === 1 ? "" : "s"} still open.`; }
    } else if (re) {
      delete pbre[sym];
    } else {
      pbre[sym] = { pb: 0, re: 1 };
    }
    if (pbre[sym] && !pbre[sym].pb && !pbre[sym].re) delete pbre[sym];
    savePbre(pbre, msg);
  }
  function askResetPbre(sym) {
    const pb = pbCount(sym), re = reCount(sym);
    s.confirm = {
      title: "Reset PB / RE — " + sym,
      msg: `Clear the PB and RE marks on ${sym}? ${pb
        ? `${pb} open booking${pb === 1 ? "" : "s"}`
        : "The RE mark"} and ${re ? `${re} recorded re-entr${re === 1 ? "y" : "ies"}` : "no re-entries"} will be forgotten. It does not undo any trade — only the marks.`,
      ok: () => {
        const pbre = { ...s.pbre };
        delete pbre[sym];
        savePbre(pbre, `${sym}: PB/RE marks cleared.`);
      },
    };
    render();
  }
  function savePbre(pbre, msg) {
    const prev = s.pbre, url = s.syncUrl;
    gatedSave("The PB / RE change",
      () => { s.pbre = pbre; T.savePbre(pbre);
              return () => { s.pbre = prev; T.savePbre(prev); }; },
      (token) => T.postAuth(url, "pbre", pbre, token),
      () => { if (msg) flash(msg); });
  }

  /* ---------------- settings form ---------------- */
  /* A working copy: nothing here reaches the live settings until Save, and
     formOf is the one place that maps settings -> form, so the dirty check can
     rebuild the pristine copy and compare against it. */
  function formOf(set) {
    return {
      dataUrl: s.dataUrl, syncUrl: s.syncUrl,
      /* browser-local, and deliberately NOT part of `settings` — settings get
         POSTed to the bucket and the write password must never travel there */
      adminToken: T.loadToken(),
      horizon: set.horizon || "long", reEntryMode: set.reEntryMode || "base",
      gainPct: set.gainPct, highZonePct: set.highZonePct, lowZonePct: set.lowZonePct,
      trendFast: set.trendFast || 50, trendSlow: set.trendSlow || 150,
      alertChannel: set.alertChannel || "both",
      alertTypes: { ...(set.alertTypes || {}) },
      captionText: set.captionText === true,
      alertEmails: (set.alertEmails || []).join(", "),
      trend: set.weights.trend, momentum: set.weights.momentum,
      participation: set.weights.participation, relStrength: set.weights.relStrength, risk: set.weights.risk,
    };
  }
  const WEIGHTS = ["trend", "momentum", "participation", "relStrength", "risk"];
  const weightTotal = () => s.form ? WEIGHTS.reduce((a, k) => a + (+s.form[k] || 0), 0) : 100;
  function settingsDirty() {
    if (!s.form) return false;
    const was = formOf(s.settings);
    const fields = ["dataUrl", "syncUrl", "adminToken", "horizon", "reEntryMode", "gainPct",
      "highZonePct", "lowZonePct", "trendFast", "trendSlow", "alertChannel", "captionText",
      "alertEmails"].concat(WEIGHTS);
    /* compared as strings: a number input hands back "20" where the setting holds 20 */
    if (fields.some(k => String(s.form[k]) !== String(was[k]))) return true;
    return T.ALERT_KINDS.some(a => on(s.form.alertTypes, a.key) !== on(was.alertTypes, a.key));
  }
  function requestCloseSettings(intent) {
    if (settingsDirty()) { s.confirmClose = intent || "close"; return render(); }
    s.settingsOpen = false; s.listsOpen = intent === "lists"; render();
  }
  function saveSettings(after) {
    const f = s.form;
    const total = weightTotal();
    if (total !== 100) return flash("Score weights must sum to 100 (currently " + total + ").");
    if (+f.trendFast >= +f.trendSlow)
      return flash(`Trend EMAs: the fast one must be shorter than the slow one (got ${f.trendFast} and ${f.trendSlow}).`);
    /* a bad address must not be swallowed: cleanEmails would drop it silently
       and the alert would never arrive where the user thought it would */
    const typed = String(f.alertEmails || "").split(/[,\s]+/).filter(Boolean);
    const good = T.cleanEmails(f.alertEmails);
    if (typed.length > T.MAX_EXTRA_EMAILS)
      return flash(`At most ${T.MAX_EXTRA_EMAILS} extra email addresses (got ${typed.length}).`);
    const bad = typed.find(a => !good.includes(a.trim().toLowerCase()));
    if (bad) return flash("That does not look like an email address: " + bad);

    const next = {
      ...s.settings,
      gainPct: Math.max(5, Math.min(200, +f.gainPct || 20)),
      highZonePct: Math.max(0.5, Math.min(10, +f.highZonePct || 2)),
      lowZonePct: Math.max(2, Math.min(30, +f.lowZonePct || 10)),
      trendFast: +f.trendFast || 50, trendSlow: +f.trendSlow || 150,
      horizon: f.horizon, reEntryMode: f.reEntryMode,
      alertChannel: ["telegram", "email", "both"].includes(f.alertChannel) ? f.alertChannel : "both",
      alertTypes: Object.fromEntries(T.ALERT_KINDS.map(a => [a.key, on(f.alertTypes, a.key)])),
      captionText: f.captionText === true,
      alertEmails: good,
      weights: {
        trend: +f.trend, momentum: +f.momentum, participation: +f.participation,
        relStrength: +f.relStrength, risk: +f.risk,
      },
    };
    const prevSet = s.settings, prevCh = s.chCfg;
    const prevData = s.dataUrl, prevSync = s.syncUrl;
    const urlChanged = f.dataUrl !== s.dataUrl || f.syncUrl !== s.syncUrl;
    const chChanged = s.settings.horizon !== f.horizon;
    /* post to the endpoint in force AS THE SAVE STARTS: apply() may swap
       s.syncUrl underneath us, and the new address is not authorised yet */
    const postUrl = s.syncUrl;

    /* the Access field lives in this very form, so what was typed is offered as
       the candidate — the save that first enters the password authorises itself
       and never sees the prompt */
    gatedSave("Your settings change",
      () => {
        if (chChanged) {
          s.chCfg = { ...s.chCfg, span: T.HORIZON_PRESETS[f.horizon].span };
          T.saveCh(s.chCfg, s.vis);
          chartDirty = true;
        }
        T.saveSettings(next);
        T.lsSet(T.keys.DATA_KEY, f.dataUrl); T.lsSet(T.keys.SYNC_KEY, f.syncUrl);
        s.settings = next; s.dataUrl = f.dataUrl; s.syncUrl = f.syncUrl;
        s.settingsOpen = false; s.confirmClose = null;
        return () => {
          if (chChanged) { s.chCfg = prevCh; T.saveCh(prevCh, s.vis); chartDirty = true; }
          T.saveSettings(prevSet);
          T.lsSet(T.keys.DATA_KEY, prevData); T.lsSet(T.keys.SYNC_KEY, prevSync);
          s.settings = prevSet; s.dataUrl = prevData; s.syncUrl = prevSync;
          /* reopen on the same form so a rejected password does not cost the
             user everything they had just typed */
          s.settingsOpen = true; s.form = f;
        };
      },
      (token) => T.postAuth(postUrl, "settings", next, token),
      () => { if (urlChanged) load(); if (after) after(); },
      f.adminToken);
  }
  function reconnect() {
    const f = s.form;
    T.lsSet(T.keys.DATA_KEY, f.dataUrl); T.lsSet(T.keys.SYNC_KEY, f.syncUrl);
    s.dataUrl = f.dataUrl; s.syncUrl = f.syncUrl;
    T.pull(f.syncUrl, "pbre").then(pb => {
      if (pb) { s.pbre = pb; T.savePbre(pb); }
      load();
    });
  }
  function setTheme(t) {
    if (t === s.theme) return;
    s.theme = t; T.saveTheme(t);
    /* charts draw to canvas and cannot read CSS custom properties, so their
       palette has to be rebuilt by hand */
    teardownCharts();
    render();
  }

  /* ---------------- charts ---------------- */
  let charts = null, chartedSym = null, chartDirty = false, chartPending = {};
  const paneEl = {};
  function pane(k) {
    if (!paneEl[k]) {
      const d = document.createElement("div");
      d.className = "pane-canvas";
      paneEl[k] = d;
    }
    return paneEl[k];
  }
  function chartUrlFor(sym) {
    const base = (s.dataUrl || "").replace(/data\.json(\?.*)?$/, "");
    return base && base !== s.dataUrl ? base + T.chartObject(sym) : null;
  }
  /* One object per symbol, fetched the first time its chart is opened and kept
     for the session. A null result is cached too — componentless re-renders
     would otherwise re-request a symbol with no published bars forever. */
  function ensureChart(sym) {
    if (!sym) return;
    const store = s.chartStore || {};
    if (store[sym] !== undefined || chartPending[sym]) return;
    const url = chartUrlFor(sym);
    if (!url) return;
    chartPending[sym] = true;
    fetch(url + "?t=" + Date.now())
      .then(r => r.ok ? r.json() : null)
      .catch(() => null)
      .then(bars => {
        s.chartStore = Object.assign({}, s.chartStore, { [sym]: bars });
        delete chartPending[sym];
        chartDirty = true;
        render();
      });
  }
  function barsFor(sym) {
    const cs = s.chartStore;
    if (cs && cs[sym] && cs[sym].length) return cs[sym];
    const d = bySym(sym);
    return T.genBars(sym, d ? d.close : 100);
  }
  function teardownCharts() {
    if (!charts) return;
    try { ["price", "rsi", "macd"].forEach(k => charts[k] && charts[k].remove()); } catch (e) { }
    charts = null; chartedSym = null;
    /* the containers are reused, but their canvases went with the charts */
    Object.keys(paneEl).forEach(k => { paneEl[k].innerHTML = ""; });
  }
  function syncCharts() {
    const LWC = window.LightweightCharts;
    if (!LWC || !paneEl.price || !paneEl.price.isConnected) return;
    const CT = T.chartTheme;
    /* Every pane carries its own date axis. It used to be on the MACD pane
       alone, so switching RSI and MACD off left the price chart with no dates
       at all, and even with them on the reader had to trace a column down two
       panes to find out when a candle happened. */
    const opts = (h) => ({
      height: h, layout: { background: { color: "transparent" }, textColor: CT.text, fontSize: 11 },
      grid: { vertLines: { color: CT.grid }, horzLines: { color: CT.grid } },
      rightPriceScale: { borderColor: CT.border, minimumWidth: 72 },
      timeScale: { borderColor: CT.border, timeVisible: false, secondsVisible: false, visible: true },
      crosshair: {
        vertLine: { color: CT.cross, labelBackgroundColor: CT.accent },
        horzLine: { color: CT.cross, labelBackgroundColor: CT.accent },
      },
    });
    if (!charts) {
      const price = LWC.createChart(pane("price"), opts(340));
      const rsi = LWC.createChart(pane("rsi"), opts(150));
      const macd = LWC.createChart(pane("macd"), opts(150));
      const candle = price.addCandlestickSeries({
        upColor: CT.up, downColor: CT.down, wickUpColor: CT.up, wickDownColor: CT.down, borderVisible: false,
      });
      let syncing = false;
      const link = (src, others) => src.timeScale().subscribeVisibleLogicalRangeChange(r => {
        if (syncing || !r) return;
        syncing = true; others.forEach(o => o.timeScale().setVisibleLogicalRange(r)); syncing = false;
      });
      link(price, [rsi, macd]); link(rsi, [price, macd]); link(macd, [price, rsi]);
      [price, rsi, macd].forEach(c => c.subscribeCrosshairMove(onCross));
      charts = { price, rsi, macd, candle, ema: [], lines: [], chan: [], rsiSeries: [], macdSeries: [] };
      chartedSym = null;
    }
    [["price", 340], ["rsi", 150], ["macd", 150]].forEach(([k, h]) => {
      const w = paneEl[k].clientWidth;
      if (w > 0) charts[k].applyOptions({ width: w, height: h });
    });
    if (s.selected !== chartedSym || chartDirty) {
      drawChart(s.selected); chartedSym = s.selected; chartDirty = false;
    }
  }
  function drawChart(sym) {
    const LWC = window.LightweightCharts, c = charts;
    if (!c || !sym) return;
    const CT = T.chartTheme, cfg = s.chCfg, vis = s.vis;
    const bars = barsFor(sym), closes = bars.map(b => b.close);
    c.candle.setData(bars);
    const pl = vis.pline !== false;
    const cc = vis.candles !== false
      ? { upColor: CT.up, downColor: CT.down, wickUpColor: CT.up, wickDownColor: CT.down }
      : { upColor: "transparent", downColor: "transparent", wickUpColor: "transparent", wickDownColor: "transparent" };
    /* the last-price line carries its own colour: with candles hidden the
       series colour is transparent, which is what used to make the rule vanish */
    c.candle.applyOptions(Object.assign({
      priceLineVisible: pl, lastValueVisible: pl,
      priceLineColor: CT.accent, priceLineWidth: 1, priceLineStyle: LWC.LineStyle.Dashed,
    }, cc));

    c.ema.forEach(x => c.price.removeSeries(x)); c.ema = [];
    cfg.ema.forEach(e => {
      if (!vis[e.key]) return;
      const ev = T.emaArr(closes, e.period);
      const line = c.price.addLineSeries({ color: e.color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
      line.setData(bars.map((b, i) => ev[i] != null ? { time: b.time, value: +ev[i].toFixed(2) } : null).filter(Boolean));
      c.ema.push(line);
    });

    /* One markers array for the whole series: setMarkers REPLACES, so the
       crossover arrows and the channel entries have to be merged and sorted by
       time rather than set from two places. */
    const mk = [];
    if (vis.cross !== false) {
      const visE = cfg.ema.filter(e => vis[e.key]).sort((a, b) => a.period - b.period);
      if (visE.length === 2 && visE[0].period !== visE[1].period) {
        const eA = T.emaArr(closes, visE[0].period), eB = T.emaArr(closes, visE[1].period);
        for (let i = 1; i < bars.length; i++) {
          if (eA[i] == null || eB[i] == null || eA[i - 1] == null || eB[i - 1] == null) continue;
          if (eA[i - 1] <= eB[i - 1] && eA[i] > eB[i])
            mk.push({ time: bars[i].time, position: "belowBar", color: CT.up, shape: "arrowUp", text: visE[0].period + "↑" + visE[1].period });
          else if (eA[i - 1] >= eB[i - 1] && eA[i] < eB[i])
            mk.push({ time: bars[i].time, position: "aboveBar", color: CT.down, shape: "arrowDown", text: visE[0].period + "↓" + visE[1].period });
        }
      }
    }

    /* ---- regression channel ---- */
    c.chan.forEach(x => { try { c.price.removeSeries(x); } catch (e) { } }); c.chan = [];
    const ch = channelFor(sym);
    if (ch) {
      /* rails solid, the fit itself dashed: the mid-line is a description, not
         a level anything trades off */
      [[ch.upper, 1, LWC.LineStyle.Solid], [ch.mid, 1, LWC.LineStyle.Dashed],
       [ch.lower, 1, LWC.LineStyle.Solid]].forEach(([data, w, style]) => {
        const line = c.price.addLineSeries({
          color: CT.chan, lineWidth: w, lineStyle: style,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        line.setData(data);
        c.chan.push(line);
      });
      ch.entries.forEach(e => mk.push({
        time: e.time, position: "belowBar", color: CT.up, shape: "circle", text: "entry",
      }));
    }
    mk.sort((a, b) => a.time < b.time ? -1 : a.time > b.time ? 1 : 0);
    c.candle.setMarkers(mk);

    c.lines.forEach(l => { try { c.candle.removePriceLine(l); } catch (e) { } }); c.lines = [];
    if (vis.sr) {
      const cs = s.chartStore;
      const stored = cs && cs[sym] && cs[sym].length && cs[sym][cs[sym].length - 1].sr;
      (stored || T.autoSR(bars)).forEach(lv => c.lines.push(c.candle.createPriceLine({
        price: +(+lv.price).toFixed(2), color: CT.sr, lineWidth: 1, lineStyle: LWC.LineStyle.Dotted,
        axisLabelVisible: true, title: (lv.kind === "support" ? "S " : "R ") + (+lv.price).toFixed(2),
      })));
    }
    if (vis.w52) {
      const look = bars.slice(-252);
      let hi = -Infinity, lo = Infinity;
      for (const b of look) { if (b.high > hi) hi = b.high; if (b.low < lo) lo = b.low; }
      if (isFinite(hi) && isFinite(lo)) {
        [[hi, "52W H"], [lo, "52W L"]].forEach(([p, t]) => c.lines.push(c.candle.createPriceLine({
          price: +p.toFixed(2), color: CT.gold, lineWidth: 1, lineStyle: LWC.LineStyle.Solid,
          axisLabelVisible: true, title: t,
        })));
      }
    }

    c.rsiSeries.forEach(x => c.rsi.removeSeries(x)); c.rsiSeries = [];
    if (vis.rsi) {
      const rv = T.rsiArr(closes, cfg.rsi.period);
      [[cfg.rsi.ob, 100, CT.obZone], [cfg.rsi.os, 0, CT.osZone]].forEach(([base, val, col]) => {
        const b = c.rsi.addBaselineSeries({
          baseValue: { type: "price", price: base },
          topFillColor1: col, topFillColor2: col, bottomFillColor1: col, bottomFillColor2: col,
          topLineColor: "rgba(0,0,0,0)", bottomLineColor: "rgba(0,0,0,0)",
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        b.setData(bars.map(x => ({ time: x.time, value: val })));
        c.rsiSeries.push(b);
      });
      const line = c.rsi.addLineSeries({ color: cfg.rsi.color, lineWidth: 2, priceFormat: { type: "price", precision: 1, minMove: 0.1 } });
      line.setData(bars.map((b, i) => rv[i] != null ? { time: b.time, value: +rv[i].toFixed(1) } : { time: b.time }));
      c.rsiSeries.push(line);
      [[cfg.rsi.ob, CT.downA], [cfg.rsi.os, CT.upA]].forEach(([val, col]) => {
        const g = c.rsi.addLineSeries({ color: col, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, lastValueVisible: false, crosshairMarkerVisible: false });
        g.setData(bars.map(b => ({ time: b.time, value: val })));
        c.rsiSeries.push(g);
      });
    }

    c.macdSeries.forEach(x => c.macd.removeSeries(x)); c.macdSeries = [];
    if (vis.macd) {
      const m = T.macdArr(closes, cfg.macd.fast, cfg.macd.slow, cfg.macd.signal);
      const hist = c.macd.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
      hist.setData(bars.map((b, i) => m.hist[i] != null
        ? { time: b.time, value: +m.hist[i].toFixed(4), color: m.hist[i] >= 0 ? CT.histUp : CT.histDown }
        : { time: b.time }));
      const line = c.macd.addLineSeries({ color: cfg.macd.macdColor, lineWidth: 2, lastValueVisible: false });
      line.setData(bars.map((b, i) => m.line[i] != null ? { time: b.time, value: +m.line[i].toFixed(4) } : { time: b.time }));
      const sig = c.macd.addLineSeries({ color: cfg.macd.signalColor, lineWidth: 2, lastValueVisible: false });
      sig.setData(bars.map((b, i) => m.signal[i] != null ? { time: b.time, value: +m.signal[i].toFixed(4) } : { time: b.time }));
      c.macdSeries.push(hist, line, sig);
    }
    applySpan();
  }
  function applySpan() {
    if (!charts || !s.selected) return;
    const bars = barsFor(s.selected);
    if (!bars.length) return;
    const from = Math.max(0, bars.length - T.spanDays(s.chCfg.span, bars.length));
    charts.price.timeScale().setVisibleRange({ from: bars[from].time, to: bars[bars.length - 1].time });
  }
  /* The bars the regression channel is fitted to: the visible span, so the
     channel describes the window you are actually looking at. */
  function spanBars(bars) {
    return bars.slice(Math.max(0, bars.length - T.spanDays(s.chCfg.span, bars.length)));
  }
  function channelFor(sym) {
    if (!sym || !s.vis.regch) return null;
    const bars = barsFor(sym);
    if (!bars.length) return null;
    return T.regChannel(spanBars(bars), s.chCfg.regch);
  }
  /* Written straight into the DOM rather than through render(): the crosshair
     fires on every mouse move, and repainting the drawer that often would
     fight the chart for the frame. */
  function onCross(param) {
    const el = document.getElementById("readout");
    if (!el) return;
    const sym = s.selected;
    if (!param.time || !sym) { el.textContent = ""; return; }
    const bars = barsFor(sym);
    const i = bars.findIndex(x => x.time === param.time);
    if (i < 0) return;
    const bar = bars[i], closes = bars.map(b => b.close);
    const r = T.rsiArr(closes, s.chCfg.rsi.period);
    const m = T.macdArr(closes, s.chCfg.macd.fast, s.chCfg.macd.slow, s.chCfg.macd.signal);
    el.textContent = `${bar.time} · O ${bar.open} H ${bar.high} L ${bar.low} C ${bar.close}`
      + ` · Vol ${(bar.volume / 1e6).toFixed(2)}M`
      + ` · RSI ${r[i] != null ? r[i].toFixed(1) : "–"}`
      + ` · MACD ${m.line[i] != null ? m.line[i].toFixed(3) : "–"} / ${m.signal[i] != null ? m.signal[i].toFixed(3) : "–"}`;
  }
  /* At most two EMAs at once: the crossover markers are only meaningful for a
     pair, and the oldest choice drops out rather than being refused. */
  function toggleEye(key) {
    const vis = { ...s.vis };
    if (T.EMA_KEYS.includes(key)) {
      if (vis[key]) { vis[key] = false; emaOrder = emaOrder.filter(k => k !== key); }
      else {
        vis[key] = true; emaOrder.push(key);
        while (emaOrder.length > 2) { const drop = emaOrder.shift(); vis[drop] = false; }
      }
    } else vis[key] = !vis[key];
    s.vis = vis; T.saveCh(s.chCfg, vis);
    chartDirty = true; render();
  }
  function setCfg(mutate) {
    const cfg = JSON.parse(JSON.stringify(s.chCfg));
    mutate(cfg);
    s.chCfg = cfg; T.saveCh(cfg, s.vis);
    chartDirty = true; render();
  }

  /* ---------------- masthead ---------------- */
  function mastVals() {
    const idxSyms = [...new Set(s.lists.filter(l => l.type === "index").flatMap(l => l.symbols))];
    const rank = (sym) => {
      const i = TAPE_ORDER.indexOf(sym);
      return i === -1 ? TAPE_ORDER.length + idxSyms.indexOf(sym) : i;
    };
    /* Every index in the list is quoted — the tape scrolls sideways rather than
       truncating, because a tape that silently drops holdings is worse than one
       that needs a swipe. SPY and VOO share a proxy name, so a name claimed by
       more than one ticker carries its ticker to keep the two rows apart. */
    const held = idxSyms.map(bySym).filter(Boolean)
      .sort((a, b) => rank(a.symbol) - rank(b.symbol));
    const nameCount = {};
    held.forEach(d => {
      const n = PROXY[d.symbol] || d.symbol.replace(/\/USD$/, "");
      nameCount[n] = (nameCount[n] || 0) + 1;
    });
    const tape = held.map(d => {
      const n = PROXY[d.symbol] || d.symbol.replace(/\/USD$/, "");
      return {
        name: nameCount[n] > 1 ? n + " " + d.symbol : n, sym: d.symbol,
        close: T.fmt(d.close), chg: pct(d.change_pct, 1), ink: T.dirColor(d.change_pct),
      };
    });

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
      scope: s.payload ? `US market · ${(s.payload.symbols || []).length} symbols` : "US market",
    };
  }

  /* ---------------- the sentence at the top ---------------- */
  function hero(all, action, reentry) {
    const total = all.length;
    const bull = all.filter(d => T.trendOf(d, s.settings) === "bull").length;
    const up = all.filter(d => (d.change_pct || 0) > 0).length;
    const act = activeList();

    if (!total) {
      return {
        eyebrow: act ? act.name : "No list",
        headline: act && act.symbols.length ? "Nothing matches the current filters." : "This list is empty.",
        standfirst: act && act.symbols.length
          ? "Clear the search, sector or trend filter to see the book again."
          : "Add a ticker above, or create a list under Manage lists.",
      };
    }

    const mix = T.sectorMix(s.lists, s.payload).filter(m => m.count > 1);
    const lead = mix.slice().sort((a, b) => b.move - a.move)[0];
    const ratio = up / total;
    const tone = ratio >= 0.6 ? "firm" : ratio <= 0.4 ? "heavy" : "mixed";

    let decision;
    if (action.length && reentry.length)
      decision = `${words(action.length)} in the exit review zone, ${words(reentry.length)} on re-entry watch.`;
    else if (action.length)
      decision = `${words(action.length)} ${action.length === 1 ? "name has" : "names have"} reached the exit review zone.`;
    else if (reentry.length)
      decision = `${words(reentry.length)} ${reentry.length === 1 ? "name is" : "names are"} on re-entry watch.`;
    else decision = "nothing needs a decision today.";

    const parts = [];
    if (action.length) {
      const syms = action.slice(0, 3).map(d => d.symbol);
      parts.push(`${listOf(syms)} ${syms.length === 1 ? "sits" : "sit"} inside the ${T.fmt(s.settings.highZonePct, 0)}% high zone.`);
    }
    const confirmed = reentry.filter(d => d.base_status === "confirmed").map(d => d.symbol);
    if (confirmed.length)
      parts.push(`${listOf(confirmed.slice(0, 3))} ${confirmed.length === 1 ? "has a confirmed base" : "have confirmed bases"} and ${confirmed.length === 1 ? "is" : "are"} worth a look for re-entry.`);
    else if (reentry.length)
      parts.push(`${words(reentry.length)} ${reentry.length === 1 ? "name sits" : "names sit"} near the 52-week low without a confirmed base — falling-knife risk until one forms.`);
    if (!parts.length) parts.push("Every holding is between its zones — the rules are armed but silent.");

    return {
      eyebrow: `${bull} of ${total} symbols in uptrend`,
      headline: lead ? `${lead.name} leads a ${tone} tape — ${decision}` : cap(decision),
      /* each sentence is capitalised on its own — the second one starts with a
         spelled-out count, which would otherwise read as lowercase mid-line */
      standfirst: parts.map(cap).join(" "),
    };
  }

  /* ---------------- cards ---------------- */
  const markBtn = (sym, k) => {
    const n = k === "pb" ? pbCount(sym) : reCount(sym);
    const set = n > 0;
    const ink = k === "pb" ? "var(--ta-warn)" : "var(--color-accent-300)";
    const bd = k === "pb" ? "var(--ta-warn)" : "var(--color-accent)";
    /* the count rides the corner of the button rather than sitting inside the
       label, so PB and RE keep the same width whatever the tally */
    const badge = k === "pb" && n > 0
      ? `<i class="mark-n" style="background:${ink}">${n}</i>` : "";
    const title = k === "pb"
      ? (set ? `${n} open booking${n === 1 ? "" : "s"} — click to book another` : "Profit booked")
      : (pbCount(sym) > 0 ? "Re-entered — gives back one booking" : "Re-entered");
    return `<button class="mark" data-act="pbre" data-sym="${esc(sym)}" data-k="${k}"
      ${set ? `style="color:${ink};border-color:${bd}"` : ""}
      title="${esc(title)}">${k.toUpperCase()}${badge}</button>`;
  };
  /* The reset only appears once there is something to reset — a third button on
     every row would be noise on the 40-odd names that carry no marks. */
  const marks = (sym) => markBtn(sym, "pb") + markBtn(sym, "re") + (hasMarks(sym)
    ? `<button class="mark reset" data-act="pbreset" data-sym="${esc(sym)}"
        title="Reset PB / RE on ${esc(sym)}" aria-label="Reset PB and RE on ${esc(sym)}">↺</button>` : "");

  function cardHTML(d, kind) {
    const score = T.scoreOf(d, s.settings);
    const tag = T.zoneTag(d, s.settings);
    const act = T.action(d, s.settings);
    const members = s.lists.filter(l => l.symbols.includes(d.symbol)).map(l => l.name).join(" · ");
    return `
      <div class="card">
        <div class="card-top">
          <div>
            <div class="card-sym">${esc(d.symbol)}</div>
            <div class="card-sector">${esc(d.sector || "—")}${members ? " · " + esc(members) : ""}</div>
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
        <div class="card-foot">
          <button class="btn sm" data-act="open" data-sym="${esc(d.symbol)}">Detail</button>
          <button class="btn sm" data-act="chart" data-sym="${esc(d.symbol)}">Chart</button>
          <span class="spacer"></span>
          ${marks(d.symbol)}
        </div>
      </div>`;
  }

  function miniHTML(d) {
    const score = T.scoreOf(d, s.settings);
    return `
      <div class="card mini">
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
        <p class="why">${esc(T.why(d, s.settings))}</p>
        <div class="card-foot">
          <button class="btn sm" data-act="open" data-sym="${esc(d.symbol)}">Detail</button>
          <button class="btn sm" data-act="chart" data-sym="${esc(d.symbol)}">Chart</button>
          <span class="spacer"></span>
          ${marks(d.symbol)}
        </div>
      </div>`;
  }

  /* ---------------- all trends ---------------- */
  const COLS = [
    { key: "symbol", lbl: "Symbol", l: true },
    { key: "sector", lbl: "Sector", l: true, hide: true },
    { key: "score", lbl: "Score" },
    { key: "close", lbl: "Close" },
    { key: "change_pct", lbl: "Change" },
    { key: "pos52", lbl: "Off 52w high", hide: true },
    { key: "trend", lbl: "Trend" },
    { key: "marks", lbl: "", noSort: true },
  ];
  const TREND_GLYPH = { bull: "▲", bear: "▼", mixed: "◆" };
  /* The filter chips say Bullish / Bearish because they name a set; the column
     says Uptrend / Downtrend because it describes one name's state. Same
     TA.trendOf underneath — only the wording differs. */
  const TREND_STATE = { bull: "Uptrend", bear: "Downtrend", mixed: "Mixed" };
  const trendInk = (t) => t === "bull" ? T.ink.up : t === "bear" ? T.ink.down : T.ink.muted;

  function tableHTML(all) {
    if (!all.length) return `<p class="empty">Nothing in this list matches the current filters.</p>`;
    const head = COLS.map(c => {
      const sorted = s.sortKey === c.key;
      const arrow = sorted ? (s.sortDir < 0 ? " ↓" : " ↑") : "";
      const label = esc(c.lbl) + arrow;
      return `<th class="${c.l ? "l" : ""}${c.hide ? " hide-s" : ""}" data-sorted="${sorted}">${
        c.noSort ? label : `<button data-act="sort" data-k="${c.key}">${label}</button>`}</th>`;
    }).join("");
    const body = all.map(d => {
      if (d.pending) {
        return `<tr>
          <td class="sym">${esc(d.symbol)}<span class="sub">awaiting data</span></td>
          <td class="sector hide-s" style="text-align:left">—</td>
          <td>—</td><td>—</td><td>—</td><td class="hide-s">—</td><td>—</td>
          <td><button class="mark" data-act="drop" data-sym="${esc(d.symbol)}">Remove</button></td>
        </tr>`;
      }
      const score = T.scoreOf(d, s.settings), tr = T.trendOf(d, s.settings);
      const ph = T.pctFromHigh(d), zone = T.zoneOf(d, s.settings);
      const members = s.lists.filter(l => l.symbols.includes(d.symbol)).map(l => l.name).join(" · ");
      return `<tr>
        <td><button class="sym" data-act="open" data-sym="${esc(d.symbol)}">${esc(d.symbol)}<span class="sub">${esc(members || d.sector || "—")}</span></button></td>
        <td class="sector hide-s" style="text-align:left">${esc(d.sector || "—")}</td>
        <td class="num" style="color:${T.scoreColor(score)}">${T.fmt(score, 0)}</td>
        <td class="num">${esc(T.fmt(d.close))}</td>
        <td class="num" style="color:${T.dirColor(d.change_pct)}">${esc(pct(d.change_pct, 1))}</td>
        <td class="num hide-s" style="color:${zone === "high" ? T.ink.warn : zone === "low" ? "var(--color-accent-300)" : "var(--color-neutral-500)"}">${
          ph == null ? "—" : esc(T.fmt(Math.abs(ph) < 0.05 ? 0 : ph, 1)) + "%"}</td>
        <td><span class="trend" style="color:${trendInk(tr)}">${TREND_GLYPH[tr]} ${esc(TREND_STATE[tr])}</span></td>
        <td><span class="marks">${marks(d.symbol)}</span></td>
      </tr>`;
    }).join("");
    return `<div style="overflow-x:auto"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  /* ---------------- rail ---------------- */
  function railHTML(all) {
    const withChg = all.filter(d => d.change_pct != null);
    const best = withChg.slice().sort((a, b) => b.change_pct - a.change_pct)[0];
    const worst = withChg.slice().sort((a, b) => a.change_pct - b.change_pct)[0];
    const up = withChg.filter(d => d.change_pct > 0).length;
    const sum = s.payload ? T.summary(s.lists, s.payload) : null;

    const summary = `
      <div>
        <div class="rail-label">Session summary${sum ? " · " + esc(sum.date) : ""}</div>
        <div class="panel">
          <div class="kv"><span class="k">Top up</span><span class="v num">${best ? esc(best.symbol) + " " + esc(pct(best.change_pct, 1)) : "—"}</span></div>
          <div class="kv"><span class="k">Top down</span><span class="v num">${worst ? esc(worst.symbol) + " " + esc(pct(worst.change_pct, 1)) : "—"}</span></div>
          <div class="kv"><span class="k">Breadth</span><span class="v num">${withChg.length ? up + " / " + withChg.length + " up" : "—"}</span></div>
          ${sum ? `<div class="kv"><span class="k">Core</span><span class="v">${esc(sum.breadth)}</span></div>` : ""}
        </div>
      </div>`;

    /* data.json carries only crosses detected on the LATEST bar and keeps no
       history, so there is nothing to age these by — every row is today's. An
       alert history file is a backend change, noted in OPS.md. */
    const al = (s.payload && s.payload.alerts) || [];
    const alerts = `
      <div>
        <div class="rail-label">Recent alerts</div>
        ${al.length ? al.map(a => `
          <button class="alert-row" data-act="open" data-sym="${esc(a.symbol)}">
            <span class="arrow" style="color:${a.dir === "bull" ? T.ink.up : T.ink.down}">${a.dir === "bull" ? "↑" : "↓"}</span>
            <span class="txt"><b>${esc(a.symbol)}</b> ${esc(a.type)}${a.detail ? " — " + esc(a.detail.toLowerCase()) : ""}</span>
            <span class="age">today</span>
          </button>`).join("")
          : `<p class="rail-note">No EMA 50 / 150 crosses on the latest bar.</p>`}
      </div>`;

    const mix = s.payload ? T.sectorMix(s.lists, s.payload) : [];
    const sectors = `
      <div>
        <div class="rail-label">Portfolio sectors</div>
        ${mix.length ? mix.map(m => `
          <div class="sect-row" title="${esc(m.hover || m.count + " held")}">
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

  /* ---------------- drawer ---------------- */
  function detailHTML(d) {
    const score = T.scoreOf(d, s.settings);
    const zone = T.zoneOf(d, s.settings);
    const ph = T.pctFromHigh(d), pl = T.pctFromLow(d), p52 = T.pos52(d);
    const comps = d.components || {};
    const rz = T.rsiZone(d.rsi14);
    const trend = T.trendOf(d, s.settings);
    const act = T.action(d, s.settings);

    let zoneLabel = "No action zone", zoneInk = "var(--color-neutral-500)";
    if (p52 == null) { zoneLabel = "No 52-week data"; zoneInk = "var(--color-neutral-600)"; }
    else if (zone === "high") { zoneLabel = "Exit review zone"; zoneInk = T.ink.warn; }
    else if (zone === "low") {
      if (s.settings.reEntryMode === "near_low") { zoneLabel = "Re-entry watch (near-low rule)"; zoneInk = "var(--color-accent-300)"; }
      else if (d.base_status === "confirmed") { zoneLabel = "Base confirmed — re-entry candidate"; zoneInk = T.ink.up; }
      else if (d.base_status === "forming") { zoneLabel = `Base forming (${d.base_score != null ? d.base_score + "/5" : "—"}) — wait`; zoneInk = "var(--color-accent-300)"; }
      else { zoneLabel = "Near low, no base — falling-knife risk"; zoneInk = T.ink.down; }
    }

    const compMeta = [["Trend", "trend"], ["Momentum", "momentum"], ["Volume", "participation"],
      ["Rel. strength", "rel_strength"], ["Risk adj.", "risk_adj"]];
    const stats = [
      ["From 52w high", ph == null ? "—" : T.fmt(Math.abs(ph) < 0.05 ? 0 : ph, 1) + "%", zone === "high" ? T.ink.warn : "var(--color-text)"],
      ["From 52w low", pl == null ? "—" : "+" + T.fmt(pl, 1) + "%", zone === "low" ? "var(--color-accent-300)" : "var(--color-text)"],
      ["RSI (14)", T.fmt(d.rsi14, 1) + " · " + rz.toLowerCase(), rz === "Overbought" ? T.ink.down : rz === "Oversold" ? T.ink.up : "var(--color-text)"],
      ["MACD histogram", T.signed(d.macd_hist), d.macd_hist >= 0 ? T.ink.up : T.ink.down],
      ["EMA 20 / 50 / 200", [d.ema20, d.ema50, d.ema200].map(x => x == null ? "—" : T.fmt(x)).join(" · "), "var(--color-text)"],
      ["Rel. volume", d.rel_volume != null ? T.fmt(d.rel_volume) + "×" : "—", "var(--color-text)"],
    ];

    return `
      <div>
        <div class="block-label">Verdict</div>
        <div class="tag" style="display:inline-block;color:${zoneInk};border-color:${zoneInk}">${esc(zoneLabel)}</div>
        <p class="why" style="margin-top:12px">${esc(T.why(d, s.settings))}</p>
        ${act ? `<div class="do" style="margin-top:12px"><i>↳</i><span style="color:${act.ink}">${esc(act.text)}</span></div>` : ""}
      </div>

      <div>
        <div class="block-label">Score ${T.fmt(score, 0)} / 100</div>
        ${compMeta.map(([label, key]) => {
          const v = comps[key] != null ? Math.max(0, Math.min(100, comps[key])) : null;
          return `<div class="comp-row">
            <span>${esc(label)}</span>
            <span class="comp-bar"><i style="width:${v == null ? 0 : v}%;background:${v == null ? "var(--color-neutral-800)" : T.scoreColor(v)}"></i></span>
            <span class="v num">${v == null ? "—" : Math.round(v)}</span>
          </div>`;
        }).join("")}
      </div>

      <div>
        <div class="block-label">52-week position</div>
        <div class="range">
          <div class="range-track"><i style="left:calc(${(p52 == null ? 50 : p52).toFixed(1)}% - 1.5px)"></i></div>
          <div class="range-ends">
            <span>${d.low_252 != null ? "$" + esc(T.fmt(d.low_252)) + " low" : "low"}</span>
            <span>${d.high_252 != null ? "$" + esc(T.fmt(d.high_252)) + " high" : "high"}</span>
          </div>
        </div>
      </div>

      <div>
        <div class="block-label">Signals</div>
        <div class="chips">${T.signalChips(d, s.settings).map(c =>
          `<span class="tag" style="color:${c.ink};border-color:${c.border}">${esc(c.text)}</span>`).join("")}</div>
      </div>

      <div>
        <div class="block-label">Stats</div>
        <div class="stats">${stats.map(([k, v, ink]) =>
          `<div class="stat"><div class="k">${esc(k)}</div><div class="v num" style="color:${ink}">${esc(v)}</div></div>`).join("")}</div>
      </div>

      <div>
        <div class="block-label">Price levels</div>
        ${T.ladder(d).map(l => `
          <div class="ladder-row" style="background:${l.cur ? "color-mix(in srgb, var(--color-accent-900) 60%, transparent)" : "transparent"};border-radius:${l.cur ? "8px" : "0"}">
            <span style="color:${l.cur ? "var(--color-text)" : "var(--color-neutral-500)"}">${esc(l.name)}${
              l.tag ? `<span class="tag-min" style="color:${l.tagInk}">${esc(l.tag)}</span>` : ""}</span>
            <span class="num" style="color:${l.cur ? "var(--color-accent-300)" : "var(--color-text)"}">${esc(l.value)}</span>
            <span class="d num">${esc(l.dist)}</span>
          </div>`).join("")}
      </div>

      <div>
        <div class="block-label">Lists</div>
        <div class="chips">${s.lists.map(l => {
          const inIt = l.symbols.includes(d.symbol);
          return `<button class="tag" data-act="member" data-sym="${esc(d.symbol)}" data-id="${esc(l.id)}"
            style="color:${inIt ? "var(--color-text)" : "var(--color-neutral-500)"};border-color:${inIt ? "var(--color-accent)" : "var(--color-neutral-800)"};border-style:${inIt ? "solid" : "dashed"}">
            ${inIt ? "✓" : "+"} ${esc(l.name)}</button>`;
        }).join("")}</div>
      </div>`;
  }

  /* The channel's reading, in words and one number.
     It names the fit window on purpose: the channel is fitted to the visible
     span, so the same day can sit low in a 3Y channel and mid-way up a Max one.
     Saying "fitted to 756 bars · 3Y" is what stops that being a trap. */
  function chanPanel(d) {
    if (!s.vis.regch) return "";
    const ch = channelFor(d.symbol);
    if (!ch) return `
      <div>
        <div class="block-label">Regression channel</div>
        <p class="rail-note">Needs at least 30 bars in the span to fit a channel.</p>
      </div>`;
    const z = T.regZone(ch);
    const pos = ch.posNow;
    /* the marker is clamped to the track; the number below it is not, so a
       breakout still reads as 112% rather than pinning silently at 100% */
    const clamped = Math.max(0, Math.min(1, pos));
    const last = ch.entries.length ? ch.entries[ch.entries.length - 1] : null;
    const stats = [
      ["Position in channel", T.fmt(pos * 100, 0) + "%", z.ink],
      ["Trend rate", (ch.annualPct >= 0 ? "+" : "−") + T.fmt(Math.abs(ch.annualPct), 1) + "% / yr",
        ch.rising ? T.ink.up : T.ink.down],
      ["Entries in window", String(ch.entries.length), "var(--color-text)"],
      ["Last entry", last ? last.time : "—", last ? "var(--color-accent-300)" : "var(--color-neutral-600)"],
    ];
    return `
      <div>
        <div class="block-label">Regression channel · fitted to ${ch.bars} bars · ${esc(s.chCfg.span)}</div>
        <div class="tag" style="display:inline-block;color:${z.ink};border-color:${z.ink}">${esc(z.label)}</div>
        <div class="range" style="margin-top:14px">
          <div class="range-track">
            <i style="left:calc(${(clamped * 100).toFixed(1)}% - 1.5px);background:${z.ink}"></i>
          </div>
          <div class="range-ends">
            <span>$${esc(T.fmt(ch.lowerNow))} lower rail</span>
            <span>$${esc(T.fmt(ch.upperNow))} upper rail</span>
          </div>
        </div>
        <div class="stats" style="margin-top:16px">${stats.map(([k, v, ink]) =>
          `<div class="stat"><div class="k">${esc(k)}</div><div class="v num" style="color:${ink}">${esc(v)}</div></div>`).join("")}</div>
        <p class="rail-note" style="margin-top:12px">Entry when price is in the lowest
          ${esc(T.fmt(s.chCfg.regch.entryPct, 0))}% of a rising channel, at most once per
          ${esc(String(s.chCfg.regch.cooldown))} bars. Rails sit ${esc(T.fmt(s.chCfg.regch.k, 1))}
          standard deviations either side of the fit, so the width is this symbol's own
          dispersion — not a fixed percentage.</p>
      </div>`;
  }

  function chartHTML(d) {
    const cfg = s.chCfg;
    const eyes = [
      { key: "candles", label: "Candles", swatch: T.chartTheme.up },
      ...cfg.ema.map(e => ({ key: e.key, label: e.label, swatch: e.color })),
      { key: "regch", label: "Reg. channel", swatch: T.chartTheme.chan },
      { key: "sr", label: "S/R", swatch: T.chartTheme.sr },
      { key: "w52", label: "52W H/L", swatch: T.chartTheme.gold },
      { key: "pline", label: "Last price", swatch: "var(--color-neutral-600)" },
      { key: "cross", label: "Crossovers", swatch: T.chartTheme.up },
      { key: "rsi", label: "RSI pane", swatch: cfg.rsi.color },
      { key: "macd", label: "MACD pane", swatch: cfg.macd.macdColor },
    ];
    const emaNote = [cfg.ema.filter(e => s.vis[e.key]).map(e => e.label).join(" · "),
      s.vis.sr ? "S/R" : "", s.vis.w52 ? "52W H/L" : ""].filter(Boolean).join(" · ");

    const cfgRows = [
      ...cfg.ema.map((e, i) => ({ label: e.label, color: e.color, ck: "ema" + i,
        nums: [["period", e.period, "emaP" + i]] })),
      { label: "Channel", color: T.chartTheme.chan, ck: "regch", noColor: true,
        nums: [["± s.d.", cfg.regch.k, "rcK"], ["entry %", cfg.regch.entryPct, "rcE"], ["cooldown", cfg.regch.cooldown, "rcC"]] },
      { label: "RSI", color: cfg.rsi.color, ck: "rsi", nums: [["period", cfg.rsi.period, "rsiP"]] },
      { label: "MACD", color: cfg.macd.macdColor, color2: cfg.macd.signalColor, ck: "macd",
        nums: [["fast", cfg.macd.fast, "mF"], ["slow", cfg.macd.slow, "mS"], ["signal", cfg.macd.signal, "mG"]] },
    ];

    return `
      <div>
        <div class="sec-tools" style="margin:0 0 14px">
          <div class="seg sm">${T.SPANS.map(sp =>
            `<button data-act="span" data-k="${sp}" aria-pressed="${cfg.span === sp}">${sp}</button>`).join("")}</div>
          <span class="spacer" style="margin-left:auto"></span>
          <button class="btn sm" data-act="cfgtoggle">${s.chartCfgOpen ? "Hide indicator settings" : "Indicator settings"}</button>
        </div>
        <div class="eyes">${eyes.map(e =>
          `<button class="eye" data-act="eye" data-k="${e.key}" aria-pressed="${!!s.vis[e.key]}">
            <i style="background:${e.swatch}"></i>${esc(e.label)}</button>`).join("")}</div>
        ${s.chartCfgOpen ? cfgRows.map(r => `
          <div class="cfg-row">
            <span class="nm">${esc(r.label)}</span>
            ${r.noColor
              /* the channel draws in the theme's own colour, so there is no
                 stored colour for a picker to write to */
              ? `<i class="cfg-swatch" style="background:${r.color}" aria-hidden="true"></i>`
              : `<input type="color" value="${esc(r.color)}" data-act="color" data-k="${r.ck}">`}
            ${r.color2 ? `<input type="color" value="${esc(r.color2)}" data-act="color2" data-k="${r.ck}">` : ""}
            ${r.nums.map(([lbl, val, key]) =>
              `<label>${esc(lbl)}<input class="field" type="number" step="${key === "rcK" ? "0.1" : "1"}" value="${esc(val)}" data-act="cfgnum" data-k="${key}"></label>`).join("")}
          </div>`).join("") : ""}
      </div>

      ${chanPanel(d)}

      <div>
        <div class="pane">
          <p class="pane-note">Price${emaNote ? " · " + esc(emaNote) : ""}</p>
          <div id="slot-price"></div>
        </div>
        ${s.vis.rsi ? `<div class="pane">
          <p class="pane-note">RSI ${cfg.rsi.period}-period${d.rsi14 != null ? ` · now ${esc(T.fmt(d.rsi14, 1))} · ${esc(T.rsiZone(d.rsi14).toLowerCase())}` : ""}</p>
          <div id="slot-rsi"></div>
        </div>` : ""}
        ${s.vis.macd ? `<div class="pane">
          <p class="pane-note">MACD ${cfg.macd.fast}/${cfg.macd.slow}/${cfg.macd.signal}${d.macd_hist != null ? ` · histogram ${esc(T.signed(d.macd_hist))}` : ""}</p>
          <div id="slot-macd"></div>
        </div>` : ""}
        <p class="readout" id="readout"></p>
      </div>`;
  }

  function drawerHTML() {
    const d = s.selected ? bySym(s.selected) : null;
    if (!s.drawerOpen || !d) return "";
    const isChart = s.drawerTab === "chart";
    const tab = (k, label) => `<button class="tab" data-act="dtab" data-k="${k}" aria-selected="${s.drawerTab === k}">${label}</button>`;
    return `
      <div class="scrim" data-act="closedrawer"></div>
      <aside class="drawer" role="dialog" aria-label="${esc(d.symbol)} detail">
        <div class="dr-head">
          <div class="dr-top">
            <div>
              <div class="dr-sym">${esc(d.symbol)}</div>
              <div class="dr-sector">${esc(d.sector || "—")}</div>
            </div>
            <div class="dr-px">
              <b class="num">$${esc(T.fmt(d.close))}</b>
              <span class="num" style="color:${T.dirColor(d.change_pct)}">${d.change_pct >= 0 ? "▲" : "▼"} ${esc(T.signed(d.change))} (${esc(T.fmt(d.change_pct))}%) today</span>
            </div>
            <button class="icon-btn" data-act="closedrawer" title="Close">✕</button>
          </div>
          <div class="dr-tabs">${tab("detail", "Detail")}${tab("chart", "Chart")}</div>
        </div>
        <div class="dr-body">${isChart ? chartHTML(d) : detailHTML(d)}</div>
        <div class="dr-foot">
          ${marks(d.symbol)}
          <span style="margin-left:auto;font-size:12.5px;color:var(--color-neutral-600)">
            ${isChart ? "Hover any pane for OHLC, RSI and MACD — all three scroll and zoom together." : ""}</span>
        </div>
      </aside>`;
  }

  /* ---------------- settings modal ---------------- */
  function settingsHTML() {
    if (!s.settingsOpen || !s.form) return "";
    const f = s.form;
    const seg = (act, opts, cur) => `<div class="seg sm">${opts.map(o =>
      `<button data-act="${act}" data-k="${o[0]}" aria-pressed="${cur === o[0]}">${esc(o[1])}</button>`).join("")}</div>`;
    const canWrite = !!T.loadToken();
    const total = weightTotal();

    return `
      <div class="scrim" data-act="closesettings"></div>
      <div class="modal" role="dialog" aria-label="Settings">
        <div class="modal-head">
          <span class="modal-title">Settings</span>
          <button class="x" data-act="closesettings" title="Close">✕</button>
        </div>
        <div class="modal-body">

          <div class="set-group">
            <div class="set-label">Access</div>
            <div class="set-row">
              <input class="field grow" type="password" data-act="f-adminToken" value="${esc(f.adminToken)}"
                placeholder="${canWrite ? "•••••••• saved" : "read-only without a password"}">
              <span class="cap"><i>${canWrite
                ? "Editing enabled — your changes save to the shared data."
                : "Read-only — changes stay in this browser only."}</i></span>
            </div>
          </div>

          <div class="set-group">
            <div class="set-label">Appearance</div>
            <div class="set-row">
              ${seg("theme", [["dark", "Dark"], ["light", "Light"]], s.theme)}
              <span class="cap"><i>Applied immediately. A device preference — it is not part of the synced settings.</i></span>
            </div>
          </div>

          <div class="set-group">
            <div class="set-label">Data</div>
            <div class="set-row">
              <span class="cap"><b>data.json</b></span>
              <input class="field grow" data-act="f-dataUrl" value="${esc(f.dataUrl)}">
            </div>
            <div class="set-row">
              <span class="cap"><b>notes endpoint</b></span>
              <input class="field grow" data-act="f-syncUrl" value="${esc(f.syncUrl)}">
              <button class="btn sm" data-act="reconnect">Reconnect</button>
            </div>
          </div>

          <div class="set-group">
            <div class="set-label">Horizon</div>
            <div class="set-row">
              ${seg("horizon", [["long", "Long term"], ["swing", "Swing"]], f.horizon)}
              <span class="cap"><i>Presets fill the zone widths below.</i></span>
            </div>
            <div class="nums">
              <label>High zone %<input class="field" type="number" step="0.5" data-act="f-highZonePct" value="${esc(f.highZonePct)}"></label>
              <label>Basing zone %<input class="field" type="number" step="0.5" data-act="f-lowZonePct" value="${esc(f.lowZonePct)}"></label>
            </div>
          </div>

          <div class="set-group">
            <div class="set-label">Re-entry rule</div>
            <div class="set-row">
              ${seg("reentry", [["base", "Base confirmed"], ["near_low", "Near low"]], f.reEntryMode)}
              <span class="cap"><i>${f.reEntryMode === "base"
                ? "A name must clear all five basing checks before it counts as a candidate."
                : "Anything inside the basing zone counts, base or not."}</i></span>
            </div>
          </div>

          <div class="set-group">
            <div class="set-label">Trend definition</div>
            <div class="set-row">
              <select class="field" style="flex:0 0 120px" data-act="f-trendFast">
                ${T.TREND_EMAS.map(n => `<option value="${n}" ${+f.trendFast === n ? "selected" : ""}>EMA ${n}</option>`).join("")}
              </select>
              <span style="color:var(--color-neutral-600)">above</span>
              <select class="field" style="flex:0 0 120px" data-act="f-trendSlow">
                ${T.TREND_EMAS.map(n => `<option value="${n}" ${+f.trendSlow === n ? "selected" : ""}>EMA ${n}</option>`).join("")}
              </select>
            </div>
            <p class="set-note">Uptrend when EMA ${esc(f.trendFast)} is above EMA ${esc(f.trendSlow)}, downtrend when it is below.
              The default pair is the one the pipeline calls a golden or death cross.</p>
          </div>

          <div class="set-group">
            <div class="set-label">Alerts</div>
            <div class="set-row">
              ${seg("channel", [["telegram", "Telegram"], ["email", "Email"], ["both", "Both"]], f.alertChannel || "both")}
              <span class="cap"><i>${(f.alertChannel || "both") === "both"
                ? "Alerts go to both Telegram and email."
                : `Alerts go to ${f.alertChannel === "telegram" ? "Telegram" : "email"} only — the other channel stays silent.`}</i></span>
            </div>
            ${T.ALERT_KINDS.map(a => `
              <div class="set-row">
                <span class="cap"><b>${esc(a.label)}</b><i>${esc(a.when)}</i></span>
                ${seg("alert-" + a.key, [["on", "On"], ["off", "Off"]], on(f.alertTypes, a.key) ? "on" : "off")}
              </div>`).join("")}
            <div class="set-row">
              <span class="cap"><b>Text summary</b><i>written signals above the link</i></span>
              ${seg("caption", [["on", "On"], ["off", "Off"]], f.captionText ? "on" : "off")}
            </div>
            <div class="set-row">
              <span class="cap"><b>Extra recipients</b></span>
              <input class="field grow" data-act="f-alertEmails" value="${esc(f.alertEmails)}" placeholder="name@example.com, …">
            </div>
            <p class="set-note">Comma-separated, up to ${T.MAX_EXTRA_EMAILS}. The configured inbox always gets the alert;
              this list only adds to it. Telegram is unaffected.</p>
          </div>

          <div class="set-group">
            <div class="set-label">Score weights</div>
            <div class="nums">
              ${[["trend", "Trend"], ["momentum", "Momentum"], ["participation", "Volume"],
                 ["relStrength", "Rel. strength"], ["risk", "Risk"]].map(([k, lbl]) =>
                `<label>${lbl}<input class="field" type="number" data-act="f-${k}" value="${esc(f[k])}"></label>`).join("")}
            </div>
            <p class="set-note" style="color:${total === 100 ? "var(--color-neutral-500)" : T.ink.down}">
              Total ${total} — must be 100 to save.</p>
          </div>

        </div>
        <div class="modal-foot">
          <button class="btn" data-act="resetsettings">Reset to defaults</button>
          <span class="spacer"></span>
          <button class="btn" data-act="closesettings">Cancel</button>
          <button class="btn primary" data-act="savesettings">Save</button>
        </div>
      </div>`;
  }

  /* ---------------- manage lists ---------------- */
  function listsHTML() {
    if (!s.listsOpen) return "";
    return `
      <div class="scrim" data-act="closelists"></div>
      <div class="modal" role="dialog" aria-label="Manage lists">
        <div class="modal-head">
          <span class="modal-title">Manage lists</span>
          <button class="x" data-act="closelists" title="Close">✕</button>
        </div>
        <div class="modal-body">
          ${s.lists.map(l => `
            <div>
              <div class="list-edit">
                <input class="field grow" data-act="lname" data-id="${esc(l.id)}" value="${esc(l.name)}">
                <select class="field" style="flex:0 0 140px" data-act="ltype" data-id="${esc(l.id)}">
                  ${Object.entries(T.TYPE_LABEL).map(([v, lbl]) =>
                    `<option value="${v}" ${l.type === v ? "selected" : ""}>${esc(lbl)}</option>`).join("")}
                </select>
                <button class="btn sm danger" data-act="dellist" data-id="${esc(l.id)}">Delete</button>
              </div>
              <div class="syms">
                ${l.symbols.length ? l.symbols.map(sym =>
                  `<button data-act="unsym" data-id="${esc(l.id)}" data-sym="${esc(sym)}" title="Remove ${esc(sym)}">${esc(sym)} ✕</button>`).join("")
                  : `<span class="rail-note">Empty — add a ticker from the page.</span>`}
              </div>
            </div>`).join("")}

          <div class="set-group">
            <div class="set-label">New list</div>
            <div class="set-row">
              <input class="field grow" data-act="newname" value="${esc(s.newListName)}" placeholder="List name">
              <select class="field" style="flex:0 0 140px" data-act="newtype">
                ${Object.entries(T.TYPE_LABEL).map(([v, lbl]) =>
                  `<option value="${v}" ${s.newListType === v ? "selected" : ""}>${esc(lbl)}</option>`).join("")}
              </select>
              <button class="btn primary" data-act="addlist">Add list</button>
            </div>
          </div>
        </div>
        <div class="modal-foot">
          <span class="spacer"></span>
          <button class="btn" data-act="closelists">Done</button>
        </div>
      </div>`;
  }

  function addModalHTML() {
    if (!s.addModal) return "";
    const m = s.addModal;
    return `
      <div class="scrim" data-act="canceladd"></div>
      <div class="modal narrow" role="dialog" aria-label="Add ${esc(m.sym)}">
        <div class="modal-head"><span class="modal-title">Add ${esc(m.sym)}</span>
          <button class="x" data-act="canceladd" title="Close">✕</button></div>
        <div class="modal-body">
          <div class="set-group">
            <div class="set-label">Add to list</div>
            <select class="field" data-act="addtarget">
              ${s.lists.map(l => `<option value="${esc(l.id)}" ${m.target === l.id ? "selected" : ""}
                ${l.symbols.includes(m.sym) ? "disabled" : ""}>${esc(l.name)} · ${esc(T.TYPE_LABEL[l.type])}${
                l.symbols.includes(m.sym) ? " (already in)" : ""}</option>`).join("")}
            </select>
            <p class="set-note">A ticker added here reaches the pipeline through the notes endpoint —
              it needs the write password, and it shows as awaiting data until the next run.</p>
          </div>
        </div>
        <div class="modal-foot">
          <span class="spacer"></span>
          <button class="btn" data-act="canceladd">Cancel</button>
          <button class="btn primary" data-act="confirmadd">Add</button>
        </div>
      </div>`;
  }

  function confirmHTML() {
    if (s.confirmClose) {
      return `
        <div class="scrim" data-act="keepediting"></div>
        <div class="modal narrow" role="dialog" aria-label="Unsaved changes">
          <div class="modal-head"><span class="modal-title">Unsaved changes</span></div>
          <div class="modal-body"><p class="why">Settings have edits that have not been saved.</p></div>
          <div class="modal-foot">
            <button class="btn" data-act="discard">Discard</button>
            <span class="spacer"></span>
            <button class="btn" data-act="keepediting">Keep editing</button>
            <button class="btn primary" data-act="saveandclose">Save</button>
          </div>
        </div>`;
    }
    if (!s.confirm) return "";
    return `
      <div class="scrim" data-act="cancelconfirm"></div>
      <div class="modal narrow" role="dialog" aria-label="${esc(s.confirm.title)}">
        <div class="modal-head"><span class="modal-title">${esc(s.confirm.title)}</span></div>
        <div class="modal-body"><p class="why">${esc(s.confirm.msg)}</p></div>
        <div class="modal-foot">
          <span class="spacer"></span>
          <button class="btn" data-act="cancelconfirm">Cancel</button>
          <button class="btn primary" data-act="okconfirm">${esc(s.confirm.okLabel || "OK")}</button>
        </div>
      </div>`;
  }

  function pwHTML() {
    if (!s.pw) return "";
    const p = s.pw;
    return `
      <div class="scrim" data-act="pwcancel"></div>
      <div class="modal narrow" role="dialog" aria-label="Write password">
        <div class="modal-head"><span class="modal-title">Write password</span>
          <button class="x" data-act="pwcancel" title="Close">✕</button></div>
        <div class="modal-body">
          <div class="set-group">
            <div class="set-label">${esc(p.what)} needs the write password</div>
            <input class="field" type="password" data-act="pwinput" value="${esc(p.value || "")}"
                   placeholder="Write password" autocomplete="current-password"
                   aria-label="Write password" ${p.busy ? "disabled" : ""}>
            ${p.error ? `<p class="set-note" role="alert" style="color:${T.ink.down}">${esc(p.error)}</p>` : ""}
            <p class="set-note">Checked against the server. Asked once per session — later edits
              in this tab save without asking again. Nothing is saved until it is accepted.</p>
          </div>
        </div>
        <div class="modal-foot">
          <span class="spacer"></span>
          <button class="btn" data-act="pwcancel">Cancel</button>
          <button class="btn primary" data-act="pwsubmit" ${p.busy ? "disabled" : ""}>${
            p.busy ? "Checking…" : "Unlock and save"}</button>
        </div>
      </div>`;
  }

  /* ---------------- render ---------------- */
  /* A repaint replaces both roots, so the focused field would lose the caret on
     every keystroke. It is restored by (act, id) — the same pair that
     identifies the field to the event handler. */
  function withFocus(paint) {
    const a = document.activeElement;
    const act = a && a.dataset ? a.dataset.act : null;
    const id = a && a.dataset ? a.dataset.id : null;
    const st = a && a.selectionStart, en = a && a.selectionEnd;
    paint();
    if (!act) return;
    const sel = `[data-act="${act}"]` + (id ? `[data-id="${id}"]` : "");
    const n = document.querySelector(sel);
    if (!n) return;
    n.focus();
    try { if (st != null) n.setSelectionRange(st, en); } catch (e) { }
  }

  function render() { withFocus(paint); }

  function paint() {
    document.documentElement.dataset.theme = s.theme;
    const m = mastVals();
    const all = rows();
    const solid = all.filter(d => !d.pending);
    const grp = (n) => solid.filter(d => T.groupOf(d, s.settings) === n).sort(byScore);
    const action = grp(1), reentry = grp(3);
    const pending = all.filter(d => d.pending);
    const h = hero(solid, action, reentry);
    const act = activeList();
    const of = listsOfType(s.activeType);
    const canWrite = T.canWrite();
    const symCount = (t) => [...new Set(s.lists.filter(l => l.type === t).flatMap(l => l.symbols))].length;

    app.innerHTML = `
      <header class="mast">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M3 13h4l3 7 4-16 3 9h4"/></svg>
          </span>
          <span class="brand-name">TrendAlert</span>
        </div>
        <span class="chip-scope" title="${esc(m.scope)}">US market</span>
        <div class="mast-right">
          ${m.stale ? `<span class="stale" style="color:${m.stale.level === "bad" ? T.ink.down : T.ink.warn}">${esc(m.stale.text)}</span>` : ""}
          <span class="status"><i class="status-dot" style="background:${m.dot}"></i>${esc(m.status)}</span>
          <button class="icon-btn accent" data-act="opensettings">
            <i class="ro-dot" style="background:${canWrite ? T.ink.up : "var(--color-neutral-600)"}"
               title="${canWrite ? "Editing enabled" : "Read-only — no write password"}"></i>
            Settings
          </button>
        </div>
      </header>

      <div class="tapebar">
        <div class="tape">
          ${m.tape.map(t => `<button class="tape-item" data-act="open" data-sym="${esc(t.sym)}" title="${esc(t.sym)} — open detail">
            <span class="tape-name">${esc(t.name)}</span>
            <span class="tape-val num">${esc(t.close)}</span>
            <span class="tape-chg num" style="color:${t.ink}">${esc(t.chg)}</span>
          </button>`).join("")}
        </div>
      </div>

      <div class="page">
        <div class="col">
          <nav class="subnav">
            ${TYPES.map(([t, label]) =>
              `<button class="tab" data-act="type" data-k="${t}" aria-selected="${s.activeType === t}">
                ${label}<span class="n num">${symCount(t)}</span></button>`).join("")}
            <span class="right">
              <button class="btn sm" data-act="openlists">Manage lists</button>
            </span>
          </nav>

          ${of.length > 1 ? `<div class="lists-row">${of.map(l =>
            `<button class="lchip" data-act="list" data-id="${esc(l.id)}" aria-pressed="${act && act.id === l.id}">
              ${esc(l.name)}<span class="n num">${l.symbols.length}</span></button>`).join("")}</div>` : ""}

          <div class="addbar">
            <span class="search">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                   stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
              <input class="field" data-act="add" value="${esc(s.addDraft)}" placeholder="Add ticker" aria-label="Add ticker">
            </span>
            <button class="btn" data-act="submitadd">Add</button>
            <span class="search">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                   stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
              <input class="field" data-act="q" value="${esc(s.q)}" placeholder="Filter symbols" aria-label="Filter symbols">
            </span>
            <select class="field" style="flex:0 0 150px" data-act="sector" aria-label="Sector">
              ${T.SECTORS.map(sec =>
                `<option value="${esc(sec)}" ${s.sector === sec ? "selected" : ""}>${sec === "all" ? "All sectors" : esc(sec)}</option>`).join("")}
            </select>
          </div>
          ${s.addNote ? `<p class="note">${esc(s.addNote)}</p>` : ""}

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

          ${pending.length ? `
            <section class="sec">
              <div class="sec-head"><h2 class="sec-title">Awaiting data</h2><span class="count">${pending.length}</span></div>
              <div class="pend">${pending.map(d =>
                `<button class="pend-chip" data-act="drop" data-sym="${esc(d.symbol)}" title="Remove from ${esc(act ? act.name : "list")}">${esc(d.symbol)} ✕</button>`).join("")}</div>
            </section>` : ""}

          <section class="sec">
            <div class="sec-head">
              <h2 class="sec-title">All trends</h2>
              <span class="count">${solid.length}</span>
              <div class="sec-tools">
                <div class="seg">${[["all", "All"], ["bull", "Bullish"], ["bear", "Bearish"], ["mixed", "Mixed"]].map(o =>
                  `<button data-act="trendf" data-k="${o[0]}" aria-pressed="${s.trendF === o[0]}">${o[1]}</button>`).join("")}</div>
                <div class="seg icons">
                  <button data-act="view" data-k="cards" aria-pressed="${s.view === "cards"}" title="Cards">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>
                      <rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
                  </button>
                  <button data-act="view" data-k="table" aria-pressed="${s.view === "table"}" title="Table">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18"/></svg>
                  </button>
                </div>
              </div>
            </div>
            ${s.view === "table" ? tableHTML(all)
              : (solid.length ? `<div class="grid-3">${solid.map(miniHTML).join("")}</div>`
                : `<p class="empty">Nothing in this list matches the current filters.</p>`)}
          </section>
        </div>

        <aside class="col rail">${railHTML(solid)}</aside>
      </div>

      <footer class="foot">
        Symbols can belong to several portfolios, watchlists and indexes.
        ${s.mode === "live" ? "Showing the live pipeline feed." : "Sample data shown — the live feed did not load."}
        ${canWrite ? "" : "This browser has no write password, so edits stay local."}
      </footer>`;

    /* next.css has always carried a .toast rule and the shipped dashboard
       renders one, but this tree never emitted the markup -- so every flash()
       was invisible, including the weights-must-sum-to-100 and bad-email
       validation errors. role=status so it is announced, not just seen. */
    layer.innerHTML = drawerHTML() + settingsHTML() + listsHTML() + addModalHTML()
      + confirmHTML() + pwHTML()
      + (s.toast ? `<div class="toast" role="status">${esc(s.toast)}</div>` : "");
    document.body.classList.toggle("locked",
      !!(s.drawerOpen || s.settingsOpen || s.listsOpen || s.addModal || s.confirm
         || s.confirmClose || s.pw));

    /* charts: move the persistent containers into the freshly painted slots */
    if (s.drawerOpen && s.drawerTab === "chart" && s.selected) {
      const slots = { price: "slot-price", rsi: "slot-rsi", macd: "slot-macd" };
      Object.entries(slots).forEach(([k, id]) => {
        const slot = document.getElementById(id);
        if (slot) slot.appendChild(pane(k));
      });
      ensureChart(s.selected);
      syncCharts();
    } else if (charts) teardownCharts();
  }

  /* ---------------- events ---------------- */
  /* Cancelling is a discard: gatedSave applies nothing until the server has
     accepted the password, so there is no half-saved state to unwind here. */
  function cancelPw() {
    if (!s.pw) return;
    const what = s.pw.what;
    s.pw = null;
    render();
    flash(`${what} was not saved — no write password entered.`);
  }

  const closeTop = () => {
    if (s.pw) return cancelPw();
    if (s.confirm) { s.confirm = null; return render(); }
    if (s.confirmClose) { s.confirmClose = null; return render(); }
    if (s.addModal) { s.addModal = null; return render(); }
    if (s.listsOpen) { s.listsOpen = false; return render(); }
    if (s.settingsOpen) return requestCloseSettings("close");
    if (s.drawerOpen) { s.drawerOpen = false; return render(); }
  };

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-act]");
    if (!t) return;
    const a = t.dataset.act, k = t.dataset.k, sym = t.dataset.sym, id = t.dataset.id;

    /* list navigation and filters */
    if (a === "type") { s.activeType = k; s.activeListId = null; return render(); }
    if (a === "list") { s.activeListId = id; return render(); }
    if (a === "trendf") { s.trendF = k; return render(); }
    if (a === "view") {
      s.view = k;
      s.settings = { ...s.settings, view: k }; T.saveSettings(s.settings);
      return render();
    }
    if (a === "sort") {
      if (s.sortKey === k) s.sortDir *= -1;
      else { s.sortKey = k; s.sortDir = (k === "symbol" || k === "sector") ? 1 : -1; }
      return render();
    }

    /* symbols */
    if (a === "open" || a === "chart") {
      s.selected = sym; s.drawerOpen = true; s.drawerTab = a === "chart" ? "chart" : "detail";
      return render();
    }
    if (a === "dtab") { s.drawerTab = k; return render(); }
    if (a === "closedrawer") { s.drawerOpen = false; return render(); }
    if (a === "pbre") return askPbre(sym, t.dataset.k);
    if (a === "pbreset") return askResetPbre(sym);
    if (a === "member") return toggleMembership(sym, id);
    if (a === "drop") {
      const l = activeList();
      if (l) removeFrom(l.id, sym);
      return;
    }

    /* add ticker */
    if (a === "submitadd") return submitAdd();
    if (a === "confirmadd") return confirmAdd();
    if (a === "canceladd") { s.addModal = null; return render(); }

    /* lists modal */
    if (a === "openlists") { s.listsOpen = true; return render(); }
    if (a === "closelists") { s.listsOpen = false; return render(); }
    if (a === "addlist") return addList();
    if (a === "dellist") return deleteList(id);
    if (a === "unsym") return removeFrom(id, sym);

    /* settings modal */
    if (a === "opensettings") { s.settingsOpen = true; s.confirmClose = null; s.form = formOf(s.settings); return render(); }
    if (a === "closesettings") return requestCloseSettings("close");
    if (a === "savesettings") return saveSettings();
    if (a === "resetsettings") { s.form = formOf(JSON.parse(JSON.stringify(T.DEFAULT_SETTINGS))); return render(); }
    if (a === "reconnect") return reconnect();
    if (a === "theme") return setTheme(k);
    if (a === "horizon") {
      const p = T.HORIZON_PRESETS[k];
      s.form = { ...s.form, horizon: k, gainPct: p.gain, highZonePct: p.high, lowZonePct: p.low };
      return render();
    }
    if (a === "reentry") { s.form = { ...s.form, reEntryMode: k }; return render(); }
    if (a === "channel") { s.form = { ...s.form, alertChannel: k }; return render(); }
    if (a === "caption") { s.form = { ...s.form, captionText: k === "on" }; return render(); }
    if (a.startsWith("alert-")) {
      const key = a.slice(6);
      s.form = { ...s.form, alertTypes: { ...s.form.alertTypes, [key]: k === "on" } };
      return render();
    }

    /* dirty-close dialog */
    if (a === "keepediting") { s.confirmClose = null; return render(); }
    if (a === "discard") {
      const next = s.confirmClose;
      s.confirmClose = null; s.settingsOpen = false; s.listsOpen = next === "lists";
      s.form = formOf(s.settings);
      return render();
    }
    if (a === "saveandclose") {
      const next = s.confirmClose;
      s.confirmClose = null;
      return saveSettings(() => { if (next === "lists") { s.listsOpen = true; render(); } });
    }

    /* generic confirm */
    if (a === "cancelconfirm") { s.confirm = null; return render(); }
    if (a === "pwsubmit") return s.pw && s.pw.submit();
    if (a === "pwcancel") return cancelPw();
    if (a === "okconfirm") {
      const c = s.confirm;
      s.confirm = null;
      if (c && c.ok) c.ok(); else render();
      return;
    }

    /* chart controls */
    if (a === "span") return setCfg(c => { c.span = k; });
    if (a === "eye") return toggleEye(k);
    if (a === "cfgtoggle") { s.chartCfgOpen = !s.chartCfgOpen; return render(); }
  });

  document.addEventListener("input", (e) => {
    const t = e.target.closest("[data-act]");
    if (!t) return;
    const a = t.dataset.act, v = t.value;

    if (a === "pwinput") { if (s.pw) s.pw.value = v; return; }
    if (a === "q") { s.q = v.trim().toLowerCase(); return render(); }
    if (a === "add") { s.addDraft = v; return; }
    if (a === "newname") { s.newListName = v; return; }
    if (a === "sector") { s.sector = v; return render(); }
    if (a === "newtype") { s.newListType = v; return; }
    if (a === "addtarget") { s.addModal = { ...s.addModal, target: v }; return; }
    if (a === "lname") {
      /* renaming re-publishes nothing but the list shape; symbols are untouched */
      return saveLists(s.lists.map(l => l.id === t.dataset.id ? { ...l, name: v } : l));
    }
    if (a === "ltype") {
      return saveLists(s.lists.map(l => l.id === t.dataset.id ? { ...l, type: v } : l));
    }
    if (a && a.startsWith("f-")) {
      s.form = { ...s.form, [a.slice(2)]: v };
      /* the note lines under several fields read from the form, so repaint */
      return render();
    }
    if (a === "color") return setCfg(c => {
      if (t.dataset.k === "rsi") c.rsi.color = v;
      else if (t.dataset.k === "macd") c.macd.macdColor = v;
      else c.ema[+t.dataset.k.slice(3)].color = v;
    });
    if (a === "color2") return setCfg(c => { c.macd.signalColor = v; });
    if (a === "cfgnum") {
      const key = t.dataset.k, n = +v;
      return setCfg(c => {
        if (key === "rsiP") c.rsi.period = Math.max(2, Math.min(100, n || 14));
        else if (key === "mF") c.macd.fast = Math.max(2, n || 12);
        else if (key === "mS") c.macd.slow = Math.max(3, n || 26);
        else if (key === "mG") c.macd.signal = Math.max(1, n || 9);
        else if (key === "rcK") c.regch.k = Math.max(0.5, Math.min(4, n || 1.8));
        else if (key === "rcE") c.regch.entryPct = Math.max(2, Math.min(50, n || 18));
        else if (key === "rcC") c.regch.cooldown = Math.max(0, Math.min(252, n == null || isNaN(n) ? 60 : n));
        else {
          const i = +key.slice(4);
          c.ema[i].period = Math.max(2, Math.min(400, n || c.ema[i].period));
        }
      });
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return closeTop();
    if (e.key === "Enter" && e.target.dataset) {
      if (e.target.dataset.act === "pwinput") return s.pw && s.pw.submit();
      if (e.target.dataset.act === "add") return submitAdd();
      if (e.target.dataset.act === "newname") return addList();
    }
  });

  /* ---------------- boot ---------------- */
  render();
  T.pull(s.syncUrl, "pbre").then(pb => {
    if (pb) { s.pbre = pb; T.savePbre(pb); }
    load();
  });
  setInterval(load, 300000);
  /* the staleness pill is time-based, so the page has to repaint without new
     data for it to ever appear */
  setInterval(() => { if (!s.settingsOpen && !s.drawerOpen) render(); }, 60000);
  window.addEventListener("resize", () => { if (charts) syncCharts(); });
})();
