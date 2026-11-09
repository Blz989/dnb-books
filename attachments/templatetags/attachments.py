from django import template
from django.urls import reverse

from core.roles import is_admin

from ..files import human_size
from ..service import active, removed, target_kind

register = template.Library()


@register.inclusion_tag("attachments/_panel.html", takes_context=True)
def attachments_panel(context, target):
    user = context["request"].user
    admin = is_admin(user)
    return {
        "files": list(active(target)),
        "removed_files": list(removed(target)) if admin else [],
        "is_admin": admin,
        "add_url": reverse("attachments:add", args=[target_kind(target), target.number]),
        "csrf_token": context.get("csrf_token"),
    }


@register.filter
def filesize(n):
    return human_size(n or 0)
