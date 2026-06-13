import json
import time
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from outreach import tasks
from outreach.models import Enrollment, Mailbox, Message
from outreach.providers.base import ParsedMessage
from outreach.providers.gmail import GmailProvider
from outreach.providers.graph import GraphProvider
from outreach.replies import is_auto_reply, is_bounce
from pipeline.models import Activity, Stage
from pipeline.models import Task as PipelineTask

from .factories import (
    ContactFactory,
    EnrollmentFactory,
    LeadFactory,
    MailboxFactory,
    MessageFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


def _parsed(thread_id="thr-1", from_addr="cto@acme.com", subject="Re: quick question", **kw):
    return ParsedMessage(
        provider_message_id=kw.pop("pid", "inbound-1"),
        thread_id=thread_id,
        from_addr=from_addr,
        subject=subject,
        snippet=kw.pop("snippet", "Sounds interesting, let's talk."),
        headers=kw.pop("headers", {}),
    )


def _sent_enrollment(thread_id="thr-1", with_lead=True, **contact_kwargs):
    """An enrollment with one sent Message (so an inbound thread can match it)."""
    contact = ContactFactory(**contact_kwargs)
    if with_lead:
        LeadFactory(contact=contact, owner=contact.owner)
    enrollment = EnrollmentFactory(contact=contact)
    MessageFactory(
        enrollment=enrollment,
        thread_id=thread_id,
        provider_message_id="pm-1",
        status=Message.Status.SENT,
        sent_at=timezone.now(),
    )
    return enrollment


@pytest.fixture
def fake_provider(monkeypatch):
    provider = MagicMock()

    def configure(messages, cursor="cursor-2"):
        provider.fetch_new_messages.return_value = (messages, cursor)
        return provider

    monkeypatch.setattr(tasks, "get_provider", lambda mailbox: provider)
    provider.configure = configure
    return provider


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "headers,subject",
    [
        ({"auto-submitted": "auto-replied"}, "Re: hi"),
        ({"x-autoreply": "yes"}, "Re: hi"),
        ({"precedence": "bulk"}, "Re: hi"),
        ({}, "Out of Office: back Monday"),
        ({}, "Automatic reply: traveling"),
    ],
)
def test_auto_reply_detected(headers, subject):
    assert is_auto_reply(_parsed(headers=headers, subject=subject))


@pytest.mark.parametrize(
    "headers,subject",
    [
        ({"auto-submitted": "no"}, "Re: quick question"),
        ({}, "Re: your email"),
        ({"precedence": "list"}, "Re: hi"),
    ],
)
def test_genuine_reply_not_auto(headers, subject):
    assert not is_auto_reply(_parsed(headers=headers, subject=subject))


@pytest.mark.parametrize(
    "from_addr,headers",
    [
        ("mailer-daemon@googlemail.com", {}),
        ("postmaster@acme.com", {}),
        ("x@y.com", {"content-type": "multipart/report; report-type=delivery-status"}),
    ],
)
def test_bounce_detected(from_addr, headers):
    assert is_bounce(_parsed(from_addr=from_addr, headers=headers))


def test_genuine_reply_not_bounce():
    assert not is_bounce(_parsed(from_addr="cto@acme.com", headers={"content-type": "text/plain"}))


# ---------------------------------------------------------------- reply handling


def test_reply_marks_replied_advances_stage_and_creates_task(fake_provider):
    enrollment = _sent_enrollment()
    fake_provider.configure([_parsed()])

    handled = tasks.poll_mailbox_replies(enrollment.mailbox_id)

    assert handled == 1
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.REPLIED
    message = Message.objects.get(enrollment=enrollment)
    assert message.replied_at is not None
    # lead advanced New → Engaged
    lead = enrollment.contact.open_lead
    assert lead.stage.name == "Engaged"
    # follow-up Task for the lead owner, due ~tomorrow
    task = PipelineTask.objects.get(lead=lead)
    assert "follow up" in task.title
    assert task.due_at > timezone.now()
    # timeline records the reply
    assert Activity.objects.filter(
        contact=enrollment.contact, type=Activity.Type.EMAIL_REPLIED
    ).exists()


def test_reply_does_not_regress_a_later_stage(fake_provider):
    enrollment = _sent_enrollment()
    lead = enrollment.contact.open_lead
    qualified = Stage.objects.get(name="Qualified")  # later than Engaged
    lead.move_to(qualified)
    fake_provider.configure([_parsed()])

    tasks.poll_mailbox_replies(enrollment.mailbox_id)

    lead.refresh_from_db()
    assert lead.stage.name == "Qualified"  # reply never moves a lead backwards


def test_stage_advance_can_be_disabled(fake_provider, settings):
    settings.REPLY_ADVANCES_STAGE = False
    enrollment = _sent_enrollment()
    fake_provider.configure([_parsed()])

    tasks.poll_mailbox_replies(enrollment.mailbox_id)

    assert enrollment.contact.open_lead.stage.name == "New"
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.REPLIED  # still marked replied


def test_reply_without_open_lead_still_marks_replied(fake_provider):
    enrollment = _sent_enrollment(with_lead=False)
    fake_provider.configure([_parsed()])

    tasks.poll_mailbox_replies(enrollment.mailbox_id)

    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.REPLIED
    assert not PipelineTask.objects.exists()  # no lead to hang a task on


# ---------------------------------------------------------------- auto-reply / bounce


def test_auto_reply_notes_without_pausing(fake_provider):
    enrollment = _sent_enrollment()
    fake_provider.configure([_parsed(subject="Out of Office: back next week")])

    tasks.poll_mailbox_replies(enrollment.mailbox_id)

    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE  # sequence keeps going
    assert Activity.objects.filter(
        contact=enrollment.contact, type=Activity.Type.NOTE, payload__auto_reply_id="inbound-1"
    ).exists()


def test_bounce_terminalizes_and_sets_contact_bounced_at(fake_provider):
    enrollment = _sent_enrollment()
    fake_provider.configure([_parsed(from_addr="mailer-daemon@googlemail.com")])

    tasks.poll_mailbox_replies(enrollment.mailbox_id)

    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.BOUNCED
    assert Message.objects.get(enrollment=enrollment).status == Message.Status.BOUNCED
    enrollment.contact.refresh_from_db()
    assert enrollment.contact.bounced_at is not None
    assert Activity.objects.filter(
        contact=enrollment.contact, type=Activity.Type.EMAIL_BOUNCED
    ).exists()


# ---------------------------------------------------------------- matching & cursor


def test_own_echo_is_skipped(fake_provider):
    enrollment = _sent_enrollment()
    own = _parsed(from_addr=enrollment.mailbox.email)
    fake_provider.configure([own])

    handled = tasks.poll_mailbox_replies(enrollment.mailbox_id)

    assert handled == 0
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE


def test_unmatched_thread_is_skipped(fake_provider):
    enrollment = _sent_enrollment(thread_id="ours")
    fake_provider.configure([_parsed(thread_id="someone-elses")])

    handled = tasks.poll_mailbox_replies(enrollment.mailbox_id)

    assert handled == 0
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE


def test_cursor_advances_after_batch(fake_provider):
    enrollment = _sent_enrollment()
    fake_provider.configure([_parsed()], cursor="history-999")

    tasks.poll_mailbox_replies(enrollment.mailbox_id)

    enrollment.mailbox.refresh_from_db()
    assert enrollment.mailbox.history_cursor == "history-999"


def test_replay_is_idempotent(fake_provider):
    enrollment = _sent_enrollment()
    fake_provider.configure([_parsed()])

    tasks.poll_mailbox_replies(enrollment.mailbox_id)
    tasks.poll_mailbox_replies(enrollment.mailbox_id)  # crash-replay: same batch again

    assert PipelineTask.objects.count() == 1  # not duplicated
    assert (
        Activity.objects.filter(
            contact=enrollment.contact, type=Activity.Type.EMAIL_REPLIED
        ).count()
        == 1
    )


def test_auth_error_leaves_cursor_untouched(fake_provider):
    from outreach.providers.base import ProviderAuthError

    enrollment = _sent_enrollment()
    enrollment.mailbox.history_cursor = "before"
    enrollment.mailbox.save()
    fake_provider.fetch_new_messages.side_effect = ProviderAuthError("revoked")

    handled = tasks.poll_mailbox_replies(enrollment.mailbox_id)

    assert handled == 0
    enrollment.mailbox.refresh_from_db()
    assert enrollment.mailbox.history_cursor == "before"


# ---------------------------------------------------------------- fan-out


def test_poll_replies_fans_out_per_connected_mailbox(monkeypatch):
    connected = MailboxFactory(user=UserFactory())
    connected.token = '{"access_token": "a"}'
    connected.save()
    MailboxFactory(user=UserFactory(), status=Mailbox.Status.ERROR)  # skipped
    MailboxFactory(user=UserFactory())  # no token → skipped

    queued = []
    monkeypatch.setattr(
        tasks.poll_mailbox_replies, "delay", lambda mailbox_id: queued.append(mailbox_id)
    )

    result = tasks.poll_replies()

    assert result == 1
    assert queued == [connected.pk]


# ---------------------------------------------------------------- provider parsing


def test_gmail_first_poll_records_baseline(monkeypatch):
    provider = GmailProvider(MailboxFactory(provider=Mailbox.Provider.GMAIL))
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "12345"}
    monkeypatch.setattr(GmailProvider, "_service", lambda self: service)

    messages, cursor = provider.fetch_new_messages("")

    assert messages == []
    assert cursor == "12345"  # baseline only — replies match from the next tick


def test_gmail_parses_inbox_message(monkeypatch):
    provider = GmailProvider(MailboxFactory(provider=Mailbox.Provider.GMAIL))
    service = MagicMock()
    users = service.users.return_value
    users.history.return_value.list.return_value.execute.return_value = {
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]}],
    }
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m1",
        "threadId": "thr-1",
        "snippet": "sure, let's talk",
        "payload": {
            "headers": [
                {"name": "From", "value": "cto@acme.com"},
                {"name": "Subject", "value": "Re: hi"},
            ]
        },
    }
    monkeypatch.setattr(GmailProvider, "_service", lambda self: service)

    messages, cursor = provider.fetch_new_messages("100")

    assert cursor == "200"
    assert len(messages) == 1
    assert messages[0].thread_id == "thr-1"
    assert messages[0].from_addr == "cto@acme.com"
    assert messages[0].headers["subject"] == "Re: hi"


def test_gmail_skips_non_inbox_added(monkeypatch):
    provider = GmailProvider(MailboxFactory(provider=Mailbox.Provider.GMAIL))
    service = MagicMock()
    service.users.return_value.history.return_value.list.return_value.execute.return_value = {
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "m1", "labelIds": ["SENT"]}}]}],
    }
    monkeypatch.setattr(GmailProvider, "_service", lambda self: service)

    messages, _ = provider.fetch_new_messages("100")
    assert messages == []


def _outlook_mailbox():
    mailbox = MailboxFactory(provider=Mailbox.Provider.OUTLOOK)
    mailbox.token = json.dumps(
        {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}
    )
    mailbox.save()
    return mailbox


def test_graph_delta_parses_and_returns_deltalink(monkeypatch):
    provider = GraphProvider(_outlook_mailbox())
    page = {
        "value": [
            {
                "id": "m1",
                "conversationId": "conv-1",
                "subject": "Re: hi",
                "from": {"emailAddress": {"address": "cto@acme.com"}},
                "bodyPreview": "sure",
                "internetMessageHeaders": [{"name": "Auto-Submitted", "value": "no"}],
            }
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/.../delta?$deltatoken=abc",
    }
    monkeypatch.setattr(provider, "_request_url", lambda method, url, **kw: page)

    messages, cursor = provider.fetch_new_messages("")

    assert cursor.endswith("deltatoken=abc")
    assert len(messages) == 1
    assert messages[0].thread_id == "conv-1"
    assert messages[0].from_addr == "cto@acme.com"
    assert messages[0].headers["auto-submitted"] == "no"


def test_graph_pages_through_nextlinks(monkeypatch):
    provider = GraphProvider(_outlook_mailbox())

    def msg(mid, cid, addr):
        return {"id": mid, "conversationId": cid, "from": {"emailAddress": {"address": addr}}}

    first_page = {"value": [msg("m1", "c1", "a@b.com")], "@odata.nextLink": "page2"}
    second_page = {
        "value": [msg("m2", "c2", "c@d.com")],
        "@odata.deltaLink": "https://graph.microsoft.com/delta?$deltatoken=z",
    }
    pages = iter([first_page, second_page])
    monkeypatch.setattr(provider, "_request_url", lambda method, url, **kw: next(pages))

    messages, cursor = provider.fetch_new_messages("")

    assert [m.provider_message_id for m in messages] == ["m1", "m2"]
    assert cursor.endswith("deltatoken=z")
