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
