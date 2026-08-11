"""
One-time processing of the exported Talentos job-search keyword list into a
clean, repo-committed CSV used to drive keyword-based Adzuna searches.

Run: python -m scripts.process_keywords "C:\\path\\to\\all_job_search_keywords.csv"
"""
import csv
import sys
import os

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "keywords.csv")


def main(src_path: str):
    rows = []
    with open(src_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            keyword = (r.get("keyword") or "").strip()
            if not keyword:
                continue
            rows.append(
                {
                    "keyword": keyword,
                    "normalized_keyword": (r.get("normalized_keyword") or keyword.lower()).strip(),
                    "occurrences": int(r.get("occurrences") or 0),
                    "source_types": r.get("source_types") or "",
                }
            )

    # De-dupe by normalized_keyword, keeping the highest occurrence count seen
    by_norm = {}
    for r in rows:
        key = r["normalized_keyword"]
        if key not in by_norm or r["occurrences"] > by_norm[key]["occurrences"]:
            by_norm[key] = r

    deduped = sorted(by_norm.values(), key=lambda r: -r["occurrences"])

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["keyword", "normalized_keyword", "occurrences", "source_types"])
        writer.writeheader()
        writer.writerows(deduped)

    print(f"Processed {len(rows)} rows -> {len(deduped)} unique keywords")
    print(f"Written to {OUT_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m scripts.process_keywords <path_to_csv>")
    main(sys.argv[1])
