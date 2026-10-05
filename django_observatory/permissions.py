"""Access control for the UI and API.

PERMISSION_MODE "staff" (default): active staff users may view and triage everything
except the elevated set below. "permissions": every section needs its own permission.
Superusers always pass. Audit data, settings and deletion always need an explicit grant.
"""
import functools

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from . import conf

APP = "django_observatory"
ELEVATED = {"view_audit_events", "manage_observability_settings", "delete_observability_data"}
ALL = ("view_observability", "view_logs", "view_requests", "view_traces", "view_exceptions", "view_metrics",
       "view_security_events", "view_audit_events", "manage_alerts", "manage_observability_settings",
       "export_observability_data", "delete_observability_data")


def allowed(user, perm="view_observability"):
    if not (user and user.is_authenticated and user.is_active):
        return False
    if user.is_superuser:
        return True
    if perm in ELEVATED:
        return user.has_perm(f"{APP}.{perm}")
    if conf.get("PERMISSION_MODE") == "permissions":
        return user.has_perm(f"{APP}.view_observability") and (
            perm == "view_observability" or user.has_perm(f"{APP}.{perm}"))
    return user.is_staff


def granted(user):
    """{perm: bool} for templates (navigation only shows what the user may open)."""
    return {p: allowed(user, p) for p in ALL}


def check(request, perm):
    """Raise/redirect unless allowed. Returns a redirect response for anonymous users."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return redirect_to_login(request.get_full_path())
    if not allowed(user, perm):
        raise PermissionDenied("You do not have access to this observability section.")
    return None


def require(perm="view_observability"):
    def deco(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            return check(request, perm) or view(request, *args, **kwargs)
        return wrapper
    return deco
