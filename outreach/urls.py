from django.urls import path

from . import public_views, views

urlpatterns = [
    # Public tracking endpoints (ENGINE_SPEC §4) — no auth
    path("t/o/<uuid:uuid>.gif", public_views.track_open, name="track-open"),
    path("t/c/<uuid:uuid>/<str:sig>/", public_views.track_click, name="track-click"),
    path("templates/", views.templates_list, name="templates"),
    path("templates/new/", views.template_create, name="template-create"),
    path("templates/preview/", views.template_preview, name="template-preview"),
    path("templates/<int:pk>/", views.template_edit, name="template-edit"),
    path("sequences/", views.sequences_list, name="sequences"),
    path("sequences/new/", views.sequence_create, name="sequence-create"),
    path("sequences/<int:pk>/", views.sequence_detail, name="sequence-detail"),
    path("sequences/<int:pk>/steps/", views.sequence_steps, name="sequence-steps"),
    path(
        "sequences/<int:pk>/enrollments/",
        views.sequence_enrollments,
        name="sequence-enrollments",
    ),
    path("sequences/<int:pk>/toggle/", views.sequence_toggle, name="sequence-toggle"),
    path("sequences/<int:pk>/clone/", views.sequence_clone, name="sequence-clone"),
    path("sequences/<int:pk>/steps/add/", views.step_add, name="step-add"),
    path("steps/<int:pk>/delete/", views.step_delete, name="step-delete"),
    path("steps/<int:pk>/preview/", views.step_preview, name="step-preview"),
    path("enroll/modal/", views.enroll_modal, name="enroll-modal"),
    path("enroll/", views.enroll, name="enroll"),
    path("enrollments/<int:pk>/action/", views.enrollment_action, name="enrollment-action"),
    path(
        "contacts/<int:pk>/enrollments/",
        views.contact_enrollments,
        name="contact-enrollments",
    ),
    path("settings/mailboxes/", views.mailboxes_settings, name="mailboxes"),
    path("settings/mailboxes/gmail/connect/", views.gmail_connect, name="gmail-connect"),
    path("settings/mailboxes/gmail/callback/", views.gmail_callback, name="gmail-callback"),
    path("settings/mailboxes/outlook/connect/", views.outlook_connect, name="outlook-connect"),
    path(
        "settings/mailboxes/outlook/callback/",
        views.outlook_callback,
        name="outlook-callback",
    ),
    path("settings/mailboxes/<int:pk>/edit/", views.mailbox_edit, name="mailbox-edit"),
    path(
        "settings/mailboxes/<int:pk>/test-send/",
        views.mailbox_test_send,
        name="mailbox-test-send",
    ),
]
