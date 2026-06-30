from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.shortcuts import redirect

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from allauth.core.exceptions import ImmediateHttpResponse

from .models import SchoolProfile


class RestrictMicrosoftLoginAdapter(DefaultSocialAccountAdapter):
    """Only allow Microsoft SSO logins for pre-provisioned emails.

    Security model:
    - Microsoft (Entra) authenticates the person.
    - We authorize by checking the email exists as a Django User and has a
      SchoolProfile.
    """

    def pre_social_login(self, request: HttpRequest, sociallogin: SocialLogin):
        email = (sociallogin.user.email or "").strip().lower()
        if not email:
            self._deny(request, "Your Microsoft account did not provide an email address.")

        # Match an existing, pre-provisioned user.
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            self._deny(request, "You are not authorised to use this service.")

        # Superusers can access everything; they do not require a SchoolProfile.
        if user.is_superuser:
            sociallogin.connect(request, user)
            return

        # Require a SchoolProfile (and schools are managed there).
        if not SchoolProfile.objects.filter(user=user).exists():
            self._deny(request, "Your account is not configured with a school yet.")

        # Link the social account to the existing user.
        sociallogin.connect(request, user)

    def _deny(self, request: HttpRequest, message: str) -> None:
        messages.error(request, message)
        raise ImmediateHttpResponse(redirect("account_login"))
