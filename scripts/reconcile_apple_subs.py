#!/opt/dreamr-venv/bin/python

"""Reconcile Apple App Store subscriptions with Dreamr DB.

Usage (from back_end/dreamr):
  python reconcile_apple_subs.py --dry-run
  python reconcile_apple_subs.py --apply --limit 100

This script does NOT run automatically; you call it when you want to
re-sync Apple billing truth into user_subscriptions.

It expects that:
- user_subscriptions rows for Apple have payment_provider='apple'.
- receipt_data holds the base64 App Store receipt string.
- SubscriptionPlan.product_id is the App Store product identifier
  (e.g. com.example.dreamr.pro_monthly).

KNOWN LIMITATION (as of Aug 2026): the app's live purchase flow
(SubscriptionService._verify_apple_receipt, "Path 1" in app.py) now stores
raw StoreKit2 transaction JSON as receipt_data for most rows, not the
legacy base64 App Store receipt this script's verifyReceipt call expects.
Apple's legacy /verifyReceipt endpoint rejects StoreKit2 JSON with
status 21002 ("malformed receipt"), so those rows cannot be reconciled by
this script today — see _is_storekit2_json() below, which detects and
skips them with an honest reason instead of a confusing 21002. Properly
reconciling those rows requires Apple's App Store Server API (a signed
JWT call using an App Store Connect "In-App Purchase" key: issuer id, key
id, .p8 private key — none of which exist in config.py yet), or an Apple
Server Notifications V2 webhook. Neither is implemented yet.

Environment variables:
- APPLE_SHARED_SECRET (optional): app-specific shared secret used for
  auto-renewable subscriptions. If provided, it is sent as `password`.
  Otherwise falls back to app.config["APPSTORE_SHARED_SECRET"] (the same
  value the live purchase flow uses) so there is one source of truth.
"""
import sys
from pathlib import Path

# Always import Dreamr app from the repo root, regardless of CWD
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent   # if script will live in dreamr/scripts/
sys.path.insert(0, str(REPO_ROOT))

import argparse
import datetime as dt
import json
import logging
import os
from typing import Any, Dict, Optional

import requests

from app import app, db, UserSubscription, SubscriptionPlan


LOG = logging.getLogger("reconcile_apple_subs")

# Official Apple endpoints for the legacy verifyReceipt API
APPLE_VERIFY_PROD = "https://buy.itunes.apple.com/verifyReceipt"
APPLE_VERIFY_SBX = "https://sandbox.itunes.apple.com/verifyReceipt"


def _call_apple_verify(
    receipt_data: str,
    shared_secret: Optional[str] = None,
    use_sandbox: bool = False,
) -> Dict[str, Any]:
    """Call Apple verifyReceipt and return the parsed JSON.

    This helper does a single call to either the production or sandbox
    endpoint. Higher-level logic handles 21007/21008 switching.
    """

    url = APPLE_VERIFY_SBX if use_sandbox else APPLE_VERIFY_PROD
    payload: Dict[str, Any] = {"receipt-data": receipt_data}
    if shared_secret:
        payload["password"] = shared_secret

    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data


def verify_receipt_with_fallback(receipt_data: str) -> Dict[str, Any]:
    """Verify a receipt, handling prod/sandbox switching.

    Apple’s guidance: send to production first; if status == 21007,
    resend to sandbox. Some environments go the other way (21008), so
    we handle that as well.
    """

    # APPLE_SHARED_SECRET env var wins if set; otherwise use the same
    # config value app.py's live purchase flow uses (SubscriptionService.
    # APPSTORE_SHARED_SECRET / config.APPSTORE_SHARED_SECRET), so there is
    # a single source of truth instead of a second hardcoded copy here.
    shared_secret = os.getenv("APPLE_SHARED_SECRET") or app.config.get("APPSTORE_SHARED_SECRET")

    try:
        resp = _call_apple_verify(receipt_data, shared_secret, use_sandbox=False)
    except Exception as e:  # noqa: BLE001
        LOG.error("verifyReceipt production call failed: %s", e)
        raise

    status = resp.get("status")
    if status == 21007:
        # Receipt is from the sandbox; retry there.
        LOG.info("Receipt reported as sandbox (21007); retrying against sandbox endpoint")
        resp = _call_apple_verify(receipt_data, shared_secret, use_sandbox=True)
    elif status == 21008:
        # Receipt is from production but was sent to sandbox; retry prod.
        LOG.info("Receipt reported as production (21008); ensure production endpoint is used")
        resp = _call_apple_verify(receipt_data, shared_secret, use_sandbox=False)

    return resp


def _is_storekit2_json(receipt_data: str) -> bool:
    """True if receipt_data looks like the StoreKit2 transaction JSON the
    app's live purchase flow now stores, rather than a legacy base64 App
    Store receipt. See KNOWN LIMITATION note at the top of this file."""
    try:
        tx = json.loads(receipt_data)
    except (TypeError, ValueError):
        return False
    return isinstance(tx, dict) and "transactionId" in tx


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(str(value))
    except Exception:  # noqa: BLE001
        return default


def map_apple_to_local(
    verify_resp: Dict[str, Any],
    product_id: str,
) -> Optional[Dict[str, Any]]:
    """Map Apple verifyReceipt payload to Dreamr fields.

    Returns dict: {"status": str, "end_date": datetime | None, "auto_renew": bool}
    or None if we cannot find a matching transaction.
    """

    latest_info = verify_resp.get("latest_receipt_info") or []
    if not isinstance(latest_info, list):
        latest_info = []

    # Narrow to this plan’s product_id when possible.
    items = [i for i in latest_info if i.get("product_id") == product_id]
    if not items:
        # Fallback: take all items if there is exactly one distinct product.
        products = {i.get("product_id") for i in latest_info if i.get("product_id")}
        if len(products) == 1:
            items = latest_info

    if not items:
        LOG.warning("No latest_receipt_info entries for product_id=%s", product_id)
        return None

    # Pick the transaction with the furthest expires_date_ms.
    def _expiry_ms(it: Dict[str, Any]) -> int:
        return _parse_int(it.get("expires_date_ms"))

    latest = max(items, key=_expiry_ms)
    expiry_ms = _expiry_ms(latest)
    end_date = dt.datetime.utcfromtimestamp(expiry_ms / 1000.0) if expiry_ms else None

    now = dt.datetime.utcnow()
    status = "active" if end_date and now < end_date else "expired"

    # auto_renew_status lives in pending_renewal_info for the
    # original_transaction_id / product_id pair.
    auto_renew = False
    pending = verify_resp.get("pending_renewal_info") or []
    if isinstance(pending, list):
        otid = latest.get("original_transaction_id")
        for entry in pending:
            if otid and entry.get("original_transaction_id") != otid:
                continue
            if entry.get("product_id") not in (None, product_id):
                continue
            ars = str(entry.get("auto_renew_status", "0"))
            auto_renew = ars == "1"
            break

    return {
        "status": status,
        "end_date": end_date,
        "auto_renew": auto_renew,
    }


def reconcile_apple_subscriptions(dry_run: bool = True, limit: Optional[int] = None) -> None:
    with app.app_context():
        q = UserSubscription.query.filter_by(payment_provider="apple")
        # Require a stored receipt so we can actually ask Apple.
        q = q.filter(UserSubscription.receipt_data.isnot(None))
        if limit is not None:
            q = q.limit(limit)

        db_count = q.count()
        LOG.info(
            "Found %s Apple user_subscriptions with receipt_data", db_count,
        )
        print(
            f"[reconcile_apple_subs] Found {db_count} Apple subs to check "
            f"(dry_run={dry_run})"
        )
        if db_count == 0:
            return

        total = 0
        updated = 0
        unsupported_storekit2 = 0

        for sub in q:
            total += 1
            receipt = (sub.receipt_data or "").strip()
            if not receipt:
                LOG.warning(
                    "user_subscriptions id=%s has empty receipt_data; skipping", sub.id
                )
                print(
                    f"[reconcile_apple_subs] SKIP user={sub.user_id} sub={sub.id}: "
                    "no receipt_data"
                )
                continue

            if _is_storekit2_json(receipt):
                # Don't send this to legacy verifyReceipt — it will come
                # back status=21002 (malformed), which looks like a receipt
                # problem when it's actually a "wrong API for this receipt
                # format" problem. See KNOWN LIMITATION note at top of file.
                unsupported_storekit2 += 1
                LOG.warning(
                    "user_subscriptions id=%s has StoreKit2 JSON receipt_data; "
                    "legacy verifyReceipt cannot check it, skipping", sub.id
                )
                print(
                    f"[reconcile_apple_subs] SKIP user={sub.user_id} sub={sub.id}: "
                    "receipt_data is StoreKit2 JSON, not a legacy App Store "
                    "receipt — needs App Store Server API support (not yet implemented)"
                )
                continue

            plan: SubscriptionPlan = sub.plan
            if plan is None:
                LOG.warning("user_subscriptions id=%s has no plan; skipping", sub.id)
                print(
                    f"[reconcile_apple_subs] SKIP user={sub.user_id} sub={sub.id}: no plan"
                )
                continue

            product_id = plan.product_id or plan.id

            LOG.info(
                "Reconciling Apple sub user=%s sub=%s plan=%s product_id=%s",
                sub.user_id,
                sub.id,
                sub.plan_id,
                product_id,
            )
            print(
                f"[reconcile_apple_subs] Checking user={sub.user_id} sub={sub.id} "
                f"plan={sub.plan_id} product_id={product_id}"
            )

            try:
                resp = verify_receipt_with_fallback(receipt)
            except Exception as e:  # noqa: BLE001
                LOG.error("Apple verifyReceipt error user=%s sub=%s: %s", sub.user_id, sub.id, e)
                print(
                    f"[reconcile_apple_subs] Apple API ERROR user={sub.user_id} "
                    f"sub={sub.id}: {e}"
                )
                continue

            status_code = resp.get("status")
            if status_code not in (0, "0"):
                LOG.warning(
                    "Non-success verifyReceipt status for user=%s sub=%s: %s",
                    sub.user_id,
                    sub.id,
                    status_code,
                )
                print(
                    f"[reconcile_apple_subs] SKIP user={sub.user_id} sub={sub.id}: "
                    f"verifyReceipt status={status_code}"
                )
                continue

            new_fields = map_apple_to_local(resp, product_id)
            if new_fields is None:
                print(
                    f"[reconcile_apple_subs] SKIP user={sub.user_id} sub={sub.id}: "
                    "no matching Apple transaction for this product_id"
                )
                continue

            changed = (
                sub.status != new_fields["status"]
                or sub.end_date != new_fields["end_date"]
                or sub.auto_renew != new_fields["auto_renew"]
            )

            print(
                "[reconcile_apple_subs] DB vs Apple for "
                f"user={sub.user_id} sub={sub.id}: "
                f"status {sub.status} -> {new_fields['status']}, "
                f"end {sub.end_date} -> {new_fields['end_date']}, "
                f"auto_renew {sub.auto_renew} -> {new_fields['auto_renew']} "
                f"(changed={changed}, dry_run={dry_run})"
            )

            if not dry_run and changed:
                sub.status = new_fields["status"]
                sub.end_date = new_fields["end_date"]
                sub.auto_renew = new_fields["auto_renew"]
                updated += 1
                print(
                    f"[reconcile_apple_subs] UPDATED user={sub.user_id} sub={sub.id} "
                    f"to status={sub.status}, end={sub.end_date}, auto_renew={sub.auto_renew}"
                )

        if not dry_run:
            db.session.commit()

        LOG.info(
            "Processed %s Apple subs, updated %s, %s skipped as unsupported StoreKit2 JSON",
            total, updated, unsupported_storekit2,
        )
        print(
            f"[reconcile_apple_subs] Processed {total} Apple subs, "
            f"updated {updated}, {unsupported_storekit2} skipped as unsupported "
            f"StoreKit2 JSON (dry_run={dry_run})"
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Log only, no DB writes")
    parser.add_argument("--apply", action="store_true", help="Apply DB changes")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of subs")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        raise SystemExit("Use either --dry-run or --apply, not both.")

    dry_run = not args.apply
    reconcile_apple_subscriptions(dry_run=dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
