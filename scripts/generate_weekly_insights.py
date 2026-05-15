#!/opt/dreamr-venv/bin/python

"""Generate weekly DreamInsight rows for all eligible users.

Run via cron, e.g.:
  # Sundays at 03:00 — run weekly deep dream insights
  0 3 * * 0  /opt/dreamr-venv/bin/python /data/dreamr/scripts/generate_weekly_insights.py >> /var/log/dreamr/weekly_insights.log 2>&1

Eligibility is enforced inside generate_deep_insights_for_user() — this
script just sweeps every user with at least the minimum number of dreams
and calls the generator. The generator decides whether enough time has
passed and enough new dreams have been logged to warrant a regeneration.

Flags:
  --dry-run        Don't call OpenAI or insert rows; just log who would run.
  --force          Bypass the cadence checks (still requires ≥10 dreams).
  --limit N        Only process the first N eligible users (debugging).
  --user-id ID     Run for a single user (useful for one-off backfills).
"""

import sys
from pathlib import Path

# Make the script work regardless of CWD.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import argparse
import logging
import time

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


def main():
    parser = argparse.ArgumentParser(description="Generate weekly dream insights.")
    parser.add_argument("--dry-run", action="store_true", help="Don't call OpenAI or write to DB.")
    parser.add_argument("--force", action="store_true", help="Bypass cadence checks.")
    parser.add_argument("--limit", type=int, default=None, help="Max users to process.")
    parser.add_argument("--user-id", type=int, default=None, help="Run for a single user.")
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

        LOG.info(f"Processing {len(user_ids)} eligible user(s); dry_run={args.dry_run}, force={args.force}")

        counts = {"ok": 0, "skipped": 0, "locked": 0, "error": 0}

        for uid in user_ids:
            user = User.query.get(uid)
            if not user:
                LOG.warning(f"user_id={uid} not found, skipping")
                continue

            if args.dry_run:
                LOG.info(f"DRY-RUN user_id={uid} email={user.email}")
                continue

            try:
                status, payload = generate_deep_insights_for_user(uid, force=args.force)
            except Exception as e:
                LOG.exception(f"Unhandled error for user_id={uid}: {e}")
                counts["error"] += 1
                continue

            counts[status] = counts.get(status, 0) + 1
            if status == "ok":
                LOG.info(f"OK user_id={uid} insight_id={payload.id} dreams={payload.dream_count}")
            elif status == "skipped":
                LOG.info(f"SKIP user_id={uid} {payload}")
            elif status == "locked":
                LOG.info(f"LOCKED user_id={uid} {payload}")
            else:
                LOG.warning(f"ERROR user_id={uid} {payload}")

            # Gentle pacing between OpenAI calls.
            time.sleep(1)

        LOG.info(f"Done. Counts: {counts}")


if __name__ == "__main__":
    main()
