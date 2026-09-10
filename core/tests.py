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

import io
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.template.loader import render_to_string
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.urls import reverse

from allauth.core.exceptions import ImmediateHttpResponse

from appraisals.models import AcademicYear, Appraisal, Goal, SelfReview, SelfReviewItem
from line_management.models import LineMeeting

from .allauth_adapters import RestrictMicrosoftLoginAdapter
from .recovery import submitted_text
from .models import School, SchoolProfile, StaffMember
from .provisioning import (
    LABEL_ADMIN,
    LABEL_DEACTIVATED,
    LABEL_DUPLICATE,
    LABEL_NO_EMAIL,
    LABEL_NO_LOGIN,
    LABEL_NO_SCHOOL_LINK,
    LABEL_NO_SCHOOL_SET,
    LABEL_YES,
    OUTCOME_MESSAGES,
    ProvisionOutcome,
    annotate_login_state,
    login_state_label,
    provision_staff_member,
)


def make_user(email, *, is_superuser=False, is_active=True, username=None):
    """A Django User keyed by email (username mirrors it for uniqueness).

    ``username`` is only passed explicitly when a test needs two accounts on
    one email address (auth.User.email is not unique, but username is) — the
    duplicate-login case core.provisioning refuses to guess about.
    """
    return User.objects.create_user(
        username=username or email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
        is_active=is_active,
    )


def make_profile(user):
    school = School.objects.create(name="Test School")
    return SchoolProfile.objects.create(user=user, school=school)


def message_request(user=None, path="/admin/core/staffmember/"):
    """A RequestFactory POST carrying the session-backed message storage the
    admin needs (same shape as the _request helpers on the classes below)."""
    request = RequestFactory().post(path)
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    if user is not None:
        request.user = user
    return request


def message_texts(request):
    return [str(message) for message in request._messages]


def stub_sociallogin(email):
    """The adapter only reads .user.email and calls .connect(); a stub keeps
    the test about the authorisation decision, not allauth internals."""
    return SimpleNamespace(user=SimpleNamespace(email=email), connect=mock.Mock())


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


class CheckReadinessCommandTests(TestCase):
    """The check_readiness audit surfaces the onboarding dead-ends that block a
    smooth first login: no active year, unclassified staff, staff with no login,
    logins with no staff record, and dangling manager links. Read-only.
    """

    def _run(self):
        """Run the command, capturing stdout and whether it exited non-zero."""
        out = io.StringIO()
        blocked = False
        try:
            call_command("check_readiness", stdout=out)
        except SystemExit:
            blocked = True
        return out.getvalue(), blocked

    def _healthy_staff(self, email):
        """A fully-provisioned, classified staff member with a login + profile."""
        school = School.objects.create(name=f"School {email}")
        user = make_user(email)
        SchoolProfile.objects.create(user=user, school=school)
        return StaffMember.objects.create(
            email=email, staff_type=StaffMember.StaffType.TEACHING, school=school
        )

    def test_all_clean_passes(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        self._healthy_staff("ok@oxlip.test")
        output, blocked = self._run()
        self.assertFalse(blocked)
        self.assertIn("All readiness checks passed", output)

    def test_no_current_year_is_blocker(self):
        self._healthy_staff("ok@oxlip.test")  # otherwise-clean data
        output, blocked = self._run()
        self.assertTrue(blocked)
        self.assertIn("No active appraisal cycle", output)

    def test_unclassified_staff_flagged(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school = School.objects.create(name="S")
        user = make_user("blank@oxlip.test")
        SchoolProfile.objects.create(user=user, school=school)
        StaffMember.objects.create(email="blank@oxlip.test", school=school)
        output, blocked = self._run()
        self.assertTrue(blocked)
        self.assertIn("no staff_type", output)
        self.assertIn("blank@oxlip.test", output)

    def test_staff_without_login_flagged(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school = School.objects.create(name="S")
        StaffMember.objects.create(
            email="nologin@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=school,
        )
        output, blocked = self._run()
        self.assertTrue(blocked)
        self.assertIn("no login account", output)
        self.assertIn("nologin@oxlip.test", output)

    def test_login_without_staff_record_is_warning(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        make_user("orphan@oxlip.test")  # a User with no StaffMember
        output, blocked = self._run()
        # A warning, not a blocker — the command still exits zero.
        self.assertFalse(blocked)
        self.assertIn("no staff record", output)
        self.assertIn("orphan@oxlip.test", output)

    def test_dangling_manager_link_is_warning(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        self._healthy_staff("ok@oxlip.test")
        StaffMember.objects.filter(email="ok@oxlip.test").update(
            line_manager_email="ghost@oxlip.test"
        )
        output, blocked = self._run()
        self.assertFalse(blocked)
        self.assertIn("dangling manager link", output)
        self.assertIn("ghost@oxlip.test", output)

    def test_superuser_login_without_staff_is_not_flagged(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        make_user("admin@oxlip.test", is_superuser=True)
        output, blocked = self._run()
        self.assertFalse(blocked)

    # Catches a leaver's deliberate deactivation being reported as a blocker
    # telling the operator to run provision_users — which would invite them to
    # hand a departed member of staff their access back, and would leave the
    # readiness check permanently red.
    def test_staff_with_only_a_deactivated_login_is_info_not_blocker(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school = School.objects.create(name="S")
        make_user("leaver@oxlip.test", is_active=False)
        StaffMember.objects.create(
            email="leaver@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=school,
        )
        output, blocked = self._run()
        self.assertFalse(blocked)
        self.assertIn("deactivated login", output)
        self.assertIn("leaver@oxlip.test", output)
        self.assertNotIn("no login account", output)

    # A staff member with no User at all is still a blocker — the split above
    # must not swallow the genuine "never provisioned" case.
    def test_staff_with_no_user_at_all_is_still_a_blocker(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school = School.objects.create(name="S")
        make_user("leaver@oxlip.test", is_active=False)
        StaffMember.objects.create(
            email="leaver@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=school,
        )
        StaffMember.objects.create(
            email="never@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=school,
        )
        output, blocked = self._run()
        self.assertTrue(blocked)
        self.assertIn("no login account", output)
        self.assertIn("never@oxlip.test", output)

    # Catches duplicate active logins going unreported: the SSO gate picks one
    # of them, so if the other holds the SchoolProfile, access is a coin flip.
    def test_duplicate_active_logins_on_one_email_is_blocker(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school = School.objects.create(name="S")
        first = make_user("dup@oxlip.test", username="dup-1")
        make_user("dup@oxlip.test", username="dup-2")
        SchoolProfile.objects.create(user=first, school=school)
        StaffMember.objects.create(
            email="dup@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=school,
        )
        output, blocked = self._run()
        self.assertTrue(blocked)
        self.assertIn("more than one login", output)
        self.assertIn("dup@oxlip.test (2 active accounts)", output)

    # Catches the "no school so no login" warning being reported for someone
    # who demonstrably has one: clearing the school from a working account does
    # not revoke anything, so the warning would simply be false — and a check
    # that cries wolf is a check nobody reads.
    def test_no_school_warning_is_not_raised_for_an_already_provisioned_person(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school = School.objects.create(name="S")
        user = make_user("ok@oxlip.test")
        SchoolProfile.objects.create(user=user, school=school)
        StaffMember.objects.create(
            email="ok@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=None,
        )
        output, blocked = self._run()
        self.assertFalse(blocked)
        self.assertNotIn("have no school", output)
        self.assertIn("All readiness checks passed", output)

    # ... and the other direction: for someone with no login it is exactly the
    # blocking fact, and must still be reported.
    def test_no_school_warning_is_raised_for_someone_not_yet_provisioned(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        StaffMember.objects.create(
            email="waiting@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
            school=None,
        )
        output, blocked = self._run()
        self.assertTrue(blocked)
        self.assertIn("have no school", output)
        self.assertIn("waiting@oxlip.test", output)


class StaffTypeAdminActionTests(TestCase):
    """The bulk 'Set staff type' admin actions reclassify selected staff."""

    def setUp(self):
        from django.contrib.admin.sites import AdminSite

        from .admin import StaffMemberAdmin

        self.admin = StaffMemberAdmin(StaffMember, AdminSite())
        self.a = StaffMember.objects.create(email="a@oxlip.test")
        self.b = StaffMember.objects.create(email="b@oxlip.test")

    def _request(self):
        request = RequestFactory().post("/admin/core/staffmember/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    def test_bulk_action_sets_type_on_selected_only(self):
        queryset = StaffMember.objects.filter(pk=self.a.pk)
        self.admin.set_type_leader(self._request(), queryset)

        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.staff_type, StaffMember.StaffType.LEADER)
        self.assertEqual(self.b.staff_type, "")  # untouched


class ProvisionStaffMemberTests(TestCase):
    """The access-grant rules themselves (core/provisioning.py).

    Provisioning mints an SSO login: an active User plus the SchoolProfile that
    IS the authorisation gate. Every skip below is a deliberate refusal to grant
    access, so these are the tests a future refactor is most likely to break.
    """

    def setUp(self):
        self.school = School.objects.create(name="Copleston")
        self.other_school = School.objects.create(name="Northgate")
        self.staff = StaffMember.objects.create(
            email="teacher@oxlip.test", school=self.school
        )

    # Catches provisioning silently reactivating a leaver: deactivating the
    # User is how access is revoked, and a save must never undo it.
    def test_inactive_user_is_never_reactivated(self):
        user = make_user("teacher@oxlip.test", is_active=False)

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.SKIPPED_INACTIVE_USER)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(SchoolProfile.objects.filter(user=user).exists())

    # Catches provisioning guessing which of two accounts is the real one. The
    # SSO gate picks the lowest id; putting a profile on the wrong one grants
    # access to an account nobody meant to grant it to.
    def test_duplicate_active_users_are_refused(self):
        make_user("teacher@oxlip.test", username="dup-1")
        make_user("teacher@oxlip.test", username="dup-2")

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.SKIPPED_DUPLICATE_USER)
        self.assertEqual(SchoolProfile.objects.count(), 0)
        self.assertEqual(User.objects.count(), 2)

    # Catches a usable local password being set on a created account: auth is
    # SSO-only, so a password here would be a second, unmonitored way in.
    def test_created_user_has_no_usable_password(self):
        provision_staff_member(self.staff)

        user = User.objects.get(email="teacher@oxlip.test")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.check_password("anything"))
        self.assertFalse(user.check_password(""))

    # Superusers bypass the SchoolProfile gate already; touching them would add
    # rows that mean nothing and could only confuse a later audit.
    def test_active_superuser_is_skipped_and_untouched(self):
        admin_user = make_user("teacher@oxlip.test", is_superuser=True)

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.SKIPPED_SUPERUSER)
        admin_user.refresh_from_db()
        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.is_active)
        self.assertFalse(SchoolProfile.objects.filter(user=admin_user).exists())

    # Catches the superuser check running over ALL matching users rather than
    # the active ones: a long-deactivated admin account on this email would
    # then report "already has full access", which is false, and the real staff
    # member would never be provisioned and never be told why.
    def test_inactive_superuser_reports_deactivated_not_administrator(self):
        make_user("teacher@oxlip.test", is_superuser=True, is_active=False)

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.SKIPPED_INACTIVE_USER)
        self.assertEqual(SchoolProfile.objects.count(), 0)

    # Catches provisioning stopping being safe to call on every admin save.
    def test_provisioning_twice_is_a_no_op_the_second_time(self):
        first = provision_staff_member(self.staff)
        self.assertIs(first, ProvisionOutcome.CREATED_BOTH)
        users, profiles = User.objects.count(), SchoolProfile.objects.count()

        second = provision_staff_member(self.staff)

        self.assertIs(second, ProvisionOutcome.ALREADY_OK)
        self.assertEqual(User.objects.count(), users)
        self.assertEqual(SchoolProfile.objects.count(), profiles)

    # Catches an IntegrityError 500 (and a rolled-back changelist batch) when a
    # legacy account holds this email as its USERNAME but carries a different
    # email, so the email lookup cannot see it.
    def test_username_collision_is_reported_not_crashed(self):
        User.objects.create_user(
            username="teacher@oxlip.test", email="old-address@oxlip.test"
        )

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.SKIPPED_USERNAME_TAKEN)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SchoolProfile.objects.count(), 0)

    # ... and the dry run must predict the same refusal rather than promising a
    # create that cannot happen.
    def test_username_collision_is_predicted_by_dry_run(self):
        User.objects.create_user(
            username="teacher@oxlip.test", email="old-address@oxlip.test"
        )

        outcome = provision_staff_member(self.staff, dry_run=True)

        self.assertIs(outcome, ProvisionOutcome.SKIPPED_USERNAME_TAKEN)

    # Catches --dry-run granting real access.
    def test_dry_run_writes_nothing(self):
        users, profiles = User.objects.count(), SchoolProfile.objects.count()

        outcome = provision_staff_member(self.staff, dry_run=True)

        self.assertIs(outcome, ProvisionOutcome.CREATED_BOTH)
        self.assertEqual(User.objects.count(), users)
        self.assertEqual(SchoolProfile.objects.count(), profiles)

    # Catches the email join becoming case-sensitive. StaffMember.save()
    # normalises, so this forces the mixed case past it the way a bulk .update()
    # or a raw import could - the lookup itself must not depend on that.
    def test_email_match_is_case_insensitive(self):
        user = make_user("teacher@oxlip.test")
        StaffMember.objects.filter(pk=self.staff.pk).update(
            email="Teacher@Oxlip.Test"
        )
        self.staff.refresh_from_db()

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.CREATED_PROFILE)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(SchoolProfile.objects.filter(user=user).exists())

    # Catches a school change leaving the SSO gate pointing at the old school,
    # and catches the `schools` M2M silently accumulating every school a person
    # has ever been at (it is only ever added to, never removed).
    def test_changing_school_repoints_the_profile_without_growing_the_m2m(self):
        provision_staff_member(self.staff)
        self.staff.school = self.other_school
        self.staff.save()

        outcome = provision_staff_member(self.staff)

        self.assertIs(outcome, ProvisionOutcome.UPDATED_PROFILE_SCHOOL)
        profile = SchoolProfile.objects.get(user__email="teacher@oxlip.test")
        self.assertEqual(profile.school, self.other_school)
        self.assertEqual(list(profile.schools.all()), [self.school])


class StaffMemberAdminProvisioningTests(TestCase):
    """Provisioning through the admin: the retry-on-next-save behaviour the
    feature exists for, and the superuser gate on both entry points."""

    def setUp(self):
        from django.contrib.admin.sites import AdminSite

        from .admin import StaffMemberAdmin

        self.admin = StaffMemberAdmin(StaffMember, AdminSite())
        self.school = School.objects.create(name="Copleston")
        self.superuser = make_user("admin@oxlip.test", is_superuser=True)
        self.plain_staff_user = User.objects.create_user(
            username="office@oxlip.test",
            email="office@oxlip.test",
            password="pw",
            is_staff=True,
        )

    def _save(self, staff, user, change=True):
        """Drive StaffMemberAdmin.save_model the way the admin does."""
        request = message_request(user)
        self.admin.save_model(
            request, staff, SimpleNamespace(changed_data=[]), change
        )
        return request

    # THE headline case: a staff member added before their school is known must
    # be reported as not-yet-provisionable, not silently ignored.
    def test_staff_member_without_school_is_not_provisioned_and_says_why(self):
        staff = StaffMember(email="teacher@oxlip.test")

        request = self._save(staff, self.superuser, change=False)

        self.assertFalse(User.objects.filter(email="teacher@oxlip.test").exists())
        self.assertEqual(SchoolProfile.objects.count(), 0)
        self.assertIn(
            OUTCOME_MESSAGES[ProvisionOutcome.SKIPPED_NO_SCHOOL],
            " ".join(message_texts(request)),
        )

    # ... and the reason the change was made: setting the school and saving
    # AGAIN finishes the job. The old command gave up permanently on a
    # school-less row, so the person stayed locked out with nothing to say why.
    def test_setting_the_school_and_saving_again_provisions_them(self):
        staff = StaffMember(email="teacher@oxlip.test")
        self._save(staff, self.superuser, change=False)

        staff.school = self.school
        request = self._save(staff, self.superuser)

        user = User.objects.get(email="teacher@oxlip.test")
        self.assertTrue(user.is_active)
        profile = SchoolProfile.objects.get(user=user)
        self.assertEqual(profile.school, self.school)
        self.assertIn(
            OUTCOME_MESSAGES[ProvisionOutcome.CREATED_BOTH],
            " ".join(message_texts(request)),
        )

    # End-to-end: the rows created above are the ones the SSO gate reads, so
    # the person can genuinely sign in afterwards. Catches provisioning writing
    # something the adapter does not accept.
    def test_provisioned_staff_member_passes_the_sso_gate(self):
        staff = StaffMember(email="teacher@oxlip.test")
        self._save(staff, self.superuser, change=False)
        staff.school = self.school
        self._save(staff, self.superuser)

        sociallogin = stub_sociallogin("teacher@oxlip.test")
        RestrictMicrosoftLoginAdapter().pre_social_login(
            message_request(path="/accounts/microsoft/login/callback/"), sociallogin
        )

        sociallogin.connect.assert_called_once()
        self.assertEqual(
            sociallogin.connect.call_args.args[1],
            User.objects.get(email="teacher@oxlip.test"),
        )

    # Catches the changelist becoming a login-minting machine: list_editable
    # makes Django call save_model once per changed row, checking only
    # has_change_permission - so a non-superuser could tick every row, nudge a
    # dropdown, and grant the whole trust live SSO accounts.
    def test_non_superuser_save_does_not_provision(self):
        staff = StaffMember(email="teacher@oxlip.test", school=self.school)

        self._save(staff, self.plain_staff_user, change=False)

        self.assertFalse(User.objects.filter(email="teacher@oxlip.test").exists())
        self.assertEqual(SchoolProfile.objects.count(), 0)

    def test_superuser_save_does_provision(self):
        staff = StaffMember(email="teacher@oxlip.test", school=self.school)

        self._save(staff, self.superuser, change=False)

        user = User.objects.get(email="teacher@oxlip.test")
        self.assertTrue(SchoolProfile.objects.filter(user=user).exists())

    # Catches the bulk action relying on permissions=["change"] alone: Django
    # appends actions without allowed_permissions unconditionally, and
    # admin_view only checks is_active and is_staff.
    def test_provision_logins_action_denied_to_non_superuser(self):
        StaffMember.objects.create(email="teacher@oxlip.test", school=self.school)
        request = message_request(self.plain_staff_user)

        with self.assertRaises(PermissionDenied):
            self.admin.provision_logins(request, StaffMember.objects.all())

        self.assertFalse(User.objects.filter(email="teacher@oxlip.test").exists())

    # Catches the action provisioning everyone rather than the selection -
    # granting access to staff the administrator never picked.
    def test_provision_logins_action_touches_only_the_selection(self):
        chosen = StaffMember.objects.create(
            email="chosen@oxlip.test", school=self.school
        )
        StaffMember.objects.create(email="other@oxlip.test", school=self.school)

        self.admin.provision_logins(
            message_request(self.superuser), StaffMember.objects.filter(pk=chosen.pk)
        )

        self.assertTrue(
            SchoolProfile.objects.filter(user__email="chosen@oxlip.test").exists()
        )
        self.assertFalse(User.objects.filter(email="other@oxlip.test").exists())


class LoginStateLabelTests(TestCase):
    """The "Can sign in?" changelist column. An administrator acts on what this
    says, so a wrong "Yes" is worse than no column at all."""

    def setUp(self):
        self.school = School.objects.create(name="Copleston")

    def _label(self, staff):
        annotated = annotate_login_state(
            StaffMember.objects.filter(pk=staff.pk)
        ).get()
        return login_state_label(annotated)

    def _staff(self, email):
        return StaffMember.objects.create(email=email, school=self.school)

    def test_fully_provisioned_staff_member_is_yes(self):
        user = make_user("ok@oxlip.test")
        SchoolProfile.objects.create(user=user, school=self.school)
        self.assertEqual(self._label(self._staff("ok@oxlip.test")), LABEL_YES)

    # Superusers sign in without a SchoolProfile, so the profile flag alone
    # would report them as broken.
    def test_superuser_is_labelled_administrator(self):
        make_user("admin@oxlip.test", is_superuser=True)
        self.assertEqual(self._label(self._staff("admin@oxlip.test")), LABEL_ADMIN)

    def test_staff_member_with_no_email_is_labelled_no_email(self):
        self.assertEqual(self._label(self._staff("")), LABEL_NO_EMAIL)

    def test_staff_member_with_no_user_is_labelled_no_login(self):
        self.assertEqual(
            self._label(self._staff("nologin@oxlip.test")), LABEL_NO_LOGIN
        )

    # Catches the school-less row reading as "no login account". It is true but
    # useless: it points the administrator at the bulk action, which then
    # refuses because there is no school to build a SchoolProfile from. Name
    # the actionable problem instead — this is the case that prompted the
    # feature, so it must not be the vague one.
    def test_staff_member_with_no_school_is_labelled_no_school_set(self):
        staff = StaffMember.objects.create(email="noschool@oxlip.test")

        self.assertEqual(self._label(staff), LABEL_NO_SCHOOL_SET)

    # Catches the new branch swallowing the old one: with a school set and no
    # User, "no login account" is still the right answer, and the two labels
    # must stay distinguishable.
    def test_no_school_set_and_no_login_are_different_answers(self):
        with_school = self._staff("hasschool@oxlip.test")
        without_school = StaffMember.objects.create(email="noschool@oxlip.test")

        self.assertEqual(self._label(with_school), LABEL_NO_LOGIN)
        self.assertEqual(self._label(without_school), LABEL_NO_SCHOOL_SET)
        self.assertNotEqual(LABEL_NO_LOGIN, LABEL_NO_SCHOOL_SET)
        # And neither is the "has a login, but it carries no school" case.
        self.assertNotEqual(LABEL_NO_SCHOOL_SET, LABEL_NO_SCHOOL_LINK)

    def test_user_without_profile_is_labelled_no_school_link(self):
        make_user("noprofile@oxlip.test")
        self.assertEqual(
            self._label(self._staff("noprofile@oxlip.test")), LABEL_NO_SCHOOL_LINK
        )

    # Catches a deliberate revocation reading as "no login account", which
    # invites an administrator to "fix" it and restore a leaver's access.
    def test_deactivated_login_is_labelled_deactivated_not_missing(self):
        make_user("leaver@oxlip.test", is_active=False)
        self.assertEqual(
            self._label(self._staff("leaver@oxlip.test")), LABEL_DEACTIVATED
        )

    # The one wrong answer an administrator would act on: the SSO gate signs
    # this person into the LOWEST-id account, which has no SchoolProfile, so
    # they are denied - while an independent has_profile subquery sees the
    # profile on the other account and says "Yes".
    def test_duplicate_logins_are_flagged_even_when_one_has_a_profile(self):
        make_user("dup@oxlip.test", username="dup-1")
        second = make_user("dup@oxlip.test", username="dup-2")
        SchoolProfile.objects.create(user=second, school=self.school)

        self.assertEqual(self._label(self._staff("dup@oxlip.test")), LABEL_DUPLICATE)

    # Catches the column becoming an N+1 - two queries per row on a 3,000-staff
    # changelist is a timeout, and the column would be turned off rather than
    # fixed.
    def test_login_state_costs_a_fixed_number_of_queries(self):
        def build(start, stop):
            for i in range(start, stop):
                email = f"staff{i}@oxlip.test"
                user = make_user(email)
                SchoolProfile.objects.create(user=user, school=self.school)
                self._staff(email)

        def render():
            return [
                login_state_label(staff)
                for staff in annotate_login_state(StaffMember.objects.all())
            ]

        build(0, 3)
        with self.assertNumQueries(1):
            self.assertEqual(len(render()), 3)

        build(3, 30)
        with self.assertNumQueries(1):
            labels = render()
        self.assertEqual(len(labels), 30)
        self.assertEqual(set(labels), {LABEL_YES})


class LoginStateFilterTests(TestCase):
    """The "can sign in" changelist filter.

    Driven through the real changelist so the filter and the annotations in
    get_queryset are exercised together — a filter that reads an annotation the
    admin does not apply would 500 in production and pass a unit test.
    """

    def setUp(self):
        self.school = School.objects.create(name="Copleston")
        self.superuser = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.superuser)
        self.url = reverse("admin:core_staffmember_changelist")

        # One row in every state the column can report.
        ok_user = make_user("ok@oxlip.test")
        SchoolProfile.objects.create(user=ok_user, school=self.school)
        self.ok = StaffMember.objects.create(email="ok@oxlip.test", school=self.school)
        self.admin_row = StaffMember.objects.create(
            email="admin@oxlip.test", school=self.school
        )
        self.no_login = StaffMember.objects.create(
            email="nologin@oxlip.test", school=self.school
        )
        self.no_school_set = StaffMember.objects.create(email="noschool@oxlip.test")
        make_user("noprofile@oxlip.test")
        self.no_school_link = StaffMember.objects.create(
            email="noprofile@oxlip.test", school=self.school
        )
        make_user("leaver@oxlip.test", is_active=False)
        self.deactivated = StaffMember.objects.create(
            email="leaver@oxlip.test", school=self.school
        )
        make_user("dup@oxlip.test", username="dup-1")
        dup_two = make_user("dup@oxlip.test", username="dup-2")
        SchoolProfile.objects.create(user=dup_two, school=self.school)
        self.duplicate = StaffMember.objects.create(
            email="dup@oxlip.test", school=self.school
        )

    def _filtered(self, value=None):
        url = self.url if value is None else f"{self.url}?login_state={value}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return set(response.context["cl"].queryset.values_list("email", flat=True))

    # Catches the two "no login" options overlapping. They name different
    # remedies — set a school, versus run the bulk action — so a row appearing
    # under both would send the administrator down the path that refuses.
    def test_no_login_and_no_school_set_are_disjoint(self):
        no_login = self._filtered("no_login")
        no_school_set = self._filtered("no_school_set")

        self.assertEqual(no_login, {"nologin@oxlip.test"})
        self.assertEqual(no_school_set, {"noschool@oxlip.test"})
        self.assertEqual(no_login & no_school_set, set())

    # Catches a state falling through both options and becoming invisible, or
    # being counted twice — "show me everyone who cannot sign in" has to be
    # exactly the complement of "yes" or it cannot be trusted after an import.
    def test_yes_and_any_problem_partition_every_row(self):
        everyone = self._filtered()
        can = self._filtered("yes")
        cannot = self._filtered("any_problem")

        self.assertEqual(can | cannot, everyone)
        self.assertEqual(can & cannot, set())
        # Administrators sign in without a SchoolProfile; duplicates do not
        # sign in despite having one.
        self.assertEqual(can, {"ok@oxlip.test", "admin@oxlip.test"})
        self.assertIn("dup@oxlip.test", cannot)
        self.assertIn("noschool@oxlip.test", cannot)


class NonSuperuserAdminPathTests(TestCase):
    """What a staff user with plain change rights sees and cannot do.

    They can edit staff records but cannot grant access. The risk is in both
    directions: a silent save that mints nothing (the dead end this feature
    exists to remove), and any route by which they could mint one anyway.
    """

    def setUp(self):
        from django.contrib.admin.sites import AdminSite

        from .admin import StaffMemberAdmin

        self.admin = StaffMemberAdmin(StaffMember, AdminSite())
        self.school = School.objects.create(name="Copleston")
        self.superuser = make_user("admin@oxlip.test", is_superuser=True)
        self.office = User.objects.create_user(
            username="office@oxlip.test",
            email="office@oxlip.test",
            password="pw",
            is_staff=True,
        )
        # Give them the real change permission. Without it the action would be
        # filtered out by permissions=["change"] anyway and this suite would
        # pass with the is_superuser check deleted.
        self.office.user_permissions.add(
            Permission.objects.get(
                content_type=ContentType.objects.get_for_model(StaffMember),
                codename="change_staffmember",
            )
        )
        self.office = User.objects.get(pk=self.office.pk)  # drop the perm cache

    def _save(self, staff, user, change=True):
        request = message_request(user)
        self.admin.save_model(
            request, staff, SimpleNamespace(changed_data=[]), change
        )
        return request

    # Catches the non-superuser path returning silently: the record saves, a
    # green "added successfully" appears, and nothing says the person still has
    # no way in. That is the same dead end, moved onto a different desk.
    def test_non_superuser_save_says_the_person_cannot_sign_in_yet(self):
        staff = StaffMember(email="teacher@oxlip.test", school=self.school)

        request = self._save(staff, self.office, change=False)

        self.assertIn(
            "only a trust administrator can set up logins",
            " ".join(message_texts(request)),
        )
        self.assertFalse(User.objects.filter(email="teacher@oxlip.test").exists())

    # ... and not crying wolf on every edit of somebody who is already fine.
    def test_non_superuser_save_is_silent_when_the_person_can_sign_in(self):
        user = make_user("teacher@oxlip.test")
        SchoolProfile.objects.create(user=user, school=self.school)
        staff = StaffMember.objects.create(
            email="teacher@oxlip.test", school=self.school
        )

        request = self._save(staff, self.office)

        self.assertNotIn("cannot sign in yet", " ".join(message_texts(request)))

    # Catches the action being offered to someone whose only possible outcome
    # is a 403 page.
    def test_provision_logins_is_hidden_from_a_non_superuser(self):
        actions = self.admin.get_actions(message_request(self.office))

        self.assertNotIn("provision_logins", actions)
        # The harmless bulk actions must survive the removal.
        self.assertIn("set_type_teaching", actions)

    def test_provision_logins_is_offered_to_a_superuser(self):
        actions = self.admin.get_actions(message_request(self.superuser))

        self.assertIn("provision_logins", actions)

    # The security-relevant half of hiding it: hiding a <option> means nothing
    # if the POST still runs. Django refuses an action it did not offer, so
    # nobody is provisioned.
    def test_non_superuser_posting_the_action_directly_provisions_nobody(self):
        staff = StaffMember.objects.create(
            email="teacher@oxlip.test", school=self.school
        )
        self.client.force_login(self.office)

        response = self.client.post(
            reverse("admin:core_staffmember_changelist"),
            {
                "action": "provision_logins",
                "index": "0",
                "_selected_action": [str(staff.pk)],
            },
        )

        self.assertIn(response.status_code, (200, 302))
        self.assertFalse(User.objects.filter(email="teacher@oxlip.test").exists())
        self.assertEqual(SchoolProfile.objects.count(), 0)


class StaffMemberDeleteWarningTests(TestCase):
    """Deleting a StaffMember is the obvious way to offboard someone, and it
    leaves the login untouched — so the person still passes the SSO gate."""

    def setUp(self):
        from django.contrib.admin.sites import AdminSite

        from .admin import StaffMemberAdmin

        self.admin = StaffMemberAdmin(StaffMember, AdminSite())
        self.school = School.objects.create(name="Copleston")
        self.superuser = make_user("admin@oxlip.test", is_superuser=True)

    def _provisioned(self, email):
        staff = StaffMember.objects.create(email=email, school=self.school)
        provision_staff_member(staff)
        return staff

    # Catches a deletion that silently leaves a working login behind: the
    # leaver can still sign in, they just land on "couldn't find a staff
    # record" — which reads like a bug, not like retained access.
    def test_deleting_provisioned_staff_warns_the_login_survives(self):
        staff = self._provisioned("leaver@oxlip.test")
        request = message_request(self.superuser)

        self.admin.delete_model(request, staff)

        texts = " ".join(message_texts(request))
        self.assertIn("leaver@oxlip.test", texts)
        self.assertIn("still exist and still work", texts)
        # The warning must be true: the User really is still there and active.
        self.assertTrue(
            User.objects.filter(email="leaver@oxlip.test", is_active=True).exists()
        )
        self.assertFalse(StaffMember.objects.filter(pk=staff.pk).exists())

    # Catches the orphan-login lookup being case-sensitive while the SSO gate
    # is not. auth.User.email is normalised nowhere: provisioning lowercases
    # what IT creates, so the accounts stored with capitals are exactly the
    # hand-made and legacy ones — and an exact match leaves those unwarned. The
    # admin deletes the staff record, sees a clean success page, and the leaver
    # keeps working access.
    def test_deleting_staff_warns_when_the_login_email_is_stored_mixed_case(self):
        user = make_user("Leaver@Oxlip.Test")
        # Guard the fixture: Django lowercases the domain part on create_user,
        # so assert the stored value really is still mixed case. Without this
        # the test could quietly become a duplicate of the lowercase one.
        user.refresh_from_db()
        self.assertNotEqual(user.email, user.email.lower())
        staff = StaffMember.objects.create(
            email="leaver@oxlip.test", school=self.school
        )
        request = message_request(self.superuser)

        self.admin.delete_model(request, staff)

        texts = " ".join(message_texts(request))
        self.assertIn("still exist and still work", texts)
        # Named as it is actually stored, so the admin can find the account.
        self.assertIn(user.email, texts)
        self.assertTrue(User.objects.filter(pk=user.pk, is_active=True).exists())
        self.assertFalse(StaffMember.objects.filter(pk=staff.pk).exists())

    # Catches the warning firing for someone who never had a login, which would
    # train the administrator to ignore it.
    def test_deleting_unprovisioned_staff_emits_no_orphan_warning(self):
        staff = StaffMember.objects.create(
            email="never@oxlip.test", school=self.school
        )
        request = message_request(self.superuser)

        self.admin.delete_model(request, staff)

        self.assertNotIn("still exist and still work", " ".join(message_texts(request)))

    # The bulk path is the likelier one after a reorganisation, and it is a
    # separate ModelAdmin hook — so it can regress on its own.
    def test_bulk_delete_warns_for_the_whole_selection(self):
        self._provisioned("one@oxlip.test")
        self._provisioned("two@oxlip.test")
        request = message_request(self.superuser)

        self.admin.delete_queryset(request, StaffMember.objects.all())

        texts = " ".join(message_texts(request))
        self.assertIn("one@oxlip.test", texts)
        self.assertIn("two@oxlip.test", texts)
        self.assertIn("2 login account(s)", texts)
        self.assertEqual(User.objects.filter(is_active=True).count(), 3)  # + admin
        self.assertEqual(StaffMember.objects.count(), 0)


class BulkInlineSaveMessageTests(TestCase):
    """list_editable calls save_model once per changed row, so provisioning
    banners have to be grouped or the real summary is pushed off screen."""

    def setUp(self):
        self.school = School.objects.create(name="Copleston")
        self.superuser = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.superuser)
        self.staff = [
            StaffMember.objects.create(email=f"staff{i}@oxlip.test", school=self.school)
            for i in range(3)
        ]

    # Catches one banner per row returning: six rows previously produced six
    # near-identical messages.
    def test_editing_several_rows_inline_produces_one_grouped_banner(self):
        data = {
            "form-TOTAL_FORMS": str(len(self.staff)),
            "form-INITIAL_FORMS": str(len(self.staff)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        for index, staff in enumerate(self.staff):
            data[f"form-{index}-id"] = str(staff.pk)
            data[f"form-{index}-staff_type"] = StaffMember.StaffType.TEACHING

        response = self.client.post(
            reverse("admin:core_staffmember_changelist"), data, follow=True
        )
        self.assertEqual(response.status_code, 200)

        # All three really were provisioned — the grouping must not have come
        # from the work not happening.
        self.assertEqual(SchoolProfile.objects.count(), 3)
        for staff in self.staff:
            staff.refresh_from_db()
            self.assertEqual(staff.staff_type, StaffMember.StaffType.TEACHING)

        notes = [str(message) for message in response.context["messages"]]
        provisioning = [
            note
            for note in notes
            if OUTCOME_MESSAGES[ProvisionOutcome.CREATED_BOTH] in note
        ]
        self.assertEqual(len(provisioning), 1, provisioning)
        self.assertIn("3", provisioning[0])
        self.assertIn("staff0@oxlip.test", provisioning[0])


def post_data(pairs):
    """A QueryDict in the same shape (and order) a real form POST arrives in.

    ``submitted_text`` iterates the POST directly, so the ordering guarantee it
    documents ("reads down the page the way the user wrote it") only holds for
    a real QueryDict — a plain dict would not exercise it.
    """
    qd = QueryDict(mutable=True)
    for key, value in pairs:
        qd[key] = value
    return qd


class SubmittedTextTests(TestCase):
    """core.recovery.submitted_text: what gets handed back after a refused save.

    Two failure modes matter here and they pull in opposite directions. Echo
    too little and the user loses the paragraph the whole feature exists to
    save. Echo too much and the recovery page fills with CSRF tokens, hidden
    primary keys and one-character scores, burying the prose underneath — which
    is the same data loss with extra steps, because nobody scrolls.
    """

    # Catches the CSRF token being rendered back into the page as if it were
    # something the user typed.
    def test_csrf_token_is_never_echoed_back(self):
        result = submitted_text(
            post_data(
                [
                    ("csrfmiddlewaretoken", "a" * 64),
                    ("job_summary", "I taught Year 9 all year."),
                ]
            )
        )

        self.assertEqual(result, [("Job summary", "I taught Year 9 all year.")])

    # Catches formset bookkeeping ("30", "1000") being presented as prose.
    def test_formset_management_keys_are_skipped(self):
        result = submitted_text(
            post_data(
                [
                    ("items-TOTAL_FORMS", "30"),
                    ("items-INITIAL_FORMS", "30"),
                    ("items-MIN_NUM_FORMS", "0"),
                    ("items-MAX_NUM_FORMS", "1000"),
                    ("bullets-TOTAL_FORMS", "112"),
                    ("bullets-INITIAL_FORMS", "112"),
                    ("bullets-MIN_NUM_FORMS", "0"),
                    ("bullets-MAX_NUM_FORMS", "1000"),
                ]
            )
        )

        self.assertEqual(result, [])

    # Catches hidden formset plumbing — the round-tripped primary keys and the
    # delete flags — surfacing as recovered "text".
    def test_hidden_ids_and_delete_flags_are_skipped(self):
        result = submitted_text(
            post_data(
                [
                    ("items-0-id", "4471"),
                    ("items-0-DELETE", "on"),
                    ("items-0-evidence", "Book scrutiny in November."),
                ]
            )
        )

        self.assertEqual(result, [("Evidence (row 1)", "Book scrutiny in November.")])

    # Regression: single-character values must be KEPT.
    #
    # This page exists to be truthful about what a refused save did not store,
    # and it says "Nothing you typed has been thrown away." An earlier version
    # skipped values under two characters as presumed radio/date noise — but
    # every per-bullet self-review score posts as exactly one character, so a
    # teacher who had scored forty descriptors was shown their evidence, told
    # nothing was lost, and given none of their scores. Empty values are still
    # dropped, since there is nothing to hand back.
    def test_single_character_values_such_as_scores_are_kept(self):
        result = submitted_text(
            post_data(
                [
                    ("bullets-0-score", "3"),
                    ("upr_declaration_agreed", ""),
                    ("initial", " x "),
                    ("signed_name", "AB"),
                ]
            )
        )

        self.assertEqual(
            result,
            [
                ("Score (row 1)", "3"),
                ("Initial", "x"),
                ("Signed name", "AB"),
            ],
        )

    # Catches the row number being echoed 0-based, so the label points a user at
    # the wrong row of a 30-row self-review.
    def test_indexed_key_is_humanised_to_a_one_based_row_number(self):
        result = submitted_text(
            post_data([("items-3-evidence", "Evidence for the fourth group.")])
        )

        self.assertEqual(
            result, [("Evidence (row 4)", "Evidence for the fourth group.")]
        )

    # Catches an unindexed field losing its label entirely, or keeping its raw
    # underscored form name.
    def test_plain_key_is_humanised_without_a_row_number(self):
        result = submitted_text(
            post_data([("summary_teacher_comment", "A strong year overall.")])
        )

        self.assertEqual(
            result, [("Summary teacher comment", "A strong year overall.")]
        )

    # Catches the order being rearranged (e.g. by sorting the keys), so the
    # recovered text no longer reads down the page as it was written.
    def test_order_follows_the_submitted_form(self):
        result = submitted_text(
            post_data(
                [
                    ("items-1-evidence", "Second group evidence."),
                    ("items-0-evidence", "First group evidence."),
                ]
            )
        )

        self.assertEqual(
            [label for label, _ in result],
            ["Evidence (row 2)", "Evidence (row 1)"],
        )


class SuperuserOnlyDeleteTests(TestCase):
    """SuperuserOnlyDeleteMixin on every model that holds staff-written text.

    Non-superusers are deliberately given staff-admin rights over StaffMember,
    so the admin surface exists for them by design. Deleting one Appraisal
    cascades a whole year of someone's writing — goals, self-review, items,
    per-bullet scores — in a single confirm with no undo and no backup behind
    it, so the check is asserted against the *registered* admin instances
    rather than the mixin in isolation: a class that stops mixing it in is the
    regression this catches.
    """

    # Registered in appraisals/admin.py and line_management/admin.py.
    GATED_MODELS = (Appraisal, Goal, SelfReview, SelfReviewItem, LineMeeting)

    def setUp(self):
        from django.contrib import admin as django_admin

        self.registry = django_admin.site._registry
        self.superuser = make_user("admin@oxlip.test", is_superuser=True)
        self.office = User.objects.create_user(
            username="office@oxlip.test",
            email="office@oxlip.test",
            password="pw",
            is_staff=True,
        )
        # Grant the real Django delete permission on every gated model. Without
        # it the user would be refused anyway and this suite would still pass
        # with the mixin deleted — the point is that the model permission is
        # no longer sufficient on its own.
        for model in self.GATED_MODELS:
            self.office.user_permissions.add(
                Permission.objects.get(
                    content_type=ContentType.objects.get_for_model(model),
                    codename=f"delete_{model._meta.model_name}",
                )
            )
        self.office = User.objects.get(pk=self.office.pk)  # drop the perm cache

    def _request(self, user):
        request = RequestFactory().get("/admin/")
        request.user = user
        return request

    # Catches a staff user with the model delete permission still being able to
    # destroy a year of someone's PD record.
    def test_non_superuser_has_no_delete_permission_on_staff_text_models(self):
        request = self._request(self.office)
        for model in self.GATED_MODELS:
            with self.subTest(model=model.__name__):
                model_admin = self.registry[model]
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request, obj=None))

    # Catches the gate being tightened so far that nobody can ever delete,
    # which would push the work onto raw SQL instead.
    def test_superuser_keeps_delete_permission_on_staff_text_models(self):
        request = self._request(self.superuser)
        for model in self.GATED_MODELS:
            with self.subTest(model=model.__name__):
                self.assertTrue(self.registry[model].has_delete_permission(request))

    # Catches the changelist still offering "Delete selected", which Django
    # builds independently of has_delete_permission.
    def test_delete_selected_is_not_offered_to_a_non_superuser(self):
        request = self._request(self.office)
        for model in self.GATED_MODELS:
            with self.subTest(model=model.__name__):
                self.assertNotIn(
                    "delete_selected", self.registry[model].get_actions(request)
                )

    # ...and is still offered to a superuser, so the removal above is not just
    # "no actions for anyone".
    def test_delete_selected_is_still_offered_to_a_superuser(self):
        request = self._request(self.superuser)
        for model in self.GATED_MODELS:
            with self.subTest(model=model.__name__):
                self.assertIn(
                    "delete_selected", self.registry[model].get_actions(request)
                )


class FormsetErrorMarkupTests(TestCase):
    """core/_formset_errors.html must mark formset-level errors as .field-errors.

    unsaved_changes.js keys its "this page is holding unsaved work" detection on
    the ``.field-errors`` class, and seeds the form as dirty when it finds one.
    Django renders formset non-form errors as ``<ul class="errorlist nonform">``,
    which carries no such class — so before this wrapper, a formset-level
    validation failure re-rendered all of the user's typed text with the form
    marked clean. Navigating away from that page discarded it with no warning,
    which is the exact gap the dirty-seeding was added to close.
    """

    def _render(self, formset):
        return render_to_string("core/_formset_errors.html", {"formset": formset})

    def test_non_form_errors_are_wrapped_in_field_errors(self):
        class Stub:
            def non_form_errors(self):
                return ["ManagementForm data is missing."]

            def __iter__(self):
                return iter(())

        html = self._render(Stub())
        self.assertIn("field-errors", html)
        self.assertIn("ManagementForm data is missing.", html)

    def test_nothing_is_rendered_when_there_are_no_errors(self):
        class Stub:
            def non_form_errors(self):
                return []

            def __iter__(self):
                return iter(())

        html = self._render(Stub())
        # Critical: a clean page must NOT contain the marker, or every ordinary
        # page load would be treated as holding unsaved work and warn on exit.
        self.assertNotIn("field-errors", html)
