from django.urls import path

from . import views

urlpatterns = [
    path("templates/", views.templates_list, name="templates"),
    path("templates/new/", views.template_create, name="template-create"),
    path("templates/preview/", views.template_preview, name="template-preview"),
    path("templates/<int:pk>/", views.template_edit, name="template-edit"),
    path("settings/mailboxes/", views.mailboxes_settings, name="mailboxes"),
    path("settings/mailboxes/gmail/connect/", views.gmail_connect, name="gmail-connect"),
    path("settings/mailboxes/gmail/callback/", views.gmail_callback, name="gmail-callback"),
    path("settings/mailboxes/<int:pk>/edit/", views.mailbox_edit, name="mailbox-edit"),
    path(
        "settings/mailboxes/<int:pk>/test-send/",
        views.mailbox_test_send,
        name="mailbox-test-send",
    ),
]
