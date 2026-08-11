"""
Derive the top-N Adzuna search keywords from the ACTIVE base-resume profiles,
rather than from raw occurrence counts in the historical export.

Why this differs from data/keywords.csv:
  keywords.csv is ranked by how often a term appeared across the whole
  historical export (including archived profiles, legacy OpenJobs defaults,
  and Apify groups). That over-weights terms nobody active is hiring against.

Ranking signal here:
  profile_count   how many of the 19 active profiles carry the term  (x100)
  candidate_count how many distinct candidates carry it              (x10)
  is_role_like    term looks like a job title, not a bare tool/skill (+25)
  brevity         short, searchable phrases beat long ones

A bare tool ("AutoCAD") returns noisy results on a job board; a role phrase
("AutoCAD Drafter") returns postings. Both are kept, roles ranked higher.

Run: python -m scripts.rank_profile_keywords --top 250
"""
import argparse
import csv
import json
import os
import re
from collections import defaultdict

from app import db

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "profile_keywords.csv")

ROLE_WORDS = re.compile(
    r"\b(engineer|technician|designer|drafter|analyst|specialist|manager|"
    r"coordinator|administrator|developer|architect|operator|planner|"
    r"surveyor|estimator|programmer|lead|intern|associate|consultant)\b",
    re.IGNORECASE,
)

# Terms too generic to be useful as a standalone job-board query
# (masterprompt s.5: a generic keyword is not enough for a recommendation)
STOPLIST = {
    "engineer", "designer", "project", "python", "cad", "gis", "bim", "asic",
    "design", "analysis", "documentation", "standards", "broadband", "as-built",
}


def score_term(term: str, profile_count: int, candidate_count: int) -> float:
    score = profile_count * 100 + candidate_count * 10
    if ROLE_WORDS.search(term):
        score += 25
    words = len(term.split())
    if words == 1:
        score -= 15          # bare tokens are noisy queries
    elif words > 4:
        score -= 10          # over-long phrases return nothing
    return score


def main(top_n: int, include_test: bool):
    query = "SELECT candidate_name, base_resume_name, keywords FROM resume_profiles"
    if not include_test:
        query += " WHERE is_test_account = 0"

    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(query).fetchall()]

    profiles_with: dict[str, set] = defaultdict(set)
    candidates_with: dict[str, set] = defaultdict(set)
    original_case: dict[str, str] = {}

    for row in rows:
        for term in json.loads(row["keywords"] or "[]"):
            term = (term or "").strip()
            if not term:
                continue
            key = term.lower()
            if key in STOPLIST:
                continue
            profiles_with[key].add(row["base_resume_name"])
            candidates_with[key].add(row["candidate_name"])
            original_case.setdefault(key, term)

    ranked = []
    for key in profiles_with:
        pc = len(profiles_with[key])
        cc = len(candidates_with[key])
        ranked.append(
            {
                "keyword": original_case[key],
                "score": round(score_term(original_case[key], pc, cc), 1),
                "profile_count": pc,
                "candidate_count": cc,
                "is_role_like": int(bool(ROLE_WORDS.search(original_case[key]))),
            }
        )

    ranked.sort(key=lambda r: (-r["score"], r["keyword"].lower()))
    top = ranked[:top_n]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["keyword", "score", "profile_count", "candidate_count", "is_role_like"]
        )
        w.writeheader()
        w.writerows(top)

    roles = sum(r["is_role_like"] for r in top)
    shared = sum(1 for r in top if r["profile_count"] > 1)
    print(f"{len(rows)} active profiles -> {len(ranked)} distinct terms -> top {len(top)} written")
    print(f"  role-like terms : {roles}/{len(top)}")
    print(f"  shared by 2+ profiles: {shared}")
    print(f"  -> {OUT_PATH}")
    print("\nTop 15:")
    for r in top[:15]:
        print(f"  {r['score']:>6}  {r['keyword']}  (profiles={r['profile_count']}, candidates={r['candidate_count']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=250)
    parser.add_argument("--include-test", action="store_true")
    args = parser.parse_args()
    main(top_n=args.top, include_test=args.include_test)
