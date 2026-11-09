from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.roles import require_admin

from .files import CONTENT_TYPE
from .models import Attachment
from .service import (
    AttachmentError,
    add_files,
    can_view,
    document_url,
    find_document,
    label,
    remove,
    target_kind,
)

NAV = {"bill": "bills", "payment": "payments", "vendor-payment": "pay-bills"}


@login_required
def serve(request, pk):
    """The only way a stored file is sent: signed in, allowed, typed by what was checked at upload."""
    attachment = get_object_or_404(Attachment, pk=pk)
    if not can_view(request.user, attachment):
        raise Http404("No such file.")
    try:
        handle = attachment.file.open("rb")
    except FileNotFoundError:
        raise Http404("The file is missing.") from None
    response = FileResponse(handle, content_type=CONTENT_TYPE[attachment.kind], as_attachment=False,
                            filename=attachment.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def add(request, kind, number):
    target = find_document(kind, number)
    if target is None:
        raise Http404("No such document.")
    back = document_url(target) + "#attachments"
    files = request.FILES.getlist("files")
    if not files:
        messages.error(request, "Choose a file to add.")
        return redirect(back)
    result = add_files(request.user, target, files)
    for a in result.added:
        messages.success(request, f"Added {a.original_name}.")
    for name, problem in result.refused:
        messages.error(request, f"{name}: {problem}")
    return redirect(back)


@login_required
def remove_view(request, pk):
    require_admin(request.user)
    attachment = get_object_or_404(Attachment, pk=pk, removed_at__isnull=True, target_id__isnull=False)
    target = attachment.target
    back = document_url(target) + "#attachments"
    context = {"attachment": attachment, "number": label(target), "back": back, "reason": "", "error": "",
               "nav_active": NAV[target_kind(target)]}
    if request.method == "POST":
        try:
            remove(request.user, attachment, request.POST.get("reason", ""))
        except AttachmentError as exc:
            context.update(error=str(exc), reason=request.POST.get("reason", ""))
            return render(request, "attachments/remove.html", context)
        messages.success(request, f"Removed {attachment.original_name}.")
        return redirect(back)
    return render(request, "attachments/remove.html", context)


@login_required
def link_page(request, token):
    from . import links
    from .models import Source, UploadLink

    link = get_object_or_404(UploadLink.objects.select_related("pending_change"), token=token)
    error = links.problem(link, request.user)
    added, refused = [], []
    if request.method == "POST" and not error:
        target = links.resolve_target(link)
        files = request.FILES.getlist("files")
        if not files:
            refused.append(("", "Choose a file to add."))
        else:
            result = add_files(request.user, target, files, source=Source.CLAUDE_LINK, link=link)
            added = [(a.original_name, f"Added {a.original_name} to {target.number}." if target is not None
                      else f"Added {a.original_name}. Go back to Claude and confirm the change.")
                     for a in result.added]
            refused = result.refused
    return render(request, "attachments/link.html", {
        "link": link, "error": error, "heading": "" if error and error == links.SOMEONE_ELSE else links.heading(link),
        "added": added, "refused": refused,
    })
