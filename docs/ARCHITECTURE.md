# Architecture & Daily Cycle

Status: 2026-08-11. Audience: engineers collaborating on this repo.

Read [PIPELINE_PLAN.md](PIPELINE_PLAN.md) first for *why* the pipeline is shaped
this way — it records the six sourcing approaches that were measured and which
ones failed. This document is the *what runs, when, and in what order*.

---

## 1. What this system does

Every night it finds the jobs posted in the last 24 hours that are worth a
Skarion candidate's application, ranks them per base resume, and waits for a
human to approve before anything reaches Talentos.

Two hard boundaries:

- **Talentos is read-only until a human clicks Assign.** The nightly cycle
  never writes to Talentos. Only `push_to_talentos.py`, invoked from the UI or
  CLI with an explicit approval, writes.
- **Only jobs backing an approved match cross over.** The local corpus is a
  discovery pool (~27k rows); Talentos gets tens per day. This is the
  "don't crowd Talentos" rule and it is enforced in code, not by convention.

---

## 2. Nightly cycle (00:00 local)

`scripts/daily_cycle.py` runs seven stages in order. Each is independently
re-runnable; a failure in one does not corrupt the others.

```
 00:00  STAGE 1  sync_resume_profiles
        Pull candidates where pipeline_stage='applying' from Talentos Neon
        (READ-ONLY) with their approved base resumes and search profiles.
        NOTE: candidates.status is 'active' for every row including dropped
        and placed people — pipeline_stage is the real gate.

        STAGE 2  keyword_strategist            [gpt-5.6-luna]
        Choose the top 500 search keywords for tonight. Sees measured ROI
        history, the active roster's disciplines, and coverage gaps. Output
        is validated against the profile vocabulary before use.

        STAGE 3  ingest — Apify FIRST, Adzuna second
        Apify       LinkedIn / Indeed / Google actors, 24h window, token
                    rotation. Returns the FULL posting text, so these are
                    the jobs that can actually be pushed.
        Adzuna      keyword search on a reduced budget. Broad board
                    coverage, but truncates at ~500 chars.
        Deduped on external_job_id -> apply_url -> source_url ->
        normalized company+title.

        STAGE 4  enrich
        Recover a readable employer/ATS link for matched jobs, then pull the
        full description. Apify rows already carry full text and skip this.

        STAGE 5  match                          [deepseek-v4-flash, 100-wide]
        Per base resume: deterministic prefilter (location gate, seniority
        and years rules, dedupe, keyword overlap) then batched LLM scoring.
        Work is flattened to (profile, batch) units so concurrency is not
        capped by the number of profiles.

        STAGE 6  export manual-chase sheet
        Everything that matched well but is too thin to automate is written
        to data/exports/manual_chase_<date>.xlsx, and is downloadable from
        the Manual Chase tab. These are good matches we cannot automate,
        not rejects — somebody chases them by hand.

        STAGE 7  report
        Per-candidate sendable-vs-matched counts and the automatable rate.
        Nothing is pushed. A human reviews in the UI.
```

### Why Apify leads the ingest

Measured over 28,325 ingested jobs:

| Source | Jobs | Usable (≥1500 chars) | Avg length |
|---|---|---|---|
| adzuna | 25,938 | 224 (1%) | 535 |
| apify:linkedin | 2,150 | 2,093 (97%) | 5,473 |
| apify:indeed | 227 | 225 (99%) | 5,622 |

Adzuna was 92% of the corpus and 9% of what could actually be pushed. It
truncates descriptions at ~500 characters and gates its own links behind a
login, so there is nothing left to scrape. About half of every candidate's
matches were being dropped at push time as a result.

Adzuna is kept rather than removed: it still produced 80 usable matches and
covers boards the actors do not reach. It is a discovery signal, not a source
of applications.

### Why 24 hours

A midnight cycle with a 24-hour window means each posting is considered exactly
once, on the morning after it appears. Every source supports it natively:
LinkedIn `publishedAt=r86400`, Adzuna `max_days_old=1`, Indeed `datePosted=1`.

LinkedIn rejects arbitrary windows with a 400 — it only accepts
`r86400`/`r604800`/`r2592000`, so requests snap up to the next valid window and
exact freshness is enforced locally on `posted_date`.

---

## 3. The agents

| Agent | Model | Runs | Job |
|---|---|---|---|
| **Keyword strategist** | `gpt-5.6-luna` | 1x/night | Pick tonight's 500 keywords from ROI history + roster |
| **Matcher** | `deepseek-v4-flash` | ~200 batches/night, 100-wide | Score jobs against a base resume |
| **Description enricher** | `deepseek-v4-flash` | per unenriched match | Extract full description + evidence-backed posted date |
| **CEO agent** | `deepseek-v4-pro` | on demand | Conversational front door in the UI |
| Link finder | *(no LLM)* | per unenriched match | Web search, throttled process-wide |
| ATS detectors | *(no LLM)* | per company | Greenhouse/Lever/Workday/etc. public APIs |

A strong model for the once-nightly strategic decision; a cheap fast model for
the tens of thousands of mechanical ones. ATS/aggregator calls use no LLM at
all — an LLM there would be slower and less reliable than an HTTP call.

### Keyword strategist context

The strategist is given, and must reason over:

- **Measured ROI per keyword** — jobs pulled, matches produced, distinct
  profiles served, top-match rate. Evidence, not theory.
- **Proven waste** — keywords that pulled volume and converted nothing
  (`DOT compliance` 200 jobs/0 matches, `QA/QC` 186/0, `BOM` 140/0).
- **Active roster** — each candidate's disciplines and resume vocabulary, so
  underserved profiles get coverage instead of only the crowded CAD cluster.
- **The role-vs-tool lesson** — `"AutoCAD Drafter"` converts at 60%, bare
  `"AutoCAD"` returns noise. Job boards match titles, not skills.
- **Exploration budget** — a slice reserved for untried terms so the set does
  not ossify around today's winners.

Output is validated in code: every keyword must exist in the profile
vocabulary or the measured history, so the model cannot invent terms.

---

## 4. Review and assignment UI

`app/main.py`, Streamlit, port 3100.

| Tab | Purpose |
|---|---|
| **Review & Assign** | **The daily workflow: per-candidate ranked matches, filter by base resume, pick an AE, assign** |
| **Manual Chase** | **Good matches automation cannot pursue, as a downloadable Excel worklist** |
| Dashboard | Backlog, pipeline health, achieved ATS scores, sourcing quality |
| CEO Chat | Ask for stats or trigger runs conversationally |
| Scrape Control | Manual aggregator / fallback runs |
| Readiness | Which companies have a cached deterministic scraper |
| Jobs | Company-sourced jobs |
| Keyword Jobs | The raw discovery pool |

The Review & Assign tab is the only path to production:

1. Pick a candidate, optionally filter to one base resume.
2. Matches are listed ranked, with score, band, reason, location, posted date
   and a direct link.
3. Tick the ones to send. Choose an AE from the live roster (14 active
   `application_engineer` profiles plus admins).
4. **Assign** — runs the same guarded push as the CLI.

Nothing else in the UI writes to Talentos.

### The count on the button is the count that arrives

Two filters used to be applied at push time and nowhere else, so the UI could
promise more than it delivered:

- **Thin descriptions.** Anything under 1,500 characters is skipped by the
  push. The table showed those rows as selectable, so ticking forty could
  produce twenty applications with no explanation. They are now separated out
  and offered as the manual-chase sheet.
- **One application per candidate/job.** `applications` has a unique partial
  index on `(candidate_id, job_id)`, so a job matched by two of a candidate's
  own resumes yields one application. The UI now collapses those the same way
  the push does — for one candidate that was the difference between 64
  selectable rows and 56 real applications.

Both are verified by a consistency check: the UI count and the push count agree
exactly for every candidate.

### Manual Chase

About half of all matches cannot be automated, almost all Adzuna-sourced. They
are strong matches with a truncated description and an unrecoverable link.

The workbook is built once in `app/exports.py` and produced identically by the
UI download button, `scripts/export_unpursued.py`, and nightly stage 6, so an
operator downloading from the app and a developer running the CLI get the same
file. Two sheets: every job with a clickable link and the reason automation
gave up, plus a summary by candidate and reason.

---

## 5. Writing to Talentos

`scripts/push_to_talentos.py`. Dry run by default.

Per approved match, in order:

```
jobs                     insert if absent (4-pass dedupe)
target_jobs              candidate/job link + fit score
applications             source_type=base_resume, ae_stage=in_ai_pipeline,
                         assigned to chosen AE, applied_at NULL
application_events       one 'assigned' event
application_ai_workflows status=queued  -> the dispatcher picks it up
```

Then `deduplicate_candidate_jobs.py` enforces one candidate per job.

### Constraints that matter

- `jobs` has **no** unique index on `external_job_id`/`source_url`/`apply_url`.
  The database will not prevent duplicate jobs; dedupe is entirely our job.
- `applications` has a **unique partial** index on `(candidate_id, job_id)`.
  `ON CONFLICT` must repeat the predicate or Postgres raises
  `InvalidColumnReference`.
- `automation_idempotency_key` is deterministic, so re-running repairs rather
  than duplicates.
- `ae_stage` and `application_stage` are kept in sync, as every existing
  `base_resume` row does.
- `applied_at` and proof fields are never set. Marking a job applied is a
  human action.

### One-candidate-per-job

Several candidates share a discipline — the CAD/drafting cluster all draft with
AutoCAD/Civil 3D. Without this, one job went to four candidates: four people
competing for one opening through the same agency.

Winner is chosen by pipeline progress, then match score, then current load, so
one strong generalist does not absorb the queue. The rule refuses to displace
anything already advanced or belonging to another source — in practice it
correctly deferred to two candidates who had already applied manually.

---

## 6. Data model (local SQLite, `data/jobsearch.db`)

| Table | Holds |
|---|---|
| `keyword_jobs` | Discovery pool. Carries every Talentos dedupe key: `external_job_id`, `job_url`, `source_url`, `apply_url`, `source`, `company_url` |
| `resume_profiles` | Read-only cache of active candidates + base resumes. `location_gate` carries machine-enforced regional limits |
| `resume_job_matches` | Score, band, reason, matched terms per (profile, job) |
| `companies` | 4,444 exported from Talentos, for the company-scraping track |
| `scrape_methods` | Cached deterministic scraper per company |
| `daily_runs`, `match_runs` | Run history |

Talentos remains the source of truth for candidates, resumes, approvals and
application state. Local tables are a cache and a workspace; nothing here is
authoritative and nothing is written back except through the push script.

---

## 7. Rules enforced in code, not in prompts

A rule that lives only in a prompt is not a rule. Each of these was added after
the model violated the prompt version:

| Rule | Where | Why |
|---|---|---|
| USA only | `app/filters.py` | — |
| Regional gates (e.g. DMV 100mi or remote) | `app/filters.py::GATES` | The model returned 8 California jobs for a DMV-only candidate; "Flex" in a title read as remote |
| State codes matched in original case | `app/filters.py` | Uppercasing made "De Pere, Brown County" (WI) parse as Delaware |
| Job dedupe before scoring | `matcher_agent::_dedupe` | One posting occupied 8 of a candidate's 50 slots |
| One candidate per job | `deduplicate_candidate_jobs.py` | 4 candidates on one opening |
| posted_date requires evidence | `description_agent` | A scrape timestamp must never pass as a posting date |
| Usable-description floor (1,500 chars) | `app/quality.py` | One definition, shared by push, review, export and dashboard. It previously existed as four separate copies of the number, and the review UI applied none of them |
| Pipeline health: idle / draining / stalled | `dashboard_tab::pipeline_throughput` | The analyst called an **empty** queue "critical — pipeline stalled". A stall needs work that is not moving; both conditions are now decided in SQL and the model is told to use the verdict verbatim |
| Actor input minimums | `apify_jobs::ACTORS[*]["min_items"]` | The LinkedIn actor rejects `maxItems < 150` with a 400 that looks exactly like a token failure |
| 400 never rotates tokens | `apify_jobs::run_actor` | A bad payload gets the same 400 from every account, so retrying would burn all 20 tokens on our own bug |

---

## 8. Credentials

All in `.env` (gitignored). Nothing is committed.

| Key | Use |
|---|---|
| `OPENCODE_API_KEY` + `OPENCODE_BASE_URL` | OpenCode **Go** (`/zen/go/v1`) — the subscription tier. The pay-per-credit Zen path (`/zen/v1`) returns `CreditsError`. |
| `ADZUNA_APP_ID/KEY` (+ `_2`) | Aggregator, ~250 calls/account/day, auto-rotating |
| `APIFY_TOKEN_1..20` | Synced from Talentos `job_agent_apify_tokens`, auto-rotating |
| `NEON_DB_URL` | Talentos database |

`private/` holds the operating masterprompt and is gitignored.

---

## 9. Known gaps

1. Description enrichment tops out around 30% via HTTP — LinkedIn/Indeed/etc.
   return an anti-bot shell. Apify-sourced rows are unaffected (100% full
   text), so the fix is to shift more volume to Apify.
2. Adzuna is capped at ~500 calls/day across two accounts.
3. `company_ats_resolver` resolves only ~12% of companies; this corpus skews to
   staffing agencies and enterprises on Workday/iCIMS rather than
   Greenhouse/Lever.
4. Akash is `role=admin`, not `application_engineer`. If AE dashboards filter
   by role, applications assigned to him may not appear where expected.
