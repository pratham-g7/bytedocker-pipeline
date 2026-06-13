"""Report aggregations (BACKLOG 4.3) — pure functions over (user, date range).

Kept out of the view so each metric is unit-testable. Everything is scoped:
reps see only their own rows; managers/admins see all (UI_SPEC §5). Date bounds
are inclusive dates compared against the local date of each timestamp.
"""

from django.db.models import Q

from accounts.models import User
from core.permissions import scope_to_user
from outreach.models import Message, Sequence, scope_sequences
from pipeline.models import Contact, Lead, Stage, Task


def _pct(numerator: int, denominator: int) -> int:
    return round(100 * numerator / denominator) if denominator else 0


def funnel_report(user, start, end) -> dict:
    """Leads created in range, as a narrowing funnel of stages reached + outcomes."""
    leads = scope_to_user(Lead.objects.select_related("stage"), user).filter(
        created_at__date__range=(start, end)
    )
    open_stages = Stage.objects.filter(is_won=False, is_lost=False).order_by("order")
    reached = []
    for stage in open_stages:
        count = (
            leads.exclude(status=Lead.Status.LOST)
            .filter(Q(stage__order__gte=stage.order) | Q(status=Lead.Status.WON))
            .count()
        )
        reached.append((stage.name, count))
    top = reached[0][1] if reached else 0
    won = leads.filter(status=Lead.Status.WON).count()
    lost = leads.filter(status=Lead.Status.LOST).count()
    return {
        "stages": [{"name": n, "count": c, "pct": _pct(c, top)} for n, c in reached],
        "total": leads.count(),
        "won": won,
        "lost": lost,
        "win_rate": _pct(won, won + lost),
    }


def sequence_report(user, start, end) -> list[dict]:
    """Per-sequence send volume + open/click/reply rates (opens are approximate)."""
    rows = []
    for sequence in scope_sequences(Sequence.objects, user).order_by("name"):
        messages = Message.objects.filter(
            enrollment__sequence=sequence, sent_at__date__range=(start, end)
        )
        sent = messages.count()
        if not sent:
            continue
        rows.append(
            {
                "name": sequence.name,
                "sent": sent,
                "open_rate": _pct(messages.filter(opened_at__isnull=False).count(), sent),
                "click_rate": _pct(messages.filter(clicked_at__isnull=False).count(), sent),
                "reply_rate": _pct(messages.filter(replied_at__isnull=False).count(), sent),
            }
        )
    return rows


def rep_report(user, start, end) -> list[dict]:
    """Per-rep activity in range. Reps see only themselves."""
    if user.role == "rep":
        reps = [user]
    else:
        reps = list(User.objects.filter(is_active=True).order_by("email"))
    rows = []
    for rep in reps:
        sent = Message.objects.filter(
            enrollment__enrolled_by=rep, sent_at__date__range=(start, end)
        ).count()
        replies = Message.objects.filter(
            enrollment__enrolled_by=rep, replied_at__date__range=(start, end)
        ).count()
        tasks_done = Task.objects.filter(owner=rep, done_at__date__range=(start, end)).count()
        new_contacts = Contact.objects.filter(
            owner=rep, created_at__date__range=(start, end)
        ).count()
        if sent or replies or tasks_done or new_contacts:
            rows.append(
                {
                    "name": rep.name or rep.email,
                    "new_contacts": new_contacts,
                    "sent": sent,
                    "replies": replies,
                    "tasks_done": tasks_done,
                }
            )
    return rows
