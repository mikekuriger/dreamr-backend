"""Apple StoreKit2 JWS verification — Server Notifications V2 and direct
purchase-flow verification both live here.

Apple POSTs a signed JWS payload to our /appstore/notifications endpoint on
every subscription lifecycle event (renewal, cancellation, refund, plan
change, ...). This module verifies that payload really came from Apple
(using Apple's own app-store-server-library, not a hand-rolled JWKS check —
Apple explicitly recommends against reimplementing this), decodes it, and
updates the matching user_subscriptions row.

verify_transaction_jws() below verifies a single signed transaction JWS —
used both for a notification's data.signedTransactionInfo, and (as of Aug
2026) for SubscriptionService._verify_apple_receipt's purchase-flow
verification in app.py, once the client sends serverVerificationData (the
real signed JWS) instead of the unsigned jsonRepresentation it used to.

Design notes
------------
- Verification uses Apple's official `app-store-server-library`
  (https://github.com/apple/app-store-server-library-python) rather than a
  custom JWS/OCSP implementation. Pin >=3.1.2 — 3.1.1 and earlier have a
  disclosed OCSP-bypass issue (GHSA-8f6j-263m-g72x).
- One endpoint handles both Sandbox and Production: Apple lets you register
  the same URL for both fields in App Store Connect, and every notification
  carries its own environment. We peek at the *unverified* JWT to see which
  environment it claims, pick the matching SignedDataVerifier (each one
  pinned to its own Environment), and then perform real signature
  verification with that verifier — the peek never establishes trust by
  itself, it only selects which trust root to check against.
- Idempotency: Apple retries a notification delivery reusing the same
  `notificationUUID` (Apple's own documented dedup key — see
  https://developer.apple.com/documentation/appstoreservernotifications/notificationuuid).
  We gate processing on that. We also record `transaction_id` on the same
  row per-event, so "has this transaction's event already been recorded" is
  answerable directly — see AppleNotificationEvent in app.py.
- Subscription identity: we look up / update user_subscriptions by
  provider_subscription_id (Apple's originalTransactionId), never by the
  per-event transactionId — same fix as SubscriptionService._create_subscription.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from appstoreserverlibrary.models.Environment import Environment as AppleEnvironment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
    VerificationStatus,
)

logger = logging.getLogger("dreamr")

CERT_DIR = Path(__file__).resolve().parent / "certs"
ROOT_CERT_FILES = ["AppleRootCA-G3.cer"]

# Notification types that never touch user_subscriptions (informational,
# consumables, or one-time-purchase-only). We still record them for the
# idempotency ledger, we just don't try to map them onto a subscription.
NON_SUBSCRIPTION_NOTIFICATION_TYPES = {
    "TEST",
    "CONSUMPTION_REQUEST",
    "METADATA_UPDATE",
    "ONE_TIME_CHARGE",
    "EXTERNAL_PURCHASE_TOKEN",
}


class AppleNotificationConfig(Exception):
    """Raised when Apple notification handling is asked to run without the
    config it needs (bundle id / app apple id / root certs)."""


_verifiers: dict = {}  # AppleEnvironment -> SignedDataVerifier, built lazily


def _load_root_certificates() -> list:
    certs = []
    for fname in ROOT_CERT_FILES:
        path = CERT_DIR / fname
        if not path.is_file():
            raise AppleNotificationConfig(
                f"Missing Apple root certificate at {path}. Download it from "
                "https://www.apple.com/certificateauthority/ (DER/.cer format) "
                "and commit it under certs/."
            )
        certs.append(path.read_bytes())
    return certs


def _get_verifier(app, apple_environment: AppleEnvironment) -> SignedDataVerifier:
    if apple_environment in _verifiers:
        return _verifiers[apple_environment]

    bundle_id = app.config.get("APPLE_BUNDLE_ID")
    app_apple_id = app.config.get("APPLE_APP_APPLE_ID")
    if not bundle_id:
        raise AppleNotificationConfig("APPLE_BUNDLE_ID is not configured")
    if apple_environment == AppleEnvironment.PRODUCTION and not app_apple_id:
        raise AppleNotificationConfig(
            "APPLE_APP_APPLE_ID is not configured (required to verify Production "
            "notifications) — this is the numeric app id from your App Store "
            "Connect URL, e.g. appstoreconnect.apple.com/apps/<this number>/..."
        )

    verifier = SignedDataVerifier(
        root_certificates=_load_root_certificates(),
        enable_online_checks=True,
        environment=apple_environment,
        bundle_id=bundle_id,
        app_apple_id=app_apple_id,
    )
    _verifiers[apple_environment] = verifier
    return verifier


def _peek_environment(signed_payload: str) -> AppleEnvironment:
    """Read the (not-yet-verified) environment claim so we know which
    SignedDataVerifier to check the signature against. This does not trust
    the payload — verify_and_decode_notification() re-checks the
    environment against the chosen verifier's configuration afterwards, so
    a forged claim here just picks the "wrong" verifier and fails
    verification, it can't itself grant trust."""
    import jwt  # PyJWT, already a hard dependency of the app

    try:
        unverified = jwt.decode(signed_payload, options={"verify_signature": False})
    except Exception as e:
        # Not even a well-formed JWT — treat identically to a signature
        # verification failure (400, not 500): this payload can never
        # succeed no matter how many times Apple retries it.
        raise VerificationException(VerificationStatus.VERIFICATION_FAILURE) from e

    data = unverified.get("data") or unverified.get("summary") or {}
    raw_env = data.get("environment") or unverified.get("environment")
    return AppleEnvironment.SANDBOX if raw_env == "Sandbox" else AppleEnvironment.PRODUCTION


def is_jws(value: str) -> bool:
    """True if value looks like compact JWS serialization (header.payload.signature)
    rather than plain JSON. Used to tell a real signed StoreKit2 transaction
    (serverVerificationData) apart from the old unsigned jsonRepresentation
    some still-installed app versions may send."""
    value = (value or "").strip()
    return value.count(".") == 2 and not value.startswith("{")


def verify_transaction_jws(app, signed_transaction: str):
    """Verify + decode a single signed StoreKit2 transaction JWS. Used both
    for a notification's data.signedTransactionInfo and for direct
    purchase-flow verification (SubscriptionService._verify_apple_receipt
    in app.py). Returns a JWSTransactionDecodedPayload. Raises
    VerificationException if the signature doesn't check out, or
    AppleNotificationConfig if this server isn't configured to verify."""
    apple_environment = _peek_environment(signed_transaction)
    verifier = _get_verifier(app, apple_environment)
    return verifier.verify_and_decode_signed_transaction(signed_transaction)


def _ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if not ms:
        return None
    return datetime.utcfromtimestamp(int(ms) / 1000.0)


def _map_transaction_to_status(tx) -> Tuple[str, Optional[datetime]]:
    """Mirror of reconcile_apple_subs.py's map_apple_to_local, applied to a
    single already-verified transaction instead of a verifyReceipt blob."""
    if tx.revocationDate:
        return "refunded", _ms_to_dt(tx.revocationDate)

    end_date = _ms_to_dt(tx.expiresDate)
    now = datetime.utcnow()
    status = "active" if end_date and now < end_date else "expired"
    return status, end_date


def _extract_auto_renew(renewal) -> Optional[bool]:
    if renewal is None or renewal.rawAutoRenewStatus is None:
        return None
    return bool(int(renewal.rawAutoRenewStatus))


def handle_notification(app, db, UserSubscription, AppleNotificationEvent, signed_payload: str) -> dict:
    """Verify + process one Server Notifications V2 delivery.

    Returns a dict describing the outcome (for logging / tests). Raises
    VerificationException if the payload does not verify — the Flask route
    is responsible for turning that into an HTTP response.
    """
    apple_environment = _peek_environment(signed_payload)
    verifier = _get_verifier(app, apple_environment)
    payload = verifier.verify_and_decode_notification(signed_payload)

    notification_uuid = payload.notificationUUID
    notification_type = payload.rawNotificationType
    subtype = payload.rawSubtype

    # --- Idempotency gate -------------------------------------------------
    # Apple redelivers retries under the *same* notificationUUID, so this is
    # the correct key to dedupe on (per Apple's own docs). We still capture
    # transaction_id on the row itself so "was this transaction's event
    # processed" is directly queryable too.
    if notification_uuid:
        already = AppleNotificationEvent.query.filter_by(
            notification_uuid=notification_uuid
        ).first()
        if already:
            logger.info(
                "[apple_notifications] duplicate notificationUUID=%s (%s/%s) — already "
                "processed as event id=%s, skipping",
                notification_uuid, notification_type, subtype, already.id,
            )
            return {
                "status": "duplicate",
                "notification_type": notification_type,
                "subtype": subtype,
            }

    data = payload.data
    tx = None
    renewal = None
    transaction_id = None
    original_transaction_id = None

    if data and data.signedTransactionInfo:
        tx = verifier.verify_and_decode_signed_transaction(data.signedTransactionInfo)
        transaction_id = tx.transactionId
        original_transaction_id = tx.originalTransactionId
    if data and data.signedRenewalInfo:
        renewal = verifier.verify_and_decode_renewal_info(data.signedRenewalInfo)

    result = {
        "status": "ignored",
        "notification_type": notification_type,
        "subtype": subtype,
        "original_transaction_id": original_transaction_id,
    }

    if tx is not None and original_transaction_id and notification_type not in NON_SUBSCRIPTION_NOTIFICATION_TYPES:
        sub = UserSubscription.query.filter_by(
            payment_provider="apple",
            provider_subscription_id=original_transaction_id,
        ).first()

        new_status, end_date = _map_transaction_to_status(tx)
        auto_renew = _extract_auto_renew(renewal)

        if sub is not None:
            sub.status = new_status
            sub.end_date = end_date
            if auto_renew is not None:
                sub.auto_renew = auto_renew
            sub.provider_transaction_id = transaction_id
            result["status"] = "updated"
            result["subscription_id"] = sub.id
            logger.info(
                "[apple_notifications] %s/%s updated user_sub id=%s "
                "orig_txn=%s txn=%s -> status=%s end=%s auto_renew=%s",
                notification_type, subtype, sub.id, original_transaction_id,
                transaction_id, new_status, end_date, sub.auto_renew,
            )
        else:
            # No local row for this lineage. We deliberately do NOT create
            # one here — we don't know the user_id or plan_id from the
            # notification alone, and guessing would risk attaching a
            # subscription to the wrong account. This should be rare (the
            # row is normally created by SubscriptionService._create_subscription
            # at purchase time) — log loudly so it's investigable, and let
            # the App Store Server API reconciliation cron catch it.
            result["status"] = "no_matching_subscription"
            logger.warning(
                "[apple_notifications] %s/%s for originalTransactionId=%s has no "
                "matching user_subscriptions row (payment_provider='apple'); "
                "nothing updated. txn=%s",
                notification_type, subtype, original_transaction_id, transaction_id,
            )

    event = AppleNotificationEvent(
        notification_uuid=notification_uuid,
        notification_type=notification_type,
        subtype=subtype,
        transaction_id=transaction_id,
        original_transaction_id=original_transaction_id,
        signed_date=_ms_to_dt(payload.signedDate),
    )
    db.session.add(event)
    db.session.commit()

    return result
