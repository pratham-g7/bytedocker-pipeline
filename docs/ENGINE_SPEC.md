# Outreach Engine Spec — algorithms, edge cases, failure modes

> Companion to [PLAN.md](../PLAN.md) §7/§9. The Celery side of the system.

---

## 1. Sender loop

Beat schedule: every 60s → `outreach.tasks.dispatch_due_sends`.

```
dispatch_due_sends():
    due = Enrollment.objects
            .filter(status='active', next_send_at__lte=now)
            .select_for_update(skip_locked=True)        # no double-send across workers
            .order_by('next_send_at')[:BATCH]           # BATCH=50 at small scale
    for e in due (inside transaction):
        mailbox = e.mailbox
        if mailbox.status != 'active':            skip (leave next_send_at; retried next tick)
        if not within_send_window(mailbox, now):  e.next_send_at = window_open(mailbox); continue
        if mailbox.sends_today >= daily_cap:      e.next_send_at = next_window_open(mailbox); continue
        if e.contact.unsubscribed_at or e.contact.bounced_at:
            e.status = terminal state; continue
        send_step.delay(e.id, e.current_step + 1)  # actual send is its own task
```

`send_step(enrollment_id, step_no)` — idempotency guard first:

```
send_step():
    if Message.objects.filter(enrollment=e, step__order=step_no).exists(): return  # already sent
    msg = Message(status='scheduled', uuid=uuid4(), subject_rendered=render(subject))
    html = render(template, contact_ctx)
    html = wrap_links(html, msg.uuid) + open_pixel(msg.uuid) + unsubscribe_footer(contact)
    provider = GmailProvider(mailbox) | GraphProvider(mailbox)
    ids = provider.send(to, subject, html, text)        # threading: same provider thread if step>1
    msg.provider_message_id, msg.thread_id = ids; msg.status='sent'; msg.sent_at=now
    mailbox.sends_today += 1   (F() expression)
    Activity(type='email_sent', ...)
    if step_no == last:  e.status='finished'
    else:                e.current_step=step_no; e.next_send_at = now + next_step.wait_days
```

**Send retries:** Celery `autoretry_for=(TransientProviderError,)`, exponential backoff,
max 3; then `Message.status='failed'`, enrollment **paused** + rep Task created ("send
failed — investigate"). Never auto-skip a step silently.

**Threading:** steps 2+ send with `In-Reply-To`/`References` of step 1's message id and the
same subject (`Re: …`) so the sequence reads as one conversation in the prospect's inbox.

## 2. Provider abstraction

```python
class MailProvider(Protocol):
    def send(to, subject, html, text, thread_ref=None) -> (provider_message_id, thread_id)
    def fetch_new_messages(cursor) -> (messages, new_cursor)   # reply polling
    def refresh_token() -> None
```
Implementations: `GmailProvider` (Gmail API, `users.messages.send`, `users.history.list`),
`GraphProvider` (Graph `/sendMail`, delta query on Inbox). All provider calls live behind
this interface — nothing outside `outreach/providers/` imports Google/Microsoft SDKs.

**OAuth scopes (minimal):**
- Gmail: `gmail.send` + `gmail.readonly` (reply detection needs read)
- Graph: `Mail.Send` + `Mail.Read`, offline_access

**Token lifecycle:** refresh on 401 inside the provider; if refresh fails (revoked) →
`mailbox.status='error'`, notify owner (in-app banner + Task), all its enrollments stay
`active` but the dispatcher skips them until reconnected.

## 3. Reply detection

Beat: every 3 min → `poll_replies` fans out one task per active mailbox.

```
poll_mailbox_replies(mailbox):
    msgs, cursor = provider.fetch_new_messages(mailbox.history_cursor)
    for m in msgs:
        match = Message.objects.filter(thread_id=m.thread_id).first()
        if not match: continue
        if is_auto_reply(m): log Activity(note='auto-reply'), continue
        if is_bounce(m):     handle_bounce(match);            continue
        match.replied_at = now
        enrollment → status='replied'
        lead → advance to 'Engaged' stage if currently earlier   (configurable toggle)
        Task(owner=lead.owner, title='Reply from {contact} — follow up', due=+1 day)
        Activity(type='email_replied', payload={snippet})
    mailbox.history_cursor = cursor
```

**Auto-reply heuristics (v1):** headers `Auto-Submitted: auto-*`, `X-Autoreply`,
`X-Autorespond`, `Precedence: bulk/auto_reply`, subject prefixes ("Out of Office",
"Automatic reply"). Auto-replies do **not** pause the sequence.

**Bounce detection (v1):** sender `mailer-daemon@`/`postmaster@` or
`Content-Type: multipart/report; report-type=delivery-status` in the thread →
`Message.status='bounced'`, `contact.bounced_at=now`, enrollment `bounced`, Activity logged.

**Cursor safety:** cursor only advances after the batch commits; a crash re-processes
messages, and reply/bounce handling is idempotent (timestamps set once).

## 4. Tracking endpoints

| Route | Behavior |
|-------|----------|
| `GET /t/o/<msg_uuid>.gif` | Set `opened_at` if null, append Activity, return cached 1×1 transparent GIF. Unknown uuid → still return the GIF (no probing oracle) |
| `GET /t/c/<msg_uuid>/<sig>/?u=<url>` | Verify HMAC sig over (uuid, url); set `clicked_at`, Activity, 302 → url. Bad sig → 404 |
| `GET /unsubscribe/<signed_token>/` | Token = signed contact id (Django `signing`, no expiry). One-click confirm page → `unsubscribed_at=now`, terminal-ize enrollments, Activity |

Outbound mail also sets **`List-Unsubscribe`** + `List-Unsubscribe-Post` headers (Gmail/
Yahoo bulk-sender requirements; cheap deliverability insurance even at low volume).

**Honesty note:** Apple MPP / Gmail proxies auto-fire pixels. Opens are directional;
dashboards should rank replies > clicks > opens and label opens "approx."

## 5. Other scheduled jobs

| Job | Cadence | Notes |
|-----|---------|-------|
| `dispatch_due_sends` | 1 min | §1 |
| `poll_replies` | 3 min | §3 |
| `refresh_expiring_tokens` | 30 min | Proactive refresh < 10 min to expiry |
| `reset_daily_counters` | hourly | Resets `sends_today` for mailboxes whose local midnight passed since last reset (per-tz correctness without a per-tz cron) |
| `bump_stale_leads` | daily | Optional: Task for leads with no activity ≥ N days |

## 6. Failure-mode table

| Failure | Behavior |
|---------|----------|
| Worker dies mid-send | `select_for_update(skip_locked)` lock released on rollback; idempotency guard in `send_step` prevents duplicate if the send actually left |
| Provider 429/5xx | Retry w/ backoff ×3 → pause enrollment + Task |
| Token revoked | Mailbox `error`, sends skipped, owner notified; nothing lost |
| Redis down | Beat/worker stall; web app unaffected; sends resume on recovery (all state in Postgres, queue is stateless dispatch) |
| Duplicate webhook/poll delivery | Idempotent handlers (first-timestamp-wins) |
| Template renders empty body | Validation at save + pre-send guard refuses empty body, pauses enrollment + Task |
| Contact unsubscribes mid-sequence | Dispatcher checks at send time — last-moment suppression |

## 7. Deliverability guardrails (small-scale defaults)

- daily_cap default **100/mailbox** (well under Gmail's ~500 and Workspace's 2 000)
- Send window 08:00–18:00 mailbox-local, weekdays only (v1: hardcoded weekday rule, flag to disable)
- **Jitter:** ±90 s random delay per send so timing isn't metronomic
- Plain-text alternative always included; unsubscribe link always present
- New-mailbox soft warmup: cap starts at 20/day, +20/day until configured cap (toggleable)
