import factory
from django.utils import timezone

from outreach.models import EmailTemplate, Enrollment, Mailbox, Message, Sequence, SequenceStep
from pipeline.models import Activity, Company, Contact, Lead, Stage, Task


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "accounts.User"
        django_get_or_create = ("email",)

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    name = factory.Faker("name")
    role = "rep"


class CompanyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Company

    name = factory.Sequence(lambda n: f"Company {n}")
    domain = factory.Sequence(lambda n: f"company{n}.com")


class ContactFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Contact

    company = factory.SubFactory(CompanyFactory)
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"contact{n}@example.com")
    owner = factory.SubFactory(UserFactory)


def first_stage():
    return Stage.objects.exclude(is_won=True).exclude(is_lost=True).first()


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lead

    contact = factory.SubFactory(ContactFactory)
    stage = factory.LazyFunction(first_stage)
    owner = factory.SelfAttribute("contact.owner")


class ActivityFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Activity

    contact = factory.SubFactory(ContactFactory)
    type = Activity.Type.NOTE
    payload = {"text": "a note"}


class TaskFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Task

    lead = factory.SubFactory(LeadFactory)
    owner = factory.SelfAttribute("lead.owner")
    title = factory.Sequence(lambda n: f"Task {n}")
    due_at = factory.LazyFunction(timezone.now)


class MailboxFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Mailbox

    user = factory.SubFactory(UserFactory)
    provider = Mailbox.Provider.GMAIL
    email = factory.Sequence(lambda n: f"mailbox{n}@example.com")


class EmailTemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EmailTemplate

    name = factory.Sequence(lambda n: f"Template {n}")
    subject = "Quick question, {{first_name|there}}"
    body_html = "<p>Hi {{first_name|there}}, saw {{company}} is growing.</p><p>{{sender_name}}</p>"


class SequenceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Sequence

    name = factory.Sequence(lambda n: f"Sequence {n}")
    owner = factory.SubFactory(UserFactory)


class SequenceStepFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SequenceStep

    sequence = factory.SubFactory(SequenceFactory)
    order = factory.Sequence(lambda n: n + 1)
    wait_days = 2
    template = factory.SubFactory(EmailTemplateFactory)


class EnrollmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Enrollment

    contact = factory.SubFactory(ContactFactory)
    sequence = factory.SubFactory(SequenceFactory)
    mailbox = factory.SubFactory(MailboxFactory)
    enrolled_by = factory.SelfAttribute("sequence.owner")
    next_send_at = factory.LazyFunction(timezone.now)


class MessageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Message

    enrollment = factory.SubFactory(EnrollmentFactory)
    step = factory.SubFactory(
        SequenceStepFactory, sequence=factory.SelfAttribute("..enrollment.sequence")
    )
    mailbox = factory.SelfAttribute("enrollment.mailbox")
