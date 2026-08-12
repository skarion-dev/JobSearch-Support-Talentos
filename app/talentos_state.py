"""
What Talentos already knows about each candidate — the single cross-check that
keeps every "sendable"/"ready" number honest.

WHY THIS EXISTS
---------------
push_to_talentos.py already refuses to create a duplicate application: it
looks up the job by four keys, then checks whether THIS candidate already has
an application against it, and a unique idempotency key backs that up at the
database level. That make the actual WRITE safe — nothing ever double-logs.

What was missing is upstream of that: every "sendable" / "ready to send" count
shown to an operator (Review & Assign overview, the nightly log, the Manual
Chase sheet) was computed from local matches alone, with no view of what
Talentos already holds. A job already logged — by this tool on an earlier run,
or by an AE manually, regardless — still counted as an opportunity. The push
itself would have silently skipped it, so no duplicate was ever created, but
the numbers shown along the way were inflated, and Manual Chase could tell
someone to go chase a job that was already handled.

This module is the one place that answers "does Talentos already have this,
for this candidate" and "does another candidate already have this job" — used
everywhere a count or a list is shown, not only at write time.

MATCHING, MIRRORED FROM THE PUSH'S OWN find_existing_job
----------------------------------------------------------
Four keys, strongest first: external_job_id, apply_url, source_url, then
normalized company+title (same regex push_to_talentos and review_tab use).
Scoped to the SOURCE side — a Talentos application row's own job — never the
local corpus, since Talentos is the authority on what has actually been sent.

EDGE CASES THIS COVERS
-----------------------
  * Logged by a human AE, not this tool — checked by candidate_id + job
    identity only, never filtered by application.source. A manually-applied
    job is exactly as much a duplicate as one this tool logged.
  * Any application status counts as "logged" — applied_at, review state and
    workflow status are irrelevant here; the push's own dup check works the
    same way (existence of the row is what matters, not its stage).
  * Unlogged jobs become available again automatically — this queries live
    Talentos state on every call rather than a static "ever pushed" table, so
    scripts/unlog_from_talentos.py freeing a row is immediately visible here
    with no special-case code.
  * Same job, two of a candidate's own resumes — handled separately in
    push_to_talentos.load_matches and mirrored in review_tab; not this
    module's concern.
  * Same job already logged for a DIFFERENT active candidate — not blocked
    (a large employer can legitimately have two openings that look
    identical), but surfaced via logged_by_others() so an operator can
    catch an accidental double-allocation before assigning.

RESIDUAL RISK
-------------
Company+title normalization strips punctuation but does not resolve synonyms
("Sr." vs "Senior", abbreviations). Two postings for the same real opening
with meaningfully different title text on different boards can still slip
past the title key. external_job_id/apply_url/source_url matches catch same
job posted identically; only a genuinely reworded duplicate can evade all
four keys. Not solved here — would need fuzzy/embedding matching, which is a
bigger and riskier change than this fix warrants.
"""
import re
from collections import defaultdict

import psycopg

from app.config import NEON_DB_URL


def _norm(s: str | None) -> str:
    """Same normalisation push_to_talentos and review_tab use."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fetch_logged_state(candidate_ids: list[str] | None = None) -> dict:
    """
    Every application Talentos holds for the given candidates (any source, any
    stage), indexed for O(1) lookup by each of the four identity keys.

    candidate_ids=None fetches Talentos-wide, which is rarely what you want —
    always scope to the active roster.
    """
    sql = """
        SELECT a.candidate_id::text, j.external_job_id, j.apply_url, j.source_url,
               j.company, j.title
        FROM applications a JOIN jobs j ON j.id = a.job_id
    """
    params: tuple = ()
    if candidate_ids:
        sql += " WHERE a.candidate_id = ANY(%s::uuid[])"
        params = (list(candidate_ids),)

    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    external, url, title = set(), set(), set()
    title_global: dict[tuple, set] = defaultdict(set)
    for cid, ext, apply_url, source_url, company, job_title in rows:
        if ext:
            external.add((cid, ext))
        if apply_url:
            url.add((cid, apply_url))
        if source_url:
            url.add((cid, source_url))
        key = (_norm(company), _norm(job_title))
        title.add((cid, *key))
        title_global[key].add(cid)

    return {"external": external, "url": url, "title": title,
            "title_global": dict(title_global)}


def is_logged(candidate_id, state: dict, *, external_job_id: str | None = None,
              apply_url: str | None = None, source_url: str | None = None,
              job_url: str | None = None, company: str | None = None,
              title: str | None = None) -> bool:
    """True if Talentos already has an application for this candidate against
    a job matching any of the four identity keys."""
    cid = str(candidate_id)
    if external_job_id and (cid, external_job_id) in state["external"]:
        return True
    for url in (apply_url, source_url, job_url):
        if url and (cid, url) in state["url"]:
            return True
    key = (cid, _norm(company), _norm(title))
    return key in state["title"]


def logged_by_others(state: dict, *, company: str | None, title: str | None,
                     exclude_candidate=None) -> set[str]:
    """Other candidate_ids (from the same fetched roster) already logged
    against a job with this normalized company+title. Advisory only — does
    not block, since a real employer can have more than one identical-sounding
    opening; the review UI surfaces it as a heads-up before Assign."""
    cands = set(state["title_global"].get((_norm(company), _norm(title)), set()))
    if exclude_candidate:
        cands.discard(str(exclude_candidate))
    return cands
