import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage

from app import db
from app.agents.ceo_agent import build_ceo_agent, ask_ceo
from app.agents.scraper_agent import scrape_company
from scripts.daily_scrape import run_aggregator_pass

st.set_page_config(page_title="Talentos JobSearch Support", layout="wide")
st.title("Talentos JobSearch Support")
st.caption("Multi-agent job scraper — CEO agent + scraper fleet, powered by OpenCode Go (deepseek-v4-flash / deepseek-v4-pro)")

tab_chat, tab_scrape, tab_readiness, tab_jobs = st.tabs(["CEO Chat", "Scrape Control", "Readiness", "Jobs"])

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
