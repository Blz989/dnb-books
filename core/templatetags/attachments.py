from django import template

from core import attachments as service
from core.roles import is_admin

register = template.Library()


@register.inclusion_tag("core/_attachments.html", takes_context=True)
def attachments_card(context, obj):
    request = context["request"]
    return {
        "files": list(service.for_object(obj)),
        "kind": service.kind_of(obj),
        "obj": obj,
        "next": request.get_full_path(),
        "is_admin": is_admin(request.user),
        "csrf_token": context.get("csrf_token"),
    }


@register.filter
def filesize(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
