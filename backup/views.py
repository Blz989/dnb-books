from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.roles import require_admin

from .service import BackupError, live_state, request_backup


@login_required
@require_POST
def run_now(request):
    require_admin(request.user)
    try:
        request_backup(request.user)
    except BackupError as exc:
        messages.error(request, str(exc))
    return redirect("home")


@login_required
@require_GET
def status(request):
    response = JsonResponse(live_state(timezone.now()).as_json())
    response["Cache-Control"] = "no-store"
    return response
