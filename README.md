# Dreamr — Back End

This is the back-end API and database service for **Dreamr**, an AI-powered dream journal and analysis app. It's a Flask REST API that the [Dreamr mobile app](https://github.com/mikekuriger/dart-dreamr) (iOS/Android, built in Flutter) talks to.

## Tech stack

- **Flask** 3.x — REST API
- **SQLAlchemy** + **Flask-Migrate** (Alembic) — ORM and DB migrations
- **MySQL/MariaDB** (via `mysqlclient` / `PyMySQL`)
- **Flask-Login** — session-based auth, plus **Authlib** / **google-auth** / **PyJWT** for Google and Sign in with Apple
- **OpenAI API** — dream interpretation text + image generation, and content moderation (`omni-moderation-latest`)
- **Pillow** — image post-processing
- **Flask-Mail** (via Mailgun) — account confirmation / password reset email
- **Gunicorn** — WSGI server, containerized with **Docker**

## What it does

- **Auth** — email/password, Google, Apple, and Facebook login; password reset and email confirmation flows
- **Dream journal** — CRUD for dreams, hide/delete, gallery of generated images
- **AI dream interpretation** — submits dream text to OpenAI using persona-specific prompts (`prompts.py`) to produce interpretations from different "interpreter" personalities, plus generated dream imagery
- **Content moderation** — screens both user input and AI output against OpenAI's moderation API before returning/generating content, per Apple/Google AI-generated-content policies (`moderation.py`)
- **Insights** — endpoint(s) backing the app's Insights tab (pattern analysis across a user's dream history)
- **Chat** — conversational follow-up on a dream analysis, with a per-user session/history store (`sessions.py`)
- **Subscriptions & billing** — plan/status endpoints, purchase handling, and webhook receivers for Apple App Store and Google Play server notifications; StoreKit2/Play Billing receipt verification
- **Quota / credits** — weekly free-tier credits, image-generation credit costs, decrement/refund logic (`quota.py`)
- **Reporting** — endpoint for users to report problematic AI-generated content
- **Admin** — internal endpoint(s) for support/ops (e.g. forcing a subscription state)

### API surface (high level)

| Area | Examples |
|---|---|
| Auth | `POST /api/login`, `/api/register`, `/api/google_login`, `/api/apple_login`, `/api/facebook_login`, `/api/logout`, `/api/change_password`, `/api/request_password_reset`, `/api/reset_password`, `/api/confirm/<token>` |
| Dreams | `GET /api/dreams`, `DELETE /api/dreams/<id>`, `POST /api/dreams/<id>/toggle-hidden`, `GET /api/alldreams`, `GET /api/gallery`, `GET /api/images` |
| AI | `POST /api/chat` |
| Insights | `GET /api/insights`, `POST /api/insights/refresh` |
| Subscriptions | `GET /api/subscription/plans`, `/api/subscription/status`, `POST /api/subscription/purchase`, `/api/subscription/cancel`, `/api/subscription/payment-method` |
| Webhooks | `POST /api/webhooks/apple-iap`, `/api/webhooks/google-play` |
| Reporting / misc | `POST /api/reports`, `GET /api/check_auth`, `GET/POST /api/profile` |

## Project layout

```
app.py              Main Flask app: models, routes, request handling
config.py            Instance config (DB URI, secrets, mail, Apple/Google keys) — NOT committed, see below
prompts.py            Interpreter personas / prompt templates for dream analysis
moderation.py         OpenAI moderation helpers for input/output content screening
quota.py               Weekly free-credit and image-credit accounting
sessions.py            In-memory per-user chat session store
migrations/             Alembic/Flask-Migrate DB migrations
scripts/                One-off / maintenance scripts (subscription reconciliation, image sample generation, interpreter seeding, weekly credit updates, weekly insights generation)
sleep_sounds/           Static audio assets served by the app
Dockerfile              Container build (Python 3.11-slim + Gunicorn)
requirements.txt        Python dependencies
```

## Configuration & secrets

App config (DB credentials, `SECRET_KEY`, Mailgun credentials, Apple shared secret, Google service-account path, OAuth client IDs) lives in `config.py`, which is **not tracked in this checkout's parent git repo** (it's gitignored) — it's deployed to the server separately and read via `FLASK_CONFIG_FILE` (defaults to `/home/mk7193/dreamr/config.py`), with env var overrides supported via the `DREAMR_` prefix (`app.config.from_prefixed_env`). Keep real credentials out of version control; use a `config.example.py` / environment variables if this ever needs to be shared or onboarded by someone else.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_CONFIG_FILE=/path/to/your/config.py   # or set DREAMR_* env vars
export OPENAI_API_KEY=...
flask --app app run
```

Database migrations:

```bash
flask db upgrade
```

## Deployment

Built and run via the included `Dockerfile` (Gunicorn, 4 workers, threaded). App data (images, uploads) is expected under a mounted `/data` volume. The `scripts/` directory has standalone maintenance scripts (Apple/Google subscription reconciliation, weekly credit resets, weekly insights generation) intended to run out-of-band (e.g. cron).

## Related repositories

- Mobile app (Flutter, iOS/Android): https://github.com/mikekuriger/dart-dreamr
