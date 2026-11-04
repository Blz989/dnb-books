from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import never_cache
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from core.roles import require_admin
from ledger.errors import PostingError

from ..errors import SalesError
from ..forms import SalesSettingsForm
from ..models import SALES_DOC_TYPES, DocumentSequence, SalesSettings
from ..numbering import _sequence, set_sequence


def _numbering_rows(post=None, errors=None):
    rows = []
    for doc_type in SALES_DOC_TYPES:
        seq = _sequence(doc_type)
        rows.append({
            "type": doc_type.value, "label": doc_type.label,
            "prefix": post.get(f"prefix_{doc_type.value}", seq.prefix) if post else seq.prefix,
            "next_number": post.get(f"next_{doc_type.value}", seq.next_number) if post else seq.next_number,
            "error": (errors or {}).get(doc_type.value, ""),
        })
    return rows


@login_required
def sales_settings(request):
    require_admin(request.user)
    config = SalesSettings.load()
    form = SalesSettingsForm(instance=config)
    errors: dict[str, str] = {}
    post = None
    if request.method == "POST" and request.POST.get("action") == "settings":
        form = SalesSettingsForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            return redirect(reverse("sales:settings"))
    elif request.method == "POST" and request.POST.get("action") == "numbering":
        post = request.POST
        for doc_type in SALES_DOC_TYPES:
            seq = DocumentSequence.objects.get(doc_type=doc_type)
            prefix = post.get(f"prefix_{doc_type.value}", seq.prefix).strip()
            raw = post.get(f"next_{doc_type.value}", "").strip()
            if not raw.isdigit() or int(raw) < 1:
                errors[doc_type.value] = "Enter a next number of 1 or more."
                continue
            if prefix == seq.prefix and int(raw) == seq.next_number:
                continue
            try:
                set_sequence(doc_type, prefix=prefix, next_number=int(raw), user=request.user)
            except (SalesError, PostingError) as exc:
                errors[doc_type.value] = str(exc)
        if not errors:
            messages.success(request, "Numbering saved.")
            return redirect(reverse("sales:settings"))
    return render(request, "sales/settings.html", {
        "form": form, "numbering": _numbering_rows(post, errors),
        "nav_active": "settings", "settings_section": "sales",
    })


@never_cache
def logo(request):
    """Public on purpose: the login page shows the logo before anyone signs in."""
    config = SalesSettings.load()
    if not config.logo:
        raise Http404("No logo has been uploaded.")
    kind = "image/png" if config.logo.name.endswith(".png") else "image/jpeg"
    return FileResponse(config.logo.open("rb"), content_type=kind)
