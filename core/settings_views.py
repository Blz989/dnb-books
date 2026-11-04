"""Settings area: company profile, users and roles, and the link to the raw database screens."""
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from sales.forms import CompanyProfileForm

from . import company
from .forms import UserForm
from .roles import ADMIN_GROUP, STANDARD_GROUP, is_admin, require_admin


def _ctx(section, **extra):
    return {"nav_active": "settings", "settings_section": section, **extra}


@login_required
def index(request):
    require_admin(request.user)
    return redirect(reverse("settings:company"))


@login_required
def company_profile(request):
    require_admin(request.user)
    config = company.profile()
    form = CompanyProfileForm(instance=config)
    if request.method == "POST":
        form = CompanyProfileForm(request.POST, request.FILES, instance=config)
        if form.is_valid():
            saved = form.save(commit=False)
            if form.logo_data:
                company.replace_logo(saved, form.logo_data, form.logo_extension)
            elif form.cleaned_data.get("remove_logo"):
                company.remove_logo(saved)
            saved.save()
            messages.success(request, "Company profile saved.")
            return redirect(reverse("settings:company"))
    return render(request, "core/settings/company.html", _ctx("company", form=form, has_logo=bool(config.logo)))


def _role(user) -> str:
    return "Admin" if is_admin(user) else "Standard"


@login_required
def users(request):
    require_admin(request.user)
    rows = [{"user": u, "role": _role(u)} for u in get_user_model().objects.order_by("username").prefetch_related("groups")]
    return render(request, "core/settings/users.html", _ctx("users", rows=rows))


def _save_user(form):
    with transaction.atomic():
        user = form.save()
        if not form.fields["role"].disabled:
            role = form.cleaned_data["role"]
            admin_group, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
            standard_group, _ = Group.objects.get_or_create(name=STANDARD_GROUP)
            user.groups.remove(admin_group, standard_group)
            user.groups.add(admin_group if role == ADMIN_GROUP else standard_group)
    return user


@login_required
def user_new(request):
    require_admin(request.user)
    form = UserForm(request.POST or None, initial={"role": STANDARD_GROUP, "is_active": True})
    if request.method == "POST" and form.is_valid():
        user = _save_user(form)
        messages.success(request, f"User {user.username} added.")
        return redirect(reverse("settings:users"))
    return render(request, "core/settings/user_form.html", _ctx("users", form=form, editing=None))


@login_required
def user_edit(request, pk):
    require_admin(request.user)
    target = get_object_or_404(get_user_model(), pk=pk)
    editing_self = target.pk == request.user.pk
    initial = {"role": ADMIN_GROUP if is_admin(target) else STANDARD_GROUP}
    form = UserForm(request.POST or None, instance=target, initial=initial, editing_self=editing_self)
    if request.method == "POST" and form.is_valid():
        user = _save_user(form)
        if editing_self and form.cleaned_data.get("password"):
            update_session_auth_hash(request, user)
        messages.success(request, f"User {user.username} saved.")
        return redirect(reverse("settings:users"))
    return render(request, "core/settings/user_form.html", _ctx(
        "users", form=form, editing=target, is_superuser=target.is_superuser))


@login_required
def advanced(request):
    require_admin(request.user)
    return render(request, "core/settings/advanced.html", _ctx("advanced"))
