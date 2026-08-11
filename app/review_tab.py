"""
Review & Assign — the daily human step, and the only path to Talentos.

Pick a candidate, optionally narrow to one base resume, tick the matches worth
sending, choose an AE, and assign. The push runs the same guarded path as the
CLI: dedupe, one-candidate-per-job, idempotency, applied_at left NULL.
"""
import json

import psycopg
import streamlit as st

from app import db
from app.config import NEON_DB_URL


@st.cache_data(ttl=300)
def load_aes() -> list[dict]:
    """Live AE roster from Talentos, admins last."""
    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, display_name, email, role
                FROM profiles
                WHERE is_active = true AND role IN ('application_engineer','admin')
                ORDER BY (role <> 'application_engineer'), display_name
            """)
            return [
                {"user_id": str(r[0]), "name": r[1], "email": r[2], "role": r[3]}
                for r in cur.fetchall()
            ]


def load_candidates() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT p.candidate_name, count(DISTINCT p.id) resumes, count(m.id) matches
            FROM resume_profiles p
            LEFT JOIN resume_job_matches m ON m.resume_profile_id = p.id
            WHERE p.is_test_account = 0
            GROUP BY p.candidate_name ORDER BY matches DESC
        """).fetchall()]


def load_matches(candidate: str, resume: str | None, min_score: int) -> list[dict]:
    sql = """
        SELECT m.id match_id, m.score, m.band, m.reason,
               p.base_resume_name, p.candidate_id, p.base_resume_id,
               j.id local_job_id, j.title, j.company_name, j.location,
               j.posted_date, j.source_url, j.job_url, j.apply_url, j.source
        FROM resume_job_matches m
        JOIN resume_profiles p ON p.id = m.resume_profile_id
        JOIN keyword_jobs   j ON j.id = m.keyword_job_id
        WHERE p.candidate_name = ? AND m.score >= ?
    """
    params = [candidate, min_score]
    if resume and resume != "All base resumes":
        sql += " AND p.base_resume_name = ?"
        params.append(resume)
    sql += " ORDER BY m.score DESC"
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def already_pushed(candidate_id: str) -> set[str]:
    """Normalized company|title already applied for, so the UI can grey them out."""
    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lower(coalesce(j.company,'')) || '|' || lower(coalesce(j.title,''))
                FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE a.candidate_id = %s
            """, (candidate_id,))
            return {r[0] for r in cur.fetchall()}


def render():
    st.subheader("Review & Assign")
    st.caption(
        "The only path to Talentos. Nothing here is sent until you click Assign. "
        "Pushes are idempotent and enforce one candidate per job."
    )

    candidates = load_candidates()
    if not candidates:
        st.info("No matches yet. Run `python -m scripts.daily_cycle`.")
        return

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        picked = st.selectbox(
            "Candidate",
            [f"{c['candidate_name']}  ({c['matches']} matches)" for c in candidates],
        )
        candidate = picked.split("  (")[0]
    with c2:
        with db.get_conn() as conn:
            resumes = [r[0] for r in conn.execute(
                "SELECT DISTINCT base_resume_name FROM resume_profiles WHERE candidate_name=? ORDER BY 1",
                (candidate,)).fetchall()]
        resume = st.selectbox("Base resume", ["All base resumes"] + resumes)
    with c3:
        min_score = st.number_input("Min score", 75, 100, 90)

    matches = load_matches(candidate, resume, min_score)
    if not matches:
        st.warning("No matches at this threshold.")
        return

    seen = already_pushed(matches[0]["candidate_id"])
    fresh = [m for m in matches
             if f"{(m['company_name'] or '').lower()}|{(m['title'] or '').lower()}" not in seen]
    dupes = len(matches) - len(fresh)

    m1, m2, m3 = st.columns(3)
    m1.metric("Matches", len(matches))
    m2.metric("Not yet in Talentos", len(fresh))
    m3.metric("Already applied", dupes)

    st.divider()

    aes = load_aes()
    a1, a2 = st.columns([3, 1])
    with a1:
        ae_label = st.selectbox(
            "Assign to",
            [f"{a['name']} — {a['role']}" for a in aes],
            help="Live roster from Talentos. Application engineers first, admins last.",
        )
        ae = next(a for a in aes if f"{a['name']} — {a['role']}" == ae_label)
    with a2:
        select_all = st.checkbox("Select all shown")

    rows = []
    for m in fresh:
        rows.append({
            "Send": select_all,
            "Score": m["score"],
            "Band": m["band"],
            "Title": m["title"],
            "Company": m["company_name"],
            "Location": m["location"],
            "Posted": m["posted_date"],
            "Resume": m["base_resume_name"],
            "Why": (m["reason"] or "")[:110],
            "Link": m["source_url"] or m["apply_url"] or m["job_url"],
            "_job": m["local_job_id"],
        })

    edited = st.data_editor(
        rows,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in rows[0] if c not in ("Send",)] if rows else None,
        column_config={
            "Send": st.column_config.CheckboxColumn("Send", width="small"),
            "Link": st.column_config.LinkColumn("Link", display_text="open"),
            "Score": st.column_config.NumberColumn("Score", width="small"),
            "_job": None,
        },
        key=f"editor_{candidate}_{resume}",
    )

    chosen = [r for r in edited if r.get("Send")]
    st.caption(f"{len(chosen)} selected")

    if st.button(f"Assign {len(chosen)} to {ae['name']}", type="primary", disabled=not chosen):
        from scripts.push_to_talentos import push, load_matches as lm

        job_ids = {r["_job"] for r in chosen}
        payload = [m for m in lm(None, min_score, 3650, per_candidate_cap=None)
                   if m["local_job_id"] in job_ids and m["candidate_name"] == candidate]

        with st.spinner(f"Pushing {len(payload)} to Talentos..."):
            try:
                stats, _ = push(payload, commit=True, ae_user_id=ae["user_id"])
                st.success(
                    f"Assigned {stats.get('will_create_application', 0)} to {ae['name']}. "
                    "They are queued in the AI pipeline."
                )
                st.cache_data.clear()
            except Exception as e:
                st.error(f"Push failed, nothing was written: {e}")
