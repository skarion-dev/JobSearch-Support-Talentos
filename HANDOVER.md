# Handover

Snapshot for anyone (human or AI) picking this project up. For durable
policy and architecture, read [CONTRIBUTING.md](CONTRIBUTING.md),
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/RUNBOOK.md](docs/RUNBOOK.md),
[docs/GATEWAY.md](docs/GATEWAY.md), and [docs/PIPELINE_PLAN.md](docs/PIPELINE_PLAN.md)
first — this file is "where things are and what's happening right now,"
not a replacement for those.

---

## Where everything lives

| What | Where |
|---|---|
| GitHub repo (public) | https://github.com/skarion-dev/JobSearch-Support-Talentos |
| Dev checkout (this machine) | `C:\Users\sakis\Documents\Claude\JobSearch-Support-Talentos` |
| **Production** — app, nightly cycle, LLM gateway all run here | `192.168.1.193`, user `saki-`, repo at `C:\JobSearch-Support-Talentos` |
| Live app (LAN) | `http://192.168.1.193:3100` |
| Live app (public) | `jobs.skarion.com` via Cloudflare Tunnel |
| LLM gateway (local proxy in front of OpenCode Go) | `http://127.0.0.1:8787` on the production machine only |
| Talentos production DB | Neon Postgres — `NEON_DB_URL` in `.env`, READ-ONLY except `scripts/push_to_talentos.py` and `scripts/deduplicate_candidate_jobs.py` |
| Local job/match cache | SQLite, `data/jobsearch.db` on whichever machine runs it — gitignored, not synced between machines |

**A GitHub push does not update production.** Someone has to SSH in and run
`deploy.ps1` (see below). This has bitten this project before — see the
"Split-brain DB" and nightly-cycle-silently-did-nothing incidents in
`docs/RUNBOOK.md`'s troubleshooting table.

---

## Getting in

- **GitHub**: repo is public, clone freely; ask `skarion-dev` for push access.
- **SSH to production**: `ssh -i ~/.ssh/id_ed25519_jobsearch saki-@192.168.1.193`.
  The key on this dev machine is already authorized. A different machine
  needs its own key added to `saki-`'s `authorized_keys` on the production
  box, or a copy of this one — ask the operator, don't just copy private
  keys around casually.
- **`.env` credentials** (OpenCode Go key, Adzuna, Apify token pool, Neon
  URL, gateway admin token): gitignored, never in the repo, never over
  chat/email. Ask the operator. `.env.example` in the repo root lists every
  key needed with no real values.
- **The public app URL**: currently gated only by a shared admin
  password (`admin` / see operator) — Cloudflare Access is NOT configured
  yet despite the tunnel being live. See "Open items" below.

---

## Deploying a change

```bash
git push origin master
ssh -i ~/.ssh/id_ed25519_jobsearch saki-@192.168.1.193 "cd C:\JobSearch-Support-Talentos && powershell -ExecutionPolicy Bypass -File deploy.ps1"
```

`deploy.ps1` pulls, installs new requirements, and restarts the app.
**It kills every `python.exe` process on the machine to do that restart** —
not just the app's own. If anything else is running there (a one-off
script, a manual test), it dies too. Don't deploy while something else is
mid-run; there's no scoping in that kill step currently.

---

## Known gap: some operational scripts exist only on production, never committed

`cf_task.ps1`, `daily_scrape.ps1`, `deploy.ps1`, `fix_tunnel.ps1`,
`launch_app.ps1`, `run_app.ps1`, `run_tunnel.ps1`, `tok_install.ps1` all
live directly on `192.168.1.193` and are **not in git** — `nightly.ps1` is
the one exception, it's tracked. If that machine were ever rebuilt from a
fresh `git clone`, these would be missing and the deploy/tunnel/scheduling
setup would need to be reconstructed by hand. Worth pulling these into the
repo (strip anything environment-specific first) rather than leaving them
as the single point of failure they currently are.

---

## Current state (as of 2026-08-14)

- **Seniority matching was recently overhauled.** Candidates were scoring
  TOP_MATCH against Senior/Lead/Principal postings regardless of actual
  experience (measured: 13% of TOP_MATCH rows). Fixed with a real
  years-of-experience gate (`app/experience.py`) computed from resume
  dates + an education bonus (degree substitutes for experience, standard
  hiring pattern), with a tolerance band so borderline cases still reach
  the LLM instead of being auto-rejected. One candidate (Mir Najiur Rahman)
  has an explicit operator override in `scripts/sync_resume_profiles.py`
  (`EXPERIENCE_OVERRIDES`) — his computed years are high but real-world
  placement outcomes say he should be steered toward lower-requirement
  roles; that judgment can't come from any resume field, it's recorded
  there deliberately. Re-verify with `app/dashboard_tab.py::seniority_quality()`
  after any future change to matching logic — gate-violation rate should
  stay near 0%.
- **The LLM gateway (`gateway/`) is new** (added 2026-08-12) — a local
  proxy in front of OpenCode Go enforcing per-client daily token budgets,
  rate limits, and a model allowlist, since this app and Talentos itself
  share one OpenCode subscription through it. `jobsearch-app`'s daily
  budget (2,000,000 tokens, resets at UTC midnight) was fully exhausted by
  a single heavy day of matching/rescoring work — if that happens again,
  either wait for the reset, raise the budget via `scripts/gateway_admin.py`,
  or fall back to the raw upstream OpenCode credentials
  (`app.config._RAW_OPENCODE_API_KEY` / `_RAW_OPENCODE_BASE_URL`) for a
  one-off run, bypassing the gateway and its shared budget entirely.
- **`matcher_agent.py` now falls back to `mimo-v2.5`** if `deepseek-v4-flash`
  fails for any reason — same price, same endpoint shape. Does not help a
  gateway budget cap specifically (that's exhausted across every model a
  client can call), but covers real outages/errors that previously looked
  identical to "no matches found" (silent empty return, nothing logged).
- **Sourcing is Apify-first** (LinkedIn/Indeed/Google), Adzuna second on a
  reduced budget — Adzuna truncates descriptions at ~500 chars and is
  ~1% usable vs Apify's ~97%. See `docs/PIPELINE_PLAN.md` for the measured
  comparison across all six sourcing approaches tried.
- **`nightly.ps1` was recently broken and is now fixed** — it had hardcoded
  paths from a different machine (silent no-op every night, Task Scheduler
  still reported success) and then a `$ErrorActionPreference` bug that hung
  the child process instead of failing loudly. Both fixed and verified with
  a real end-to-end run before trusting it again.

## Open items

- Cloudflare Access is not configured — `jobs.skarion.com` is publicly
  reachable, gated only by the shared admin password.
- The untracked ops scripts on production (see above).
- Corpus usable-rate and seniority-mismatch-rate are both tracked on the
  Dashboard now (`app/dashboard_tab.py`) — check both after any matching
  or sourcing change, they're the fastest signal that something regressed.
