from django.contrib import admin

from .models import Activity, Company, Contact, Lead, Stage, Task


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "domain", "industry")
    search_fields = ("name", "domain")


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "company", "owner", "source")
    search_fields = ("email", "first_name", "last_name")
    list_filter = ("owner",)


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "is_won", "is_lost")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("contact", "stage", "status", "owner", "value")
    list_filter = ("stage", "status", "owner")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("type", "contact", "lead", "actor", "ts")
    list_filter = ("type",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "lead", "owner", "due_at", "done_at")
