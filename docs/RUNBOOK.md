# Runbook

Operational guide. For design rationale see [ARCHITECTURE.md](ARCHITECTURE.md);
for the measured comparison of sourcing approaches see
[PIPELINE_PLAN.md](PIPELINE_PLAN.md).

---

## Setup

```bash
git clone https://github.com/skarion-dev/JobSearch-Support-Talentos.git
cd JobSearch-Support-Talentos
pip install -r requirements.txt
python -m playwright install chromium      # only needed for the AI page scraper

cp .env.example .env                       # then fill in the values below
python -m scripts.sync_apify_tokens        # pulls the Apify pool from Talentos
streamlit run app/main.py --server.port 3100
```

`.env` keys:

| Key | Notes |
|---|---|
| `OPENCODE_API_KEY` | OpenCode **Go** subscription |
| `OPENCODE_BASE_URL` | `https://opencode.ai/zen/go/v1` — the `/zen/v1` path is the pay-per-credit tier and returns `CreditsError` |
| `ADZUNA_APP_ID` / `_KEY` | plus `_2` for a second account; rotates automatically |
| `APIFY_TOKEN_1..20` | written by `sync_apify_tokens` |
| `NEON_DB_URL` | Talentos database |

Never commit `.env` or `private/`. Both are gitignored.

---

## Daily operation

### Automatic — 00:00

```bash
python -m scripts.daily_cycle
```

Register on Windows:

```bash
schtasks /Create /TN JobSearchNightly /TR "powershell -ExecutionPolicy Bypass -File C:\JobSearch-Support-Talentos\nightly.ps1" /SC DAILY /ST 00:00 /RU <user> /F
```

Runs sync → keywords → ingest → enrich → match → report, then stops. It does
**not** write to Talentos. Output goes to `daily_cycle.log`.

### Manual — each morning

1. Open http://localhost:3100 → **Review & Assign**
2. Pick a candidate, optionally filter to one base resume, set a minimum score
3. Tick the matches worth sending
4. Choose an AE from the live roster
5. **Assign** — creates the applications and queues them in the AI pipeline

---

## Useful commands

| Task | Command |
|---|---|
| Full nightly cycle | `python -m scripts.daily_cycle` |
| Re-match only (no ingest) | `python -m scripts.daily_cycle --skip-ingest` |
| Refresh candidate roster | `python -m scripts.sync_resume_profiles` |
| Re-rank keywords by measured ROI | `python -m scripts.rank_keyword_roi --top 500` |
| Adzuna pull | `python -m scripts.keyword_search --source roi --top 250 --days 1` |
| Apify pull | `python -m scripts.apify_ingest --source linkedin --top 120 --days 1` |
| Match all profiles | `python -m scripts.match_resumes_to_jobs --workers 100 --posted-days 1` |
| Preview a push (no writes) | `python -m scripts.push_to_talentos --limit 20` |
| Execute a push | `python -m scripts.push_to_talentos --limit 20 --commit` |
| Enforce one candidate per job | `python -m scripts.deduplicate_candidate_jobs --commit` |

Every write path is dry-run by default. `--commit` is always required.

---

## Verifying a push landed correctly

```sql
-- volume and pipeline state
SELECT a.ae_stage, w.status, count(*)
FROM applications a
LEFT JOIN application_ai_workflows w ON w.application_id = a.id
WHERE a.source = 'jobsearch_support'
GROUP BY 1,2;

-- must always be 0: we never mark anything applied
SELECT count(*) FROM applications
WHERE source='jobsearch_support' AND (applied_at IS NOT NULL OR proof_url IS NOT NULL);

-- must always be 0: one candidate per job
SELECT count(*) FROM (
  SELECT job_id FROM applications WHERE source='jobsearch_support'
  GROUP BY job_id HAVING count(DISTINCT candidate_id) > 1
) x;
```

Healthy pipeline: workflows move `queued → running → completed` on their own and
applications advance `in_ai_pipeline → ready_for_review`. If everything sits at
`queued` for hours, the Talentos dispatcher is not running — that is a Talentos
issue, not this repo's.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CreditsError` from OpenCode | Using `/zen/v1` (pay-per-credit) instead of `/zen/go/v1` | Fix `OPENCODE_BASE_URL` |
| Adzuna 429 | Daily quota spent | Rotation handles it; add a third account via `ADZUNA_APP_ID_3` |
| LinkedIn actor 400 | Invalid `publishedAt` | Only `r86400`/`r604800`/`r2592000` are accepted; code snaps to these |
| Indeed returns 0 rows | OR-joined keyword list | Indeed must be driven one keyword per run (`--chunk 1`, already the default) |
| `InvalidColumnReference` on push | `ON CONFLICT` missing the partial-index predicate | Already fixed; the transaction rolls back cleanly with no partial writes |
| Search engine 429 during enrichment | Too many concurrent searches | Throttle is process-wide; keep `--workers` ≤ 6 |
| Playwright `EPIPE` | Too many concurrent browsers | The HTTP+LLM enricher replaced it; do not exceed ~10 browser workers |
| Candidate missing from matches | Not `pipeline_stage='applying'` | Check the STAGE column in Talentos — `status` is `'active'` for everyone and gates nothing |

---

## Safety rules

These are enforced in code. Do not weaken them without reading why they exist.

1. **Talentos is read-only** except `push_to_talentos.py` and
   `deduplicate_candidate_jobs.py`, both dry-run by default.
2. **`applied_at` and proof fields are never set.** Marking a job applied is a
   human action.
3. **Only jobs backing an approved match** reach Talentos. The local corpus is
   a discovery pool, not something to mirror.
4. **One candidate per job.** Several candidates share a discipline; without
   this, four people competed for one opening through the same agency.
5. **Location and seniority gates run in code, not in prompts.** A rule that
   lives only in a prompt gets ignored — that is how a DMV-only candidate
   received eight California jobs.
6. **`posted_date` requires evidence.** A scrape timestamp must never be
   recorded as a posting date.

---

## Adding a candidate

Nothing to do here. Set their stage to Active in Talentos and give them an
approved base resume; the nightly sync picks them up.

If they have a base resume but no generated search profile, the sync
synthesizes keywords from the resume's own skills and experience rather than
skipping them — that path was added after an active candidate was silently
dropped for months.

To constrain someone geographically, add a gate in
`app/filters.py::GATES` and map the candidate to it in
`scripts/sync_resume_profiles.py::LOCATION_GATES`.
