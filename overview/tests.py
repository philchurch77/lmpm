"""Tests for the superuser-only overview dashboards.

The overview app owns no models. Its two risk surfaces are (1) the single
``_require_superuser`` gate that is the *only* access check these trust-wide
pages have, and (2) the pure ``classify`` / ``classify_line`` status functions
that were deliberately split out to be unit-testable. Both are pinned here,
plus the row/count/filter composition in the views.

Identity is by email only (no FK from StaffMember to User).
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import School, StaffMember

from appraisals.models import AcademicYear, Appraisal
from line_management.models import LineMeeting

from .views import classify, classify_line


def make_user(email, *, is_superuser=False):
    return User.objects.create_user(
        username=email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
    )


class ClassifyTests(TestCase):
    """The appraisal-status bucketing rule (pure function, no DB needed)."""

    def test_signed_off_appraisal_is_complete(self):
        member = StaffMember(staff_type=StaffMember.StaffType.TEACHING)
        appraisal = Appraisal(status=Appraisal.Status.SIGNED_OFF)
        self.assertEqual(classify(member, appraisal), ("signed_off", "Signed off"))

    def test_draft_appraisal_is_in_progress(self):
        member = StaffMember(staff_type=StaffMember.StaffType.TEACHING)
        appraisal = Appraisal(status=Appraisal.Status.DRAFT)
        self.assertEqual(classify(member, appraisal), ("in_progress", "In progress"))

    def test_shared_appraisal_is_in_progress(self):
        member = StaffMember(staff_type=StaffMember.StaffType.TEACHING)
        appraisal = Appraisal(status=Appraisal.Status.SHARED)
        self.assertEqual(classify(member, appraisal), ("in_progress", "In progress"))

    # No appraisal + blank staff_type is a data-prep gap that must take priority
    # over "not started" so it reads as setup, not a person to chase.
    def test_no_appraisal_blank_type_is_not_classified(self):
        member = StaffMember(staff_type="")
        self.assertEqual(classify(member, None), ("not_classified", "Not classified"))

    def test_no_appraisal_with_type_is_not_started(self):
        member = StaffMember(staff_type=StaffMember.StaffType.SUPPORT)
        self.assertEqual(classify(member, None), ("not_started", "Not started"))


class ClassifyLineTests(TestCase):
    """The line-management engagement bucketing rule (pure function)."""

    def test_no_manager_takes_priority(self):
        member = StaffMember(line_manager_email="")
        self.assertEqual(classify_line(member, 0), ("no_manager", "No manager"))

    def test_manager_but_no_meetings(self):
        member = StaffMember(line_manager_email="boss@oxlip.test")
        self.assertEqual(classify_line(member, 0), ("no_meetings", "No meetings"))

    def test_manager_with_meetings(self):
        member = StaffMember(line_manager_email="boss@oxlip.test")
        self.assertEqual(classify_line(member, 3), ("has_meetings", "Has meetings"))


class OverviewAccessGateTests(TestCase):
    """The superuser-only gate — the single access check these pages have."""

    def setUp(self):
        self.urls = [
            reverse("overview:appraisals"),
            reverse("overview:line_management"),
        ]

    def test_anonymous_is_redirected_to_login(self):
        for url in self.urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)

    def test_ordinary_user_is_denied(self):
        self.client.force_login(make_user("staff@oxlip.test"))
        for url in self.urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_superuser_is_allowed(self):
        self.client.force_login(make_user("admin@oxlip.test", is_superuser=True))
        for url in self.urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class AppraisalsOverviewViewTests(TestCase):
    """Row/count composition and the no-active-year branch."""

    def setUp(self):
        self.admin = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.admin)
        self.url = reverse("overview:appraisals")

    def test_no_current_year_shows_notice(self):
        response = self.client.get(self.url)
        self.assertTrue(response.context["no_year"])

    def test_counts_are_derived_from_rows(self):
        year = AcademicYear.objects.create(start_year=2025, is_current=True)
        classified = StaffMember.objects.create(
            email="t@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        StaffMember.objects.create(email="blank@oxlip.test")  # not classified
        Appraisal.objects.create(
            teacher=classified, academic_year=year, status=Appraisal.Status.DRAFT
        )
        response = self.client.get(self.url)
        counts = response.context["counts"]
        self.assertEqual(response.context["total"], 2)
        self.assertEqual(counts["in_progress"], 1)
        self.assertEqual(counts["not_classified"], 1)

    def test_school_filter_narrows_rows(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        school_a = School.objects.create(name="Alpha")
        school_b = School.objects.create(name="Beta")
        StaffMember.objects.create(email="a@oxlip.test", school=school_a)
        StaffMember.objects.create(email="b@oxlip.test", school=school_b)
        response = self.client.get(self.url, {"school": school_a.pk})
        emails = {r["member"].email for r in response.context["rows"]}
        self.assertEqual(emails, {"a@oxlip.test"})

    def test_email_query_filter_narrows_rows(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)
        StaffMember.objects.create(email="alice@oxlip.test")
        StaffMember.objects.create(email="bob@oxlip.test")
        response = self.client.get(self.url, {"q": "alice"})
        emails = {r["member"].email for r in response.context["rows"]}
        self.assertEqual(emails, {"alice@oxlip.test"})


class LineManagementOverviewViewTests(TestCase):
    """Engagement counts across every staff member."""

    def setUp(self):
        self.admin = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.admin)
        self.url = reverse("overview:line_management")

    def test_counts_reflect_engagement_states(self):
        no_manager = StaffMember.objects.create(email="lonely@oxlip.test")
        has_mgr_no_meet = StaffMember.objects.create(
            email="new@oxlip.test", line_manager_email="boss@oxlip.test"
        )
        engaged = StaffMember.objects.create(
            email="active@oxlip.test", line_manager_email="boss@oxlip.test"
        )
        LineMeeting.objects.create(staff=engaged, meeting_date=date(2026, 2, 1))
        response = self.client.get(self.url)
        counts = response.context["counts"]
        self.assertEqual(response.context["total"], 3)
        self.assertEqual(counts["no_manager"], 1)
        self.assertEqual(counts["no_meetings"], 1)
        self.assertEqual(counts["has_meetings"], 1)
        # Sanity-check the annotation the count relies on.
        by_email = {r["member"].email: r for r in response.context["rows"]}
        self.assertEqual(by_email["active@oxlip.test"]["meeting_count"], 1)
        self.assertEqual(by_email["lonely@oxlip.test"]["meeting_count"], 0)
