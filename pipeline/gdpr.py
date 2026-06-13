"""Per-contact GDPR export (BACKLOG 4.6).

contact_export_data() returns a JSON-serializable dict of everything we hold on
a contact — profile, company, leads, the full activity timeline, tasks, and
sequence enrollments with their messages. Deletion is a plain cascade (FK
on_delete=CASCADE), so it lives in the view, not here.
"""

from .models import Task


def _dt(value):
    return value.isoformat() if value else None


def contact_export_data(contact) -> dict:
    company = contact.company
    return {
        "contact": {
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "email": contact.email,
            "title": contact.title,
            "phone": contact.phone,
            "linkedin_url": contact.linkedin_url,
            "source": contact.source,
            "owner": contact.owner.email if contact.owner else None,
            "custom_fields": contact.custom_fields,
            "unsubscribed_at": _dt(contact.unsubscribed_at),
            "bounced_at": _dt(contact.bounced_at),
            "created_at": _dt(contact.created_at),
        },
        "company": (
            {
                "name": company.name,
                "domain": company.domain,
                "industry": company.industry,
                "size": company.size,
                "location": company.location,
            }
            if company
            else None
        ),
        "leads": [
            {
                "stage": lead.stage.name,
                "status": lead.status,
                "value": str(lead.value) if lead.value is not None else None,
                "source": lead.source,
                "created_at": _dt(lead.created_at),
                "last_activity_at": _dt(lead.last_activity_at),
            }
            for lead in contact.leads.select_related("stage")
        ],
        "activities": [
            {
                "type": activity.type,
                "payload": activity.payload,
                "actor": activity.actor.email if activity.actor else None,
                "ts": _dt(activity.ts),
            }
            for activity in contact.activities.select_related("actor")
        ],
        "tasks": [
            {"title": task.title, "due_at": _dt(task.due_at), "done_at": _dt(task.done_at)}
            for task in Task.objects.filter(lead__contact=contact)
        ],
        "enrollments": [
            {
                "sequence": enrollment.sequence.name,
                "status": enrollment.status,
                "current_step": enrollment.current_step,
                "messages": [
                    {
                        "step": message.step.order,
                        "status": message.status,
                        "sent_at": _dt(message.sent_at),
                        "opened_at": _dt(message.opened_at),
                        "clicked_at": _dt(message.clicked_at),
                        "replied_at": _dt(message.replied_at),
                    }
                    for message in enrollment.messages.select_related("step")
                ],
            }
            for enrollment in contact.enrollments.select_related("sequence")
        ],
    }
