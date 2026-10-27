from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from backup.service import backup_status


@login_required
def home(request):
    return render(request, "core/home.html", {"backup": backup_status(timezone.now())})
