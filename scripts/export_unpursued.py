"""
Export the matched jobs we cannot pursue automatically, for manual chasing.

Thin CLI over app/exports.py. The workbook itself is built there so that the
Manual Chase tab, the nightly cycle and this command all produce an identical
file — an operator downloading from the app and a developer running this get the
same sheet.

    python -m scripts.export_unpursued
    python -m scripts.export_unpursued --min-score 85 --candidate "Bhaskar Roy"
"""
import argparse
import os
from datetime import date, timedelta

from app import exports

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "exports")


def main(min_score: int, days: int | None, candidate: str | None, out: str | None):
    d_from = d_to = None
    if days:
        d_to = date.today()
        d_from = d_to - timedelta(days=days)

    rows = exports.unpursued_rows(min_score=min_score, d_from=d_from, d_to=d_to,
                                  candidate=candidate)
    if not rows:
        print("Nothing needs manual chasing with these filters.")
        return

    path = out or os.path.join(OUT_DIR, exports.filename(d_from, d_to))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(exports.build_workbook(rows))

    summary = exports.summarise(rows)
    print(f"{summary['total']} unpursued matches -> {os.path.abspath(path)}")
    print("by candidate:", dict(sorted(summary["by_candidate"].items(), key=lambda x: -x[1])))
    print("by reason:", summary["by_reason"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=int, default=90)
    ap.add_argument("--days", type=int, default=None, help="Only postings from the last N days")
    ap.add_argument("--candidate", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    main(a.min_score, a.days, a.candidate, a.out)
