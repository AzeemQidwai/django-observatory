"""Admin integration. The custom UI is the primary interface; the admin is for
triage records and for inspecting security/audit data with the usual admin tooling."""
from django.contrib import admin

from .models import Alert, AlertRule, AuditEvent, Incident, Issue, ObservabilityConfiguration, SecurityEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "occurrence_count", "first_seen", "last_seen", "assignee")
    list_filter = ("status", "severity")
    search_fields = ("title", "culprit", "exc_type")
    readonly_fields = ("fingerprint", "exc_type", "title", "culprit", "first_seen", "last_seen", "occurrence_count",
                       "first_release", "last_release")


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "metric", "operator", "threshold", "window_minutes", "severity", "enabled")
    list_filter = ("enabled", "severity")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "severity", "value", "started_at", "resolved_at")
    list_filter = ("status", "severity")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "severity", "confidence", "detected_at", "resolved_at")
    list_filter = ("status", "severity")


@admin.register(SecurityEvent)
class SecurityEventAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "event", "severity", "username", "ip_address", "resource")
    list_filter = ("event", "severity")
    search_fields = ("username", "ip_address", "resource")


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    """Read-only by design: audit records cannot be changed or deleted here."""

    list_display = ("timestamp", "username", "action", "object_type", "object_id", "result")
    list_filter = ("action", "result")
    search_fields = ("username", "object_type", "object_id")

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm("django_observatory.view_audit_events")

    has_module_permission = has_view_permission


@admin.register(ObservabilityConfiguration)
class ConfigurationAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at", "updated_by")
