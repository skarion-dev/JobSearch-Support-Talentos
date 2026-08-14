"""
Operations dashboard — what is still waiting for review, and what is going wrong.

Every number is computed in SQL. The analyst agent only narrates them, so the
charts and the summary can never disagree.

OUTCOME TRACKING
----------------
The dashboard used to stop at "how many did we push". That is how 367 of 431
applications sat at an ATS score of 0 without anything on screen going red: the
pipeline was draining, the queue was empty, every stage looked healthy, and the
resumes were blank. Volume told us nothing about quality.

So the results section reads the scores back out of Talentos. An empty resume or
a run of zeros is now a red banner, not something you find by looking at
Talentos by hand a week later.
"""
from datetime import date, timedelta

import altair as alt
import pandas as pd
import psycopg
import streamlit as st

from app import db
from app.config import NEON_DB_URL
from app.experience import TITLE_TOLERANCE_YEARS, title_implied_years
from app.quality import MIN_DESCRIPTION
from app.talentos_state import fetch_logged_state, is_logged

# Talentos' own applications average 7.49. Below this we are underperforming
# the manual pipeline we are supposed to be scaling.
BASELINE_ATS = 7.49

SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
STATUS_BANNER = {
    "critical": ("🔴 Critical", "error"),
    "attention": ("🟠 Needs attention", "warning"),
    "healthy": ("🟢 Healthy", "success"),
}


@st.cache_data(ttl=120)
def logged_state() -> dict:
    """Live Talentos state for the active roster — see app/talentos_state.py.
    Backlog and every downstream metric are computed net of this, so a job
    already logged (any source, any stage) never inflates the numbers."""
    return fetch_logged_state(db.active_candidate_ids())


@st.cache_data(ttl=120)
def talentos_pipeline() -> list[dict]:
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT a.ae_stage, coalesce(w.status,'no workflow') wf, count(*) n
            FROM applications a
            LEFT JOIN application_ai_workflows w ON w.application_id = a.id
            WHERE a.source = 'jobsearch_support'
            GROUP BY 1,2 ORDER BY 3 DESC
        """)
        return [{"stage": r[0], "workflow": r[1], "n": r[2]} for r in cur.fetchall()]


@st.cache_data(ttl=120)
def pipeline_throughput() -> dict:
    """
    Queue depth alone cannot distinguish 'stalled' from 'draining normally' from
    'nothing to do'. Two false alarms came out of getting this wrong:

      * a deep queue was called critical while it cleared 34 workflows per
        15 minutes — that is draining, not stalled;
      * an EMPTY queue was called critical because nothing had completed
        recently — there was nothing left to complete.

    A stall requires work that is not moving. Both conditions, decided here in
    code rather than left to the model to infer.
    """
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE w.completed_at > now() - interval '15 minutes'),
              count(*) FILTER (WHERE w.completed_at > now() - interval '1 hour'),
              max(w.completed_at),
              count(*) FILTER (WHERE w.status IN ('queued','running'))
            FROM application_ai_workflows w
            JOIN applications a ON a.id = w.application_id
            WHERE a.source = 'jobsearch_support'
        """)
        last15, last60, latest, waiting = cur.fetchone()

    waiting = waiting or 0
    draining = (last15 or 0) > 0
    if waiting == 0:
        status = "idle"          # nothing queued: healthy, not stalled
    elif draining:
        status = "draining"
    else:
        status = "stalled"

    return {
        "completed_last_15min": last15 or 0,
        "completed_last_hour": last60 or 0,
        "last_completion_at": latest,
        "waiting": waiting,
        "is_draining": draining,
        "is_stalled": status == "stalled",
        "status": status,
        "status_explanation": {
            "idle": "Nothing queued — the pipeline has no work. This is healthy, "
                    "NOT a stall. Do not report it as an issue.",
            "draining": "Work is queued and completing normally. NOT a stall.",
            "stalled": "Work is queued and nothing has completed recently. This "
                       "is a Talentos dispatcher problem, not a sourcing problem.",
        }[status],
    }


@st.cache_data(ttl=120)
def talentos_outcomes() -> list[dict]:
    """
    What the applications we pushed actually scored. The number that matters.

    empty_resumes counts tailored resumes with no experience section — the
    signature of a workflow queued without a config_snapshot, which produces a
    header-and-skills skeleton and an ATS score of 0.
    """
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT c.name,
                   count(*) n,
                   round(avg(v.ats_score)::numeric, 2) avg_ats,
                   min(v.ats_score) min_ats,
                   max(v.ats_score) max_ats,
                   count(*) FILTER (WHERE v.ats_score = 0) zeros,
                   count(*) FILTER (WHERE w.status = 'failed') failed,
                   count(*) FILTER (WHERE w.status IN ('queued','running')) pending,
                   count(*) FILTER (
                       WHERE jsonb_array_length(
                           coalesce(v.content->'experience', '[]'::jsonb)) = 0
                   ) empty_resumes
            FROM applications a
            JOIN candidates c ON c.id = a.candidate_id
            JOIN application_ai_workflows w ON w.application_id = a.id
            LEFT JOIN application_resume_versions v ON v.id = a.tailored_resume_version_id
            WHERE a.source = 'jobsearch_support'
            GROUP BY c.name
            ORDER BY avg_ats DESC NULLS LAST
        """)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def source_quality() -> pd.DataFrame:
    """
    Usable rate per source. Adzuna is 92% of the corpus and 9% of what can be
    pushed; this chart is why the nightly cycle now leads with Apify.
    """
    with db.get_conn() as conn:
        return pd.DataFrame([dict(r) for r in conn.execute(f"""
            SELECT coalesce(source,'could not determine') source, count(*) jobs,
                   sum(CASE WHEN length(coalesce(description,'')) >= {MIN_DESCRIPTION}
                            THEN 1 ELSE 0 END) usable,
                   cast(avg(length(coalesce(description,''))) AS INT) avg_chars
            FROM keyword_jobs GROUP BY source ORDER BY jobs DESC
        """).fetchall()])


def seniority_quality() -> dict:
    """
    Gate-violation rate among current TOP_MATCH rows: candidate has computed
    years_experience, the job implies a real requirement (explicit "X years"
    or a Senior/Lead/Principal/Staff/Director/Manager title), and the gap
    exceeds TOLERANCE_YEARS. Measured before this fix shipped: 81 of 611
    TOP_MATCH rows (13%) had a senior title on a candidate with 1-2 years —
    example, "Principal Network Engineer" scored 98 with the model's own
    reasoning calling it "a perfect match for an experienced..." candidate.

    prefilter() now blocks these before the LLM ever scores them, so this
    should sit near zero for new matches. A rise here is a real regression,
    not something to notice by hand three weeks later.
    """
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.years_experience,
                   coalesce(p.years_experience_raw, p.years_experience) years_raw,
                   j.title, m.score, m.band
            FROM resume_job_matches m
            JOIN resume_profiles p ON p.id = m.resume_profile_id
            JOIN keyword_jobs   j ON j.id = m.keyword_job_id
            WHERE p.is_test_account = 0 AND m.band = 'TOP_MATCH'
              AND p.years_experience IS NOT NULL
        """).fetchall()]

    # Mirrors passes_experience_gate: a Senior/Lead TITLE is measured against
    # RAW years at the tighter title tolerance, not against education-boosted
    # years at the loose stated-number tolerance.
    violations = []
    for r in rows:
        implied = title_implied_years(r["title"])
        if implied is not None and (implied - r["years_raw"]) > TITLE_TOLERANCE_YEARS:
            violations.append(r)

    return {
        "total_checked": len(rows),
        "violations": len(violations),
        "rate_pct": round(100 * len(violations) / len(rows), 1) if rows else 0.0,
        "sample": violations[:10],
    }


def backlog() -> pd.DataFrame:
    """Matches never logged in Talentos, with age. 'Never logged' is checked
    against live Talentos state (any source), not just this tool's own pushes."""
    state = logged_state()
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.candidate_name, p.candidate_id, p.base_resume_name,
                   m.score, m.band, j.title, j.company_name, j.posted_date, j.source,
                   j.source_url, j.job_url, j.apply_url, j.external_job_id
            FROM resume_job_matches m
            JOIN resume_profiles p ON p.id = m.resume_profile_id
            JOIN keyword_jobs   j ON j.id = m.keyword_job_id
            WHERE p.is_test_account = 0
        """).fetchall()]

    today = date.today()
    out = []
    for r in rows:
        if is_logged(r["candidate_id"], state, external_job_id=r["external_job_id"],
                    apply_url=r["apply_url"], source_url=r["source_url"],
                    job_url=r["job_url"], company=r["company_name"], title=r["title"]):
            continue
        try:
            age = (today - date.fromisoformat(str(r["posted_date"])[:10])).days
        except (TypeError, ValueError):
            age = None
        out.append({**r, "age_days": age})
    return pd.DataFrame(out)


def build_metrics(df: pd.DataFrame) -> dict:
    pipe = talentos_pipeline()
    flow = pipeline_throughput()
    with db.get_conn() as conn:
        coverage = [dict(r) for r in conn.execute("""
            SELECT p.candidate_name, p.base_resume_name, count(m.id) matches
            FROM resume_profiles p
            LEFT JOIN resume_job_matches m ON m.resume_profile_id = p.id
            WHERE p.is_test_account = 0
            GROUP BY p.id ORDER BY matches ASC
        """).fetchall()]
        corpus = conn.execute("SELECT count(*) FROM keyword_jobs").fetchone()[0]

    by_cand = (
        df.groupby("candidate_name")
          .agg(unreviewed=("score", "size"),
               top=("band", lambda s: int((s == "TOP_MATCH").sum())),
               oldest_days=("age_days", "max"))
          .reset_index().sort_values("unreviewed", ascending=False)
        if not df.empty else pd.DataFrame(columns=["candidate_name", "unreviewed", "top", "oldest_days"])
    )

    outcomes = talentos_outcomes()
    scored = [o for o in outcomes if o["avg_ats"] is not None]
    pushed_total = sum(o["n"] for o in outcomes)
    overall_ats = (
        round(sum(float(o["avg_ats"]) * o["n"] for o in scored) / sum(o["n"] for o in scored), 2)
        if scored else None
    )

    sq = source_quality()
    usable_total = int(sq["usable"].sum()) if not sq.empty else 0
    jobs_total = int(sq["jobs"].sum()) if not sq.empty else 0

    seniority = seniority_quality()

    return {
        "total_unreviewed": int(len(df)),
        "candidates_with_backlog": int(by_cand.shape[0]),
        "backlog_by_candidate": by_cand.to_dict("records"),
        "stale_over_7_days": int((df["age_days"] > 7).sum()) if not df.empty else 0,
        "underserved_profiles": [c for c in coverage if c["matches"] < 20],
        "talentos_pipeline": pipe,
        "queued_not_processed": sum(p["n"] for p in pipe if p["workflow"] == "queued"),
        "completed": sum(p["n"] for p in pipe if p["workflow"] == "completed"),
        "pipeline_throughput": flow,
        "pipeline_status": flow["status"],
        "pipeline_status_explanation": flow["status_explanation"],
        "pipeline_is_draining": flow["is_draining"],
        "pipeline_is_stalled": flow["is_stalled"],
        "corpus_size": corpus,
        # ---- quality, not just volume ----
        "outcomes_by_candidate": outcomes,
        "applications_pushed": pushed_total,
        "overall_avg_ats": overall_ats,
        "baseline_avg_ats": BASELINE_ATS,
        "total_zero_scores": sum(o["zeros"] or 0 for o in outcomes),
        "total_empty_resumes": sum(o["empty_resumes"] or 0 for o in outcomes),
        "total_failed_workflows": sum(o["failed"] or 0 for o in outcomes),
        "source_quality": sq.to_dict("records") if not sq.empty else [],
        "corpus_usable": usable_total,
        "corpus_usable_pct": round(100 * usable_total / jobs_total, 1) if jobs_total else 0,
        "seniority_mismatch_rate_pct": seniority["rate_pct"],
        "seniority_mismatch_count": seniority["violations"],
        "seniority_checked_count": seniority["total_checked"],
    }


def render():
    st.subheader("Operations dashboard")
    st.caption("Every figure is computed from the database; the summary only narrates it.")

    df = backlog()
    metrics = build_metrics(df)

    # ---- headline metrics ----
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Awaiting review", metrics["total_unreviewed"])
    k2.metric("Candidates affected", metrics["candidates_with_backlog"])
    k3.metric("Stale (>7 days)", metrics["stale_over_7_days"],
              delta="needs action" if metrics["stale_over_7_days"] else None,
              delta_color="inverse")
    k4.metric("Queued in AI pipeline", metrics["queued_not_processed"])
    ats = metrics["overall_avg_ats"]
    k5.metric(
        "Avg ATS achieved", ats if ats is not None else "—",
        delta=(f"{ats - BASELINE_ATS:+.2f} vs Talentos" if ats is not None else None),
        help=f"Talentos' own manual pipeline averages {BASELINE_ATS}.",
    )

    # Quality alarms come before anything else — a healthy-looking queue full of
    # blank resumes is the exact failure this dashboard missed once already.
    if metrics["total_empty_resumes"]:
        st.error(
            f"**{metrics['total_empty_resumes']} tailored resumes have no experience "
            "section.** That means workflows were queued without a config_snapshot, "
            "so the generator had nothing to tailor. Re-push those applications "
            "rather than repairing them in place — repairs score 0–1, fresh pushes "
            "score 8–9."
        )
    elif metrics["total_zero_scores"]:
        st.warning(
            f"{metrics['total_zero_scores']} applications scored 0 on ATS. "
            "Check the resume versions before the AE reviews them."
        )

    # ---- analyst summary ----
    st.divider()
    left, right = st.columns([4, 1])
    with left:
        st.markdown("##### Leadership summary")
    with right:
        refresh = st.button("Refresh", use_container_width=True)

    if refresh or "analyst" not in st.session_state:
        from app.agents.analyst_agent import summarize
        with st.spinner("Analysing…"):
            st.session_state.analyst = summarize(metrics)
    a = st.session_state.analyst

    label, kind = STATUS_BANNER.get(a.get("status", "attention"), STATUS_BANNER["attention"])
    getattr(st, kind)(f"**{label} — {a.get('headline','')}**")

    for issue in a.get("issues", []):
        icon = SEVERITY_ICON.get(issue.get("severity", "low"), "🟡")
        with st.expander(f"{icon}  {issue.get('title','Issue')}", expanded=issue.get("severity") == "high"):
            st.write(issue.get("detail", ""))
            if issue.get("action"):
                st.caption(f"**Next step:** {issue['action']}")
    for w in a.get("wins", []):
        st.caption(f"✅ {w}")

    st.divider()

    # ---- charts ----
    if df.empty:
        st.success("Nothing awaiting review — every match has been actioned.")
    else:
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("**Backlog by candidate**")
            b = pd.DataFrame(metrics["backlog_by_candidate"])
            st.altair_chart(
                alt.Chart(b).mark_bar().encode(
                    x=alt.X("unreviewed:Q", title="Awaiting review"),
                    y=alt.Y("candidate_name:N", sort="-x", title=None),
                    color=alt.Color("top:Q", title="TOP", scale=alt.Scale(scheme="blues")),
                    tooltip=["candidate_name", "unreviewed", "top", "oldest_days"],
                ).properties(height=260),
                use_container_width=True,
            )

        with c2:
            st.markdown("**Age of unreviewed matches**")
            aged = df.dropna(subset=["age_days"]).copy()
            if aged.empty:
                st.caption("No dated matches.")
            else:
                aged["bucket"] = pd.cut(
                    aged["age_days"], [-1, 1, 3, 7, 14, 999],
                    labels=["≤1d", "2-3d", "4-7d", "8-14d", ">14d"],
                )
                counts = aged.groupby("bucket", observed=False).size().reset_index(name="n")
                st.altair_chart(
                    alt.Chart(counts).mark_bar().encode(
                        x=alt.X("bucket:N", title="Age", sort=None),
                        y=alt.Y("n:Q", title="Matches"),
                        color=alt.Color("bucket:N", legend=None,
                                        scale=alt.Scale(scheme="orangered")),
                        tooltip=["bucket", "n"],
                    ).properties(height=260),
                    use_container_width=True,
                )

        c3, c4 = st.columns(2)
        with c3:
            st.markdown("**New matches per day**")
            per_day = (df.dropna(subset=["posted_date"])
                         .groupby("posted_date").size().reset_index(name="n")
                         .sort_values("posted_date").tail(14))
            st.altair_chart(
                alt.Chart(per_day).mark_line(point=True).encode(
                    x=alt.X("posted_date:N", title="Posted"),
                    y=alt.Y("n:Q", title="Matches"),
                    tooltip=["posted_date", "n"],
                ).properties(height=240),
                use_container_width=True,
            )
        with c4:
            st.markdown("**Where matches come from**")
            src = df.groupby("source").size().reset_index(name="n")
            st.altair_chart(
                alt.Chart(src).mark_arc(innerRadius=55).encode(
                    theta="n:Q", color=alt.Color("source:N", title="Source"),
                    tooltip=["source", "n"],
                ).properties(height=240),
                use_container_width=True,
            )

    # ---- Talentos pipeline state ----
    st.divider()
    st.markdown("##### Talentos pipeline (applications this tool created)")
    pipe = pd.DataFrame(metrics["talentos_pipeline"])
    if pipe.empty:
        st.caption("Nothing pushed yet.")
    else:
        p1, p2 = st.columns([2, 3])
        with p1:
            st.dataframe(pipe, use_container_width=True, hide_index=True)
        with p2:
            st.altair_chart(
                alt.Chart(pipe).mark_bar().encode(
                    x=alt.X("n:Q", title="Applications"),
                    y=alt.Y("stage:N", sort="-x", title=None),
                    color=alt.Color("workflow:N", title="Workflow"),
                    tooltip=["stage", "workflow", "n"],
                ).properties(height=200),
                use_container_width=True,
            )
        flow = metrics["pipeline_throughput"]
        if flow["status"] == "stalled":
            st.error(
                f"{flow['waiting']} queued and nothing completed in the last 15 "
                "minutes — the Talentos dispatcher may be down. That is a "
                "Talentos issue, not this pipeline."
            )
        elif flow["status"] == "draining":
            st.success(
                f"Pipeline is draining normally — {flow['completed_last_15min']} completed "
                f"in the last 15 min ({flow['completed_last_hour']} in the last hour)."
            )
        else:
            st.info("Nothing queued — every application this tool sent has finished processing.")

    # ---- results: what the pushed applications actually scored ----
    st.divider()
    st.markdown("##### Results in Talentos")
    st.caption(
        "Quality of what we sent, not just how much. Read back from Talentos' "
        "own resume versions."
    )
    outcomes = metrics["outcomes_by_candidate"]
    if not outcomes:
        st.caption("Nothing pushed yet.")
    else:
        o = pd.DataFrame(outcomes)
        o["avg_ats"] = pd.to_numeric(o["avg_ats"], errors="coerce")
        r1, r2 = st.columns([3, 2])
        with r1:
            st.dataframe(
                o.rename(columns={
                    "name": "Candidate", "n": "Sent", "avg_ats": "Avg ATS",
                    "min_ats": "Min", "max_ats": "Max", "zeros": "Zeros",
                    "failed": "Failed", "pending": "Pending",
                    "empty_resumes": "Empty",
                }),
                use_container_width=True, hide_index=True,
                column_config={
                    "Avg ATS": st.column_config.ProgressColumn(
                        "Avg ATS", min_value=0, max_value=10, format="%.2f"),
                    "Empty": st.column_config.NumberColumn(
                        "Empty", help="Resumes with no experience section — always a bug."),
                },
            )
        with r2:
            chart = o.dropna(subset=["avg_ats"])
            if chart.empty:
                st.caption("No scores yet — workflows still running.")
            else:
                st.altair_chart(
                    alt.Chart(chart).mark_bar().encode(
                        x=alt.X("avg_ats:Q", title="Avg ATS", scale=alt.Scale(domain=[0, 10])),
                        y=alt.Y("name:N", sort="-x", title=None),
                        color=alt.condition(
                            alt.datum.avg_ats >= BASELINE_ATS,
                            alt.value("#2e7d32"), alt.value("#c62828")),
                        tooltip=["name", "n", "avg_ats", "zeros", "failed"],
                    ).properties(height=240)
                    + alt.Chart(pd.DataFrame({"x": [BASELINE_ATS]})).mark_rule(
                        strokeDash=[4, 4], color="grey").encode(x="x:Q"),
                    use_container_width=True,
                )
                st.caption(f"Dashed line: Talentos' own average ({BASELINE_ATS}).")

    # ---- sourcing quality: why half the matches cannot be automated ----
    st.divider()
    st.markdown("##### Sourcing quality")
    sq = pd.DataFrame(metrics["source_quality"])
    if sq.empty:
        st.caption("No jobs ingested yet.")
    else:
        sq["usable_pct"] = (100 * sq["usable"] / sq["jobs"]).round(1)
        q1, q2 = st.columns([2, 3])
        with q1:
            st.metric("Corpus usable", f"{metrics['corpus_usable_pct']}%",
                      help=f"Jobs with a description of {MIN_DESCRIPTION}+ chars — "
                           "the rest cannot be tailored and go to Manual Chase.")
            st.dataframe(
                sq.rename(columns={"source": "Source", "jobs": "Jobs",
                                   "usable": "Usable", "usable_pct": "%",
                                   "avg_chars": "Avg chars"}),
                use_container_width=True, hide_index=True,
            )
        with q2:
            st.altair_chart(
                alt.Chart(sq).mark_bar().encode(
                    x=alt.X("usable_pct:Q", title="% usable", scale=alt.Scale(domain=[0, 100])),
                    y=alt.Y("source:N", sort="-x", title=None),
                    color=alt.Color("avg_chars:Q", title="Avg chars",
                                    scale=alt.Scale(scheme="greens")),
                    tooltip=["source", "jobs", "usable", "usable_pct", "avg_chars"],
                ).properties(height=200),
                use_container_width=True,
            )
            st.caption(
                "Adzuna truncates at ~500 characters and gates its own links, so "
                "almost none of its volume can be pushed. The nightly cycle leads "
                "with Apify for this reason."
            )

    # ---- seniority match quality ----
    st.divider()
    st.markdown("##### Seniority match quality")
    checked = metrics["seniority_checked_count"]
    if checked == 0:
        st.caption("No candidates with computed years_experience yet — run sync_resume_profiles.")
    else:
        rate = metrics["seniority_mismatch_rate_pct"]
        s1, s2 = st.columns([1, 3])
        with s1:
            st.metric(
                "Senior-role mismatches in TOP_MATCH", f"{rate}%",
                delta=None if rate == 0 else f"{metrics['seniority_mismatch_count']} of {checked}",
                delta_color="inverse",
                help="Candidate's computed years fall meaningfully short of what the "
                     "posting implies (explicit years figure, or a Senior/Lead/"
                     "Principal/Staff/Director/Manager title), beyond the tolerance "
                     "band. Measured at 13% before this gate existed.",
            )
        with s2:
            if rate == 0:
                st.success("No gate violations in current TOP_MATCH rows.")
            else:
                st.warning(
                    f"{metrics['seniority_mismatch_count']} TOP_MATCH rows still look "
                    "mismatched on seniority — likely pre-fix matches still in the "
                    "table. Re-run scripts.match_resumes_to_jobs to refresh them."
                )

    # ---- underserved ----
    if metrics["underserved_profiles"]:
        st.divider()
        st.markdown("##### Underserved base resumes (<20 matches)")
        st.caption("These profiles need better keyword coverage — nothing to review is worse than a backlog.")
        st.dataframe(pd.DataFrame(metrics["underserved_profiles"]),
                     use_container_width=True, hide_index=True)
