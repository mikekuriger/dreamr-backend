#!/opt/dreamr-venv/bin/python

"""Generate weekly DreamInsight rows for all eligible users.

Run via cron, e.g.:
  # Sundays at 03:00 — run weekly deep dream insights
  0 3 * * 0  /home/mk7193/dreamr/scripts/generate_weekly_insights.py >> /var/log/dreamr/weekly_insights.log 2>&1

Eligibility is enforced inside generate_deep_insights_for_user() — this
script just sweeps every user with at least the minimum number of dreams
and calls the generator. The generator decides whether enough time has
passed and enough new dreams have been logged to warrant a regeneration.

Work is dispatched across a small thread pool. OpenAI calls are I/O bound
(5–10s waiting on the API), so threads scale this well even though Python
has the GIL. Flask-SQLAlchemy's session is thread-scoped, so each worker
opens its own app_context and gets its own DB session.

Flags:
  --dry-run        Don't call OpenAI or insert rows; just log who would run.
  --force          Bypass the cadence checks (still requires ≥10 dreams).
  --limit N        Only process the first N eligible users (debugging).
  --user-id ID     Run for a single user (useful for one-off backfills).
  --workers N      Worker threads (default 4). Set to 1 to force serial.
"""

import sys
from pathlib import Path

# Make the script work regardless of CWD.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import (
    app,
    db,
    Dream,
    User,
    MIN_DREAMS_FOR_INSIGHTS,
    generate_deep_insights_for_user,
)

LOG = logging.getLogger("generate_weekly_insights")


def _eligible_user_ids():
    """Return user IDs that have at least MIN_DREAMS_FOR_INSIGHTS non-hidden dreams."""
    rows = (
        db.session.query(Dream.user_id, db.func.count(Dream.id).label("c"))
        .filter(Dream.hidden == False)  # noqa: E712
        .group_by(Dream.user_id)
        .having(db.func.count(Dream.id) >= MIN_DREAMS_FOR_INSIGHTS)
        .all()
    )
    return [r[0] for r in rows]


def _process_user(uid, force, dry_run):
    """Runs in a worker thread. Pushes its own app context so Flask-SQLAlchemy
    hands this thread an isolated session. Returns (uid, status, payload)."""
    with app.app_context():
        try:
            user = db.session.get(User, uid)
            if not user:
                return uid, "missing", "user_not_found"

            if dry_run:
                return uid, "dry_run", user.email

            status, payload = generate_deep_insights_for_user(uid, force=force)
            return uid, status, payload
        except Exception as e:
            LOG.exception(f"Unhandled error for user_id={uid}: {e}")
            return uid, "error", f"unhandled: {e}"


def main():
    parser = argparse.ArgumentParser(description="Generate weekly dream insights.")
    parser.add_argument("--dry-run", action="store_true", help="Don't call OpenAI or write to DB.")
    parser.add_argument("--force", action="store_true", help="Bypass cadence checks.")
    parser.add_argument("--limit", type=int, default=None, help="Max users to process.")
    parser.add_argument("--user-id", type=int, default=None, help="Run for a single user.")
    parser.add_argument("--workers", type=int, default=4, help="Worker threads (default 4).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    with app.app_context():
        if args.user_id:
            user_ids = [args.user_id]
        else:
            user_ids = _eligible_user_ids()

        if args.limit:
            user_ids = user_ids[: args.limit]

    workers = max(1, args.workers)
    LOG.info(
        f"Processing {len(user_ids)} eligible user(s); "
        f"workers={workers} dry_run={args.dry_run} force={args.force}"
    )

    counts = {"ok": 0, "skipped": 0, "locked": 0, "error": 0, "missing": 0, "dry_run": 0}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_user, uid, args.force, args.dry_run): uid
            for uid in user_ids
        }
        for fut in as_completed(futures):
            uid, status, payload = fut.result()
            counts[status] = counts.get(status, 0) + 1

            if status == "ok":
                LOG.info(f"OK user_id={uid} insight_id={payload.id} dreams={payload.dream_count}")
            elif status == "skipped":
                LOG.info(f"SKIP user_id={uid} {payload}")
            elif status == "locked":
                LOG.info(f"LOCKED user_id={uid} {payload}")
            elif status == "dry_run":
                LOG.info(f"DRY-RUN user_id={uid} email={payload}")
            elif status == "missing":
                LOG.warning(f"MISSING user_id={uid} {payload}")
            else:
                LOG.warning(f"ERROR user_id={uid} {payload}")

    LOG.info(f"Done. Counts: {counts}")


if __name__ == "__main__":
    main()
