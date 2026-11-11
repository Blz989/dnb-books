from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import BankAccount, BankTransaction, Rule, Transfer

for _model in (BankAccount, Rule):
    admin.site.register(_model)
for _model in (BankTransaction, Transfer):
    admin.site.register(_model, ReadOnlyAdmin)
