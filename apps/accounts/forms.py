from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import User, UserKey, UserHyperLiquidKey


class RegistrationForm(UserCreationForm):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Username"}
        ),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": "form-control", "placeholder": "Email"})
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "placeholder": "Password"}
        ),
    )
    password2 = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "placeholder": "Confirm Password"}
        ),
    )

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def save(self, commit=True):
        user = super().save(commit=False)
        # ensure case_insensitive_username is stored
        user.case_insensitive_username = self.cleaned_data["username"].lower()
        if commit:
            user.save()
        return user


class UserKeyForm(forms.Form):
    api_key = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Binance API Key",
                "autocomplete": "off",
            }
        ),
    )
    api_secret = forms.CharField(
        max_length=255,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Binance API Secret",
                "autocomplete": "off",
            },
            render_value=False,
        ),
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class UserHyperLiquidKeyForm(forms.Form):
    master_wallet_address = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Master Wallet Address",
                "autocomplete": "off",
            }
        ),
    )
    api_wallet_address = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "API Wallet Address",
                "autocomplete": "off",
            }
        ),
    )
    api_private_key = forms.CharField(
        max_length=255,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "API Private Key",
                "autocomplete": "off",
            },
            render_value=False,
        ),
    )
    api_valid_days = forms.IntegerField(
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "placeholder": "API Valid Days",
            }
        ),
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class LoginForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Username"}
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "placeholder": "Password"}
        )
    )
