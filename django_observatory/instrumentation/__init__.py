from .. import conf, context, internal


def _instrument_templates():
    """Top-level template renders become spans (includes are part of their parent)."""
    from django.template.backends.django import Template

    if getattr(Template.render, "_obs", False):
        return
    original = Template.render

    def render(self, context_=None, request=None):
        if context.current() is None or not conf.get("TRACING", "TEMPLATES"):
            return original(self, context_, request)
        from ..tracing import span

        with span(f"render {getattr(self.origin, 'template_name', None) or 'template'}", "template"):
            return original(self, context_, request)

    render._obs = True
    Template.render = render


def install():
    """Called once from AppConfig.ready(). Each instrument is independent and optional."""
    from . import database, http

    for name, fn in (("database", database.install), ("http", http.install), ("templates", _instrument_templates)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            internal.warn(f"install.{name}", "instrumentation not installed", exc)
