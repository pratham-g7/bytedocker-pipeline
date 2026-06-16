# Bytedocker Pipeline — Handoff (read this first)

> You are a fresh Claude instance picking up the **Bytedocker Pipeline** build. This single doc
> gets you fully productive without the prior conversation. It is current as of commit `7fff8c4`
> + the uncommitted fixes described in §0. It supersedes the original Phase-2-era handoff.
>
> **Two halves:** Part A (§1–§14) is the **CRM/engineering** handoff. Part B (§15–§20) is the
> **design-system** handoff. Read Part A §1–§3 once, then jump to whatever you're touching.
> The authoritative product contracts remain the four specs in `docs/` (§6); this doc points at
> them, it does not replace them.

---

## 0. State right now (the very latest)

- **Phases 0 → 4 are complete, committed, pushed, and green** (288 tests). The premium UI/UX
  polish pass is also complete (`7fff8c4`).
- **Uncommitted working-tree changes** (made after `7fff8c4`, not yet committed):
  - **Board drag-and-drop bug fix** — `templates/pipeline/board.html`. Dragging a card worked
    once, then all further drags were dead. **Real root cause (confirmed live):** the board
    refreshes itself via htmx after a move, and the new columns need SortableJS re-wired — but
    re-init was registered with `htmx.onLoad(initBoard)` inside `DOMContentLoaded`, which
    **silently no-ops on htmx 2.0.10** (htmx isn't reliably ready at that point), so the
    refreshed columns got no Sortable instance and were undraggable. Fix: replace `htmx.onLoad`
    with a plain bubbling DOM listener — `document.addEventListener('htmx:load', e => initBoard(e.target))`
    — plus the `DOMContentLoaded` initial init. Verified in-browser: three consecutive moves all
    keep every column wired (pre-fix the count dropped to 0 after move #1). (A first attempt that
    blamed orphaned Sortable instances was wrong and inert; this is the actual fix.)
  - **Emoji removal** — the user wants **zero emojis** anywhere in the product. The dashboard /
    timeline already render line-SVG icons (`{% icon %}`), so any emojis the user saw were a
    **stale cached template** (see the cached.Loader gotcha in §13). Deleted the now-dead
    `ACTIVITY_ICONS` emoji dict + `activity_icon` filter from `pipeline/templatetags/ui.py`, and
    removed the 🐳 from the Gmail test-send body in `outreach/views.py`. `npm run css:build` not
    needed (no CSS change). Tests still 288 green, ruff/black clean.
  - Several test files + a few templates show as modified in `git status` from linters/the user
    (`tests/test_reports.py`, `test_unsubscribe.py`, `test_sequences.py`, `pipeline/duplicates.py`,
    `templates/ingestion/_enrichment_queue.html`, `templates/pipeline/_duplicates.html`,
    `templates/outreach/_preview.html`, `template_editor.html`, `_templates_table.html`,
    `outreach/providers/gmail.py`, `graph.py`, `tests/test_tracking.py`). These are intentional —
    do **not** revert them. Commit them with the fixes when the user asks.
- **Commit/push discipline:** only commit or push when the user explicitly asks. The project
  commits directly to `main`.

---

# PART A — CRM / engineering

## 1. Orientation (60 seconds)

**Product:** an internal web app for ByteDocker's sales operation. ByteDocker is a
developer-staffing business (full domain in §2); this app handles the **client-acquisition**
half — ingest leads (CSV / hosted form / signed webhook / Apollo enrichment) → manage them
through a CRM pipeline (contacts, companies, a Kanban board of leads, tasks, activities) → run
automated multi-step **email sequences sent from reps' own Gmail/Outlook mailboxes** via OAuth
(no ESP / bulk-send path) → track opens/clicks/replies/bounces/unsubscribes → report on funnel,
sequence performance, and rep activity.

**Where we are:** functionally complete through Phase 4 and visually polished. There is **no
queued next phase** — the user drives what's next. Recent live concerns: exercise Apollo
enrichment with a real key, deploy to Render, connect an Outlook mailbox (never done — no MS
creds yet).

**Working style the user expects:** phased, incremental, **one logical task per commit**; match
existing code patterns exactly; **tests + ruff + black clean before every commit**; don't ask
permission to grind through a pre-approved plan, but **do** pause for genuine decisions
(credentials, architecture forks, irreversible actions). The user moves fast and dislikes
over-iteration ("just start the thing") — when something is verifiable, verify it and report,
don't narrate options.

---

## 2. What ByteDocker does (business context — build for the right domain)

**The business.** A **developer-staffing operation**: places India-based senior engineers with
international tech clients and earns the **spread** between the client's international rate and
the developer's Indian-market rate. Two independent contracts (bill client high, pay dev low);
**rate opacity** is a hard rule — the developer never learns the client rate and the two numbers
must never appear together in any client-facing view. Illustrative placement: client ~$75K/yr,
dev ~20 LPA, margin ~52 LPA/yr. Goal: ~10+ stable concurrent placements, then hand off to
managers.

**Two sides that meet at a placement:**
- **Client side (demand)** — sign companies that need engineers. Buyers: CTOs, founders, VC
  talent contacts. Channels: cold email, LinkedIn, Reddit hiring posts, VC referrals.
- **Developer side (supply)** — source India-based seniors per requirement (Reddit, WeWorkRemotely,
  Wellfound, etc.). No pre-built bench; dev data is sparse early.

**Active strategic deal — VC portfolio pipeline:** a VC routes engineering requirements from its
portfolio to ByteDocker (takes no cut, acts as quality verifier; portfolio cos pay ByteDocker
directly, upfront monthly). ~1 placement closed, ~40 requirements expected. Single most important
relationship — one client, many requirements.

**ICP.** Pays market rate for seniors (~$60K+/£60K+/SGD 80K+); remote-friendly; decision-maker
reachable; 1–200 employees, Seed–Series B; vertical SaaS / recently-funded / venture-studio
portfolio cos. Disqualify: in-person-only/visa-restricted, top YC names, 500+ with TA
gatekeepers, budgets below margin viability.

**Operating realities:** emails are often pattern-guesses (verified vs guessed must be
distinguishable before sending); cold/lost leads must be re-surfaceable, not deleted; tooling
today is manual + Apollo (Apollo import desirable).

### 2.1 The two-sided-domain decision — RESOLVED (do not reopen)

The original handoff flagged an open architecture question: extend the schema to first-class
**Requirements / Developers / Placements** (the supply + fulfillment side), or keep v1
client-acquisition-only? **The user decided: client-acquisition only.** Verbatim:

> "we're not going to track dev stuff post-placement, this is just to track up till the placement
> process and contract signing, and maybe some slight things post that."

So: **no Requirements/Developers/Placements entities.** The schema stays Company → Contact →
Lead (on a Stage board) + Activities/Tasks + the outreach engine. Don't build the supply side
unless the user explicitly reverses this. The §2 business facts about the dev side are domain
background only.

---

## 3. Stack & locked decisions (do not re-litigate)

- **Django 5.2**, custom `accounts.User` (email login; `admin`/`manager`/`rep` roles),
  `LoginRequiredMiddleware` (everything auth'd unless `@login_not_required`).
- **HTMX + Alpine.js + Tailwind CSS v3** (standalone CLI build), server-rendered. **No SPA, no
  custom JS modules** beyond Alpine sprinkles + SortableJS (kanban). House rules: `UI_SPEC §2`.
- **Celery 5 + Redis + django-celery-beat** for background/scheduled work. Locally
  `CELERY_TASK_ALWAYS_EAGER=true` runs tasks inline (no worker/Redis needed).
- **PostgreSQL 16** in Docker/CI (partial unique constraints + composite indexes are used);
  **sqlite** locally when `DATABASE_URL` is unset.
- **Fernet** symmetric encryption for OAuth tokens at rest (`core/crypto.py`); never store a
  token in plaintext.
- **Provider abstraction** (`outreach/providers/`): **nothing outside this package imports the
  Google/MS SDKs.** `MailProvider` protocol, `ParsedMessage` dataclass, `TransientProviderError`
  / `ProviderAuthError`.
- **factory_boy + pytest-django**; **ruff + black** (line length 100; migrations are E501-exempt);
  CI on every push (ruff + black --check + pytest vs Postgres 16). Python ≥3.12.

**Scope cuts already agreed** (don't let these creep): email channel only (no multi-channel
steps); no A/B template testing; no fuzzy dedupe / auto company-merge (manual merge only);
role+ownership perms only (no per-object ACLs); **light theme only**; mailbox-OAuth only (no
ESP); reports limited to the funnel/sequence/rep set.

---

## 4. Repo state

| | |
|---|---|
| Remote | `https://github.com/pratham-g7/bytedocker-pipeline.git` |
| Branch | `main` (commit directly here) |
| HEAD | `7fff8c4` — UI premium polish (presentation-only) |
| Working tree | board-fix + emoji-removal + linter edits uncommitted (§0) |
| Tests | **288 passed** (full suite ~18s locally) |
| CI | ruff + black --check + pytest vs Postgres 16, on every push |

Commit history (newest first):
```
7fff8c4 UI premium polish: icons, refined tokens, tables, motion (presentation-only)
55d16e8 Phase 4.5: Render deploy blueprint + Sentry + prod hardening
f6bc33b Phase 4.1+4.2: contact enrichment (Apollo) + LinkedIn preset + queue
3fdc27c Phase 4.6: per-contact GDPR export + delete
77e3be9 Phase 4.4: duplicate-companies report + manual merge
10684f4 Phase 4.3: reports — funnel, sequence performance, rep activity
372a393 Add seed_demo management command for test data
17e59f5 Phase 3.6: mailbox warmup ramp
8f2afc7 Phase 3.5: hosted capture form + signed webhook intake + auto-enroll
72c3cbd Phase 3.4: unsubscribe page + List-Unsubscribe headers + suppression
dff942a Phase 3.3: open-pixel + click tracking endpoints
3c882cb Phase 3.1+3.2: reply detection + auto-reply/bounce classification
8f6245d Micro-interactions and motion pass
1a35b57 UI overhaul: Bytedocker brand system from the letterhead
4864205 Phase 2.3 fix: persist PKCE code_verifier across Gmail OAuth redirect
40f215a Phase 2.8: threading — steps 2+ reply in-thread
79c4dde Phase 2.6+2.7: sender loop with window, cap, jitter, weekday guards
aa9281a Phase 2.5: sequence builder + enrollment (single + bulk)
bf27e5d Phase 2.4: Outlook OAuth + GraphProvider.send
9b12c73 Phase 2.3: Gmail OAuth connect + GmailProvider.send
(… Phase 0/1/1.5 below this)
```

---

## 5. Run & test (Windows / PowerShell)

```powershell
.venv\Scripts\pip install -e ".[dev]"     # if deps changed
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py runserver

.venv\Scripts\pytest                       # full suite (288)
.venv\Scripts\ruff check .
.venv\Scripts\black --check .              # what CI runs

npm run css:build                          # after ANY template/app.css change
python manage.py seed_demo                 # idempotent demo data; --wipe to reset
```

- No `DATABASE_URL` → sqlite locally. Keep the **local sqlite migrated** — Phase 4 added tables
  (`ingestion_enrichmenttask`, accounts/ingestion migrations); a stale local db throws "no such
  table". The test DB is always fresh, so the suite can pass while the dev server 500s — run
  `migrate` after pulling.
- `FIELD_ENCRYPTION_KEY` required before any Mailbox token I/O. Generate:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  Tests inject their own key via an autouse fixture.
- Tailwind: `static/css/app.css` is checked in and built from `static/src/app.css` via
  `npm run css:build`. The build scans `./templates/**/*.html` **and** `./pipeline/templatetags/*.py`
  (templatetags emit class names like `nav-active`/`act-emerald` that never appear in HTML).
- **Local dev preview gotcha:** the launched runserver resolves `DEBUG=false` behind the preview
  proxy, which enables Django's cached template Loader — template edits won't show until you
  **kill the port-8000 process and restart** the server. This is why "I changed the template but
  nothing changed" keeps happening; always restart for visual checks.

---

## 6. The specs (authoritative — read the relevant one before coding)

| Spec | What it pins down |
|------|-------------------|
| `docs/DATA_SPEC.md` | Field-level schema, constraints, **state machines** (Enrollment/Lead/Message), dedupe rules. |
| `docs/ENGINE_SPEC.md` | Celery side: sender-loop pseudocode (§1), provider abstraction (§2), reply detection (§3), tracking endpoints (§4), scheduled jobs (§5), failure-mode table (§6), deliverability guardrails (§7). |
| `docs/UI_SPEC.md` | Route map (§1), the HTMX house rules (§2), per-screen notes (§3), permissions-in-querysets rule (§5). |
| `docs/BACKLOG.md` | Phased tasks + acceptance criteria + scope cuts. |
| `docs/DEPLOY.md` | Render deployment runbook (referenced by `render.yaml`). |
| `PLAN.md` | High-level overview linking them all (oldest doc — reconcile against the four specs when they disagree; specs win). |

**Reconciliations already applied** (PLAN.md was stale): Message status has no "delivered" but
has "failed"; there is no `SequenceStep.channel`; `unsubscribed_at`/`bounced_at` are datetimes;
a reply moves the enrollment to a **terminal** `replied` state (not pause). `citext` is gone in
Django 5 → lowercase-at-save + unique index instead.

---

## 7. App / file map (current)

```
config/   settings.py (env-driven, Sentry wired), celery.py (beat schedule §10), urls.py, wsgi.py
core/     models.TimeStampedModel; crypto.py (Fernet encrypt/decrypt); http.py (hx_toast / hx_events);
          permissions.py (role_required, scope_to_user); reporting.py (funnel/sequence/rep reports);
          checks.py; tasks.py (heartbeat); views.py (dashboard, reports)
accounts/ User (+ Role choices), Team; managers.py (lowercases email)
pipeline/ CRM core: Company, Contact, Stage, Lead, Activity, Task; normalize_domain;
          create_open_lead; duplicates.py (name-core dedupe + merge); views/forms/urls;
          templatetags/ui.py (icon tag, activity_icon_name/activity_tone/activity_text,
          nav_active, qs_replace); management/commands/seed_demo.py
ingestion/CSV import (mapping UI + async run + audit); hosted capture form; signed webhook intake;
          enrichment queue (missing-email rows). models/services/tasks/views
outreach/ THE ENGINE. models: Mailbox, EmailTemplate, Sequence, SequenceStep, Enrollment, Message.
          rendering.py (sandboxed merge-field render against a closed dict). tasks.py (dispatch_due_sends,
          send_step, reset_daily_counters, refresh_expiring_tokens, poll_replies). tracking.py
          (open pixel, click wrap, unsubscribe token/footer/List-Unsubscribe). public_views.py
          (unsubscribe + tracking + capture/webhook — the only @login_not_required surface).
          enrichment.py (Apollo). warmup.py (daily-cap ramp). providers/ (base, gmail, graph) —
          SDK isolation boundary.
templates/ base.html; _empty_state.html; core/ pipeline/ outreach/ ingestion/ registration/
static/   src/app.css → css/app.css (built); img/icons.svg (sprite); img/ brand assets; fonts/ (woff2);
          vendor/ (htmx, alpine, sortable)
tests/    conftest.py (autouse encryption key + plain static storage); factories.py; test_*.py
render.yaml  docker-compose.yml  Dockerfile  .github/workflows/ci.yml  pyproject.toml  .env.example
```

Lean on: `core.http.hx_toast(msg, level, extra_events)` / `hx_events(...)` (the "204 + HX-Trigger
{toast, close-modal, refresh-*}" pattern); `core.permissions.scope_to_user(qs, user, field="owner")`
(reps see own, managers/admins see all) + `@role_required("admin")`; `core.crypto.encrypt/decrypt`.

---

## 8. Phases — what's built (all done)

- **Phase 0** — project foundations (settings, CI, Docker, crypto, base templates).
- **Phase 1 + 1.5** — CRM core (Company/Contact/Stage/Lead/Activity/Task, board, lists, detail,
  tasks) + CSV import (upload, column mapping, async run, audit).
- **Phase 2** — outreach foundation: models (2.1) + factories; template editor with sandboxed
  merge rendering + preview (2.2); **Gmail OAuth + GmailProvider.send (2.3)**; **Outlook OAuth +
  GraphProvider.send (2.4)** (code complete, never connected live); sequence builder +
  single/bulk enrollment with edit-lock-once-enrolled (2.5); sender loop with idempotency +
  retries (2.6); send-window + daily-cap + ±90s jitter + weekday-only guards (2.7); threading so
  steps 2+ reply in-thread (2.8).
- **Phase 3** — engagement: reply detection + auto reply/bounce classification (3.1/3.2);
  open-pixel + click-tracking endpoints (3.3); unsubscribe page + `List-Unsubscribe` headers +
  one-click + suppression-at-dispatch (3.4); hosted capture form + signed webhook intake +
  auto-enroll (3.5); mailbox warmup ramp (3.6).
- **Phase 4** — sources/reporting/ops: Apollo contact enrichment + LinkedIn preset + enrichment
  queue (4.1/4.2); reports — funnel, sequence performance, rep activity, date filter (4.3);
  duplicate-companies report + manual merge (4.4); Render deploy blueprint + Sentry + prod
  hardening (4.5); per-contact GDPR export + delete + perms polish (4.6).
- **UI/UX premium polish** — presentation-only pass (Part B). Brand overhaul → micro-interactions
  → icon/token system. Schema/routes/logic untouched.

State machines to respect (no raw status writes — use the model methods): Enrollment
(active/paused/replied/bounced/unsubscribed/finished; reply & unsubscribe are terminal),
Lead (stage board + won/lost), Message (scheduled/sent/bounced/failed; first-event-wins on
opened/clicked/replied timestamps).

---

## 9. OAuth / mailbox connect

- **Google is configured and working live.** The user provisioned a Google Cloud project, OAuth
  consent screen (with their own email added as a **Test User** — required, otherwise you get
  "Access blocked: app not verified, Error 403"), and a Web OAuth client. The **Client ID +
  Secret live in the gitignored local `.env`** — they are deliberately **not** copied into this
  doc (committed secret = leak). Ask the user if you need them.
- **Redirect URIs** registered: `<BASE_URL>/settings/mailboxes/gmail/callback/` (and the outlook
  equivalent). Locally `BASE_URL=http://localhost:8000`; uncomment `OAUTHLIB_INSECURE_TRANSPORT=1`
  in `.env` so oauthlib accepts the http redirect (never in prod).
- **PKCE gotcha (already fixed, commit `4864205`):** `google-auth-oauthlib` generates a code
  verifier at `authorization_url()` that a fresh Flow at callback time doesn't have →
  "Missing code verifier". Fix: `authorization_url()` now returns `(url, state, code_verifier)`;
  the verifier is stored in `request.session["oauth_code_verifier"]` and passed to
  `exchange_code(..., code_verifier=...)`. Don't regress this.
- **Outlook:** code complete (`GraphProvider`, draft→send flow) but **never connected** — no MS
  Entra app registration / creds yet. Scopes: `Mail.Send` + `Mail.Read` + `offline_access`.
- Provider behavior: refresh token on 401 inside the provider; on refresh-fail (revoked) set
  `mailbox.status='error'`, surface an in-app banner + rep Task. Test-send button delivers a real
  email to the mailbox itself (manual deliverability check).

---

## 10. Hosting / deployment

**Target: Render, via the `render.yaml` Blueprint** (not yet deployed — needs a Render account +
the `sync:false` secrets filled in the dashboard). Runbook: `docs/DEPLOY.md`.

Blueprint provisions five things:
- **Postgres** (`bytedocker-db`, basic-256mb, daily backups).
- **Redis/keyvalue** (`bytedocker-redis`, internal only) — Celery broker.
- **Web** (`bytedocker-web`) — `gunicorn config.wsgi`; build runs `collectstatic`; **preDeploy
  runs `migrate`** (once per deploy, before traffic shifts).
- **Worker** (`bytedocker-worker`) — `celery -A config worker --concurrency 2`.
- **Beat** (`bytedocker-beat`) — `celery -A config beat` with the DatabaseScheduler.

**Env (group `bytedocker`):** `DEBUG=false`, `PYTHON_VERSION=3.12.6`, `SECRET_KEY` (generated),
`CELERY_TASK_ALWAYS_EAGER=false`, plus **secrets to set in the dashboard**:
`FIELD_ENCRYPTION_KEY` (the **same** Fernet value across all 3 services — tokens are
cross-service), `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `BASE_URL` (public https — OAuth
redirects + tracking links), `SENTRY_DSN`, `GOOGLE_CLIENT_ID/SECRET`, `MS_CLIENT_ID/SECRET`,
`MS_TENANT`, `APOLLO_API_KEY`. After deploy, register the prod callback URLs in Google/MS.

**Beat schedule (`config/celery.py`):** `dispatch_due_sends` 60s · `poll_replies` 180s ·
`refresh_expiring_tokens` 1800s · `reset_daily_counters` 3600s · `core.heartbeat` 300s.

**Sentry:** wired in `config/settings.py`, active only when `SENTRY_DSN` is set.

**Local Docker:** `docker-compose.yml` runs web/worker/beat/postgres/redis + a one-shot `migrate`
service the others depend on (`condition: service_completed_successfully`) — keep that dependency
if you add a service.

---

## 11. Conventions to match (so your code looks native)

1. **HTMX house rules (`UI_SPEC §2`):** full page = template extending `base.html`; every
   mutation returns a **partial**. Modals: `hx-get` a partial into `#modal-slot`; form `hx-post`
   returns either the re-rendered partial (validation errors) or **204 + `HX-Trigger`** carrying
   `{toast, close-modal, refresh-<entity>}` via `hx_toast`/`hx_events`. Tables: filter form
   `hx-get` + `hx-push-url="true"` (bookmarkable). The board refreshes itself via a
   `refresh-board from:body` listener that swaps `#board-wrap`.
2. **Permissions in querysets, never template `if`s (`UI_SPEC §5`):** every view scopes via
   `scope_to_user(...)`; settings/admin views get `@role_required("admin")`.
3. **Activities for state changes:** anything a user expects on the contact timeline (enrolled,
   email_sent/opened/clicked/replied/bounced, stage_change, task_done, unsubscribed, note, call,
   import) logs an `Activity` with a typed `payload`. The timeline + `templatetags/ui.py`
   (`activity_icon_name` → SVG, `activity_tone` → color class, `activity_text` → label) render
   these — add a mapping there for any new type, **using a sprite icon, never an emoji**.
4. **Normalization at save:** lowercase emails (`User`/`Contact`/`Mailbox`), normalize domains via
   `pipeline.models.normalize_domain`.
5. **Encryption:** OAuth tokens only ever through `core.crypto`.
6. **Provider isolation:** Google/MS SDK imports live **only** in `outreach/providers/`.
7. **Tests:** `tests/` is a package; add factories to `tests/factories.py`;
   `pytestmark = pytest.mark.django_db` per module; autouse fixtures give crypto + plain static
   storage. No bare `pytest.raises(Exception)` (ruff B017) — name the exception.
8. **Migrations:** ship in the same commit as the model change; migrations are E501-exempt —
   don't hand-reformat them.
9. **No new JS modules** beyond Alpine + SortableJS. **Ask before adding any dependency.**

---

## 12. The Apollo / enrichment seam

- `outreach/enrichment.py` calls Apollo. Key resolution: a **per-team key** (set in the app) > the
  global `APOLLO_API_KEY` env fallback. Blank → Enrich button + queue resolution disabled
  (degrade, don't 500).
- Import rows missing an email land in the **enrichment queue** (`ingestion_enrichmenttask`);
  resolving fills the email + merges fields. LinkedIn preset assists manual enrichment.
- Not exercised against the live Apollo API yet — needs a real key.

---

## 13. Gotchas / lessons already paid for

- **cached template Loader in local preview** (§5) — restart the server to see template edits.
- **stale local sqlite** (§5) — `migrate` after pulling; the test DB masks this.
- **PKCE "Missing code verifier"** (§9) — keep the session-stored verifier.
- **Google "app not verified" 403** — add the tester's email as a Test User on the consent screen.
- **Static manifest in CI:** `DEBUG=false` selects whitenoise's manifest storage (needs
  `collectstatic`); tests dodge it via the `_plain_static_storage` autouse fixture — keep it.
- **Docker beat raced migrations** — fixed by the one-shot `migrate` service dependency.
- **black/ruff target mismatch:** `pyproject` pins black `py313`, ruff `py312`. Leave both.
- **B017** (name the exception) / **B905** (`zip(strict=)`) — ruff will fail CI otherwise.
- **PowerShell:** `$pid` is read-only (rename loop vars); `$null` not `/dev/null`; `$env:VAR`.
- **No emojis** anywhere in the product UI (user directive, §0). Use the SVG icon sprite.

---

## 14. Outstanding / candidate next steps (none are queued — user decides)

- Commit the §0 working-tree changes (board fix + emoji removal + linter edits) when asked.
- Board drag fix is browser-verified (three consecutive moves) — no further action needed there.
- Live-exercise **Apollo enrichment** with a real key.
- **Deploy to Render** (account + secrets) per `docs/DEPLOY.md`.
- Connect a real **Outlook** mailbox (MS Entra app + creds) to exercise `GraphProvider` live.
- Leftover throwaway local admin user `ui-check@bytedocker.com` (pw `ui-check-pass-1`) exists in
  the local sqlite db only — delete if you want a clean local state.

---

# PART B — design system

> The product was taken from a flat, editorial first cut to a **premium, sharp, high-signal**
> internal-SaaS look (Linear + Attio reference points). This is **presentation only** — it
> changed CSS, tokens, template markup/classes, an icon system, and three presentation-only
> template helpers. It changed **no** functionality, business logic, schema, routes, API
> behavior, auth, state, field names, form behavior, validation, filters, sorting, or API calls.

## 15. The user's design directives (verbatim intent — honor all of these)

- Source brief: "Make the CRM feel modern, premium, sharp, and professional… Linear + Attio…
  **PREMIUM, SHARP, MINIMAL, HIGH-SIGNAL**." Earlier brand input came from a company
  **letterhead PDF** (near-black ink on paper, one cobalt accent, Montserrat display type).
- Hard constraints: "Do **NOT** change functionality, business logic, database schema, routes,
  API behavior, auth, state management, or existing data flows." "No changing field names, form
  behavior, validation, filters, sorting, or API calls."
- Motion: add tasteful micro-interactions + a few signature animations; **reduced-motion safe**.
  "Do not add animation libraries unless one is already installed or absolutely necessary.
  **Ask before adding any new dependency.**"
- **"AND NO AI SLOP DESIGN."** / "DO NOT MAKE AN AI SLOP DESIGN." — restraint over decoration;
  no gradient-confetti, no generic dashboard-template look.
- **No emojis** anywhere (§0).
- Process: plan first, implement incrementally keeping the app working, review screen-by-screen
  for cohesion at the end.

## 16. Brand foundation & tokens (`tailwind.config.js`)

- **Color:** near-black ink on near-white paper, **one** electric-cobalt accent.
  - `ink` `#0A0A0A`; `accent` DEFAULT/600 `#2447F0`, 50 `#EEF1FE`, 100 `#DCE3FD`, 700 `#1D39C9`,
    800 `#182E9E`. Body background `#fafafb`. Neutrals are Tailwind's stock scale.
  - Semantic tints: emerald (success/won/replied), amber (warning/warmup), red (lost/bounced/
    unsubscribed), sky (opened), accent (clicked/enrolled).
- **Type:** `display` = **Montserrat** (headings/numbers), `sans` = **Inter** (body). Both
  **self-hosted woff2** in `static/fonts/` (no Google Fonts CDN). Page titles use the display
  font; big numbers use `font-display … tabular-nums`.
- **Elevation (restrained, Linear-leaning, cool slate-tinted, low-spread):**
  `shadow-xs` (resting hairline depth on cards/inputs/buttons), `shadow-card` (hover lift),
  `shadow-pop` (modals/toasts). Depth is reserved for hover/overlays/key cards — not sprayed
  everywhere.
- **Motion easing:** `transitionTimingFunction.premium` = `cubic-bezier(0.16, 1, 0.3, 1)`
  (decelerate-out). Durations 120–240ms. Nothing bouncy or looping.

## 17. Icon system (dependency-free)

- `static/img/icons.svg` — an SVG **sprite** of `<symbol>`s (24-grid, ~1.6px stroke,
  `currentColor` via an embedded `<style>`). Symbols available: `dashboard board leads contacts
  companies tasks imports enrich sequences templates mailbox reports integrations merge settings
  search plus filter download trash pencil reply eye check x chevron-right chevron-down
  arrow-right external alert logout inbox building sparkle`.
- `{% icon "name" class="h-4 w-4" %}` — a `simple_tag` in `pipeline/templatetags/ui.py` emitting
  `<svg class="…"><use href="{static}/img/icons.svg#name"/></svg>`. Pure presentation, reused
  everywhere (nav, buttons, empty states, activity feed). **This replaced all emojis.**
- Activity feed mapping lives in the same file: `activity_icon_name` (type → sprite id),
  `activity_tone` (type → `act-emerald/red/accent/sky/neutral` color class). The old emoji dict
  was deleted (§0).

## 18. Component layer (`static/src/app.css` `@layer components` → built to `static/css/app.css`)

All **existing class names were preserved** — restyled in place so every screen improved at once.
Key classes (use these, don't invent parallel ones):
- Surfaces: `.card` (rounded-xl, hairline border, `shadow-xs`), `.card-hover` (accent-tinted
  border + `shadow-card` + 1px lift), `.table-card`.
- Buttons: `.btn` (rounded-lg, `active:scale-[0.98]` press), `.btn-primary` (accent + resting
  `shadow-xs`), `.btn-secondary`, `.btn-ghost`.
- Forms: `.input` (taller, ring focus + accent border); field scaffolding `.field` /
  `.field-label` / `.field-error` (alert icon + red).
- Page chrome: `.page-header` / `.page-title` (display font) / `.page-subtitle`.
- Tables: `.th` / `.td` (px-4 py-3, `tabular-nums` for numbers), quiet row hover, selected-row
  via `tr:has(input[name="cid"]:checked)`. **Note:** sticky `thead` was deliberately **dropped**
  — it conflicts with the table-card `overflow-hidden`. Don't reintroduce it without solving that.
- Status: `.badge` + dot system + tints `.badge-emerald/amber/red/accent/neutral`. Activity:
  `.act-icon` + `.act-neutral/emerald/red/accent/sky`.
- Nav: `.nav-link` (icon + label), animated active highlight `.nav-active::before` (accent bar).
- States/feedback: `.empty-state` (+ `_empty_state.html` partial: icon badge + title + subtext +
  optional CTA), `.skeleton` (shimmer), `.htmx-progress` top loading bar (`.active`-gated).
- Keyframes (all reduced-motion-guarded): `overlay-in`, `panel-in`, `rise-in`, `fade-in`, `spin`,
  `shimmer`, `progress-slide`; `.stagger > *:nth-child(n)` reveal delays.

## 19. Chrome & screen patterns

- **`base.html`:** dark sidebar with a "B" logo badge + wordmark + a subtle top sheen + per-link
  `{% icon %}` + animated active bar; sticky **blurred** topbar with a user chip (avatar initial +
  email) + a logout icon button; the error-mailbox banner (restyled only); a top htmx loading bar
  (`#htmx-progress`, toggled `.active` on `htmx:beforeRequest`/`afterRequest`, request-counted);
  a refined toast (rounded-xl, `shadow-pop`, ring).
- **Modals** (`pipeline/_modal_form.html`, `outreach/_enroll_modal.html`, `_step_preview.html`):
  `rounded-2xl`, `shadow-pop`, `backdrop-blur`, x-icon close, `panel-in` entrance; **preserve**
  Escape / click-out / `$nextTick` autofocus / all htmx attrs.
- **Lists:** `.page-header` with display title + subtitle + icon action buttons; icon search
  inputs; `.table-card` partials; `_empty_state` includes replacing plain "No X yet" rows.
- **Dashboard:** `.stagger` grid of stat cards (first two are full `card-hover` links to
  board/sequences) with corner icons + **count-up** (`counter()` Alpine, reduced-motion aware);
  activity feed uses `.act-icon` badges + sprite icons.
- **Board:** columns with a stage dot (emerald/red/accent) + count chip; cards with `shadow-xs` +
  hover lift; SortableJS hooks preserved (see the §0 drag fix).
- **Detail:** header card with avatar circle + display name + badge tints + icon action buttons
  (Enrich=sparkle, Export=download, Edit=pencil, Delete=trash); timeline with typed sprite icons.
- **Reports:** funnel bars are `bg-gradient-to-r from-accent to-accent-700` rounded-full.
- **Brand pages** (login, unsubscribe, capture form): tightened brand moments.

## 20. Motion principles

CSS + Alpine only, all behind `motion-safe:` / the global reduced-motion guard: stat-card
**stagger** on load; section reveal (`rise-in`); animated **nav active bar**; **card-hover**
elevation on interactive cards; quiet table-row hover/selection; **skeleton shimmer** + the top
**htmx progress** bar (active-gated so it doesn't loop forever and block screenshots); refined
modal/toast easing. Fast, premium easing, restrained — **no AI slop.**

---

## 21. First moves for the new instance

1. Read §1–§3 and §15. If touching the engine, read the relevant spec (§6) end to end.
2. The app is feature-complete; there's no auto-next-phase. Confirm direction with the user.
3. If asked to commit: include the §0 working-tree changes; run `pytest` (288), `ruff check .`,
   `black --check .`, and `npm run css:build` if any template/CSS changed — all clean — before
   committing. One logical change per commit; push only when asked.
