"""
Export Archive — every manual-chase workbook a run has actually produced,
browsable by date without SSH access to the server.

Nothing on this page is a live query. These are the exact files
scripts.daily_cycle and scripts.export_unpursued wrote to disk via
app.exports.archive_path() — one file per run, grouped under that day's
folder, never overwritten. A run from three days ago still shows exactly
what that run found; tonight's numbers don't retroactively replace it.

Manual Chase's download button is the live-query counterpart and
deliberately does not appear here — filtering to one candidate or a custom
range is a question against current state, not a record of a run.
"""
import os
from datetime import date

import streamlit as st
from openpyxl import load_workbook

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "exports")


def _job_count(path: str) -> int | None:
    """Read the Total cell off the Summary sheet rather than re-parsing the
    workbook's row count — same number app.exports.summarise() would give."""
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        return wb["Summary"]["B7"].value
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _list_runs() -> dict[str, list[dict]]:
    """{"YYYY-MM-DD": [run, ...]}, newest date first, newest run within a day first."""
    out: dict[str, list[dict]] = {}
    if not os.path.isdir(EXPORT_DIR):
        return out
    for day in sorted(os.listdir(EXPORT_DIR), reverse=True):
        day_path = os.path.join(EXPORT_DIR, day)
        if not os.path.isdir(day_path):
            continue
        runs = []
        for fname in sorted(os.listdir(day_path), reverse=True):
            if not fname.lower().endswith(".xlsx"):
                continue
            fpath = os.path.join(day_path, fname)
            stem = fname.rsplit(".", 1)[0]
            time_part = stem.split("_")[-1]
            time_label = (f"{time_part[:2]}:{time_part[2:4]}:{time_part[4:]}"
                          if len(time_part) == 6 and time_part.isdigit() else fname)
            runs.append({
                "name": fname,
                "path": fpath,
                "time_label": time_label,
                "size_kb": round(os.path.getsize(fpath) / 1024),
            })
        if runs:
            out[day] = runs
    return out


def render():
    st.subheader("Export Archive")
    st.caption(
        "Every manual-chase workbook a run has actually produced — one file per "
        "run, grouped by date, never overwritten. For a live filtered download "
        "against current state, use Manual Chase instead."
    )

    by_day = _list_runs()
    if not by_day:
        st.info(
            "No archived runs yet. The nightly cycle writes one here after its "
            "export stage, or run `python -m scripts.export_unpursued`."
        )
        return

    today_str = date.today().isoformat()
    n_runs, n_days = sum(len(r) for r in by_day.values()), len(by_day)
    st.caption(f"{n_runs} run{'s' if n_runs != 1 else ''} across "
               f"{n_days} day{'s' if n_days != 1 else ''}")

    for day, runs in by_day.items():
        label = f"{day} — today" if day == today_str else day
        with st.expander(
            f"**{label}**  ·  {len(runs)} run{'s' if len(runs) != 1 else ''}",
            expanded=(day == today_str),
        ):
            for r in runs:
                c1, c2, c3, c4 = st.columns([1.5, 2, 1.5, 3])
                c1.write(f"`{r['time_label']}`")
                n = _job_count(r["path"])
                c2.write(f"{n} jobs" if n is not None else "—")
                c3.write(f"{r['size_kb']} KB")
                with open(r["path"], "rb") as f:
                    c4.download_button(
                        f"⬇ {r['name']}",
                        data=f.read(),
                        file_name=r["name"],
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=r["path"],
                        use_container_width=True,
                    )
