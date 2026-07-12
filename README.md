# TrendAlert

Personal US-equity + BTC momentum dashboard. GCS-hosted static frontend,
Cloud Functions backend on the Alpaca free tier.

**Not investment advice.** Signal score is a scanning rank; the booking rule
is a personal discipline tool.

## Layout

```
backend/
  main.py             # pipeline: fetch bars -> indicators -> score -> publish JSON
  scoring.py          # 5-bucket composite score (trend/momentum/volume/RS/risk)
  chart_backend.py    # BTC fetch, S/R, 52w fields, base-formation detector, chart.json
  notes_function.py   # HTTP API: notes.json + positions.json persistence
  requirements.txt
  tests/              # pytest regression tests (synthetic-data fixtures)
frontend/
  dashboard.html      # single-file dashboard (Lightweight Charts via CDN)
.github/workflows/
  deploy.yml          # push-to-main -> deploy both functions + dashboard
```

## 1. One-time: Git

```bash
git init trendalert && cd trendalert
# copy the files into the layout above, then:
git add -A && git commit -m "initial: pipeline, scoring, dashboard"
git branch -M main
git remote add origin git@github.com:YOURUSER/trendalert.git
git push -u origin main
```

## 2. One-time: Secret Manager (replaces env-var keys)

```bash
gcloud services enable secretmanager.googleapis.com

printf '%s' "YOUR_ALPACA_KEY_ID"    | gcloud secrets create alpaca-key-id    --data-file=-
printf '%s' "YOUR_ALPACA_SECRET"    | gcloud secrets create alpaca-secret-key --data-file=-

# grant the function's runtime service account access:
PROJECT=$(gcloud config get-value project)
SA="$PROJECT@appspot.gserviceaccount.com"   # or your Gen2 runtime SA
for s in alpaca-key-id alpaca-secret-key; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
done
```

`deploy.yml` mounts these via `--set-secrets`, so `main.py` keeps reading
`os.environ["ALPACA_KEY_ID"]` unchanged — the values just come from Secret
Manager instead of plain env vars. Delete the old env-var config after the
first successful deploy.

## 3. One-time: GitHub Actions auth

```bash
# minimal deploy SA
gcloud iam service-accounts create gh-deploy --display-name="GitHub deploy"
PROJECT=$(gcloud config get-value project)
for role in roles/cloudfunctions.developer roles/storage.objectAdmin roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:gh-deploy@$PROJECT.iam.gserviceaccount.com" --role=$role
done
gcloud iam service-accounts keys create key.json \
  --iam-account=gh-deploy@$PROJECT.iam.gserviceaccount.com
# paste key.json content into GitHub repo secret GCP_SA_KEY, then DELETE key.json
# also set repo secrets: GCP_PROJECT, GCS_BUCKET
```

## 4. Wiring recap

- `main.py` ends with:
  ```python
  scored = compute_scores(frames, frames["SPY"])
  extra  = enrich_summary_records(frames, scored)   # 52w + zones + base + score inputs
  for sym, rec in data_records.items(): rec.update(extra.get(sym, {}))
  publish(data_records, "data.json")
  publish(build_chart_json(frames), "chart.json")
  ```
- Dashboard meta bar: `JSON URL` -> data.json, `Sync API` -> notes function URL
  (persists notes AND tracked positions to GCS; localStorage is the offline cache).

## 5. Production checklist

- [ ] Bucket CORS locked to the dashboard origin (not `*`)
- [ ] notes function CORS origin locked (edit `_CORS` in notes_function.py)
- [ ] Cloud Monitoring alert on function failures
- [ ] Budget alert at $5/month
- [ ] `settings.json` publish (optional): mirror dashboard weight changes into
      scoring.py by having main.py read gs://bucket/settings.json before scoring
