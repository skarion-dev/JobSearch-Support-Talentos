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
  1. A candidate with a large unreviewed backlog is a person not getting
     applications sent. That is the core failure of this system.
  2. A candidate with few or no matches is being underserved by the keyword
     strategy — worse than a backlog, because there is nothing to review.
  3. Stale backlog (matches days old, never actioned) means the daily review
     is not happening.
  4. Pipeline stalls are a Talentos dispatcher problem, not a sourcing problem —
     say so explicitly. But a deep queue is NOT a stall on its own. Check
     pipeline_throughput: if completed_last_15min > 0 the pipeline is draining
     normally and you must NOT report it as stalled or critical, however many
     items are queued. Only call it stalled when nothing has completed
     recently.

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
