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

Runs sync → keywords → ingest → enrich → match → export → report, then stops.
It does **not** write to Talentos. Output goes to `daily_cycle.log`.

The last two lines of the log are the ones to read:

```
READY TO SEND: 405 of 780 matches across 7 candidates
automatable rate: 52% (375 for manual chase)
```

A falling automatable rate means the sourcing mix has drifted back towards
truncated sources — check the Sourcing quality panel on the Dashboard.

### Manual — each morning

1. Open http://localhost:3100 → **Review & Assign**
2. Pick a candidate, optionally filter to one base resume, set a minimum score
3. Tick the matches worth sending — every row shown can actually be sent; the
   thin ones are separated into their own expander
4. Choose an AE from the live roster
5. **Assign** — creates the applications and queues them in the AI pipeline
6. → **Manual Chase**, download the Excel, hand it to whoever chases by hand
7. → **Dashboard**, check *Avg ATS achieved* is above 7.49 and *Empty* is 0

### The one number that matters after a push

**Avg ATS achieved**, on the Dashboard. Volume tells you nothing: 431
applications once sat at an average of 0.27 with 367 zeros while every queue
and stage looked perfectly healthy, because the resumes were blank.

If **Empty** is above 0, workflows were queued without a `config_snapshot`.
**Re-push those applications rather than repairing them** — measured, repairs
score 0–1 and fresh pushes score 8–9.

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
| Manual-chase Excel | `python -m scripts.export_unpursued` |
| …for one candidate, last week | `python -m scripts.export_unpursued --days 7 --candidate "Bhaskar Roy"` |
| Start the app from anywhere | `python run_app.py` |
| Check Apify credit across the pool | `python -c "import app.config; from app.agents.aggregators.apify_jobs import probe_pool; print(probe_pool())"` |

Every write path is dry-run by default. `--commit` is always required.

`run_app.py` exists because `streamlit run app/main.py` only works from the repo
root — anywhere else every `from app import ...` fails with `ModuleNotFoundError`.

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
| LinkedIn actor 400 `maxItems must be >= 150` | Testing with a small `--max-items` | The actor has a hard floor; `min_items` in the actor spec now clamps it automatically |
| Apify 403 `Monthly usage hard limit exceeded` | That free tier is spent | Expected — `probe_pool()` retires spent tokens before the first real call. A spent token still authenticates, so only `/users/me/limits` reveals it |
| `No APIFY_TOKEN_* configured` despite a full `.env` | Import order — the token module read `os.environ` before `load_dotenv` ran | Fixed; tokens load lazily. Import `app.config` first if writing a bare script |
| Dashboard says "pipeline stalled" with 0 queued | Analyst inferring a stall from no recent completions | Fixed; `pipeline_status` is computed in SQL as idle/draining/stalled and an empty queue is `idle` |
| Assign button sends fewer than it promised | Thin descriptions or duplicate resume matches | Fixed; the UI now applies both filters, and a consistency check confirms UI count == push count |
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

---

## Remote access (Cloudflare Tunnel + Access)

The app runs on the spare PC and is reachable at **https://jobs.skarion.com**.

There is **no inbound port open**. Streamlit binds to `127.0.0.1:3100` only;
Cloudflare Tunnel makes the outbound connection, so the machine is not
reachable from the LAN or the internet except through Cloudflare.

### What runs where

| Component | Mechanism | Notes |
|---|---|---|
| Streamlit app | Scheduled task `JobSearchApp` (ONSTART) | binds 127.0.0.1 only |
| Cloudflare Tunnel | Scheduled task `CloudflareTunnel` (ONSTART) | tunnel `jobsearch` |
| Nightly cycle | Scheduled task `JobSearchNightly` (00:00) | on the main PC |

The tunnel runs as a scheduled task rather than a Windows service. The service
installer produced a service with no arguments and would not retain a binPath
override; scheduled tasks were already proven on this host, so that path was
taken instead.

### Granting someone access

1. Cloudflare dashboard → **Zero Trust → Access → Applications**
2. Add a self-hosted application for `jobs.skarion.com`
3. Add a policy allowing specific emails, or `@skarion.com` as a domain rule
4. They visit the URL, authenticate with Cloudflare, and are in

**Every authenticated user can push to production Talentos.** There is no
read-only role. The Access allowlist is the authorisation boundary — keep it
tight. Pushes are attributed to the signed-in email in `created_by`,
`updated_by`, and the application event text.

If someone reaches the app without an Access session, the sidebar shows an
"Unauthenticated" warning so a misconfigured deployment is visible rather than
silently open.

### Checking it is healthy

```bash
curl -o /dev/null -w "%{http_code}\n" https://jobs.skarion.com   # expect 200
ssh saki-@192.168.1.193 "cloudflared tunnel info jobsearch"      # expect connectors listed
```

If it returns 530, the tunnel has no active connector — restart the task:

```bash
ssh saki-@192.168.1.193 "schtasks /Run /TN CloudflareTunnel"
```
