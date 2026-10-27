from django.contrib import admin

from .models import BankAccount, BankTransaction, Rule, Transfer

for _model in (BankAccount, BankTransaction, Rule, Transfer):
    admin.site.register(_model)
