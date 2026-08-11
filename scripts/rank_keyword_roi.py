"""
Rank keywords by MEASURED return on API spend, not by theory.

Two prior attempts at ranking were guesses:
  data/keywords.csv          occurrence across the historical export
  data/profile_keywords.csv  overlap with the 19 active profiles

Now there is evidence. Every keyword_jobs row records the keyword that found
it, and resume_job_matches records which of those jobs a profile actually
matched. So for each keyword we can measure what a spent API call bought.

Observed on the first 250-keyword run:
  - 109/250 keywords produced ZERO matches (5,848 jobs, ~32% of the corpus)
  - generic artifacts/processes ("QA/QC", "BOM", "Floor Plans", "DOT
    compliance") pull high volume and convert nothing
  - role titles ("AutoCAD Drafter" 60%, "CAD Drafter" 50%) convert

ROI score
---------
  precision   matched / jobs                     x 100   value per job pulled
  breadth     distinct profiles served           x  12   serves many candidates
  quality     top_matches / jobs                 x  40   TOP over REVIEWABLE
  penalty     high-volume zero-match keywords     -50    actively wasteful

Keywords never searched keep a prior from the profile ranking so new terms can
still enter the rotation instead of being locked out by having no history.

Run: python -m scripts.rank_keyword_roi --top 250
"""
import argparse
import csv
import json
import os
from collections import defaultdict

from app import db

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "roi_keywords.csv")
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "profile_keywords.csv")

PRIOR_SCORE = 40.0   # unsearched terms sit mid-pack: worth trying, not proven


def measured_performance() -> dict[str, dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT j.keyword,
                   count(DISTINCT j.id)                AS jobs,
                   count(DISTINCT m.keyword_job_id)    AS matched,
                   sum(CASE WHEN m.band='TOP_MATCH' THEN 1 ELSE 0 END) AS tops,
                   count(DISTINCT m.resume_profile_id) AS profiles
            FROM keyword_jobs j
            LEFT JOIN resume_job_matches m ON m.keyword_job_id = j.id
            GROUP BY j.keyword
            """
        ).fetchall()
    return {r["keyword"]: dict(r) for r in rows}


def roi_score(jobs: int, matched: int, tops: int, profiles: int) -> float:
    if not jobs:
        return PRIOR_SCORE
    precision = matched / jobs
    quality = (tops or 0) / jobs
    score = precision * 100 + profiles * 12 + quality * 40
    if matched == 0 and jobs >= 50:
        score -= 50          # proven waste, push below untried terms
    return round(score, 1)


def load_profile_prior() -> list[str]:
    if not os.path.exists(PROFILE_PATH):
        return []
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return [r["keyword"] for r in csv.DictReader(f)]


def load_profile_terms() -> set[str]:
    """Every term on an active profile — the legal universe of keywords."""
    terms = set()
    with db.get_conn() as conn:
        for row in conn.execute(
            "SELECT keywords FROM resume_profiles WHERE is_test_account = 0"
        ).fetchall():
            for t in json.loads(row["keywords"] or "[]"):
                if t and t.strip():
                    terms.add(t.strip())
    return terms


def main(top_n: int):
    perf = measured_performance()
    universe = load_profile_terms() | set(perf.keys())
    prior_order = {k: i for i, k in enumerate(load_profile_prior())}

    ranked = []
    for term in universe:
        p = perf.get(term)
        if p:
            score = roi_score(p["jobs"], p["matched"], p["tops"], p["profiles"])
            ranked.append(
                {
                    "keyword": term,
                    "roi_score": score,
                    "status": "measured",
                    "jobs": p["jobs"],
                    "matched": p["matched"],
                    "top_matches": p["tops"] or 0,
                    "profiles_served": p["profiles"],
                }
            )
        else:
            # never searched — prior, nudged by its profile-relevance rank
            bump = max(0, 20 - prior_order.get(term, 999) / 25)
            ranked.append(
                {
                    "keyword": term,
                    "roi_score": round(PRIOR_SCORE + bump, 1),
                    "status": "untried",
                    "jobs": 0, "matched": 0, "top_matches": 0, "profiles_served": 0,
                }
            )

    ranked.sort(key=lambda r: (-r["roi_score"], r["keyword"].lower()))
    top = ranked[:top_n]

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["keyword", "roi_score", "status", "jobs", "matched", "top_matches", "profiles_served"],
        )
        w.writeheader()
        w.writerows(top)

    measured = [r for r in top if r["status"] == "measured"]
    untried = [r for r in top if r["status"] == "untried"]
    dropped = [r for r in ranked[top_n:] if r["status"] == "measured" and r["matched"] == 0]

    print(f"universe: {len(universe)} terms ({len(perf)} with measured history)")
    print(f"selected top {len(top)}: {len(measured)} measured, {len(untried)} untried")
    print(f"dropped {len(dropped)} proven zero-match keywords from the rotation")
    print(f"-> {OUT_PATH}\n")
    print("Top 20 by measured ROI:")
    for r in top[:20]:
        if r["status"] == "measured":
            print(f"  {r['roi_score']:>6}  {r['matched']:>3}/{r['jobs']:<4} matched, "
                  f"{r['profiles_served']} profiles  {r['keyword']}")
        else:
            print(f"  {r['roi_score']:>6}  (untried)                    {r['keyword']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=250)
    args = parser.parse_args()
    main(top_n=args.top)
