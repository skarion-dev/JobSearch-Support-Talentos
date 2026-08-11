"""
Operations dashboard — what is still waiting for review, and what is going wrong.

Every number is computed in SQL. The analyst agent only narrates them, so the
charts and the summary can never disagree.
"""
from datetime import date, timedelta

import altair as alt
import pandas as pd
import psycopg
import streamlit as st

from app import db
from app.config import NEON_DB_URL

SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
STATUS_BANNER = {
    "critical": ("🔴 Critical", "error"),
    "attention": ("🟠 Needs attention", "warning"),
    "healthy": ("🟢 Healthy", "success"),
}


@st.cache_data(ttl=120)
def pushed_pairs() -> set[str]:
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT a.candidate_id::text || '|' ||
                   lower(coalesce(j.company,'')) || '|' || lower(coalesce(j.title,''))
            FROM applications a JOIN jobs j ON j.id = a.job_id
        """)
        return {r[0] for r in cur.fetchall()}


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
    Queue depth alone cannot distinguish 'stalled' from 'draining normally'.
    Without this the analyst called a healthy pipeline critical while it was
    clearing 34 workflows every 15 minutes.
    """
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE w.completed_at > now() - interval '15 minutes'),
              count(*) FILTER (WHERE w.completed_at > now() - interval '1 hour'),
              max(w.completed_at)
            FROM application_ai_workflows w
            JOIN applications a ON a.id = w.application_id
            WHERE a.source = 'jobsearch_support'
        """)
        last15, last60, latest = cur.fetchone()
    return {
        "completed_last_15min": last15 or 0,
        "completed_last_hour": last60 or 0,
        "last_completion_at": latest,
        "is_draining": (last15 or 0) > 0,
    }


def backlog() -> pd.DataFrame:
    """Matches never pushed to Talentos, with age."""
    seen = pushed_pairs()
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.candidate_name, p.candidate_id, p.base_resume_name,
                   m.score, m.band, j.title, j.company_name, j.posted_date, j.source
            FROM resume_job_matches m
            JOIN resume_profiles p ON p.id = m.resume_profile_id
            JOIN keyword_jobs   j ON j.id = m.keyword_job_id
            WHERE p.is_test_account = 0
        """).fetchall()]

    today = date.today()
    out = []
    for r in rows:
        key = f"{r['candidate_id']}|{(r['company_name'] or '').lower()}|{(r['title'] or '').lower()}"
        if key in seen:
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
        "pipeline_is_draining": flow["is_draining"],
        "corpus_size": corpus,
    }


def render():
    st.subheader("Operations dashboard")
    st.caption("Every figure is computed from the database; the summary only narrates it.")

    df = backlog()
    metrics = build_metrics(df)

    # ---- headline metrics ----
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Awaiting review", metrics["total_unreviewed"])
    k2.metric("Candidates affected", metrics["candidates_with_backlog"])
    k3.metric("Stale (>7 days)", metrics["stale_over_7_days"],
              delta="needs action" if metrics["stale_over_7_days"] else None,
              delta_color="inverse")
    k4.metric("Queued in AI pipeline", metrics["queued_not_processed"])

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
        if metrics["queued_not_processed"] > 0 and not flow["is_draining"]:
            st.error(
                f"{metrics['queued_not_processed']} queued and nothing completed in the "
                "last 15 minutes — the Talentos dispatcher may be down. "
                "That is a Talentos issue, not this pipeline."
            )
        elif flow["is_draining"]:
            st.success(
                f"Pipeline is draining normally — {flow['completed_last_15min']} completed "
                f"in the last 15 min ({flow['completed_last_hour']} in the last hour)."
            )

    # ---- underserved ----
    if metrics["underserved_profiles"]:
        st.divider()
        st.markdown("##### Underserved base resumes (<20 matches)")
        st.caption("These profiles need better keyword coverage — nothing to review is worse than a backlog.")
        st.dataframe(pd.DataFrame(metrics["underserved_profiles"]),
                     use_container_width=True, hide_index=True)
