"""
Send Jobs to Talentos — an alternative to Review & Assign.

Review & Assign pushes a candidate/job PAIR: it creates an application tied
to one candidate, queued straight into Talentos' AI resume-generation
pipeline. This tab is different on purpose. It only pushes the raw JOB
POSTING — no application, no candidate link — for anything our local
matcher already paired with at least one candidate. Talentos' own matching
pipeline then picks the candidate for that posting independently, instead
of trusting this app's local LLM matcher's opinion.

Nothing is written until Push is clicked, same convention as Review & Assign.
"""
from datetime import date, timedelta

import streamlit as st

from app.quality import MIN_DESCRIPTION

PRESETS = {
    "Today": 0,
    "Yesterday + today": 1,
    "Last 3 days": 2,
    "Last 7 days": 6,
}


def _date_controls() -> tuple[date, date]:
    st.markdown("##### Which ingestion day(s)?")
    st.caption("Filters on when THIS app pulled the job in, not when the employer posted it — "
               "so you can review and send one day's ingest at a time.")
    c1, c2 = st.columns([2, 3])
    with c1:
        preset = st.radio(
            "Quick pick", list(PRESETS) + ["Custom range"],
            index=1, horizontal=False, label_visibility="collapsed",
            key="sjt_preset",
        )
    with c2:
        today = date.today()
        if preset == "Custom range":
            picked = st.date_input(
                "Ingested between", value=(today - timedelta(days=1), today),
                max_value=today,
            )
            if isinstance(picked, tuple) and len(picked) == 2:
                d_from, d_to = picked
            else:
                d_from = d_to = picked if isinstance(picked, date) else today
        else:
            back = PRESETS[preset]
            d_from, d_to = today - timedelta(days=back), today
            st.info(
                f"**{d_from:%a %d %b}** → **{d_to:%a %d %b}**"
                + ("  ·  single day" if back == 0 else f"  ·  {back + 1} days merged")
            )
    return d_from, d_to


def render():
    st.subheader("Send Jobs to Talentos")
    st.caption(
        "Pushes raw job postings only — no application, no candidate link — for "
        "jobs our agents already matched to at least one candidate locally. "
        "Talentos' own matching pipeline then assigns candidates from there. "
        "Nothing is written until you click Push."
    )

    d_from, d_to = _date_controls()
    st.divider()

    min_score = st.slider("Minimum match score to include", 50, 100, 75)

    from scripts.push_to_talentos import load_matched_jobs_only, push_jobs_only

    jobs = load_matched_jobs_only(min_score=min_score, d_from=d_from, d_to=d_to)
    if not jobs:
        st.info(
            f"No matched jobs ingested between {d_from:%d %b} and {d_to:%d %b} at this "
            "score. Widen the range or lower the score."
        )
        return

    with st.spinner(f"Checking which of {len(jobs)} matched jobs are already in Talentos..."):
        stats, plan = push_jobs_only(jobs, commit=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Matched & workable", len(jobs),
              help=f"In resume_job_matches, {MIN_DESCRIPTION}+ char description, "
                   f"score >= {min_score}, ingested in this range.")
    c2.metric("Already in Talentos", stats.get("already_in_talentos", 0),
              help="Same four-pass dedupe push_to_talentos uses: external_job_id, "
                   "apply_url, source_url, then normalized company+title.")
    c3.metric("New — would push", stats.get("new", 0))

    if stats.get("new", 0) == 0:
        st.success("Everything at this score and range is already in Talentos.")
        return

    table = [{
        "Keep": True,
        "Score": j["best_score"],
        "Title": j["title"],
        "Company": j["company_name"],
        "Location": j["location"],
        "Candidates": j["candidate_count"],
        "Ingested": (j["scraped_at"] or "")[:10],
        "Posted": j["posted_date"],
        "Link": j["source_url"] or j["apply_url"] or j["job_url"],
        "_job": j["local_job_id"],
    } for j in plan]

    st.caption("Uncheck **Keep** to drop a row from this push.")
    edited = st.data_editor(
        table, use_container_width=True, hide_index=True, height=420,
        disabled=[c for c in table[0] if c != "Keep"],
        column_config={
            "Keep": st.column_config.CheckboxColumn("Keep", width="small"),
            "Candidates": st.column_config.NumberColumn(
                "Candidates", width="small",
                help="How many local candidates matched this job."),
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
            "_job": None,
        },
        key=f"sjt_editor_{d_from}_{d_to}_{min_score}",
    )

    kept_ids = {r["_job"] for r in edited if r.get("Keep")}
    chosen_plan = [j for j in plan if j["local_job_id"] in kept_ids]

    go = st.button(f"Push {len(chosen_plan)} jobs to Talentos", type="primary",
                   disabled=not chosen_plan)
    if go:
        with st.spinner(f"Pushing {len(chosen_plan)} jobs..."):
            try:
                commit_stats, _ = push_jobs_only(chosen_plan, commit=True)
                st.success(
                    f"Pushed {commit_stats.get('inserted', 0)} job postings to Talentos "
                    f"(0 applications created). Talentos' own pipeline picks candidates from here."
                )
                st.cache_data.clear()
            except Exception as e:
                st.error(f"Push failed — nothing was written. {e}")
