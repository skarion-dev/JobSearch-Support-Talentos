# Scraping & Reporting Plan

Status date: 2026-08-11. This is the working operating plan for this local build:
what we tested, what actually works, and how results get pushed to Talentos.

No credentials, candidate names, or resume content belong in this file. Private
inputs live in `private/` and `.env` (both gitignored).

---

## 1. What we tested, and the evidence

Approaches are ranked by measured result, not by expectation.

| # | Approach | Result | Verdict |
|---|---|---|---|
| 1 | **Aggregator keyword search** (Adzuna `/search` by keyword, USA-wide) | 250 keywords → **5,186 jobs in ~1 min**; with pagination, 40 keywords → 1,756 jobs using 57 API calls | **Primary source** |
| 2 | **Deterministic ATS APIs** (Greenhouse, Lever, Workday, SmartRecruiters, Ashby, Workable, Recruitee, BambooHR) | Greenhouse/Stripe returned **562 jobs, 120 US**, with full descriptions, zero LLM cost, direct apply URLs | **Best quality when a company uses one** |
| 3 | **Aggregator bulk browse** (Adzuna date-sorted, matched to company list by name) | 1,000 postings → only **3 companies / 4 jobs** matched the 4,444-company list | Low yield; keep as secondary |
| 4 | **Web search for the real posting link** (title + company via DDGS) | **20/20** returned a plausible link; ~14/20 were genuinely job-specific | **Use to recover source links** |
| 5 | **AI scrape of a company careers page** (ScrapeGraphAI + deepseek-v4-flash) | 20 companies → mostly 0 jobs; careers pages are JS-rendered or link out | Long-tail fallback only |
| 6 | **AI scrape of Adzuna landing page for the employer link** | 17/20 found nothing; the 3 "hits" were other Adzuna URLs | **Dead end — abandoned** |

### Two hard limits worth remembering

- **Adzuna never exposes the employer's apply URL.** Its `redirect_url` is a
  tracking page, and the `/apply` endpoint returns `403 error=logged_out`
  without a user session. This is deliberate lead-gating. Approach #4 exists
  because of this. Do not spend more effort trying to unwrap Adzuna links.
- **Adzuna descriptions are truncated at ~500 chars.** 6,907 of 6,942 stored
  jobs end mid-sentence. Full text requires visiting the real source (#4 → #5),
  which is why description enrichment is a separate, slower stage.

---

## 2. Current pipeline

```
                 ┌──────────────────────────────────────────┐
  data/keywords  │ 1,147 keywords (from Talentos export,     │
      .csv       │ deduped + ranked by occurrence)           │
                 └───────────────┬──────────────────────────┘
                                 ▼
   STAGE 1   scripts/keyword_search.py      Adzuna keyword search, paginated,
   discovery                                parallel, hard call budget (250/day)
                                 │          → keyword_jobs
                                 ▼
   STAGE 2   scripts/backfill_source_links  Web search (title + company) to find
   link recovery                            the real posting URL
                                 │          → keyword_jobs.source_url
                                 ▼
   STAGE 3   scripts/backfill_descriptions  AI scrape of source_url for FULL
   enrichment                               description + verified posted_date
                                 │          → keyword_jobs.description/posted_date
                                 ▼
   STAGE 4   scripts/match_resumes_to_jobs  Per base resume: deterministic
   matching                                 prefilter → deepseek-v4-flash batch
                                 │          scoring → rank → top 50
                                 ▼          → resume_job_matches
   STAGE 5   Streamlit "Resume Matches"     Human review before anything is
   review                                   pushed anywhere
```

A parallel, separate track keeps the original **4,444-company list** alive
(`scripts/daily_scrape.py`): deterministic ATS detectors first, cached per
company in `scrape_methods`, AI scrape only as fallback. That track answers
"what are the companies we've applied to posting today," which is a different
question from "what's on the market for this resume."

### Hardcoded rules

- **USA only** (`app/filters.py`) — strict allowlist of US state/territory
  signals plus an explicit non-US denylist. Applied to every scrape path.
- **10-day freshness** — filter on ingest, plus `purge_old_jobs(days=10)` at the
  start of each daily run.
- **posted_date is null unless proven.** The enrichment agent must return
  `posted_date_evidence` (a quote from the page) or the date is discarded. A
  scrape timestamp is never a posting date.

### Models

| Job | Model | Why |
|---|---|---|
| CEO agent (orchestration/chat) | `deepseek-v4-pro` | Reasoning over tools |
| Scraper / matcher / enrichment | `deepseek-v4-flash` | High volume, cheap |

Endpoint is **OpenCode Go** (`https://opencode.ai/zen/go/v1`) — the subscription
tier, not pay-per-credit Zen (`/zen/v1`), which returns `CreditsError`.

---

## 3. Measured throughput and known ceilings

| Stage | Rate | Limit that bites first |
|---|---|---|
| Keyword search | 250 keywords / ~60s at 20 workers | **Adzuna 250 API calls/day** |
| Source-link recovery | ~2,485 in ~10 min at 25 workers | Search engines 429 under sustained load — got 36% before throttling |
| Description enrichment | slow; **crashed at 50 workers** (`EPIPE` from the Playwright/Node layer) | Browser concurrency, not the LLM |
| Resume matching | 3 of 21 profiles in ~10 min at 6 workers | Wall-clock; each profile scores up to 250 jobs in batches of 25 |

**Corrections these numbers imply:**

1. Description enrichment must drop to **~8–10 browser workers** with retry and
   a resumable queue. 50 is past what the local Playwright driver survives.
2. Source-link recovery needs **backoff + rotation across search backends** and
   should run as a slow trickle, not a burst. Re-run repeatedly to fill gaps.
3. Resume matching should run **per-profile as separate scheduled invocations**
   rather than one long job, so a timeout can't wipe out the remaining profiles.
4. The 250-call Adzuna ceiling is the real constraint on discovery volume. More
   keywords per day requires either a paid tier or a second aggregator.

---

## 4. Local → Talentos push plan

Nothing is pushed today. Everything below is the design for when review says go.
**Jobs must land first**, because every downstream row references `jobs.id`.

### Step 0 — Selection gate: do NOT bulk-load the corpus

**Talentos must not be crowded with non-essential jobs.** The local corpus
(6,942 and growing daily) is a *discovery pool*, not something to mirror. It is
cheap to hold locally and expensive to dump into production — it pollutes
search, dedupe, categorization, and the AE's view.

A job earns its way into Talentos only if **all** of these hold:

1. It backs a match that survived scoring — `TOP_MATCH`, or `REVIEWABLE_MATCH`
   that a human explicitly kept.
2. A human approved that match in the Resume Matches review step.
3. It passes the quality gates: US-only, evidence-verified `posted_at` inside
   the window, real description text, and a recovered `source_url`.
4. It is not already in `jobs` under any dedupe key.

Everything else stays local and ages out via the 10-day purge. Expected volume
is **tens of jobs per push**, not thousands — roughly the count of approved
matches, minus jobs shared across candidates.

Corollary: a job with no approved match never enters Talentos, no matter how
good its score looked. Score alone is not admission.

### Step 1 — Insert/dedupe into `jobs` (approved matches only)

| Talentos `jobs` column | Local source | Notes |
|---|---|---|
| `title` | `keyword_jobs.title` | required |
| `company` | `keyword_jobs.company_name` | |
| `location` | `keyword_jobs.location` | |
| `posted_at` (date) | `keyword_jobs.posted_date` | **null unless evidence-verified** |
| `source` | literal `'adzuna'` / `'greenhouse'` / etc. | track provenance |
| `source_url` | `keyword_jobs.source_url` | the recovered real link |
| `apply_url` | `keyword_jobs.source_url` | when it is genuinely an apply page |
| `raw_description` / `description_text` | `keyword_jobs.description` | full text when enrichment succeeded |
| `external_job_id` | Adzuna posting id (parse from `job_url`) | best dedupe key |
| `is_active` | `true` | |
| `raw_source_payload` | original API record (jsonb) | audit trail |

**Dedupe order** (masterprompt rule 7): `external_job_id` → `ref_id` →
`source_url` / `apply_url` → normalized `company + title + location`. Keep the
freshest canonical row; never create a second row for the same posting.

**Gate before insert:** US-only, `posted_at` present and within the window, and
a description long enough to justify a match (~120+ chars). Anything failing
these goes to a needs-verification report, not into `jobs`.

### Step 2 — `target_jobs` (candidate ↔ job with a score)

Unique on `(candidate_id, job_id)`. Maps cleanly from `resume_job_matches`:
`fit_score` ← `score`, `recommendation` ← `band`, `raw_description` ← job text.
This is the safe first write: it records the match without creating work.

### Step 3 — `applications` (only after explicit approval)

This is the step that creates real AE workload, so it stays behind a flag
(`ALLOW_QUEUE_WRITES`) and a named human approval. Required field values:

```
source_type              = 'base_resume'
base_resume_id           = the exact matched base resume
assigned_to_user_id      = resolved active AE user
ae_stage                 = 'in_ai_pipeline'
resume_generation_status = 'queued'
applied_at               = NULL          -- never mark applied
proof/submission fields  = untouched
automation_idempotency_key set, one application event written
```

Then trigger the existing AI workflow for the application. Use the Talentos
service layer, **not** ad-hoc SQL from this repo — the app owns event creation,
resume versions, packets, and workflow rows, and bypassing it produces
half-created records.

### Step 4 — Audit

Write `candidate_job_match_runs` / `candidate_job_match_decisions` so every
recommendation is traceable to a run, score, rule outcome, and profile version.

### Non-negotiables for the push

- **Read-only until explicitly approved.** Today this build only reads Neon
  (company list, resume profiles) and writes locally.
- **Never bulk-load the discovery pool.** Only jobs backing an approved match
  cross over (Step 0). The local corpus stays local.
- Only profiles that are `review_status='approved'` with
  `approved_profile_version = profile_version` may auto-queue. Currently **1 of
  21** profiles meets that bar; the rest are report-only by design.
- Test/demo accounts are excluded from production runs unless explicitly named.
- Re-runs must be idempotent: no duplicate applications, no resurrecting a
  manager-dismissed keyword or a rejected job.

---

## 5. Schema comparison — local vs Talentos

The local schema is deliberately a superset staging area, not a mirror.

| Concept | Local | Talentos | Reconciliation |
|---|---|---|---|
| Job | `keyword_jobs` (aggregator) + `jobs` (per-company) | `jobs` | Local splits by discovery path; both collapse into Talentos `jobs` on push |
| Posted date | `posted_date` TEXT, null unless proven | `posted_at` DATE | Direct; nulls stay null |
| Description | single `description` | `raw_description`, `description_text`, `description_html`, `parsed_description` | Map to `raw_description` + `description_text`; leave parsing to Talentos |
| Real link | `source_url` (recovered) + `job_url` (Adzuna) | `source_url`, `apply_url` | `source_url` → both when it is an apply page |
| Match | `resume_job_matches` (score/band/reason/terms) | `target_jobs` + `candidate_job_match_decisions` | score → `fit_score`, band → `recommendation` |
| Profile | `resume_profiles` (read-only cache) | `candidate_resume_search_profiles` | **Local is a cache. Talentos is source of truth. Never write back.** |
| Company | `companies` (4,444 exported) | `companies` | `source_id` holds the Talentos uuid |

**Gaps to close before any push:**

1. `external_job_id` is not yet parsed out of the Adzuna URL — needed as the
   primary dedupe key.
2. Provenance (`source`) is not stored per row in `keyword_jobs` yet; currently
   inferable only from which script wrote it.
3. `salary` is a free-text string locally; Talentos wants
   `salary_min`/`salary_max`/`currency`/`period` as separate typed columns.
4. No `raw_source_payload` retained — the original API record is discarded after
   mapping, which weakens the audit trail.

---

## 6. Next actions

Ordered by what unblocks the most.

1. Re-run source-link recovery with backoff to lift coverage past 36%.
2. Re-run description enrichment at ~8 workers with a resumable queue.
3. Run resume matching **per profile** so all 21 complete (3 done so far).
4. Add `external_job_id`, `source`, and `raw_source_payload` to `keyword_jobs`
   (closes gaps 1, 2, 4 above).
5. Split `salary` into typed min/max/currency/period (gap 3).
6. Add an **approve/reject control** to the Resume Matches tab. Nothing can be
   pushed until a human marks a match approved — that flag is what Step 0 reads.
7. Build the push script as **dry-run first**: given the approved matches only,
   report exactly which jobs would be inserted, with dedupe decisions shown, and
   change nothing. Expect tens of rows; if the dry run proposes thousands, the
   selection gate is broken — stop and fix it.
8. Only after that report is reviewed: enable `jobs` + `target_jobs` writes.
   `applications` writes stay off until separately approved.
