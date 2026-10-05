"""Django signal instrumentation: authentication events and admin audit mirroring.
Deliberately narrow: only low-volume, high-value signals."""
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save

from . import audit, security


def _logged_in(sender, request, user, **_):
    security.record("LOGIN_SUCCESS", user=user, request=request)
    if getattr(user, "is_superuser", False):
        security.record("SUPERUSER_LOGIN", user=user, request=request)


def _logged_out(sender, request, user, **_):
    security.record("LOGOUT", user=user, request=request)


def _login_failed(sender, credentials, request=None, **_):
    # credentials is already cleansed by Django, and we keep only the identifier anyway
    name = str(credentials.get("username") or credentials.get("email") or "")[:150]
    security.record("LOGIN_FAILURE", username=name, request=request, reason="Invalid credentials")


def _user_saved(sender, instance, created, **_):
    # set_password() leaves the raw value on _password until save() finishes; we only
    # look at whether it is set, never at its value.
    if not created and getattr(instance, "_password", None) is not None:
        security.record("PASSWORD_CHANGE", user=instance)


def _admin_log(sender, instance, created, **_):
    if not created:
        return
    action = {1: "CREATE", 2: "UPDATE", 3: "DELETE"}.get(instance.action_flag, "ADMIN")
    ctype = instance.content_type
    audit.record(action, ctype.model if ctype else "", instance.object_id or "", user=instance.user,
                 source="django.admin", object_repr=instance.object_repr, change_message=instance.change_message)


def connect():
    uid = "django_observatory"
    user_logged_in.connect(_logged_in, dispatch_uid=uid)
    user_logged_out.connect(_logged_out, dispatch_uid=uid)
    user_login_failed.connect(_login_failed, dispatch_uid=uid)
    post_save.connect(_user_saved, sender=get_user_model(), dispatch_uid=uid + ".pw")
    try:
        from django.contrib.admin.models import LogEntry

        post_save.connect(_admin_log, sender=LogEntry, dispatch_uid=uid + ".admin")
    except Exception:  # noqa: BLE001 - admin not installed
        pass
