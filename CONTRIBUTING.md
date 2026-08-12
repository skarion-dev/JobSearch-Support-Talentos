# Contributing

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the system fits
together and [docs/RUNBOOK.md](docs/RUNBOOK.md) for how to operate it. This
file is about working on the code safely.

---

## The one thing to understand first

**This repo writes to production Talentos.** `push_to_talentos.py` creates real
applications that real application engineers act on. A careless run creates
real work for real people.

Two rules protect you:

1. Every write path is **dry-run by default**. `--commit` is always required.
2. Nothing reaches Talentos except through `push_to_talentos.py` and
   `deduplicate_candidate_jobs.py`. Everything else is read-only against Neon.

Do not add a third write path without discussing it.

---

## Local setup

```bash
git clone https://github.com/skarion-dev/JobSearch-Support-Talentos.git
cd JobSearch-Support-Talentos
python -m venv .venv && .venv\Scripts\activate     # or source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

You need credentials in `.env` before anything runs — ask for them, they are
not in the repo. `.env` and `private/` are gitignored and must stay that way.

To work without touching production at all, point `NEON_DB_URL` at a copy, or
simply never pass `--commit`.

```bash
streamlit run app/main.py --server.port 3100
```

Get a working local database by copying `data/jobsearch.db` from the server
rather than re-scraping — a full rebuild costs an entire day's API budget.

---

## Layout

```
app/
  main.py               Streamlit entry point and tab wiring
  auth.py               Cloudflare Access identity
  review_tab.py         Review & Assign — the only path to Talentos
  dashboard_tab.py      Ops dashboard and charts
  config.py             Env config and model selection
  db.py                 Local SQLite access
  filters.py            USA filter and regional gates (enforced in code)
  agents/
    keyword_strategist.py  gpt-5.6-luna, picks nightly keywords
    matcher_agent.py       deepseek-v4-flash, scores job vs resume
    description_agent.py   deepseek-v4-flash, extracts full descriptions
    analyst_agent.py       deepseek-v4-flash, narrates the dashboard
    ceo_agent.py           deepseek-v4-pro, conversational front door
    link_finder.py         web search, no LLM
    ats_detectors.py       Greenhouse/Lever/Workday/... public APIs, no LLM
    aggregators/           Adzuna and Apify clients
scripts/
  daily_cycle.py            the nightly orchestrator
  push_to_talentos.py       the only write path
  deduplicate_candidate_jobs.py
  ...
docs/                       architecture, runbook, measured pipeline plan
```

---

## Conventions that matter here

**Rules go in code, not in prompts.** Every gate in `app/filters.py` exists
because a model ignored the prompt version. A DMV-only candidate received
eight California jobs when the constraint lived only in the prompt. If a
constraint must hold, enforce it deterministically and let the model see it as
context.

**Verify against the live schema, don't trust documentation.** The operating
masterprompt says to filter candidates on `status='active'` — that column is
`'active'` for every row including dropped and placed people. The real gate is
`pipeline_stage`. Check the database.

**Measure before optimising.** `docs/PIPELINE_PLAN.md` records six sourcing
approaches with numbers. Two obvious-sounding ideas produced nothing. Add your
result to that table rather than replacing the approach silently.

**Prefer deterministic over LLM.** ATS APIs, dedupe, and ranking maths use no
model. An LLM there is slower, costlier and less reliable than an HTTP call.

**Comment the why, not the what.** Especially for anything that looks odd —
it is usually load-bearing. `ON CONFLICT` repeating an index predicate, state
codes matched in original case, LinkedIn's fixed time windows: each of those
looks like a mistake and is not.

---

## Testing a change

There are no unit tests yet; verification is done against real data.

| Change | How to check it |
|---|---|
| Matching logic | `python -m scripts.match_resumes_to_jobs --workers 20 --posted-days 1` and inspect scores |
| Ingestion | `--top 5` first, confirm rows land with `external_job_id` and `apply_url` populated |
| Push path | Always dry-run first; the plan output tells you exactly what would be created |
| UI | `streamlit run app/main.py`, click through Review & Assign end to end |

Before opening a PR, confirm a dry-run push still reports sane numbers. If it
proposes thousands of applications, the selection gate is broken.

---

## Pull requests

- Branch from `master`, one concern per PR.
- Explain **why** in the description; the diff already shows what.
- Note anything you measured — numbers are how decisions get made here.
- Never commit `.env`, `private/`, `data/*.db`, or credentials of any kind.
  Check `git status` before committing; the `.gitignore` covers these but a
  `-f` flag defeats it.

---

## Getting access

| Thing | How |
|---|---|
| Repo | Public — clone freely; ask for push access |
| Credentials | Ask the operator; never share `.env` over chat or email |
| The running app | Cloudflare Access — your email must be added to the allowlist |
| The server | SSH to the host machine; ask for a key to be authorised |

**Any authenticated user of the app can push to production Talentos.** There is
no read-only role yet. Every push is attributed to the signed-in email, so
actions are traceable — treat the Assign button accordingly.

---

## Deploying a change

**A GitHub push does not update the running app.** Nothing polls the repo
during the day. The server keeps running the code it last pulled.

There are two ways your change goes live:

| When | What happens |
|---|---|
| **Tonight at 00:00** | The nightly task runs `git pull --ff-only` before the cycle, so a merged change is picked up automatically |
| **Immediately** | Run the deploy script on the server |

```bash
ssh saki-@192.168.1.193 "powershell -ExecutionPolicy Bypass -File C:\JobSearch-Support-Talentos\deploy.ps1"
```

`deploy.ps1` pulls, installs any new requirements, restarts the app, and
verifies it came back on `127.0.0.1:3100`. It refuses to restart if the pull
fails, so a bad merge leaves the previous version running.

### Everything runs on the server

The app, the tunnel, and the nightly cycle all run on the spare PC
(`192.168.1.193`), against one SQLite database.

This matters: the nightly cycle was briefly registered on a different machine
from the app. Both had their own `data/jobsearch.db`, so the cycle wrote
matches into a database the app never read — the UI would have shown nothing
new each morning with no error anywhere. If you add a scheduled job, put it on
the server.

### After changing the schema

`db.get_conn()` runs `db/schema.sqlite.sql` on every connect, and it uses
`CREATE TABLE IF NOT EXISTS`. New tables appear automatically; **new columns on
an existing table do not**. Add an `ALTER TABLE` guarded by try/except, as the
existing migrations do, or the column will be missing on the server while
working fine on your fresh local database.
