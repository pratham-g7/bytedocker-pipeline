from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.reporting import funnel_report, rep_report, sequence_report
from outreach.models import Message
from pipeline.models import Lead, Stage

from .factories import (
    ContactFactory,
    EnrollmentFactory,
    LeadFactory,
    MessageFactory,
    SequenceStepFactory,
    TaskFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


@pytest.fixture
def manager(db):
    return UserFactory(email="manager@x.com", role="manager")


def _today_range():
    today = timezone.localdate()
    return today - timedelta(days=30), today + timedelta(days=1)


# ---------------------------------------------------------------- funnel


def test_funnel_narrows_and_counts_outcomes(manager):
    new = Stage.objects.get(name="New")
    engaged = Stage.objects.get(name="Engaged")
    won = Stage.objects.get(is_won=True)
    lost = Stage.objects.get(is_lost=True)
    LeadFactory(stage=new)
    LeadFactory(stage=engaged)
    LeadFactory(stage=won, status=Lead.Status.WON)
    LeadFactory(stage=lost, status=Lead.Status.LOST)
    start, end = _today_range()

    report = funnel_report(manager, start, end)

    rows = {s["name"]: s["count"] for s in report["stages"]}
    assert rows["New"] == 3  # new + engaged + won reached "New" (lost excluded)
    assert rows["Engaged"] == 2  # engaged + won
    assert report["won"] == 1
    assert report["lost"] == 1
    assert report["win_rate"] == 50  # 1 won / (1 won + 1 lost)
    assert report["stages"][0]["pct"] == 100  # top of funnel


def test_funnel_scoped_to_rep(rep, manager):
    LeadFactory(contact=ContactFactory(owner=rep), owner=rep)
    LeadFactory(contact=ContactFactory(owner=manager), owner=manager)
    start, end = _today_range()
    assert funnel_report(rep, start, end)["total"] == 1
    assert funnel_report(manager, start, end)["total"] == 2


def test_funnel_respects_date_range(manager):
    old = LeadFactory()
    Lead.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=90))
    start, end = _today_range()
    assert funnel_report(manager, start, end)["total"] == 0


# ---------------------------------------------------------------- sequences


def test_sequence_rates(manager):
    enrollment = EnrollmentFactory()
    SequenceStepFactory(sequence=enrollment.sequence, order=1)
    now = timezone.now()
    # 4 sent, 3 opened, 1 clicked, 2 replied
    for i in range(4):
        msg = MessageFactory(enrollment=enrollment, status=Message.Status.SENT, sent_at=now)
        Message.objects.filter(pk=msg.pk).update(
            opened_at=now if i < 3 else None,
            clicked_at=now if i < 1 else None,
            replied_at=now if i < 2 else None,
        )
    start, end = _today_range()

    rows = sequence_report(manager, start, end)
    row = next(r for r in rows if r["name"] == enrollment.sequence.name)
    assert row["sent"] == 4
    assert row["open_rate"] == 75
    assert row["click_rate"] == 25
    assert row["reply_rate"] == 50


def test_sequence_with_no_sends_omitted(manager):
    EnrollmentFactory()  # no messages
    start, end = _today_range()
    assert sequence_report(manager, start, end) == []


# ---------------------------------------------------------------- reps


def test_rep_activity_counts(manager):
    rep_user = UserFactory(email="r@x.com", role="rep")
    enrollment = EnrollmentFactory(enrolled_by=rep_user)
    now = timezone.now()
    msg = MessageFactory(enrollment=enrollment, status=Message.Status.SENT, sent_at=now)
    Message.objects.filter(pk=msg.pk).update(replied_at=now)
    task = TaskFactory(owner=rep_user)
    task.done_at = now
    task.save()
    start, end = _today_range()

    rows = rep_report(manager, start, end)
    row = next(r for r in rows if r["name"] == rep_user.email or r["name"] == rep_user.name)
    assert row["sent"] == 1
    assert row["replies"] == 1
    assert row["tasks_done"] == 1


def test_rep_sees_only_self(rep):
    other = UserFactory(email="other@x.com", role="rep")
    EnrollmentFactory(enrolled_by=other)
    start, end = _today_range()
    rows = rep_report(rep, start, end)
    assert all(r["name"] in (rep.name, rep.email) for r in rows)


# ---------------------------------------------------------------- view


def test_reports_page_renders(client, manager):
    client.force_login(manager)
    response = client.get(reverse("reports"))
    assert response.status_code == 200
    assert b"Pipeline funnel" in response.content
    assert b"Sequence performance" in response.content


def test_reports_date_filter_parsed(client, manager):
    client.force_login(manager)
    response = client.get(reverse("reports"), {"from": "2026-01-01", "to": "2026-01-31"})
    assert response.status_code == 200
    assert response.context["from_date"].isoformat() == "2026-01-01"
    assert response.context["to_date"].isoformat() == "2026-01-31"


def test_reports_swaps_reversed_dates(client, manager):
    client.force_login(manager)
    response = client.get(reverse("reports"), {"from": "2026-02-01", "to": "2026-01-01"})
    assert response.context["from_date"].isoformat() == "2026-01-01"  # swapped
