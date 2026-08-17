"""
Keyword strategist — chooses the search keywords for tonight's cycle.

Runs ONCE per night on kimi-k2.7-code, falling back to minimax-m3 on failure.
Everything else in this system runs on deepseek-v4-flash, but this is the one
decision where being wrong is expensive: a bad keyword set wastes the whole
night's API budget and starves a candidate of matches. It is one call, so a
strong model is affordable here.

The strategist is not asked to be creative. It is given measured evidence and
asked to allocate a fixed budget across it:

  * per-keyword ROI actually observed (jobs pulled, matches produced,
    distinct profiles served, top-match rate)
  * keywords proven to be waste (high volume, zero matches)
  * the active roster and which disciplines are underserved
  * an exploration budget so the set does not ossify

Output is validated against the known vocabulary — the model cannot invent a
keyword that no resume or history contains.
"""
import json
import logging
import re
from collections import defaultdict

from openai import OpenAI

from app import db
from app.config import (
    OPENCODE_API_KEY,
    OPENCODE_BASE_URL,
    STRATEGIST_FALLBACK_MODEL,
    STRATEGIST_MODEL,
)

log = logging.getLogger("keyword_strategist")

# max_retries above the SDK default: this is the one call gating the entire
# night's ingest (see daily_cycle.s2_keywords), so it's worth a few extra
# attempts against a transient blip before giving up.
client = OpenAI(api_key=OPENCODE_API_KEY, base_url=OPENCODE_BASE_URL, max_retries=5)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _extract_json(content: str) -> dict:
    """
    response_format={"type": "json_object"} does not guarantee a bare JSON
    payload — minimax-m3 (the fallback) prepends a <think>...</think>
    reasoning block before the object even in JSON mode, confirmed by hand
    2026-08-16. json.loads on the raw content fails outright in that case, so
    strip any think block first, then fall back to slicing between the first
    "{" and last "}" for anything else a model might wrap the JSON in.
    """
    cleaned = _THINK_RE.sub("", content).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise
        return json.loads(cleaned[start : end + 1])

SYSTEM = """You are the keyword strategist for Skarion's job-sourcing pipeline.

Once per night you choose which search keywords to spend the day's API budget
on. Job boards are queried with these terms; whatever they return is then
scored against each candidate's base resume by a separate matcher.

You are optimising ONE thing: the number of defensible, high-scoring matches
produced per API call spent, spread fairly across the active roster.

What the measured data has already shown:

1. ROLE TITLES CONVERT. TOOL AND ARTEFACT NAMES DO NOT.
   "AutoCAD Drafter" matched 27/45 jobs (60%). "CAD Drafter" 27/54 (50%).
   Meanwhile "QA/QC" pulled 186 jobs and matched 0, "DOT compliance" 200/0,
   "BOM" 140/0, "Floor Plans" 185/0. Job boards index titles. A skill or
   deliverable name returns high volume of irrelevant postings.

2. BREADTH IS VALUABLE. A keyword serving 9 profiles is worth far more than an
   equally precise one serving 1, because the cost is the same.

3. UNDERSERVED PROFILES MATTER MORE THAN CROWDED ONES. The CAD/drafting cluster
   is well covered. A profile producing few matches needs targeted terms even
   if their historical ROI is unproven.

4. NARROW BEATS GENERIC. Single generic tokens ("engineer", "design",
   "project") are useless. Two-to-four word role phrases are ideal.

Rules:
- Choose exactly the requested number of keywords.
- Only use keywords from the supplied vocabulary. Never invent one.
- Drop proven-waste keywords unless you can state why they will behave
  differently.
- Reserve roughly the requested exploration share for untried terms, weighted
  toward underserved profiles.
- Return JSON only."""

USER_TEMPLATE = """Select the top {n} keywords for tonight.

ACTIVE ROSTER — profiles needing coverage (matches produced in the last cycle):
{roster}

MEASURED PERFORMANCE — keywords with history (jobs -> matched, profiles served):
{measured}

PROVEN WASTE — high volume, zero matches (avoid unless justified):
{waste}

UNTRIED VOCABULARY — terms on active resumes never yet searched:
{untried}

Reserve about {explore_pct}% of the {n} slots for untried terms, prioritising
profiles marked underserved above.

Return JSON:
{{"keywords": ["...", ...],
  "reasoning": "2-3 sentences on how you allocated the budget",
  "dropped": ["proven-waste terms you deliberately excluded"]}}"""


def _gather_context(top_measured: int = 120, top_untried: int = 200) -> dict:
    with db.get_conn() as conn:
        perf = [dict(r) for r in conn.execute("""
            SELECT j.keyword,
                   count(DISTINCT j.id) jobs,
                   count(DISTINCT m.keyword_job_id) matched,
                   count(DISTINCT m.resume_profile_id) profiles,
                   sum(CASE WHEN m.band='TOP_MATCH' THEN 1 ELSE 0 END) tops
            FROM keyword_jobs j
            LEFT JOIN resume_job_matches m ON m.keyword_job_id = j.id
            GROUP BY j.keyword
        """).fetchall()]

        roster = [dict(r) for r in conn.execute("""
            SELECT p.candidate_name, p.base_resume_name, p.keywords,
                   count(m.id) AS matches
            FROM resume_profiles p
            LEFT JOIN resume_job_matches m ON m.resume_profile_id = p.id
            WHERE p.is_test_account = 0
            GROUP BY p.id ORDER BY matches ASC
        """).fetchall()]

    searched = {p["keyword"] for p in perf}
    vocab = defaultdict(set)
    for r in roster:
        for k in json.loads(r["keywords"] or "[]"):
            vocab[k].add(r["base_resume_name"])

    measured = sorted(
        [p for p in perf if p["matched"] > 0],
        key=lambda p: -(p["matched"] / max(p["jobs"], 1) * 100 + p["profiles"] * 12),
    )[:top_measured]

    waste = sorted(
        [p for p in perf if p["matched"] == 0 and p["jobs"] >= 50],
        key=lambda p: -p["jobs"],
    )[:40]

    # untried terms, weighted toward the profiles with the fewest matches
    underserved = {r["base_resume_name"] for r in roster[: max(3, len(roster) // 3)]}
    untried = sorted(
        (k for k in vocab if k not in searched),
        key=lambda k: (0 if vocab[k] & underserved else 1, -len(vocab[k])),
    )[:top_untried]

    return {"perf": perf, "roster": roster, "measured": measured,
            "waste": waste, "untried": untried, "vocab": set(vocab) | searched}


def choose_keywords(n: int = 500, explore_pct: int = 25) -> tuple[list[str], str]:
    ctx = _gather_context()

    roster_txt = "\n".join(
        f"  {'UNDERSERVED ' if r['matches'] < 20 else ''}{r['candidate_name']} / "
        f"{r['base_resume_name']}: {r['matches']} matches"
        for r in ctx["roster"]
    )
    measured_txt = "\n".join(
        f"  {p['keyword']}: {p['jobs']} jobs -> {p['matched']} matched, "
        f"{p['profiles']} profiles, {p['tops'] or 0} top"
        for p in ctx["measured"]
    )
    waste_txt = "\n".join(f"  {p['keyword']}: {p['jobs']} jobs, 0 matched" for p in ctx["waste"])
    untried_txt = ", ".join(ctx["untried"])

    prompt = USER_TEMPLATE.format(
        n=n, roster=roster_txt, measured=measured_txt, waste=waste_txt,
        untried=untried_txt, explore_pct=explore_pct,
    )

    data = None
    for model in (STRATEGIST_MODEL, STRATEGIST_FALLBACK_MODEL):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            data = _extract_json(resp.choices[0].message.content)
            break
        except Exception as e:
            log.warning(f"{model} failed ({e}); "
                        f"{'trying ' + STRATEGIST_FALLBACK_MODEL if model == STRATEGIST_MODEL else 'giving up'}")
    if data is None:
        # daily_cycle.s2_keywords() catches this and falls back to top-N by
        # measured ROI, so the night still runs -- just without a strategist.
        raise RuntimeError(f"both {STRATEGIST_MODEL} and {STRATEGIST_FALLBACK_MODEL} failed")

    # The model may not invent keywords — validate against known vocabulary.
    proposed = [k for k in data.get("keywords", []) if isinstance(k, str)]
    valid, invented = [], []
    seen = set()
    for k in proposed:
        key = k.strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        (valid if key in ctx["vocab"] else invented).append(key)

    if invented:
        log.warning(f"strategist proposed {len(invented)} unknown keywords, dropped: {invented[:5]}")

    # Top up from measured ROI if the model returned fewer than requested
    if len(valid) < n:
        for p in ctx["measured"] + [{"keyword": k} for k in ctx["untried"]]:
            if len(valid) >= n:
                break
            if p["keyword"].lower() not in seen:
                seen.add(p["keyword"].lower())
                valid.append(p["keyword"])

    return valid[:n], data.get("reasoning", "")
