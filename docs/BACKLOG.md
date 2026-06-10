# Backlog — phased tasks with acceptance criteria

> Companion to [PLAN.md](../PLAN.md) §11. Tasks ordered within each phase; a task isn't done
> until its acceptance criteria (AC) pass. Specs referenced: [DATA_SPEC](DATA_SPEC.md),
> [ENGINE_SPEC](ENGINE_SPEC.md), [UI_SPEC](UI_SPEC.md).

---

## Phase 0 — Foundations

| # | Task | Acceptance criteria |
|---|------|---------------------|
| 0.1 | Repo scaffold: `pyproject.toml` (ruff, black, pytest-django config), `.env.example`, README | `pip install -e .` works; `ruff check` + `pytest` run clean on empty suite |
| 0.2 | Django project `config/` + apps `core, accounts, ingestion, pipeline, outreach` | `manage.py check` passes; settings read env via django-environ |
| 0.3 | Docker Compose: web, worker, beat, postgres:16, redis:7 | `docker compose up` → migrate runs, web serves on :8000, worker + beat connect |
| 0.4 | Custom `User` model + roles + login/logout pages | Can create admin via `createsuperuser`; role enum per DATA_SPEC; login required everywhere by default |
| 0.5 | Base layout: sidebar nav, Tailwind build, HTMX + Alpine + toast listener wired | A styled placeholder dashboard renders; `HX-Trigger` toast demo works |
| 0.6 | Celery wiring + django-celery-beat; one demo beat task | Beat schedules a heartbeat task visible in worker logs |
| 0.7 | CI: lint + test on push (GitHub Actions) | Red on ruff error or test failure |
| 0.8 | Fernet field-encryption util + `FIELD_ENCRYPTION_KEY` env | Round-trip test; key missing → clear startup error |

## Phase 1 — CRM core

| # | Task | AC |
|---|------|----|
| 1.1 | Models: Company, Contact, Stage (+seed migration), Lead, Activity, Task per DATA_SPEC | Constraints enforced (unique citext email, one open lead/contact); factories for all |
| 1.2 | Contact & company CRUD + list views w/ filters, search, pagination (HTMX patterns §2.7) | Rep sees only own contacts (queryset-level); filters bookmarkable |
| 1.3 | Contact detail: timeline + right rail per UI_SPEC | Activities render typed icons; note-add inline works |
| 1.4 | Kanban board + drag-to-move | Move writes `stage_change` Activity + bumps `last_activity_at`; won/lost sets Lead.status |
| 1.5 | CSV import: upload → mapping UI → async job → summary + errors.csv | Idempotent re-import creates 0 records; dedupe per DATA_SPEC §5; ImportJob audit row |
| 1.6 | Tasks: my-tasks view, inline check-off, due/overdue split | Done sets `done_at`, logs Activity |
| 1.7 | Stage settings editor (admin) | Reorder persists; deleting a stage with leads is blocked (PROTECT surfaced nicely) |

## Phase 2 — Outreach foundation

| # | Task | AC |
|---|------|----|
| 2.1 | Models: Mailbox, EmailTemplate, Sequence, SequenceStep, Enrollment, Message | State machines per DATA_SPEC §4 enforced in model methods (no raw status writes) |
| 2.2 | Template editor + sandboxed merge-field rendering + preview | Unknown merge field rejected at save; preview uses sample contact; fallback syntax works |
| 2.3 | Gmail OAuth connect flow + `GmailProvider.send` | Connect → token stored encrypted; test-send button delivers; revoke → status `error` + banner |
| 2.4 | Outlook OAuth + `GraphProvider.send` | Same AC as 2.3 via Graph |
| 2.5 | Sequence builder UI + enrollment flow (single + bulk from contact list) | Double-enroll blocked; enrollment sets `next_send_at` = now (step 1 due immediately, window-gated) |
| 2.6 | Sender loop per ENGINE_SPEC §1: dispatcher + send_step + idempotency + retries | Two workers racing send exactly once (test w/ select_for_update); failed-after-retries pauses + creates Task |
| 2.7 | Send window + daily cap + jitter + weekday rule | Sends outside window deferred to window open; cap reached → deferred to next day; counters reset per-tz |
| 2.8 | Threading: steps 2+ reply in-thread | Manual check: Gmail shows sequence as one conversation |

## Phase 3 — Engagement & automation

| # | Task | AC |
|---|------|----|
| 3.1 | Reply polling per ENGINE_SPEC §3 (both providers, cursor-safe) | Reply → enrollment `replied`, stage advance (if toggle on), rep Task, Activity; crash-replay safe |
| 3.2 | Auto-reply + bounce classification | OOO doesn't pause sequence; bounce terminal-izes + sets `contact.bounced_at` |
| 3.3 | Open pixel + click redirect endpoints | HMAC-verified clicks; unknown uuid returns GIF/404 per spec; first-event-wins timestamps |
| 3.4 | Unsubscribe page + `List-Unsubscribe` headers + suppression at dispatch | Unsubscribed contact never receives another send (dispatcher test) |
| 3.5 | Hosted capture form + signed webhook intake + optional auto-enroll | Webhook bad-signature → 403; form post creates Contact+Lead with source attribution |
| 3.6 | Mailbox warmup ramp (toggleable) | Cap ramps 20/day → configured cap |

## Phase 4 — Sources, reporting, polish

| # | Task | AC |
|---|------|----|
| 4.1 | Enrichment provider interface + Apollo (or Hunter) implementation | Enrich fills only blank fields; raw payload stored; per-team API key in settings |
| 4.2 | LinkedIn CSV preset mapping | Preset auto-applies; missing-email rows routed to enrichment queue |
| 4.3 | Dashboard v2 + `/reports/`: funnel conversion, per-sequence open/click/reply, per-rep activity | Opens labeled "approx."; date-range filter |
| 4.4 | Duplicate-companies report (DATA_SPEC §5.3) | Lists candidate merges; manual merge action with Activity audit |
| 4.5 | Render deployment: render.yaml (web, worker, beat, Postgres, Redis), backups, Sentry | Deploy from main; daily Postgres backup; error tracking live |
| 4.6 | Permissions polish + GDPR export/delete per contact | Delete cascades cleanly; export produces JSON of contact + activities |

---

## Cross-cutting definition of done (every task)

- Tests for model constraints / task logic touched (pytest-django + factory_boy)
- Queryset-level permission filtering where a view is added
- Activities logged for any state change a user would expect on the timeline
- `ruff` + `black` clean; migration included when models change

## Explicit v1 scope cuts (so they don't creep back silently)

- No multi-channel steps (email only), no A/B testing of templates
- No fuzzy dedupe, no automatic company merging
- No per-object permissions (role + ownership only)
- No dark mode, no mobile-specific layouts
- No ESP/bulk-send path — mailbox OAuth only
- Reports limited to §4.3 set
