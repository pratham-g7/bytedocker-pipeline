"""Populate the CRM with realistic demo data for clicking around / testing.

    python manage.py seed_demo               # seed (idempotent)
    python manage.py seed_demo --owner a@b.c # attribute to a specific user
    python manage.py seed_demo --wipe        # remove everything this command created

Everything it creates is tagged so --wipe can find it: contacts carry
source="seed:demo", and the demo company domains / sequence / template /
mailbox use the SEED_* markers below. Nothing here sends real email — the
demo mailbox has no token and active enrollments are dated into the future.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from outreach.models import (
    EmailTemplate,
    Enrollment,
    Mailbox,
    Message,
    Sequence,
    SequenceStep,
)
from pipeline.models import Activity, Company, Contact, Lead, Stage, Task

SEED_SOURCE = "seed:demo"
SEED_MAILBOX = "demo-sender@bytedocker.test"
SEED_SEQUENCE = "Founder outreach (demo)"
SEED_TEMPLATE = "Intro — engineering capacity (demo)"

# (name, domain, industry, size, location) — ICP-shaped: seed–Series B, remote-friendly.
COMPANIES = [
    ("Ledgerline", "ledgerline.io", "Fintech SaaS", "40", "London, UK"),
    ("Cropwise", "cropwise.ai", "AgTech", "25", "Austin, US"),
    ("Northwind Health", "northwindhealth.com", "Health SaaS", "70", "Boston, US"),
    ("Parsec Labs", "parseclabs.dev", "Developer tools", "18", "Remote"),
    ("Tideway", "tideway.co", "Logistics SaaS", "55", "Rotterdam, NL"),
    ("Quanta Retail", "quantaretail.com", "Retail analytics", "120", "Bangalore, IN"),
    ("Mosaic Energy", "mosaicenergy.io", "Climate tech", "30", "Berlin, DE"),
    ("Brightloom", "brightloom.app", "Marketing SaaS", "22", "Toronto, CA"),
    ("Auxon", "auxon.io", "Robotics", "45", "San Francisco, US"),
    ("Verra Health", "verrahealth.com", "Telehealth", "60", "Manchester, UK"),
    ("Stacklane", "stacklane.dev", "Infra / DevOps", "15", "Remote"),
    ("Finch & Co", "finchpay.com", "Payments", "90", "Singapore, SG"),
]

# (company_index, first, last, title)
CONTACTS = [
    (0, "Priya", "Nair", "CTO"),
    (0, "James", "Whitfield", "VP Engineering"),
    (1, "Diego", "Marquez", "Co-founder & CTO"),
    (2, "Sarah", "Lindqvist", "Head of Engineering"),
    (3, "Tom", "Becker", "Founder"),
    (4, "Anika", "Roy", "CTO"),
    (5, "Vikram", "Shah", "VP Engineering"),
    (6, "Lena", "Hofmann", "Co-founder"),
    (7, "Marcus", "Bell", "Head of Platform"),
    (8, "Yuki", "Tanaka", "CTO"),
    (9, "Olivia", "Grant", "Eng Director"),
    (10, "Sam", "Okoye", "Founder & CTO"),
    (11, "Wei", "Chen", "VP Engineering"),
    (1, "Hannah", "Cole", "Talent Lead"),
    (4, "Ravi", "Menon", "Founder"),
    (8, "Grace", "Liu", "Staff Engineer"),
]

NOTES = [
    "Met at SaaStr — warm intro from their lead investor.",
    "Hiring 3 backend engineers per their careers page.",
    "Mentioned 6-month timeline to ship the new platform.",
    "Prefers async; follow up over email not calls.",
    "Asked for India-based senior Django profiles.",
]


class Command(BaseCommand):
    help = "Seed realistic demo CRM data (idempotent). Use --wipe to remove it."

    def add_arguments(self, parser):
        parser.add_argument("--owner", help="Email of the user to own the data")
        parser.add_argument("--wipe", action="store_true", help="Remove all seeded data")

    def handle(self, *args, **options):
        if options["wipe"]:
            return self._wipe()
        owner = self._owner(options.get("owner"))
        random.seed(42)  # deterministic spread
        with transaction.atomic():
            self._seed(owner)

    # ---------------------------------------------------------------- owner

    def _owner(self, email):
        if email:
            try:
                return User.objects.get(email=email.lower())
            except User.DoesNotExist:
                raise CommandError(f"No user with email {email}") from None
        owner = (
            User.objects.filter(is_superuser=True).order_by("pk").first()
            or User.objects.order_by("pk").first()
        )
        if owner is None:
            raise CommandError("No users exist — create one first (createsuperuser).")
        return owner

    # ---------------------------------------------------------------- seed

    def _seed(self, owner):
        stages = list(Stage.objects.order_by("order"))
        open_stages = [s for s in stages if not s.is_won and not s.is_lost]
        won = next((s for s in stages if s.is_won), None)
        lost = next((s for s in stages if s.is_lost), None)
        if not open_stages:
            raise CommandError("No pipeline stages found — run migrations first.")

        mailbox = self._mailbox(owner)
        sequence, steps = self._sequence(owner)

        companies = {}
        for name, domain, industry, size, location in COMPANIES:
            companies[domain], _ = Company.objects.get_or_create(
                domain=domain,
                defaults={"name": name, "industry": industry, "size": size, "location": location},
            )

        contacts = []
        for company_index, first, last, title in CONTACTS:
            domain = COMPANIES[company_index][1]
            email = f"{first.lower()}.{last.lower()}@{domain}"
            contact, created = Contact.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "title": title,
                    "company": companies[domain],
                    "owner": owner,
                    "source": SEED_SOURCE,
                },
            )
            contacts.append(contact)
            if created:
                self._story(contact, owner, open_stages, won, lost)

        self._enrollments(owner, mailbox, sequence, steps, contacts)
        self._tasks(owner, contacts)
        self._summary()

    def _mailbox(self, owner):
        mailbox, _ = Mailbox.objects.get_or_create(
            email=SEED_MAILBOX,
            defaults={
                "user": owner,
                "provider": Mailbox.Provider.GMAIL,
                "status": Mailbox.Status.ACTIVE,
                "sends_today": 12,
                "warmup": False,  # full cap so the meter reads sensibly
            },
        )
        return mailbox

    def _sequence(self, owner):
        template, _ = EmailTemplate.objects.get_or_create(
            name=SEED_TEMPLATE,
            defaults={
                "subject": "Senior engineers for {{company}}, {{first_name|there}}?",
                "body_html": (
                    "<p>Hi {{first_name|there}},</p>"
                    "<p>Saw {{company}} is scaling the engineering team. We place vetted "
                    "India-based senior engineers with teams like yours, fast.</p>"
                    "<p>Worth a quick chat?</p><p>— {{sender_name}}</p>"
                ),
            },
        )
        followup, _ = EmailTemplate.objects.get_or_create(
            name="Bump (demo)",
            defaults={
                "subject": "Re: Senior engineers for {{company}}",
                "body_html": "<p>{{first_name|there}}, floating this back up — worth a chat?</p>",
            },
        )
        sequence, created = Sequence.objects.get_or_create(
            name=SEED_SEQUENCE, defaults={"owner": owner, "is_active": True}
        )
        if created:
            SequenceStep.objects.create(sequence=sequence, order=1, wait_days=0, template=template)
            SequenceStep.objects.create(sequence=sequence, order=2, wait_days=3, template=followup)
            SequenceStep.objects.create(sequence=sequence, order=3, wait_days=4, template=followup)
        return sequence, list(sequence.steps.order_by("order"))

    def _story(self, contact, owner, open_stages, won, lost):
        """Give a contact a lead at some stage + a backdated activity trail."""
        roll = random.random()
        if roll < 0.12 and won:
            stage, status = won, Lead.Status.WON
        elif roll < 0.20 and lost:
            stage, status = lost, Lead.Status.LOST
        else:
            stage = random.choice(open_stages)
            status = Lead.Status.OPEN
        age = random.randint(2, 40)
        lead = Lead.objects.create(
            contact=contact,
            stage=stage,
            owner=owner,
            source=SEED_SOURCE,
            status=status,
            value=random.choice([None, None, 48000, 60000, 72000]),
            last_activity_at=timezone.now() - timedelta(days=random.randint(0, age)),
        )
        Lead.objects.filter(pk=lead.pk).update(created_at=timezone.now() - timedelta(days=age))

        note = {"text": random.choice(NOTES)}
        self._activity(contact, lead, owner, Activity.Type.NOTE, age, note)
        if stage.order >= 2:  # contacted+: there was outreach
            self._activity(contact, lead, owner, Activity.Type.EMAIL_SENT, age - 1, {"step": 1})
            if random.random() < 0.6:
                self._activity(contact, lead, owner, Activity.Type.EMAIL_OPENED, age - 1, {})
        if stage.order >= 3 and status == Lead.Status.OPEN:  # engaged: replied
            self._activity(contact, lead, owner, Activity.Type.EMAIL_REPLIED, age - 2, {})

    def _activity(self, contact, lead, owner, type_, days_ago, payload):
        activity = Activity.objects.create(
            contact=contact, lead=lead, type=type_, actor=owner, payload=payload
        )
        Activity.objects.filter(pk=activity.pk).update(
            ts=timezone.now() - timedelta(days=max(days_ago, 0), hours=random.randint(0, 20))
        )

    def _enrollments(self, owner, mailbox, sequence, steps, contacts):
        # A spread of enrollment states so the sequence + dashboard look alive.
        plan = [
            (Enrollment.Status.ACTIVE, 1),
            (Enrollment.Status.ACTIVE, 2),
            (Enrollment.Status.ACTIVE, 1),
            (Enrollment.Status.REPLIED, 1),
            (Enrollment.Status.REPLIED, 2),
            (Enrollment.Status.FINISHED, 3),
            (Enrollment.Status.BOUNCED, 1),
        ]
        for contact, (status, current_step) in zip(contacts, plan, strict=False):
            if Enrollment.objects.filter(contact=contact, sequence=sequence).exists():
                continue
            next_send = (
                timezone.now() + timedelta(days=random.randint(2, 6))
                if status == Enrollment.Status.ACTIVE
                else None
            )
            enrollment = Enrollment.objects.create(
                contact=contact,
                sequence=sequence,
                mailbox=mailbox,
                enrolled_by=owner,
                current_step=current_step,
                status=status,
                next_send_at=next_send,
            )
            for step in steps[:current_step]:
                msg = Message.objects.create(
                    enrollment=enrollment,
                    step=step,
                    mailbox=mailbox,
                    provider_message_id=f"seed-{enrollment.pk}-{step.order}",
                    thread_id=f"seed-thread-{enrollment.pk}",
                    subject_rendered="Senior engineers for their team",
                    status=Message.Status.SENT,
                    sent_at=timezone.now() - timedelta(days=7 - step.order),
                )
                stamp = {}
                if random.random() < 0.7:
                    stamp["opened_at"] = msg.sent_at + timedelta(hours=5)
                if status == Enrollment.Status.REPLIED and step.order == current_step:
                    stamp["replied_at"] = msg.sent_at + timedelta(days=1)
                if status == Enrollment.Status.BOUNCED:
                    stamp["status"] = Message.Status.BOUNCED
                if stamp:
                    Message.objects.filter(pk=msg.pk).update(**stamp)

    def _tasks(self, owner, contacts):
        titles = [
            ("Follow up on reply", -1),
            ("Send case study", -2),
            ("Book intro call", 1),
            ("Share 2 dev profiles", 2),
            ("Chase contract redlines", -3),
            ("Quarterly check-in", 5),
        ]
        leads = [c.open_lead for c in contacts if c.open_lead]
        for (title, due_offset), lead in zip(titles, leads, strict=False):
            Task.objects.get_or_create(
                lead=lead,
                title=title,
                defaults={"owner": owner, "due_at": timezone.now() + timedelta(days=due_offset)},
            )

    def _summary(self):
        self.stdout.write(
            self.style.SUCCESS(
                "Seeded demo data — "
                f"{Company.objects.count()} companies, "
                f"{Contact.objects.count()} contacts, "
                f"{Lead.objects.count()} leads, "
                f"{Enrollment.objects.count()} enrollments, "
                f"{Task.objects.filter(done_at__isnull=True).count()} open tasks."
            )
        )

    # ---------------------------------------------------------------- wipe

    def _wipe(self):
        seed_domains = [c[1] for c in COMPANIES]
        contacts = Contact.objects.filter(source=SEED_SOURCE)
        n = contacts.count()
        contacts.delete()  # cascades leads, activities, enrollments, messages, tasks
        Company.objects.filter(domain__in=seed_domains).delete()
        Sequence.objects.filter(name=SEED_SEQUENCE).delete()
        EmailTemplate.objects.filter(name__in=[SEED_TEMPLATE, "Bump (demo)"]).delete()
        Mailbox.objects.filter(email=SEED_MAILBOX).delete()
        self.stdout.write(self.style.WARNING(f"Wiped demo data ({n} seeded contacts + related)."))
