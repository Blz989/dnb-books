from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from .roles import ADMIN_GROUP, STANDARD_GROUP

ROLE_CHOICES = [(ADMIN_GROUP, "Admin"), (STANDARD_GROUP, "Standard")]
ROLE_HELP = "Admins can change settings, void, close months, and manage users. Standard users do day-to-day work."


class UserForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, label="Role", help_text=ROLE_HELP)
    password = forms.CharField(
        label="Password", required=False, strip=False, widget=forms.PasswordInput(render_value=False),
        help_text="At least 8 characters. Leave blank to keep the current password.",
    )

    class Meta:
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email", "is_active"]
        labels = {"username": "Username", "first_name": "First name", "last_name": "Last name",
                  "email": "Email", "is_active": "Can sign in"}
        help_texts = {"username": "", "is_active": "Turn off to block sign-in without deleting the user."}

    def __init__(self, *args, editing_self=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing_self = editing_self
        self.fields["username"].error_messages["required"] = "Enter a username."
        self.fields["username"].error_messages["unique"] = "That username is taken."
        if self.instance.pk is None:
            self.fields["password"].required = True
            self.fields["password"].help_text = "At least 8 characters."
            self.fields["password"].error_messages["required"] = "Enter a password."
        if editing_self:
            # Keep the signed-in admin from locking themselves out.
            self.fields["role"].disabled = True
            self.fields["is_active"].disabled = True
            self.fields["role"].help_text = "You can't change your own role."

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        if password:
            try:
                validate_password(password, self.instance)
            except forms.ValidationError as exc:
                raise forms.ValidationError(exc.messages)
        return password

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("password"):
            user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user
