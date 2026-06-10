# UI Spec — screens, routes, HTMX patterns

> Companion to [PLAN.md](../PLAN.md) §6. Server-rendered Django + HTMX + Alpine + Tailwind.

---

## 1. Route map

| URL | Screen | Notes |
|-----|--------|-------|
| `/` | Dashboard | Funnel snapshot, today's tasks, recent replies, send volume |
| `/board/` | Kanban pipeline | Columns = stages; SortableJS drag → `POST /leads/<id>/move/` |
| `/leads/` | Lead list | Filter: stage, owner, source, staleness; bulk actions |
| `/contacts/` | Contact list | Search + filters; bulk: assign owner, enroll in sequence |
| `/contacts/<id>/` | Contact detail | Profile · timeline · enrollments · tasks (tabbed) |
| `/companies/<id>/` | Company detail | Company fields + its contacts/leads |
| `/sequences/` | Sequence list | Per-row stats: active / replied / finished counts |
| `/sequences/<id>/` | Sequence builder | Step list editor + enrollment table |
| `/templates/` `/templates/<id>/` | Template editor | Side-by-side edit ↔ rendered preview w/ sample contact |
| `/imports/` | Import center | Upload CSV, LinkedIn preset, job history + error downloads |
| `/imports/<id>/map/` | Column mapping | Map CSV columns → fields, preview first 5 rows |
| `/tasks/` | My tasks | Due/overdue, check-off inline |
| `/settings/mailboxes/` | Mailbox connect | OAuth connect buttons, cap/window/tz editors, status badges |
| `/settings/stages/` | Stage editor | Reorder, rename, won/lost flags |
| `/settings/users/` | Users & teams | Admin only |
| `/settings/integrations/` | API keys & webhooks | Enrichment keys, webhook URL + secret display |
| `/reports/` | Reports | Phase 4: funnel conversion, per-sequence/per-rep stats |

Public (no auth): `/t/o/…`, `/t/c/…`, `/unsubscribe/…`, `/ingest/webhook/<token>/`,
`/forms/<slug>/` (hosted capture form).

## 2. HTMX interaction patterns (house rules)

1. **Full page = layout template; every mutating interaction = partial.** Each list/detail
   template splits into `page.html` (extends base) + `_partials/*.html` returned by HTMX.
2. **Modals:** `hx-get` → `<dialog>` partial into `#modal-slot`; form `hx-post` returns either
   the re-rendered partial (validation errors) or `HX-Trigger: refresh-<entity>` + empty 204.
3. **Inline edit:** click field → `hx-get` swap to input → blur/submit `hx-post` swaps back.
4. **Kanban move:** SortableJS `onEnd` → `htmx.ajax('POST', /leads/<id>/move/, {stage, index})`
   → response re-renders the two affected column headers (counts) only.
5. **Toasts:** server sets `HX-Trigger: {"toast": {"level": "...", "msg": "..."}}`; one Alpine
   listener in base layout renders them.
6. **Polling:** dashboard "recent replies" card only — `hx-trigger="every 60s"`. Nothing else
   polls; this is an internal tool, refresh is fine.
7. **Tables:** filter form `hx-get` with `hx-push-url="true"` so filtered views are
   bookmarkable; pagination via `hx-get` on page links targeting the table body.
8. **No custom JS modules** beyond Alpine sprinkles + SortableJS init. If a screen seems to
   need more, the screen is too clever — simplify it.

## 3. Screen notes (the non-obvious ones)

**Kanban board** — card shows contact name, company, value, days-in-stage, tiny
sequence-status icon. Won/lost columns collapsed by default. Column WIP counts in headers.
Filter bar (owner, source) re-renders whole board via `hx-get`.

**Contact detail** — timeline is the spine (Activities, newest first, type icons).
Right rail: enrollment status with "pause / resume / stop" buttons, owner, tasks.
"Enroll in sequence" modal asks: sequence + sending mailbox (defaults to owner's first
active mailbox).

**Sequence builder** — ordered step cards: "Day N · template name · [preview]". Editing
locked once enrollments exist → "Clone to edit" button instead (per DATA_SPEC §3).
Enrollment tab: table with per-status filter chips and bulk pause/stop.

**Import mapping** — auto-guess mapping by header name (email, first name…), preview table
of first 5 parsed rows, dedupe policy displayed (fixed, not configurable in v1), then async
job with progress via `hx-trigger="every 2s"` until terminal, then summary + errors.csv.

**Mailbox settings** — connect = OAuth redirect flow; status badge (active/error) with
"reconnect" CTA on error; shows sends-today vs cap as a meter.

## 4. Layout & navigation

- Left sidebar nav (Dashboard / Board / Contacts / Leads / Sequences / Templates / Imports /
  Tasks / Reports / Settings), topbar with global search (`/contacts?q=`) and user menu.
- Tailwind, light theme only in v1. Density: compact tables — this is a working tool.
- Empty states everywhere ("No leads yet — import a CSV") linking to the action.

## 5. Permissions in the UI

| Role | Sees |
|------|------|
| rep | Own leads/contacts/tasks; sequences shared with team; own mailboxes |
| manager | Everything reps see + team-wide views + all sequences |
| admin | Everything + Settings |

Enforced in querysets (not template `if`s) — a rep's `/leads/` is filtered server-side.
