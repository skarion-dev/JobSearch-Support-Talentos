"""
Review & Assign — the daily human step, and the only path to Talentos.

Designed around the real workflow: most days you review yesterday's batch, but
after a weekend or a busy stretch you need to catch up on several days at once.
So the date range is front and centre with one-click presets, and an overview
shows every candidate's backlog before you drill into one.
"""
from datetime import date, timedelta

import psycopg
import streamlit as st

from app import db
from app.config import NEON_DB_URL

PRESETS = {
    "Today": 0,
    "Yesterday + today": 1,
    "Last 3 days": 2,
    "Last 7 days (catch-up)": 6,
}


@st.cache_data(ttl=300)
def load_aes() -> list[dict]:
    """Live roster from Talentos; application engineers first, admins last."""
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT user_id, display_name, email, role
            FROM profiles
            WHERE is_active = true AND role IN ('application_engineer','admin')
            ORDER BY (role <> 'application_engineer'), display_name
        """)
        return [{"user_id": str(r[0]), "name": r[1], "email": r[2], "role": r[3]}
                for r in cur.fetchall()]


@st.cache_data(ttl=120)
def pushed_keys() -> set[str]:
    """company|title already in Talentos for a candidate, to hide re-pushes."""
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT a.candidate_id::text || '|' ||
                   lower(coalesce(j.company,'')) || '|' || lower(coalesce(j.title,''))
            FROM applications a JOIN jobs j ON j.id = a.job_id
        """)
        return {r[0] for r in cur.fetchall()}


def overview(d_from: date, d_to: date) -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT p.candidate_name, count(*) matches,
                   sum(CASE WHEN m.band='TOP_MATCH' THEN 1 ELSE 0 END) tops,
                   max(m.score) best, count(DISTINCT p.base_resume_name) resumes
            FROM resume_job_matches m
            JOIN resume_profiles p ON p.id = m.resume_profile_id
            JOIN keyword_jobs   j ON j.id = m.keyword_job_id
            WHERE p.is_test_account = 0
              AND j.posted_date BETWEEN ? AND ?
            GROUP BY p.candidate_name ORDER BY tops DESC, matches DESC
        """, (d_from.isoformat(), d_to.isoformat())).fetchall()]


def matches_for(candidate: str, resume: str | None, min_score: int,
                d_from: date, d_to: date) -> list[dict]:
    sql = """
        SELECT m.score, m.band, m.reason,
               p.base_resume_name, p.candidate_id, p.base_resume_id,
               j.id local_job_id, j.title, j.company_name, j.location,
               j.posted_date, j.source_url, j.job_url, j.apply_url, j.source
        FROM resume_job_matches m
        JOIN resume_profiles p ON p.id = m.resume_profile_id
        JOIN keyword_jobs   j ON j.id = m.keyword_job_id
        WHERE p.candidate_name = ? AND m.score >= ?
          AND j.posted_date BETWEEN ? AND ?
    """
    params = [candidate, min_score, d_from.isoformat(), d_to.isoformat()]
    if resume and resume != "All":
        sql += " AND p.base_resume_name = ?"
        params.append(resume)
    sql += " ORDER BY m.score DESC, j.posted_date DESC"
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _date_controls() -> tuple[date, date]:
    st.markdown("##### 1 · Which days are you reviewing?")
    c1, c2 = st.columns([2, 3])
    with c1:
        preset = st.radio(
            "Quick pick", list(PRESETS) + ["Custom range"],
            index=1, horizontal=False, label_visibility="collapsed",
        )
    with c2:
        today = date.today()
        if preset == "Custom range":
            picked = st.date_input(
                "Posted between",
                value=(today - timedelta(days=3), today),
                max_value=today,
                help="Pick a start and end date — useful after a weekend.",
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
    st.subheader("Review & Assign")
    st.caption(
        "The only path to Talentos — nothing is sent until you click Assign. "
        "Pushes are idempotent and enforce one candidate per job."
    )

    d_from, d_to = _date_controls()
    st.divider()

    # ---------- overview ----------
    st.markdown("##### 2 · Who has work waiting?")
    rows = overview(d_from, d_to)
    if not rows:
        st.info(
            f"No matches posted between {d_from:%d %b} and {d_to:%d %b}. "
            "Widen the range, or run `python -m scripts.daily_cycle`."
        )
        return

    t1, t2, t3 = st.columns(3)
    t1.metric("Candidates with matches", len(rows))
    t2.metric("Total matches", sum(r["matches"] for r in rows))
    t3.metric("TOP_MATCH", sum(r["tops"] or 0 for r in rows))

    st.dataframe(
        [{"Candidate": r["candidate_name"], "Matches": r["matches"],
          "Top": r["tops"] or 0, "Best": r["best"], "Resumes": r["resumes"]}
         for r in rows],
        use_container_width=True, hide_index=True,
        column_config={
            "Matches": st.column_config.ProgressColumn(
                "Matches", min_value=0,
                max_value=max(r["matches"] for r in rows), format="%d"),
        },
    )

    st.divider()

    # ---------- drill-down ----------
    st.markdown("##### 3 · Pick a candidate and choose what to send")
    c1, c2, c3 = st.columns([3, 3, 2])
    with c1:
        candidate = st.selectbox(
            "Candidate", [r["candidate_name"] for r in rows],
            format_func=lambda n: f"{n}  ({next(r['matches'] for r in rows if r['candidate_name']==n)})",
        )
    with c2:
        with db.get_conn() as conn:
            resumes = [r[0] for r in conn.execute(
                "SELECT DISTINCT base_resume_name FROM resume_profiles WHERE candidate_name=? ORDER BY 1",
                (candidate,)).fetchall()]
        resume = st.selectbox("Base resume", ["All"] + resumes)
    with c3:
        min_score = st.slider("Min score", 75, 100, 90)

    found = matches_for(candidate, resume, min_score, d_from, d_to)
    if not found:
        st.warning("Nothing at this score in this date range. Lower the score or widen the dates.")
        return

    seen = pushed_keys()
    cid = str(found[0]["candidate_id"])
    fresh, dupes = [], 0
    for m in found:
        key = f"{cid}|{(m['company_name'] or '').lower()}|{(m['title'] or '').lower()}"
        if key in seen:
            dupes += 1
        else:
            fresh.append(m)

    s1, s2, s3 = st.columns(3)
    s1.metric("In range", len(found))
    s2.metric("New to Talentos", len(fresh))
    s3.metric("Already applied", dupes, help="Hidden below — already in Talentos for this candidate.")

    if not fresh:
        st.success("Everything in this range is already in Talentos for this candidate.")
        return

    # ---------- assignment ----------
    st.markdown("##### 4 · Assign")
    aes = load_aes()
    a1, a2, a3 = st.columns([3, 1, 1])
    with a1:
        ae_label = st.selectbox(
            "Assign to",
            [f"{a['name']}  ·  {a['role'].replace('_',' ')}" for a in aes],
            help="Live Talentos roster. Application engineers listed first.",
        )
        ae = next(a for a in aes if f"{a['name']}  ·  {a['role'].replace('_',' ')}" == ae_label)
    with a2:
        top_n = st.number_input("Preselect top", 0, len(fresh), min(10, len(fresh)),
                                help="Tick the N highest-scoring rows automatically.")
    with a3:
        st.write("")
        st.write("")
        select_all = st.checkbox("Select all")

    table = [{
        "Send": bool(select_all or i < top_n),
        "Score": m["score"],
        "Band": "TOP" if m["band"] == "TOP_MATCH" else "REVIEW",
        "Posted": m["posted_date"],
        "Title": m["title"],
        "Company": m["company_name"],
        "Location": m["location"],
        "Resume": m["base_resume_name"],
        "Why": (m["reason"] or "")[:120],
        "Link": m["source_url"] or m["apply_url"] or m["job_url"],
        "_job": m["local_job_id"],
    } for i, m in enumerate(fresh)]

    edited = st.data_editor(
        table, use_container_width=True, hide_index=True, height=420,
        disabled=[c for c in table[0] if c != "Send"],
        column_config={
            "Send": st.column_config.CheckboxColumn("✓", width="small"),
            "Score": st.column_config.NumberColumn("Score", width="small"),
            "Band": st.column_config.TextColumn("Band", width="small"),
            "Posted": st.column_config.TextColumn("Posted", width="small"),
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
            "Why": st.column_config.TextColumn("Why matched", width="large"),
            "_job": None,
        },
        key=f"ed_{candidate}_{resume}_{d_from}_{d_to}_{min_score}",
    )

    chosen = [r for r in edited if r.get("Send")]
    b1, b2 = st.columns([1, 3])
    with b1:
        go = st.button(f"Assign {len(chosen)}", type="primary", disabled=not chosen,
                       use_container_width=True)
    with b2:
        if chosen:
            st.caption(
                f"→ **{len(chosen)}** applications for **{candidate}**, assigned to "
                f"**{ae['name']}**, queued into the AI pipeline. Not marked applied."
            )

    if go:
        from scripts.push_to_talentos import push, load_matches as lm
        job_ids = {r["_job"] for r in chosen}
        payload = [m for m in lm(None, 75, 3650, per_candidate_cap=None)
                   if m["local_job_id"] in job_ids and m["candidate_name"] == candidate]
        with st.spinner(f"Pushing {len(payload)} to Talentos…"):
            try:
                from app.auth import actor_label
                stats, _ = push(payload, commit=True, ae_user_id=ae["user_id"],
                                actor=actor_label())
                st.success(
                    f"Assigned {stats.get('will_create_application', 0)} to {ae['name']}. "
                    f"Reused {stats.get('job_reuse', 0)} existing jobs, "
                    f"skipped {stats.get('skip_existing_application', 0)} duplicates."
                )
                st.cache_data.clear()
            except Exception as e:
                st.error(f"Push failed — nothing was written. {e}")
