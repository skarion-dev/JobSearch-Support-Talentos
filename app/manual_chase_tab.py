"""
Manual Chase — good matches automation cannot pursue, as a downloadable sheet.

Roughly half of all matches land here, almost all Adzuna-sourced: Adzuna
truncates descriptions at ~500 characters and gates its own links, so there is
neither enough text to tailor a resume nor a page left to scrape.

Rather than letting those disappear, they become an assignable worklist. Same
filters as Review & Assign, same workbook the nightly cycle writes, so the file
someone downloads here is the file they would have got from the command line.
"""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app import db, exports
from app.quality import MIN_DESCRIPTION

PRESETS = {
    "Today": 0,
    "Yesterday + today": 1,
    "Last 3 days": 2,
    "Last 7 days": 6,
    "Last 30 days": 29,
}


def _candidates() -> list[str]:
    with db.get_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT candidate_name FROM resume_profiles "
            "WHERE is_test_account=0 ORDER BY 1"
        ).fetchall()]


def render():
    st.subheader("Manual Chase")
    st.caption(
        "Strong matches the pipeline cannot automate — the description is too "
        "short to tailor a resume from and the page could not be recovered. "
        "Download the sheet and hand it to whoever is chasing these by hand."
    )

    # ---------- filters ----------
    f1, f2, f3 = st.columns([2, 2, 2])
    with f1:
        preset = st.selectbox("Posted", list(PRESETS) + ["Custom range", "All time"], index=4)
    with f2:
        candidate = st.selectbox("Candidate", ["All"] + _candidates())
    with f3:
        min_score = st.slider("Min score", 75, 100, 90,
                              help="Same scale as Review & Assign.")

    today = date.today()
    d_from = d_to = None
    if preset == "Custom range":
        picked = st.date_input("Between", value=(today - timedelta(days=7), today),
                               max_value=today)
        if isinstance(picked, tuple) and len(picked) == 2:
            d_from, d_to = picked
    elif preset != "All time":
        d_from, d_to = today - timedelta(days=PRESETS[preset]), today

    rows = exports.unpursued_rows(min_score=min_score, d_from=d_from, d_to=d_to,
                                  candidate=candidate)

    if not rows:
        st.success(
            "Nothing to chase in this range — every match here has a full "
            "description and can go through the pipeline."
        )
        return

    summary = exports.summarise(rows)

    # ---------- headline ----------
    m1, m2, m3 = st.columns(3)
    m1.metric("Needing manual chase", summary["total"])
    m2.metric("Candidates affected", len(summary["by_candidate"]))
    m3.metric("Avg description held", f"{int(sum(len(r['description'] or '') for r in rows)/len(rows))} chars",
              help=f"{MIN_DESCRIPTION} is the minimum the resume generator can work with.")

    # ---------- download, first thing on the page after the numbers ----------
    st.download_button(
        f"⬇  Download Excel  ({summary['total']} jobs)",
        data=exports.build_workbook(rows),
        file_name=exports.filename(d_from, d_to),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
    st.caption(
        "Two sheets: every job with a clickable link and why automation gave up, "
        "plus a summary by candidate and reason."
    )

    st.divider()

    # ---------- why ----------
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown("**Why automation gave up**")
        st.dataframe(
            pd.DataFrame(
                [{"Reason": k, "Jobs": v} for k, v in
                 sorted(summary["by_reason"].items(), key=lambda x: -x[1])]
            ),
            use_container_width=True, hide_index=True,
        )
    with c2:
        st.markdown("**By candidate**")
        st.dataframe(
            pd.DataFrame(
                [{"Candidate": k, "Jobs": v} for k, v in
                 sorted(summary["by_candidate"].items(), key=lambda x: -x[1])]
            ),
            use_container_width=True, hide_index=True,
        )

    st.divider()

    # ---------- the list ----------
    st.markdown("**The jobs**")
    st.dataframe(
        [{
            "Score": r["score"],
            "Candidate": r["candidate_name"],
            "Title": r["title"],
            "Company": r["company_name"],
            "Location": r["location"],
            "Posted": r["posted_date"],
            "Source": r["source"],
            "Have": len(r["description"] or ""),
            "Link": r["source_url"] or r["apply_url"] or r["job_url"],
        } for r in rows],
        use_container_width=True, hide_index=True, height=420,
        column_config={
            "Score": st.column_config.NumberColumn("Score", width="small"),
            "Have": st.column_config.NumberColumn(
                "Chars", width="small",
                help=f"Description length we hold. Needs {MIN_DESCRIPTION}+."),
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
        },
    )
