from django.urls import path

from . import views

urlpatterns = [
    path("imports/", views.imports_list, name="imports"),
    path("imports/upload/", views.import_upload, name="import-upload"),
    path("imports/<int:pk>/map/", views.import_map, name="import-map"),
    path("imports/<int:pk>/", views.import_detail, name="import-detail"),
    path("imports/<int:pk>/errors.csv", views.import_errors, name="import-errors"),
    # Intake (BACKLOG 3.5)
    path("ingest/webhook/<str:token>/", views.webhook_intake, name="webhook-intake"),
    path("forms/<slug:slug>/", views.capture_form, name="capture-form"),
    path("contacts/<int:pk>/enrich/", views.contact_enrich, name="contact-enrich"),
    path("enrichment/", views.enrichment_queue, name="enrichment-queue"),
    path("enrichment/<int:pk>/resolve/", views.enrichment_resolve, name="enrichment-resolve"),
    path("enrichment/<int:pk>/dismiss/", views.enrichment_dismiss, name="enrichment-dismiss"),
    path("settings/integrations/", views.integrations, name="integrations"),
    path("settings/integrations/new/", views.integration_create, name="integration-create"),
    path(
        "settings/integrations/enrichment/",
        views.enrichment_settings,
        name="enrichment-settings",
    ),
]
