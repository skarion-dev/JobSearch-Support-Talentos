"""Rules and daily TalentOS handoff reporting for the jobs support app."""

from datetime import date, timedelta
import json

import altair as alt
import pandas as pd
import psycopg
import streamlit as st

from app import db
from app.config import NEON_DB_URL


def _json_items(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [str(value).strip()] if str(value).strip() else []
    if isinstance(parsed, list):
        return [str(x).strip() for x in parsed if str(x).strip()]
    return [str(parsed).strip()] if str(parsed).strip() else []


def _load_profiles():
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT candidate_name, base_resume_name, target_roles,
                   location_preference, open_to_relocation, work_authorization,
                   visa_status, keywords, additional_rules, review_status,
                   is_match_ready, synced_at
            FROM resume_profiles
            WHERE is_test_account = 0
            ORDER BY lower(candidate_name), lower(base_resume_name)
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _load_local_daily(days):
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    with db.get_conn() as conn:
        jobs = pd.DataFrame(
            [dict(row) for row in conn.execute(
                """
                SELECT date(scraped_at) AS day, count(*) AS jobs_ingested
                FROM keyword_jobs
                WHERE date(scraped_at) >= date(?)
                GROUP BY date(scraped_at)
                ORDER BY day
                """, (cutoff,)
            ).fetchall()]
        )
        matches = pd.DataFrame(
            [dict(row) for row in conn.execute(
                """
                SELECT date(matched_at) AS day,
                       count(*) AS matches,
                       count(DISTINCT keyword_job_id) AS matched_jobs
                FROM resume_job_matches
                WHERE date(matched_at) >= date(?)
                GROUP BY date(matched_at)
                ORDER BY day
                """, (cutoff,)
             ).fetchall()]
        )
    return jobs, matches


def _load_talentos(days):
    if not NEON_DB_URL:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), "NEON_DB_URL is not configured."
    try:
        with psycopg.connect(NEON_DB_URL, connect_timeout=15) as conn:
            daily = pd.read_sql_query(
                """
                SELECT (a.created_at AT TIME ZONE 'America/New_York')::date AS day,
                       count(DISTINCT a.id) AS sent,
                       count(DISTINCT a.id) FILTER (
                           WHERE a.application_stage = 'in_ai_pipeline') AS in_ai_pipeline,
                       count(DISTINCT a.id) FILTER (
                           WHERE a.application_stage = 'ready_for_review') AS ready_for_review,
                       count(DISTINCT a.id) FILTER (
                           WHERE a.application_stage = 'ae_applied') AS ae_applied,
                       count(DISTINCT w.id) FILTER (WHERE w.status = 'queued') AS workflows_queued,
                       count(DISTINCT w.id) FILTER (WHERE w.status = 'completed') AS workflows_completed,
                       count(DISTINCT w.id) FILTER (WHERE w.status = 'failed') AS workflows_failed
                FROM applications a
                LEFT JOIN application_ai_workflows w ON w.application_id = a.id
                WHERE a.source = 'jobsearch_support'
                  AND a.created_at >= now() - (%s * interval '1 day')
                GROUP BY 1
                ORDER BY 1
                """, conn, params=(days,)
            )
            by_candidate = pd.read_sql_query(
                """
                SELECT c.name AS candidate,
                       count(DISTINCT a.id) AS sent,
                       count(DISTINCT a.id) FILTER (
                           WHERE a.application_stage = 'in_ai_pipeline') AS in_ai_pipeline,
                       count(DISTINCT a.id) FILTER (
                           WHERE a.application_stage = 'ready_for_review') AS ready_for_review,
                       count(DISTINCT a.id) FILTER (
                           WHERE a.application_stage = 'ae_applied') AS ae_applied,
                       count(DISTINCT w.id) FILTER (WHERE w.status = 'failed') AS failed
                FROM applications a
                JOIN candidates c ON c.id = a.candidate_id
                LEFT JOIN application_ai_workflows w ON w.application_id = a.id
                WHERE a.source = 'jobsearch_support'
                  AND a.created_at >= now() - (%s * interval '1 day')
                GROUP BY c.name
                ORDER BY sent DESC, c.name
                """, conn, params=(days,)
            )
            by_source = pd.read_sql_query(
                """
                SELECT coalesce(j.source, 'unknown') AS source,
                       count(DISTINCT a.id) AS applications
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                WHERE a.source = 'jobsearch_support'
                  AND a.created_at >= now() - (%s * interval '1 day')
                GROUP BY 1
                ORDER BY applications DESC
                """, conn, params=(days,)
            )
        return daily, by_candidate, by_source, None
    except Exception as exc:  # show the dashboard with local data if Neon is transient
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), str(exc)


def _metric_value(frame, column):
    return int(frame[column].fillna(0).sum()) if not frame.empty and column in frame else 0


def _rules_section(profiles):
    st.subheader("Custom rules by active base resume")
    st.caption(
        "These are the current synchronized search contracts. They are applied at the next "
        "7 PM Eastern run; edits belong in TalentOS Job Search Profiles and are picked up "
        "on the next sync."
    )
    if not profiles:
        st.info("No active base-resume profiles are synchronized yet.")
        return

    candidates = {}
    for profile in profiles:
        candidates.setdefault(profile["candidate_name"] or "Unnamed candidate", []).append(profile)
    st.caption(f"{len(profiles)} base resumes across {len(candidates)} active candidates")
    for candidate, candidate_profiles in candidates.items():
        with st.expander(f"{candidate} · {len(candidate_profiles)} base resume(s)", expanded=False):
            for profile in candidate_profiles:
                name = profile["base_resume_name"] or "Unnamed base resume"
                keywords = _json_items(profile["keywords"])
                roles = ", ".join(_json_items(profile["target_roles"])) or "Not specified"
                with st.container(border=True):
                    st.markdown(f"#### {name}")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Keywords", len(keywords))
                    c2.metric("Match ready", "Yes" if profile["is_match_ready"] else "No")
                    c3.metric("Review status", profile["review_status"] or "Unknown")
                    st.write(f"**Target roles:** {roles}")
                    st.write(
                        f"**Location:** {profile['location_preference'] or 'Not specified'} · "
                        f"**Relocation:** {'Yes' if profile['open_to_relocation'] else 'No'} · "
                        f"**Authorization:** {profile['work_authorization'] or 'Not specified'}"
                    )
                    if keywords:
                        st.write("**Search terms:** " + " · ".join(keywords))
                    else:
                        st.warning("No active keywords; this profile will not produce matches.")
                    st.markdown("**Additional rules**")
                    st.text_area(
                        f"Rules for {candidate} / {name}",
                        value=profile["additional_rules"] or "No custom rules configured.",
                        height=130,
                        disabled=True,
                        label_visibility="collapsed",
                    )
                    st.caption(f"Last synced: {profile['synced_at'] or 'unknown'}")


def _handoff_section(days):
    st.subheader("Daily handoff to TalentOS")
    st.caption(
        "TalentOS counts use applications created by the JobSearch Support autopilot, "
        "converted to Eastern Time. Local ingestion and matching counts are shown beside "
        "them so the daily funnel is visible."
    )
    local_jobs, local_matches = _load_local_daily(days)
    daily, by_candidate, by_source, error = _load_talentos(days)
    if error:
        st.warning(f"TalentOS report unavailable right now: {error}")

    total_jobs = _metric_value(local_jobs, "jobs_ingested")
    total_matches = _metric_value(local_matches, "matches")
    total_sent = _metric_value(daily, "sent")
    queued = _metric_value(daily, "workflows_queued")
    completed = _metric_value(daily, "workflows_completed")
    failed = _metric_value(daily, "workflows_failed")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Jobs ingested", total_jobs)
    c2.metric("Resume matches", total_matches)
    c3.metric("Sent to TalentOS", total_sent)
    c4.metric("AI queued", queued)
    c5.metric("AI completed", completed)
    c6.metric("AI failed", failed)

    if daily.empty:
        st.info("No autopilot handoffs in this period yet. The first scheduled run will appear here.")
        return

    daily["day"] = pd.to_datetime(daily["day"]).dt.strftime("%Y-%m-%d")
    chart = alt.Chart(daily).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("day:N", title="Eastern date", sort=None),
        y=alt.Y("sent:Q", title="Applications sent"),
        tooltip=["day", "sent", "in_ai_pipeline", "ready_for_review", "ae_applied"],
        color=alt.value("#6d5dfc"),
    ).properties(height=260)
    st.altair_chart(chart, use_container_width=True)

    left, right = st.columns(2)
    with left:
        st.markdown("**TalentOS handoff by candidate**")
        if not by_candidate.empty:
            st.altair_chart(
                alt.Chart(by_candidate).mark_bar().encode(
                    x=alt.X("sent:Q", title="Applications sent"),
                    y=alt.Y("candidate:N", sort="-x", title=None),
                    tooltip=list(by_candidate.columns),
                    color=alt.value("#4caf9b"),
                ).properties(height=max(220, 28 * len(by_candidate))),
                use_container_width=True,
            )
    with right:
        st.markdown("**What happened to the handoff**")
        status_cols = [c for c in ["in_ai_pipeline", "ready_for_review", "ae_applied"] if c in daily]
        if status_cols:
            status = daily[["day"] + status_cols].melt("day", var_name="stage", value_name="count")
            st.altair_chart(
                alt.Chart(status).mark_bar().encode(
                    x=alt.X("day:N", title="Eastern date", sort=None),
                    y=alt.Y("count:Q", title="Applications"),
                    color=alt.Color("stage:N", title="Application stage"),
                    tooltip=["day", "stage", "count"],
                ).properties(height=260),
                use_container_width=True,
            )

    st.markdown("**Daily handoff ledger**")
    display = daily.rename(columns={
        "day": "Eastern date", "sent": "Sent", "in_ai_pipeline": "AI pipeline",
        "ready_for_review": "Ready for AE review", "ae_applied": "AE applied",
        "workflows_queued": "AI queued", "workflows_completed": "AI completed",
        "workflows_failed": "AI failed",
    })
    st.dataframe(display, use_container_width=True, hide_index=True)
    if not by_source.empty:
        with st.expander("Source mix for TalentOS handoffs"):
            st.dataframe(by_source, use_container_width=True, hide_index=True)


def render():
    st.subheader("Rules & Handoffs")
    st.caption("Search contracts, daily autopilot output, and TalentOS pipeline visibility in one place.")
    if st.button("Refresh rules and live report"):
        st.cache_data.clear()
        st.rerun()
    days = st.selectbox("Report period", [7, 14, 30], format_func=lambda n: f"Last {n} days")
    _rules_section(_load_profiles())
    st.divider()
    _handoff_section(days)
