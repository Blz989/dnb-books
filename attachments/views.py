from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404

from .files import CONTENT_TYPE
from .models import Attachment
from .service import can_view


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
