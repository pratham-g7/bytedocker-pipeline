# Bytedocker Pipeline

End-to-end outbound client management: lead ingestion → sales pipeline (CRM) → automated
email outreach from reps' real mailboxes.

**Docs:** [PLAN.md](PLAN.md) (overview) · [docs/](docs/) (specs + backlog)

## Quickstart (local, no Docker)

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env        # then fill in SECRET_KEY + FIELD_ENCRYPTION_KEY
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```

Without `DATABASE_URL` set, local dev falls back to sqlite. Celery worker/beat need Redis
(`REDIS_URL`) — easiest via Docker below.

## Quickstart (Docker)

```bash
docker compose up --build
# web :8000 · worker · beat · postgres :5432 · redis :6379
```

## Frontend assets

Compiled Tailwind output (`static/css/app.css`) is checked in. Rebuild after template changes:

```bash
npm install
npm run css:build       # or css:watch during development
```

## Tests & lint

```bash
.venv\Scripts\pytest
.venv\Scripts\ruff check .
.venv\Scripts\black .
```

CI (GitHub Actions) runs ruff + black + pytest against Postgres 16 on every push/PR.
