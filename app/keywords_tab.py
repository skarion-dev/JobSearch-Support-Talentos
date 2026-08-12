"""
Keywords — which terms actually got searched, and what they bought.

Reuses scripts.rank_keyword_roi's own measurement rather than re-deriving it,
so the ranking an operator sees here is exactly the ranking that decides
tonight's keyword set — see that module's docstring for the ROI formula and
what "measured" data has already shown (role titles convert, tool/artefact
names don't).
"""
import json
import os
from datetime import date

import pandas as pd
import streamlit as st

from app import db
from scripts.rank_keyword_roi import measured_performance, roi_score

TONIGHT_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "tonight_keywords.json")


def _last_searched() -> dict[str, str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT keyword, max(scraped_at) last_seen FROM keyword_jobs GROUP BY keyword"
        ).fetchall()
    return {r["keyword"]: r["last_seen"] for r in rows}


def _tonight() -> dict | None:
    if not os.path.exists(TONIGHT_JSON):
        return None
    try:
        with open(TONIGHT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def render():
    st.subheader("Keywords")
    st.caption(
        "Every keyword ever searched, ranked by measured return: jobs it found, "
        "how many became real matches, how many were TOP_MATCH quality, and how "
        "many distinct candidates it served. This is the same ranking the "
        "nightly strategist reads before choosing tonight's set."
    )

    # ---------- tonight's picks, if the strategist has run ----------
    tonight = _tonight()
    if tonight:
        chosen_date = tonight["chosen_at"][:10]
        label = "Tonight's picks" if chosen_date == date.today().isoformat() else f"Last strategist run — {chosen_date}"
        with st.expander(f"**{label}** — {tonight['n_chosen']} of {tonight['n_requested']} requested", expanded=False):
            st.write(tonight.get("reasoning") or "No reasoning recorded for this run.")
            st.caption(f"Chosen at {tonight['chosen_at']}")

    # ---------- performance table ----------
    perf = measured_performance()
    last_seen = _last_searched()

    if not perf:
        st.info("No keywords searched yet. Run `python -m scripts.keyword_search` or wait for the nightly cycle.")
        return

    rows = []
    for kw, p in perf.items():
        jobs, matched, tops, profiles = p["jobs"], p["matched"], p["tops"] or 0, p["profiles"]
        rows.append({
            "Keyword": kw,
            "ROI score": roi_score(jobs, matched, tops, profiles),
            "Jobs found": jobs,
            "Matched": matched,
            "Match rate": round(100 * matched / jobs, 1) if jobs else 0.0,
            "Top matches": tops,
            "Profiles served": profiles,
            "Last searched": (last_seen.get(kw) or "")[:19],
        })

    search = st.text_input("Filter keywords", placeholder="e.g. drafter, QA, project manager")
    if search:
        s = search.lower()
        rows = [r for r in rows if s in r["Keyword"].lower()]

    c1, c2, c3, c4 = st.columns(4)
    zero_match = sum(1 for r in rows if r["Jobs found"] >= 50 and r["Matched"] == 0)
    c1.metric("Keywords shown", len(rows))
    c2.metric("Total jobs found", sum(r["Jobs found"] for r in rows))
    c3.metric("Total matched", sum(r["Matched"] for r in rows))
    c4.metric("Proven zero-match (50+ jobs)", zero_match,
              help="High volume, zero matches — dropped from the rotation by rank_keyword_roi.")

    st.dataframe(
        pd.DataFrame(rows).sort_values("ROI score", ascending=False),
        use_container_width=True, hide_index=True, height=520,
        column_config={
            "ROI score": st.column_config.NumberColumn("ROI score", width="small"),
            "Jobs found": st.column_config.NumberColumn("Jobs found", width="small"),
            "Matched": st.column_config.NumberColumn("Matched", width="small"),
            "Match rate": st.column_config.NumberColumn("Match rate", format="%.1f%%", width="small"),
            "Top matches": st.column_config.NumberColumn("Top matches", width="small"),
            "Profiles served": st.column_config.NumberColumn("Profiles served", width="small"),
        },
    )
    st.caption(
        "Click any column header to sort. ROI score combines match precision, "
        "breadth across candidates, and TOP_MATCH quality, with a penalty for "
        "high-volume zero-match keywords — see scripts/rank_keyword_roi.py."
    )

    # ---------- browse raw jobs for one keyword ----------
    st.divider()
    st.markdown("**Browse jobs for a keyword**")
    all_keywords = sorted(perf.keys())
    picked = st.selectbox("Keyword", ["Select..."] + all_keywords)
    if picked != "Select...":
        with db.get_conn() as conn:
            jrows = [dict(r) for r in conn.execute(
                """
                SELECT title, company_name, location, posted_date, job_url, source_url, remote, salary
                FROM keyword_jobs WHERE keyword = ?
                ORDER BY (posted_date IS NULL), posted_date DESC
                """,
                (picked,),
            ).fetchall()]
        st.caption(f"{len(jrows)} jobs found for “{picked}”")
        st.dataframe(
            [{
                "Title": r["title"], "Company": r["company_name"], "Location": r["location"],
                "Posted": r["posted_date"] or "Unknown",
                "Remote": "Yes" if r["remote"] else ("" if r["remote"] is None else "No"),
                "Salary": r["salary"],
                "Link": r["source_url"] or r["job_url"],
            } for r in jrows],
            use_container_width=True, hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ->")},
        )
