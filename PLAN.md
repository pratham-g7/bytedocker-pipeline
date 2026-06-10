# Bytedocker Outbound Pipeline — Build Plan

> Status: **Planning** (no application code yet)
> Last updated: 2026-06-10

An end-to-end system to manage outbound clients: **ingest leads → track them through a sales
pipeline → run automated email outreach** from reps' real mailboxes, with reply detection,
engagement tracking, and reporting.

**Detailed specs** (this file is the overview; these are the build contracts):
- [docs/DATA_SPEC.md](docs/DATA_SPEC.md) — field-level schema, constraints, indexes, state machines, dedupe rules
- [docs/ENGINE_SPEC.md](docs/ENGINE_SPEC.md) — sender loop, reply detection, tracking, failure modes, deliverability guardrails
- [docs/UI_SPEC.md](docs/UI_SPEC.md) — route map, screens, HTMX house rules, permissions in UI
- [docs/BACKLOG.md](docs/BACKLOG.md) — per-phase tasks with acceptance criteria + explicit v1 scope cuts

---

## 1. Locked decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Framework | **Django** | Batteries-included ORM, migrations, auth, admin — ideal for CRUD-heavy CRM |
| Frontend | **Django templates + HTMX + Alpine.js + Tailwind** | All-Python, fast, great for an internal tool; Kanban via SortableJS |
| Background jobs | **Celery + Redis** (+ Celery Beat) | Drives sequence timing, sending, reply polling, enrichment |
| Database | **PostgreSQL** | Relational fit for leads/contacts/sequences/activities |
| Email channel | **Real mailbox OAuth** — Gmail API + Microsoft Graph | Lands in inbox, native reply threading, per-mailbox caps; right for 1:1 outbound |
| Lead sources | CSV import, sales-intel APIs, LinkedIn exports, web forms/webhooks | All four ingestion paths |
| Scale target | **Small** (1–5 reps, <500 sends/day), architected to reach mid | Single worker now; provider-agnostic, cap-aware engine for later growth |
| Packaging | **Docker Compose** | web + worker + beat + postgres + redis; on-brand for "Bytedocker" 🐳 |
| Hosting | **Render** | Managed Postgres + Redis, simple Docker deploys; right fit for small scale |

---

## 2. Architecture overview

```
                         ┌─────────────────────────────────────────┐
   Lead sources          │                Django web                │
   ┌──────────────┐      │  HTMX UI · CRM views · Kanban · settings │
   │ CSV upload   │─────▶│                                          │
   │ Apollo/etc.  │─API─▶│   ┌──────────────────────────────────┐  │
   │ LinkedIn CSV │─────▶│   │ Ingestion · Pipeline · Outreach  │  │
   │ Web form /   │─hook▶│   │            (apps)                │  │
   │ webhook      │      │   └──────────────────────────────────┘  │
   └──────────────┘      └───────────────┬──────────────────────────┘
                                          │
                         ┌────────────────┼─────────────────┐
                         ▼                ▼                  ▼
                   ┌──────────┐    ┌────────────┐     ┌────────────┐
                   │ Postgres │    │   Redis    │     │  Celery    │
                   │  (data)  │    │  (broker)  │◀───▶│ worker+beat│
                   └──────────┘    └────────────┘     └─────┬──────┘
                                                            │ OAuth
                                              ┌─────────────┴─────────────┐
                                              ▼                           ▼
                                        Gmail API                 Microsoft Graph
                                     (send + read replies)      (send + read replies)
```

**Three Django apps:**
- `ingestion` — importers, enrichment, web-form/webhook intake, dedupe
- `pipeline` — companies, contacts, leads/deals, stages, activities, tasks (the CRM)
- `outreach` — templates, sequences, enrollments, the sending engine, reply/tracking handlers

Plus `accounts` (users/teams/roles) and `core` (shared utils, base models, settings UI).

---

## 3. Tech stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12 |
| Web | Django 5.x |
| Interactivity | HTMX + Alpine.js |
| Styling | Tailwind CSS (via `django-tailwind` or a standalone CLI build) |
| Drag-drop | SortableJS (Kanban) |
| Async tasks | Celery 5 + Redis |
| Scheduling | Celery Beat (DB-backed via `django-celery-beat`) |
| DB | PostgreSQL 16 |
| Mailbox APIs | `google-api-python-client` (Gmail), `msal` + Graph REST (Outlook) |
| CSV/data | Python stdlib `csv` / `pandas` (only if needed) |
| Auth | Django auth + roles; OAuth via `authlib` or provider SDKs |
| Config | `django-environ` (12-factor env vars) |
| Tests | `pytest-django`, `factory_boy` |
| Lint/format | `ruff` + `black` |
| Container | Docker + Docker Compose |

---

## 4. Data model

```
accounts
  User           id, email, name, role(admin|manager|rep), is_active
  Team           id, name
  Mailbox        id, user FK, provider(gmail|outlook), email,
                 oauth_tokens(enc), daily_cap, send_window, status

pipeline
  Company        id, name, domain, industry, size, location, custom_fields(JSON)
  Contact        id, company FK, first_name, last_name, email, title,
                 phone, linkedin_url, source, owner FK, custom_fields(JSON),
                 unsubscribed(bool), bounced(bool)
  Stage          id, name, order, is_won, is_lost   (configurable pipeline)
  Lead           id, contact FK, stage FK, owner FK, source, value,
                 status(open|won|lost), created_at, last_activity_at
  Activity       id, contact FK, lead FK, type(email_sent|opened|clicked|
                 replied|note|stage_change|call|task), payload(JSON), actor FK, ts
  Task           id, lead FK, owner FK, title, due_at, done(bool)

outreach
  EmailTemplate  id, name, subject, body_html, body_text, merge_fields
  Sequence       id, name, owner FK, is_active
  SequenceStep   id, sequence FK, order, channel(email), wait_days,
                 template FK, send_window_override
  Enrollment     id, contact FK, sequence FK, mailbox FK, current_step,
                 status(active|paused|finished|replied|bounced|unsubscribed),
                 next_send_at, enrolled_by FK
  Message        id, enrollment FK, step FK, mailbox FK, provider_message_id,
                 thread_id, status(scheduled|sent|delivered|bounced),
                 sent_at, opened_at, clicked_at, replied_at
```

**Key relationships:** a `Contact` belongs to a `Company`; a `Lead` is the pipeline item for a
`Contact`; a `Contact` is enrolled into a `Sequence` via `Enrollment`; each send produces a
`Message`; every meaningful event writes an `Activity` (the timeline).

---

## 5. Ingestion design

| Source | Approach |
|--------|----------|
| **CSV / spreadsheet** | Upload → column-mapping UI → validate → dedupe on email/domain → create Company/Contact/Lead. Idempotent re-imports. |
| **Sales-intel (Apollo/Clearbit/Hunter)** | Pluggable `EnrichmentProvider` interface; pull by domain/email, fill missing fields, store raw payload. API keys per-team in settings. |
| **LinkedIn exports** | Treated as a CSV variant with a preset column map; flag fields LinkedIn exports don't include (email often missing → route to enrichment). |
| **Web forms / webhooks** | Signed inbound webhook endpoint (`/ingest/webhook/<token>/`) + a hosted capture form; maps payload → Contact/Lead, optional auto-enroll into a sequence. |

Shared concerns: **dedupe** (email primary, company-domain secondary), **normalization**
(email/phone/URL), **source attribution**, and an **import audit log**.

---

## 6. Pipeline (CRM) design

- **Kanban board** — columns = `Stage`s, cards = `Lead`s; HTMX + SortableJS for drag-to-move,
  which writes a `stage_change` Activity.
- **Contact / Company detail** — profile, custom fields, full activity timeline, enrolled
  sequences, open tasks.
- **List views** — filter/sort/saved-views over contacts and leads; bulk actions
  (assign owner, enroll in sequence, add to stage).
- **Tasks & reminders** — per-rep task list; auto-created on replies and bounces.
- **Configurable stages** — admins edit the stage set and order.

---

## 7. Outreach engine

**Sender loop** (Celery Beat, ~every minute):
1. Query `Enrollment`s where `status=active` AND `next_send_at <= now`.
2. Respect mailbox **send window** (business hours) and **daily cap**.
3. Render `EmailTemplate` with contact merge fields; wrap links + insert open pixel.
4. Send via the contact's mailbox provider (Gmail/Graph), store `provider_message_id` + `thread_id`.
5. Write `email_sent` Activity; advance to next step (`next_send_at = now + step.wait_days`) or mark `finished`.

**Reply detection** (poll Gmail/Graph history or webhook):
- Match inbound message `thread_id` → `Enrollment` → mark `replied`, **pause sequence**,
  advance `Lead` stage, create a rep follow-up `Task`.

**Tracking:**
- **Opens** — 1×1 pixel endpoint stamps `Message.opened_at`.
- **Clicks** — link-wrapping redirect endpoint stamps `clicked_at` then 302s to the target.
- *Caveat:* pixel-based opens are noisy (privacy proxies); treat as directional, not exact.

**Guardrails:**
- Per-mailbox **daily caps** + send-window throttling (deliverability).
- **Unsubscribe** link + suppression list; skip `unsubscribed`/`bounced` contacts.
- **Bounce handling** → mark contact bounced, stop enrollment.
- Dedupe so a contact isn't double-enrolled in the same sequence.

---

## 8. Auth, roles, compliance

- **Roles:** `admin` (settings, users, all data), `manager` (team data, sequences),
  `rep` (own leads + assigned sequences).
- **Mailbox OAuth:** per-user connect flow; tokens encrypted at rest; refresh handled by worker.
- **Compliance (outbound email):** physical address + unsubscribe in footer (CAN-SPAM),
  suppression list, GDPR-minded data handling (export/delete a contact), audit trail via Activities.

---

## 9. Background jobs

| Task | Trigger | Job |
|------|---------|-----|
| Sequence sender | Beat, ~1 min | Send due steps within caps/windows |
| Reply poller | Beat, ~2–5 min | Pull mailbox history, match replies |
| Token refresh | Beat, hourly | Refresh expiring OAuth tokens |
| Enrichment | On-demand / queued | Call sales-intel provider, fill fields |
| CSV import | On upload | Parse, dedupe, create records |
| Daily cap reset | Beat, midnight/tz | Reset per-mailbox counters |

---

## 10. Repo / project structure

```
bytedocker/
├── docker-compose.yml          # web, worker, beat, postgres, redis
├── Dockerfile
├── pyproject.toml              # deps, ruff, black, pytest config
├── .env.example
├── manage.py
├── config/                     # settings, urls, celery app
├── core/                       # base models, mixins, settings UI
├── accounts/                   # users, teams, roles, mailbox OAuth
├── ingestion/                  # importers, enrichment, webhooks
├── pipeline/                   # companies, contacts, leads, stages, tasks
├── outreach/                   # templates, sequences, engine, tracking
├── templates/                  # Django + HTMX templates
├── static/                     # Tailwind build, Alpine, SortableJS
└── tests/
```

---

## 11. Phased delivery

### Phase 0 — Foundations
Repo, `pyproject`, Docker Compose (web/worker/beat/postgres/redis), Django + Postgres wired,
`accounts` with auth + roles, base models, Tailwind/HTMX/Alpine asset pipeline, CI (ruff + pytest).

### Phase 1 — CRM core
Company / Contact / Lead / Stage models + CRUD, **Kanban board** (drag-to-move), contact &
company detail with **activity timeline**, **CSV import** with column mapping + dedupe,
tasks/reminders.

### Phase 2 — Outreach foundation
Email templates + merge fields, sequences + steps, enrollment flow, **mailbox OAuth**
(Gmail + Outlook), **Celery sender loop** with send logging, send window + daily caps.

### Phase 3 — Engagement & automation
**Reply detection** → pause + stage advance + task, **open/click tracking**, unsubscribe +
suppression, bounce handling, auto-enroll from web forms.

### Phase 4 — Sources, reporting, polish
Sales-intel enrichment + LinkedIn import presets, webhook intake hardening,
**dashboards** (funnel conversion, reply/open rates, per-rep activity), permissions polish,
deployment hardening + backups.

---

## 12. Risks & watch-items

- **Deliverability** — sending from real mailboxes is good for inbox placement, but caps,
  warmup, and content hygiene still matter. Keep volumes conservative at small scale.
- **OAuth app verification** — Google/Microsoft may require app review for production mailbox
  scopes; budget lead time.
- **Open-tracking accuracy** — privacy proxies inflate/obscure opens; lean on replies and
  clicks as the real signals.
- **Scope creep** — the CRM surface can balloon; Phases 1–3 are the core, keep Phase 4 lean.

---

## 13. Next step

All decisions locked (hosting: **Render**). Say "go" on Phase 0 and I'll start scaffolding.
Until then this stays a planning document.
```
