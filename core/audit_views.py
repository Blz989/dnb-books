"""Settings, Audit log: every recorded change (who, what, when, before and after). Admins only."""
from datetime import date

from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.shortcuts import render

from contacts.models import mask_tax_id
from ledger.models import JournalLine

from .roles import require_admin

ACTIONS = {LogEntry.Action.CREATE: "Created", LogEntry.Action.UPDATE: "Changed", LogEntry.Action.DELETE: "Deleted",
           LogEntry.Action.ACCESS: "Viewed"}
MASKED_FIELDS = {"tax_id", "tax id"}
MAX_CHANGES_SHOWN = 6


def _type_label(ct: ContentType | None) -> str:
    model = ct.model_class() if ct else None
    return model._meta.verbose_name.capitalize() if model else (ct.model if ct else "")


def _changes(entry: LogEntry) -> list[dict]:
    try:
        display = entry.changes_display_dict
    except Exception:  # a model that no longer exists: fall back to the raw stored values
        display = entry.changes_dict
    out = []
    for field, values in display.items():
        old, new = ("" if v in (None, "None") else v for v in (list(values) + ["", ""])[:2])
        if str(field).lower() in MASKED_FIELDS:
            old, new = mask_tax_id(str(old or "")), mask_tax_id(str(new or ""))
        name = str(field).replace("_", " ")
        out.append({"field": name[:1].upper() + name[1:], "old": old, "new": new})
    return out


@login_required
def audit_log(request):
    require_admin(request.user)
    entries = LogEntry.objects.select_related("actor", "content_type").order_by("-timestamp", "-id")
    q = request.GET.get("q", "").strip()
    type_id = request.GET.get("type", "")
    actor_id = request.GET.get("user", "")
    action = request.GET.get("action", "")
    start, end = request.GET.get("start", ""), request.GET.get("end", "")
    if q:
        entries = entries.filter(object_repr__icontains=q)
    if type_id.isdigit():
        entries = entries.filter(content_type_id=int(type_id))
    else:
        # Each journal entry's own row already records it; one row per line would bury everything else.
        entries = entries.exclude(content_type=ContentType.objects.get_for_model(JournalLine))
    if actor_id == "system":
        entries = entries.filter(actor__isnull=True)
    elif actor_id.isdigit():
        entries = entries.filter(actor_id=int(actor_id))
    if action.isdigit():
        entries = entries.filter(action=int(action))
    error = ""
    try:
        if start:
            entries = entries.filter(timestamp__date__gte=date.fromisoformat(start))
        if end:
            entries = entries.filter(timestamp__date__lte=date.fromisoformat(end))
    except ValueError:
        error = "Enter a date like 2026-01-31."
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    rows = []
    for e in page:
        changes = _changes(e) if e.action == LogEntry.Action.UPDATE else []
        label = _type_label(e.content_type)
        record = label if e.object_repr.endswith(" object (1)") else e.object_repr  # single settings rows
        rows.append({"e": e, "record": record, "action": ACTIONS.get(e.action, ""), "type": label,
                     "changes": changes[:MAX_CHANGES_SHOWN], "more": max(len(changes) - MAX_CHANGES_SHOWN, 0)})
    used_types = LogEntry.objects.values_list("content_type", flat=True).distinct()
    types = sorted(((ct.pk, _type_label(ct)) for ct in ContentType.objects.filter(pk__in=used_types)),
                   key=lambda t: t[1])
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "core/settings/audit_log.html", {
        "nav_active": "settings", "settings_section": "audit", "page": page, "rows": rows, "error": error,
        "types": types, "users": get_user_model().objects.order_by("username"), "actions": list(ACTIONS.items()),
        "f": {"q": q, "type": type_id, "user": actor_id, "action": action, "start": start, "end": end},
        "query": query.urlencode(),
    })
