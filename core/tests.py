"""Auth-gate tests for the core platform layer.

Identity in this project is the user's email (core/identity.py matches
``request.user.email`` to a StaffMember), so the security boundary under test
here is: nobody can create an account or change an email except an admin.
Two layers enforce that and both are pinned here:

- The allauth adapters (``core/allauth_adapters.py``): Microsoft SSO only
  connects pre-provisioned users, and local signup is closed.
- The URL overrides in ``lmpm/urls.py``: the allauth endpoints that create
  accounts, manage emails, or reset passwords 404 for everyone, so an allauth
  upgrade re-exposing them fails these tests rather than shipping.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from allauth.core.exceptions import ImmediateHttpResponse

from .allauth_adapters import RestrictMicrosoftLoginAdapter
from .models import School, SchoolProfile


def make_user(email, *, is_superuser=False, is_active=True):
    """A Django User keyed by email (username mirrors it for uniqueness)."""
    return User.objects.create_user(
        username=email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
        is_active=is_active,
    )


def make_profile(user):
    school = School.objects.create(name="Test School")
    return SchoolProfile.objects.create(user=user, school=school)


class RestrictMicrosoftLoginAdapterTests(TestCase):
    """The SSO authorisation gate: pre-provisioned, active, school-linked."""

    def setUp(self):
        self.adapter = RestrictMicrosoftLoginAdapter()

    def _request(self):
        # pre_social_login uses django messages, which need a session-backed
        # storage that a bare RequestFactory request does not have.
        request = RequestFactory().get("/accounts/microsoft/login/callback/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    def _sociallogin(self, email):
        # The adapter only reads .user.email and calls .connect(); a stub keeps
        # the test about the authorisation decision, not allauth internals.
        return SimpleNamespace(user=SimpleNamespace(email=email), connect=mock.Mock())

    def _assert_denied(self, sociallogin):
        with self.assertRaises(ImmediateHttpResponse):
            self.adapter.pre_social_login(self._request(), sociallogin)
        sociallogin.connect.assert_not_called()

    # Catches the gate failing open for someone with no account at all.
    def test_unknown_email_is_denied(self):
        self._assert_denied(self._sociallogin("nobody@oxlip.test"))

    # Catches Microsoft accounts that supply no email slipping through.
    def test_blank_email_is_denied(self):
        self._assert_denied(self._sociallogin(""))

    # Catches deactivation not revoking SSO access.
    def test_inactive_user_is_denied(self):
        make_user("leaver@oxlip.test", is_active=False)
        self._assert_denied(self._sociallogin("leaver@oxlip.test"))

    # Catches the SchoolProfile gate being skipped for ordinary users.
    def test_user_without_school_profile_is_denied(self):
        make_user("noprofile@oxlip.test")
        self._assert_denied(self._sociallogin("noprofile@oxlip.test"))

    def test_user_with_school_profile_is_connected(self):
        user = make_user("staff@oxlip.test")
        make_profile(user)
        sociallogin = self._sociallogin("staff@oxlip.test")
        self.adapter.pre_social_login(self._request(), sociallogin)
        sociallogin.connect.assert_called_once()
        self.assertEqual(sociallogin.connect.call_args.args[1], user)

    # Superusers are exempt from the SchoolProfile requirement by design.
    def test_superuser_without_profile_is_connected(self):
        user = make_user("admin@oxlip.test", is_superuser=True)
        sociallogin = self._sociallogin("admin@oxlip.test")
        self.adapter.pre_social_login(self._request(), sociallogin)
        sociallogin.connect.assert_called_once()
        self.assertEqual(sociallogin.connect.call_args.args[1], user)

    # Catches the email comparison becoming case-sensitive: Entra may return
    # a differently-cased email than the one the admin provisioned.
    def test_email_match_is_case_insensitive(self):
        user = make_user("staff@oxlip.test")
        make_profile(user)
        sociallogin = self._sociallogin("STAFF@OXLIP.TEST")
        self.adapter.pre_social_login(self._request(), sociallogin)
        sociallogin.connect.assert_called_once()

    # Signup via the social flow is closed outright (defense in depth:
    # pre_social_login always connects or denies before signup is reached).
    def test_social_signup_is_closed(self):
        self.assertFalse(
            self.adapter.is_open_for_signup(self._request(), self._sociallogin("x@y.test"))
        )


class BlockedAccountEndpointTests(TestCase):
    """The allauth endpoints that could mint or hijack an identity must 404.

    Open signup or self-service email change would let anyone claim a staff
    member's email — and with it that person's data and everyone they manage.
    """

    BLOCKED_URLS = [
        "/accounts/signup/",
        "/accounts/email/",
        "/accounts/confirm-email/",
        "/accounts/confirm-email/some-key/",
        "/accounts/password/reset/",
        "/accounts/password/reset/done/",
        "/accounts/3rdparty/",
        "/accounts/3rdparty/signup/",
        "/accounts/social/signup/",
        "/accounts/social/connections/",
    ]

    def test_blocked_endpoints_404_for_anonymous(self):
        for url in self.BLOCKED_URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    # Catches the post-login variant of the identity hijack: a signed-in user
    # adding an unverified email at /accounts/email/ and making it primary.
    def test_blocked_endpoints_404_for_authenticated_user(self):
        user = make_user("staff@oxlip.test")
        make_profile(user)
        self.client.force_login(user)
        for url in self.BLOCKED_URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    # Catches signup being reachable by POST even with the page blocked.
    def test_signup_post_creates_no_user(self):
        response = self.client.post(
            "/accounts/signup/",
            {
                "email": "attacker@oxlip.test",
                "username": "attacker",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(User.objects.filter(email="attacker@oxlip.test").exists())

    # The sign-in surface itself must stay up: the login page (Microsoft
    # button + password fallback) and the Microsoft provider redirect.
    def test_login_page_still_works(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)

    def test_microsoft_login_route_still_exists(self):
        response = self.client.get("/accounts/microsoft/login/")
        self.assertNotEqual(response.status_code, 404)

    def test_password_login_still_works(self):
        user = make_user("staff@oxlip.test")
        make_profile(user)
        response = self.client.post(
            "/accounts/login/",
            {"login": "staff@oxlip.test", "password": "pw"},
        )
        self.assertEqual(response.status_code, 302)
