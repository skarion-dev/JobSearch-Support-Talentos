import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage

from app import db
from app.agents.ceo_agent import build_ceo_agent, ask_ceo
from app.agents.scraper_agent import scrape_company
from scripts.daily_scrape import run_aggregator_pass

st.set_page_config(page_title="Talentos JobSearch Support", layout="wide")

from app.auth import render_identity_badge
_user = render_identity_badge()
st.title("Talentos JobSearch Support")
st.caption("Multi-agent job scraper — CEO agent + scraper fleet, powered by OpenCode Go (deepseek-v4-flash / deepseek-v4-pro)")

tab_review, tab_dash, tab_chat, tab_scrape, tab_readiness, tab_jobs, tab_keywords, tab_matches = st.tabs(
    ["Review & Assign", "Dashboard", "CEO Chat", "Scrape Control", "Readiness", "Jobs", "Keyword Jobs", "Resume Matches"]
)

with tab_review:
    from app.review_tab import render as render_review
    render_review()

with tab_dash:
    from app.dashboard_tab import render as render_dash
    render_dash()

if "history" not in st.session_state:
    st.session_state.history = []
if "ceo_agent" not in st.session_state:
    st.session_state.ceo_agent = build_ceo_agent()

with tab_chat:
    st.subheader("Talk to the CEO agent")
    for msg in st.session_state.history:
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        with st.chat_message(role):
            st.write(msg.content)

    prompt = st.chat_input("Ask for jobs, stats, or to kick off a scrape...")
    if prompt:
        st.session_state.history.append(HumanMessage(content=prompt))
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                reply = ask_ceo(st.session_state.ceo_agent, prompt, st.session_state.history[:-1])
            st.write(reply)
        st.session_state.history.append(AIMessage(content=reply))

with tab_scrape:
    st.subheader("Manual scrape control")
    stats = db.job_stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total companies", stats["total_companies"])
    c2.metric("Scraped", stats["scraped"])
    c3.metric("Total jobs", stats["total_jobs"])
    c4.metric("Jobs in last 10 days", stats["recent_jobs"])

    st.markdown("### Aggregator pass (primary, scales to any company count)")
    st.caption("Bulk-pulls recent US postings from Adzuna and matches them to companies by name. Free, fast, one pass covers many companies.")
    pages = st.number_input("Adzuna pages to pull (50 results/page)", min_value=1, max_value=200, value=10)
    if st.button("Run aggregator pass"):
        with st.spinner("Pulling and matching postings..."):
            companies_matched, jobs_found, _ = run_aggregator_pass(max_pages=pages)
        st.success(f"Matched {companies_matched} companies, found {jobs_found} jobs.")

    st.divider()
    st.markdown("### Fallback pass (per-company ATS/AI scrape, small batches only)")
    st.caption("Use for pending companies the aggregator didn't cover. Slower and not meant for large-scale runs.")
    batch_size = st.number_input("Fallback batch size", min_value=1, max_value=500, value=20)
    if st.button("Run fallback batch"):
        pending = db.fetch_companies(status="pending", limit=batch_size)
        progress = st.progress(0)
        log = st.empty()
        results = []
        for i, company in enumerate(pending):
            res = scrape_company(company)
            results.append(res)
            log.write(f"{company['name']}: {res['status']} ({res.get('jobs', 0)} jobs)")
            progress.progress((i + 1) / max(len(pending), 1))
        st.success(f"Done. {len(results)} companies processed.")

with tab_readiness:
    st.subheader("Scrape readiness")
    st.caption(
        "Companies with a cached deterministic method (Greenhouse/Lever/Workday API) "
        "never need an LLM call again. New/unknown companies fall back to the AI agent, "
        "which then caches a method if it detects a known ATS."
    )
    r = db.readiness_stats()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total companies", r["total_companies"])
    c2.metric("Figured out (deterministic)", r["figured_out"])
    c3.metric("Still needs AI discovery", r["pending_ai_discovery"])

    if r["by_type"]:
        st.write("**By method type:**")
        st.dataframe(
            [{"method": k, "companies": v} for k, v in r["by_type"].items()],
            use_container_width=True,
        )

    st.write("**Last daily run:**")
    if r["last_run"]:
        st.dataframe([r["last_run"]], use_container_width=True)
    else:
        st.info("No daily run has executed yet. Run `python -m scripts.daily_scrape` or wait for the 6AM schedule.")

with tab_jobs:
    st.subheader("Scraped jobs")
    from datetime import datetime, date, timedelta

    with db.get_conn() as conn:
        cur = conn.execute(
            """
            SELECT j.title, c.name AS company, j.location, j.remote, j.salary,
                   j.posted_date, j.job_url, j.scraped_at, j.description
            FROM jobs j JOIN companies c ON c.id = j.company_id
            ORDER BY (j.posted_date IS NULL), j.posted_date DESC, j.scraped_at DESC
            """
        )
        rows = [dict(row) for row in cur.fetchall()]

    if not rows:
        st.info("No jobs scraped yet. Run a scrape batch from the 'Scrape Control' tab.")
    else:
        today = date.today()

        def days_ago(posted):
            if not posted:
                return None
            try:
                return (today - date.fromisoformat(posted[:10])).days
            except ValueError:
                return None

        for r in rows:
            r["days_ago"] = days_ago(r["posted_date"])

        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            search = st.text_input("Search title or company")
        with col2:
            max_age = st.selectbox("Posted within", ["Any time", "3 days", "7 days", "10 days"], index=3)
        with col3:
            sort_choice = st.selectbox("Sort by", ["Newest first", "Oldest first", "Company"])

        filtered = rows
        if search:
            s = search.lower()
            filtered = [r for r in filtered if s in (r["title"] or "").lower() or s in (r["company"] or "").lower()]
        if max_age != "Any time":
            limit_days = int(max_age.split()[0])
            filtered = [r for r in filtered if r["days_ago"] is not None and r["days_ago"] <= limit_days]

        if sort_choice == "Oldest first":
            filtered = sorted(filtered, key=lambda r: (r["posted_date"] is None, r["posted_date"] or ""))
        elif sort_choice == "Company":
            filtered = sorted(filtered, key=lambda r: (r["company"] or "").lower())
        # "Newest first" is already the default DB order

        st.caption(f"{len(filtered)} of {len(rows)} jobs")

        display_rows = []
        for r in filtered:
            age_label = "Unknown" if r["days_ago"] is None else (
                "Today" if r["days_ago"] == 0 else f"{r['days_ago']}d ago"
            )
            display_rows.append(
                {
                    "Title": r["title"],
                    "Company": r["company"],
                    "Location": r["location"],
                    "Posted": r["posted_date"] or "Unknown",
                    "Age": age_label,
                    "Remote": "Yes" if r["remote"] else ("" if r["remote"] is None else "No"),
                    "Salary": r["salary"],
                    "Link": r["job_url"],
                }
            )

        st.dataframe(
            display_rows,
            use_container_width=True,
            column_config={
                "Link": st.column_config.LinkColumn("Link", display_text="Open ->"),
                "Posted": st.column_config.TextColumn("Posted", width="small"),
                "Age": st.column_config.TextColumn("Age", width="small"),
            },
            hide_index=True,
        )

        st.divider()
        st.subheader("Full job description")
        options = {f"{r['title']} — {r['company']}": i for i, r in enumerate(filtered)}
        if options:
            picked = st.selectbox("Pick a job to read", list(options.keys()))
            job = filtered[options[picked]]
            st.caption(f"{job['location'] or 'Location unknown'} · Posted {job['posted_date'] or 'unknown'}")
            st.write(job.get("description") or "No description captured for this job.")

with tab_keywords:
    st.subheader("Keyword-driven job search (USA-wide, Talentos keyword export)")
    st.caption(
        "Searched from data/keywords.csv via Adzuna, independent of the company list above. "
        "Run `python -m scripts.keyword_search` to refresh."
    )

    kstats = db.keyword_job_stats()
    k1, k2, k3 = st.columns(3)
    k1.metric("Total keyword jobs", kstats["total_keyword_jobs"])
    k2.metric("Keywords with hits", kstats["keywords_with_hits"])
    k3.metric("With direct source link", kstats["with_source_url"])
    st.caption(
        "Adzuna's own link is gated behind a login wall, so 'Source link' is found via "
        "web search (title + company) instead — run `python -m scripts.backfill_source_links` to refresh."
    )

    with db.get_conn() as conn:
        krows = conn.execute(
            """
            SELECT keyword, title, company_name, location, remote, salary,
                   posted_date, job_url, source_url, description, scraped_at
            FROM keyword_jobs
            ORDER BY (posted_date IS NULL), posted_date DESC, scraped_at DESC
            """
        ).fetchall()
        all_keywords = [r["keyword"] for r in conn.execute(
            "SELECT DISTINCT keyword FROM keyword_jobs ORDER BY keyword"
        ).fetchall()]

    krows = [dict(r) for r in krows]

    if not krows:
        st.info("No keyword jobs yet. Run scripts.keyword_search first.")
    else:
        kc1, kc2 = st.columns([1, 2])
        with kc1:
            keyword_filter = st.multiselect("Filter by keyword", all_keywords)
        with kc2:
            ksearch = st.text_input("Search title or company", key="kw_search")

        kfiltered = krows
        if keyword_filter:
            kfiltered = [r for r in kfiltered if r["keyword"] in keyword_filter]
        if ksearch:
            s = ksearch.lower()
            kfiltered = [
                r for r in kfiltered
                if s in (r["title"] or "").lower() or s in (r["company_name"] or "").lower()
            ]

        st.caption(f"{len(kfiltered)} of {len(krows)} jobs")

        kdisplay = [
            {
                "Keyword": r["keyword"],
                "Title": r["title"],
                "Company": r["company_name"],
                "Location": r["location"],
                "Posted": r["posted_date"] or "Unknown",
                "Remote": "Yes" if r["remote"] else ("" if r["remote"] is None else "No"),
                "Salary": r["salary"],
                "Adzuna Link": r["job_url"],
                "Source Link": r["source_url"],
            }
            for r in kfiltered
        ]

        st.dataframe(
            kdisplay,
            use_container_width=True,
            column_config={
                "Adzuna Link": st.column_config.LinkColumn("Adzuna Link", display_text="Open ->"),
                "Source Link": st.column_config.LinkColumn("Source Link", display_text="Open ->"),
            },
            hide_index=True,
        )

        st.divider()
        st.subheader("Keyword hit-rate breakdown")
        with db.get_conn() as conn:
            breakdown = conn.execute(
                "SELECT keyword, count(*) n FROM keyword_jobs GROUP BY keyword ORDER BY n DESC"
            ).fetchall()
        st.dataframe([dict(r) for r in breakdown], use_container_width=True, hide_index=True)

with tab_matches:
    import json as _json

    st.subheader("Resume-to-job matches (report-only, local, never sent to Talentos)")
    st.caption(
        "Each base resume is treated as a separate candidate profile, per the Talentos "
        "matching masterprompt. Scores use the same rubric (title/role, skills, "
        "responsibilities, seniority, location, freshness). Run "
        "`python -m scripts.sync_resume_profiles` to refresh profiles, then "
        "`python -m scripts.match_resumes_to_jobs` to refresh matches."
    )

    with db.get_conn() as conn:
        profiles = [dict(r) for r in conn.execute(
            "SELECT * FROM resume_profiles ORDER BY candidate_name, base_resume_name"
        ).fetchall()]

    if not profiles:
        st.info("No resume profiles synced yet. Run `python -m scripts.sync_resume_profiles`.")
    else:
        profile_options = {
            f"{p['candidate_name']} — {p['base_resume_name']}"
            + ("" if p["is_match_ready"] else " (needs review)"): p["id"]
            for p in profiles
        }
        picked_label = st.selectbox("Base resume profile", list(profile_options.keys()))
        profile_id = profile_options[picked_label]
        profile = next(p for p in profiles if p["id"] == profile_id)

        p1, p2, p3 = st.columns(3)
        p1.metric("Review status", profile["review_status"] or "unknown")
        p2.metric("Auto-queue ready", "Yes" if profile["is_match_ready"] else "No")
        p3.metric("Active keywords", len(_json.loads(profile["keywords"] or "[]")))

        if profile["additional_rules"]:
            with st.expander("Profile rules"):
                st.write(profile["additional_rules"])

        with db.get_conn() as conn:
            match_rows = conn.execute(
                """
                SELECT m.score, m.band, m.reason, m.matched_terms, m.matched_at,
                       j.title, j.company_name, j.location, j.posted_date,
                       j.job_url, j.source_url, j.description
                FROM resume_job_matches m
                JOIN keyword_jobs j ON j.id = m.keyword_job_id
                WHERE m.resume_profile_id = ?
                ORDER BY m.score DESC
                LIMIT 50
                """,
                (profile_id,),
            ).fetchall()
        match_rows = [dict(r) for r in match_rows]

        if not match_rows:
            st.info("No matches yet for this profile. Run `python -m scripts.match_resumes_to_jobs`.")
        else:
            st.caption(f"Top {len(match_rows)} matches (max 50 per masterprompt cap)")
            mdisplay = [
                {
                    "Score": r["score"],
                    "Band": r["band"],
                    "Title": r["title"],
                    "Company": r["company_name"],
                    "Location": r["location"],
                    "Posted": r["posted_date"] or "Unknown",
                    "Reason": r["reason"],
                    "Link": r["source_url"] or r["job_url"],
                }
                for r in match_rows
            ]
            st.dataframe(
                mdisplay,
                use_container_width=True,
                column_config={
                    "Link": st.column_config.LinkColumn("Link", display_text="Open ->"),
                    "Score": st.column_config.NumberColumn("Score", width="small"),
                    "Band": st.column_config.TextColumn("Band", width="small"),
                },
                hide_index=True,
            )

            st.divider()
            st.subheader("Match detail")
            job_options = {f"{r['title']} — {r['company_name']} ({r['score']})": i for i, r in enumerate(match_rows)}
            picked_job = st.selectbox("Pick a match to inspect", list(job_options.keys()))
            detail = match_rows[job_options[picked_job]]
            st.write(f"**Reason:** {detail['reason']}")
            matched_terms = _json.loads(detail["matched_terms"] or "[]")
            if matched_terms:
                st.write(f"**Matched terms:** {', '.join(matched_terms)}")
            st.write("**Job description:**")
            st.write(detail["description"] or "No description captured.")
