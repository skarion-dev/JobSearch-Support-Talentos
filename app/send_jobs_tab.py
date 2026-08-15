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
import streamlit as st

from app.quality import MIN_DESCRIPTION


def render():
    st.subheader("Send Jobs to Talentos")
    st.caption(
        "Pushes raw job postings only — no application, no candidate link — for "
        "jobs our agents already matched to at least one candidate locally. "
        "Talentos' own matching pipeline then assigns candidates from there. "
        "Nothing is written until you click Push."
    )

    from scripts.push_to_talentos import load_matched_jobs_only, push_jobs_only

    min_score = st.slider("Minimum match score to include", 50, 100, 75)

    jobs = load_matched_jobs_only(min_score=min_score)
    if not jobs:
        st.info("No matched jobs meet this score yet. Lower the score, or run "
                 "`python -m scripts.match_resumes_to_jobs`.")
        return

    with st.spinner(f"Checking which of {len(jobs)} matched jobs are already in Talentos..."):
        stats, plan = push_jobs_only(jobs, commit=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Matched & workable", len(jobs),
              help=f"In resume_job_matches, {MIN_DESCRIPTION}+ char description, "
                   f"score >= {min_score}.")
    c2.metric("Already in Talentos", stats.get("already_in_talentos", 0),
              help="Same four-pass dedupe push_to_talentos uses: external_job_id, "
                   "apply_url, source_url, then normalized company+title.")
    c3.metric("New — would push", stats.get("new", 0))

    if stats.get("new", 0) == 0:
        st.success("Everything at this score is already in Talentos.")
        return

    st.dataframe(
        [{"Score": j["best_score"], "Title": j["title"], "Company": j["company_name"],
          "Location": j["location"], "Candidates matched": j["candidate_count"],
          "Posted": j["posted_date"],
          "Link": j["source_url"] or j["apply_url"] or j["job_url"]}
         for j in plan],
        use_container_width=True, hide_index=True, height=420,
        column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")},
    )

    go = st.button(f"Push {stats['new']} jobs to Talentos", type="primary")
    if go:
        with st.spinner(f"Pushing {stats['new']} jobs..."):
            try:
                commit_stats, _ = push_jobs_only(plan, commit=True)
                st.success(
                    f"Pushed {commit_stats.get('inserted', 0)} job postings to Talentos "
                    f"(0 applications created). Talentos' own pipeline picks candidates from here."
                )
                st.cache_data.clear()
            except Exception as e:
                st.error(f"Push failed — nothing was written. {e}")
