from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import attachments
from .attachments import AttachmentError
from .models import Attachment
from .roles import is_admin


def _next(request) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()) and target.startswith("/"):
        return target
    return "/"


@login_required
@require_POST
def add(request, kind, pk):
    model = attachments.model_for(kind)
    if model is None:
        raise Http404("No such record.")
    obj = get_object_or_404(model, pk=pk)
    try:
        a = attachments.add(obj, request.FILES.get("file"), user=request.user)
    except AttachmentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Added {a.name}.")
    return redirect(_next(request))


@login_required
def open_file(request, pk):
    a = get_object_or_404(Attachment, pk=pk)
    try:
        handle = a.file.open("rb")
    except FileNotFoundError:
        raise Http404("The file is missing from the server.") from None
    resp = FileResponse(handle, content_type=a.mime, as_attachment=False, filename=a.name)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
def remove(request, pk):
    a = get_object_or_404(Attachment, pk=pk)
    if not is_admin(request.user):
        raise PermissionDenied("Only an admin can remove an attachment.")
    if request.method == "POST":
        name = a.name
        attachments.remove(a, user=request.user)
        messages.success(request, f"Removed {name}.")
        return redirect(_next(request))
    return render(request, "core/attachment_remove.html", {"attachment": a, "next": _next(request)})
