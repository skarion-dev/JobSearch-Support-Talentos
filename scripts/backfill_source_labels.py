"""
One-time backfill: keyword_jobs.source was NULL for every row that came
through the Adzuna path, because app/db.py::upsert_keyword_jobs never wrote
the column and app/agents/aggregators/adzuna.py::to_job() never set it.
Fixed going forward (source now defaults to 'could not determine' rather
than NULL); this repairs the historical rows.

Determinable from job_url: an adzuna.com link means the row is Adzuna's,
same as any live row already labeled 'adzuna'. Everything else with a NULL
source gets the honest 'could not determine' rather than staying NULL —
a named unknown, not a silent one that crashes the first f-string that
assumes a string.

Dry run by default.

    python -m scripts.backfill_source_labels
    python -m scripts.backfill_source_labels --commit
"""
import argparse
import logging

from app import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_source_labels")


def main(commit: bool):
    with db.get_conn() as conn:
        total = conn.execute("SELECT count(*) FROM keyword_jobs WHERE source IS NULL").fetchone()[0]
        adzuna = conn.execute(
            "SELECT count(*) FROM keyword_jobs WHERE source IS NULL AND job_url LIKE '%adzuna.com%'"
        ).fetchone()[0]
        undetermined = total - adzuna

        log.info(f"{total} rows with source IS NULL")
        log.info(f"  {adzuna} identifiable as adzuna (adzuna.com in job_url) -> 'adzuna'")
        log.info(f"  {undetermined} not identifiable -> 'could not determine'")

        if not commit:
            log.info("DRY RUN - no writes. Re-run with --commit.")
            return

        conn.execute(
            "UPDATE keyword_jobs SET source='adzuna' "
            "WHERE source IS NULL AND job_url LIKE '%adzuna.com%'"
        )
        conn.execute(
            "UPDATE keyword_jobs SET source='could not determine' WHERE source IS NULL"
        )
        remaining = conn.execute("SELECT count(*) FROM keyword_jobs WHERE source IS NULL").fetchone()[0]
        log.info(f"backfilled {total} rows. remaining NULL: {remaining} (should be 0)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    main(a.commit)
