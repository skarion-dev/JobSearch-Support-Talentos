# Contributing

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the system fits
together, [docs/RUNBOOK.md](docs/RUNBOOK.md) for how to operate it, and
[docs/GATEWAY.md](docs/GATEWAY.md) for the LLM gateway (`gateway/`) that
proxies OpenCode Go for this app and, eventually, Talentos. This file is
about working on the code safely.

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
run_app.py              Start the app from any directory
app/
  main.py               Streamlit entry point and tab wiring
  auth.py               Cloudflare Access identity
  review_tab.py         Review & Assign — the only path to Talentos
  manual_chase_tab.py   What cannot be automated, as a downloadable sheet
  dashboard_tab.py      Ops dashboard, achieved ATS scores, sourcing quality
  quality.py            ONE definition of a usable job description
  talentos_state.py     ONE definition of "already logged" — used everywhere
                        a count is shown, not only at push time
  exports.py            The manual-chase workbook (UI, CLI and nightly share it)
  config.py             Env config and model selection
  db.py                 Local SQLite access
  filters.py            USA filter and regional gates (enforced in code)
  agents/
    keyword_strategist.py  kimi-k2.7-code (fallback minimax-m3), picks nightly keywords
    matcher_agent.py       deepseek-v4-flash, scores job vs resume
    description_agent.py   deepseek-v4-flash, extracts full descriptions
    analyst_agent.py       deepseek-v4-flash, narrates the dashboard
    ceo_agent.py           deepseek-v4-pro, conversational front door
    link_finder.py         web search, no LLM
    ats_detectors.py       Greenhouse/Lever/Workday/... public APIs, no LLM
    aggregators/           Adzuna and Apify clients
gateway/                  OpenAI-compatible proxy in front of OpenCode Go —
                          model allowlist, key rotation, per-client limits.
                          See docs/GATEWAY.md.
scripts/
  daily_cycle.py            the nightly orchestrator
  push_to_talentos.py       the only write path
  deduplicate_candidate_jobs.py
  gateway_issue_key.py      issue a gateway client token
  gateway_admin.py          list/enable/disable/revoke clients, kill switch
  ...
docs/                       architecture, runbook, gateway, measured pipeline plan
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
codes matched in original case, LinkedIn's fixed time windows, the actor's
`min_items: 150`: each of those looks like a mistake and is not.

**A threshold belongs in one place.** The 1,500-character floor decides whether
a real person gets a real application. It lived in four files and the review UI
applied none of them, so operators selected rows that silently vanished. It is
`app/quality.py` now — import it, never re-type the number.

**What the UI promises must be what the push delivers.** Any filter the push
applies has to be visible in Review & Assign, or the button lies. There is a
consistency check for this; run it after touching either side:

```bash
python -c "import logging;logging.disable(logging.INFO);from datetime import date,timedelta;from app.review_tab import matches_for,_norm,overview;from app.quality import MIN_DESCRIPTION;from scripts.push_to_talentos import load_matches;d=date.today();f=d-timedelta(days=3650);[print(c['candidate_name'],len({(_norm(m['company_name']),_norm(m['title'])):m for m in sorted([x for x in matches_for(c['candidate_name'],'All',90,f,d) if (x['desc_len'] or 0)>=MIN_DESCRIPTION],key=lambda x:-x['score'])}),len([m for m in load_matches(None,90,3650,per_candidate_cap=None,candidate=c['candidate_name']) if len(m['description'] or '')>=MIN_DESCRIPTION])) for c in overview(f,d)]"
```

**Measure quality, not throughput.** 431 applications once averaged an ATS score
of 0.27 with 367 zeros while every queue, stage and chart looked healthy. The
dashboard reads scores back out of Talentos for exactly this reason. A change
that increases volume and lowers `overall_avg_ats` is a regression.

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

---

## Getting onto the server

The server is the spare PC at `192.168.1.193`, user `saki-`. It runs the app,
the tunnel and the nightly cycle, and holds the only real database.

### 1. Generate a key (on your machine)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/jobsearch -C "yourname@skarion"
cat ~/.ssh/jobsearch.pub
```

Send the **.pub** line to the operator. Never send the private key.

### 2. Operator adds it

The account is a Windows administrator, so OpenSSH reads
`administrators_authorized_keys`, **not** the per-user `.ssh/authorized_keys`.
Adding it to the wrong file silently fails to authenticate.

```powershell
$key = "<paste the ssh-ed25519 ... line>"
Add-Content "$env:ProgramData\ssh\administrators_authorized_keys" $key
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /grant "SYSTEM:F"
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /grant "BUILTIN\Administrators:F"
Restart-Service sshd
```

### 3. Connect

```bash
ssh -i ~/.ssh/jobsearch saki-@192.168.1.193
```

You must be on the same network, or on the VPN. Cloudflare Access covers the
web app only — it does not tunnel SSH.

### Pulling the latest code on the server

```bash
ssh -i ~/.ssh/jobsearch saki-@192.168.1.193 \
  "powershell -ExecutionPolicy Bypass -File C:\JobSearch-Support-Talentos\deploy.ps1"
```

That is the normal way. Use a bare `git pull` only if you do not want the app
restarted.

### Notes on running commands over SSH

The default remote shell is `cmd.exe`, so `;` does not chain commands. Wrap
anything non-trivial:

```bash
ssh ... "powershell -Command \"cd C:\JobSearch-Support-Talentos; git log --oneline -3\""
```

For anything longer, write a `.ps1`, `scp` it over and run it with
`-ExecutionPolicy Bypass -File`. Quoting through bash then SSH then PowerShell
mangles quickly, and non-ASCII characters get corrupted in transit — keep
scripts ASCII-only.

### What runs on the server

| Task | Schedule | Purpose |
|---|---|---|
| `JobSearchApp` | ONSTART | Streamlit on 127.0.0.1:3100 |
| `CloudflareTunnel` | ONSTART | Tunnel for jobs.skarion.com |
| `JobSearchNightly` | 00:00 daily | git pull, then the full cycle |

Nothing runs on any other machine. If you find a JobSearch task elsewhere,
delete it — a second machine scraping into its own database is how the
split-brain bug happened.
