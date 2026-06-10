# Data Spec — field-level schema, constraints, state machines

> Companion to [PLAN.md](../PLAN.md) §4. This is the contract the Django models implement.
> Conventions: all tables get `id` (BigAutoField), `created_at`, `updated_at`. All timestamps
> stored **UTC**; per-mailbox send windows are evaluated in the mailbox's local timezone.
>
> **Amendment (Phase 0):** `citext` columns are replaced by **lowercase normalization at
> save time** (Django 5.x removed `CITextField`; §5's normalization rules already require
> lowercasing). Wherever this doc says `citext`, read: EmailField/CharField + unique index,
> values normalized to lowercase in the model's manager/save path.

---

## 1. accounts

### User
| Field | Type | Notes |
|-------|------|-------|
| email | citext, unique | Login identifier (custom user model from day one — hard to retrofit) |
| name | varchar(120) | |
| role | enum: `admin` / `manager` / `rep` | Single role, no per-object ACLs at this scale |
| team | FK Team, null | |
| is_active | bool | Django convention |

### Team
| Field | Type | Notes |
|-------|------|-------|
| name | varchar(80), unique | |

### Mailbox
| Field | Type | Notes |
|-------|------|-------|
| user | FK User, on_delete=CASCADE | One user may connect several mailboxes |
| provider | enum: `gmail` / `outlook` | |
| email | citext, unique | The sending address |
| oauth_token | text, **Fernet-encrypted** | Access + refresh token JSON blob; key from `FIELD_ENCRYPTION_KEY` env |
| history_cursor | varchar(64), null | Gmail `historyId` / Graph delta token for reply polling |
| daily_cap | int, default 100 | Hard ceiling on sends per UTC-day reset at mailbox-local midnight |
| sends_today | int, default 0 | Counter, reset by beat job (DB counter is fine at small scale; move to Redis if mid-scale) |
| send_window_start / end | time, default 08:00 / 18:00 | Mailbox-local |
| timezone | varchar(40), default `UTC` | IANA name |
| status | enum: `active` / `paused` / `error` | `error` set on token revocation; pauses all its enrollments' sends |

---

## 2. pipeline

### Company
| Field | Type | Notes |
|-------|------|-------|
| name | varchar(200) | |
| domain | citext, unique-nullable | Normalized: lowercase, strip `www.` and scheme. Dedupe key #2 |
| industry / size / location | varchar, null | |
| custom_fields | JSONB, default `{}` | Free-form; admin defines suggested keys in settings |

### Contact
| Field | Type | Notes |
|-------|------|-------|
| company | FK Company, null, on_delete=SET_NULL | |
| first_name / last_name | varchar(80) | |
| email | citext, **unique** | Dedupe key #1. Normalize: trim + lowercase. **No** plus-tag stripping (b2b addresses rarely use it; stripping causes false merges) |
| title / phone / linkedin_url | varchar, null | |
| source | varchar(60) | e.g. `csv:2026-06-10-apollo.csv`, `webhook:landing`, `manual` |
| owner | FK User, null | |
| custom_fields | JSONB, default `{}` | |
| unsubscribed_at | datetime, null | Null = subscribed. Timestamp beats bool: it's the audit record |
| bounced_at | datetime, null | Same pattern |

Index: `(owner, created_at)`, plus the unique email index.

### Stage
| Field | Type | Notes |
|-------|------|-------|
| name | varchar(60) | |
| order | smallint | Board column order |
| is_won / is_lost | bool | At most one of each enforced in clean() |

Seed default set in a data migration: New → Contacted → Engaged → Qualified → Meeting → Won / Lost.

### Lead
| Field | Type | Notes |
|-------|------|-------|
| contact | FK Contact, on_delete=CASCADE | |
| stage | FK Stage, on_delete=PROTECT | |
| owner | FK User, null | |
| source | varchar(60) | Copied from contact at creation; they can diverge |
| value | decimal(12,2), null | |
| status | enum: `open` / `won` / `lost` | Denormalized from stage flags for cheap filtering; set when moved into a won/lost stage |
| last_activity_at | datetime, null | Bumped by Activity writes; drives "stale lead" views |

Constraint: **one open Lead per Contact** (partial unique index on `contact` where `status='open'`).

### Activity
| Field | Type | Notes |
|-------|------|-------|
| contact | FK Contact, on_delete=CASCADE | Always set |
| lead | FK Lead, null | |
| type | enum: `email_sent` / `email_opened` / `email_clicked` / `email_replied` / `email_bounced` / `note` / `call` / `stage_change` / `task_done` / `enrolled` / `unsubscribed` / `import` | |
| payload | JSONB | Type-specific: e.g. stage_change `{from, to}`, email_* `{message_id, subject}` |
| actor | FK User, null | Null = system/engine |
| ts | datetime, default now | |

Index: `(contact, ts DESC)` — the timeline query. Append-only; never updated.

### Task
| Field | Type | Notes |
|-------|------|-------|
| lead | FK Lead, on_delete=CASCADE | |
| owner | FK User | |
| title | varchar(200) | |
| due_at | datetime | |
| done_at | datetime, null | |

---

## 3. outreach

### EmailTemplate
| Field | Type | Notes |
|-------|------|-------|
| name | varchar(120) | |
| subject | varchar(300) | Merge fields allowed |
| body_html / body_text | text | body_text auto-derived if blank (html2text) |

**Merge fields (v1, closed set):** `{{first_name}}` `{{last_name}}` `{{full_name}}`
`{{company}}` `{{title}}` `{{sender_name}}` — rendered with Django's template engine in a
**sandboxed context** (no object access, only this flat dict). Unknown field → render-time
validation error at template save, never a broken send. Optional fallback syntax:
`{{first_name|there}}`.

### Sequence
| Field | Type | Notes |
|-------|------|-------|
| name | varchar(120) | |
| owner | FK User | |
| is_active | bool | Deactivating pauses new sends but keeps enrollments' state |

### SequenceStep
| Field | Type | Notes |
|-------|------|-------|
| sequence | FK, on_delete=CASCADE | |
| order | smallint | unique with sequence |
| wait_days | smallint | Days after the *previous* step (step 1: after enrollment). v1 unit is days; window logic handles the time-of-day |
| template | FK EmailTemplate, on_delete=PROTECT | |

v1 scope cut: **email channel only** — no `channel` column until a second channel exists.
Steps are editable only while no active enrollments reference the sequence (else: clone).

### Enrollment
| Field | Type | Notes |
|-------|------|-------|
| contact | FK Contact, on_delete=CASCADE | |
| sequence | FK Sequence, on_delete=CASCADE | |
| mailbox | FK Mailbox, on_delete=PROTECT | The sending identity, fixed at enrollment |
| current_step | smallint, default 0 | 0 = not yet sent step 1 |
| status | enum — see state machine below | |
| next_send_at | datetime, null | Null unless status=active |
| enrolled_by | FK User | |

Constraints: partial unique `(contact, sequence)` where status in (`active`,`paused`) — no
double-enrollment. **Hot index:** `(status, next_send_at)` — the sender-loop query.

### Message
| Field | Type | Notes |
|-------|------|-------|
| uuid | uuid, unique | Public identifier for tracking endpoints (never expose pk) |
| enrollment | FK, on_delete=CASCADE | |
| step | FK SequenceStep, on_delete=PROTECT | |
| mailbox | FK Mailbox, on_delete=PROTECT | |
| provider_message_id | varchar(120), indexed | Gmail message id / Graph id |
| thread_id | varchar(120), indexed | Reply-matching key |
| subject_rendered | varchar(300) | Snapshot — templates change later |
| status | enum: `scheduled` / `sent` / `bounced` / `failed` | |
| sent_at / opened_at / clicked_at / replied_at | datetime, null | First-event timestamps; repeat events only append Activities |

---

## 4. State machines

### Enrollment.status
```
                ┌────────────────────────────────────────────┐
                │                                            ▼
  active ──────▶ paused ──────▶ active          (manual pause/resume)
    │
    ├── reply detected ────────────▶ replied     (terminal; rep takes over)
    ├── bounce detected ───────────▶ bounced     (terminal; contact.bounced_at set)
    ├── contact unsubscribes ──────▶ unsubscribed(terminal)
    └── last step sent ────────────▶ finished    (terminal)
```
Terminal states never resume automatically; re-engaging means a new enrollment.

### Lead.status
`open → won` or `open → lost` (via drag into a won/lost stage). Reopening = manual stage
move back, which resets status to `open` and logs an Activity.

### Message.status
`scheduled → sent → (bounced)` ; `scheduled → failed` (send error after retries).
`opened/clicked/replied` are timestamps, not statuses — a bounced message can still have
`opened_at` from a privacy proxy; statuses stay honest.

---

## 5. Dedupe & normalization rules (ingestion contract)

1. **Email** — trim, lowercase → exact match on `Contact.email` ⇒ same contact: update
   blank fields only (never overwrite populated ones on import), log `import` Activity.
2. **Company domain** — normalize (lowercase, strip scheme/`www.`) → match ⇒ attach
   contact to the existing company.
3. **No fuzzy name matching in v1.** False merges are worse than duplicates; flag
   "possible duplicate companies" in a report view instead.
4. Imports are **idempotent**: re-uploading the same CSV creates nothing new.
5. Every import run gets an `ImportJob` row (`ingestion` app): filename, mapping JSON,
   counts (created/updated/skipped/errored), errors CSV download.
