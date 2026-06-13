# Deploying to Render

The repo ships a [`render.yaml`](../render.yaml) Blueprint that provisions the
whole stack: a **web** service (Django + gunicorn), a **worker** (Celery), a
**beat** scheduler, managed **Postgres**, and **Redis**. One-time setup:

## 1. Create the Blueprint
1. Push to GitHub (already the remote).
2. Render dashboard → **New → Blueprint** → pick this repo. Render reads
   `render.yaml` and shows the five resources it will create.
3. **Apply.** It builds web/worker/beat, the database, and Redis.

## 2. Fill the secrets
The Blueprint marks per-environment values as `sync: false`, so Render prompts
for them (or set them later under the **bytedocker** env group). All three
Python services share the group, so set each once:

| Var | Value |
|-----|-------|
| `FIELD_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — **the same value** on all services, and never rotate it without re-encrypting stored tokens |
| `ALLOWED_HOSTS` | `bytedocker-web.onrender.com` (+ any custom domain) |
| `CSRF_TRUSTED_ORIGINS` | `https://bytedocker-web.onrender.com` (+ custom domain, with scheme) |
| `BASE_URL` | the public https URL — used for OAuth redirects + tracking links |
| `SENTRY_DSN` | from your Sentry project (optional; blank disables it) |
| `GOOGLE_CLIENT_ID/SECRET`, `MS_*`, `APOLLO_API_KEY` | as in local `.env` |

`SECRET_KEY` is auto-generated; `DATABASE_URL` and `REDIS_URL` are wired
automatically from the database/Redis resources.

**Update the OAuth redirect URIs** in Google Cloud / Microsoft Entra to point at
`<BASE_URL>/settings/mailboxes/gmail/callback/` (and the Outlook equivalent).

## 3. Deploy
Every push to `main` redeploys. Migrations run automatically before traffic
shifts via the web service's `preDeployCommand` (`manage.py migrate`); static
files are collected at build time (`collectstatic`, served by WhiteNoise).

## Backups
Render's **managed Postgres includes automatic daily backups** with
point-in-time recovery (retention depends on plan — verify under the database's
**Backups** tab and set the retention you need). For an offsite copy, add a
Render **Cron Job** running `pg_dump "$DATABASE_URL"` piped to S3/GCS with a
storage credential — left out of the Blueprint because it needs your bucket +
keys.

## Error tracking
Setting `SENTRY_DSN` turns on Sentry for both Django and Celery (see
`config/settings.py`); `send_default_pii=False` keeps contact PII out of Sentry.
Leaving the DSN blank is a no-op, so non-prod environments stay quiet.

## Production hardening
When `DEBUG=false`, `config/settings.py` enables SSL redirect, HSTS, secure
cookies, and the `X-Forwarded-Proto` proxy header (Render terminates TLS at its
edge). Run `python manage.py check --deploy` against the production settings to
confirm before going live.
