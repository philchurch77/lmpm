"""Access-control tests for the line_management app.

These focus on the security boundary, not cosmetics: ownership, the live
line-manager lookup, IDOR via guessed primary keys, and the manager-change
inheritance rule that is the headline governance decision for this app.

Identity is by email only (no FK from StaffMember to User), so every fixture
creates BOTH a Django ``User`` (to log in) and a ``StaffMember`` with the same
email. ``PermissionDenied`` surfaces as HTTP 403 through the test client.
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import StaffMember

from .models import LineMeeting


def make_user(email, *, is_superuser=False):
    """A Django User keyed by email (username mirrors it for uniqueness)."""
    return User.objects.create_user(
        username=email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
    )


def make_staff(email, *, line_manager_email=""):
    return StaffMember.objects.create(
        email=email,
        line_manager_email=line_manager_email,
    )


def make_meeting(staff, *, created_by_email="", meeting_date=None):
    return LineMeeting.objects.create(
        staff=staff,
        created_by_email=created_by_email,
        meeting_date=meeting_date or date(2026, 1, 15),
    )


class MeetingRoleMatrixTests(TestCase):
    """The view-level role matrix for meeting_detail / meeting_save."""

    def setUp(self):
        # A report, their current line manager, and an unrelated user.
        self.report_email = "report@oxlip.test"
        self.manager_email = "manager@oxlip.test"
        self.stranger_email = "stranger@oxlip.test"

        self.report_user = make_user(self.report_email)
        self.manager_user = make_user(self.manager_email)
        self.stranger_user = make_user(self.stranger_email)
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)

        self.report = make_staff(
            self.report_email, line_manager_email=self.manager_email
        )
        self.manager = make_staff(self.manager_email)
        self.stranger = make_staff(self.stranger_email)

        self.meeting = make_meeting(self.report, created_by_email=self.manager_email)
        self.detail_url = reverse(
            "line_management:meeting_detail", args=[self.meeting.pk]
        )
        self.save_url = reverse(
            "line_management:meeting_save", args=[self.meeting.pk]
        )

    def _save_payload(self, **overrides):
        payload = {
            "meeting_date": "2026-02-01",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "saved by test",
            "actions_from_meeting": "",
        }
        payload.update(overrides)
        return payload

    # Catches a report being able to open a meeting that is not theirs.
    def test_report_can_view_own_meeting(self):
        self.client.force_login(self.report_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches the read-only boundary failing: a report must never write.
    def test_report_cannot_save_own_meeting(self):
        self.client.force_login(self.report_user)
        response = self.client.post(self.save_url, self._save_payload())
        self.assertEqual(response.status_code, 403)
        self.meeting.refresh_from_db()
        self.assertNotEqual(self.meeting.main_matters, "saved by test")

    # Catches the current line manager being locked out of records they own.
    def test_current_manager_can_view_meeting(self):
        self.client.force_login(self.manager_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches the manager edit path silently dropping the write.
    def test_current_manager_can_save_meeting(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(
            self.save_url, self._save_payload(), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.main_matters, "saved by test")

    # Catches IDOR: an unrelated user reaching a meeting by guessing its PK.
    def test_unrelated_user_gets_403_on_view(self):
        self.client.force_login(self.stranger_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)

    # Catches IDOR on the write path independently of the read path.
    def test_unrelated_user_gets_403_on_save(self):
        self.client.force_login(self.stranger_user)
        response = self.client.post(self.save_url, self._save_payload())
        self.assertEqual(response.status_code, 403)
        self.meeting.refresh_from_db()
        self.assertNotEqual(self.meeting.main_matters, "saved by test")

    # Catches a logged-in user with no StaffMember row gaining access.
    def test_user_without_staff_member_gets_403_on_view(self):
        make_user("ghost@oxlip.test")  # User exists, but no StaffMember.
        self.client.force_login(User.objects.get(email="ghost@oxlip.test"))
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)

    # Catches superuser oversight access regressing.
    def test_superuser_can_view_and_save(self):
        self.client.force_login(self.super_user)
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)
        self.client.post(self.save_url, self._save_payload(), follow=True)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.main_matters, "saved by test")

    # Catches the login gate being removed from the detail view.
    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url.lower())


class ManagerChangeInheritanceTests(TestCase):
    """The headline governance rule: access follows the *current* line manager.

    When line_manager_email changes, the successor inherits the whole history
    (including meetings authored by the predecessor) and the predecessor loses
    access entirely. created_by_email is provenance only and grants nothing.
    """

    def setUp(self):
        self.old_email = "old.manager@oxlip.test"
        self.new_email = "new.manager@oxlip.test"
        self.report_email = "report@oxlip.test"

        self.old_user = make_user(self.old_email)
        self.new_user = make_user(self.new_email)
        make_user(self.report_email)

        self.old_manager = make_staff(self.old_email)
        self.new_manager = make_staff(self.new_email)
        self.report = make_staff(
            self.report_email, line_manager_email=self.old_email
        )

        # Meeting authored by the OLD manager — provenance points at them.
        self.meeting = make_meeting(self.report, created_by_email=self.old_email)
        self.detail_url = reverse(
            "line_management:meeting_detail", args=[self.meeting.pk]
        )
        self.save_url = reverse(
            "line_management:meeting_save", args=[self.meeting.pk]
        )

    def _save_payload(self):
        return {
            "meeting_date": "2026-02-01",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "edited after handover",
            "actions_from_meeting": "",
        }

    def _switch_manager_to_new(self):
        self.report.line_manager_email = self.new_email
        self.report.save()

    # Baseline: while they are the line manager, the old manager can edit.
    def test_old_manager_has_access_before_handover(self):
        self.client.force_login(self.old_user)
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)

    # Catches a successor NOT inheriting the existing history after handover.
    def test_successor_inherits_view_of_existing_history(self):
        self._switch_manager_to_new()
        self.client.force_login(self.new_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches a successor inheriting read but not write of inherited records.
    def test_successor_inherits_edit_of_existing_history(self):
        self._switch_manager_to_new()
        self.client.force_login(self.new_user)
        self.client.post(self.save_url, self._save_payload(), follow=True)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.main_matters, "edited after handover")

    # Catches a former manager retaining access after losing the relationship.
    def test_previous_manager_loses_view_after_handover(self):
        self._switch_manager_to_new()
        self.client.force_login(self.old_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)

    # Catches provenance (created_by_email) being mistaken for an access grant.
    def test_previous_manager_loses_edit_despite_being_author(self):
        self._switch_manager_to_new()
        self.client.force_login(self.old_user)
        response = self.client.post(self.save_url, self._save_payload())
        self.assertEqual(response.status_code, 403)
        self.meeting.refresh_from_db()
        self.assertNotEqual(self.meeting.main_matters, "edited after handover")


class CaseInsensitiveManagerMatchTests(TestCase):
    """The live manager compare must be case-insensitive end to end."""

    # Catches a case-sensitivity regression locking a legitimate manager out.
    def test_mixed_case_manager_email_still_grants_manager_role(self):
        # StaffMember.save() lowercases stored emails, so to exercise a genuine
        # case mismatch the manager's *login* email differs in case from the
        # stored value while resolving to the same StaffMember (email__iexact).
        manager_login = make_user("Manager.Mixed@OxLip.Test")
        StaffMember.objects.create(email="manager.mixed@oxlip.test")

        report = make_staff(
            "report.case@oxlip.test", line_manager_email="MANAGER.MIXED@oxlip.test"
        )
        make_user("report.case@oxlip.test")
        meeting = make_meeting(report)

        self.client.force_login(manager_login)
        detail_url = reverse(
            "line_management:meeting_detail", args=[meeting.pk]
        )
        save_url = reverse("line_management:meeting_save", args=[meeting.pk])

        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.client.post(
            save_url,
            {
                "meeting_date": "2026-03-01",
                "actions_from_last_meeting": "",
                "upcoming": "",
                "rotation_update": "",
                "main_matters": "case-insensitive edit",
                "actions_from_meeting": "",
            },
            follow=True,
        )
        meeting.refresh_from_db()
        self.assertEqual(meeting.main_matters, "case-insensitive edit")


class MyMeetingsSectioningTests(TestCase):
    """The widened 'My Line Meetings' page must not leak across users.

    'meetings' = records about the viewer themselves; 'hosted_meetings' =
    records of people the viewer CURRENTLY line-manages. Neither may include
    other people's data, nor people the viewer used to manage but no longer does.
    """

    def setUp(self):
        self.viewer_email = "viewer@oxlip.test"
        self.viewer_user = make_user(self.viewer_email)

        # The viewer is themselves line-managed by someone else.
        self.viewer = make_staff(
            self.viewer_email, line_manager_email="boss@oxlip.test"
        )
        self.viewer_own_meeting = make_meeting(self.viewer)

        # Someone the viewer currently line-manages.
        self.current_report = make_staff(
            "current@oxlip.test", line_manager_email=self.viewer_email
        )
        self.current_report_meeting = make_meeting(self.current_report)

        # Someone the viewer USED to line-manage (now reassigned away).
        self.former_report = make_staff(
            "former@oxlip.test", line_manager_email="someone.else@oxlip.test"
        )
        self.former_report_meeting = make_meeting(self.former_report)

        # An entirely unrelated person.
        self.outsider = make_staff("outsider@oxlip.test")
        self.outsider_meeting = make_meeting(self.outsider)

        self.url = reverse("line_management:my_meetings")

    # Catches another user's meeting being shown as one of the viewer's own.
    def test_meetings_section_contains_only_viewers_own(self):
        self.client.force_login(self.viewer_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        own = list(response.context["meetings"])
        self.assertEqual(own, [self.viewer_own_meeting])

    # Catches the hosted section showing too much or too little.
    def test_hosted_section_contains_only_current_reports(self):
        self.client.force_login(self.viewer_user)
        response = self.client.get(self.url)
        hosted = set(response.context["hosted_meetings"])
        self.assertEqual(hosted, {self.current_report_meeting})

    # Catches a former report's history leaking after reassignment.
    def test_hosted_section_excludes_former_reports(self):
        self.client.force_login(self.viewer_user)
        response = self.client.get(self.url)
        hosted = set(response.context["hosted_meetings"])
        self.assertNotIn(self.former_report_meeting, hosted)

    # Catches unrelated people's meetings leaking into either section.
    def test_no_section_includes_unrelated_meetings(self):
        self.client.force_login(self.viewer_user)
        response = self.client.get(self.url)
        own = set(response.context["meetings"])
        hosted = set(response.context["hosted_meetings"])
        self.assertNotIn(self.outsider_meeting, own | hosted)

    # Catches a user with no StaffMember crashing the page rather than degrading.
    def test_user_without_staff_member_sees_no_staff_page(self):
        make_user("nobody@oxlip.test")
        self.client.force_login(User.objects.get(email="nobody@oxlip.test"))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "line_management/no_staff.html")


class ManagedStaffChokepointTests(TestCase):
    """get_managed_staff_or_403 guards staff_meetings and meeting_create."""

    def setUp(self):
        self.manager_email = "lead@oxlip.test"
        self.manager_user = make_user(self.manager_email)
        self.manager = make_staff(self.manager_email)

        self.report = make_staff(
            "managed@oxlip.test", line_manager_email=self.manager_email
        )
        make_user("managed@oxlip.test")

        self.stranger_user = make_user("nosy@oxlip.test")
        make_staff("nosy@oxlip.test")

        self.super_user = make_user("root@oxlip.test", is_superuser=True)

        self.list_url = reverse(
            "line_management:staff_meetings", args=[self.report.pk]
        )
        self.new_url = reverse(
            "line_management:meeting_new", args=[self.report.pk]
        )
        self.create_url = reverse(
            "line_management:meeting_create", args=[self.report.pk]
        )
        # A valid create POST: a date plus at least one note section.
        self.valid_post = {
            "meeting_date": "2026-02-01",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "Discussed timetable.",
            "actions_from_meeting": "",
        }

    # Catches the current manager being denied their own team list.
    def test_current_manager_can_list_reports_meetings(self):
        self.client.force_login(self.manager_user)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)

    # Catches a non-manager reading another person's meeting list (IDOR).
    def test_non_manager_gets_403_on_staff_meetings(self):
        self.client.force_login(self.stranger_user)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    # Catches a non-manager opening the blank create form for someone else.
    def test_non_manager_gets_403_on_meeting_new(self):
        self.client.force_login(self.stranger_user)
        self.assertEqual(self.client.get(self.new_url).status_code, 403)

    # Catches a non-manager creating meetings against someone else's record.
    def test_non_manager_cannot_create_meeting(self):
        self.client.force_login(self.stranger_user)
        response = self.client.post(self.create_url, self.valid_post)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LineMeeting.objects.filter(staff=self.report).exists())

    # Catches the create form being rendered, or a record being written, on GET.
    def test_meeting_new_renders_form_without_creating(self):
        self.client.force_login(self.manager_user)
        response = self.client.get(self.new_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LineMeeting.objects.filter(staff=self.report).exists())

    # Catches the manager create flow failing to produce a record.
    def test_current_manager_can_create_meeting(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(self.create_url, self.valid_post)
        self.assertEqual(response.status_code, 302)
        meeting = LineMeeting.objects.get(staff=self.report)
        self.assertEqual(meeting.main_matters, "Discussed timetable.")
        # Provenance is stamped from the acting user, not the form.
        self.assertEqual(meeting.created_by_email, self.manager_email)

    # Catches the root cause of blank records: a notes-free save must not persist.
    def test_create_with_only_a_date_is_rejected(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(self.create_url, {"meeting_date": "2026-02-01"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LineMeeting.objects.filter(staff=self.report).exists())

    # Catches a former manager retaining create rights after reassignment.
    def test_former_manager_cannot_create_after_reassignment(self):
        self.report.line_manager_email = "someone.new@oxlip.test"
        self.report.save()
        self.client.force_login(self.manager_user)
        response = self.client.post(self.create_url, self.valid_post)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LineMeeting.objects.filter(staff=self.report).exists())

    # Catches superuser oversight on the manager-only views regressing.
    def test_superuser_can_reach_manager_views(self):
        self.client.force_login(self.super_user)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        self.assertEqual(self.client.get(self.new_url).status_code, 200)


class EmptyRecordTests(TestCase):
    """The is_empty property and the purge_empty_line_meetings command."""

    def setUp(self):
        self.staff = make_staff("person@oxlip.test")

    def test_is_empty_true_when_all_notes_blank(self):
        meeting = make_meeting(self.staff)  # no notes supplied
        self.assertTrue(meeting.is_empty)

    def test_is_empty_false_when_any_note_has_content(self):
        meeting = make_meeting(self.staff)
        meeting.main_matters = "Something"
        meeting.save()
        self.assertFalse(meeting.is_empty)

    def test_is_empty_treats_whitespace_only_as_empty(self):
        meeting = make_meeting(self.staff)
        meeting.upcoming = "   \n\t "
        meeting.save()
        self.assertTrue(meeting.is_empty)

    def test_purge_deletes_empties_and_keeps_content(self):
        empty = make_meeting(self.staff)
        kept = make_meeting(self.staff)
        kept.actions_from_meeting = "Follow up on cover."
        kept.save()

        call_command("purge_empty_line_meetings")

        self.assertFalse(LineMeeting.objects.filter(pk=empty.pk).exists())
        self.assertTrue(LineMeeting.objects.filter(pk=kept.pk).exists())

    def test_purge_dry_run_deletes_nothing(self):
        empty = make_meeting(self.staff)
        call_command("purge_empty_line_meetings", "--dry-run")
        self.assertTrue(LineMeeting.objects.filter(pk=empty.pk).exists())
