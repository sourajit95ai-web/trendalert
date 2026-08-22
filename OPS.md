# TrendAlert — Production Ops Runbook

One-time hardening commands. Run after the initial deploy (README §1–3).
Everything here fails safe: if an alert or setting is missing, the pipeline
still runs on defaults.

---

## 1. Function-failure email alerts (Cloud Monitoring, no code)

```bash
PROJECT=$(gcloud config get-value project)

# notification channel (your email)
gcloud beta monitoring channels create \
  --display-name="TrendAlert alerts" \
  --type=email \
  --channel-labels=email_address=YOU@EXAMPLE.COM
CHANNEL=$(gcloud beta monitoring channels list \
  --filter='displayName="TrendAlert alerts"' --format='value(name)')

# alert: any execution failure on either function in a 5-min window
gcloud alpha monitoring policies create \
  --display-name="TrendAlert function failures" \
  --notification-channels="$CHANNEL" \
  --condition-display-name="cloud function error" \
  --condition-filter='resource.type="cloud_function" AND metric.type="cloudfunctions.googleapis.com/function/execution_count" AND metric.labels.status!="ok"' \
  --condition-threshold-value=0 \
  --condition-threshold-comparison=COMPARISON_GT \
  --condition-threshold-duration=300s \
  --combiner=OR
```

Dashboard-side staleness is already built in: the meta bar shows an amber
badge at >1h and a red badge at >2h on weekdays (>26h any day), checked
every minute against `generated_at`.

## 2. Budget guard ($5/month)

```bash
BILLING=$(gcloud billing projects describe $PROJECT --format='value(billingAccountName)')
gcloud billing budgets create \
  --billing-account="$BILLING" \
  --display-name="TrendAlert cap" \
  --budget-amount=5USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

## 3. CORS lockdown

Bucket (dashboard + JSON reads) — create `cors.json`:
```json
[{"origin": ["https://storage.googleapis.com"],
  "method": ["GET"],
  "responseHeader": ["Content-Type"],
  "maxAgeSeconds": 3600}]
```
```bash
gsutil cors set cors.json gs://YOUR_BUCKET
```

Notes/positions/settings API — set the allowed origin at deploy:
```bash
gcloud functions deploy notes \
  --gen2 --runtime python312 --region us-central1 \
  --source backend --entry-point notes \
  --trigger-http --allow-unauthenticated \
  --set-env-vars "GCS_BUCKET=YOUR_BUCKET,ALLOWED_ORIGIN=https://storage.googleapis.com"
```

Serving from a custom domain later? Change both origins to that domain.
Testing locally from file://? Temporarily omit ALLOWED_ORIGIN (falls back
to *) — never leave it that way in production.

## 4. settings.json bridge (dashboard → backend score)

Already wired end to end:
- Dashboard: saving Settings POSTs `{kind:"settings", data:{…}}` to the Sync API
- Notes function: validates and writes `settings.json` (weights must sum to 100)
- Pipeline: add to main.py before scoring:

```python
from chart_backend import load_published_settings
cfg = load_published_settings(BUCKET)
if cfg.get("weights"):
    w = cfg["weights"]
    import scoring
    scoring.WEIGHTS = {"trend": w["trend"]/100, "momentum": w["momentum"]/100,
                       "participation": w["participation"]/100,
                       "rel_strength": w["relStrength"]/100, "risk_adj": w["risk"]/100}
if cfg.get("horizon") == "swing":
    import scoring
    scoring.RS_WEIGHTS = {63: 0.40, 126: 0.30, 189: 0.20, 252: 0.10}
```

After this, the dashboard's Settings modal drives the *published* score on the
next pipeline run — the client approximation and backend can no longer drift.

## 5. Verify

- [ ] Trigger a deliberate function failure (bad secret name) → email arrives
- [ ] `gsutil cors get gs://YOUR_BUCKET` shows the locked origin
- [ ] Save settings in the dashboard → `gsutil cat gs://YOUR_BUCKET/settings.json`
- [ ] Stop Cloud Scheduler for 3h on a weekday → dashboard badge turns red


## EOD scheduling & alert email (added)

**Scheduler.** The rule engine reads settled daily closes, so run the pipeline
after the close, not every 30 min during the session:

    gcloud scheduler jobs update http trendalert-tick \
      --schedule="35 21 * * 1-5" --time-zone="Etc/UTC"

21:35 UTC = 17:35 EDT / 16:35 EST — past the 16:00 ET close year-round.
(An optional second midday run is harmless: alerts only fire when
`data_current` is true and events are transition-deduped.)

**Calendar.** `trading_calendar.py` computes NYSE holidays (incl. observed
shifts and Good Friday). The payload now carries `expected_last_trading_day`
and `data_current`; alerts are suppressed on weekends/holidays.

**Adjustment.** Bars are fetched with `adjustment=all` (splits + dividends);
without it, split days read as crashes and dividend gaps skew EMAs/52-week
levels on yield names.

**Email.** One summary mail per rule-engine transition: BOOK 1/3, TRAIL EXIT,
BASE CONFIRMED, plus golden/death crosses. Setup:
1. Create a Gmail app password (or any SMTP account).
2. `echo -n "<app-password>" | gcloud secrets create smtp-pass --data-file=-`
3. GitHub repo secrets: `SMTP_USER` (sender address), `ALERT_TO`
   (comma-separated recipients). Push to main to redeploy.
Leave `SMTP_USER` unset to disable email entirely (`email:disabled` in the
function response). Dedupe state lives in `alerts_state.json` in the bucket.

**Telegram.** Same alerts, pushed to a Telegram chat (runs alongside email;
either channel can be disabled independently). Setup:
1. In Telegram, message **@BotFather** → `/newbot` → copy the HTTP API token.
2. `printf '%s' "<token>" | gcloud secrets create telegram-bot-token --data-file=-`
   and grant the runtime service account `roles/secretmanager.secretAccessor`
   on it (same command pattern as the alpaca secrets, README §2).
3. Send your bot any message (it can't message you first), then open
   `https://api.telegram.org/bot<token>/getUpdates` and read
   `result[0].message.chat.id`.
4. GitHub repo secret `TELEGRAM_CHAT_ID` = that id. Redeploy the backend.
Leave the secret or chat id unset to disable (`telegram:disabled`). If the
token ever leaks, revoke it in BotFather with `/revoke`.

**Choosing the channel (Settings > Alerts).** The dashboard writes
`alertChannel` (`telegram` | `email` | `both`, default `both`) into
`settings.json` via the notes function, and every scheduled alert — daily
summary, morning brief, bloodbath alarm, EOD rule signals — routes through
`alerts_email.fan_out`, which reports a skipped channel as `telegram:off` /
`email:off` in the function response. It gates only what the user picked; the
env config still has the final say (`telegram:disabled` when the token or chat
id is unset). A missing or unrecognised value means both, so a bad write can
never silence the alerts.

**Choosing which alerts fire (Settings > Alerts > Which alerts).** The same
panel carries one switch per scheduled alert, written as `alertTypes` in
`settings.json`. The keys are `ALERT_KINDS` in `alerts_email.py` — the order
the trading day fires them:

| key | alert | scheduler job | when |
| --- | --- | --- | --- |
| `summary_premkt` | Pre-market summary chart | `trendalert-summary-premkt` | 8:35 ET |
| `brief` | Morning brief | `trendalert-morning-brief` | 9:50 ET |
| `summary_close` | Market-close summary chart | `trendalert-summary-close` | 16:50 ET |

**The text under the poster (Settings > Alerts > Text summary).** The daily
summary's caption is the dashboard link and nothing else. Switching this on
writes `captionText: true` into `settings.json` and puts the written signals
(action / re-entry / crosses, the same grouping the poster cards draw) above
the link, in the Telegram caption and the email body alike. Unlike the alert
switches this one fails CLOSED — `caption_text_on` treats a missing key as
off, because the poster already says it. Telegram truncates a caption at 1024
characters; long signal days are cut there, the image is unaffected.

**Email recipients are `ALERT_TO` only (changed 2026-08-22).** Set it as a
GitHub repo secret, comma-separated; it falls back to `SMTP_USER` when unset.
`email_recipients()` still takes a `settings` argument so the call sites did
not have to change, but it ignores it.

They used to be editable from Settings > Alerts as `alertEmails` in
`settings.json`. That was a **PII leak**: `settings.json` is a public bucket
object, so
`curl https://storage.googleapis.com/trendalert-data-rattle/settings.json`
returned every address in the clear — including addresses belonging to other
people, who never agreed to be published. The field is gone from the dashboard,
the notes function silently DROPS it on write, and since the settings whitelist
is rebuilt from scratch on every save, a stored copy disappears the next time
anything saves settings. Adding a recipient now means editing the secret and
redeploying, which is the correct amount of friction for publishing somebody's
inbox. Telegram was never affected.

Purging an address already in the live file needs a direct write — the notes
function only ever rewrites on a save:

```bash
gcloud storage cp settings.json gs://trendalert-data-rattle/settings.json --cache-control="no-cache, max-age=0" --content-type="application/json"
```

Never hand out the live dashboard URL for review — use
`python frontend/make_review_copy.py`.

**Retired 2026-07-28: the bloodbath alarm and the end-of-day rule signals.**
They have no switch and never send — `RETIRED_ALERTS` in `alerts_email.py`
makes `alert_on` refuse them whatever `settings.json` holds, so they cannot be
revived by a stale browser. `bloodbath.py`, the `?mode=bloodbath` route and
their tests are all kept, so bringing one back is a one-line edit. The 8:30
scheduler job now costs a settings read and returns `skipped(retired)`; pause
it so it stops running at all:

```
gcloud scheduler jobs pause trendalert-bloodbath --location us-central1
```

(`resume` puts it back.) The EOD signals had no job of their own — they rode
the 16:45 `trendalert-refresh-close` pipeline run, which still refreshes data
as before and now reports `alerts:retired`.

Each entry point checks its own key first and returns `skipped(off:<key>)`
(`alerts:off` for EOD) without fetching quotes or rendering — the scheduler job
still runs, it just costs one settings read. Both summary jobs share
`run_daily_summary`, so which switch applies is decided by `session_label`:
PRE-MARKET / 30 MIN TO OPEN → `summary_premkt`, anything later →
`summary_close`. A missing key means on, so an alert added later keeps
delivering to a browser that has never seen its switch.

**Adding another recipient.** One bot can message many people:
1. Share the bot with them (its `t.me/<botname>` link) and have *them* send
   it any message first — a bot can never message someone who hasn't
   messaged it.
2. Re-open `https://api.telegram.org/bot<token>/getUpdates` — a new entry
   appears with their `message.chat.id`.
3. Update the `TELEGRAM_CHAT_ID` repo secret to a comma-separated list, e.g.
   `720876958,987654321`, and redeploy. Each id gets its own message; one
   bad/blocked id doesn't stop delivery to the others.

## Morning brief — 20 min after the open (added)

A third scheduler job hits the pipeline function with `?mode=brief` at
**9:50 AM America/New_York, Mon–Fri** (DST-safe via the job's time zone):

    gcloud scheduler jobs create http trendalert-morning-brief \
      --location us-central1 \
      --schedule="50 9 * * 1-5" --time-zone="America/New_York" \
      --uri="<pipeline-function-url>/?mode=brief" --http-method=GET \
      --attempt-deadline=540s

`mode=brief` (backend/morning_brief.py) does NOT recompute scores or touch
data.json — it pulls intraday IEX snapshots for the whole universe (core +
UI-added tickers) and sends one email + one Telegram message painting the
day so far: index open gaps vs drift since the open, average first-20-min
move per sector with best/worst names, and tracked positions (positions.json)
with day move, sector, and gain since entry. Delivery reuses the same
SMTP/Telegram config as EOD alerts; either channel disables the same way.
Skipped on weekends/holidays and when no intraday bar for today exists yet.
Note: until a backend with morning_brief.py is deployed, the job triggers a
harmless ordinary pipeline run (unknown args are ignored).

## Bloodbath alarm — 1 hour before the open (added)

A fourth scheduler job hits the pipeline function with `?mode=bloodbath` at
**8:30 AM America/New_York, Mon–Fri** — one hour before the bell, by which
time IEX pre-market (from 8:00 ET) has printed:

    gcloud scheduler jobs create http trendalert-bloodbath \
      --location us-central1 \
      --schedule="30 8 * * 1-5" --time-zone="America/New_York" \
      --uri="<pipeline-function-url>/?mode=bloodbath" --http-method=GET \
      --attempt-deadline=300s

`mode=bloodbath` (backend/bloodbath.py) recomputes nothing and is **silent
on normal days** — it is an alarm, not a digest. It delivers only when:

    GATE     SPY AND QQQ both <= -indexDropPct vs prior close, AND
    BREADTH  >= sectorFrac of sectors averaging <= -sectorDropPct,
             OR >= declinerFrac of fresh-quote names red

With fewer than `minCoverage` fresh pre-market prints breadth is
unmeasurable and the gate alone fires (the message says so — thin IEX
pre-market is normal for smaller names an hour before the open).

| param | default | meaning |
|---|---|---|
| `indexDropPct`  | 2.0  | both indexes must be down at least this much |
| `sectorDropPct` | 1.5  | a sector is "down hard" at this avg or worse |
| `sectorFrac`    | 0.7  | fraction of sectors down hard to confirm |
| `declinerFrac`  | 0.75 | fraction of names red to confirm (alternate) |
| `minCoverage`   | 10   | fresh quotes needed before breadth is trusted |

Severity from the SPY/QQQ average: **RED OPEN** (gate met) → **BLOODBATH**
(<= -3%, or <= -2.5% with >=90% red) → **CRASH WATCH** (<= -5%; the S&P's
level-1 circuit breaker halts trading at -7%). VIXY is fetched as a fear
proxy and reported, never gating.

Tune without redeploying — add a `bloodbath` key to published settings.json:

    {"bloodbath": {"indexDropPct": 2.5, "sectorFrac": 0.8}}

Junk values silently fall back to the defaults above. The message carries
indexes, per-sector averages, tracked positions with a ⚠ flag on any name
gapping under its EMA50, and a playbook reminder that the trail-exit rule
fires on the CLOSE, not a panic open. Skipped on weekends/holidays and
when SPY/QQQ have no pre-market print.

## Dynamic universe (added)

Tickers added in the dashboard UI get data automatically — no code edit:
1. Adding/removing a symbol in any list POSTs the union of all list symbols
   to the notes function (`kind=universe`) -> `universe.json` in the bucket.
2. Each pipeline run merges `universe.json` into its fetch (same single
   batched Alpaca request; caps: 50 extra equities, 10 extra cryptos, "/"
   marks a crypto pair). Sector shows as "Other" unless added to SECTORS.
3. The publish guard counts CORE symbols only, so a typo'd UI ticker can
   never block publishing — it just stays ★ in the dashboard.
New symbols appear on the next cycle (<=30 min). The hardcoded SYMBOLS in
main.py remain the curated core (RS benchmark SPY etc. must stay there).

## New listings / short history (added)

A "52-week" level requires a 52-week window. Symbols with fewer than
**252 trading days** of history (recent IPOs, new listings) publish:

    "history_bars": 90, "limited_history": true,
    "high_252": null, "low_252": null, "zone": null,
    "base_status": "insufficient_history",
    "ema150": null, "ema200": null      # an EMA(n) needs n bars

Consequence: **booking, trail, and base rules stay disarmed** on these names —
no NEAR HIGH off a 3-month high, no base call off a fake 52-week low. The
dashboard shows a `NEW LISTING · Nd` chip and files them under Holding Steady.
Symbols cross over automatically the day they reach 252 bars. Symbols with
<60 bars are dropped from the payload entirely (existing guard).

## The dashboard — the `next` build (added 2026-08-07, sole dashboard since 2026-08-15)

It lives at **https://storage.googleapis.com/trendalert-data-rattle/next/dashboard.html**,
which is also what the alert posters link to (`DASHBOARD_URL` in
`backend/daily_summary.py`). It is built like this:

    frontend/next/         source tree (index.html + next.css + next-app.js)
    frontend/build_next.py inliner  ->  frontend/dashboard-next.html
    CI runs `python frontend/build_next.py --check`, so a stale artifact fails the build

It reuses `frontend/v2/trendalert-core.js` verbatim for every rule (zones, base
status, scoring, trend, sector mix, staleness) plus v2's Nocturne tokens and
`theme.css`. Only the presentation is new.

Three things worth knowing:

* **It is the only dashboard published now.** The workflow writes
  `/next/dashboard.html` and nothing else. Moving it to the bucket root is a
  deliberate edit to the "Publish redesign" step; see the freeze note below for
  why that move should leave a redirect behind.
* **It never publishes on load.** The live dashboard POSTs `universe` and `core`
  3s after every page load, which is why its URL cannot be handed out — a
  reviewer silently rewrites the universe by visiting it. Here a POST happens
  only when someone actually edits a list, and writes still need the password
  (`TA.post` skips without a token), so opening the link changes nothing.
  `frontend/make_review_copy.py` is still the tool for hiding the numbers
  themselves.
* **Functionally at parity with the live dashboard**, with two known gaps: the
  design's sparklines and the aged "Recent alerts" both need backend work
  (a short series inside `data.json`; an alert-history object — `data.json`
  carries only latest-bar crosses, so every alert row is labelled "today").
  Everything else is there: portfolio/watchlist/index navigation with per-list
  chips, add ticker, manage lists, the full settings modal including the write
  password, the detail drawer, the three linked charts with their layer and
  indicator controls, PB/RE marks, the awaiting-data group, search and the
  sector filter.

### The frozen dashboards (2026-08-15)

The alerts now link to `/next/dashboard.html`, and every other dashboard was
frozen: still live, never updated again. Nothing builds, checks or publishes
them.

| Object | Frozen at | Was |
|---|---|---|
| `gs://BUCKET/dashboard.html` | 2026-08-13 build | the v2 dashboard, what alerts used to link to |
| `gs://BUCKET/dev/dashboard.html` | 2026-08-12 build | staging copy of the above |
| `gs://BUCKET/review/dashboard.html` | 2026-07-27 snapshot | `make_review_copy.py` output, data baked in |

Three traps in this arrangement:

1. **A frozen page does not look frozen.** The two v2 objects still fetch the
   live `data.json`, so they render today's prices in the old UI indefinitely.
   The bucket root is the URL that was in every alert for months and is in
   browser history — expect to land on it by accident. Check the masthead, not
   the numbers, to tell which page you are on. If this bites, replace the root
   object with a redirect to `/next/dashboard.html`.
2. **`frontend/v2/` is NOT dead code.** `frontend/next/index.html` reaches back
   into `../v2/` for `trendalert-core.js` (all the rules), `theme.css`, the
   Nocturne tokens, Inter and lightweight-charts. Keep editing it. What is
   retired is only the v2 *artifact* — `frontend/dashboard.html` and its
   generator `frontend/build_dashboard.py`, both kept in the repo as the record.
3. **`frontend/dashboard.html` will drift and must not be redeployed.** CI no
   longer runs `build_dashboard.py --check`; it was dropped because
   `v2/trendalert-core.js` is still live code, so the check would fail on every
   core change and demand a rebuild of an artifact nobody publishes. The
   committed artifact is therefore a snapshot of 2026-08-13, not of the current
   source. To revive that dashboard, rebuild it first and restore the check.
