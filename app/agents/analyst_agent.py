"""
Ops analyst — writes the leadership summary for the dashboard.

Runs on deepseek-v4-flash. It is a reporting job, not a decision: the numbers
are computed in SQL and handed over already correct, and the model's only task
is to say plainly what is going wrong and what to do about it. That keeps it
cheap and stops it inventing figures.
"""
import json
import logging

from openai import OpenAI

from app.config import OPENCODE_API_KEY, OPENCODE_BASE_URL, SCRAPER_MODEL

log = logging.getLogger("analyst")
client = OpenAI(api_key=OPENCODE_API_KEY, base_url=OPENCODE_BASE_URL)

SYSTEM = """You write the daily operations summary for leadership of a job-sourcing
pipeline that finds roles for placed candidates and queues applications.

You are given computed metrics. They are correct — never restate a number you
were not given, and never invent one.

Write for someone with 30 seconds. Lead with what is wrong or at risk, not with
what is fine. Be specific and concrete: name the candidate, the number, the
consequence. No filler, no praise, no hedging.

What actually matters here, in order:
  1. total_empty_resumes above 0 is the single worst thing that can appear here.
     It means tailored resumes were generated with no experience section, so
     those applications are unusable however good they look. Always report it as
     critical. The fix is to re-push those applications, not to repair them.
  2. overall_avg_ats below baseline_avg_ats means we are underperforming the
     manual pipeline we exist to scale. Say so plainly, and name the candidates
     whose avg_ats is lowest.
  3. A candidate with a large unreviewed backlog is a person not getting
     applications sent. That is the core failure of this system.
  4. A candidate with few or no matches is being underserved by the keyword
     strategy — worse than a backlog, because there is nothing to review.
  5. corpus_usable_pct is how much of what we ingest can actually be pushed. A
     low figure is a sourcing-mix problem: it means volume is coming from a
     source that truncates descriptions, and those matches go to manual chasing
     instead of becoming applications.
  6. seniority_mismatch_rate_pct above a few percent means junior candidates are
     scoring TOP_MATCH on Senior/Lead/Principal/Director postings they can't
     realistically get — measured at 13% before a hard gate was added for it.
     A nonzero rate after the gate shipped is stale pre-fix rows, not a new
     bug, unless it keeps growing after a fresh match run.
  7. Stale backlog (matches days old, never actioned) means the daily review
     is not happening.
  8. Pipeline health is ALREADY DECIDED for you in pipeline_status, which is
     exactly one of "idle", "draining" or "stalled", with the reasoning in
     pipeline_status_explanation. Use it verbatim and never second-guess it from
     the raw counts:
       - "idle"     -> nothing is queued. Say nothing about it. It is NOT an
                       issue and must never appear as one.
       - "draining" -> work is flowing. NOT an issue, however deep the queue.
       - "stalled"  -> the only case you may report, and it is a Talentos
                       dispatcher problem, not a sourcing problem. Say so.
     Do not infer a stall from completed_last_15min being 0; an empty queue has
     nothing to complete.

A high volume of applications is not a win on its own. Quality (avg_ats, no
zeros, no empty resumes) is what counts as a win.

Return JSON:
{"headline": "one sentence, the single most important thing",
 "status": "healthy" | "attention" | "critical",
 "issues": [{"severity":"high|medium|low","title":"short","detail":"one or two sentences naming numbers and who is affected","action":"the concrete next step"}],
 "wins": ["at most 2, only if genuinely notable"]}"""


def summarize(metrics: dict) -> dict:
    try:
        resp = client.chat.completions.create(
            model=SCRAPER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                    "Today's metrics:\n\n" + json.dumps(metrics, indent=2, default=str)},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        data.setdefault("issues", [])
        data.setdefault("wins", [])
        data.setdefault("status", "attention")
        data.setdefault("headline", "Summary unavailable.")
        return data
    except Exception as e:
        log.warning(f"analyst failed: {e}")
        return {
            "headline": "Analyst unavailable — metrics below are still accurate.",
            "status": "attention",
            "issues": [], "wins": [],
        }
