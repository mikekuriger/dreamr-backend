### File: app.py
from authlib.integrations.flask_client import OAuth # for google auth
from datetime import datetime, date, timezone, timedelta
from dateutil.relativedelta import relativedelta
from enum import Enum
from flask_cors import CORS
from flask import abort, Blueprint, current_app, Flask, jsonify, redirect, render_template, render_template_string, request, session, url_for
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_mail import Message, Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from google.oauth2 import id_token
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport import requests as google_requests
from jwt import InvalidTokenError, PyJWKClient
from langdetect import detect
from openai import OpenAI
from PIL import Image
from PIL import ImageFilter
from prompts import CATEGORY_PROMPTS, TONE_TO_STYLE
import apple_notifications
from appstoreserverlibrary.signed_data_verifier import VerificationException as AppleVerificationException
import moderation
from quota import ensure_week_current, next_reset_iso, get_or_create_credits
from quota import decrement_text_or_deny, refund_text
from quota import decrement_image_or_deny, refund_image
from quota import IMAGE_CREDIT_COST
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import JSON as MySQLJSON
from werkzeug.utils import secure_filename
from zoneinfo import ZoneInfo
import base64
import bcrypt
import hashlib, secrets, hmac
import io
import json
import logging
import openai
import os
import random
import re
import requests
import jwt
import shutil
import string
import time
import traceback
import uuid


logger = logging.getLogger("dreamr")
logger.setLevel(logging.INFO)
# logger.setLevel(logging.DEBUG)
logger.handlers.clear()

log_dir = "/home/mk7193/dreamr"
os.makedirs(log_dir, exist_ok=True)
file_handler = logging.FileHandler(os.path.join(log_dir, "dreamr.log"))
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(file_handler)


client = OpenAI()
openai.api_key = os.environ.get("OPENAI_API_KEY")

app = Flask(__name__)
# app.config.from_pyfile('config.py')
cfg_path = os.getenv("FLASK_CONFIG_FILE", "/home/mk7193/dreamr/config.py")
if os.path.exists(cfg_path):
    app.config.from_pyfile(cfg_path)

# env overrides (Flask 3)
app.config.from_prefixed_env(prefix="DREAMR")

CORS(app, supports_credentials=True,origins=["https://dreamr.zentha.me", "https://dreamr-us-west-01.zentha.me", "http://localhost:5173"])

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.init_app(app)
migrate = Migrate(app, db)
mail = Mail(app)

# for hashing email addresses after user deletes their account
# hashed email + any unused credits will be saved but all other data deleted
# new users will also be hashed and compared to list of hashes for a match
# if a match is found, user will get their credits restored
# this is to prevent abuse from users deleting and creating new accounts to get free credits
SECRET_PEPPER = "mikekuriger@gmail.com".encode("utf-8")
def hash_string_secret(value: str) -> str:
    return hmac.new(SECRET_PEPPER, value.encode("utf-8"), hashlib.sha256).hexdigest()

WEB_CLIENT_ID = "846080686597-61d3v0687vomt4g4tl7rueu7rv9qrari.apps.googleusercontent.com"
IOS_CLIENT_ID = "846080686597-8u85pj943ilkmlt583f3tct5h9ca0c3t.apps.googleusercontent.com"
ALLOWED_AUDS = {WEB_CLIENT_ID, IOS_CLIENT_ID}
ALLOWED_ISS = {"https://accounts.google.com", "accounts.google.com"}

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
# For native Sign in with Apple, this should match the client_id used on iOS (bundle id or service id).
APPLE_BUNDLE_ID = "me.zentha.dreamr"
APPLE_CLIENT_ID = os.getenv("APPLE_CLIENT_ID") or APPLE_BUNDLE_ID

_apple_jwk_client = PyJWKClient(APPLE_JWKS_URL)

# App Store Server Notifications V2. Register this SAME url for both the
# "Production Server URL" and "Sandbox Server URL" fields in App Store
# Connect (bottom of https://appstoreconnect.apple.com/apps/6747240349/distribution/info)
# — every notification carries its own environment, and apple_notifications
# picks the matching verifier per-request, so one endpoint handles both.
@app.post("/appstore/notifications")
def appstore_notifications():
    body = request.get_json(force=True, silent=True) or {}
    signed_payload = body.get("signedPayload")
    if not signed_payload:
        logger.warning("[apple_notifications] POST with no signedPayload: %r", body)
        return jsonify({"status": "bad_request"}), 400

    try:
        result = apple_notifications.handle_notification(
            app, db, UserSubscription, AppleNotificationEvent, signed_payload
        )
    except AppleVerificationException as e:
        # Either misconfigured (wrong bundle id / app apple id / missing
        # root cert) or the payload didn't come from Apple. Either way we
        # must not report success. Log with enough detail to tell those
        # apart, but never log the payload itself.
        logger.error("[apple_notifications] verification failed: status=%s", e.status)
        return jsonify({"status": "verification_failed"}), 400
    except apple_notifications.AppleNotificationConfig as e:
        logger.error("[apple_notifications] not configured: %s", e)
        return jsonify({"status": "not_configured"}), 500
    except Exception:
        db.session.rollback()
        logger.exception("[apple_notifications] unexpected error processing notification")
        # 5xx (not 2xx) so Apple retries later instead of us silently
        # dropping the event on a transient failure (DB hiccup, etc.).
        return jsonify({"status": "error"}), 500

    return jsonify({"status": "ok", **result}), 200


# --- Admin config ---
# ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
app.config.setdefault("ADMIN_EMAILS", os.getenv("ADMIN_EMAILS", ""))


# Password reset with token (when user clicks the email)
# ---------------------------------------------------------------------------
# Minimal web reset page (temporary until mobile deep-link is live)
# ---------------------------------------------------------------------------
RESET_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Reset your Dreamr password</title>
  <meta name="robots" content="noindex,nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { background:#0b0420; color:#fff; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }
    .card { width: min(480px, 92vw); background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 14px; padding: 22px; box-shadow: 0 6px 24px rgba(0,0,0,0.35); }
    h1 { font-size: 20px; margin: 0 0 8px; }
    p { color:#D1B2FF; margin: 0 0 16px; }
    input[type=password] { width:100%; padding:12px; border-radius:10px; border:1px solid rgba(255,255,255,0.25); background:rgba(0,0,0,0.3); color:#fff; margin-bottom:12px; }
    button { width:100%; padding:12px; border-radius:10px; border:0; background:#fff; color:#000; font-weight:600; cursor:pointer; }
    .msg{ margin-top:12px; padding:10px; border-radius:10px; }
    .err{ background:rgba(255,0,0,0.16); border:1px solid rgba(255,0,0,0.35);}
    .ok{ background:rgba(0,200,0,0.16); border:1px solid rgba(0,200,0,0.35);}
    .hint{ font-size:12px; color:#bfb8d8; margin-top:8px; text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    {% if invalid %}
      <h1>Link expired or invalid</h1>
      <p>Please request a new reset link from the Dreamr app.</p>
      <div class="hint">You can close this window.</div>
    {% elif done %}
      <h1>Password updated</h1>
      <p>You can now open the Dreamr app and sign in with your new password.</p>
      <div class="hint">You can close this window.</div>
    {% else %}
      <h1>Set a new password</h1>
      <p>Enter a new password for your account.</p>
      {% if error %}<div class="msg err">{{ error }}</div>{% endif %}
      <form method="post">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="password" name="pw1" placeholder="New password (min 8 chars)" minlength="8" required>
        <input type="password" name="pw2" placeholder="Confirm new password" minlength="8" required>
        <button type="submit">Update Password</button>
      </form>
      <div class="hint">After updating, open the Dreamr app and log in.</div>
    {% endif %}
  </div>
</body>
</html>
"""

# Confirm email with token
# ---------------------------------------------------------------------------
# Minimal web reset page (temporary until mobile deep-link is live)
# ---------------------------------------------------------------------------
CONFIRM_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Dreamr account confirmation</title>
  <meta name="robots" content="noindex,nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { background:#0b0420; color:#fff; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }
    .card { width: min(520px, 92vw); background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 14px; padding: 22px; box-shadow: 0 6px 24px rgba(0,0,0,0.35); }
    h1 { font-size: 20px; margin: 0 0 8px; }
    p { color:#D1B2FF; margin: 0 0 16px; }
    .hint{ font-size:12px; color:#bfb8d8; margin-top:8px; text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    {% if status == "ok" %}
      <h1>Account confirmed</h1>
      <p>You're all set! Open the Dreamr app and sign in.</p>
      <div class="hint">You can close this window.</div>
    {% elif status == "exists" %}
      <h1>Already confirmed</h1>
      <p>Your account was already active. Open the Dreamr app and sign in.</p>
      <div class="hint">You can close this window.</div>
    {% elif status == "expired" %}
      <h1>Link expired</h1>
      <p>Please request a new confirmation email from the Dreamr app.</p>
      <div class="hint">You can close this window.</div>
    {% else %}
      <h1>Invalid link</h1>
      <p>This confirmation link is not valid.</p>
      <div class="hint">You can close this window.</div>
    {% endif %}
  </div>
</body>
</html>
"""



# google auth
oauth = OAuth(app)

with open('/home/mk7193/dreamr/google_oauth_credentials.json') as f:
    google_creds = json.load(f)

google = oauth.register(
    name='google',
    client_id=google_creds['web']['client_id'],
    client_secret=google_creds['web']['client_secret'],
    access_token_url='https://oauth2.googleapis.com/token',
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    api_base_url='https://www.googleapis.com/oauth2/v2/',
    client_kwargs={'scope': 'openid email profile'},
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration'
)

# Models
class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    # password = db.Column(db.String(128), nullable=False)
    password = db.Column(db.String(128), nullable=True, default='')
    first_name = db.Column(db.String(50), nullable=True)
    birthdate = db.Column(db.Date, nullable=True)
    gender = db.Column(db.String(20), nullable=True)  # e.g., "male", "female", prefer not to say"
    signup_date = db.Column(db.DateTime, default=db.func.now())
    timezone = db.Column(db.String(50), nullable=True)  # e.g., "America/Los_Angeles"
    language = db.Column(db.String(10), nullable=True, default='en')
    avatar_filename = db.Column(db.String(200), nullable=True)
    enable_audio = db.Column(db.Boolean, default=False)
    email_confirmed = db.Column(db.Boolean, nullable=False, server_default=text("0"))
    apple_user_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    subscriptions = db.relationship("UserSubscription", back_populates="user")
    payments = db.relationship("PaymentTransaction", back_populates="user")

class PendingUser(db.Model):
    __tablename__ = 'pendingusers'

    uuid = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)
    first_name = db.Column(db.String(50), nullable=True)
    signup_date = db.Column(db.DateTime, default=db.func.now())
    timezone = db.Column(db.String(50), nullable=True)  # e.g., "America/Los_Angeles"
    language = db.Column(db.String(10), nullable=True, default='en')
    expires_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=24))

class Dream(db.Model):
    __tablename__ = "dream"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    text = db.Column(db.Text)                      # user's dream text
    analysis = db.Column(db.Text)                  # AI's response
    summary = db.Column(db.Text)                   # AI's response summarized
    tone = db.Column(db.String(50))                # AI's tone evaluation
    image_prompt = db.Column(db.Text)              # AI's image prompt
    hidden = db.Column(db.Boolean, default=False)  # Hides the entry (reversable)
    image_file = db.Column(db.String(255))         # saved filename (e.g., 'dream_123.png')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    notes = db.Column(db.Text, nullable=True)
    notes_updated_at = db.Column(db.DateTime, nullable=True)
    is_question = db.Column(db.Boolean, nullable=False, server_default=db.text("0"))
    interpreter_id = db.Column(db.Integer, db.ForeignKey('interpreters.id'), nullable=True, index=True)

    def set_notes(self, notes_text):
        # Only treat explicit None as clear
        if notes_text is None:
            self.notes = None
        else:
            # Optional normalization (keeps user’s spaces):
            txt = str(notes_text).replace("\r\n", "\n")
            self.notes = txt
        self.notes_updated_at = datetime.utcnow()


    def __repr__(self):
        return f"<Dream id={self.id} user_id={self.user_id} hidden={self.hidden}>"


class Discuss(db.Model):
    __tablename__ = "discuss"

    id = db.Column(db.Integer, primary_key=True)
    dream_id = db.Column(db.Integer, db.ForeignKey('dream.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)      # user's message
    response = db.Column(db.Text)                  # AI's response
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index("ix_discuss_dream_created", "dream_id", "created_at"),
    )


class DreamInsight(db.Model):
    __tablename__ = "dream_insight"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    generated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    window_start = db.Column(db.DateTime, nullable=False)
    window_end = db.Column(db.DateTime, nullable=False)
    dream_count = db.Column(db.Integer, nullable=False)

    narrative = db.Column(db.Text, nullable=False)
    symbols = db.Column(db.Text, nullable=False, default="[]")     # JSON-encoded list
    themes = db.Column(db.Text, nullable=False, default="[]")
    patterns = db.Column(db.Text, nullable=False, default="[]")
    questions = db.Column(db.Text, nullable=False, default="[]")

    model = db.Column(db.String(64), nullable=True)
    prompt_version = db.Column(db.Integer, nullable=False, default=1)

    __table_args__ = (
        db.Index("ix_dream_insight_user_generated", "user_id", "generated_at"),
    )


class LifeEvent(db.Model):
    __tablename__ = "life_event"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    title = db.Column(db.String(120), nullable=False)     # e.g., "Car accident"
    details = db.Column(db.Text, nullable=True)           # optional narrative
    occurred_at = db.Column(db.DateTime, nullable=False)  # when it happened
    tags = db.Column(MySQLJSON, nullable=True)            # e.g., ["accident","injury"]

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # helpful indexes: (user_id, occurred_at) for quick recent-pull
    __table_args__ = (
        db.Index("ix_life_event_user_occurred", "user_id", "occurred_at"),
    )

    def __repr__(self):
        return f"<LifeEvent id={self.id} user_id={self.user_id} title={self.title!r}>"


class ContentReport(db.Model):
    """User-submitted report flagging AI-generated content as offensive.

    Required by Google Play's AI-Generated Content policy: users must be
    able to flag offensive AI output from within the app, and developers
    must use those reports to inform moderation. Reports here are
    reviewed by an admin; the client hides flagged content from the
    reporter immediately so they don't see it again.
    """
    __tablename__ = "content_report"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    # What was reported: "analysis", "image", "insight", "discuss", "other"
    content_type = db.Column(db.String(32), nullable=False)
    # Free-form pointer to the source row (dream id, discuss id, insight id).
    # String avoids coupling to a single FK target.
    content_id = db.Column(db.String(64), nullable=True, index=True)
    # User-selected reason
    category = db.Column(db.String(32), nullable=False)
    comment = db.Column(db.Text, nullable=True)
    # Snapshot of what the user actually saw, capped client-side at 2KB.
    # Survives even if the underlying record gets edited or deleted.
    content_snapshot = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Review workflow: "open", "reviewing", "actioned", "dismissed"
    status = db.Column(db.String(16), nullable=False, default="open")
    reviewed_at = db.Column(db.DateTime, nullable=True)
    action = db.Column(db.String(64), nullable=True)

    def __repr__(self):
        return (
            f"<ContentReport id={self.id} user_id={self.user_id} "
            f"type={self.content_type} cat={self.category} status={self.status}>"
        )


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)  # sha256 hex
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    request_ip = db.Column(db.String(64))
    user_agent = db.Column(db.String(256))
    user = db.relationship('User')


class EmailConfirmToken(db.Model):
    __tablename__ = 'email_confirm_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User')

# --- Subscription plans ---
class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.String(50), primary_key=True)  # e.g., "pro_monthly"
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    period = db.Column(db.String(20), nullable=False)  # 'monthly', 'yearly'
    
    # Legacy/simple list of strings (keep for backward compatibility)
    features = db.Column(MySQLJSON)
    
    # New: rich feature cards (title + description, optional metadata)
    feature_cards = db.Column(MySQLJSON)  # nullable by default
    
    product_id = db.Column(db.String(100))

    created_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    user_subscriptions = db.relationship("UserSubscription", back_populates="plan")

# --- User subscriptions ---
class UserSubscription(db.Model):
    __tablename__ = "user_subscriptions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan_id = db.Column(db.String(50), db.ForeignKey("subscription_plans.id"), nullable=False, index=True)

    status = db.Column(db.String(20), nullable=False)  # active/canceled/expired
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime)

    auto_renew = db.Column(db.Boolean, server_default=text("0"), nullable=False)
    payment_method = db.Column(db.String(50))
    payment_provider = db.Column(db.String(50))
    provider_subscription_id = db.Column(db.String(100), index=True)
    provider_transaction_id = db.Column(db.String(100), index=True)
    receipt_data = db.Column(db.Text)

    created_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    user = db.relationship("User", back_populates="subscriptions")
    plan = db.relationship("SubscriptionPlan", back_populates="user_subscriptions")
    payments = db.relationship("PaymentTransaction", back_populates="subscription")

# --- Payment transactions ---
class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("user_subscriptions.id"), index=True)

    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), server_default=text("'USD'"), nullable=False)

    status = db.Column(db.String(20), nullable=False)   # pending/completed/failed
    provider = db.Column(db.String(50), nullable=False) # apple/google/stripe
    provider_transaction_id = db.Column(db.String(100), index=True)

    provider_response = db.Column(MySQLJSON)
    created_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    user = db.relationship("User", back_populates="payments")
    subscription = db.relationship("UserSubscription", back_populates="payments")

# --- Idempotency ledger for App Store Server Notifications V2 ---
class AppleNotificationEvent(db.Model):
    """One row per processed Apple notification delivery. notification_uuid
    is Apple's own retry-safe dedup key (retries of the same event reuse the
    same UUID); we gate processing on it. transaction_id is recorded too so
    "was this transaction's event handled" is directly queryable — see
    apple_notifications.handle_notification()."""
    __tablename__ = "apple_notification_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    notification_uuid = db.Column(db.String(36), nullable=False, unique=True, index=True)
    notification_type = db.Column(db.String(50), nullable=False)
    subtype = db.Column(db.String(50))
    transaction_id = db.Column(db.String(100), index=True)
    original_transaction_id = db.Column(db.String(100), index=True)
    signed_date = db.Column(db.DateTime)
    processed_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)

# --- For free users ---
class UserCredits(db.Model):
    __tablename__ = "user_credits"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    free_credits = db.Column(db.Integer, nullable=False, default=2)        # weekly free quota (bumped to 2 each Sunday)
    purchased_credits = db.Column(db.Integer, nullable=False, default=0)   # one-time IAP credits, never expire
    week_anchor_utc = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class CreditPack(db.Model):
    """One-time purchasable credit packs shown on the subscription screen."""
    __tablename__ = "credit_packs"

    id          = db.Column(db.String(32), primary_key=True)   # e.g. "credits_small"
    name        = db.Column(db.String(64), nullable=False)     # e.g. "Small Pack"
    credits     = db.Column(db.Integer, nullable=False)        # credits granted on purchase
    price_usd   = db.Column(db.Numeric(8, 2), nullable=False)  # display price
    product_id  = db.Column(db.String(128), nullable=True)     # App Store / Play Store product ID
    sort_order  = db.Column(db.Integer, nullable=False, default=0)
    is_enabled  = db.Column(db.Boolean, nullable=False, default=True)

def _assign_trial(user):
    """Assign a 5-day Pro trial to a newly created user. No-ops if trial plan missing or user already has a subscription."""
    try:
        if UserSubscription.query.filter_by(user_id=user.id).first():
            return  # Already has a subscription record — skip
        plan = SubscriptionPlan.query.get('pro_trial_5day')
        if not plan:
            logger.warning("pro_trial_5day plan not found in DB — skipping trial for user %s", user.id)
            return
        now = datetime.utcnow()
        trial = UserSubscription(
            user_id=user.id,
            plan_id='pro_trial_5day',
            status='trial',
            start_date=now,
            end_date=now + timedelta(days=5),
            auto_renew=False,
        )
        db.session.add(trial)
        db.session.commit()
        logger.info("Assigned 5-day trial to new user %s", user.id)
        # Eagerly create the user_credits row so credits are ready when trial expires
        get_or_create_credits(user.id)
    except Exception as e:
        logger.warning("Failed to assign trial to user %s: %s", user.id, e)
        db.session.rollback()


# --- Subscription Service ---
class SubscriptionService:

    APPSTORE_SHARED_SECRET = app.config.get("APPSTORE_SHARED_SECRET")
    
    @staticmethod
    def get_user_subscription_status(user_id):
        """Get the current subscription status for a user"""
        now = datetime.utcnow()
        # Find the most recent active or unexpired trial subscription
        subscription = UserSubscription.query.filter(
            UserSubscription.user_id == user_id,
            UserSubscription.status.in_(['active', 'trial']),
            db.or_(UserSubscription.end_date == None, UserSubscription.end_date > now)
        ).order_by(UserSubscription.end_date.desc()).first()

        if not subscription:
            # Return default free tier if no active subscription
            from quota import ensure_week_current, next_reset_iso  # safe import here
            uc = ensure_week_current(user_id)

            return {
                'tier': 'free',
                'expiry_date': None,
                'is_active': False,
                'auto_renew': False,
                'payment_method': None,
                'free_credits': uc.free_credits,
                'purchased_credits': uc.purchased_credits,
                # Legacy aliases for older app versions
                'text_remaining_week': uc.free_credits,
                'image_remaining_lifetime': uc.purchased_credits,
                'next_reset_iso': next_reset_iso(user_id)
            }

        # Get the plan details
        plan = subscription.plan

        return {
            'tier': plan.id,
            'expiry_date': subscription.end_date.isoformat() if subscription.end_date else None,
            'is_active': True,
            'auto_renew': subscription.auto_renew,
            'payment_method': subscription.payment_method
        }
    
    @staticmethod
    def get_subscription_plans():
        """Get all available subscription plans"""
        plans = SubscriptionPlan.query.all()
        return [{
            'id': plan.id,
            'name': plan.name,
            'description': plan.description,
            'price': float(plan.price),
            'period': plan.period,
            'features': plan.features,  # temporary, will remove once app is using new row below
            'feature_cards': plan.feature_cards,
            'product_id': plan.product_id
        } for plan in plans]
    
    @staticmethod
    def initiate_subscription(user_id, plan_id, payment_provider=None, receipt_data=None):
        """
        Initiate a subscription purchase
        
        Args:
            user_id: The user ID
            plan_id: The subscription plan ID
            payment_provider: The payment provider (apple/google/stripe)
            receipt_data: Receipt data for app store purchases
            
        Returns:
            Dictionary with subscription details or payment URL
        """
        # Allow lookup by primary key or by product_id (store product identifier)
        plan = SubscriptionPlan.query.get(plan_id)
        if not plan:
            plan = SubscriptionPlan.query.filter_by(product_id=plan_id).first()
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        
        # Handle different payment providers
        if payment_provider in ('apple', 'google'):
            # Verify receipt with app store/google play
            if not receipt_data:
                raise ValueError("Receipt data required for app store/google play purchases")
            
            # Verify receipt (implementation depends on provider)
            if payment_provider == 'apple':
                verification_result = SubscriptionService._verify_apple_receipt(receipt_data)
            else:  # google
                verification_result = SubscriptionService._verify_google_receipt(receipt_data)
            
            if not verification_result.get('valid'):
                raise ValueError(f"Invalid receipt: {verification_result.get('message')}")
            
            # Create subscription record
            subscription = SubscriptionService._create_subscription(
                user_id=user_id,
                plan_id=plan_id,
                payment_provider=payment_provider,
                provider_subscription_id=verification_result.get('subscription_id'),
                provider_transaction_id=verification_result.get('transaction_id'),
                receipt_data=receipt_data,
                auto_renew=True
            )
            
            # Create payment record
            SubscriptionService._create_payment(
                user_id=user_id,
                subscription_id=subscription.id,
                amount=float(plan.price),
                provider=payment_provider,
                provider_transaction_id=verification_result.get('transaction_id'),
                provider_response=verification_result
            )
            
            return {'success': True}
        
        elif payment_provider == 'stripe':
            # For web payments, create a Stripe checkout session
            # This is a placeholder - you would integrate with Stripe API here
            payment_url = f"https://example.com/checkout?plan={plan_id}&user={user_id}"
            return {'payment_url': payment_url}
        
        else:
            # Default web payment flow (customize based on your payment processor)
            payment_url = f"https://example.com/checkout?plan={plan_id}&user={user_id}"
            return {'payment_url': payment_url}
    
    @staticmethod
    def cancel_subscription(user_id):
        """Cancel a user's subscription"""
        subscription = UserSubscription.query.filter_by(
            user_id=user_id, 
            status='active'
        ).order_by(UserSubscription.end_date.desc()).first()
        
        if not subscription:
            return False
        
        # Update subscription status
        subscription.status = 'canceled'
        subscription.auto_renew = False
        db.session.commit()
        
        # If using a payment provider, you might need to cancel with them too
        if subscription.payment_provider in ('apple', 'google', 'stripe'):
            # This would be implemented based on the provider's API
            pass
        
        return True
    
    @staticmethod
    def update_payment_method(user_id, payment_details):
        """Update a user's payment method"""
        subscription = UserSubscription.query.filter_by(
            user_id=user_id, 
            status='active'
        ).order_by(UserSubscription.end_date.desc()).first()
        
        if not subscription:
            return False
        
        # Update payment method
        subscription.payment_method = payment_details.get('method')
        db.session.commit()
        
        # If using a payment provider, you might need to update with them too
        if subscription.payment_provider in ('apple', 'google', 'stripe'):
            # This would be implemented based on the provider's API
            pass
        
        return True
    
    @staticmethod
    def _create_subscription(
        user_id,
        plan_id,
        payment_provider=None,
        provider_subscription_id=None,
        provider_transaction_id=None,
        receipt_data=None,
        auto_renew=True,
    ):
        plan = SubscriptionPlan.query.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found in _create_subscription")

        now = datetime.utcnow()

        # Simple end_date calculation – keep whatever logic you already have
        if plan.period == "monthly":
            end_date = now + relativedelta(months=1)
        elif plan.period == "yearly":
            end_date = now + relativedelta(years=1)
        else:
            # fallback, or raise
            end_date = now + relativedelta(months=1)

        # Identify the subscription *lineage*, not the individual purchase
        # event. provider_subscription_id is Apple's original_transaction_id
        # / Google's linked purchase token lineage — it stays constant across
        # renewals and even across plan changes (e.g. monthly -> yearly)
        # within the same subscription group. provider_transaction_id is
        # unique *per renewal* and must never be used as the row identity,
        # or every renewal mints a brand new "active" row (see reconcile
        # investigation, Aug 2026 — user 1 ended up with 14 active rows from
        # a single sandbox renewal burst).
        existing = None
        if payment_provider and provider_subscription_id:
            existing = UserSubscription.query.filter_by(
                payment_provider=payment_provider,
                provider_subscription_id=provider_subscription_id,
            ).first()
        if existing is None and payment_provider and provider_transaction_id:
            # Fall back for providers/rows that never had a subscription-level
            # id recorded (e.g. older rows, or providers without one).
            existing = UserSubscription.query.filter_by(
                payment_provider=payment_provider,
                provider_transaction_id=provider_transaction_id,
            ).first()

        if existing:
            # Idempotent update of an existing subscription row — a renewal,
            # plan change, or re-verification of the same subscription
            # lineage, never a new row.
            logger.info(
                "SubscriptionService._create_subscription: updating existing "
                f"user_sub id={existing.id} user={user_id} provider={payment_provider} "
                f"sub_id={provider_subscription_id} txn={provider_transaction_id}"
            )
            existing.plan_id = plan_id
            existing.status = "active"
            existing.start_date = existing.start_date or now
            existing.end_date = end_date
            existing.auto_renew = auto_renew
            existing.payment_method = payment_provider
            existing.provider_subscription_id = provider_subscription_id
            existing.provider_transaction_id = provider_transaction_id
            existing.receipt_data = receipt_data
            db.session.commit()
            return existing

        # Otherwise, create a new subscription row
        subscription = UserSubscription(
            user_id=user_id,
            plan_id=plan_id,
            status="active",
            start_date=now,
            end_date=end_date,
            auto_renew=auto_renew,
            payment_method=payment_provider,
            payment_provider=payment_provider,
            provider_subscription_id=provider_subscription_id,
            provider_transaction_id=provider_transaction_id,
            receipt_data=receipt_data,
        )

        try:
            db.session.add(subscription)
            db.session.commit()
            logger.info(
                "SubscriptionService._create_subscription: created new "
                f"user_sub id={subscription.id} user={user_id} provider={payment_provider} "
                f"txn={provider_transaction_id}"
            )
            return subscription
        except IntegrityError as e:
            db.session.rollback()
            logger.warning(
                "SubscriptionService._create_subscription: IntegrityError on insert, "
                "retrying as update: %s",
                e,
            )
            # Last-resort: fetch again and update, in case of race. Same
            # lineage-first lookup as above.
            if payment_provider and provider_subscription_id:
                existing = UserSubscription.query.filter_by(
                    payment_provider=payment_provider,
                    provider_subscription_id=provider_subscription_id,
                ).first()
            if existing is None and payment_provider and provider_transaction_id:
                existing = UserSubscription.query.filter_by(
                    payment_provider=payment_provider,
                    provider_transaction_id=provider_transaction_id,
                ).first()
            if existing:
                existing.plan_id = plan_id
                existing.status = "active"
                existing.start_date = existing.start_date or now
                existing.end_date = end_date
                existing.auto_renew = auto_renew
                existing.payment_method = payment_provider
                existing.provider_subscription_id = provider_subscription_id
                existing.provider_transaction_id = provider_transaction_id
                existing.receipt_data = receipt_data
                db.session.commit()
                return existing

            # If we still can't find it, re-raise so you see the error
            raise
    
    
    @staticmethod
    def _create_payment(user_id, subscription_id, amount, provider, 
                       provider_transaction_id=None, provider_response=None):
        """Create a payment record"""
        payment = PaymentTransaction(
            user_id=user_id,
            subscription_id=subscription_id,
            amount=amount,
            status='completed',
            provider=provider,
            provider_transaction_id=provider_transaction_id,
            provider_response=provider_response
        )
        
        db.session.add(payment)
        db.session.commit()
        return payment
    
    # added by AZAD, not used
    @staticmethod
    def upsert_manual_subscription(
        user_id,
        plan_id,
        months=None,
        years=None,
        expires_at=None,
        payment_provider=None,
    ):
        """Manually create or repair an active subscription for a user.

        Useful for admin repair tools when the store reports an active
        subscription but the local DB is out of sync.
        """
        plan = SubscriptionPlan.query.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")

        now = datetime.utcnow()
        if expires_at is None:
            # Default to one billing period unless months/years override it
            if months is not None:
                expires_at = now + relativedelta(months=months)
            elif years is not None:
                expires_at = now + relativedelta(years=years)
            elif plan.period == 'monthly':
                expires_at = now + relativedelta(months=1)
            elif plan.period == 'yearly':
                expires_at = now + relativedelta(years=1)
            else:
                expires_at = now + timedelta(days=30)

        # Most recent subscription for this user/plan, if any
        sub = (
            UserSubscription.query
            .filter_by(user_id=user_id, plan_id=plan_id)
            .order_by(UserSubscription.end_date.desc())
            .first()
        )

        if sub is None:
            sub = UserSubscription(
                user_id=user_id,
                plan_id=plan_id,
                status='active',
                start_date=now,
                end_date=expires_at,
                auto_renew=True,
                payment_method=payment_provider or 'manual',
                payment_provider=payment_provider or 'manual',
            )
            db.session.add(sub)
        else:
            sub.status = 'active'
            sub.end_date = expires_at
            sub.auto_renew = True
            if payment_provider:
                sub.payment_provider = payment_provider
                sub.payment_method = payment_provider

        db.session.commit()
        return sub


    @staticmethod
    def _verify_apple_receipt(receipt_data: str) -> dict:
        """
        Verify an Apple purchase.

        1a. Preferred: receipt_data is a signed StoreKit2 transaction JWS
            (serverVerificationData — purchase_service.dart was sending the
            wrong field, verificationData.localVerificationData, until Aug
            2026; fixed client-side, this is the server-side half of that
            fix). Verified cryptographically via apple_notifications'
            SignedDataVerifier, the same verifier used for Server
            Notifications V2 — Apple explicitly recommends against
            hand-rolling this.
        1b. Compat fallback: plain StoreKit2 JSON (jsonRepresentation) from
            app versions still on the old client. NOT cryptographically
            verified — we only reject it if it's expired/revoked according
            to its own embedded claims (added Aug 2026 after an old, lapsed
            transaction was replayed by StoreKit and silently granted a
            fresh billing period). Remove this path once telemetry shows no
            more "UNVERIFIED StoreKit2 JSON" log lines.
        2.  Last resort: legacy /verifyReceipt flow, base64 app receipt.
        """

        # --- Path 1a: signed StoreKit2 transaction JWS (preferred) ---
        if apple_notifications.is_jws(receipt_data):
            try:
                tx = apple_notifications.verify_transaction_jws(app, receipt_data)
            except apple_notifications.AppleNotificationConfig as e:
                logger.error("Apple verify: JWS path not configured: %s", e)
                return {"valid": False, "message": "Server not configured to verify Apple receipts"}
            except AppleVerificationException as e:
                logger.warning("Apple verify: signed transaction failed verification: status=%s", e.status)
                return {"valid": False, "message": "Receipt signature verification failed"}

            if tx.revocationDate:
                logger.warning(
                    "Apple verify: verified transaction %s has revocationDate=%s, rejecting",
                    tx.transactionId, tx.revocationDate,
                )
                return {"valid": False, "message": "Transaction was refunded/revoked by Apple"}

            expiry_iso = None
            if tx.expiresDate:
                expiry_dt = datetime.utcfromtimestamp(tx.expiresDate / 1000.0)
                expiry_iso = expiry_dt.isoformat() + "Z"
                if expiry_dt <= datetime.utcnow():
                    logger.warning(
                        "Apple verify: verified transaction %s expired at %s, "
                        "rejecting stale/replayed receipt",
                        tx.transactionId, expiry_iso,
                    )
                    return {"valid": False, "message": f"Transaction expired at {expiry_iso}"}

            logger.info(
                "Apple verify: cryptographically verified transaction id=%s orig=%s product=%s",
                tx.transactionId, tx.originalTransactionId, tx.productId,
            )
            return {
                "valid": True,
                "subscription_id": tx.originalTransactionId,
                "transaction_id": tx.transactionId,
                "expiry_date": expiry_iso,
                "raw": {
                    "transactionId": tx.transactionId,
                    "originalTransactionId": tx.originalTransactionId,
                    "productId": tx.productId,
                },
            }

        # --- Path 1b: unsigned StoreKit2 JSON (legacy client compat only) ---
        try:
            tx = json.loads(receipt_data)
            if isinstance(tx, dict) and "transactionId" in tx:
                logger.warning(
                    "Apple verify: UNVERIFIED StoreKit2 JSON from client (old app "
                    "build?) txn=%s — not cryptographically checked, only "
                    "expiry/revocation-checked", tx.get("transactionId"),
                )

                transaction_id = tx.get("transactionId")
                original_transaction_id = (
                    tx.get("originalTransactionId") or transaction_id
                )

                # Reject refunded/revoked transactions outright.
                revocation_ms = tx.get("revocationDate")
                if revocation_ms:
                    logger.warning(
                        "Apple verify: transaction %s has revocationDate=%s, rejecting",
                        transaction_id, revocation_ms,
                    )
                    return {
                        "valid": False,
                        "message": "Transaction was refunded/revoked by Apple",
                    }

                # expiresDate is in ms since epoch, optional
                expires_ms = tx.get("expiresDate")
                expiry_iso = None
                if expires_ms:
                    try:
                        ms = int(expires_ms)
                        expiry_dt = datetime.utcfromtimestamp(ms / 1000.0)
                        expiry_iso = expiry_dt.isoformat() + "Z"
                    except Exception as e:
                        logger.warning(f"Could not parse expiresDate={expires_ms}: {e}")
                    else:
                        # Reject stale/already-lapsed transactions. This is
                        # what catches StoreKit re-delivering an old,
                        # unfinished transaction instead of a fresh
                        # purchase -- the exact failure mode from the Aug
                        # 2026 incident (transaction expired Jan 2026,
                        # re-signed and re-sent by the client in Aug 2026).
                        if expiry_dt <= datetime.utcnow():
                            logger.warning(
                                "Apple verify: transaction %s expired at %s, "
                                "rejecting stale/replayed receipt",
                                transaction_id, expiry_iso,
                            )
                            return {
                                "valid": False,
                                "message": f"Transaction expired at {expiry_iso}",
                            }

                return {
                    "valid": True,
                    "subscription_id": original_transaction_id,
                    "transaction_id": transaction_id,
                    "expiry_date": expiry_iso,
                    "raw": tx,
                }
        except Exception as e:
            # Not JSON or not the expected shape – fall back to legacy logic
            logger.info(f"Apple verify: receipt_data is not StoreKit2 JSON: {e}")

        # --- Path 2: legacy /verifyReceipt base64 receipt (only if above fails) ---

        shared_secret = SubscriptionService.APPSTORE_SHARED_SECRET
        if not shared_secret:
            logger.error("APPSTORE_SHARED_SECRET is not configured")
            return {"valid": False, "message": "Missing shared secret"}

        def call_apple(url: str) -> dict:
            payload = {
                "receipt-data": receipt_data,
                "password": shared_secret,
                "exclude-old-transactions": True,
            }
            r = requests.post(url, json=payload, timeout=10)
            try:
                return r.json()
            except Exception:
                logger.error(f"Apple verifyReceipt non-JSON response: {r.text}")
                return {"status": -1, "message": "non-JSON response"}

        prod_url = "https://buy.itunes.apple.com/verifyReceipt"
        sandbox_url = "https://sandbox.itunes.apple.com/verifyReceipt"

        result = call_apple(prod_url)
        status = result.get("status")

        if status == 21007:
            logger.info("Apple verifyReceipt 21007 → retrying sandbox endpoint")
            result = call_apple(sandbox_url)
            status = result.get("status")

        if status != 0:
            return {
                "valid": False,
                "message": f"Apple verifyReceipt status={status}",
            }

        latest_info = None
        info_list = result.get("latest_receipt_info")
        if isinstance(info_list, list) and info_list:
            latest_info = info_list[-1]
        elif isinstance(info_list, dict):
            latest_info = info_list

        if not latest_info:
            return {
                "valid": False,
                "message": "Apple verifyReceipt: missing latest_receipt_info",
            }

        sub_id = (
            latest_info.get("original_transaction_id")
            or latest_info.get("transaction_id")
        )
        txn_id = latest_info.get("transaction_id")

        expires_ms = latest_info.get("expires_date_ms")
        expiry_iso = None
        if expires_ms:
            try:
                ms = int(expires_ms)
                expiry_iso = datetime.utcfromtimestamp(ms / 1000.0).isoformat() + "Z"
            except Exception as e:
                logger.warning(f"Could not parse expires_date_ms={expires_ms}: {e}")

        return {
            "valid": True,
            "subscription_id": sub_id,
            "transaction_id": txn_id,
            "expiry_date": expiry_iso,
            "raw": latest_info,
        }
    

    # Android
    def _get_android_publisher_client():
        creds = service_account.Credentials.from_service_account_file(
            app.config["GOOGLE_SERVICE_ACCOUNT_JSON"],
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _verify_google_receipt(receipt_data: str) -> dict:
        """
        Verify a Google Play subscription using purchases.subscriptionsv2.get.
        receipt_data: purchase token from the device (serverVerificationData on Android).
        Returns {valid: bool, subscription_id, transaction_id, product_id, expiry_date?, message?}
        """

        try:
            android_publisher = _get_android_publisher_client()
            package_name = app.config["GOOGLE_PACKAGE_NAME"]

            # v2 endpoint – recommended for modern subs.:contentReference[oaicite:5]{index=5}
            resp = (
                android_publisher
                .purchases()
                .subscriptionsv2()
                .get(
                    packageName=package_name,
                    token=receipt_data,
                )
                .execute()
            )

            # SubscriptionPurchaseV2 structure:
            # - subscriptionState: "SUBSCRIPTION_STATE_ACTIVE", etc.
            # - latestOrderId: the most recent order ID
            # - lineItems[0].productId, lineItems[0].expiryTime, etc.:contentReference[oaicite:6]{index=6}
            state = resp.get("subscriptionState")
            line_items = resp.get("lineItems") or []
            line = line_items[0] if line_items else {}
            product_id = line.get("productId")
            expiry_time = line.get("expiryTime")  # e.g. "2025-01-15T10:00:00Z"

            if state != "SUBSCRIPTION_STATE_ACTIVE":
                return {
                    "valid": False,
                    "product_id": product_id,
                    "message": f"Google subscriptionState={state}",
                }

            latest_order_id = resp.get("latestOrderId")

            return {
                "valid": True,
                "subscription_id": latest_order_id,
                "transaction_id": latest_order_id,
                "product_id": product_id,
                "expiry_date": expiry_time,
            }

        except HttpError as e:
            return {
                "valid": False,
                "message": f"Google API error: {e}",
            }
        except Exception as e:
            return {
                "valid": False,
                "message": f"Unexpected Google verification error: {e}",
            }


    def process_subscription_renewals():
        """
        Process subscription renewals and expirations
        """
        # Get all active subscriptions that are due for renewal
        subscriptions = UserSubscription.query.filter(
            UserSubscription.status == 'active',
            UserSubscription.end_date <= datetime.utcnow() + timedelta(days=1),
            UserSubscription.auto_renew == True
        ).all()
        for subscription in subscriptions:
            # Process renewal based on payment provider
            if subscription.payment_provider == 'apple':
                # Verify subscription status with Apple
                pass
            elif subscription.payment_provider == 'google':
                # Verify subscription status with Google
                pass
            elif subscription.payment_provider == 'stripe':
                # Process renewal with Stripe
                pass

# new!
class Interpreter(db.Model):
    __tablename__ = "interpreters"
    __table_args__ = (UniqueConstraint("slug", name="uq_interpreters_slug"),)

    id = db.Column(db.Integer, primary_key=True)

    # stable key used by app/backend (e.g. "warm_storyteller")
    slug = db.Column(db.String(64), nullable=False)

    name = db.Column(db.String(120), nullable=False)
    alias = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(32), nullable=False, default="grounded")
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # access control
    access_tier = db.Column(db.String(16), nullable=False, default="pro")  # "free" | "pro"
    unlock_rule = db.Column(MySQLJSON, nullable=True)  # optional: {"type":"streak_days","value":7}

    # persona prompt fields
    core_voice = db.Column(db.Text, nullable=False)
    interpretive_lens = db.Column(db.Text, nullable=False)
    emotional_stance = db.Column(db.Text, nullable=False)
    prompt_extra = db.Column(db.Text, nullable=True)  # optional per-persona extra constraints

    # UI card fields
    card_blurb = db.Column(db.String(255), nullable=False, default="")
    card_bullets = db.Column(MySQLJSON, nullable=False, default=list)      # ["...", "..."]
    tone_examples = db.Column(MySQLJSON, nullable=False, default=list)     # ["...", "..."]

    # icon metadata
    icon_key = db.Column(db.String(64), nullable=False, default="")
    icon_file = db.Column(db.String(128), nullable=True)            # e.g. "abc123.png"
    animated_icon_file = db.Column(db.String(128), nullable=True)   # e.g. "abc123.mp4"
    # icon_tile_file = db.Column(db.String(128), nullable=True)  # e.g. "abc123.png"
    icon_prompt = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

# End Classes

# Notes
NOTES_MAX_LEN = 8000
NOTES_AI_ENABLED = False
REANALYZE_WITH_NOTES_ALLOWED = False
NOTES_POLICY_VERSION = "v1"

# pro
def _user_is_pro(user_id: int) -> bool:
    try:
        st = SubscriptionService.get_user_subscription_status(user_id)
        tier = (st.get("tier") or "").lower()
        active = st.get("is_active") is True
        return active and (tier.startswith("pro") or tier.startswith("trial"))
    except Exception:
        return False

# Checks if user can generate images (pro or has free credits)
def _can_generate_image(user_id: int) -> bool:
    # Check if pro user
    if _user_is_pro(user_id):
        return True
    
    # Check if free user with remaining image credits
    try:
        credits = get_or_create_credits(user_id)
        return (credits.free_credits + credits.purchased_credits) >= IMAGE_CREDIT_COST
    except Exception:
        return False

from functools import wraps
from flask import jsonify

def requires_pro(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not _user_is_pro(current_user.id):
            return jsonify({"error": "pro_required"}), 402
        return fn(*args, **kwargs)
    return _wrap

def _iso_utc(dt: datetime | None) -> str | None:
    if not dt:
        return None
    # store naive UTC in DB; emit ISO with Z for API consistency
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

def _notes_conflict(d: Dream, last_seen: str | None) -> bool:
    """True if client-supplied last_seen doesn't match current server timestamp."""
    if not last_seen:
        return False
    cur = _iso_utc(d.notes_updated_at)
    return cur is not None and last_seen.strip() != cur.strip()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

RESET_TTL_MINUTES = 60  # tweak as you like

def _generate_raw_token() -> str:
    return secrets.token_urlsafe(32)

def _password_policy_ok(pw: str) -> bool:
    return isinstance(pw, str) and len(pw) >= 8  # expand if needed
  
# class DreamTone(Enum):
#     PEACEFUL = "Peaceful / gentle"
#     EPIC = "Epic / heroic"
#     WHIMSICAL = "Whimsical / surreal"
#     NIGHTMARISH = "Nightmarish / dark"
#     ROMANTIC = "Romantic / nostalgic"
#     ANCIENT = "Ancient / mythic"
#     FUTURISTIC = "Futuristic / uncanny"
#     ELEGANT = "Elegant / ornate"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))



# Sends confirmation email (new)
def send_confirmation_email(recipient_email, token):
    base = app.config.get("CONFIRM_LINK_BASE", "https://dreamr-us-west-01.zentha.me/confirm")
    confirm_url = f"{base}?token={token}"
    msg = Message(
        subject="Confirm your Dreamr✨account",
        recipients=[recipient_email],
        body=(
            "Welcome to Dreamr!\n\n"
            "Click the link below to confirm your account:\n\n"
            f"{confirm_url}\n\n"
            "After confirming, open the Dreamr app and sign in.\n"
            "If you didn't sign up for Dreamr, ignore this message."
        )
    )
    mail.send(msg)


# Profile pic stuff
UPLOAD_FOLDER = '/data/dreamr-frontend/static/avatars'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# --- Admin helpers ---
# def is_admin_user():
#     return current_user.is_authenticated and (current_user.email or "").lower() in ADMIN_EMAILS

def _get_admin_emails():
    cfg = (current_app.config.get("ADMIN_EMAILS")
           or os.getenv("ADMIN_EMAILS")
           or "")
    # allow comma or semicolon
    parts = cfg.replace(";", ",").split(",")
    return {p.strip().lower() for p in parts if p.strip()}

def is_admin_user():
    return current_user.is_authenticated and (current_user.email or "").lower() in _get_admin_emails()
    

def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    @login_required
    def _wrap(*a, **k):
        if not is_admin_user():
            abort(403)
        return fn(*a, **k)
    return _wrap



# ROUTES
# for fetching all file names (not images) to display on landing page
@app.route("/api/images", methods=["GET"])
def get_images():
    IMAGE_DIR = "/data/dreamr-frontend/static/images/dreams"
    files = os.listdir(IMAGE_DIR)
    return jsonify([f for f in files if f.endswith(".png")])


# profile update page
@app.route('/api/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = current_user

    if request.method == 'GET':
        return jsonify({
            'email': user.email,
            'first_name': user.first_name,
            'birthdate': user.birthdate.isoformat() if user.birthdate else '',
            'gender': user.gender,
            'timezone': user.timezone,
            'avatar_url': f'/static/avatars/{user.avatar_filename}' if user.avatar_filename else '',
            'enable_audio': user.enable_audio
        })

    # POST: update profile
    data = request.form
    file = request.files.get('avatar')

    if 'email' in data:
        user.email = data['email']
    if 'first_name' in data:
        user.first_name = data['first_name']
    if 'birthdate' in data:
        try:
            user.birthdate = datetime.strptime(data['birthdate'], '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'Invalid birthdate format'}), 400
    if 'gender' in data:
        user.gender = data['gender']
    if 'timezone' in data:
        user.timezone = data['timezone']

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        file.save(filepath)
        user.avatar_filename = filename

    if 'enable_audio' in data:
      user.enable_audio = data['enable_audio'].lower() in ['true', '1', 'yes']

    db.session.commit()
    # return jsonify({'success': True})
    return jsonify({
      'first_name': user.first_name
  })


# for google logins via web app
@app.route('/login/google')
def login_google():
    redirect_uri = url_for('auth_google', _external=True)
    return google.authorize_redirect(redirect_uri)

# for google logins via web app
@app.route('/auth/google')
def auth_google():
    token = google.authorize_access_token()
    resp = google.get('userinfo')
    user_info = resp.json()

    email = user_info['email']
    full_name = user_info.get("name", "")
    name = full_name.split()[0] if full_name else ""

    # Check if user exists
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            first_name=name or "Unknown",
            password='',
            timezone='',
            email_confirmed=True,
        )
        db.session.add(user)
        db.session.commit()
        _assign_trial(user)
    elif not user.email_confirmed:
        user.email_confirmed = True
        db.session.commit()

    # login_user(user)
    login_user(user, remember=True, duration=timedelta(days=90))
    # return redirect("/dashboard?confirmed=1")
    return redirect("/dashboard")



# for google logins via mobile app
@app.route('/api/google_login', methods=['POST'])
def api_google_login():
    data = request.get_json(silent=True) or {}
    token = data.get('id_token')
    if not token:
        return jsonify({"error": "missing id_token"}), 400

    try:
        req = google_requests.Request()
        idinfo = id_token.verify_oauth2_token(token, req, audience=None)

        aud = idinfo.get("aud")
        azp = idinfo.get("azp")
        iss = idinfo.get("iss")
        email = (idinfo.get("email") or "").strip().lower()
        email_verified = idinfo.get("email_verified", False)
        full_name = idinfo.get("name") or ""
        first_name = full_name.split()[0] if full_name else "Unknown"

        if iss not in ALLOWED_ISS:
            raise ValueError(f"bad iss: {iss}")

        if aud not in ALLOWED_AUDS:
            if azp not in ALLOWED_AUDS:
                raise ValueError(f"bad aud: {aud} azp: {azp}")

        if not email or not email_verified:
            raise ValueError("email not verified")

        # ------------ NORMAL / REACTIVATION FLOW ------------

        # 1) Try real email first (case-insensitive)
        user = User.query.filter(func.lower(User.email) == email).first()

        if not user:
            # 2) Try deleted/reactivated user: email stored as hash
            email_hash = hash_string_secret(email)
            deleted = User.query.filter(User.email == email_hash).first()

            if deleted:
                # Reactivate this user in place, keep same id & credits
                deleted.email = email
                deleted.first_name = first_name or deleted.first_name
                deleted.email_confirmed = True
                # Optionally clear deletion flags if you have them:
                # deleted.deleted_at = None
                # deleted.status = "active"

                db.session.commit()
                user = deleted
            else:
                # 3) Truly new user – create fresh
                user = User(
                    email=email,
                    first_name=first_name,
                    password='',
                    timezone='',
                    email_confirmed=True,
                )
                db.session.add(user)
                db.session.commit()
                _assign_trial(user)
                logger.info("✅ Registered new user (Google): %s", email)

        else:
            # Existing normal user: ensure confirmed
            if not user.email_confirmed:
                user.email_confirmed = True
                db.session.commit()

        # Log them in (whether new, existing, or reactivated)
        login_user(user)
        return jsonify({"success": True})

    except Exception as e:
        try:
            logger.info(
                "google login failed: aud=%s azp=%s iss=%s email=%s err=%s",
                idinfo.get("aud") if 'idinfo' in locals() else None,
                idinfo.get("azp") if 'idinfo' in locals() else None,
                idinfo.get("iss") if 'idinfo' in locals() else None,
                idinfo.get("email") if 'idinfo' in locals() else None,
                e,
            )
        except Exception:
            logger.info("google login failed: %s", e)
        return jsonify({"error": "Invalid token, naughty!"}), 400


# for facebook logins via mobile app
@app.route('/api/facebook_login', methods=['POST'])
def api_facebook_login():
    data = request.get_json(silent=True) or {}
    access_token = data.get('access_token')
    if not access_token:
        return jsonify({"error": "missing access_token"}), 400

    try:
        # Verify the token and get user info from Facebook Graph API
        import urllib.request as _urllib_req
        import json as _json

        graph_url = (
            f"https://graph.facebook.com/me"
            f"?fields=id,name,email"
            f"&access_token={access_token}"
        )
        with _urllib_req.urlopen(graph_url, timeout=10) as resp:
            fb_data = _json.loads(resp.read())

        fb_id    = fb_data.get("id")
        email    = (fb_data.get("email") or "").strip().lower()
        full_name = fb_data.get("name") or ""
        first_name = full_name.split()[0] if full_name else "Unknown"

        if not fb_id:
            raise ValueError("Facebook did not return a user id")

        # If Facebook didn't share the email, use a stable placeholder
        if not email:
            email = f"fb_{fb_id}@facebook.placeholder"

        # 1) Try existing user by email
        user = User.query.filter(func.lower(User.email) == email).first()

        if not user:
            # 2) Try deleted/reactivated user: email stored as hash
            email_hash = hash_string_secret(email)
            deleted = User.query.filter(User.email == email_hash).first()

            if deleted:
                deleted.email = email
                deleted.first_name = first_name or deleted.first_name
                deleted.email_confirmed = True
                db.session.commit()
                user = deleted
            else:
                # 3) Truly new user
                user = User(
                    email=email,
                    first_name=first_name,
                    password='',
                    timezone='',
                    email_confirmed=True,
                )
                db.session.add(user)
                db.session.commit()
                _assign_trial(user)
                logger.info("✅ Registered new user (Facebook): %s", email)
        else:
            if not user.email_confirmed:
                user.email_confirmed = True
                db.session.commit()

        login_user(user)
        return jsonify({"success": True, "user": {"id": user.id}})

    except Exception as e:
        logger.info("facebook login failed: %s", e)
        return jsonify({"error": "Facebook login failed"}), 400


# helper for Apple verification
def verify_apple_identity_token(identity_token: str) -> dict:
    """Verify an Apple Sign in with Apple identity token (JWT) and return its claims."""
    if not APPLE_CLIENT_ID:
        raise RuntimeError(
            "APPLE_CLIENT_ID or APPLE_BUNDLE_ID must be configured on the server"
        )

    try:
        # Get the signing key for this token from Apple's JWKS
        signing_key = _apple_jwk_client.get_signing_key_from_jwt(identity_token)
    except Exception as e:
        logger.exception("Failed to get Apple signing key from JWKS: %s", e)
        raise

    try:
        claims = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_CLIENT_ID,
            issuer=APPLE_ISSUER,
        )
        # Log the good case at info level once while testing
        # logger.info(
        #     "Apple token verified: iss=%s aud=%s sub=%s email=%s exp=%s",
        #     claims.get("iss"),
        #     claims.get("aud"),
        #     claims.get("sub"),
        #     claims.get("email"),
        #     claims.get("exp"),
        # )
        return claims

    except InvalidTokenError as e:
        # Try decoding without verification just so we can inspect claims.
        try:
            raw_claims = jwt.decode(
                identity_token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                },
            )
            # logger.warning(
            #     "Apple token failed verification (%s). Raw claims: iss=%s aud=%s sub=%s email=%s exp=%s",
            #     e,
            #     raw_claims.get("iss"),
            #     raw_claims.get("aud"),
            #     raw_claims.get("sub"),
            #     raw_claims.get("email"),
            #     raw_claims.get("exp"),
            # )
        except Exception as inner:
            logger.warning(
                "Apple token failed verification (%s) and could not decode raw claims: %s",
                e,
                inner,
            )
        raise

    except Exception as e:
        logger.exception("Unexpected error verifying Apple identity token: %s", e)
        raise



# Apple Logins on IOS
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

@app.route("/api/apple_login", methods=["POST"])
def apple_login():
    data = request.get_json(silent=True) or {}
    identity_token = data.get("identity_token")
    authorization_code = data.get("authorization_code")  # currently unused
    user_identifier = data.get("user_identifier")
    email = data.get("email")
    full_name = data.get("full_name")
    first_name = full_name.split()[0] if full_name else ""

    if not identity_token:
        return jsonify({"error": "Missing identity token"}), 400

    # Verify the Apple identity token (signature + iss/aud/exp).
    try:
        claims = verify_apple_identity_token(identity_token)
    except (InvalidTokenError, RuntimeError) as e:
        logger.info("Apple login failed token check: %s", e)
        return jsonify({"error": "Invalid Apple identity token"}), 400
    except Exception:
        logger.exception("Apple login unexpected error while verifying token")
        return jsonify({"error": "Apple identity token verification failed"}), 400

    token_sub = claims.get("sub")
    if not token_sub:
        return jsonify({"error": "Apple identity token missing subject"}), 400

    # If the client also sent a user_identifier, make sure it matches the token.
    if user_identifier and user_identifier != token_sub:
        return jsonify({"error": "Apple user id mismatch"}), 400

    apple_user_id = token_sub

    email_from_token = claims.get("email")
    email_verified = claims.get("email_verified")
    if isinstance(email_verified, str):
        email_verified = email_verified.lower() == "true"

    # Prefer the (verified) email from the token, but fall back to payload.
    if email_from_token and (email_verified is True or email_verified is None):
        email = email_from_token or email

    # Normalize email
    email = (email or "").strip().lower()

    # ---- USER LOOKUP / REACTIVATION FLOW ----

    user = None

    # 1) Try by apple_user_id first
    if apple_user_id:
        user = User.query.filter_by(apple_user_id=apple_user_id).first()

        # If this user has a hashed email matching current email, restore it
        if user and email:
            email_hash = hash_string_secret(email)
            if user.email == email_hash:
                user.email = email
                user.first_name = first_name or user.first_name
                user.email_confirmed = True

    # 2) If no user yet, try by real email (case-insensitive)
    if not user and email:
        user = User.query.filter(func.lower(User.email) == email).first()

    # 3) If still no user, try “deleted” user where email == hash(email)
    if not user and email:
        email_hash = hash_string_secret(email)
        deleted = User.query.filter(User.email == email_hash).first()
        if deleted:
            logger.info("Reactivating deleted user via Apple login: %s", email)
            if first_name:
                deleted.first_name = first_name
            else:
                if deleted.first_name.startswith("Deleted-"):
                    deleted.first_name = deleted.first_name[len("Deleted-"):]
            deleted.email = email
            deleted.email_confirmed = True
            if not deleted.apple_user_id:
                deleted.apple_user_id = apple_user_id
            user = deleted

    # 4) If still no user, create new one
    if not user:
        if not email:
            # Apple didn’t provide an email we can trust; we can’t create an account.
            return jsonify({
                "error": (
                    "Apple did not provide an email address, try clearing your "
                    "saved password for Dreamr in Settings / your account / "
                    "Sign in with Apple, then try again."
                )
            }), 400

        user = User(
            apple_user_id=apple_user_id,
            email=email,
            first_name=first_name,
            password='',
            timezone='',
            email_confirmed=True,
        )
        db.session.add(user)
        is_new_user = True
    else:
        # If we found user by email, make sure apple_user_id is attached
        if apple_user_id and not user.apple_user_id:
            user.apple_user_id = apple_user_id
        # Make sure they’re marked confirmed
        if not user.email_confirmed:
            user.email_confirmed = True
        is_new_user = False

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Account conflict, please contact support"}), 400

    if is_new_user:
        _assign_trial(user)
        logger.info("✅ Registered new user (Apple): %s", email)

    # 5) Log the user in
    login_user(user)
    return jsonify({"success": True}), 200



# New user registration
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    first_name = (data.get("first_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    timezone_val = data.get("timezone")
    password = data.get("password") or ""

    logger.info(f"📨 Registration attempt: {email}")

    EMAIL_REGEX = re.compile(r"^[^@]+@[^@]+\.[^@]+$")

    if not first_name or len(first_name) > 50:
        logger.warning("❌ Invalid name")
        return jsonify({"error": "Name must be 1–50 characters"}), 400

    if not password or len(password) < 8:
        logger.warning("❌ Invalid password")
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    if not email or not EMAIL_REGEX.match(email):
        logger.warning("❌ Invalid email")
        return jsonify({"error": "Invalid email address"}), 400

    # Check for duplicates in users (case-insensitive)
    existing = User.query.filter(func.lower(User.email) == email).first()
    if existing:
        logger.warning("⚠️ Duplicate user registration attempt")
        return jsonify({"error": "User already exists"}), 400

    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    # Check for deleted (hashed) users (case-insensitive)
    email_hash = hash_string_secret(email)
    deleted = User.query.filter(func.lower(User.email) == email_hash).first()
    if deleted:
        logger.warning("⚠️ Deleted user registration attempt")
        
        # re-enable the deleted user
        deleted.email = email
        deleted.password = hashed
        deleted.first_name = first_name
        deleted.timezone = timezone_val
        deleted.signup_date = datetime.utcnow()
        db.session.commit()
        db.session.flush()  # ensure user.id is populated

        logger.warning("⚠️ Deleted user registration successful")
        
        return jsonify({
            "message": "Welcome back to your Dreamr✨account"
        })
            
    # Create real user immediately
    user = User(
        email=email,
        password=hashed,
        first_name=first_name,
        timezone=timezone_val,
        signup_date=datetime.utcnow(),
    )

    db.session.add(user)
    db.session.flush()  # ensure user.id is populated
    _assign_trial(user)

    # Create a confirmation token, but do not gate access on it
    try:
        raw_token = _generate_raw_token()
        ect = EmailConfirmToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db.session.add(ect)
        db.session.commit()

        logger.info(f"✅ Registered new user (manual): {email}")
        send_confirmation_email(email, raw_token)
    except Exception:
        # Do not block registration/log-in if email sending fails
        logger.exception("Failed to create/send confirmation token")
        db.session.commit()

    # Log the user in immediately so the app can start using the session
    try:
        login_user(user, remember=True, duration=timedelta(days=90))
    except Exception:
        logger.exception("Failed to log in user immediately after registration")

    return jsonify({
        "message": "Please check your email to confirm your Dreamr✨account"
    })


# Request Password Reset (no user enumeration)
@app.route("/api/request_password_reset", methods=["POST"])
def api_request_password_reset():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    # Always say 200
    user = User.query.filter(func.lower(User.email) == email).first()
    if user:
        # invalidate outstanding tokens
        PasswordResetToken.query.filter_by(user_id=user.id, used_at=None)\
            .update({PasswordResetToken.used_at: datetime.utcnow()})
        db.session.flush()

        raw = _generate_raw_token()
        prt = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw),
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=RESET_TTL_MINUTES),
            request_ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent", "")[:250],
        )
        db.session.add(prt)
        db.session.commit()

        try:
            base = app.config.get("RESET_LINK_BASE", "https://dreamr.zentha.me/reset")
            link = f"{base}?token={raw}"
            msg = Message(
                subject="Reset your Dreamr password",
                recipients=[user.email],
                body=(
                    "We received a request to reset your password.\n\n"
                    f"Open this link to set a new password (expires in {RESET_TTL_MINUTES} minutes):\n{link}\n\n"
                    "If you didn’t request this, ignore this email."
                ),
            )
            mail.send(msg)
        except Exception:
            logger.exception("Failed to send reset email") 

    return jsonify({"message": "If that email exists, a reset link was sent."}), 200


# Confirmation (new)
@app.route("/confirm", methods=["GET"])
def confirm_page():
    """Finalize account via token and show a simple message."""
    raw = request.args.get("token", "", type=str)
    if not raw:
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="invalid"), 400

    # Primary path: new-style email confirmation tokens
    h = _hash_token(raw)
    ect = EmailConfirmToken.query.filter_by(token_hash=h).first()
    if ect:
        if ect.expires_at < datetime.utcnow():
            return render_template_string(CONFIRM_PAGE_TEMPLATE, status="expired"), 410

        user = ect.user or User.query.get(ect.user_id)
        if not user:
            return render_template_string(CONFIRM_PAGE_TEMPLATE, status="invalid"), 400

        if ect.used_at is not None or user.email_confirmed:
            return render_template_string(CONFIRM_PAGE_TEMPLATE, status="exists"), 200

        user.email_confirmed = True
        ect.used_at = datetime.utcnow()
        db.session.commit()
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="ok"), 200

    # Legacy fallback for older PendingUser-based links
    pending = PendingUser.query.filter_by(uuid=raw).first()
    if not pending:
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="invalid"), 400

    if pending.expires_at and pending.expires_at < datetime.utcnow():
        db.session.delete(pending)
        db.session.commit()
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="expired"), 410

    existing = User.query.filter_by(email=pending.email).first()
    if existing:
        db.session.delete(pending)
        db.session.commit()
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="exists"), 200

    new_user = User(
        email=pending.email,
        password=pending.password,
        first_name=pending.first_name,
        timezone=pending.timezone,
        signup_date=datetime.utcnow(),
        email_confirmed=True,
    )
    db.session.add(new_user)
    db.session.delete(pending)
    db.session.commit()
    _assign_trial(new_user)

    return render_template_string(CONFIRM_PAGE_TEMPLATE, status="ok"), 200


@app.route("/reset", methods=["GET", "POST"])
def reset_page():
    """
    Temporary web UI for password reset.
    GET: show form if token valid, else show 'expired/invalid'.
    POST: set new password and show 'success—open the app'.
    """
    if request.method == "GET":
        raw = request.args.get("token", "", type=str)
        if not raw:
            return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400
        h = _hash_token(raw)
        prt = PasswordResetToken.query.filter_by(token_hash=h).first()
        if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
            return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400
        return render_template_string(RESET_PAGE_TEMPLATE, token=raw, invalid=False, done=False)

    # POST
    raw = request.form.get("token", "")
    pw1 = request.form.get("pw1", "")
    pw2 = request.form.get("pw2", "")
    if not raw or not pw1 or not pw2:
        return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400
    if pw1 != pw2:
        return render_template_string(RESET_PAGE_TEMPLATE, token=raw, error="Passwords do not match"), 400
    if len(pw1) < 8:
        return render_template_string(RESET_PAGE_TEMPLATE, token=raw, error="Use at least 8 characters"), 400

    h = _hash_token(raw)
    prt = PasswordResetToken.query.filter_by(token_hash=h).first()
    if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
        return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400

    user = prt.user
    user.password = bcrypt.hashpw(pw1.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    prt.used_at = datetime.utcnow()
    db.session.commit()

    # (Optional) invalidate other sessions here.

    return render_template_string(RESET_PAGE_TEMPLATE, done=True)


# Password Reset with token
@app.route("/api/reset_password", methods=["POST"])
def api_reset_password():
    data = request.get_json(silent=True) or {}
    raw = data.get("token") or ""
    new_pw = data.get("new_password") or ""
    if not raw or not new_pw:
        return jsonify({"error": "token and new_password required"}), 400
    if not _password_policy_ok(new_pw):
        return jsonify({"error": "Password does not meet policy"}), 400

    h = _hash_token(raw)
    prt = PasswordResetToken.query.filter_by(token_hash=h).first()
    if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
        return jsonify({"error": "Invalid or expired token"}), 400

    user = prt.user
    # set bcrypt hash
    user.password = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    prt.used_at = datetime.utcnow()

    # optional: invalidate other sessions here

    db.session.commit()
    return jsonify({"message": "Password updated"}), 200


# Change Password (logged in)
@app.route("/api/change_password", methods=["POST"])
@login_required
def api_change_password():
    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""
    if not new_pw:
        return jsonify({"error": "new_password required"}), 400
    if not _password_policy_ok(new_pw):
        return jsonify({"error": "Password does not meet policy"}), 400

    user = current_user  # User

    # If the account was created via Google and has no local password yet
    if not user.password or user.password == "":
        # allow setting initial local password without current_pw
        user.password = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        db.session.commit()
        return jsonify({"message": "Password set"}), 200

    # Normal flow: verify current
    ok = False
    try:
        ok = bcrypt.checkpw(current_pw.encode('utf-8'), user.password.encode('utf-8'))
    except Exception:
        ok = False
    if not ok:
        return jsonify({"error": "Current password incorrect"}), 401

    # Disallow reusing same password
    if bcrypt.checkpw(new_pw.encode('utf-8'), user.password.encode('utf-8')):
        return jsonify({"error": "New password must be different"}), 400

    user.password = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    # optional: invalidate other sessions here

    db.session.commit()
    return jsonify({"message": "Password changed"}), 200


# Confirmation (for old web-app)
@app.route("/api/confirm/<token>", methods=["GET"])
def confirm_account(token):
    """JSON confirmation endpoint; does not gate access, just flips a flag."""
    raw = token or ""
    if not raw:
        return jsonify({"error": "Invalid or expired confirmation link."}), 404

    # Primary path: new-style confirmation tokens
    h = _hash_token(raw)
    ect = EmailConfirmToken.query.filter_by(token_hash=h).first()
    if ect:
        if ect.expires_at < datetime.utcnow():
            return jsonify({"error": "Confirmation link has expired."}), 410

        user = ect.user or User.query.get(ect.user_id)
        if not user:
            return jsonify({"error": "Invalid or expired confirmation link."}), 404

        if ect.used_at is not None or user.email_confirmed:
            return jsonify({"message": "Account already confirmed."}), 200

        user.email_confirmed = True
        ect.used_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"message": "Account confirmed."}), 200

    # Legacy fallback for older PendingUser-based links
    pending = PendingUser.query.filter_by(uuid=raw).first()
    if not pending:
        return jsonify({"error": "Invalid or expired confirmation link."}), 404

    if pending.expires_at and pending.expires_at < datetime.utcnow():
        db.session.delete(pending)
        db.session.commit()
        return jsonify({"error": "Confirmation link has expired."}), 410

    existing = User.query.filter_by(email=pending.email).first()
    if existing:
        db.session.delete(pending)
        db.session.commit()
        return jsonify({"message": "Account already confirmed."}), 200

    new_user = User(
        email=pending.email,
        password=pending.password,
        first_name=pending.first_name,
        timezone=pending.timezone,
        signup_date=datetime.utcnow(),
        email_confirmed=True,
    )

    db.session.add(new_user)
    db.session.delete(pending)
    db.session.commit()
    _assign_trial(new_user)

    login_user(new_user, remember=True)
    return jsonify({"message": "Logged in"})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "invalid_request", "message": "Email and password are required."}), 400

    user = User.query.filter_by(email=email).first()

    # Do not reveal whether the email exists (prevents account enumeration)
    if not user:
        return jsonify({"error": "invalid_credentials", "message": "Invalid credentials"}), 401

    # Handle social-only accounts (no password set)
    stored = (user.password or "").strip()
    if not stored:
        return jsonify({
            "error": "password_not_set",
            "message": "This account does not have a password. Sign in with Google/Apple or set a password."
        }), 401

    # Handle common bad storage: "b'$2b$...'" stored as text
    if stored.startswith("b'") or stored.startswith('b"'):
        try:
            import ast
            stored = ast.literal_eval(stored).decode("utf-8")
        except Exception:
            return jsonify({"error": "invalid_credentials", "message": "Invalid credentials"}), 401

    # Verify bcrypt safely
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except ValueError:
        ok = False

    if not ok:
        return jsonify({"error": "invalid_credentials", "message": "Invalid credentials"}), 401

    login_user(user, remember=True)

    return jsonify({
        "message": "Logged in",
        "user": {"id": user.id, "email": user.email}
    }), 200


#@app.route("/api/login", methods=["POST"])
#def login():
#    data = request.get_json() or {}
#    email = data.get("email", "").strip().lower() 
#    password = data.get("password") or ""
#    user = User.query.filter_by(email=email).first()
#    if not user or not bcrypt.checkpw(password.encode('utf-8'), user.password.encode('utf-8')):
#        return jsonify({"error": "Invalid credentials"}), 401
#    login_user(user, remember=True)
#    # return jsonify({"message": "Logged in"})
#    return jsonify({
#        "message": "Logged in",
#        "user": {
#            "id": user.id,
#            "email": user.email
#        }
#    })


@app.route("/api/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logged out"})


# Delete user account
@app.route("/api/delete_account", methods=["POST"])
@login_required
def delete_account():
    user = current_user
    user_id = user.id
    user_email = user.email
    user_name = user.first_name
    
    try:
        # 1) Delete dreams one-by-one so we can archive images
        dreams = Dream.query.filter_by(user_id=user_id).all()
        for d in dreams:
            _archive_dream_images(d)
            db.session.delete(d)
            
        # 2) Delete user-owned data
        EmailConfirmToken.query.filter_by(user_id=user.id).delete()
        UserSubscription.query.filter_by(user_id=user.id).delete()
        # UserCredits.query.filter_by(user_id=user.id).delete()
        PasswordResetToken.query.filter_by(user_id=user.id).delete()
        LifeEvent.query.filter_by(user_id=user.id).delete()
        # NOTE: UserCredits and PaymentTransaction is intentionally kept for accounting/audit
        
        # 3) Anonymize the user instead of deleting the row
        #    Hash the email so new users can be compared.
        # ts = int(time.time())
        # rand = secrets.token_hex(4)  # 8 random hex chars

        # user.email = f"Deleted-{user_email}-{ts}-{rand}"
        user.email = hash_string_secret(user_email)
        user.first_name = f"Deleted-{user_name}"
        user.birthdate = None
        user.gender = None
        user.timezone = None
        user.language = "en"
        user.avatar_filename = None
        user.enable_audio = False
        user.email_confirmed = False

        # Sever any social login links so they can't be used to log in again
        user.apple_user_id = None

        # Make password unusable
        user.password = ""

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        app.logger.exception("Failed to delete account for user_id=%s: %s", user_id, e)
        return jsonify({"error": "Failed to delete account"}), 500

    # 3) Log out the now-anonymized user
    logout_user()
    return jsonify({"success": True}), 200
        

def call_openai_with_retry(prompt, retries=3, delay=2):
    for attempt in range(retries):
        try:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}]
            )
            return response
        except Exception as e:
            logger.warning(f"[GPT Retry] Attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise

              
def convert_dream_to_image_prompt(message, tone=None, quality="high", image_style_slug=None):
    if quality == "low":
        base_prompt = CATEGORY_PROMPTS["image_free"]
    else:
        base_prompt = CATEGORY_PROMPTS["image"]
  
    tone = tone.strip() if tone else None

    # for dall-e-3
    #style = TONE_TO_STYLE.get(tone, "Photo Realistic")

    # for gpt-image-1.5
    # If user explicitly chose a style, bypass tone->style entirely
    if image_style_slug:
        style = pretty_from_slug(image_style_slug)
        logger.debug(f"[convert_dream_to_image_prompt] User selected style: {style}")
    else:
        style = random.choice(TONE_TO_STYLE.get(tone, TONE_TO_STYLE["Peaceful / gentle"]))
        logger.info(f"[convert_dream_to_image_prompt] AI selected style: {style}")
        logger.debug(f"[convert_dream_to_image_prompt] Received tone: {repr(tone)}")
        logger.debug(f"[convert_dream_to_image_prompt] Available tones: {list(TONE_TO_STYLE.keys())}")

    full_prompt = f"{base_prompt}\n\nRender the image in the style of \"{style}\".\n\nDream: {message}"
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": full_prompt}]
    )
    return response.choices[0].message.content.strip()


# Use profile details in prompt
def _age_years(birthdate: date | None, asof: date | None = None) -> int | None:
    if not birthdate:
        return None
    asof = asof or datetime.now(timezone.utc).date()
    y = asof.year - birthdate.year
    return y - 1 if (asof.month, asof.day) < (birthdate.month, birthdate.day) else y


def intro_line_for_prompt(user, *, include_gender: bool = True, include_timezone: bool = False) -> str | None:
    """
    Build something like:
      "My name is Mike. I'm a 46-year-old male, based in Los Angeles (America/Los_Angeles)."
    Returns None if nothing useful is available.
    """
    bits = []

    # if user.first_name:
        # bits.append(f"My name is {user.first_name}.")

    age = _age_years(user.birthdate)
    who = []
    if age is not None:
        who.append(f"{age}-year-old")
    if include_gender and user.gender:
        who.append(user.gender.strip().lower())
    if who:
        bits.append("I'm a " + " ".join(who) + ".")

    if include_timezone and user.timezone:
        city = user.timezone.split("/")[-1].replace("_", " ")
        bits.append(f"Based in {city} ({user.timezone}).")

    return " ".join(bits) or None


api = Blueprint("api", __name__)
def _life_event_to_dict(ev: LifeEvent):
    return {
        "id": ev.id,
        "title": ev.title,
        "details": ev.details,
        "occurred_at": ev.occurred_at.replace(tzinfo=timezone.utc).isoformat(),
        "tags": ev.tags or [],
        "created_at": (ev.created_at.replace(tzinfo=timezone.utc).isoformat()
                       if ev.created_at else None),
    }

@api.route("/api/life_events", methods=["GET"])
@login_required
@requires_pro
def list_life_events():
    # ?limit=50 (default), newest first
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except Exception:
        limit = 50
    q = LifeEvent.query.filter_by(user_id=current_user.id).order_by(desc(LifeEvent.occurred_at)).limit(limit)
    return jsonify([_life_event_to_dict(ev) for ev in q.all()])

@api.route("/api/life_events", methods=["POST"])
@login_required
@requires_pro
def create_life_event():
    data = request.get_json(force=True, silent=False) or {}
    title = (data.get("title") or "").strip()
    occurred_at_raw = data.get("occurred_at")
    if not title or not occurred_at_raw:
        abort(400, "title and occurred_at are required")
    try:
        occurred_at = datetime.fromisoformat(occurred_at_raw.replace("Z", "+00:00"))
    except Exception:
        abort(400, "occurred_at must be ISO8601")

    details = (data.get("details") or None)
    tags = data.get("tags") or None
    if tags is not None and not isinstance(tags, list):
        abort(400, "tags must be a list of strings")

    ev = LifeEvent(
        user_id=current_user.id,
        title=title,
        details=details,
        occurred_at=occurred_at,
        tags=tags,
    )
    db.session.add(ev)
    db.session.commit()
    return jsonify(_life_event_to_dict(ev)), 201

@api.route("/api/life_events/<int:event_id>", methods=["PATCH"])
@login_required
@requires_pro
def update_life_event(event_id: int):
    ev = LifeEvent.query.filter_by(id=event_id, user_id=current_user.id).first()
    if not ev:
        abort(404)

    data = request.get_json(force=True, silent=False) or {}

    if "title" in data:
        t = (data.get("title") or "").strip()
        if not t:
            abort(400, "title cannot be empty")
        ev.title = t

    if "details" in data:
        ev.details = data.get("details") or None

    if "occurred_at" in data:
        try:
            ev.occurred_at = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
        except Exception:
            abort(400, "occurred_at must be ISO8601")

    if "tags" in data:
        tags = data.get("tags")
        if tags is not None and not isinstance(tags, list):
            abort(400, "tags must be a list")
        ev.tags = tags or None

    db.session.commit()
    return jsonify(_life_event_to_dict(ev))

@api.route("/api/life_events/<int:event_id>", methods=["DELETE"])
@login_required
def delete_life_event(event_id: int):
    ev = LifeEvent.query.filter_by(id=event_id, user_id=current_user.id).first()
    if not ev:
        abort(404)
    db.session.delete(ev)
    db.session.commit()
    return jsonify({"ok": True})


# add personal notes
def update_dream_notes(dream_id: int, user_id: int, notes: str | None):
    d = Dream.query.filter_by(id=dream_id, user_id=user_id).first()
    if not d:
        return None
    d.set_notes(notes)
    db.session.commit()
    return d


# Generate blurry images for free gallery (decided to blur images from app side so as to keep the high res images in the back end)
# def generate_blurred_tile(input_path, output_path, size=(256,256)):
#     with Image.open(input_path) as img:
#         img.thumbnail(size)
#         img = img.filter(ImageFilter.GaussianBlur(radius=6))
#         os.makedirs(os.path.dirname(output_path), exist_ok=True)
#         img.save(output_path, "PNG")

# # inside /api/image_generate after saving the main file:
# tile_path = os.path.join("static","images","tiles", filename)
# blur_path = os.path.join("static","images","tiles_blur", filename)
# generate_resized_image(image_path, tile_path, size=(256,256))
# generate_blurred_tile(image_path, blur_path, size=(256,256))



# --- NEW: helpers ------------------------------------------------------------
_TYPE_LINE = r"^\s*\**\s*Type\s*:\s*(Dream|Question)\s*\**\s*$"
_FLAGS = re.I | re.M
_TYPE_RE = re.compile(_TYPE_LINE, _FLAGS)

def _parse_is_question(ai_text: str) -> bool:
    m = _TYPE_RE.search(ai_text or "")
    return bool(m and m.group(1).lower() == "question")

def _parse_iso_dt(value: str) -> datetime:
    # Accept "YYYY-MM-DD" or full ISO; raise on bad input
    v = value.strip()
    try:
        if len(v) == 10:  # YYYY-MM-DD
            return datetime.strptime(v, "%Y-%m-%d")
        # naive ISO fallback (e.g., 2025-10-12T14:30:00Z or without Z)
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        raise ValueError("Invalid occurred_at; use YYYY-MM-DD or ISO 8601")


def _events_for_prompt(user_id: int, days: int | None = None, cap: int = 5) -> list[str]:
    """
    Fetch up to `cap` most recent life events.
    If `days` is None => no date cutoff (useful for long-ago events like childhood).
    """
    q = (LifeEvent.query
         .filter(LifeEvent.user_id == user_id)
         .order_by(LifeEvent.occurred_at.desc()))
    if days is not None:
        q = q.filter(LifeEvent.occurred_at >= datetime.utcnow() - timedelta(days=days))
    rows = q.limit(cap).all()
    # return [f"{r.occurred_at.date()}: {r.details}" for r in rows]
    return [f"{r.occurred_at.date()}: {r.title}" for r in rows]


def _build_user_payload(dream_prompt: str, user_id: int, dream_text: str) -> str:
    try:
        u = User.query.get(user_id)   # your User model
        intro = intro_line_for_prompt(u, include_gender=True, include_timezone=True) if u else None
    except Exception:
        logger.warning("user fetch/intro build failed", exc_info=True)
        intro = None

    try:
        ctx_items = _events_for_prompt(user_id)
    except Exception:
        logger.warning("life_event fetch failed", exc_info=True)
        ctx_items = []

    parts = [dream_prompt]
    if intro:
        parts.append("User:\n- " + intro)
    if ctx_items:
        parts.append("Context:\n" + "\n".join(f"- {x}" for x in ctx_items))
    parts.append("Dream:\n" + dream_text.strip())
    return "\n\n".join(parts)


MAX_TURNS = 8
def _build_discussion_payload(dream: Dream, turns: list[Discuss], new_text: str) -> str:
    parts = []
    # parts.append('ORIGINAL DREAM:\n"""' + (dream.text or "") + '"""')
    # parts.append('\nPRIOR AI ANALYSIS:\n"""' + (dream.analysis or "") + '"""')
    parts.append("ORIGINAL DREAM:\n---\n" + (dream.text or ""))
    parts.append("PRIOR AI ANALYSIS:\n---\n" + (dream.analysis or ""))

    # Only include turns that have both sides (or at least user text)
    if turns:
        lines = []
        for t in turns:
            if t.text:
                # lines.append(f'User: "{t.text.strip()}"')
                lines.append("User:\n" + t.text.strip())
            if t.response:
                # lines.append(f'Assistant: "{t.response.strip()}"')
                lines.append("Assistant:\n" + t.response.strip())
        if lines:
            parts.append("\nDISCUSSION SO FAR:\n" + "\n".join(lines))

    # parts.append('\nUSER FOLLOW-UP:\n"""' + (new_text or "") + '"""')
    parts.append("USER FOLLOW-UP:\n---\n" + (new_text or ""))
    return "\n\n".join(parts)



def _strip_trailing_type_block(text: str) -> str:
    if not text:
        return text
    if "**Type:**" in text:
        return text.rsplit("**Type:**", 1)[0].rstrip()
    if "Type:" in text:
        return text.rsplit("Type:", 1)[0].rstrip()
    return text

# look for bad entry before sending to AI
MIN_CHARS = 20   
MIN_WORDS = 4

def is_mostly_noise(text: str) -> bool:
    t = text.strip()
    if not t:
        return True

    # % of characters that are letters/digits
    import string
    valid_chars = sum(1 for c in t if c.isalnum())
    noise_ratio = 1 - (valid_chars / max(len(t), 1))
    if noise_ratio > 0.6:  # >60% non-alnum = emoji/symbol spam
        return True

    # all same character like "aaaaaa" or "111111"
    if len(set(t)) <= 2:
        return True

    return False

def validate_dream_text(dream_text: str) -> tuple[bool, str]:
    t = (dream_text or "").strip()
    if not t:
        return False, "Please describe your dream in a sentence or two.  The more detail, the better."
    if len(t) < MIN_CHARS or len(t.split()) < MIN_WORDS:
        return False, "Please add a bit more detail about your dream (at least a few words). The more detail, the better."
    if is_mostly_noise(t):
        return False, "That doesn't look like a dream. Try typing a short description instead."
    return True, ""

# helper to convert ugly slug name into pretty name for the AI
def pretty_from_slug(slug: str) -> str:
    normalized = slug.replace("_", ", ").replace("-", " ").strip()
    if not normalized:
        return slug

    words = re.split(r"\s+", normalized)
    words = [w for w in words if w]

    return " ".join(w[:1].upper() + w[1:] for w in words)

    
# for interpreter addition
DEFAULT_INTERPRETER_ID = "26"

INTERPRETER_TEMPLATE = """
Use the following persona as a voice/style overlay (do not override other instructions)
Respond in the style of {alias} using the following persona elements:
---
- Alias: {alias}
- Persona Name: {name}
- Core Voice: {core_voice}
- Interpretive Lens: {interpretive_lens}
- Emotional Stance: {emotional_stance}

Hard rules:
- Do NOT claim to be a real person.
- Write in an original voice inspired by the persona description only.
- Keep all interpretations psychological, symbolic, and grounded.

"""

# INTERPRETER_TEMPLATE = """
# Interpret the dream using the following persona style:
# - Persona Name: {name}
# - Core Voice: {core_voice}
# - Interpretive Lens: {interpretive_lens}
# - Emotional Stance: {emotional_stance}

# Hard rules:
# - Do NOT claim to be a real person.
# - Write in an original voice inspired by the persona description only.
# - Keep all interpretations psychological, symbolic, and grounded.

# """

def get_interpreter_for_user(user_id: int, interpreter_id):
    # interpreter_id may be int, str, None
    if interpreter_id is None:
        return None

    # normalize
    if isinstance(interpreter_id, int):
        iid = interpreter_id
    else:
        s = str(interpreter_id).strip()
        if not s:
            return None
            logger.debug("[get_interpreter_for_user] None")
        try:
            iid = int(s)
        except ValueError:
            return None  # invalid id -> default
            logger.debug("[get_interpreter_for_user] None")

    if iid == DEFAULT_INTERPRETER_ID:
        return None  # default: no overlay
        logger.debug("[get_interpreter_for_user] Default")

    interp = Interpreter.query.filter_by(id=iid).first()
    if not interp or not bool(interp.is_enabled):
        return None
        logger.debug("[get_interpreter_for_user] None")
        

    tier = (interp.access_tier or "").lower().strip()
    if tier == "pro" and not _user_is_pro(user_id):
        return None
        logger.debug("[get_interpreter_for_user] tier == pro and not _user_is_pro : None")

    return interp


# dream analysis
@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    logger.info(" /api/chat called")
    data = request.get_json()
    logger.debug(f"Received JSON: {data}")

    message = data.get("message")
    interpreter_id = data.get("interpreter_id")

    if not message:
        logger.debug("[WARN] Missing dream message.")
        return jsonify({"error": "Missing dream message."}), 400

    # AI-content policy: screen user input before doing any work or burning
    # credits. Rejected attempts are logged so we can monitor abuse patterns.
    if moderation.check(message, label="dream_input").flagged:
        logger.warning(
            "[MODERATION] dream input rejected user=%s len=%s",
            current_user.id, len(message),
        )
        return jsonify({
            "error": "content_policy",
            "message": moderation.INPUT_REJECTED_MESSAGE,
        }), 400

    interp = get_interpreter_for_user(current_user.id, interpreter_id)

    logger.debug(f"{current_user.id} - {interpreter_id}")
    logger.debug(f"[get_interpreter_for_user] {interp}")

    overlay = ""
    if interp:
        overlay = INTERPRETER_TEMPLATE.format(
            name=interp.name,
            alias=interp.alias,
            core_voice=interp.core_voice,
            interpretive_lens=interp.interpretive_lens,
            emotional_stance=interp.emotional_stance,
        )

    # Check if user is using a free plan, and update counts
    decremented_text = False
    is_pro = _user_is_pro(current_user.id)

    if is_pro:
        logger.debug(f"User is Pro")
    else:
        logger.debug(f"User is Free")

    try:
        if not is_pro:
            ok, reset_iso = decrement_text_or_deny(current_user.id)
            
            if not ok:
                return jsonify({"error": "quota_exhausted", "kind": "text", "next_reset_iso": reset_iso}), 402
            decremented_text = True

        # 1) Save bare dream
        logger.info("Saving dream to database...")
        dream = Dream(
            user_id=current_user.id,
            text=message,
            created_at=datetime.utcnow(),
            interpreter_id=(interp.id if interp else None),
        )
        db.session.add(dream)
        db.session.commit()
        logger.debug(f"Dream saved with ID: {dream.id}")

        # Check user input for length, reject if too short
        # logger.debug("[WARN] Validate input.")
        ok, err = validate_dream_text(message)
        if not ok:
            logger.debug(f"[WARN] Invalid input - {err}")
            dream.summary  =  "Non-dream entry"
            dream.is_question = False
            dream.hidden   = True
            db.session.commit()
            
            return jsonify({
                "dream_id": dream.id,
                "analysis": err,
                "is_question": False,
                "should_generate_image": False,
            }), 200
        
        q = "pro" if is_pro else "simple"
        
        # 2) Build prompt (adds recent life events if any)
        #dream_prompt = CATEGORY_PROMPTS["dream"] if is_pro else CATEGORY_PROMPTS["dream_free"]
        dream_prompt = CATEGORY_PROMPTS["dream"] 
        prompt = _build_user_payload(dream_prompt, current_user.id, message)
        
        if overlay:
            # prompt = overlay + "\n\n" + prompt
            prompt += "\n\nINTERPRETER PROFILE:\n---\n" + overlay
            prompt += "\n\n" 
            
        # logger.debug(f"Sending {q} prompt to OpenAI: {prompt}")
        # logger.debug(f"Dream Analysis Prompt: {prompt}")

        response = call_openai_with_retry(prompt)
        if not getattr(response, "choices", None) or not response.choices[0].message:
            logger.error("[ERROR] AI response was empty.")
            return jsonify({"error": "AI response was empty"}), 500

        content = response.choices[0].message.content.strip()
        logger.debug(f"Dream Analysis Reply: {content}")


        # 3) Parse Analysis / Summary / Tone / Type
        analysis = summary = tone = None
        type_val = None
        is_question = is_nonsense = False
        
        # Accept bold (**X:**) or plain (X:)
        ANALYSIS_MARK = "**Analysis:**" if "**Analysis:**" in content else ("Analysis:" if "Analysis:" in content else None)
        SUMMARY_MARK  = "**Summary:**"  if "**Summary:**"  in content else ("Summary:"  if "Summary:"  in content else None)
        TONE_MARK     = "**Tone:**"     if "**Tone:**"     in content else ("Tone:"     if "Tone:"     in content else None)
        TYPE_MARK     = "**Type:**"     if "**Type:**"     in content else ("Type:"     if "Type:"     in content else None)
        
        # Positions (or -1 if missing)
        iA = content.find(ANALYSIS_MARK) if ANALYSIS_MARK else -1
        iS = content.find(SUMMARY_MARK)  if SUMMARY_MARK  else -1
        iT = content.find(TONE_MARK)     if TONE_MARK     else -1
        iY = content.find(TYPE_MARK)     if TYPE_MARK     else -1
        
        def slice_between(start_mark, start_idx, end_idx):
            if start_idx == -1 or not start_mark:
                return None
            start = start_idx + len(start_mark)
            end   = len(content) if end_idx == -1 else end_idx
            return content[start:end].strip()
        
        
        # 1) Analysis = between Analysis and Summary
        analysis = slice_between(ANALYSIS_MARK, iA, iS)
        # logger.debug(f"Analysis: {analysis}")
        logger.debug(f"Analysis: <snip>")
        
        
        # 2) Summary = between Summary and Tone (if Tone exists) else up to Type else to end
        summary_end_idx = iT if iT != -1 else (iY if iY != -1 else -1)
        summary = slice_between(SUMMARY_MARK, iS, summary_end_idx)
        logger.debug(f"Summary: {summary}")
        
        
        # 3) Tone = between Tone and Type (if Type exists) else to end; keep only first line
        tone_block = slice_between(TONE_MARK, iT, iY)
        tone = tone_block.splitlines()[0].strip().rstrip(string.punctuation) if tone_block else None
        logger.debug(f"Tone: {tone}")
        
        
        # 4) Type = whatever comes after Type marker (used for routing, not rendered)
        type_val = slice_between(TYPE_MARK, iY, -1)
        tv = (type_val or "").strip().lower()
        is_question = tv.startswith("question")
        is_nonsense = tv.startswith("decline")
        logger.debug(f"Type: {tv}")
        
        
        # 5) Fallbacks:
        # If no Analysis/Summary/Tone were found at all, show the model text minus any 'Type:' lines
        if not any([analysis, summary, tone]):
            content_without_type = "\n".join(
                ln for ln in content.splitlines()
                if not ln.strip().lower().startswith(("**type:**", "type:"))
            ).strip()
            analysis = content_without_type or content
            logger.debug(f"Not Dream: {analysis}")
            

        logger.debug(f"[parsed] is_question={is_question} is_nonsense={is_nonsense} tone={tone} summary_present={bool(summary)}")

        # AI-content policy: screen the model's output before returning it
        # to the user. If anything in `content` is flagged, route through
        # the existing decline path so the dream is hidden and the user
        # sees a safe fallback message instead of the raw output.
        if moderation.check(content, label="dream_analysis_output").flagged:
            logger.warning(
                "[MODERATION] analysis output flagged dream=%s user=%s",
                dream.id, current_user.id,
            )
            is_nonsense = True
            analysis = moderation.OUTPUT_FALLBACK_MESSAGE
            summary = "Filtered for safety"
            tone = None

        # --- Decline (non-dream / unrelated) ---
        if is_nonsense:
            # Use whatever we parsed; if parsing missed, fall back to whole content
            # but strip trailing "Type:" so the user never sees it.
            def _strip_trailing_type_block(text: str) -> str:
                if not text: 
                    return "That doesn't look like a dream. Try typing a short description instead."
                if "**Type:**" in text:
                    return text.rsplit("**Type:**", 1)[0].rstrip()
                if "Type:" in text:
                    return text.rsplit("Type:", 1)[0].rstrip()
                return text
        
            user_analysis = analysis or content
            user_analysis = _strip_trailing_type_block(user_analysis)
        
            # Keep the row (don’t delete), hide it by default, and save the AI reply.
            dream.analysis = user_analysis
            dream.summary  = summary or "Non-dream entry"
            dream.tone     = None
            dream.is_question = False
            dream.hidden   = True
            dream.image_file = "placeholders/decline.png"  # neutral icon so FE has a thumbnail
            db.session.commit()
        
            # Return the same shape the FE expects
            return jsonify({
                "dream_id": dream.id,
                "analysis": dream.analysis,
                "tone": dream.tone,
                "is_question": False,
                "should_generate_image": False,
            }), 200

        
        # Question → keep, but no image
        if is_question:
            dream.analysis = analysis
            dream.summary  = summary
            dream.tone     = None
            dream.is_question = True
            dream.image_file = f"placeholders/question2.png" 
            db.session.commit()
            return jsonify({
                "dream_id": dream.id,
                "analysis": dream.analysis,
                "tone": dream.tone,
                "is_question": True,                # <-- give the client a real flag
                "should_generate_image": False,     # <-- authoritative “don’t start”
            }), 200
        
        # Dream → keep + image
        dream.analysis = analysis
        dream.summary  = summary
        dream.tone     = tone
        dream.is_question = False
        db.session.commit()
        
        # only dreams are allowed to enqueue image
        # enqueue_image(dream.id)

        return jsonify({
            "dream_id": dream.id,
            "analysis": dream.analysis,
            "tone": dream.tone,
            "is_question": False,
            "should_generate_image": _can_generate_image(current_user.id),          # <-- check if user is "pro", or "free + has credits"
        }), 200
      
    except Exception as e:
        db.session.rollback()
        if decremented_text and not is_pro:
            refund_text(current_user.id)
        logger.error("Exception during dream processing", exc_info=True)
        return jsonify({"error": "internal error"}), 500


# dream discussion
@app.post("/api/dreams/<int:dream_id>/discuss")
@login_required
def discuss_dream(dream_id: int):
    data = request.get_json(silent=True) or {}
    user_text = (data.get("text") or "").strip()
    if not user_text:
        return jsonify({"error": "missing text"}), 400
    if len(user_text) > 4000:
        return jsonify({"error": "text too long"}), 413

    # AI-content policy: screen user input before doing any work.
    if moderation.check(user_text, label="discuss_input").flagged:
        logger.warning(
            "[MODERATION] discuss input rejected dream=%s user=%s",
            dream_id, current_user.id,
        )
        return jsonify({
            "error": "content_policy",
            "message": moderation.INPUT_REJECTED_MESSAGE,
        }), 400

    interpreter_id = data.get("interpreter_id")
    interp = get_interpreter_for_user(current_user.id, interpreter_id)

    overlay = ""
    if interp:
        overlay = INTERPRETER_TEMPLATE.format(
            name=interp.name,
            alias=interp.alias,
            core_voice=interp.core_voice,
            interpretive_lens=interp.interpretive_lens,
            emotional_stance=interp.emotional_stance,
        )

    dream = Dream.query.filter_by(id=dream_id, user_id=current_user.id).first()
    if not dream:
        return jsonify({"error": "dream not found"}), 404

    # Create discuss row first (so you have an id even if generation fails)
    drow = Discuss(
        dream_id=dream.id,
        user_id=current_user.id,
        text=user_text,
        created_at=datetime.utcnow(),
    )
    db.session.add(drow)
    db.session.commit()

    # Load last MAX_TURNS prior turns (excluding current row if you want)
    recent = (Discuss.query
              .filter_by(dream_id=dream.id, user_id=current_user.id)
              .order_by(Discuss.created_at.desc())
              .limit(MAX_TURNS + 1)
              .all())

    # Remove current row from context, then reverse to chronological
    recent = [t for t in recent if t.id != drow.id]
    recent.reverse()

    system_msg = CATEGORY_PROMPTS["discuss"]
    prompt = _build_discussion_payload(dream, recent, user_text)
    full_prompt = system_msg + "\n\n" + prompt

    if overlay:
        # full_prompt = overlay + "\n\n" + full_prompt
        full_prompt += "\n\nINTERPRETER PROFILE:\n---\n" + overlay
    full_prompt += "\n\n" 

    try:
        response = call_openai_with_retry(full_prompt)
        if not getattr(response, "choices", None) or not response.choices[0].message:
            logger.error("[ERROR] AI response was empty.")
            return jsonify({"error": "AI response was empty"}), 500

        content = response.choices[0].message.content.strip()
        logger.debug(f"Dream Analysis Reply: {content}")

    except Exception as e:
        # keep the row, but store an error message or leave response NULL
        drow.response = None
        db.session.commit()
        return jsonify({"error": "generation failed"}), 500

    # AI-content policy: screen model output before saving or returning.
    if moderation.check(content, label="discuss_output").flagged:
        logger.warning(
            "[MODERATION] discuss reply flagged dream=%s discuss=%s user=%s",
            dream.id, drow.id, current_user.id,
        )
        content = moderation.OUTPUT_FALLBACK_MESSAGE

    drow.response = content
    db.session.commit()

    return jsonify({
        "dream_id": dream.id,
        "discuss_id": drow.id,
        "response": content,
    })

    

# Create a life event
@app.post("/api/life-events")
@login_required
def create_life_event():
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    if len(title) > 120:
        return jsonify({"error": "title too long (max 120)"}), 400

    try:
        occurred_at = _parse_iso_dt(data.get("occurred_at") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ev = LifeEvent(
        user_id=current_user.id,
        title=title,
        occurred_at=occurred_at,
        details=(data.get("details") or None),
        tags=(data.get("tags") or None),  # list or None; OK with JSON column
    )
    db.session.add(ev)
    db.session.commit()

    return jsonify({
        "id": ev.id,
        "title": ev.title,
        "occurred_at": ev.occurred_at.isoformat() + "Z",
        "details": ev.details,
        "tags": ev.tags,
        "created_at": ev.created_at.isoformat() + "Z",
    }), 201


# Update a life event
@app.patch("/api/life-events/<int:event_id>")
@login_required
def update_life_event(event_id):
    ev = LifeEvent.query.filter_by(id=event_id, user_id=current_user.id).first()
    if not ev:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(force=True) or {}

    if "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title cannot be empty"}), 400
        if len(title) > 120:
            return jsonify({"error": "title too long (max 120)"}), 400
        ev.title = title

    if "occurred_at" in data and data.get("occurred_at"):
        try:
            ev.occurred_at = _parse_iso_dt(data["occurred_at"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if "details" in data:
        ev.details = (data.get("details") or None)

    if "tags" in data:
        ev.tags = (data.get("tags") or None)

    db.session.commit()

    return jsonify({
        "id": ev.id,
        "title": ev.title,
        "occurred_at": ev.occurred_at.isoformat() + "Z",
        "details": ev.details,
        "tags": ev.tags,
        "created_at": ev.created_at.isoformat() + "Z",
    })


# Delete life event
@app.delete("/api/life-events/<int:event_id>")
@login_required
def delete_life_event(event_id):
    ev = LifeEvent.query.filter_by(id=event_id, user_id=current_user.id).first()
    if not ev:
        return jsonify({"error": "not found"}), 404
    db.session.delete(ev)
    db.session.commit()
    return jsonify({"ok": True})


# Fetch all life events for the UI/Editor
@app.get("/api/life-events")
@login_required
def list_life_events():
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(max(int(request.args.get("per_page", 20)), 1), 100)

    since = request.args.get("since")  # optional
    until = request.args.get("until")  # optional

    q = LifeEvent.query.filter(LifeEvent.user_id == current_user.id)
    if since:
        try:
            q = q.filter(LifeEvent.occurred_at >= _parse_iso_dt(since))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if until:
        try:
            q = q.filter(LifeEvent.occurred_at <= _parse_iso_dt(until))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    q = q.order_by(desc(LifeEvent.occurred_at), desc(LifeEvent.id))
    items = q.limit(per_page).offset((page - 1) * per_page).all()

    return jsonify({
        "page": page,
        "per_page": per_page,
        "items": [{
            "id": r.id,
            "title": r.title,
            "occurred_at": r.occurred_at.isoformat() + "Z",
            "details": r.details,
            "tags": r.tags,
            "created_at": r.created_at.isoformat() + "Z",
        } for r in items]
    })
  
# Tiny endpoint to fetch recent events (for the picker)
# GET /api/life-events/recent?days=90&limit=10
@app.get("/api/life-events/recent")
@login_required
def recent_life_events():
    days = int(request.args.get("days", 90))
    limit = int(request.args.get("limit", 10))
    rows = (LifeEvent.query
            .filter(LifeEvent.user_id == current_user.id,
                    LifeEvent.occurred_at >= datetime.utcnow() - timedelta(days=days))
            .order_by(LifeEvent.occurred_at.desc())
            .limit(limit).all())
    return jsonify([{
        "id": r.id,
        "title": r.title,
        "occurred_at": r.occurred_at.isoformat() + "Z"
    } for r in rows])



# used to generate smaller images for journal and tiles
def generate_resized_image(input_path, output_path, size=(48, 48)):
    try:
        with Image.open(input_path) as img:
            img.thumbnail(size)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, "PNG")
            logger.info(f"Resized image saved to {output_path}")
    except Exception as e:
        logger.error(f"[ERROR] Failed to create resized image ({size}): {e}")


@app.post("/api/image_generate")
@login_required
def generate_dream_image():
    # return jsonify({"error": "disabled for testing", "kind": "image"}), 402
    
    is_pro = _user_is_pro(current_user.id)

    # Free user: gate before any work
    decremented_image = False
    if not is_pro:
        ok = decrement_image_or_deny(current_user.id)
        if not ok:
            return jsonify({"error": "quota_exhausted", "kind": "image"}), 402
        decremented_image = True

    # was unable to get usable images from "low" quality engine, so skipping completely.
    # q = "high" if is_pro else "low" 
    q = "high"
    
    logger.info(" /api/image_generate called")
    data = request.get_json()
    dream_id = data.get("dream_id")

    logger.debug(f"[generate_dream_image] - {data}")

    # 1) Input guard
    if not dream_id:
        logger.debug("[WARN] Missing dream ID.")
        return jsonify({"error": "Missing dream ID."}), 400

    # 2) Lookup + auth (keep separate => correct 404 semantics)
    dream = Dream.query.get(dream_id)
    if dream is None or dream.user_id != current_user.id:
        return jsonify({"error": "Dream not found or unauthorized"}), 404

    # 3) Skip conditions (no need to check "not dream" again)
    # if dream.hidden or dream.is_question or not dream.summary or not dream.tone:
    if dream.hidden or dream.is_question:
        return jsonify({
            "skipped": True,
            "image_file": dream.image_file  # no need for getattr; dream exists
        }), 200

    message = dream.text
    tone = dream.tone
    image_style_slug = (data.get("image_style") or "").strip() or None

    try:
        logger.info(f"Converting dream to {q} quality image prompt...")
        logger.debug(f"Selected Style:  {image_style_slug}")
        image_prompt = convert_dream_to_image_prompt(message, tone, q, image_style_slug=image_style_slug)
        logger.debug(f"[image prompt]: {image_prompt}")

        # logger.info("Sending image generation request...")

        # Supported values are: 'gpt-image-1', 'gpt-image-1-mini', 'gpt-image-0721-mini-alpha', 'dall-e-2', and 'dall-e-3'
        # model = "dall-e-2" if q == "low" else "dall-e-3"
        # size  = "512x512"  if q == "low" else "1024x1024"

        # dall-e-3 model
        # logger.info("Sending dall-e-3 image generation request...")
        # image_response = client.images.generate(
        #     model="dall-e-3",
        #     prompt=image_prompt,
        #     n=1,
        #     size="1024x1024",
        #     response_format="url"
        # )
        # image_url = image_response.data[0].url
        # logger.info(f"Image URL received: {image_url}")

        # filename = f"{uuid.uuid4().hex}.png"
        # image_path = os.path.join("static", "images", "dreams", filename)
        # tile_path = os.path.join("static", "images", "tiles", filename)
        # os.makedirs(os.path.dirname(image_path), exist_ok=True)

        # # Fetch and save the image with a timeout
        # img_response = requests.get(image_url, timeout=30)
        # img_response.raise_for_status()

        # with open(image_path, "wb") as f:
        #     f.write(img_response.content)
        # logger.info(f"Image saved to {image_path}")

        # gpt-image-1.5 model
        logger.info("Sending gpt-image-1.5 image generation request...")
        image_response = client.images.generate(
            model="gpt-image-1.5",
            prompt=image_prompt,
            n=1,
            size="1024x1024",
        )
        b64 = image_response.data[0].b64_json
        img_bytes = base64.b64decode(b64)
        logger.info(f"Image data received")

        filename = f"{uuid.uuid4().hex}.png"
        image_path = os.path.join("static", "images", "dreams", filename)
        tile_path = os.path.join("static", "images", "tiles", filename)
        os.makedirs(os.path.dirname(image_path), exist_ok=True)

        with open(image_path, "wb") as f:
            f.write(img_bytes)
        logger.info(f"Image saved to {image_path}")

        generate_resized_image(image_path, tile_path, size=(256, 256))

        # Update DB
        dream.image_file = filename
        dream.image_prompt = image_prompt
        db.session.commit()
        logger.info("Dream successfully updated with image.")

        logger.info("Returning image response to frontend")
        return jsonify({
            # "analysis": dream.analysis,
            "image": f"/static/images/dreams/{dream.image_file}"
        })

    except openai.OpenAIError as e:
        db.session.rollback()
        if decremented_image:
            refund_image(current_user.id)
        logger.error("...", exc_info=True)
        return jsonify({"error": "OpenAI image generation failed"}), 502
    except requests.RequestException as e:
        db.session.rollback()
        if decremented_image:
            refund_image(current_user.id)
        logger.error("...", exc_info=True)
        return jsonify({"error": "Failed to fetch image"}), 504
    except Exception:
        db.session.rollback()
        if decremented_image:
            refund_image(current_user.id)
        logger.exception("Unexpected error during image generation")
        return jsonify({"error": "Image generation failed"}), 500

    # except openai.OpenAIError as e:
    #     logger.error(f"[ERROR] OpenAI image generation failed: {e}")
    #     return jsonify({"error": "OpenAI image generation failed"}), 502

    # except requests.RequestException as e:
    #     logger.error(f"[ERROR] Failed to fetch image from URL: {e}")
    #     return jsonify({"error": "Failed to fetch image"}), 504

    # except Exception as img_error:
    #     logger.exception("Unexpected error during image generation")
    #     return jsonify({"error": "Image generation failed"}), 500

    # finally:
    #     db.session.rollback()  # Only triggers on unhandled exception


# all dreams need to be displayed in the manage page
@app.route("/api/alldreams", methods=["GET"])
@login_required
def get_alldreams():
    user_tz = ZoneInfo(current_user.timezone or "UTC")

    rows = db.session.query(Dream, Interpreter).outerjoin(
        Interpreter, Dream.interpreter_id == Interpreter.id
    ).filter(Dream.user_id == current_user.id).order_by(Dream.created_at.desc()).all()

    def convert_created_at(dt):
        try:
            print(f"Original datetime: {dt} (tzinfo={dt.tzinfo})")
            return dt.replace(tzinfo=timezone.utc).astimezone(user_tz).isoformat()
        except Exception as e:
            print(f"[ERROR] Timestamp conversion failed: {e}")
            traceback.print_exc()
            return None

    def interpreter_icon_path(interp):
        if not interp:
            return None
        f = interp.animated_icon_file or interp.icon_file
        return f"/static/images/interpreters/{f}" if f else None

    return jsonify([
        {
            "id": d.id,
            "summary": d.summary,
            "text": d.text,
            "analysis": d.analysis,
            "hidden": d.hidden,
            "tone": d.tone,
            "image_file": f"/static/images/dreams/{d.image_file}" if d.image_file else None,
            "image_tile": f"/static/images/tiles/{d.image_file}" if d.image_file else None,
            "created_at": convert_created_at(d.created_at) if d.created_at else None,
            "interpreter_id": d.interpreter_id,
            "interpreter_name": interp.name if interp else None,
            "interpreter_icon": interpreter_icon_path(interp),
        } for d, interp in rows
    ])

# fetch gallery images
@app.route("/api/gallery", methods=["GET"])
@login_required
def get_gallery():
    user_tz = ZoneInfo(current_user.timezone or "UTC")

    # dreams = Dream.query.filter(
    #     Dream.user_id == current_user.id,
    #     or_(Dream.hidden == False, Dream.hidden.is_(None))
    # ).order_by(Dream.created_at.desc()).all()

    dreams = (
        Dream.query
        .filter(
            Dream.user_id == current_user.id,
            or_(Dream.hidden == False, Dream.hidden.is_(None)),

            # has an image filename
            Dream.image_file.isnot(None),
            func.length(func.trim(Dream.image_file)) > 0,

            # EXCLUDE placeholders (matches 'placeholder' or 'placeholders')
            ~func.lower(Dream.image_file).like("%placehold%"),

            # EXCLUDE AI questions
            or_(Dream.is_question == False, Dream.is_question.is_(None)),
        )
        .order_by(Dream.created_at.desc())
        .all()
    )

    def convert_created_at(dt):
        try:
            print(f"Original datetime: {dt} (tzinfo={dt.tzinfo})")
            return dt.replace(tzinfo=timezone.utc).astimezone(user_tz).isoformat()
        except Exception as e:
            print(f"[ERROR] Timestamp conversion failed: {e}")
            traceback.print_exc()
            return None

    return jsonify([
        {
            "id": d.id,
            "summary": d.summary,
            "text": d.text,
            "analysis": d.analysis,
            "tone": d.tone,
            "image_file": f"/static/images/dreams/{d.image_file}" if d.image_file else None,
            "image_tile": f"/static/images/tiles/{d.image_file}" if d.image_file else None,
            "created_at": convert_created_at(d.created_at) if d.created_at else None,
            "notes": d.notes
        } for d in dreams
    ])

    
# fetch dreams
@app.route("/api/dreams", methods=["GET"])
@login_required
def get_dreams():
    user_tz = ZoneInfo(current_user.timezone or "UTC")

    rows = db.session.query(Dream, Interpreter).outerjoin(
        Interpreter, Dream.interpreter_id == Interpreter.id
    ).filter(
        Dream.user_id == current_user.id,
        or_(Dream.hidden == False, Dream.hidden.is_(None))
    ).order_by(Dream.created_at.desc()).all()

    def convert_created_at(dt):
        try:
            print(f"Original datetime: {dt} (tzinfo={dt.tzinfo})")
            return dt.replace(tzinfo=timezone.utc).astimezone(user_tz).isoformat()
        except Exception as e:
            print(f"[ERROR] Timestamp conversion failed: {e}")
            traceback.print_exc()
            return None

    def interpreter_icon_path(interp):
        if not interp:
            return None
        f = interp.animated_icon_file or interp.icon_file
        return f"/static/images/interpreters/{f}" if f else None

    return jsonify([
        {
            "id": d.id,
            "summary": d.summary,
            "text": d.text,
            "analysis": d.analysis,
            "tone": d.tone,
            "image_file": f"/static/images/dreams/{d.image_file}" if d.image_file else None,
            "image_tile": f"/static/images/tiles/{d.image_file}" if d.image_file else None,
            "created_at": convert_created_at(d.created_at) if d.created_at else None,
            "notes": d.notes,
            "interpreter_id": d.interpreter_id,
            "interpreter_name": interp.name if interp else None,
            "interpreter_icon": interpreter_icon_path(interp),
        } for d, interp in rows
    ])

# For deleting dreams, and moving the images
@app.route("/api/dreams/<int:dream_id>", methods=["DELETE"])
@login_required
def delete_dream(dream_id):
    dream = Dream.query.get_or_404(dream_id)
    if dream.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        # Delete discussions first (owned by this user + dream)
        (Discuss.query
            .filter_by(dream_id=dream.id, user_id=current_user.id)
            .delete(synchronize_session=False))

        # Move image files to archive folder — best-effort, never blocks the delete
        if dream.image_file and dream.image_file.strip():
            try:
                image_filename = os.path.basename(dream.image_file.strip())
                image_path = os.path.join("static", "images", "dreams", image_filename)
                tile_path = os.path.join("static", "images", "tiles", image_filename)
                archive_dir = os.path.join("static", "images", "deleted")

                os.makedirs(archive_dir, exist_ok=True)

                for path in [image_path, tile_path]:
                    if os.path.exists(path):
                        shutil.move(path, os.path.join(archive_dir, os.path.basename(path)))
            except Exception as img_err:
                print(f"[WARN] Could not archive image for dream {dream_id}: {img_err}")

        db.session.delete(dream)
        db.session.commit()
        return '', 204

    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] Failed to delete dream {dream_id}: {e}")
        return jsonify({"error": "Delete failed"}), 500

# def delete_dream(dream_id):
#     dream = Dream.query.get_or_404(dream_id)
#     if dream.user_id != current_user.id:
#         return jsonify({"error": "Unauthorized"}), 403

#     # Move image files to archive folder
#     if dream.image_file:
#         try:
#             image_path = os.path.join("static", "images", "dreams", dream.image_file)
#             tile_path = os.path.join("static", "images", "tiles", dream.image_file)
#             archive_dir = os.path.join("static", "images", "deleted")

#             os.makedirs(archive_dir, exist_ok=True)

#             for path in [image_path, tile_path]:
#                 if os.path.exists(path):
#                     shutil.move(path, os.path.join(archive_dir, os.path.basename(path)))

#         except Exception as e:
#             print(f"[WARN] Failed to archive image: {e}")

#     db.session.delete(dream)
#     db.session.commit()
#     return '', 204

@app.route("/api/dreams/<int:dream_id>/toggle-hidden", methods=["POST"])
@login_required
def toggle_hidden_dream(dream_id):
    dream = Dream.query.get_or_404(dream_id)
    if dream.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    dream.hidden = not dream.hidden
    db.session.commit()
    return jsonify({"hidden": dream.hidden})


# --- for notes ---
@app.patch("/api/dreams/<int:dream_id>/notes")
@login_required
def patch_dream_notes(dream_id):
    """
    Update/clear personal notes on a dream.
    - Never logs note content.
    - 8k hard cap.
    - Optional optimistic concurrency via last_seen_notes_updated_at.
    """
    # Lookup + ownership guard; return 404 on missing OR not-owned
    dream = Dream.query.get(dream_id)
    if dream is None or dream.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}

    # Validate 'notes'
    if "notes" not in data:
        return jsonify({"error": "invalid_request", "message": "Field 'notes' is required"}), 422
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        return jsonify({"error": "invalid_request", "message": "Field 'notes' must be string or null"}), 422
    if isinstance(notes, str) and len(notes) > NOTES_MAX_LEN:
        return jsonify({"error": "too_large", "message": f"Notes exceed {NOTES_MAX_LEN} characters."}), 413

    last_seen = data.get("last_seen_notes_updated_at")

    # Conflict?
    if _notes_conflict(dream, last_seen):
        # DO NOT log content; include current server state only
        logger.info("notes_update_conflict user_id=%s dream_id=%s", current_user.id, dream.id)
        return jsonify({
            "error": "conflict",
            "message": "Notes were updated elsewhere.",
            "current": {
                "notes": dream.notes,
                "notes_updated_at": _iso_utc(dream.notes_updated_at)
            }
        }), 409

    # Normalize & short-circuit if unchanged (trim compare)
    incoming = (notes or "").strip() or None
    if (dream.notes or "").strip() == (incoming or ""):
        # return 200 with current object for simplicity/consistency
        return jsonify({
            "id": dream.id,
            "notes": dream.notes,
            "notes_updated_at": _iso_utc(dream.notes_updated_at)
        }), 200

    # Apply update (set_notes already bumps notes_updated_at)
    dream.set_notes(incoming)
    db.session.commit()

    # Log IDs only—never the text
    logger.info("notes_updated user_id=%s dream_id=%s", current_user.id, dream.id)

    return jsonify({
        "id": dream.id,
        "notes": dream.notes,
        "notes_updated_at": _iso_utc(dream.notes_updated_at)
    }), 200


# # --- reanalyze the dream with notes included scaffold (policy OFF by default) ---
# @app.post("/api/dreams/<int:dream_id>/reanalyze")
# @login_required
# def reanalyze_dream(dream_id):
#     """
#     Trigger a re-analysis. Notes inclusion is disabled by policy for now,
#     but we return explicit policy metadata so we can flip it later.
#     """
#     dream = Dream.query.get(dream_id)
#     if dream is None or dream.user_id != current_user.id:
#         return jsonify({"error": "not found"}), 404

#     data = request.get_json(silent=True) or {}
#     include_notes_req = (data.get("include_notes") or "auto").lower()
#     if include_notes_req not in ("never", "auto", "always"):
#         return jsonify({"error": "invalid_request", "message": "include_notes must be 'never'|'auto'|'always'"}), 422

#     # Resolver (OFF today)
#     included_notes = False
#     reason = "disabled_by_policy"  # future: "no_consent"|"per_dream_block"|"ok"

#     # If you later enable, gate on NOTES_AI_ENABLED && REANALYZE_WITH_NOTES_ALLOWED
#     # and user/dream consents before setting included_notes=True.

#     # If you later queue jobs, put a job_id here; keeping sync for now.
#     return jsonify({
#         "included_notes": included_notes,
#         "notes_policy_version": NOTES_POLICY_VERSION,
#         "notes_policy_reason": reason
#     }), 200

# get notes
@app.get("/api/dreams/<int:dream_id>/notes")
@login_required
def get_dream_notes(dream_id):
    dream = Dream.query.get(dream_id)
    if dream is None or dream.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404
    # Never log content
    logger.info("notes_read user_id=%s dream_id=%s", current_user.id, dream.id)
    def _iso_utc(dt):
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z") if dt else None
    return jsonify({
        "id": dream.id,
        "notes": dream.notes,
        "notes_updated_at": _iso_utc(dream.notes_updated_at),
    })

# get discussions
@app.get("/api/dreams/<int:dream_id>/discuss")
@login_required
def get_discuss(dream_id: int):
    dream = Dream.query.filter_by(id=dream_id, user_id=current_user.id).first()
    if not dream:
        return jsonify({"error": "dream not found"}), 404

    rows = (Discuss.query
            .filter_by(dream_id=dream.id, user_id=current_user.id)
            .order_by(Discuss.created_at.asc())
            .all())

    return jsonify({
        "dream_id": dream.id,
        "items": [
            {
                "id": r.id,
                "text": r.text or "",
                "response": r.response or "",
                "created_at": r.created_at.isoformat() + "Z",
            } for r in rows
        ]
    })


@app.route("/api/check_auth", methods=["GET"])
def check_auth():
    if current_user.is_authenticated:
        return jsonify({
            "authenticated": True,
            "first_name": current_user.first_name,
            "enable_audio": current_user.enable_audio,
            # "email": current_user.email
        })
    return jsonify({"authenticated": False}), 401




# --- Subscription API Endpoints ---
@app.route("/api/subscription/status", methods=["GET"])
@login_required
def get_subscription_status():
    """Get the current subscription status for the logged-in user"""
    try:
        status = SubscriptionService.get_user_subscription_status(current_user.id)

        # Always attach credit counters so the app can show purchased credits
        # even for active pro/trial subscribers
        uc = ensure_week_current(current_user.id)
        status.update({
            "free_credits": uc.free_credits,
            "purchased_credits": uc.purchased_credits,
            # Legacy aliases for older app versions
            "text_remaining_week": uc.free_credits,
            "image_remaining_lifetime": uc.purchased_credits,
            "next_reset_iso": next_reset_iso(current_user.id),
        })
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error fetching subscription status: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch subscription status"}), 500

@app.get("/api/credits/packs")
@login_required
def get_credit_packs():
    """Return enabled credit packs for display on the subscription/credits screen."""
    packs = CreditPack.query.filter_by(is_enabled=True).order_by(CreditPack.sort_order).all()
    return jsonify([{
        "id":         p.id,
        "name":       p.name,
        "credits":    p.credits,
        "price_usd":  float(p.price_usd),
        "product_id": p.product_id,
    } for p in packs])


@app.post("/api/credits/purchase")
@login_required
def purchase_credits():
    """
    Called by the app after a successful consumable IAP.
    Body: { "pack_id": "credits_small", "receipt": "<store receipt>" }
    For now receipt validation is a stub — add real validation before going live.
    """
    data = request.get_json(silent=True) or {}
    pack_id = (data.get("pack_id") or "").strip()
    if not pack_id:
        return jsonify({"error": "pack_id required"}), 400

    pack = CreditPack.query.filter_by(id=pack_id, is_enabled=True).first()
    if not pack:
        return jsonify({"error": "Unknown or disabled credit pack"}), 404

    # TODO: validate IAP receipt with Apple / Google before granting credits
    # For now, trust the client (acceptable during testing; lock down before launch)

    uc = get_or_create_credits(current_user.id)
    uc.purchased_credits += pack.credits
    db.session.commit()

    logger.info("Credits purchased: user=%s pack=%s credits=%d", current_user.id, pack_id, pack.credits)
    return jsonify({
        "ok": True,
        "credits_added": pack.credits,
        "free_credits": uc.free_credits,
        "purchased_credits": uc.purchased_credits,
        # Legacy aliases for older app versions
        "text_remaining_week": uc.free_credits,
        "image_remaining_lifetime": uc.purchased_credits,
    })


@app.route("/api/subscription/plans", methods=["GET"])
@login_required
def get_subscription_plans():
    """Get all available subscription plans"""
    try:
        plans = SubscriptionService.get_subscription_plans()
        return jsonify(plans)
    except Exception as e:
        logger.error(f"Error fetching subscription plans: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch subscription plans"}), 500

@app.route("/api/subscription/purchase", methods=["POST"])
@login_required
def purchase_subscription():
    """Initiate a subscription purchase

    NOTE: The mobile app may send either the internal plan id (e.g. "pro_monthly")
    or the store product id (e.g. "dreamr_pro_monthly"). To be robust, accept
    both by resolving via primary key *or* SubscriptionPlan.product_id.
    """
    data = request.get_json(silent=True) or {}
    raw_plan = data.get("plan_id")

    logger.info(f"purchase_subscription payload={data}")

    if not raw_plan:
        return jsonify({"error": "plan_id is required"}), 400

    # Allow lookup by primary key OR by product_id used in the stores
    plan = SubscriptionPlan.query.get(raw_plan)
    if not plan:
        plan = SubscriptionPlan.query.filter_by(product_id=raw_plan).first()
    if not plan:
        logger.warning(f"purchase_subscription: plan not found: {raw_plan}")
        return jsonify({"error": f"Plan {raw_plan} not found"}), 404

    try:
        # Determine payment provider
        payment_provider = data.get("payment_provider")
        receipt_data = data.get("receipt_data")

        # Initiate subscription (pass the canonical plan id)
        result = SubscriptionService.initiate_subscription(
            user_id=current_user.id,
            plan_id=plan.id,
            payment_provider=payment_provider,
            receipt_data=receipt_data,
        )

        return jsonify(result)
    except ValueError as e:
        logger.error(
            f"purchase_subscription ValueError: {e} "
            f"user={current_user.id} plan={plan.id} provider={data.get('payment_provider')}",
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 200
        # return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error initiating subscription: {e}", exc_info=True)
        return jsonify({"error": "Failed to initiate subscription"}), 500

@app.route("/api/subscription/cancel", methods=["POST"])
@login_required
def cancel_subscription():
    """Cancel the current subscription"""
    try:
        success = SubscriptionService.cancel_subscription(current_user.id)
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Error canceling subscription: {e}", exc_info=True)
        return jsonify({"error": "Failed to cancel subscription"}), 500

@app.route("/api/subscription/payment-method", methods=["POST"])
@login_required
def update_payment_method():
    """Update the payment method for the current subscription"""
    data = request.get_json(silent=True) or {}
    
    try:
        success = SubscriptionService.update_payment_method(current_user.id, data)
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Error updating payment method: {e}", exc_info=True)
        return jsonify({"error": "Failed to update payment method"}), 500

# added by AZAD, not used
@app.route("/api/admin/subscription/force_set", methods=["POST"])
@admin_required
def admin_force_set_subscription():
    """Admin-only endpoint to manually create or repair a user's subscription.

    Useful when the store reports an active subscription but the local DB is
    out of sync and requires repairing a single user record.
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    plan_id = data.get("plan_id")
    months = data.get("months")
    years = data.get("years")

    if not user_id or not plan_id:
        return jsonify({"error": "user_id and plan_id are required"}), 400

    try:
        sub = SubscriptionService.upsert_manual_subscription(
            user_id=int(user_id),
            plan_id=str(plan_id),
            months=months,
            years=years,
            payment_provider="admin-force",
        )
        return jsonify({
            "success": True,
            "subscription": {
                "id": sub.id,
                "user_id": sub.user_id,
                "plan_id": sub.plan_id,
                "status": sub.status,
                "start_date": sub.start_date.isoformat(),
                "end_date": sub.end_date.isoformat() if sub.end_date else None,
            },
        })
    except Exception as e:
        logger.error("admin_force_set_subscription error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500
    
# --- Optional: Webhook Handlers for App Store and Google Play ---
@app.route("/api/webhooks/apple-iap", methods=["POST"])
def apple_iap_webhook():
    """
    Handle Apple App Store Server Notifications
    
    This endpoint receives server-to-server notifications from Apple
    about subscription events (renewals, cancellations, etc.)
    """
    data = request.get_json(silent=True) or {}
    logger.info(f"Received Apple IAP webhook: {data}")
    
    # Process the notification (implementation depends on your business logic)
    # ...
    
    return jsonify({"status": "received"}), 200

@app.route("/api/webhooks/google-play", methods=["POST"])
def google_play_webhook():
    """
    Handle Google Play Developer API Notifications
    
    This endpoint receives server-to-server notifications from Google
    about subscription events (renewals, cancellations, etc.)
    """
    data = request.get_json(silent=True) or {}
    logger.info(f"Received Google Play webhook: {data}")
    
    # Process the notification (implementation depends on your business logic)
    # ...
    
    return jsonify({"status": "received"}), 200

# Grab interpreters
@app.get("/api/interpreters")
# @login_required
def get_interpreters():
    user_id = current_user.id
    is_pro = _user_is_pro(user_id)

    q = Interpreter.query.filter(Interpreter.is_enabled.is_(True))

    # All users see all interpreters; access_tier is included in the response
    # so the client can display a PRO badge and block selection for free users.
    interps = q.order_by(Interpreter.sort_order.asc(), Interpreter.name.asc()).all()

    return jsonify([
        {
            "id": i.id,
            "slug": i.slug,
            "name": i.name,
            "category": i.category,
            "sort_order": i.sort_order,

            "access_tier": i.access_tier,
            # "unlock_rule": i.unlock_rule,

            "card_blurb": i.card_blurb,
            "card_bullets": i.card_bullets,
            "tone_examples": i.tone_examples,

            # "core_voice": i.core_voice,
            # "interpretive_lens": i.interpretive_lens,
            # "emotional_stance": i.emotional_stance,
            # "prompt_extra": i.prompt_extra,

            # "icon_key": i.icon_key,
            "icon": f"/static/images/interpreters/{i.icon_file}" if i.icon_file else None,
            "animated_icon": f"/static/images/interpreters/{i.animated_icon_file}" if i.animated_icon_file else None,
            "tile": f"/static/images/interpreters_tiles/{i.icon_file}" if i.icon_file else None,
        }
        for i in interps
    ])


# Generate icons for people/theripists
@app.post("/api/interpreters/<string:interp_id>/icon_generate")
@login_required
def generate_interpreter_icon(interp_id):

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force", False))  # allow regen even if icon exists

    interp = Interpreter.query.get(interp_id)
    if not interp:
        return jsonify({"error": "Interpreter not found"}), 404

    if interp.icon_file and not force:
        return jsonify({
            "skipped": True,
            "icon": f"/static/images/interpreters/{interp.icon_file}",
            "tile": f"/static/images/interpreters_tiles/{interp.icon_file}",
        }), 200

    icon_key = interp.icon_key or interp.id
    specific = ICON_PROMPTS.get(icon_key)
    if not specific:
        return jsonify({"error": f"Missing icon prompt for icon_key={icon_key}"}), 400

    # Compose prompt
    icon_prompt = f"{ICON_STYLE_PROMPT.strip()}\n\nSubject:\n{specific.strip()}\n\nSame character style and proportions as other Dreamr interpreter icons."

    try:
        logger.info(f"Generating interpreter icon for {interp_id} (icon_key={icon_key})...")

        # Prefer gpt-image-1 for pixel-art consistency and direct bytes
        image_response = client.images.generate(
            model="gpt-image-1",
            prompt=icon_prompt,
            n=1,
            size="1024x1024"  # generate large, then downscale to tiles
        )

        b64 = image_response.data[0].b64_json
        img_bytes = base64.b64decode(b64)

        filename = f"{uuid.uuid4().hex}.png"
        icon_path = os.path.join("static", "images", "interpreters", filename)
        tile_path = os.path.join("static", "images", "interpreters_tiles", filename)
        os.makedirs(os.path.dirname(icon_path), exist_ok=True)
        os.makedirs(os.path.dirname(tile_path), exist_ok=True)

        with open(icon_path, "wb") as f:
            f.write(img_bytes)

        # Make a small tile (tune sizes to your UI)
        generate_resized_image(icon_path, tile_path, size=(256, 256))

        interp.icon_file = filename
        interp.icon_prompt = icon_prompt
        db.session.commit()

        return jsonify({
            "icon": f"/static/images/interpreters/{filename}",
            "tile": f"/static/images/interpreters_tiles/{filename}",
            "icon_key": icon_key
        }), 200

    except openai.OpenAIError:
        db.session.rollback()
        logger.error("OpenAI icon generation failed", exc_info=True)
        return jsonify({"error": "OpenAI image generation failed"}), 502
    except Exception:
        db.session.rollback()
        logger.exception("Unexpected error during icon generation")
        return jsonify({"error": "Icon generation failed"}), 500



# =========================
# Admin blueprint (HTML)
# =========================
# admin_bp = Blueprint("admin", __name__, url_prefix="/admin") breaks app

# Minimal inline templates to avoid files
ADMIN_SHELL = """<!doctype html><meta charset="utf-8">
<title>{{ title or 'Admin' }}</title>
<style>
  :root { --page-width: 1600px; }
  html,body{height:100%;margin:0;padding:0}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;color:#151515;background:#f8f9fa}
  .wrap{width:min(96vw, var(--page-width)); margin:20px auto; padding:0 16px;}

  a{color:#0a58ca;text-decoration:none}
  a:hover{text-decoration:underline}
  .msg{padding:8px 12px;border-radius:6px;background:#d1ecf1;border:1px solid #bee5eb;display:inline-block;margin:4px 0}
  .msg.success{background:#d4edda;border-color:#c3e6cb}
  .msg.error{background:#f8d7da;border-color:#f5c6cb}
  
  nav{background:#fff;padding:12px 0;margin:-20px -16px 20px;border-bottom:2px solid #dee2e6}
  nav .wrap{display:flex;align-items:center;gap:20px}
  nav a{color:#495057;font-weight:500;padding:8px 12px;border-radius:4px}
  nav a:hover{background:#e9ecef;text-decoration:none}
  
  h1{margin:0;font-size:24px;color:#212529}
  h2{font-size:20px;margin:24px 0 12px;color:#212529}
  h3{font-size:16px;margin:20px 0 10px;color:#495057}
  
  hr{border:0;border-top:1px solid #dee2e6;margin:20px 0}

  table{border-collapse:collapse;width:100%;margin:12px 0;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,0.1);table-layout:fixed}
  th,td{border:1px solid #dee2e6;padding:10px;vertical-align:top;font-size:14px}
  th{background:#f8f9fa;font-weight:600;color:#495057;text-align:left}
  td{color:#212529}
  tr:hover{background:#f8f9fa}
  
  .dream-img{width:60px;height:60px;object-fit:cover;border-radius:4px;border:1px solid #dee2e6}
  .text-cell{max-width:200px;word-wrap:break-word;overflow-wrap:break-word;line-height:1.4}
  
  form{background:#fff;padding:16px;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin:12px 0}
  form.inline{display:inline;background:none;padding:0;box-shadow:none;margin:0 4px 0 0}
  
  label{display:block;margin:8px 0 4px;font-weight:500;font-size:14px;color:#495057}
  input[type=text],input[type=number],input[type=password],select{
    width:100%;padding:8px 12px;border:1px solid #ced4da;border-radius:4px;font-size:14px;
    font-family:inherit;background:#fff
  }
  input[type=text]:focus,input[type=number]:focus,input[type=password]:focus,select:focus{
    outline:none;border-color:#80bdff;box-shadow:0 0 0 3px rgba(0,123,255,0.1)
  }
  
  button{
    padding:8px 16px;border:none;border-radius:4px;font-size:14px;font-weight:500;
    cursor:pointer;background:#007bff;color:#fff;font-family:inherit
  }
  button:hover{background:#0056b3}
  button[type=submit]{background:#28a745}
  button[type=submit]:hover{background:#218838}
  form.inline button{padding:6px 12px;font-size:13px;background:#6c757d}
  form.inline button:hover{background:#5a6268}
  
  .grid{display:grid;grid-template-columns:200px 1fr;gap:12px;align-items:center}
  .section{background:#fff;padding:20px;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin:16px 0}
  
  .pagination{margin:16px 0;display:flex;gap:12px;align-items:center}
  .pagination a{padding:6px 12px;background:#fff;border:1px solid #dee2e6;border-radius:4px}
  .pagination a:hover{background:#e9ecef;text-decoration:none}
  .pagination span{color:#6c757d}
</style>

<body>
  <nav>
    <div class="wrap">
      <h1>Dreamr Admin</h1>
      <a href="/admin/">Dashboard</a>
      <a href="/admin/users">Users</a>
      <a href="/admin/credit-packs">Credit Packs</a>
      <a href="/admin/logout" onclick="event.preventDefault();document.getElementById('al').submit()" style="margin-left:auto">Logout</a>
    </div>
  </nav>
  <div class="wrap">
    {{ body|safe }}
  </div>
  <form id="al" method="post" action="/admin/logout"></form>
</body>
"""


def _render_admin(body_tpl: str, title: str, **ctx):
    body = render_template_string(body_tpl, **ctx)
    return render_template_string(ADMIN_SHELL, title=title, body=body)


# --- Admin login form (HTML) reusing your User + bcrypt + Flask-Login ---
# ----- Admin HTML login -----
@app.get("/admin/login")
def admin_login_form():
    if current_user.is_authenticated and is_admin_user():
        return redirect("/admin/")
    return """
    <form method='post' action='/admin/login' style='max-width:340px;margin:60px auto;font-family:system-ui'>
      <h3>Admin login</h3>
      <input name='email' placeholder='Email' style='width:100%;padding:8px;margin:6px 0'>
      <input name='password' type='password' placeholder='Password' style='width:100%;padding:8px;margin:6px 0'>
      <button type='submit' style='padding:8px 12px'>Sign in</button>
      <p style='font-size:12px;color:#666'>Email must match ADMIN_EMAILS</p>
    </form>
    """

@app.post("/admin/login")
def admin_login_submit():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    u = User.query.filter_by(email=email).first()
    if not u or not u.password:
        return "Invalid credentials", 401
    ok = False
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), u.password.encode("utf-8"))
    except Exception:
        ok = False
    if not ok:
        return "Invalid credentials", 401
    login_user(u, remember=True, duration=timedelta(days=90))
    return redirect("/admin/")


@app.post("/admin/logout")
@login_required
def admin_logout():
    logout_user()
    return redirect("/admin/login")


# ----- Credit Packs admin -----
@app.get("/admin/credit-packs")
@admin_required
def admin_credit_packs():
    packs = CreditPack.query.order_by(CreditPack.sort_order).all()
    BODY = """
    <h2>Credit Packs</h2>
    <p style="color:#6c757d">These packs are shown on the subscription screen. Set the product_id once you create the IAP products in App Store Connect / Google Play Console.</p>
    <form method="post" action="/admin/credit-packs/seed" style="margin:12px 0">
      <button type="submit">Seed default packs (safe to run multiple times)</button>
    </form>
    <table>
      <tr><th>ID</th><th>Name</th><th>Credits</th><th>Price</th><th>Product ID</th><th>Sort</th><th>Enabled</th><th></th></tr>
      {% for p in packs %}
      <tr>
        <td>{{ p.id }}</td>
        <td>{{ p.name }}</td>
        <td>{{ p.credits }}</td>
        <td>${{ '%.2f'|format(p.price_usd) }}</td>
        <td>{{ p.product_id or '—' }}</td>
        <td>{{ p.sort_order }}</td>
        <td>{{ 'Yes' if p.is_enabled else 'No' }}</td>
        <td>
          <form method="post" action="/admin/credit-packs/{{ p.id }}/toggle" class="inline">
            <button type="submit">{{ 'Disable' if p.is_enabled else 'Enable' }}</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    """
    return _render_admin(BODY, "Credit Packs", packs=packs)


@app.post("/admin/credit-packs/seed")
@admin_required
def admin_seed_credit_packs():
    defaults = [
        {"id": "credits_small",  "name": "Small Pack",  "credits": 15,  "price_usd": 0.99, "sort_order": 1},
        {"id": "credits_medium", "name": "Medium Pack", "credits": 90,  "price_usd": 4.99, "sort_order": 2},
        {"id": "credits_large",  "name": "Large Pack",  "credits": 200, "price_usd": 9.99, "sort_order": 3},
    ]
    for d in defaults:
        existing = CreditPack.query.get(d["id"])
        if not existing:
            db.session.add(CreditPack(**d))
    db.session.commit()
    return redirect("/admin/credit-packs")


@app.post("/admin/credit-packs/<string:pack_id>/toggle")
@admin_required
def admin_toggle_credit_pack(pack_id: str):
    pack = CreditPack.query.get_or_404(pack_id)
    pack.is_enabled = not pack.is_enabled
    db.session.commit()
    return redirect("/admin/credit-packs")


# ----- Admin pages -----
@app.get("/admin/")
@admin_required
def admin_dashboard():
    total_users = db.session.query(func.count(User.id)).scalar()
    total_dreams = db.session.query(func.count(Dream.id)).scalar()
    active_subs = (db.session.query(func.count(UserSubscription.id))
                   .filter(UserSubscription.status.in_(["active","trial"])).scalar())
    pending = db.session.query(func.count(PendingUser.uuid)).scalar()
    recent_payments = (db.session.query(PaymentTransaction)
                       .order_by(desc(PaymentTransaction.created_at)).limit(100).all())
    BODY = """
    <h2>Dashboard</h2>
    <div style="display:flex;gap:12px;margin:16px 0">
      <div class="msg">Total users: <b>{{ total_users }}</b></div>
      <div class="msg">Total dreams: <b>{{ total_dreams }}</b></div>
      <div class="msg">Active/trial subs: <b>{{ active_subs }}</b></div>
      <div class="msg">Pending signups: <b>{{ pending }}</b></div>
    </div>
    
    <h3>Recent payments</h3>
    <table>
      <tr><th>ID</th><th>User</th><th>Amount</th><th>Status</th><th>Provider</th><th>When</th></tr>
      {% for p in recent_payments %}
      <tr>
        <td>{{ p.id }}</td>
        <td><a href="{{ url_for('admin_user_detail', user_id=p.user_id) }}">#{{ p.user_id }}</a></td>
        <td>${{ '%.2f'|format(p.amount) }} {{ p.currency }}</td>
        <td>{{ p.status }}</td>
        <td>{{ p.provider }}</td>
        <td>{{ p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else '' }}</td>
      </tr>
      {% endfor %}
    </table>
    """
    return _render_admin(BODY, "Dashboard",
                         total_users=total_users, total_dreams=total_dreams,
                         active_subs=active_subs, pending=pending,
                         recent_payments=recent_payments)

@app.get("/admin/users")
@admin_required
def admin_users_list():
    try: page = max(1, int(request.args.get("page", 1)))
    except: page = 1
    try: per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except: per_page = 50
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "-signup")

    last_dream_subq = (db.session.query(Dream.user_id, func.max(Dream.created_at).label("last_dream"))
                       .group_by(Dream.user_id).subquery())

    qry = User.query
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(User.email.ilike(like), User.first_name.ilike(like)))

    if sort == "email":
        qry = qry.order_by(User.email.asc())
    elif sort == "-email":
        qry = qry.order_by(User.email.desc())
    elif sort == "name":
        qry = qry.order_by(User.first_name.asc(), User.email.asc())
    elif sort == "last_dream":
        qry = (qry.outerjoin(last_dream_subq, User.id == last_dream_subq.c.user_id)
                  .order_by(last_dream_subq.c.last_dream.is_(None).asc(),
                            last_dream_subq.c.last_dream.desc()))
    elif sort == "-last_dream":
        qry = (qry.outerjoin(last_dream_subq, User.id == last_dream_subq.c.user_id)
                  .order_by(last_dream_subq.c.last_dream.is_(None).desc(),
                            last_dream_subq.c.last_dream.asc()))
    elif sort == "id":
        qry = qry.order_by(User.id.asc())
    else:  # -signup (default)
        qry = qry.order_by(User.signup_date.is_(None), User.signup_date.desc())

    rows = qry.limit(per_page + 1).offset((page - 1) * per_page).all()
    has_more = len(rows) > per_page
    users = rows[:per_page]

    subq = (db.session.query(UserSubscription.user_id,
                             func.max(UserSubscription.created_at).label("mx"))
            .group_by(UserSubscription.user_id).subquery())
    latest_subs = {
        s.user_id: s for s in db.session.query(UserSubscription)
        .join(subq, (UserSubscription.user_id == subq.c.user_id) & (UserSubscription.created_at == subq.c.mx))
        .all()
    }
    credits_map = {c.user_id: c for c in UserCredits.query.filter(UserCredits.user_id.in_([u.id for u in users])).all()}

    last_dreams = {
        d.user_id: d.last_dream for d in db.session.query(last_dream_subq).all()
    }

    BODY = """
    <h2>Users</h2>
    <form method="get" style="margin:16px 0">
      <input name="q" value="{{ q or '' }}" placeholder="search email or name" style="width:300px;display:inline-block">
      <select name="sort" style="width:160px;display:inline-block">
        <option value="-signup" {% if sort=='-signup' %}selected{% endif %}>Newest signup</option>
        <option value="id" {% if sort=='id' %}selected{% endif %}>ID ↑</option>
        <option value="email" {% if sort=='email' %}selected{% endif %}>Email A→Z</option>
        <option value="-email" {% if sort=='-email' %}selected{% endif %}>Email Z→A</option>
        <option value="name" {% if sort=='name' %}selected{% endif %}>Name A→Z</option>
        <option value="last_dream" {% if sort=='last_dream' %}selected{% endif %}>Last Dream ↓</option>
        <option value="-last_dream" {% if sort=='-last_dream' %}selected{% endif %}>Last Dream ↑</option>
      </select>
      <button type="submit">Search</button>
    </form>
    {% macro sort_link(label, key) %}
      {% set toggle = '-' + key if sort == key else key %}
      {% set arrow = ' ↓' if sort == key else (' ↑' if sort == '-' + key else '') %}
      <a href="?sort={{ toggle }}&q={{ q }}&per_page={{ per_page }}" style="color:inherit;text-decoration:none;white-space:nowrap">{{ label }}{{ arrow }}</a>
    {% endmacro %}
    <table style="table-layout:auto">
      <tr>
        <th style="width:60px">{{ sort_link('ID', 'id') }}</th>
        <th style="width:200px">{{ sort_link('Email', 'email') }}</th>
        <th style="width:150px">Name</th>
        <th style="width:110px">Signup</th>
        <th style="width:110px">{{ sort_link('Last Dream', 'last_dream') }}</th>
        <th style="width:120px">Plan</th>
        <th style="width:80px">Status</th>
        <th style="width:80px">Text/wk</th>
        <th style="width:80px">Images</th>
      </tr>
      {% for u in users %}
        {% set s = latest_subs.get(u.id) %}
        {% set c = credits_map.get(u.id) %}
        {% set ld = last_dreams.get(u.id) %}
        <tr>
          <td><a href="{{ url_for('admin_user_detail', user_id=u.id) }}">{{ u.id }}</a></td>
          <td style="word-wrap:break-word">{{ u.email }}</td>
          <td>{{ u.first_name or '' }}</td>
          <td>{{ u.signup_date.strftime('%Y-%m-%d') if u.signup_date else '' }}</td>
          <td>{{ ld.strftime('%Y-%m-%d') if ld else '—' }}</td>
          <td>{{ s.plan_id if s else '' }}</td>
          <td>{{ s.status if s else '' }}</td>
          <td>{{ c.free_credits if c else 0 }}</td>
          <td>{{ c.purchased_credits if c else 0 }}</td>
        </tr>
      {% endfor %}
    </table>
    <div class="pagination">
      {% if page>1 %}<a href="?page={{ page-1 }}&per_page={{ per_page }}&q={{ q }}&sort={{ sort }}">← Prev</a>{% endif %}
      <span>Page {{ page }}</span>
      {% if has_more %}<a href="?page={{ page+1 }}&per_page={{ per_page }}&q={{ q }}&sort={{ sort }}">Next →</a>{% endif %}
    </div>
    """
    return _render_admin(BODY, "Users",
                         users=users, latest_subs=latest_subs, credits_map=credits_map,
                         last_dreams=last_dreams,
                         page=page, per_page=per_page, q=q, sort=sort, has_more=has_more)

@app.get("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id: int):
    u = User.query.get_or_404(user_id)
    subs = (UserSubscription.query.filter_by(user_id=u.id)
            .order_by(UserSubscription.created_at.desc()).all())
    credits = UserCredits.query.get(u.id)
    dreams = (Dream.query.filter_by(user_id=u.id)
              .order_by(Dream.created_at.desc()).limit(50).all())
    payments = (PaymentTransaction.query.filter_by(user_id=u.id)
                .order_by(PaymentTransaction.created_at.desc()).all())
    plans = (SubscriptionPlan.query
         .order_by(SubscriptionPlan.period.asc(), SubscriptionPlan.price.asc())
         .all())
    current_sub = (
        UserSubscription.query
        .filter(UserSubscription.user_id == u.id,
                UserSubscription.status.in_(["active", "trial"]))
        .order_by(
            UserSubscription.end_date.is_(None),
            UserSubscription.end_date.desc(),
            UserSubscription.start_date.desc(),
        )
        .first()
    ) or (
        UserSubscription.query
        .filter(UserSubscription.user_id == u.id)
        .order_by(
            UserSubscription.end_date.is_(None),
            UserSubscription.end_date.desc(),
            UserSubscription.start_date.desc(),
        )
        .first()
    )
    
    BODY = """
    <h2>User #{{ u.id }} — {{ u.email }}</h2>
    <p style="color:#6c757d">Name: {{ u.first_name or '' }} | TZ: {{ u.timezone or '' }} | Lang: {{ u.language or '' }} | Audio: {{ 'on' if u.enable_audio else 'off' }}</p>

    {% if request.args.get('msg') %}
      <div class="msg success">{{ request.args.get('msg') }}</div>
    {% endif %}

    <div class="section">
      <h3>Credits</h3>
      <form method="post" action="/admin/users/{{ u.id }}/credits" class="grid">
        <label>Free credits (weekly):</label>
        <input type="number" name="free_credits" value="{{ credits.free_credits if credits else 0 }}" min="0">

        <label>Purchased credits:</label>
        <input type="number" name="purchased_credits" value="{{ credits.purchased_credits if credits else 0 }}" min="0">

        <div></div>
        <button type="submit">Update credits</button>
      </form>
    </div>

    <div class="section">
      <h3>Subscription</h3>
      {% if not plans %}
        <div class="msg error">No plans found. <a href="/admin/plans">Seed default plans</a>.</div>
      {% endif %}
      
      {% if current_sub %}
        <div class="msg" style="margin:12px 0">
          <strong>Current:</strong> {{ current_sub.plan_id }}
          · status: {{ current_sub.status }}
          · start: {{ current_sub.start_date.strftime('%Y-%m-%d') if current_sub.start_date else '' }}
          · end: {{ current_sub.end_date.strftime('%Y-%m-%d') if current_sub.end_date else '—' }}
          · auto renew: {{ 'yes' if current_sub.auto_renew else 'no' }}
        </div>
      {% else %}
        <div class="msg">No active subscription</div>
      {% endif %}
      
      <form method="post" action="/admin/users/{{ u.id }}/subscription" class="grid">
        <label>Action:</label>
        <select name="action">
          <option value="create">Create new</option>
          <option value="update_latest">Update latest</option>
        </select>
      
        <label>Plan:</label>
        <select name="plan_id">
          {% for p in plans %}
            <option value="{{ p.id }}" {% if current_sub and p.id == current_sub.plan_id %}selected{% endif %}>
              {{ p.id }} ({{ p.period }}, ${{ '%.2f'|format(p.price) }})
            </option>
          {% endfor %}
        </select>
      
        <label>Status:</label>
        <select name="status">
          {% for s in ['active','trial','canceled','expired'] %}
            <option value="{{ s }}" {% if current_sub and s == current_sub.status %}selected{% endif %}>{{ s }}</option>
          {% endfor %}
        </select>
      
        <label>Auto renew:</label>
        <select name="auto_renew">
          <option value="0" {% if current_sub and not current_sub.auto_renew %}selected{% endif %}>no</option>
          <option value="1" {% if current_sub and current_sub.auto_renew %}selected{% endif %}>yes</option>
        </select>
      
        <label>Start date:</label>
        <input name="start_date" type="text" placeholder="YYYY-MM-DD or leave blank for now"
               value="{{ current_sub.start_date.strftime('%Y-%m-%d') if current_sub and current_sub.start_date else '' }}">
      
        <label>End date:</label>
        <input name="end_date" type="text" placeholder="YYYY-MM-DD or leave blank for auto"
               value="{{ current_sub.end_date.strftime('%Y-%m-%d') if current_sub and current_sub.end_date else '' }}">
      
        <label>Payment provider:</label>
        <select name="payment_provider">
          <option value="">—</option>
          <option value="apple" {% if current_sub and current_sub.payment_provider=='apple' %}selected{% endif %}>apple</option>
          <option value="google" {% if current_sub and current_sub.payment_provider=='google' %}selected{% endif %}>google</option>
          <option value="stripe" {% if current_sub and current_sub.payment_provider=='stripe' %}selected{% endif %}>stripe</option>
        </select>
      
        <label>Payment method:</label>
        <input name="payment_method" type="text" placeholder="card / apple / google"
               value="{{ current_sub.payment_method if current_sub else '' }}">
      
        <div></div>
        <button type="submit">Save subscription</button>
      </form>
    </div>

    <div class="section">
      <h3>Set password</h3>
      <form method="post" action="/admin/users/{{ u.id }}/password" class="grid">
        <label>New password:</label>
        <input name="password" type="password" minlength="8" required placeholder="Min 8 characters">
        <div></div>
        <button type="submit">Set password</button>
      </form>
    </div>

    <h3>Dreams (latest 50)</h3>
    <table style="table-layout:auto">
      <tr>
        <th style="width:80px">Image</th>
        <th style="width:60px">ID</th>
        <th style="width:140px">Created</th>
        <th style="width:70px">Hidden</th>
        <th style="width:200px">Summary</th>
        <th style="width:250px">Text</th>
        <th style="width:250px">Analysis</th>
        <th style="width:140px">Actions</th>
      </tr>
      {% for d in dreams %}
        <tr>
          <td>
            {% if d.image_file and not d.image_file.startswith('placeholder') %}
              <img src="/static/images/dreams/{{ d.image_file }}" class="dream-img" alt="Dream {{ d.id }}">
            {% else %}
              <div style="width:60px;height:60px;background:#e9ecef;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:24px">💭</div>
            {% endif %}
          </td>
          <td>{{ d.id }}</td>
          <td style="white-space:nowrap">{{ d.created_at.strftime('%Y-%m-%d %H:%M') if d.created_at else '' }}</td>
          <td>{{ 'yes' if d.hidden else 'no' }}</td>
          <td class="text-cell">{{ d.summary or '' }}</td>
          <td class="text-cell">{{ d.text or '' }}</td>
          <td class="text-cell">{{ d.analysis or '' }}</td>
          <td style="white-space:nowrap">
            <form class="inline" method="post" action="/admin/users/{{ u.id }}/dreams/{{ d.id }}/toggle-hidden">
              <button type="submit">{{ 'Unhide' if d.hidden else 'Hide' }}</button>
            </form>
            <form class="inline" method="post" action="/admin/users/{{ u.id }}/dreams/{{ d.id }}/delete" 
                  onsubmit="return confirm('Delete dream {{ d.id }}?');">
              <button type="submit">Delete</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>

    <details style="margin:24px 0">
      <summary style="cursor:pointer;font-weight:600;padding:8px 0">Subscription history ({{ subs|length }})</summary>
      <table style="margin-top:12px">
        <tr><th>ID</th><th>Plan</th><th>Status</th><th>Start</th><th>End</th><th>Auto</th><th>Provider</th></tr>
        {% for s in subs %}
          <tr>
            <td>{{ s.id }}</td>
            <td>{{ s.plan_id }}</td>
            <td>{{ s.status }}</td>
            <td>{{ s.start_date.strftime('%Y-%m-%d') if s.start_date else '' }}</td>
            <td>{{ s.end_date.strftime('%Y-%m-%d') if s.end_date else '' }}</td>
            <td>{{ 'yes' if s.auto_renew else 'no' }}</td>
            <td>{{ s.payment_provider or '' }}</td>
          </tr>
        {% endfor %}
      </table>
    </details>

    <details style="margin:24px 0">
      <summary style="cursor:pointer;font-weight:600;padding:8px 0">Payment history ({{ payments|length }})</summary>
      <table style="margin-top:12px">
        <tr><th>ID</th><th>Amount</th><th>Status</th><th>Provider</th><th>Transaction ID</th><th>When</th></tr>
        {% for p in payments %}
          <tr>
            <td>{{ p.id }}</td>
            <td>${{ '%.2f'|format(p.amount) }} {{ p.currency }}</td>
            <td>{{ p.status }}</td>
            <td>{{ p.provider }}</td>
            <td style="font-family:monospace;font-size:12px">{{ p.provider_transaction_id or '' }}</td>
            <td>{{ p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else '' }}</td>
          </tr>
        {% endfor %}
      </table>
    </details>
    """
    return _render_admin(BODY, f"User {u.id}",
                         u=u, subs=subs, credits=credits, dreams=dreams, payments=payments, plans=plans, current_sub=current_sub)

# Optional: debug
@app.get("/admin/debug")
def admin_debug():
    emails = list(_get_admin_emails())
    return {
        "is_authenticated": current_user.is_authenticated,
        "email": (current_user.email or None) if current_user.is_authenticated else None,
        "ADMIN_EMAILS": emails,
        "match": current_user.is_authenticated and (current_user.email or "").lower() in emails,
    }, 200


# --- Helpers ---
def _parse_iso_optional(s: str | None):
    if not s:
        return None
    v = s.strip()
    if not v:
        return None
    try:
        if len(v) == 10:
            return datetime.strptime(v, "%Y-%m-%d")
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        raise ValueError("Invalid date; use YYYY-MM-DD or ISO 8601")

def _admin_redirect(user_id: int, msg: str = ""):
    msg_q = f"?msg={requests.utils.quote(msg)}" if msg else ""
    return redirect(f"/admin/users/{user_id}{msg_q}")

# --- Update credits ---
@app.post("/admin/users/<int:user_id>/credits")
@admin_required
def admin_update_credits(user_id: int):
    u = User.query.get_or_404(user_id)
    try:
        fc = int(request.form.get("free_credits", "0"))
        pc = int(request.form.get("purchased_credits", "0"))
        if fc < 0 or pc < 0:
            return _admin_redirect(user_id, "Credits must be >= 0")
        uc = UserCredits.query.get(user_id)
        if not uc:
            now = datetime.utcnow()
            week_anchor = now - timedelta(days=now.weekday())
            uc = UserCredits(user_id=user_id, week_anchor_utc=week_anchor, free_credits=fc, purchased_credits=pc)
            db.session.add(uc)
        else:
            uc.free_credits = fc
            uc.purchased_credits = pc
        db.session.commit()
        return _admin_redirect(user_id, "Credits updated")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to update credits")

# --- Create or update subscription ---
@app.post("/admin/users/<int:user_id>/subscription")
@admin_required
def admin_update_subscription(user_id: int):
    u = User.query.get_or_404(user_id)
    action = (request.form.get("action") or "create").strip()
    plan_id = (request.form.get("plan_id") or "").strip()
    status = (request.form.get("status") or "active").strip()
    auto_renew = (request.form.get("auto_renew") or "0").strip() in ("1", "true", "yes")
    payment_provider = (request.form.get("payment_provider") or "").strip() or None
    payment_method = (request.form.get("payment_method") or "").strip() or None
    start_date = request.form.get("start_date") or ""
    end_date = request.form.get("end_date") or ""

    plan = SubscriptionPlan.query.get(plan_id) if plan_id else None
    if not plan:
        return _admin_redirect(user_id, "Invalid plan")

    try:
        sd = _parse_iso_optional(start_date) or datetime.utcnow()
        ed = _parse_iso_optional(end_date)
        if not ed:
            if (plan.period or "").lower().startswith("month"):
                ed = sd + relativedelta(months=1)
            elif (plan.period or "").lower().startswith("year"):
                ed = sd + relativedelta(years=1)
            else:
                ed = sd + timedelta(days=30)

        if action == "update_latest":
            latest = (UserSubscription.query
                      .filter_by(user_id=user_id)
                      .order_by(UserSubscription.created_at.desc()).first())
            if not latest:
                return _admin_redirect(user_id, "No subscription to update")
            latest.plan_id = plan_id
            latest.status = status
            latest.start_date = sd
            latest.end_date = ed
            latest.auto_renew = auto_renew
            latest.payment_provider = payment_provider
            latest.payment_method = payment_method
            db.session.commit()
            return _admin_redirect(user_id, "Subscription updated")
        else:
            sub = UserSubscription(
                user_id=user_id,
                plan_id=plan_id,
                status=status,
                start_date=sd,
                end_date=ed,
                auto_renew=auto_renew,
                payment_provider=payment_provider,
                payment_method=payment_method
            )
            db.session.add(sub)
            db.session.commit()
            return _admin_redirect(user_id, "Subscription created")
    except ValueError as ve:
        db.session.rollback()
        return _admin_redirect(user_id, str(ve))
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to save subscription")

# --- Toggle dream hidden ---
@app.post("/admin/users/<int:user_id>/dreams/<int:dream_id>/toggle-hidden")
@admin_required
def admin_toggle_dream_hidden(user_id: int, dream_id: int):
    d = Dream.query.get_or_404(dream_id)
    if d.user_id != user_id:
        return _admin_redirect(user_id, "Dream does not belong to user")
    try:
        d.hidden = not bool(d.hidden)
        db.session.commit()
        return _admin_redirect(user_id, f"Dream {dream_id} {'hidden' if d.hidden else 'unhidden'}")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to toggle")

# --- Delete dream (with image archival) ---
def _archive_dream_images(dream):
    """Move a dream's image/tile into the deleted archive, ignore errors."""
    if not dream.image_file:
        return

    try:
        image_path = os.path.join("static", "images", "dreams", dream.image_file)
        tile_path = os.path.join("static", "images", "tiles", dream.image_file)
        archive_dir = os.path.join("static", "images", "deleted")
        os.makedirs(archive_dir, exist_ok=True)

        for path in (image_path, tile_path):
            if os.path.exists(path):
                shutil.move(path, os.path.join(archive_dir, os.path.basename(path)))
    except Exception:
        pass

@app.post("/admin/users/<int:user_id>/dreams/<int:dream_id>/delete")
@admin_required
def admin_delete_dream(user_id: int, dream_id: int):
    d = Dream.query.get_or_404(dream_id)
    if d.user_id != user_id:
        return _admin_redirect(user_id, "Dream does not belong to user")
    try:
        _archive_dream_images(d)
        db.session.delete(d)
        db.session.commit()
        return _admin_redirect(user_id, f"Dream {dream_id} deleted")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to delete dream")

# --- Set user password ---
@app.post("/admin/users/<int:user_id>/password")
@admin_required
def admin_set_password(user_id: int):
    u = User.query.get_or_404(user_id)
    pw = request.form.get("password") or ""
    if len(pw) < 8:
        return _admin_redirect(user_id, "Password must be at least 8 chars")
    try:
        u.password = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        db.session.commit()
        return _admin_redirect(user_id, "Password updated")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to update password")


# =====================================================================
# Deep Insights — across-dreams AI analysis
# =====================================================================

# Eligibility / cadence knobs. Keep these in one place so the cron and the
# refresh endpoint stay in agreement.
MIN_DREAMS_FOR_INSIGHTS = 10
MIN_DAYS_BETWEEN_AUTO_INSIGHTS = 7
MIN_NEW_DREAMS_FOR_REGEN = 3
MAX_DREAMS_IN_DIGEST = 50
INSIGHTS_PROMPT_VERSION = 1
INSIGHTS_MODEL = "gpt-4o"


def _truncate(text, limit):
    if not text:
        return ""
    s = str(text).strip().replace("\r\n", "\n").replace("\n\n", "\n")
    if len(s) > limit:
        return s[: limit - 1].rstrip() + "…"
    return s


def build_dream_digest(dreams):
    """Render a chronological digest the AI can reason about.

    Each entry is intentionally compact — summary + tone + a short excerpt
    of the prior per-dream analysis and any user notes. Sending raw dream
    text for every dream blows the budget without improving the result.
    """
    lines = []
    for idx, d in enumerate(dreams, start=1):
        when = d.created_at.strftime("%Y-%m-%d") if d.created_at else "unknown"
        tone = (d.tone or "").strip() or "unspecified"
        summary = _truncate(d.summary, 200) or "(no summary)"
        analysis_excerpt = _truncate(d.analysis, 280)
        notes = _truncate(d.notes, 180)

        block = [f"[Dream #{idx} — {when} — tone: {tone}]"]
        block.append(f"Summary: {summary}")
        if analysis_excerpt:
            block.append(f"Prior AI analysis (excerpt): {analysis_excerpt}")
        if notes:
            block.append(f"User notes: {notes}")
        lines.append("\n".join(block))

    return "\n\n".join(lines)


def _validate_insights_payload(raw):
    """Returns (ok, parsed_or_error_message). Strict but forgiving on shape."""
    try:
        data = json.loads(raw)
    except Exception as e:
        return False, f"Could not parse JSON: {e}"

    if not isinstance(data, dict):
        return False, "Top-level must be an object"

    narrative = data.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        return False, "narrative missing or empty"

    for key in ("recurring_symbols", "emotional_throughlines", "patterns", "questions_to_sit_with"):
        if not isinstance(data.get(key), list):
            return False, f"{key} must be a list"

    return True, data


def _eligibility_for_user(user_id):
    """Returns a dict describing whether the user is eligible for insights."""
    total = Dream.query.filter_by(user_id=user_id, hidden=False).count()
    if total < MIN_DREAMS_FOR_INSIGHTS:
        return {
            "ok": False,
            "reason": "not_enough_dreams",
            "dreams_required": MIN_DREAMS_FOR_INSIGHTS,
            "current": total,
        }
    return {"ok": True, "current": total}


def _utc_iso(dt):
    """Emit an ISO-8601 timestamp with an explicit UTC offset.

    DreamInsight datetimes are stored as naive UTC (datetime.utcnow). Without
    an offset, the client parses them as local time, which shifts relative
    timestamps by the user's UTC offset. Tagging them as UTC fixes that.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _serialize_insight(rec):
    return {
        "id": rec.id,
        "generated_at": _utc_iso(rec.generated_at),
        "window_start": _utc_iso(rec.window_start),
        "window_end": _utc_iso(rec.window_end),
        "dream_count": rec.dream_count,
        "narrative": rec.narrative or "",
        "symbols": json.loads(rec.symbols or "[]"),
        "themes": json.loads(rec.themes or "[]"),
        "patterns": json.loads(rec.patterns or "[]"),
        "questions": json.loads(rec.questions or "[]"),
        "model": rec.model,
        "prompt_version": rec.prompt_version,
    }


def generate_deep_insights_for_user(user_id, *, force=False):
    """Build a digest, call the AI, validate, and persist a DreamInsight row.

    Returns (status, payload):
      - ("ok", DreamInsight) on success
      - ("locked", {...}) if user doesn't meet eligibility
      - ("skipped", reason_str) if eligible but doesn't need regeneration yet
      - ("error", reason_str) on AI / validation failure
    """
    eligibility = _eligibility_for_user(user_id)
    if not eligibility["ok"]:
        return "locked", eligibility

    last = (
        DreamInsight.query.filter_by(user_id=user_id)
        .order_by(DreamInsight.generated_at.desc())
        .first()
    )

    if last and not force:
        days_since = (datetime.utcnow() - last.generated_at).days
        new_dreams_since = Dream.query.filter(
            Dream.user_id == user_id,
            Dream.hidden == False,  # noqa: E712
            Dream.created_at > last.generated_at,
        ).count()
        if days_since < MIN_DAYS_BETWEEN_AUTO_INSIGHTS or new_dreams_since < MIN_NEW_DREAMS_FOR_REGEN:
            return "skipped", f"days_since={days_since}, new_dreams={new_dreams_since}"

    dreams = (
        Dream.query.filter_by(user_id=user_id, hidden=False)
        .order_by(Dream.created_at.desc())
        .limit(MAX_DREAMS_IN_DIGEST)
        .all()
    )
    # Reverse to chronological (oldest -> newest) for the model.
    dreams = list(reversed(dreams))
    if not dreams:
        return "locked", {"ok": False, "reason": "no_dreams"}

    window_start = dreams[0].created_at
    window_end = dreams[-1].created_at

    user = User.query.get(user_id)
    name = (user.first_name or "this dreamer").strip() if user else "this dreamer"

    digest = build_dream_digest(dreams)
    system_prompt = CATEGORY_PROMPTS["deep_insights"]
    user_prompt = (
        f"Here is the dream journal of {name}, covering {len(dreams)} dreams "
        f"between {window_start.strftime('%Y-%m-%d')} and {window_end.strftime('%Y-%m-%d')}.\n\n"
        f"{digest}\n\n"
        "Respond with valid JSON only, matching the shape described in the system prompt."
    )

    try:
        response = openai.chat.completions.create(
            model=INSIGHTS_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as e:
        logger.error(f"[insights] OpenAI call failed for user {user_id}: {e}")
        return "error", f"openai_call_failed: {e}"

    if not getattr(response, "choices", None) or not response.choices[0].message:
        return "error", "empty_response"

    raw = response.choices[0].message.content
    ok, parsed = _validate_insights_payload(raw)
    if not ok:
        logger.error(f"[insights] Invalid JSON from model for user {user_id}: {parsed}")
        return "error", f"invalid_json: {parsed}"

    # AI-content policy: screen the generated narrative before persisting.
    # We don't save a filtered insight (so the user can retry without
    # burning a slot) and let the route surface a generic error.
    if moderation.check(parsed.get("narrative", ""), label="insight_output").flagged:
        logger.warning("[MODERATION] insight narrative flagged user=%s", user_id)
        return "error", "content_filtered"

    rec = DreamInsight(
        user_id=user_id,
        generated_at=datetime.utcnow(),
        window_start=window_start,
        window_end=window_end,
        dream_count=len(dreams),
        narrative=parsed["narrative"].strip(),
        symbols=json.dumps(parsed.get("recurring_symbols", [])),
        themes=json.dumps(parsed.get("emotional_throughlines", [])),
        patterns=json.dumps(parsed.get("patterns", [])),
        questions=json.dumps(parsed.get("questions_to_sit_with", [])),
        model=INSIGHTS_MODEL,
        prompt_version=INSIGHTS_PROMPT_VERSION,
    )
    try:
        db.session.add(rec)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"[insights] Failed to persist for user {user_id}: {e}")
        return "error", f"persist_failed: {e}"

    return "ok", rec


@app.route("/api/insights", methods=["GET"])
@login_required
def get_latest_insights():
    """Return the most recent DreamInsight for the current user.

    Response shapes:
      - 200 { "locked": true, "dreams_required": 10, "current": N }
      - 200 { "locked": false, "insight": null }            (eligible, none yet)
      - 200 { "locked": false, "insight": {...full payload...} }
    """
    eligibility = _eligibility_for_user(current_user.id)
    if not eligibility["ok"]:
        return jsonify({
            "locked": True,
            "dreams_required": eligibility["dreams_required"],
            "current": eligibility["current"],
        }), 200

    rec = (
        DreamInsight.query.filter_by(user_id=current_user.id)
        .order_by(DreamInsight.generated_at.desc())
        .first()
    )
    return jsonify({
        "locked": False,
        "insight": _serialize_insight(rec) if rec else None,
    }), 200


@app.route("/api/insights/refresh", methods=["POST"])
@login_required
def refresh_insights():
    """Force-generate a fresh DreamInsight. Pro-only to avoid abuse.

    Returns:
      - 200 { "insight": {...} } on success
      - 200 { "locked": true, ... } if user is below the dream threshold
      - 402 if user is not pro
      - 429 if regenerated too recently (1 per hour cap)
      - 502 if the AI call or validation failed
    """
    if not _user_is_pro(current_user.id):
        return jsonify({"error": "pro_required"}), 402

    # 1-per-hour throttle on manual refresh.
    recent = (
        DreamInsight.query.filter_by(user_id=current_user.id)
        .order_by(DreamInsight.generated_at.desc())
        .first()
    )
    if recent and (datetime.utcnow() - recent.generated_at) < timedelta(hours=1):
        return jsonify({
            "error": "rate_limited",
            "retry_after_seconds": 3600 - int((datetime.utcnow() - recent.generated_at).total_seconds()),
        }), 429

    status, payload = generate_deep_insights_for_user(current_user.id, force=True)
    if status == "ok":
        return jsonify({"locked": False, "insight": _serialize_insight(payload)}), 200
    if status == "locked":
        return jsonify({
            "locked": True,
            "dreams_required": payload.get("dreams_required", MIN_DREAMS_FOR_INSIGHTS),
            "current": payload.get("current", 0),
        }), 200
    # "error" or unexpected
    return jsonify({"error": "generation_failed", "detail": str(payload)}), 502


# =============================================================================
# AI-Generated Content reporting (Google Play policy compliance)
# =============================================================================
#
# Every AI surface in the app (analysis text, dream image, deep insight,
# discuss reply) exposes a flag/report affordance that POSTs here. Reports
# are stored, logged, and (for child-safety) escalated immediately.
#
# We pair this reactive flow with proactive moderation via OpenAI's
# moderation API on both inputs and outputs at each generation site.

ALLOWED_REPORT_TYPES = {"analysis", "image", "insight", "discuss", "other"}
ALLOWED_REPORT_CATEGORIES = {
    "sexual", "hate", "violence", "child_safety", "misinfo", "other"
}


@app.route("/api/reports", methods=["POST"])
@login_required
def submit_content_report():
    """Receive a user report flagging AI-generated content."""
    data = request.get_json(silent=True) or {}

    content_type = (data.get("content_type") or "").strip().lower()
    category = (data.get("category") or "").strip().lower()
    content_id = (data.get("content_id") or "").strip() or None
    comment = (data.get("comment") or "").strip() or None
    snapshot = (data.get("content_snapshot") or "").strip() or None

    if content_type not in ALLOWED_REPORT_TYPES:
        return jsonify({"error": "invalid content_type"}), 400
    if category not in ALLOWED_REPORT_CATEGORIES:
        return jsonify({"error": "invalid category"}), 400
    if content_id and len(content_id) > 64:
        return jsonify({"error": "invalid content_id"}), 400
    if comment and len(comment) > 500:
        comment = comment[:500]
    if snapshot and len(snapshot) > 2048:
        snapshot = snapshot[:2048]

    report = ContentReport(
        user_id=current_user.id,
        content_type=content_type,
        category=category,
        content_id=content_id,
        comment=comment,
        content_snapshot=snapshot,
    )
    db.session.add(report)
    db.session.commit()

    # Logging: child_safety goes to CRITICAL so it surfaces in any
    # alerting that's wired up. The rest goes to WARNING for daily review.
    if category == "child_safety":
        logger.critical(
            "[REPORT][CHILD_SAFETY] report_id=%s user=%s type=%s content_id=%s",
            report.id, current_user.id, content_type, content_id,
        )
    else:
        logger.warning(
            "[REPORT] report_id=%s user=%s type=%s category=%s content_id=%s",
            report.id, current_user.id, content_type, category, content_id,
        )

    # TODO: notify reviewers out-of-band (Slack webhook / email digest /
    # admin panel). For child_safety, escalate immediately and consider
    # auto-hiding the content for all users until reviewed.

    return jsonify({"ok": True, "report_id": report.id}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

