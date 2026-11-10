"""Settings → Claude connections: the service switch, who may connect, and the call log."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse

from core.roles import is_admin, require_admin

from .models import McpAccess, McpCall, McpSettings


@login_required
def connections(request):
    require_admin(request.user)
    service = McpSettings.load()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "service":
            service.enabled = request.POST.get("enabled") == "1"
            service.save()
            messages.success(request, "Claude access is on." if service.enabled else "Claude access is off.")
        elif action == "user":
            user = get_user_model().objects.filter(pk=request.POST.get("user")).first()
            if user is not None:
                on = request.POST.get("enabled") == "1"
                if on and not user.email:
                    messages.error(request, f"Add an email for {user.username} in Users and roles first. "
                                            "It must match their Cloudflare login.")
                else:
                    McpAccess.objects.update_or_create(user=user, defaults={"enabled": on})
                    name = user.get_full_name() or user.username
                    messages.success(request, f"{name} can use Claude." if on else f"{name} can no longer use Claude.")
        return redirect(reverse("settings:claude"))

    access = dict(McpAccess.objects.values_list("user_id", "enabled"))
    people = [{"user": u, "role": "Admin" if is_admin(u) else "Standard", "on": access.get(u.pk, False)}
              for u in get_user_model().objects.filter(is_active=True).order_by("username").prefetch_related("groups")]
    only_voids = request.GET.get("show") == "voids"
    log = McpCall.objects.select_related("user")
    if only_voids:
        # Voids and edits: their previews, and the confirmations that carried a typed document number.
        log = log.filter(Q(tool__startswith="void_") | Q(tool__startswith="edit_")
                         | (Q(tool="confirm_change") & Q(arguments__has_key="confirm_document")
                            & ~Q(arguments__confirm_document="")))
    calls = Paginator(log, 50).get_page(request.GET.get("page"))
    hosts = settings.DNB_MCP_ALLOWED_HOSTS
    return render(request, "mcpserver/connections.html", {
        "service": service, "people": people, "calls": calls, "only_voids": only_voids,
        "connector_url": f"https://{hosts[0]}/mcp" if hosts else "",
        "aud_set": bool(settings.DNB_CF_ACCESS_AUD), "team_domain": settings.DNB_CF_ACCESS_TEAM_DOMAIN,
        "nav_active": "settings", "settings_section": "claude",
    })
