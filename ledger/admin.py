from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html

from core.roles import is_admin

from .errors import PeriodCloseBlockedError, PostingError
from .forms import CloseAnywayForm, JournalLineForm, ReopenForm, VoidForm
from .models import Account, AccountingPeriod, EntryStatus, JournalEntry, JournalLine
from .periods import close_period, close_problems, reopen_period
from .posting import post_draft, void_entry


def _money(x) -> str:
    return f"{x:,.2f}"


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "type", "subtype", "is_active")
    list_filter = ("type", "is_active")
    search_fields = ("number", "name")
    ordering = ("number",)


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    form = JournalLineForm
    extra = 2

    def _parent_is_draft(self, obj):
        return obj is None or obj.status == EntryStatus.DRAFT

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and self._parent_is_draft(obj)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and self._parent_is_draft(obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and self._parent_is_draft(obj)


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    inlines = [JournalLineInline]
    change_form_template = "admin/ledger/journalentry/change_form.html"
    list_display = ("number", "date", "reference", "memo", "status", "total")
    list_filter = ("status", "date")
    search_fields = ("reference", "memo")
    actions = ["post_drafts"]
    fields = (
        "date", "reference", "memo", "status", "created_by", "posted_by", "posted_at",
        "reverses", "source_type", "source_id", "total_debit", "total_credit", "difference",
    )
    # Status and the posting fields only ever change through ledger/posting.py.
    readonly_fields = (
        "status", "created_by", "posted_by", "posted_at", "reverses", "source_type", "source_id",
        "total_debit", "total_credit", "difference",
    )

    @admin.display(description="Entry", ordering="id")
    def number(self, obj):
        return obj.display_number

    def _sums(self, obj):
        if obj is None or obj.pk is None:
            return None
        lines = list(obj.lines.all())
        return sum(l.debit for l in lines), sum(l.credit for l in lines)

    @admin.display(description="Total")
    def total(self, obj):
        return _money(self._sums(obj)[0])

    @admin.display(description="Total debits")
    def total_debit(self, obj):
        sums = self._sums(obj)
        return "-" if sums is None else _money(sums[0])

    @admin.display(description="Total credits")
    def total_credit(self, obj):
        sums = self._sums(obj)
        return "-" if sums is None else _money(sums[1])

    @admin.display(description="Difference")
    def difference(self, obj):
        sums = self._sums(obj)
        return "-" if sums is None else _money(abs(sums[0] - sums[1]))

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.status != EntryStatus.DRAFT:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != EntryStatus.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # drafts are deleted one at a time
        return actions

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Post selected drafts", permissions=["change"])
    def post_drafts(self, request, queryset):
        posted = 0
        for entry in queryset:
            try:
                post_draft(entry, request.user)
                posted += 1
            except PostingError as exc:
                self.message_user(request, f"{entry.display_number}: {exc}", messages.ERROR)
        if posted:
            self.message_user(request, f"Posted {posted} {'entry' if posted == 1 else 'entries'}.")

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra = dict(extra_context or {})
        obj = self.get_object(request, unquote(object_id))
        extra["show_void"] = bool(obj and obj.status == EntryStatus.POSTED and is_admin(request.user))
        return super().change_view(request, object_id, form_url, extra)

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/void/",
                self.admin_site.admin_view(self.void_view),
                name="ledger_journalentry_void",
            ),
        ]
        return custom + super().get_urls()

    def void_view(self, request, object_id):
        entry = self.get_object(request, unquote(object_id))
        if entry is None:
            raise Http404
        if not is_admin(request.user):
            raise PermissionDenied("Only an admin can do this.")
        form = VoidForm(request.POST or None, initial={"date": entry.date})
        if request.method == "POST" and form.is_valid():
            try:
                void_entry(
                    entry, user=request.user, reason=form.cleaned_data["reason"], date=form.cleaned_data["date"]
                )
            except PostingError as exc:
                form.add_error(None, str(exc))
            else:
                self.message_user(request, f"{entry.display_number} was voided.")
                return HttpResponseRedirect(reverse("admin:ledger_journalentry_change", args=[entry.pk]))
        context = {
            **self.admin_site.each_context(request),
            "title": f"Void {entry.display_number}",
            "entry": entry,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "ledger/void_entry.html", context)


@admin.register(AccountingPeriod)
class AccountingPeriodAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_closed", "closed_by", "closed_at", "reopen_reason", "reopen_link")
    list_filter = ("is_closed", "year")
    actions = ["close_periods"]
    fields = (
        "year", "month", "is_closed", "closed_by", "closed_at", "reopen_reason",
        "close_override_reason", "close_override_problems",
    )
    # Periods only open and close through ledger/periods.py so every change is recorded.
    readonly_fields = (
        "is_closed", "closed_by", "closed_at", "reopen_reason", "close_override_reason", "close_override_problems",
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="")
    def reopen_link(self, obj):
        if not obj.is_closed:
            return ""
        return format_html('<a href="{}">Reopen</a>', reverse("admin:ledger_accountingperiod_reopen", args=[obj.pk]))

    @admin.action(description="Close selected periods")
    def close_periods(self, request, queryset):
        if not is_admin(request.user):
            self.message_user(request, "Only an admin can do this.", messages.ERROR)
            return
        closed = 0
        for period in queryset:
            try:
                close_period(period.year, period.month, request.user)
                closed += 1
            except PeriodCloseBlockedError as exc:
                link = reverse("admin:ledger_accountingperiod_close", args=[period.pk])
                self.message_user(
                    request, format_html('{} <a href="{}">Close anyway</a>', f"{period}: {exc}", link), messages.ERROR
                )
            except PostingError as exc:
                self.message_user(request, str(exc), messages.ERROR)
        if closed:
            self.message_user(request, f"Closed {closed} period(s).")

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/reopen/",
                self.admin_site.admin_view(self.reopen_view),
                name="ledger_accountingperiod_reopen",
            ),
            path(
                "<path:object_id>/close/",
                self.admin_site.admin_view(self.close_view),
                name="ledger_accountingperiod_close",
            ),
        ]
        return custom + super().get_urls()

    def reopen_view(self, request, object_id):
        period = self.get_object(request, unquote(object_id))
        if period is None:
            raise Http404
        if not is_admin(request.user):
            raise PermissionDenied("Only an admin can do this.")
        form = ReopenForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            reopen_period(period.year, period.month, request.user, form.cleaned_data["reason"])
            self.message_user(request, f"{period} was reopened.")
            return HttpResponseRedirect(reverse("admin:ledger_accountingperiod_changelist"))
        context = {
            **self.admin_site.each_context(request),
            "title": f"Reopen {period}",
            "period": period,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "ledger/reopen_period.html", context)

    def close_view(self, request, object_id):
        period = self.get_object(request, unquote(object_id))
        if period is None:
            raise Http404
        if not is_admin(request.user):
            raise PermissionDenied("Only an admin can do this.")
        problems = close_problems(period.year, period.month)
        form = CloseAnywayForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            close_period(period.year, period.month, request.user, form.cleaned_data["reason"])
            self.message_user(request, f"{period} was closed.")
            return HttpResponseRedirect(reverse("admin:ledger_accountingperiod_changelist"))
        context = {
            **self.admin_site.each_context(request),
            "title": f"Close {period} anyway",
            "period": period,
            "problems": problems,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "admin/ledger/accountingperiod/close.html", context)
