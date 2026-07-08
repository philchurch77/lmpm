"""Tests for the unified "My Team" page.

The team app owns no models: ``my_team`` composes ``coached_staff`` (performance
management) and ``line_managed_staff`` (line management) into one row-per-person
list. There are no field-level permissions to pin here — the security lives in
the drill-in views' own ``get_*_or_403`` chokepoints — so these tests cover the
*composition* logic that only lives here: the union/dedupe of the two
relationships, the per-row role flags, the annotations, and the empty state.

Identity is by email only (no FK from StaffMember to User), so each fixture
creates BOTH a Django ``User`` (to log in) and a ``StaffMember`` with the same
email.
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import StaffMember

from appraisals.models import AcademicYear, Appraisal
from line_management.models import LineMeeting


def make_user(email, *, is_superuser=False):
    return User.objects.create_user(
        username=email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
    )


def make_staff(email, *, line_manager_email="", performance_manager_email=""):
    return StaffMember.objects.create(
        email=email,
        line_manager_email=line_manager_email,
        performance_manager_email=performance_manager_email,
    )


class MyTeamCompositionTests(TestCase):
    """The union/dedupe of coached + line-managed people, and per-row flags."""

    def setUp(self):
        self.boss_email = "boss@oxlip.test"
        self.boss_user = make_user(self.boss_email)
        self.boss = make_staff(self.boss_email)
        self.url = reverse("team:my_team")

    def _rows(self, response):
        return response.context["rows"]

    def test_no_staff_record_shows_empty_state(self):
        # A login with no matching StaffMember hits the no_staff page, not a crash.
        user = make_user("ghost@oxlip.test")
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "team/no_staff.html")

    def test_manager_of_nobody_gets_empty_rows(self):
        self.client.force_login(self.boss_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._rows(response), [])

    def test_coached_person_appears_with_coach_flag_only(self):
        make_staff("coachee@oxlip.test", performance_manager_email=self.boss_email)
        self.client.force_login(self.boss_user)
        rows = self._rows(self.client.get(self.url))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member"].email, "coachee@oxlip.test")
        self.assertTrue(rows[0]["is_coach"])
        self.assertFalse(rows[0]["is_manager"])

    def test_line_managed_person_appears_with_manager_flag_only(self):
        make_staff("report@oxlip.test", line_manager_email=self.boss_email)
        self.client.force_login(self.boss_user)
        rows = self._rows(self.client.get(self.url))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_manager"])
        self.assertFalse(rows[0]["is_coach"])

    def test_person_both_coached_and_managed_appears_once_with_both_flags(self):
        # The headline composition rule: a person the viewer BOTH coaches and
        # line-manages must appear exactly once, flagged for both roles.
        make_staff(
            "both@oxlip.test",
            line_manager_email=self.boss_email,
            performance_manager_email=self.boss_email,
        )
        self.client.force_login(self.boss_user)
        rows = self._rows(self.client.get(self.url))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_coach"])
        self.assertTrue(rows[0]["is_manager"])

    def test_rows_do_not_include_other_managers_people(self):
        # Isolation: a different manager's reports/coachees never leak in.
        other = make_staff("other-boss@oxlip.test")
        make_staff("theirs@oxlip.test", line_manager_email=other.email)
        make_staff("mine@oxlip.test", line_manager_email=self.boss_email)
        self.client.force_login(self.boss_user)
        rows = self._rows(self.client.get(self.url))
        emails = {r["member"].email for r in rows}
        self.assertEqual(emails, {"mine@oxlip.test"})

    def test_self_is_never_listed(self):
        # A person who names themselves as their own manager (bad data) must not
        # appear in their own team list — coached_staff/line_managed_staff exclude
        # the viewer's own pk.
        self.boss.line_manager_email = self.boss_email
        self.boss.performance_manager_email = self.boss_email
        self.boss.save()
        self.client.force_login(self.boss_user)
        rows = self._rows(self.client.get(self.url))
        self.assertEqual(rows, [])


class MyTeamAnnotationTests(TestCase):
    """The appraisal/meeting summary data attached to each row."""

    def setUp(self):
        self.boss_email = "boss@oxlip.test"
        self.boss_user = make_user(self.boss_email)
        self.boss = make_staff(self.boss_email)
        self.year = AcademicYear.objects.create(start_year=2025, is_current=True)
        self.url = reverse("team:my_team")

    def test_coached_row_carries_current_year_appraisal(self):
        coachee = make_staff(
            "coachee@oxlip.test", performance_manager_email=self.boss_email
        )
        appraisal = Appraisal.objects.create(
            teacher=coachee, academic_year=self.year, coach_email=self.boss_email
        )
        self.client.force_login(self.boss_user)
        rows = self.client.get(self.url).context["rows"]
        self.assertEqual(rows[0]["appraisal"], appraisal)

    def test_managed_row_carries_meeting_count_and_last_date(self):
        report = make_staff("report@oxlip.test", line_manager_email=self.boss_email)
        LineMeeting.objects.create(staff=report, meeting_date=date(2026, 1, 10))
        LineMeeting.objects.create(staff=report, meeting_date=date(2026, 3, 2))
        self.client.force_login(self.boss_user)
        row = self.client.get(self.url).context["rows"][0]
        self.assertEqual(row["meeting_count"], 2)
        self.assertEqual(row["last_meeting"], date(2026, 3, 2))

    def test_coach_only_row_has_zero_meeting_count(self):
        # Someone coached but not line-managed has no meeting annotation; the view
        # must fall back to 0 rather than error.
        make_staff("coachee@oxlip.test", performance_manager_email=self.boss_email)
        self.client.force_login(self.boss_user)
        row = self.client.get(self.url).context["rows"][0]
        self.assertEqual(row["meeting_count"], 0)
        self.assertIsNone(row["last_meeting"])
