from django.urls import path

from . import views

urlpatterns = [
    path("templates/", views.templates_list, name="templates"),
    path("templates/new/", views.template_create, name="template-create"),
    path("templates/preview/", views.template_preview, name="template-preview"),
    path("templates/<int:pk>/", views.template_edit, name="template-edit"),
]
