from django.urls import path

from . import views

urlpatterns = [
    path("contacts/", views.contacts_list, name="contacts"),
    path("contacts/new/", views.contact_create, name="contact-create"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact-detail"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact-edit"),
    path("contacts/<int:pk>/timeline/", views.contact_timeline, name="contact-timeline"),
    path("contacts/<int:pk>/notes/", views.contact_note_add, name="contact-note-add"),
    path("contacts/<int:pk>/leads/create/", views.contact_lead_create, name="contact-lead-create"),
    path("contacts/<int:pk>/tasks/create/", views.contact_task_create, name="contact-task-create"),
    path("companies/", views.companies_list, name="companies"),
    path("companies/new/", views.company_create, name="company-create"),
    path("companies/<int:pk>/", views.company_detail, name="company-detail"),
    path("companies/<int:pk>/edit/", views.company_edit, name="company-edit"),
    path("board/", views.board, name="board"),
    path("leads/", views.leads_list, name="leads"),
    path("leads/<int:pk>/move/", views.lead_move, name="lead-move"),
    path("tasks/", views.tasks_list, name="tasks"),
    path("tasks/<int:pk>/toggle/", views.task_toggle, name="task-toggle"),
    path("settings/stages/", views.stages_settings, name="stages-settings"),
    path("settings/stages/add/", views.stage_add, name="stage-add"),
    path("settings/stages/<int:pk>/rename/", views.stage_rename, name="stage-rename"),
    path("settings/stages/<int:pk>/move/", views.stage_move, name="stage-move"),
    path("settings/stages/<int:pk>/delete/", views.stage_delete, name="stage-delete"),
]
