import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage

from app import db
from app.agents.ceo_agent import build_ceo_agent, ask_ceo
from app.agents.scraper_agent import scrape_company

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
    c4.metric("Jobs in last 30 days", stats["recent_jobs"])

    batch_size = st.number_input("Batch size", min_value=1, max_value=500, value=20)
    if st.button("Run scrape batch"):
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
    st.subheader("Scraped jobs (last 30 days)")
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            SELECT j.title, c.name AS company, j.location, j.remote, j.salary, j.posted_date, j.job_url
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE j.posted_date >= ? OR j.posted_date IS NULL
            ORDER BY j.scraped_at DESC
            """,
            (cutoff,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    st.dataframe(rows, use_container_width=True)
