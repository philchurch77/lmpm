"""Access-control tests for the line_management app.

These focus on the security boundary, not cosmetics: ownership, the live
line-manager lookup, IDOR via guessed primary keys, and the manager-change
inheritance rule that is the headline governance decision for this app.

Identity is by email only (no FK from StaffMember to User), so every fixture
creates BOTH a Django ``User`` (to log in) and a ``StaffMember`` with the same
email. ``PermissionDenied`` surfaces as HTTP 403 through the test client.
"""
from __future__ import annotations

import re
from datetime import date

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils.html import escape

from core.models import StaffMember

from .admin import LineMeetingAdmin, MeetingActionAdmin, MeetingActionInline
from .models import LineMeeting, MeetingAction
from .services import CarryForwardChanged, meeting_version, start_meeting


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


def make_meeting(staff, *, created_by_email="", meeting_date=None, state=LineMeeting.State.HELD):
    # Held by default: most tests are about meetings that have taken place, and
    # a staff member may have only one meeting being prepared.
    return LineMeeting.objects.create(
        staff=staff,
        created_by_email=created_by_email,
        meeting_date=meeting_date or date(2026, 1, 15),
        state=state,
    )


def management_form(prefix, total=0, initial=0):
    """The hidden management-form fields a formset needs to bind."""
    return {
        f"{prefix}-TOTAL_FORMS": str(total),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }


def meeting_payload(**fields):
    """A meeting-page POST with no action rows; override any field by keyword."""
    payload = {
        "meeting_date": "2026-02-01",
        "upcoming": "",
        "rotation_update": "",
        "main_matters": "",
        **management_form("agreed"),
        **management_form("carried"),
    }
    payload.update(fields)
    return payload


def versioned(payload, meeting):
    """``payload`` plus the meeting's *current* version stamp, as a fresh page carries.

    Save tests that are about something other than staleness must send this, or
    they get the 409 hand-back and stop exercising what they were written for.
    """
    current = LineMeeting.objects.get(pk=getattr(meeting, "pk", meeting))
    return {**payload, "meeting_version": meeting_version(current)}


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
        return versioned(
            meeting_payload(**{"main_matters": "saved by test", **overrides}), self.meeting
        )

    # Catches a report being able to open a meeting that is not theirs.
    def test_report_can_view_own_meeting(self):
        self.client.force_login(self.report_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches the Held boundary failing: a report must never write to a held meeting.
    # (Leg 4: they keep their role, so it is a 409 hand-back of their own text, not a 403.)
    def test_report_cannot_save_own_held_meeting(self):
        self.client.force_login(self.report_user)
        response = self.client.post(self.save_url, self._save_payload())
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "saved by test", status_code=409)
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
        return versioned(meeting_payload(main_matters="edited after handover"), self.meeting)

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
            versioned(
                meeting_payload(meeting_date="2026-03-01", main_matters="case-insensitive edit"),
                meeting,
            ),
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
        self.valid_post = meeting_payload(main_matters="Discussed timetable.")

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
        response = self.client.post(self.create_url, meeting_payload())
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
        kept.main_matters = "Follow up on cover."
        kept.save()

        call_command("purge_empty_line_meetings")

        self.assertFalse(LineMeeting.objects.filter(pk=empty.pk).exists())
        self.assertTrue(LineMeeting.objects.filter(pk=kept.pk).exists())

    def test_purge_dry_run_deletes_nothing(self):
        empty = make_meeting(self.staff)
        call_command("purge_empty_line_meetings", "--dry-run")
        self.assertTrue(LineMeeting.objects.filter(pk=empty.pk).exists())


class DoubleSubmitGuardTests(TestCase):
    """meeting_create's repeat-submission guard: one meeting per submission.

    LineMeeting deliberately carries no uniqueness constraint, because two
    genuine meetings can share a staff member and a date. That left a
    double-clicked "Save meeting" (or a browser retry on a slow POST) creating
    two identical records: the engagement counts on the overview page were
    inflated, and the manager went on editing one copy while the other sat
    frozen with the original text — a silent divergence nobody sees until
    somebody reads the wrong one.

    The guard therefore has to be exact in both directions, so both are pinned:
    a byte-for-byte repeat is folded into the record already saved, and a
    genuinely different second meeting on the same day is still created.
    """

    def setUp(self):
        self.manager_email = "lead@oxlip.test"
        self.manager_user = make_user(self.manager_email)
        self.manager = make_staff(self.manager_email)

        self.report = make_staff(
            "managed@oxlip.test", line_manager_email=self.manager_email
        )
        make_user("managed@oxlip.test")

        self.create_url = reverse(
            "line_management:meeting_create", args=[self.report.pk]
        )
        self.client.force_login(self.manager_user)

    def _post(self, **overrides):
        payload = meeting_payload(
            **{"main_matters": "Discussed timetable and cover.", **overrides}
        )
        return self.client.post(self.create_url, payload)

    # Catches the double-click duplicate: two identical POSTs, one record.
    def test_identical_create_post_twice_creates_only_one_meeting(self):
        first = self._post()
        second = self._post()

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)

    # Catches the second submit landing somewhere else — on a fresh blank form,
    # or on a second record — instead of on the meeting that was actually saved.
    def test_second_identical_post_redirects_to_the_same_meeting(self):
        first = self._post()
        second = self._post()

        meeting = LineMeeting.objects.get(staff=self.report)
        expected = reverse("line_management:meeting_detail", args=[meeting.pk])
        self.assertEqual(first["Location"], expected)
        self.assertEqual(second["Location"], expected)

    # Catches the guard being over-eager: two real meetings with the same person
    # on the same day are legitimate, and the second must not be swallowed just
    # because the date matches.
    def test_same_staff_and_date_with_different_notes_creates_a_second_meeting(self):
        # The first is held, so the second may be started (one being prepared at a time).
        self._post(main_matters="Morning catch-up about cover.", hold="1")
        self._post(main_matters="Afternoon follow-up about the trip.")

        meetings = LineMeeting.objects.filter(staff=self.report)
        self.assertEqual(meetings.count(), 2)
        self.assertEqual(
            sorted(m.main_matters for m in meetings),
            [
                "Afternoon follow-up about the trip.",
                "Morning catch-up about cover.",
            ],
        )

    # Catches the guard matching on only some of the note fields: a repeat that
    # differs in ONE section is a different meeting and must still be created.
    def test_difference_in_any_single_note_field_creates_a_second_meeting(self):
        self._post(upcoming="", hold="1")
        self._post(upcoming="Book the room for next time.")

        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 2)

    # Catches the guard reaching across staff members — identical notes written
    # for two different reports are two records, not one.
    def test_identical_notes_for_a_different_report_are_not_folded_together(self):
        other = make_staff("second@oxlip.test", line_manager_email=self.manager_email)
        make_user("second@oxlip.test")
        payload = meeting_payload(main_matters="Standing agenda item.")

        self.client.post(self.create_url, payload)
        self.client.post(
            reverse("line_management:meeting_create", args=[other.pk]), payload
        )

        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        self.assertEqual(LineMeeting.objects.filter(staff=other).count(), 1)


# ---------------------------------------------------------------------------
# Leg 1 of docs/chart/line-meeting-preparation.md: MeetingAction rows, carry-
# forward pinning, RAG ratings, legacy prose, admin gates.
# ---------------------------------------------------------------------------


def make_action(agreed_at, description="An action", *, reviewed_in=None, rag="", comment=""):
    return MeetingAction.objects.create(
        agreed_at=agreed_at,
        description=description,
        reviewed_in=reviewed_in,
        rag=rag,
        review_comment=comment,
    )


def agreed_rows(existing=(), new=()):
    """POST rows for the ``agreed`` formset.

    ``existing`` is a sequence of (action, description, delete) tuples, posted as
    the initial forms; ``new`` is a sequence of texts for the blank rows.
    """
    data = management_form("agreed", len(existing) + len(new), len(existing))
    for i, (action, text, delete) in enumerate(existing):
        data[f"agreed-{i}-id"] = str(action.pk)
        data[f"agreed-{i}-description"] = text
        if delete:
            data[f"agreed-{i}-DELETE"] = "on"
    for j, text in enumerate(new, start=len(existing)):
        data[f"agreed-{j}-description"] = text
    return data


def carried_rows(rows=()):
    """POST rows for the ``carried`` formset: a sequence of (action, rag, comment)."""
    data = management_form("carried", len(rows), len(rows))
    for i, (action, rag, comment) in enumerate(rows):
        data[f"carried-{i}-id"] = str(action.pk)
        data[f"carried-{i}-rag"] = rag
        data[f"carried-{i}-review_comment"] = comment
    return data


def snapshot(action):
    """Everything about an action row that a crafted POST must not change."""
    return (
        MeetingAction.objects.filter(pk=action.pk)
        .values_list("agreed_at_id", "reviewed_in_id", "description", "rag", "review_comment", "updated_at")
        .first()
    )


class MeetingActionIsolationTests(TestCase):
    """Gauntlet stage 3: a manager of two reports can only touch the actions of
    the meeting they posted to; reports and strangers can touch none."""

    def setUp(self):
        self.m_email = "m@oxlip.test"
        self.m_user = make_user(self.m_email)
        make_staff(self.m_email)
        self.r1_user = make_user("r1@oxlip.test")
        self.r1 = make_staff("r1@oxlip.test", line_manager_email=self.m_email)
        make_user("r2@oxlip.test")
        self.r2 = make_staff("r2@oxlip.test", line_manager_email=self.m_email)
        self.stranger_user = make_user("stranger@oxlip.test")
        make_staff("stranger@oxlip.test")

        self.m1 = make_meeting(self.r1, meeting_date=date(2026, 1, 10))
        self.m1.main_matters = "R1 notes"
        self.m1.save()

        # R2: an older meeting whose action is pinned (and rated) at a later one,
        # plus an unpinned action on R2's latest meeting.
        self.r2_old = make_meeting(self.r2, meeting_date=date(2026, 1, 3))
        self.r2_latest = make_meeting(self.r2, meeting_date=date(2026, 1, 10))
        self.r2_pinned = make_action(
            self.r2_old, "R2 pinned", reviewed_in=self.r2_latest, rag="AMBER", comment="R2 comment"
        )
        self.r2_open = make_action(self.r2_latest, "R2 open")
        self.r2_before = [snapshot(self.r2_pinned), snapshot(self.r2_open)]

        self.save_url = reverse("line_management:meeting_save", args=[self.m1.pk])
        self.create_url = reverse("line_management:meeting_create", args=[self.r1.pk])

    def assertR2Untouched(self):
        self.assertEqual([snapshot(self.r2_pinned), snapshot(self.r2_open)], self.r2_before)
        self.assertEqual(
            list(self.r2_latest.agreed_actions.values_list("description", flat=True)), ["R2 open"]
        )

    # Catches a crafted agreed-row id rewording another report's action.
    def test_crafted_agreed_id_cannot_reword_another_reports_action(self):
        self.client.force_login(self.m_user)
        payload = meeting_payload(main_matters="R1 notes", **agreed_rows([(self.r2_open, "HACKED", False)]))
        self.client.post(self.save_url, versioned(payload, self.m1))
        self.assertR2Untouched()

    # Catches a crafted agreed-row id + DELETE destroying another report's action.
    def test_crafted_agreed_id_with_delete_cannot_remove_another_reports_action(self):
        self.client.force_login(self.m_user)
        payload = meeting_payload(main_matters="R1 notes", **agreed_rows([(self.r2_open, "HACKED", True)]))
        payload["agreed-0-agreed_at"] = str(self.m1.pk)
        self.client.post(self.save_url, versioned(payload, self.m1))
        self.assertR2Untouched()

    # Catches a crafted carried-row id rating another report's reviewed action on save.
    def test_crafted_carried_id_on_save_cannot_rate_another_reports_action(self):
        self.client.force_login(self.m_user)
        payload = meeting_payload(
            main_matters="R1 notes", **carried_rows([(self.r2_pinned, "RED", "HACKED")])
        )
        self.client.post(self.save_url, versioned(payload, self.m1))
        self.assertR2Untouched()

    # Catches create for one report pinning, rating or displaying another report's action.
    def test_crafted_carried_id_on_create_cannot_pin_or_rate_another_reports_action(self):
        self.client.force_login(self.m_user)
        payload = meeting_payload(
            meeting_date="2026-02-01",
            main_matters="New R1 meeting",
            **carried_rows([(self.r2_open, "GREEN", "HACKED")]),
        )
        response = self.client.post(self.create_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertR2Untouched()
        self.assertNotContains(response, "R2 open")
        self.assertEqual(LineMeeting.objects.filter(staff=self.r1).count(), 1)

    # Catches the inline fk field re-parenting a new action onto another report's meeting.
    def test_agreed_at_field_cannot_reparent_action_to_another_reports_meeting(self):
        self.client.force_login(self.m_user)
        for url in (self.save_url, self.create_url):
            payload = meeting_payload(main_matters="R1 notes", **agreed_rows(new=["REPARENTED"]))
            payload["agreed-0-agreed_at"] = str(self.r2_latest.pk)
            self.client.post(url, versioned(payload, self.m1))
        self.assertR2Untouched()
        self.assertFalse(
            MeetingAction.objects.filter(description="REPARENTED")
            .exclude(agreed_at__staff=self.r1)
            .exists()
        )

    # Catches an inflated carried TOTAL_FORMS minting new action rows.
    def test_inflated_carried_total_forms_creates_no_action_rows(self):
        self.client.force_login(self.m_user)
        before = MeetingAction.objects.count()
        payload = meeting_payload(main_matters="R1 notes", **management_form("carried", 3, 0))
        for i in range(3):
            payload[f"carried-{i}-rag"] = "RED"
            payload[f"carried-{i}-review_comment"] = "ghost row"
        self.client.post(self.save_url, versioned(payload, self.m1))
        self.assertEqual(MeetingAction.objects.count(), before)
        self.assertFalse(MeetingAction.objects.filter(review_comment="ghost row").exists())

    # Catches the report writing action rows on their own held (read-only) record.
    def test_report_posting_action_rows_to_own_held_meeting_gets_409_and_rows_unchanged(self):
        own = make_action(self.m1, "Agreed by manager")
        later = make_meeting(self.r1, meeting_date=date(2026, 1, 20))
        carried = make_action(self.m1, "Carried one", reviewed_in=later)
        before = [snapshot(own), snapshot(carried)]
        count = MeetingAction.objects.count()

        self.client.force_login(self.r1_user)
        payload = meeting_payload(
            main_matters="R1 notes", **agreed_rows([(own, "REPORT EDIT", True)], new=["REPORT NEW"])
        )
        self.assertEqual(self.client.post(self.save_url, versioned(payload, self.m1)).status_code, 409)

        payload = meeting_payload(**carried_rows([(carried, "GREEN", "report rating")]))
        later_save = reverse("line_management:meeting_save", args=[later.pk])
        self.assertEqual(self.client.post(later_save, versioned(payload, later)).status_code, 409)

        self.assertEqual([snapshot(own), snapshot(carried)], before)
        self.assertEqual(MeetingAction.objects.count(), count)

    # Catches the report being handed editable action fields on their own record.
    def test_report_sees_action_fields_disabled(self):
        later = make_meeting(self.r1, meeting_date=date(2026, 1, 20))
        make_action(self.m1, "Carried to later", reviewed_in=later)
        make_action(later, "Agreed at later")

        self.client.force_login(self.r1_user)
        page = self.client.get(
            reverse("line_management:meeting_detail", args=[later.pk])
        ).content.decode()

        tags = re.findall(
            r"<(?:textarea|input)[^>]*name=\"(?:agreed|carried)-\d+-(?:description|rag|review_comment)\"[^>]*>",
            page,
        )
        names = {re.search(r'name="([^"]+)"', t).group(1) for t in tags}
        self.assertEqual(
            names, {"agreed-0-description", "carried-0-rag", "carried-0-review_comment"}
        )
        for tag in tags:
            self.assertIn("disabled", tag, tag)
        self.assertNotIn("agreed-0-DELETE", page)

    # Catches a stranger reaching any action path by guessing a meeting or staff pk.
    def test_stranger_gets_403_on_every_action_path_and_nothing_changes(self):
        own = make_action(self.m1, "R1 action")
        before = snapshot(own)
        self.client.force_login(self.stranger_user)
        payload = meeting_payload(main_matters="x", **agreed_rows([(own, "STRANGER", True)]))
        detail = reverse("line_management:meeting_detail", args=[self.m1.pk])
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.post(self.save_url, versioned(payload, self.m1)).status_code, 403)
        self.assertEqual(self.client.post(self.create_url, payload).status_code, 403)
        self.assertEqual(snapshot(own), before)
        self.assertEqual(LineMeeting.objects.filter(staff=self.r1).count(), 1)

    # Catches a successor manager being unable to rate actions inherited from a predecessor.
    def test_successor_manager_can_rate_inherited_carried_actions(self):
        later = make_meeting(self.r1, meeting_date=date(2026, 1, 20))
        inherited = make_action(self.m1, "Inherited action", reviewed_in=later)
        successor_user = make_user("successor@oxlip.test")
        make_staff("successor@oxlip.test")
        self.r1.line_manager_email = "successor@oxlip.test"
        self.r1.save()

        self.client.force_login(successor_user)
        payload = meeting_payload(
            meeting_date="2026-01-20", **carried_rows([(inherited, "AMBER", "Halfway there")])
        )
        response = self.client.post(
            reverse("line_management:meeting_save", args=[later.pk]), versioned(payload, later)
        )
        self.assertEqual(response.status_code, 302)
        inherited.refresh_from_db()
        self.assertEqual((inherited.rag, inherited.review_comment), ("AMBER", "Halfway there"))
        self.assertEqual(inherited.reviewed_in_id, later.pk)


class MeetingActionAdminTests(TestCase):
    """The admin's delete and re-parenting gates for action rows."""

    def setUp(self):
        self.factory = RequestFactory()
        self.super_user = make_user("root@oxlip.test", is_superuser=True)
        self.staff_admin = User.objects.create_user(
            username="staffadmin@oxlip.test", email="staffadmin@oxlip.test", password="pw", is_staff=True
        )
        person = make_staff("person@oxlip.test")
        self.m1 = make_meeting(person, meeting_date=date(2026, 1, 10))
        self.m2 = make_meeting(person, meeting_date=date(2026, 1, 20))
        self.pinned = make_action(self.m1, "Pinned", reviewed_in=self.m2, rag="RED", comment="Blocked")
        self.open = make_action(self.m2, "Open")

    def _request(self, user):
        request = self.factory.get("/")
        request.user = user
        return request

    # Catches a non-superuser admin deleting action rows through the meeting inline.
    def test_inline_refuses_action_delete_to_non_superuser(self):
        inline = MeetingActionInline(LineMeeting, django_admin.site)
        self.assertFalse(inline.has_delete_permission(self._request(self.staff_admin), self.m1))

    # Catches a superuser deleting a reviewed action and with it the later meeting's rating.
    def test_action_admin_refuses_deleting_pinned_action_even_for_superuser(self):
        model_admin = MeetingActionAdmin(MeetingAction, django_admin.site)
        request = self._request(self.super_user)
        self.assertFalse(model_admin.has_delete_permission(request, self.pinned))
        self.assertTrue(model_admin.has_delete_permission(request, self.open))

        self.client.force_login(self.super_user)
        url = reverse("admin:line_management_meetingaction_delete", args=[self.pinned.pk])
        self.client.post(url, {"post": "yes"})
        self.assertTrue(MeetingAction.objects.filter(pk=self.pinned.pk).exists())

    # Catches the admin moving a saved meeting (and its actions) onto another person.
    def test_meeting_admin_makes_staff_read_only_on_existing_meeting(self):
        model_admin = LineMeetingAdmin(LineMeeting, django_admin.site)
        request = self._request(self.super_user)
        self.assertIn("staff", model_admin.get_readonly_fields(request, self.m1))
        self.assertNotIn("staff", model_admin.get_readonly_fields(request, None))

    # Catches rating an unpinned action in the admin ending in an IntegrityError 500.
    def test_admin_rating_unpinned_action_returns_form_error_not_500(self):
        before = snapshot(self.open)
        self.client.force_login(self.super_user)
        url = reverse("admin:line_management_meetingaction_change", args=[self.open.pk])
        response = self.client.post(
            url, {"description": "Open", "rag": "RED", "review_comment": "too early", "_save": "Save"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can only be rated once it has been carried")
        self.assertEqual(snapshot(self.open), before)


class MeetingActionModelTests(TestCase):
    """Database-level rules that keep a rating attached to the meeting that wrote it."""

    def setUp(self):
        self.person = make_staff("person@oxlip.test")
        self.m1 = make_meeting(self.person, meeting_date=date(2026, 1, 10))
        self.m2 = make_meeting(self.person, meeting_date=date(2026, 1, 20))

    # Catches NOTE_FIELDS drifting, which would silently change every import source_row_hash.
    def test_note_fields_are_pinned_to_the_five_import_hash_fields(self):
        self.assertEqual(
            LineMeeting.NOTE_FIELDS,
            (
                "actions_from_last_meeting",
                "upcoming",
                "rotation_update",
                "main_matters",
                "actions_from_meeting",
            ),
        )

    # Catches an action being reviewed at the same meeting it was agreed at.
    def test_action_cannot_be_reviewed_where_agreed(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_action(self.m1, reviewed_in=self.m1)

    # Catches a rating or comment stored with no meeting it was written at.
    def test_rating_or_comment_needs_reviewed_in(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_action(self.m1, rag="RED")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_action(self.m1, comment="orphan comment")

    # Catches deleting a meeting cascading away its actions or the ratings written in it.
    def test_deleting_meeting_holding_agreed_or_reviewed_actions_is_protected(self):
        action = make_action(self.m1, "Keep me", reviewed_in=self.m2, rag="GREEN", comment="Done")
        with self.assertRaises(ProtectedError):
            self.m1.delete()
        with self.assertRaises(ProtectedError):
            self.m2.delete()
        action.refresh_from_db()
        self.assertEqual((action.rag, action.review_comment), ("GREEN", "Done"))

    # Catches is_empty calling a meeting blank when it agreed actions.
    def test_is_empty_false_when_meeting_has_agreed_actions(self):
        make_action(self.m1)
        self.assertFalse(self.m1.is_empty)

    # Catches is_empty calling a meeting blank when it reviewed actions.
    def test_is_empty_false_when_meeting_has_reviewed_actions(self):
        make_action(self.m1, reviewed_in=self.m2)
        self.assertFalse(self.m2.is_empty)

    # Catches the purge deleting notes-free meetings that hold actions and ratings.
    def test_purge_keeps_notes_free_meetings_that_hold_actions(self):
        truly_empty = make_meeting(self.person, meeting_date=date(2026, 1, 25))
        make_action(self.m1, "Agreed", reviewed_in=self.m2, rag="AMBER")
        call_command("purge_empty_line_meetings")
        self.assertTrue(LineMeeting.objects.filter(pk=self.m1.pk).exists())
        self.assertTrue(LineMeeting.objects.filter(pk=self.m2.pk).exists())
        self.assertFalse(LineMeeting.objects.filter(pk=truly_empty.pk).exists())


class _ManagerCreateMixin:
    """A manager, one report, and a helper to POST the create form."""

    def setUp(self):
        self.manager_email = "boss@oxlip.test"
        self.manager_user = make_user(self.manager_email)
        make_staff(self.manager_email)
        make_user("report@oxlip.test")
        self.report = make_staff("report@oxlip.test", line_manager_email=self.manager_email)
        self.create_url = reverse("line_management:meeting_create", args=[self.report.pk])
        self.client.force_login(self.manager_user)

    def create(self, meeting_date="2026-02-01", carried=(), agreed=(), hold=False, **notes):
        """POST the create page. ``hold`` presses "Save and mark as held" instead of "Save"."""
        payload = meeting_payload(meeting_date=meeting_date, **notes)
        payload.update(carried_rows(carried))
        payload.update(agreed_rows(new=agreed))
        if hold:
            payload["hold"] = "1"
        return self.client.post(self.create_url, payload)

    def newest(self):
        return LineMeeting.objects.filter(staff=self.report).order_by("-pk").first()


class CarryForwardTests(_ManagerCreateMixin, TestCase):
    """start_meeting pins exactly the latest meeting's unreviewed actions, once."""

    # Catches a new meeting failing to pick up last meeting's open actions.
    def test_create_pins_unreviewed_actions_from_latest_meeting(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a, b = make_action(prev, "A"), make_action(prev, "B")
        response = self.create(carried=[(a, "", ""), (b, "", "")], main_matters="Notes")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(self.newest().reviewed_actions.values_list("pk", flat=True)), sorted([a.pk, b.pk])
        )

    # Catches an already-reviewed action being re-pinned (and its rating moved) by the next meeting.
    def test_already_reviewed_action_is_not_repinned(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        self.assertEqual(
            self.create("2026-02-01", carried=[(a, "GREEN", "Done")], agreed=["B"], hold=True).status_code, 302
        )
        second = self.newest()
        b = MeetingAction.objects.get(description="B")

        self.assertEqual(self.create("2026-03-01", carried=[(b, "", "")], main_matters="Third").status_code, 302)
        third = self.newest()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.reviewed_in_id, a.rag, a.review_comment), (second.pk, "GREEN", "Done"))
        self.assertEqual(b.reviewed_in_id, third.pk)

    # Catches actions from a meeting older than the latest being swept into the new one.
    def test_older_meetings_unreviewed_actions_are_not_carried(self):
        old = make_meeting(self.report, meeting_date=date(2025, 12, 1))
        stale = make_action(old, "Never carried")
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        current = make_action(prev, "Carry me")
        self.assertEqual(self.create(carried=[(current, "", "")], main_matters="x").status_code, 302)
        stale.refresh_from_db()
        self.assertIsNone(stale.reviewed_in_id)

    # Catches the source being chosen by creation order instead of meeting date.
    def test_source_is_latest_by_meeting_date_not_creation_order(self):
        late = make_meeting(self.report, meeting_date=date(2026, 1, 20))
        late_action = make_action(late, "From the later-dated meeting")
        early = make_meeting(self.report, meeting_date=date(2026, 1, 5))  # created second
        early_action = make_action(early, "From the back-dated meeting")
        self.assertEqual(self.create(carried=[(late_action, "", "")], main_matters="x").status_code, 302)
        late_action.refresh_from_db()
        early_action.refresh_from_db()
        self.assertEqual(late_action.reviewed_in_id, self.newest().pk)
        self.assertIsNone(early_action.reviewed_in_id)

    # Catches a second meeting on the same day failing to review the first's actions.
    def test_same_day_second_meeting_carries_from_first(self):
        first = make_meeting(self.report, meeting_date=date(2026, 2, 1))
        a = make_action(first, "Morning action")
        self.assertEqual(self.create("2026-02-01", carried=[(a, "AMBER", "")], main_matters="PM").status_code, 302)
        a.refresh_from_db()
        self.assertEqual((a.reviewed_in_id, a.rag), (self.newest().pk, "AMBER"))

    # Catches a back-dated meeting reviewing actions agreed after its own date.
    def test_back_dated_meeting_pins_nothing(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        # Back-dated, it can only be recorded as already held (leg 3).
        response = self.create("2026-01-01", carried=[(a, "", "")], main_matters="Back-dated notes", hold=True)
        self.assertEqual(response.status_code, 302)
        a.refresh_from_db()
        self.assertIsNone(a.reviewed_in_id)

    # Catches a back-dated meeting writing a rating it may not hold, or discarding what was typed.
    def test_back_dated_meeting_with_typed_rating_is_refused_and_text_kept(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        before = snapshot(a)
        response = self.create(
            "2026-01-01", carried=[(a, "RED", "Blocked by cover")], main_matters="Back-dated notes kept"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Blocked by cover")
        self.assertContains(response, "Back-dated notes kept")
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        self.assertEqual(snapshot(a), before)

    # Catches another report's open actions being pinned into this report's meeting.
    def test_another_reports_actions_are_never_pinned(self):
        other = make_staff("other@oxlip.test", line_manager_email=self.manager_email)
        theirs = make_action(make_meeting(other, meeting_date=date(2026, 1, 10)), "Theirs")
        mine = make_action(make_meeting(self.report, meeting_date=date(2026, 1, 10)), "Mine")
        self.assertEqual(self.create(carried=[(mine, "", "")], main_matters="x").status_code, 302)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.reviewed_in_id)

    # Catches the carried formset's save writing back a stale reviewed_in and unpinning the action.
    def test_rating_saved_on_create_does_not_unpin(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        self.assertEqual(self.create(carried=[(a, "RED", "Stuck on timetabling")]).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(
            (a.reviewed_in_id, a.rag, a.review_comment), (self.newest().pk, "RED", "Stuck on timetabling")
        )


class StaleCarryForwardTests(_ManagerCreateMixin, TestCase):
    """A create page whose carried list went out of date is refused, text handed back."""

    # Catches a stale page's rating being written onto an action another meeting now reviews.
    def test_stale_page_rating_is_refused_and_typed_text_handed_back(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "Chase the supplier")
        page = self.client.get(reverse("line_management:meeting_new", args=[self.report.pk]))
        self.assertContains(page, "Chase the supplier")

        # Meanwhile, another meeting is created and pins the action.
        self.assertEqual(
            self.create("2026-02-01", carried=[(a, "", "")], main_matters="Other tab", hold=True).status_code,
            302,
        )
        count = LineMeeting.objects.filter(staff=self.report).count()

        response = self.create(
            "2026-02-02", carried=[(a, "RED", "Supplier never replied")], main_matters="Stale page notes"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), count)
        self.assertContains(response, "Supplier never replied")
        self.assertContains(response, "Stale page notes")
        a.refresh_from_db()
        self.assertEqual((a.rag, a.review_comment), ("", ""))

    # Catches an action added after the page loaded being pinned without the user seeing it.
    def test_action_added_to_source_after_page_load_is_not_silently_pinned(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        shown = make_action(prev, "Shown on page")
        unseen = make_action(prev, "Added after page load")
        response = self.create(carried=[(shown, "GREEN", "")], main_matters="Notes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        shown.refresh_from_db()
        unseen.refresh_from_db()
        self.assertIsNone(shown.reviewed_in_id)
        self.assertIsNone(unseen.reviewed_in_id)

    # Catches start_meeting keeping a half-made meeting when the pinned set differs from the shown set.
    def test_start_meeting_rolls_back_when_pinned_set_differs_from_shown(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        make_action(prev, "B")
        meeting = LineMeeting(staff=self.report, meeting_date=date(2026, 2, 1), main_matters="x")
        with self.assertRaises(CarryForwardChanged):
            start_meeting(meeting, source=prev, pin=True, shown_ids=[a.pk])
        self.assertIsNone(meeting.pk)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        self.assertFalse(MeetingAction.objects.filter(reviewed_in__isnull=False).exists())


class CreateWithActionsTests(_ManagerCreateMixin, TestCase):
    """The create flow with agreed and carried actions."""

    # Catches agreed actions typed on the create page being dropped.
    def test_create_saves_agreed_actions(self):
        self.assertEqual(self.create(agreed=["Book the room", "Draft the letter", ""]).status_code, 302)
        self.assertEqual(
            list(self.newest().agreed_actions.values_list("description", flat=True)),
            ["Book the room", "Draft the letter"],
        )

    # Catches the blank "Add an action" rows being saved as empty actions.
    def test_blank_extra_rows_create_nothing(self):
        self.assertEqual(self.create(agreed=["", "", ""], main_matters="Notes only").status_code, 302)
        self.assertFalse(MeetingAction.objects.exists())

    # Catches a meeting whose only content is ratings being refused as empty.
    def test_create_with_only_carried_ratings_is_saved(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        self.assertEqual(self.create(carried=[(a, "GREEN", "")]).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.rag, "GREEN")

    # Catches a date-only create persisting a blank meeting that swallows last meeting's actions.
    def test_date_only_create_is_rejected_and_pins_nothing(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        self.assertEqual(self.create(carried=[(a, "", "")]).status_code, 200)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        a.refresh_from_db()
        self.assertIsNone(a.reviewed_in_id)

    # Catches a double-clicked save creating two meetings or two sets of actions.
    def test_double_submit_with_actions_creates_one_meeting_and_one_set_of_actions(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        first = self.create(carried=[(a, "AMBER", "Going")], agreed=["New one"], main_matters="x")
        second = self.create(carried=[(a, "AMBER", "Going")], agreed=["New one"], main_matters="x")
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 2)
        self.assertEqual(MeetingAction.objects.filter(description="New one").count(), 1)

    # Catches two genuine actions-only meetings on one day being folded into one, losing the second.
    def test_two_actions_only_meetings_on_same_day_are_not_folded(self):
        self.assertEqual(self.create("2026-02-01", agreed=["Call parent"], hold=True).status_code, 302)
        call = MeetingAction.objects.get(description="Call parent")
        self.assertEqual(
            self.create("2026-02-01", carried=[(call, "", "")], agreed=["Book room"]).status_code, 302
        )
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 2)
        self.assertTrue(MeetingAction.objects.filter(description="Book room").exists())

    # Catches an invalid create discarding the actions and comments that were typed.
    def test_invalid_create_rerenders_typed_actions_and_comments(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        response = self.create(
            "not-a-date",
            carried=[(a, "RED", "Typed review comment")],
            agreed=["Typed new action"],
            main_matters="Typed notes",
        )
        self.assertEqual(response.status_code, 200)
        for text in ("Typed review comment", "Typed new action", "Typed notes"):
            self.assertContains(response, text)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        self.assertFalse(MeetingAction.objects.filter(description="Typed new action").exists())


class ActionLossTests(TestCase):
    """Nothing typed against an action is lost or silently dropped (article 6)."""

    def setUp(self):
        self.manager_email = "boss@oxlip.test"
        self.manager_user = make_user(self.manager_email)
        make_staff(self.manager_email)
        self.report = make_staff("report@oxlip.test", line_manager_email=self.manager_email)
        self.m1 = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        self.m1.main_matters = "First"
        self.m1.save()
        self.client.force_login(self.manager_user)

    def save(self, meeting, rows):
        data = meeting_payload(
            meeting_date=meeting.meeting_date.isoformat(), main_matters=meeting.main_matters
        )
        data.update(rows)
        return self.client.post(reverse("line_management:meeting_save", args=[meeting.pk]), versioned(data, meeting))

    def _pinned(self):
        m2 = make_meeting(self.report, meeting_date=date(2026, 1, 20))
        return make_action(self.m1, "Settled wording", reviewed_in=m2, rag="RED", comment="Blocked"), m2

    # Catches a crafted DELETE removing a reviewed action (and its rating) under "Meeting saved".
    def test_pinned_action_cannot_be_deleted_via_crafted_post(self):
        action, _ = self._pinned()
        before = snapshot(action)
        response = self.save(self.m1, agreed_rows([(action, "Settled wording", True)]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can no longer be changed or removed")
        self.assertNotContains(response, "Meeting saved")
        self.assertEqual(snapshot(action), before)

    # Catches a crafted reword of a reviewed action being applied, or dropped in silence.
    def test_pinned_action_cannot_be_reworded_via_crafted_post(self):
        action, _ = self._pinned()
        before = snapshot(action)
        response = self.save(self.m1, agreed_rows([(action, "Quietly rewritten", False)]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can no longer be changed or removed")
        self.assertContains(response, "Quietly rewritten")
        self.assertNotContains(response, "Meeting saved")
        self.assertEqual(snapshot(action), before)

    # Catches the remove tick box not working on an action nobody has reviewed yet.
    def test_unreviewed_action_can_be_deleted(self):
        action = make_action(self.m1, "Change of plan")
        response = self.save(self.m1, agreed_rows([(action, "Change of plan", True)]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MeetingAction.objects.filter(pk=action.pk).exists())

    # Catches an edit to a sibling action writing back stale review columns on a pinned one.
    def test_editing_unpinned_action_does_not_wipe_rating_on_pinned_sibling(self):
        pinned, m2 = self._pinned()
        sibling = make_action(self.m1, "Added later in the admin")
        before = snapshot(pinned)
        response = self.save(
            self.m1,
            agreed_rows([(pinned, "Settled wording", False), (sibling, "Reworded sibling", False)]),
        )
        self.assertEqual(response.status_code, 302)
        sibling.refresh_from_db()
        self.assertEqual(sibling.description, "Reworded sibling")
        self.assertEqual(snapshot(pinned), before)

        # Saving the reviewing meeting with the rating unchanged keeps it too.
        self.save(m2, carried_rows([(pinned, "RED", "Blocked")]))
        pinned.refresh_from_db()
        self.assertEqual((pinned.reviewed_in_id, pinned.rag, pinned.review_comment), (m2.pk, "RED", "Blocked"))

    # Catches blank "New action" rows on an older meeting, whose actions would never be reviewed.
    def test_non_latest_meeting_renders_no_blank_add_action_rows(self):
        latest = make_meeting(self.report, meeting_date=date(2026, 1, 20))
        old = self.client.get(reverse("line_management:meeting_detail", args=[self.m1.pk]))
        self.assertEqual(old.context["agreed_formset"].extra_forms, [])
        self.assertNotContains(old, ">New action ")
        # The label being asserted absent above is really the blank-row label.
        self.assertContains(
            self.client.get(reverse("line_management:meeting_detail", args=[latest.pk])), ">New action "
        )

    # Catches action text or a review comment changing across save, reload and re-save.
    def test_action_text_round_trips_emoji_crlf_and_curly_quotes(self):
        text = "Ring parent \U0001F4DE about the “trip” — café\r\nThen ‘confirm’ by Friday"
        comment = "Done ✅ “mostly”\r\n\r\nSee note — naïve"
        create_url = reverse("line_management:meeting_create", args=[self.report.pk])

        payload = meeting_payload(meeting_date="2026-01-12", main_matters="Second", hold="1")
        payload.update(agreed_rows(new=[text]))
        self.assertEqual(self.client.post(create_url, payload).status_code, 302)
        action = MeetingAction.objects.get(agreed_at__staff=self.report)
        self.assertEqual(action.description, text)

        m2 = action.agreed_at
        page = self.client.get(reverse("line_management:meeting_detail", args=[m2.pk]))
        self.assertContains(page, escape(text))
        self.assertEqual(self.save(m2, agreed_rows([(action, text, False)])).status_code, 302)
        action.refresh_from_db()
        self.assertEqual(action.description, text)

        # The review comment written at the next meeting round-trips the same way.
        payload = meeting_payload(meeting_date="2026-02-10")
        payload.update(carried_rows([(action, "GREEN", comment)]))
        self.assertEqual(self.client.post(create_url, payload).status_code, 302)
        action.refresh_from_db()
        self.assertEqual(action.review_comment, comment)

        m3 = action.reviewed_in
        page = self.client.get(reverse("line_management:meeting_detail", args=[m3.pk]))
        self.assertContains(page, escape(comment))
        self.assertEqual(self.save(m3, carried_rows([(action, "GREEN", comment)])).status_code, 302)
        action.refresh_from_db()
        self.assertEqual((action.description, action.review_comment), (text, comment))


class LegacyActionProseTests(TestCase):
    """Legacy free-text action fields are never on a form, so never overwritten."""

    LEGACY_AGREED = "Legacy agreed — line one\r\nline two"

    def setUp(self):
        self.manager_email = "boss@oxlip.test"
        self.manager_user = make_user(self.manager_email)
        make_staff(self.manager_email)
        self.report = make_staff("report@oxlip.test", line_manager_email=self.manager_email)
        self.legacy = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        self.legacy.actions_from_last_meeting = "Legacy review of last time"
        self.legacy.actions_from_meeting = self.LEGACY_AGREED
        self.legacy.main_matters = "Legacy notes"
        self.legacy.save()
        self.client.force_login(self.manager_user)

    # Catches a crafted POST overwriting or clearing stored legacy action prose.
    def test_crafted_post_cannot_overwrite_legacy_action_prose(self):
        payload = meeting_payload(
            meeting_date="2026-01-10",
            main_matters="Legacy notes",
            actions_from_meeting="HACKED",
            actions_from_last_meeting="",
        )
        self.client.post(reverse("line_management:meeting_save", args=[self.legacy.pk]), versioned(payload, self.legacy))
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.actions_from_meeting, self.LEGACY_AGREED)
        self.assertEqual(self.legacy.actions_from_last_meeting, "Legacy review of last time")

    # Catches stored legacy prose vanishing from the page, or being offered as an editable field.
    def test_stored_legacy_prose_is_shown_read_only(self):
        page = self.client.get(reverse("line_management:meeting_detail", args=[self.legacy.pk]))
        self.assertContains(page, "Legacy review of last time")
        self.assertContains(page, "Legacy agreed — line one")
        self.assertContains(page, "line two")
        self.assertNotContains(page, 'name="actions_from_meeting"')
        self.assertNotContains(page, 'name="actions_from_last_meeting"')

    # Catches a create or save writing into the legacy fields of a record that never had them.
    def test_blank_legacy_fields_stay_blank_after_save(self):
        payload = meeting_payload(
            meeting_date="2026-02-01",
            main_matters="New style meeting",
            actions_from_meeting="SMUGGLED",
            actions_from_last_meeting="SMUGGLED",
        )
        response = self.client.post(
            reverse("line_management:meeting_create", args=[self.report.pk]), payload
        )
        self.assertEqual(response.status_code, 302)
        new = LineMeeting.objects.filter(staff=self.report).order_by("-pk").first()
        self.client.post(reverse("line_management:meeting_save", args=[new.pk]), versioned(payload, new))
        new.refresh_from_db()
        self.assertEqual((new.actions_from_meeting, new.actions_from_last_meeting), ("", ""))


class AdminPinnedActionTests(TestCase):
    """A reviewed action is settled in the admin too: no delete, no reword."""

    def setUp(self):
        self.super_user = make_user("root@oxlip.test", is_superuser=True)
        person = make_staff("person@oxlip.test")
        self.m1 = make_meeting(person, meeting_date=date(2026, 1, 10))
        self.m2 = make_meeting(person, meeting_date=date(2026, 1, 20))
        self.pinned = make_action(
            self.m1, "Settled wording", reviewed_in=self.m2, rag="RED", comment="Blocked"
        )
        self.client.force_login(self.super_user)
        self.meeting_change = reverse("admin:line_management_linemeeting_change", args=[self.m1.pk])

    def _inline_post(self, **row):
        data = {
            "created_by_email": "",
            "meeting_date": "2026-01-10",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "",
            "actions_from_meeting": "",
            **management_form("agreed_actions", 1, 1),
            "agreed_actions-0-id": str(self.pinned.pk),
            "agreed_actions-0-agreed_at": str(self.m1.pk),
            "agreed_actions-0-description": "Settled wording",
            "agreed_actions-0-rag": "RED",
            "agreed_actions-0-review_comment": "Blocked",
            "_save": "Save",
        }
        data.update(row)
        # A fresh admin page's stamp, so this exercises the inline guard rather
        # than the stale-form refusal.
        return self.client.post(self.meeting_change, versioned(data, self.m1))

    # Catches the meeting inline deleting or rewording a reviewed action (and its rating).
    def test_inline_post_cannot_delete_or_reword_pinned_action(self):
        response = self._inline_post(
            **{"agreed_actions-0-DELETE": "on", "agreed_actions-0-description": "Rewritten in admin"}
        )
        self.assertEqual(response.status_code, 302)
        action = MeetingAction.objects.filter(pk=self.pinned.pk).first()
        self.assertIsNotNone(action, "pinned action was deleted through the inline")
        self.assertEqual(
            (action.description, action.reviewed_in_id, action.rag, action.review_comment),
            ("Settled wording", self.m2.pk, "RED", "Blocked"),
        )

    # Catches the inline offering a live delete box on a reviewed action.
    def test_inline_delete_checkbox_is_disabled_for_pinned_row(self):
        page = self.client.get(self.meeting_change).content.decode()
        tag = re.search(r'<input[^>]*name="agreed_actions-0-DELETE"[^>]*>', page)
        self.assertIsNotNone(tag, "no DELETE box rendered for the pinned row")
        self.assertIn("disabled", tag.group(0))

    # Catches the action admin letting a superuser reword a reviewed action.
    def test_action_admin_renders_description_read_only_for_pinned_action(self):
        url = reverse("admin:line_management_meetingaction_change", args=[self.pinned.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="description"')
        self.assertContains(page, "Settled wording")

        unpinned = make_action(self.m2, "Still open")
        url = reverse("admin:line_management_meetingaction_change", args=[unpinned.pk])
        self.assertContains(self.client.get(url), 'name="description"')


class MeetingDateChangeTests(TestCase):
    """meeting_save refuses a date change that would take actions out of the review cycle."""

    def setUp(self):
        self.manager_user = make_user("boss@oxlip.test")
        make_staff("boss@oxlip.test")
        self.report = make_staff("report@oxlip.test", line_manager_email="boss@oxlip.test")
        self.client.force_login(self.manager_user)

    def _save_date(self, meeting, new_date):
        payload = meeting_payload(meeting_date=new_date, main_matters=meeting.main_matters)
        return self.client.post(reverse("line_management:meeting_save", args=[meeting.pk]), versioned(payload, meeting))

    # Catches a reviewing meeting being re-dated before the meeting whose actions it reviews.
    def test_reviewing_meeting_cannot_be_dated_before_the_actions_it_reviews(self):
        m1 = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        m2 = make_meeting(self.report, meeting_date=date(2026, 1, 20))
        m2.main_matters = "Review"
        m2.save()
        make_action(m1, "A", reviewed_in=m2, rag="GREEN")
        response = self._save_date(m2, "2026-01-05")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can&#x27;t be dated before then")
        m2.refresh_from_db()
        self.assertEqual(m2.meeting_date, date(2026, 1, 20))

    # Catches a meeting with open actions being re-dated behind a later meeting, orphaning them.
    def test_meeting_with_unreviewed_actions_cannot_be_dated_before_a_later_meeting(self):
        make_meeting(self.report, meeting_date=date(2026, 1, 15))
        latest = make_meeting(self.report, meeting_date=date(2026, 1, 20))
        latest.main_matters = "Latest"
        latest.save()
        make_action(latest, "Still open")
        response = self._save_date(latest, "2026-01-12")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "never come up for review")
        latest.refresh_from_db()
        self.assertEqual(latest.meeting_date, date(2026, 1, 20))


class BackDatedAndStaleSaveTests(_ManagerCreateMixin, TestCase):
    """Back-dated creates, stale pages and vanished rows: refused visibly, text kept."""

    # Catches a back-dated create recording new actions that would never come up for review.
    def test_back_dated_create_with_new_action_is_refused_and_text_kept(self):
        make_meeting(self.report, meeting_date=date(2026, 1, 10))
        response = self.create("2026-01-01", agreed=["Back-dated action text"], main_matters="Old notes")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Back-dated action text")
        self.assertContains(response, "Old notes")
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        self.assertFalse(MeetingAction.objects.exists())

    # Catches a back-dated notes-only create silently skipping last meeting's actions without saying so.
    def test_back_dated_notes_only_create_saves_and_says_actions_not_reviewed(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "A")
        payload = meeting_payload(meeting_date="2026-01-01", main_matters="Catch-up notes", hold="1")
        payload.update(carried_rows([(a, "", "")]))
        response = self.client.post(self.create_url, payload, follow=True)
        self.assertEqual(response.redirect_chain[-1][1], 302)
        self.assertContains(response, "actions were not reviewed here")
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 2)
        a.refresh_from_db()
        self.assertIsNone(a.reviewed_in_id)

    # Catches a stale page adding actions to a meeting that is no longer the latest.
    def test_stale_page_new_action_on_non_latest_meeting_is_refused_and_echoed(self):
        m1 = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        m1.main_matters = "First"
        m1.save()
        make_meeting(self.report, meeting_date=date(2026, 1, 20))
        payload = meeting_payload(meeting_date="2026-01-10", main_matters="First")
        payload.update(agreed_rows(new=["Typed on a stale page"]))
        response = self.client.post(reverse("line_management:meeting_save", args=[m1.pk]), versioned(payload, m1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A later meeting has been added")
        self.assertContains(response, "Typed on a stale page")
        self.assertFalse(MeetingAction.objects.filter(description="Typed on a stale page").exists())

    # Catches the stale-page echo hiding that the typed rating's action was already reviewed elsewhere.
    def test_stale_refusal_says_where_the_action_was_already_reviewed(self):
        prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(prev, "Chase the supplier")
        self.assertEqual(
            self.create("2026-02-01", carried=[(a, "", "")], main_matters="Other tab", hold=True).status_code,
            302,
        )
        response = self.create("2026-02-02", carried=[(a, "RED", "Never replied")], main_matters="Stale")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Never replied")
        self.assertContains(response, "already reviewed at the meeting of 1 Feb 2026")

    # Catches a vanished row reporting Django's "Select a valid choice" instead of plain English.
    def test_row_no_longer_on_meeting_shows_plain_english_error(self):
        m1 = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        m1.main_matters = "First"
        m1.save()
        gone = make_action(m1, "Removed in another tab")
        gone_pk = gone.pk
        gone.delete()
        payload = meeting_payload(meeting_date="2026-01-10", main_matters="First")
        payload.update(management_form("agreed", 1, 1))
        payload["agreed-0-id"] = str(gone_pk)
        payload["agreed-0-description"] = "My edit to the removed action"
        response = self.client.post(reverse("line_management:meeting_save", args=[m1.pk]), versioned(payload, m1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "removed or moved since you opened it")
        self.assertNotContains(response, "Select a valid choice")
        self.assertContains(response, "My edit to the removed action")


# ---------------------------------------------------------------------------
# Leg 2 of docs/chart/line-meeting-preparation.md: a stale save is refused and
# the typed text handed back (the hidden ``meeting_version`` stamp, the
# compare-and-swap save, and every writer advancing the version).
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402
from datetime import timezone as dt_timezone  # noqa: E402
from unittest import mock  # noqa: E402

from django.utils import timezone  # noqa: E402

from . import services as lm_services  # noqa: E402
from .services import (  # noqa: E402
    MeetingChanged,
    _next_version,
    parse_version,
    save_meeting_page,
    touch_meetings,
)
from .permissions import SCOPE_ALL  # noqa: E402
from .views import _bind  # noqa: E402

# Every meeting is aged to this stamp in setUp, so "the version moved" never
# depends on the test clock ticking between two writes (Windows clocks are coarse).
OLD_STAMP = datetime(2020, 1, 1, 9, 0, 0, tzinfo=dt_timezone.utc)


def age_meetings(*meetings):
    LineMeeting.objects.filter(pk__in=[m.pk for m in meetings]).update(updated_at=OLD_STAMP)


def stamp_of(meeting):
    return LineMeeting.objects.values_list("updated_at", flat=True).get(pk=meeting.pk)


def db_state():
    """Every meeting and action row, exactly: a refused save must leave all of it alone."""
    return (
        list(LineMeeting.objects.order_by("pk").values()),
        list(MeetingAction.objects.order_by("pk").values()),
    )


class _StalePageMixin:
    """A manager, a report, and meeting ``self.meeting`` (the latest) reviewing one action."""

    def setUp(self):
        self.manager_email = "boss@oxlip.test"
        self.manager_user = make_user(self.manager_email)
        make_staff(self.manager_email)
        self.report_user = make_user("report@oxlip.test")
        self.report = make_staff("report@oxlip.test", line_manager_email=self.manager_email)
        self.prev = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        self.meeting = make_meeting(
            self.report, created_by_email=self.manager_email, meeting_date=date(2026, 2, 1)
        )
        self.carried = make_action(self.prev, "Chase the supplier", reviewed_in=self.meeting)
        LineMeeting.objects.filter(pk=self.meeting.pk).update(main_matters="Original notes")
        age_meetings(self.prev, self.meeting)
        self.save_url = reverse("line_management:meeting_save", args=[self.meeting.pk])
        self.client.force_login(self.manager_user)

    def version(self):
        return meeting_version(LineMeeting.objects.get(pk=self.meeting.pk))

    def page(self, version, *, main_matters="Original notes", new=(), rag="", comment="", **extra):
        """What the meeting page posts: notes, blank-row actions, the carried row, the stamp."""
        payload = meeting_payload(meeting_date="2026-02-01", main_matters=main_matters)
        payload.update(agreed_rows(new=new))
        payload.update(carried_rows([(self.carried, rag, comment)]))
        if version is not None:
            payload["meeting_version"] = version
        payload.update(extra)
        return payload

    def stored_notes(self):
        return LineMeeting.objects.values_list("main_matters", flat=True).get(pk=self.meeting.pk)


class StaleMeetingSaveTests(_StalePageMixin, TestCase):
    """Two pages open on one meeting: the second save is refused and its text handed back."""

    # Catches a second tab silently overwriting the first tab's save (last-write-wins).
    def test_second_tab_save_is_refused_and_every_typed_value_handed_back(self):
        v0 = self.version()
        self.assertEqual(
            self.client.post(self.save_url, self.page(v0, main_matters="Tab one notes")).status_code, 302
        )
        before = db_state()
        notes = "Tab two — “careful” café\r\nsecond line \U0001f600"
        action = "Book the room ✅ by Friday"
        comment = "Supplier replied\r\nat last \U0001f389"
        csrf = "c" * 64
        response = self.client.post(
            self.save_url,
            self.page(v0, main_matters=notes, new=[action], rag="GREEN", comment=comment,
                      csrfmiddlewaretoken=csrf),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(db_state(), before, "a refused save wrote something")
        self.assertEqual(self.stored_notes(), "Tab one notes")
        for typed in (notes, action, comment, "GREEN"):
            self.assertContains(response, escape(typed), status_code=409)
        for label in ("Main matters to discuss", "New action from this meeting (row 1)",
                      "Rating for: Chase the supplier", "Comment for: Chase the supplier"):
            self.assertContains(response, label, status_code=409)
        self.assertNotContains(response, v0, status_code=409)
        self.assertNotContains(response, self.version(), status_code=409)
        self.assertNotContains(response, csrf, status_code=409)
        self.assertNotContains(response, "Meeting version", status_code=409)

    # Catches a missing or malformed stamp being read as "skip the check" and saving.
    def test_missing_garbled_or_naive_stamp_is_refused_and_nothing_written(self):
        naive = LineMeeting.objects.get(pk=self.meeting.pk).updated_at.astimezone(
            dt_timezone.utc
        ).replace(tzinfo=None).isoformat()
        before = db_state()
        for stamp in (None, "", "not-a-date", "2026-13-45T99:00:00+00:00", naive):
            with self.subTest(stamp=stamp):
                response = self.client.post(
                    self.save_url, self.page(stamp, main_matters="Should not land", new=["Nor this"])
                )
                self.assertEqual(response.status_code, 409)
                self.assertContains(response, "Should not land", status_code=409)
                self.assertEqual(db_state(), before)

    # Catches a double-clicked Save either saving twice or answering the second click with a 409.
    def test_double_clicked_save_redirects_both_times_and_saves_once(self):
        payload = self.page(self.version(), main_matters="Clicked twice")
        first = self.client.post(self.save_url, payload)
        after_first = db_state()
        second = self.client.post(self.save_url, payload)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(db_state(), after_first, "the second click wrote again")
        self.assertEqual(self.stored_notes(), "Clicked twice")

    # Catches a double-clicked Save with a new action duplicating the action, and a
    # stale page with different content being folded in as a "repeat".
    def test_double_clicked_save_with_new_action_creates_one_action_but_changed_stale_page_409s(self):
        v0 = self.version()
        payload = self.page(v0, main_matters="Clicked twice", new=["Only once please"])
        self.assertEqual(self.client.post(self.save_url, payload).status_code, 302)
        self.assertEqual(self.client.post(self.save_url, payload).status_code, 302)
        self.assertEqual(MeetingAction.objects.filter(description="Only once please").count(), 1)

        before = db_state()
        response = self.client.post(self.save_url, self.page(v0, main_matters="Something else"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(db_state(), before)

    # Catches an actions-only save leaving the version alone, so another open page
    # still passes the check and saves over it.
    def test_action_only_save_makes_other_open_page_stale(self):
        v0 = self.version()
        self.assertEqual(self.client.post(self.save_url, self.page(v0, new=["Action only"])).status_code, 302)
        self.assertNotEqual(self.version(), v0, "an actions-only save did not advance the version")

        before = db_state()
        unchanged = self.client.post(self.save_url, self.page(v0))
        self.assertEqual(unchanged.status_code, 302, "a no-change stale page should just redirect")
        self.assertEqual(db_state(), before)

        changed = self.client.post(self.save_url, self.page(v0, main_matters="Tab B edits"))
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(db_state(), before)
        self.assertContains(changed, "Tab B edits", status_code=409)

    # Catches pinning N's actions into N+1 leaving N's open page able to save over them.
    def test_creating_next_meeting_makes_previous_meetings_open_page_stale(self):
        own = make_action(self.meeting, "Draft the rota")
        v0 = self.version()
        create = meeting_payload(meeting_date="2026-03-01", main_matters="Next meeting")
        create.update(carried_rows([(own, "AMBER", "Half done")]))
        create.update(agreed_rows())
        response = self.client.post(
            reverse("line_management:meeting_create", args=[self.report.pk]), create
        )
        self.assertEqual(response.status_code, 302)
        own.refresh_from_db()
        self.assertIsNotNone(own.reviewed_in_id)

        before = db_state()
        stale = self.page(v0, main_matters="Late edit on N")
        stale.update(agreed_rows(existing=[(own, "Draft the rota", False)]))
        response = self.client.post(self.save_url, stale)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(db_state(), before)
        self.assertContains(response, "Late edit on N", status_code=409)

    # Catches an invalid stale POST being re-rendered with the fresh stamp, which the
    # next Save would then pass (laundering the stale page).
    def test_invalid_stale_resubmit_is_refused_not_rerendered_with_fresh_stamp(self):
        v0 = self.version()
        self.client.post(self.save_url, self.page(v0, main_matters="Tab one notes"))
        before = db_state()
        response = self.client.post(
            self.save_url, self.page(v0, main_matters="Tab two text", meeting_date="not a date")
        )
        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, self.version(), status_code=409)
        self.assertContains(response, "Tab two text", status_code=409)
        self.assertEqual(db_state(), before)


class VersionAccessOrderTests(_StalePageMixin, TestCase):
    """Gauntlet stage 3: the 403s come before the version check, so a hand-back is
    never shown to someone without edit rights, and never leaks another record."""

    # Catches a repointed (outgoing) manager getting a 409 hand-back instead of a 403.
    def test_outgoing_manager_gets_403_with_stale_or_valid_stamp_and_no_text_echoed(self):
        make_user("successor@oxlip.test")
        make_staff("successor@oxlip.test")
        valid = self.version()
        self.report.line_manager_email = "successor@oxlip.test"
        self.report.save()
        before = db_state()
        for stamp in (valid, "2000-01-01T00:00:00+00:00"):
            with self.subTest(stamp=stamp):
                response = self.client.post(
                    self.save_url, self.page(stamp, main_matters="Outgoing typed this")
                )
                self.assertEqual(response.status_code, 403)
                self.assertNotContains(response, "Outgoing typed this", status_code=403)
        self.assertEqual(db_state(), before)

    # Catches a stranger reaching the version check at all.
    def test_stranger_gets_403_whatever_the_stamp(self):
        stranger = make_user("stranger@oxlip.test")
        make_staff("stranger@oxlip.test")
        before = db_state()
        self.client.force_login(stranger)
        for stamp in (self.version(), "2000-01-01T00:00:00+00:00", None):
            with self.subTest(stamp=stamp):
                response = self.client.post(
                    self.save_url, self.page(stamp, main_matters="Not theirs to write")
                )
                self.assertEqual(response.status_code, 403)
                self.assertNotContains(response, "Not theirs to write", status_code=403)
        self.assertEqual(db_state(), before)

    # Catches the report writing to their own held meeting, whatever the stamp: they get
    # their own text back (409) and nothing is written.
    def test_report_on_held_meeting_gets_409_whatever_the_stamp(self):
        before = db_state()
        self.client.force_login(self.report_user)
        for stamp in (self.version(), "2000-01-01T00:00:00+00:00", None):
            with self.subTest(stamp=stamp):
                response = self.client.post(
                    self.save_url, self.page(stamp, main_matters="Typed after it was held")
                )
                self.assertEqual(response.status_code, 409)
                self.assertContains(response, "Typed after it was held", status_code=409)
        self.assertEqual(db_state(), before)

    # Catches a crafted carried-N-id making the hand-back label show another report's action wording.
    def test_handback_label_never_shows_another_reports_action_wording(self):
        other = make_staff("other@oxlip.test", line_manager_email=self.manager_email)
        other_prev = make_meeting(other, meeting_date=date(2026, 1, 5))
        other_meeting = make_meeting(other, meeting_date=date(2026, 1, 25))
        foreign = make_action(other_prev, "Confidential wording for other", reviewed_in=other_meeting)
        payload = self.page("2000-01-01T00:00:00+00:00", main_matters="Stale notes")
        payload.update(carried_rows([(foreign, "RED", "My typed comment")]))

        response = self.client.post(self.save_url, payload)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "My typed comment", status_code=409)
        self.assertNotContains(response, "Confidential wording for other", status_code=409)
        self.assertEqual(snapshot(foreign)[3:5], ("", ""))

    # Catches the version stamp being handed to a viewer who may not edit (or withheld from one who may).
    def test_only_the_editable_page_carries_a_version_stamp(self):
        detail = reverse("line_management:meeting_detail", args=[self.meeting.pk])
        self.assertContains(self.client.get(detail), 'name="meeting_version"')
        self.client.force_login(self.report_user)
        report_page = self.client.get(detail)
        self.assertEqual(report_page.status_code, 200)
        self.assertNotContains(report_page, 'name="meeting_version"')


class SaveMeetingPageServiceTests(TestCase):
    """save_meeting_page: the notes and the actions land together or not at all."""

    def setUp(self):
        self.manager_user = make_user("boss@oxlip.test")
        make_staff("boss@oxlip.test")
        self.staff = make_staff("person@oxlip.test", line_manager_email="boss@oxlip.test")
        self.meeting = make_meeting(self.staff, meeting_date=date(2026, 2, 1))
        LineMeeting.objects.filter(pk=self.meeting.pk).update(main_matters="Stored notes")
        self.action = make_action(self.meeting, "Original wording")
        self.later = make_meeting(self.staff, meeting_date=date(2026, 3, 1))
        age_meetings(self.meeting, self.later)

    def _payload(self, text, delete=False):
        payload = meeting_payload(meeting_date="2026-02-01", main_matters="New notes")
        payload.update(agreed_rows(existing=[(self.action, text, delete)]))
        return payload

    def _valid_forms(self, data):
        meeting = LineMeeting.objects.get(pk=self.meeting.pk)
        forms = _bind(meeting, meeting.reviewed_actions.all(), scope=SCOPE_ALL, data=data)
        for f in forms:
            self.assertTrue(f.is_valid(), getattr(f, "errors", None))
        return meeting, forms

    def _pin_behind_the_pages_back(self):
        # An ORM update that does not touch self.meeting: the version check has passed.
        MeetingAction.objects.filter(pk=self.action.pk).update(reviewed_in=self.later)

    def _assert_rolled_back(self):
        meeting = LineMeeting.objects.get(pk=self.meeting.pk)
        self.assertEqual(meeting.main_matters, "Stored notes", "notes committed without the actions")
        self.assertEqual(meeting.updated_at, OLD_STAMP)
        action = MeetingAction.objects.filter(pk=self.action.pk).first()
        self.assertIsNotNone(action, "pinned action was deleted")
        self.assertEqual((action.description, action.reviewed_in_id), ("Original wording", self.later.pk))

    # Catches a reword of an action pinned mid-save committing the notes anyway.
    def test_reword_of_action_pinned_mid_save_raises_and_rolls_back_notes(self):
        meeting, (form, agreed, carried) = self._valid_forms(self._payload("Reworded"))
        self._pin_behind_the_pages_back()
        with self.assertRaises(MeetingChanged):
            save_meeting_page(meeting, meeting.updated_at, form, agreed, carried)
        self._assert_rolled_back()

    # Catches a delete of an action pinned mid-save being skipped under "Meeting saved".
    def test_delete_of_action_pinned_mid_save_raises_and_rolls_back_notes(self):
        meeting, (form, agreed, carried) = self._valid_forms(self._payload("Original wording", delete=True))
        self._pin_behind_the_pages_back()
        with self.assertRaises(MeetingChanged):
            save_meeting_page(meeting, meeting.updated_at, form, agreed, carried)
        self._assert_rolled_back()

    # Catches the view answering a mid-save pin with a 500 or a false "saved" instead of a 409 hand-back.
    def test_view_hands_back_text_when_action_is_pinned_mid_save(self):
        real = lm_services.save_meeting_page

        def pin_then_save(*args, **kwargs):
            self._pin_behind_the_pages_back()
            return real(*args, **kwargs)

        self.client.force_login(self.manager_user)
        url = reverse("line_management:meeting_save", args=[self.meeting.pk])
        with mock.patch("line_management.views.save_meeting_page", side_effect=pin_then_save):
            response = self.client.post(
                url, versioned(self._payload("Original wording", delete=True), self.meeting)
            )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "New notes", status_code=409)
        self._assert_rolled_back()


class VersionStampTests(TestCase):
    """The stamp itself: it must round-trip exactly and only ever move forward."""

    def setUp(self):
        self.meeting = make_meeting(make_staff("person@oxlip.test"))

    # Catches a stamp that loses precision on the round trip, making every fresh page look stale.
    def test_meeting_version_round_trips_exactly_with_and_without_microseconds(self):
        for when in (
            datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=dt_timezone.utc),
            datetime(2026, 7, 1, 23, 30, 5, 1, tzinfo=dt_timezone.utc),
        ):
            with self.subTest(when=when):
                LineMeeting.objects.filter(pk=self.meeting.pk).update(updated_at=when)
                stored = LineMeeting.objects.get(pk=self.meeting.pk)
                self.assertEqual(parse_version(meeting_version(stored)), stored.updated_at)
                self.assertEqual(parse_version(meeting_version(stored)), when)
        for junk in (None, "", "garbage", "2026-03-01T12:00:00"):
            with self.subTest(junk=junk):
                self.assertIsNone(parse_version(junk))

    # Catches a save on a coarse or skewed clock writing the same stamp twice.
    def test_next_version_strictly_increases_even_when_clock_is_behind(self):
        ahead = timezone.now() + timedelta(hours=1)
        self.assertEqual(_next_version(ahead), ahead + timedelta(microseconds=1))
        past = timezone.now() - timedelta(days=1)
        self.assertGreater(_next_version(past), past)
        fixed = datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt_timezone.utc)
        with mock.patch("line_management.services.timezone.now", return_value=fixed):
            self.assertGreater(_next_version(fixed), fixed)

    # Catches touch_meetings writing the stamp the meeting already has (clock not moved),
    # which leaves every open page's stamp valid.
    def test_touch_meetings_advances_version_even_when_clock_has_not_moved(self):
        fixed = datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt_timezone.utc)
        LineMeeting.objects.filter(pk=self.meeting.pk).update(updated_at=fixed)
        with mock.patch("line_management.services.timezone.now", return_value=fixed):
            touch_meetings(self.meeting.pk)
        self.assertNotEqual(stamp_of(self.meeting), fixed)


class VersionBumpWriterTests(TestCase):
    """Every writer that changes what a meeting page may edit advances that meeting's version."""

    def setUp(self):
        self.super_user = make_user("root@oxlip.test", is_superuser=True)
        self.person = make_staff("person@oxlip.test")
        self.m1 = make_meeting(self.person, meeting_date=date(2026, 1, 10))
        self.m2 = make_meeting(self.person, meeting_date=date(2026, 1, 20))
        self.pinned = make_action(self.m1, "Pinned", reviewed_in=self.m2)
        self.open = make_action(self.m2, "Open on m2")
        age_meetings(self.m1, self.m2)
        self.client.force_login(self.super_user)

    def assertMoved(self, *meetings):
        for m in meetings:
            self.assertNotEqual(stamp_of(m), OLD_STAMP, f"meeting {m.pk}'s version did not move")

    # Catches pinning actions into a new meeting leaving the source meeting's open page valid.
    def test_start_meeting_touches_the_source_meeting(self):
        new = LineMeeting(staff=self.person, meeting_date=date(2026, 2, 1))
        start_meeting(new, source=self.m2, pin=True, shown_ids=[self.open.pk])
        self.assertMoved(self.m2)

    # Catches an admin rating/comment edit not invalidating either meeting's open page.
    def test_action_admin_save_touches_both_meetings(self):
        url = reverse("admin:line_management_meetingaction_change", args=[self.pinned.pk])
        # A fresh admin form carries the action's stamp (stale forms are refused).
        fresh = MeetingAction.objects.get(pk=self.pinned.pk).updated_at.isoformat()
        response = self.client.post(
            url, {"rag": "GREEN", "review_comment": "Done", "action_version": fresh, "_save": "Save"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertMoved(self.m1, self.m2)

    # Catches an admin delete of an action not invalidating the meeting page that shows it.
    def test_action_admin_delete_touches_the_meeting(self):
        url = reverse("admin:line_management_meetingaction_delete", args=[self.open.pk])
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 302)
        self.assertFalse(MeetingAction.objects.filter(pk=self.open.pk).exists())
        self.assertMoved(self.m2)

    # Catches the bulk "Delete selected" path bypassing the version bump.
    def test_action_admin_bulk_delete_touches_every_affected_meeting(self):
        m3 = make_meeting(self.person, meeting_date=date(2026, 1, 30))
        also_open = make_action(m3, "Open on m3")
        age_meetings(m3)
        url = reverse("admin:line_management_meetingaction_changelist")
        response = self.client.post(url, {
            "action": "delete_selected",
            "_selected_action": [str(self.open.pk), str(also_open.pk)],
            "post": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MeetingAction.objects.filter(pk__in=[self.open.pk, also_open.pk]).exists())
        self.assertMoved(self.m2, m3)

    # Catches an admin save of a meeting (and its inline actions) leaving the reviewing meeting's page valid.
    def test_line_meeting_admin_save_touches_reviewing_meetings(self):
        data = {
            "created_by_email": "",
            "meeting_date": "2026-01-10",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "Admin note",
            "actions_from_meeting": "",
            **management_form("agreed_actions", 1, 1),
            "agreed_actions-0-id": str(self.pinned.pk),
            "agreed_actions-0-agreed_at": str(self.m1.pk),
            "agreed_actions-0-description": "Pinned",
            "agreed_actions-0-rag": "",
            "agreed_actions-0-review_comment": "",
            "_save": "Save",
        }
        url = reverse("admin:line_management_linemeeting_change", args=[self.m1.pk])
        response = self.client.post(url, versioned(data, self.m1))
        self.assertEqual(response.status_code, 302)
        self.assertMoved(self.m1, self.m2)


class AdminStaleFormTests(TestCase):
    """The LineMeeting admin change form carries the same stamp and refuses when stale."""

    def setUp(self):
        self.super_user = make_user("root@oxlip.test", is_superuser=True)
        self.meeting = make_meeting(make_staff("person@oxlip.test"), meeting_date=date(2026, 1, 10))
        LineMeeting.objects.filter(pk=self.meeting.pk).update(main_matters="Stored", updated_at=OLD_STAMP)
        self.url = reverse("admin:line_management_linemeeting_change", args=[self.meeting.pk])
        self.client.force_login(self.super_user)

    def _data(self, version):
        return {
            "created_by_email": "",
            "meeting_date": "2026-01-10",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "Admin typed this",
            "actions_from_meeting": "",
            "meeting_version": version,
            **management_form("agreed_actions"),
            "_save": "Save",
        }

    def _stored(self):
        return LineMeeting.objects.values_list("main_matters", flat=True).get(pk=self.meeting.pk)

    # Catches a stale admin form overwriting a newer page save.
    def test_stale_admin_form_is_refused_with_typed_text_kept(self):
        v0 = meeting_version(LineMeeting.objects.get(pk=self.meeting.pk))
        LineMeeting.objects.filter(pk=self.meeting.pk).update(
            main_matters="Saved on the meeting page", updated_at=timezone.now()
        )
        response = self.client.post(self.url, self._data(v0))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This meeting was changed after you opened this page")
        self.assertContains(response, "Admin typed this")
        self.assertEqual(self._stored(), "Saved on the meeting page")

    # Catches the admin check refusing a fresh form (or a missing stamp being let through).
    def test_fresh_admin_form_saves_and_missing_stamp_is_refused(self):
        refused = self.client.post(self.url, self._data(""))
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(self._stored(), "Stored")
        fresh = meeting_version(LineMeeting.objects.get(pk=self.meeting.pk))
        self.assertEqual(self.client.post(self.url, self._data(fresh)).status_code, 302)
        self.assertEqual(self._stored(), "Admin typed this")


class PreDeployCreatePageTests(TestCase):
    """A create page opened before the deploy posts the old six-field shape."""

    def setUp(self):
        self.manager_user = make_user("boss@oxlip.test")
        make_staff("boss@oxlip.test")
        self.report = make_staff("report@oxlip.test", line_manager_email="boss@oxlip.test")
        self.url = reverse("line_management:meeting_create", args=[self.report.pk])
        self.carried_text = "Old carried text ✅ “done”"
        self.agreed_text = "Old agreed text\r\nsecond line"
        self.old_shape = {
            "meeting_date": "2026-02-01",
            "actions_from_last_meeting": self.carried_text,
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "Old notes",
            "actions_from_meeting": self.agreed_text,
        }

    # Catches an old-shape create silently dropping the legacy action prose (or creating a half record).
    def test_old_shape_create_is_refused_and_both_legacy_action_texts_handed_back(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(self.url, self.old_shape)
        self.assertEqual(response.status_code, 409)
        for text in (self.carried_text, self.agreed_text, "Old notes"):
            self.assertContains(response, escape(text), status_code=409)
        self.assertFalse(LineMeeting.objects.exists())
        self.assertFalse(MeetingAction.objects.exists())

    # Catches the old-shape hand-back being shown to someone who may not create at all.
    def test_stranger_posting_old_shape_gets_403_not_the_handback(self):
        stranger = make_user("stranger@oxlip.test")
        make_staff("stranger@oxlip.test")
        self.client.force_login(stranger)
        response = self.client.post(self.url, self.old_shape)
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "Old notes", status_code=403)
        self.assertFalse(LineMeeting.objects.exists())


# --- Leg 2, round 2: regressions for the Lookout's admin findings and hand-back wording ---


class _AdminActionFixture:
    """m1 agreed ``self.action``; m2 reviews it and holds its rating and comment."""

    def setUp(self):
        self.super_user = make_user("root@oxlip.test", is_superuser=True)
        person = make_staff("person@oxlip.test")
        self.m1 = make_meeting(person, meeting_date=date(2026, 1, 10))
        self.m2 = make_meeting(person, meeting_date=date(2026, 1, 20))
        self.action = make_action(
            self.m1, "Settled wording", reviewed_in=self.m2, rag="RED", comment="Blocked"
        )
        age_meetings(self.m1, self.m2)
        self.client.force_login(self.super_user)


class AdminInlineRatingReadOnlyTests(_AdminActionFixture, TestCase):
    """C1: the rating belongs to the reviewing meeting; the agreeing meeting's admin
    inline must not be able to write it."""

    def _meeting_admin_post(self, version):
        data = {
            "created_by_email": "",
            "meeting_date": "2026-01-10",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "Admin note on m1",
            "actions_from_meeting": "",
            "meeting_version": version,
            **management_form("agreed_actions", 1, 1),
            "agreed_actions-0-id": str(self.action.pk),
            "agreed_actions-0-agreed_at": str(self.m1.pk),
            "agreed_actions-0-description": "Settled wording",
            # What a form opened before the rating was written would still post.
            "agreed_actions-0-rag": "",
            "agreed_actions-0-review_comment": "",
            "_save": "Save",
        }
        url = reverse("admin:line_management_linemeeting_change", args=[self.m1.pk])
        return self.client.post(url, data)

    # Catches the meeting inline rendering editable rating/comment inputs.
    def test_inline_renders_rating_and_comment_read_only(self):
        url = reverse("admin:line_management_linemeeting_change", args=[self.m1.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="agreed_actions-0-rag"')
        self.assertNotContains(page, 'name="agreed_actions-0-review_comment"')
        self.assertContains(page, "Blocked")

    # Catches a fresh meeting admin save wiping a rating written meanwhile on the reviewing meeting's page.
    def test_meeting_admin_save_keeps_rating_written_on_reviewing_meeting_page(self):
        m1_version = meeting_version(LineMeeting.objects.get(pk=self.m1.pk))
        page = meeting_payload(meeting_date="2026-01-20")
        page.update(carried_rows([(self.action, "GREEN", "Done \U0001f389\r\non the page")]))
        saved = self.client.post(
            reverse("line_management:meeting_save", args=[self.m2.pk]), versioned(page, self.m2)
        )
        self.assertEqual(saved.status_code, 302)

        response = self._meeting_admin_post(m1_version)
        self.assertEqual(response.status_code, 302, "m1's admin form should still be fresh")
        action = MeetingAction.objects.get(pk=self.action.pk)
        self.assertEqual((action.rag, action.review_comment), ("GREEN", "Done \U0001f389\r\non the page"))
        self.assertEqual(
            LineMeeting.objects.values_list("main_matters", flat=True).get(pk=self.m1.pk), "Admin note on m1"
        )


class MeetingActionChangeFormStaleTests(_AdminActionFixture, TestCase):
    """C2: the standalone action admin form carries the action's stamp and refuses when stale."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin:line_management_meetingaction_change", args=[self.action.pk])

    def _post(self, version):
        data = {"rag": "GREEN", "review_comment": "Admin comment", "_save": "Save"}
        if version is not None:
            data["action_version"] = version
        return self.client.post(self.url, data)

    def _stamp(self):
        return MeetingAction.objects.get(pk=self.action.pk).updated_at.isoformat()

    # Catches the action admin form not carrying the action's version at all.
    def test_change_form_carries_hidden_action_version(self):
        page = self.client.get(self.url)
        tag = re.search(r'<input[^>]*name="action_version"[^>]*>', page.content.decode())
        self.assertIsNotNone(tag, "no action_version input on the action admin form")
        self.assertIn('type="hidden"', tag.group(0))
        self.assertIn(f'value="{self._stamp()}"', tag.group(0))

    # Catches a stale action admin form overwriting a rating/comment saved on the meeting page.
    def test_stale_action_version_is_refused_and_db_unchanged(self):
        v0 = self._stamp()
        MeetingAction.objects.filter(pk=self.action.pk).update(
            rag="AMBER", review_comment="Written on the page", updated_at=timezone.now()
        )
        before = snapshot(self.action)
        response = self._post(v0)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This action was changed after you opened this page")
        self.assertContains(response, "Admin comment")
        self.assertEqual(snapshot(self.action), before)

    # Catches a missing stamp being treated as "skip the check".
    def test_missing_action_version_is_refused_and_db_unchanged(self):
        before = snapshot(self.action)
        response = self._post(None)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This action was changed after you opened this page")
        self.assertEqual(snapshot(self.action), before)

    # Catches the check refusing a fresh form, leaving the admin unable to correct a rating.
    def test_fresh_action_version_saves(self):
        response = self._post(self._stamp())
        self.assertEqual(response.status_code, 302)
        action = MeetingAction.objects.get(pk=self.action.pk)
        self.assertEqual((action.rag, action.review_comment), ("GREEN", "Admin comment"))


class HandbackWordingTests(_StalePageMixin, TestCase):
    """What the refused-save page tells the user about each piece of handed-back text."""

    # Catches an already-saved action row and a new typed row being labelled alike, so the
    # user re-adds a saved action as a duplicate (or skips a new one).
    def test_saved_and_new_action_rows_are_labelled_differently(self):
        own = make_action(self.meeting, "Draft the rota")
        payload = self.page("2000-01-01T00:00:00+00:00", main_matters="Stale notes")
        payload.update(agreed_rows(existing=[(own, "Draft the rota by Friday", False)], new=["Brand new action"]))
        response = self.client.post(self.save_url, payload)
        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response, "Action from this meeting 1 (already saved — check for changes)", status_code=409
        )
        self.assertContains(response, "New action from this meeting (row 2)", status_code=409)
        self.assertContains(response, "Draft the rota by Friday", status_code=409)
        self.assertContains(response, "Brand new action", status_code=409)

    # Catches the back link replacing the hand-back page, so the typed text is gone once followed.
    def test_back_link_opens_in_new_tab(self):
        response = self.client.post(
            self.save_url, self.page("2000-01-01T00:00:00+00:00", main_matters="Stale notes")
        )
        self.assertEqual(response.status_code, 409)
        tag = re.search(r'<a[^>]*class="button"[^>]*>', response.content.decode())
        self.assertIsNotNone(tag)
        self.assertIn('target="_blank"', tag.group(0))
        self.assertIn('rel="noopener"', tag.group(0))

    # Catches a double-clicked Save showing "Meeting saved." twice.
    def test_double_clicked_save_shows_saved_message_once(self):
        payload = self.page(self.version(), main_matters="Clicked twice")
        self.assertEqual(self.client.post(self.save_url, payload).status_code, 302)
        response = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count("Meeting saved."), 1)


class PreDeployCreateLabelTests(TestCase):
    """The old-shape create hand-back names each field in the user's terms."""

    # Catches the legacy fields being handed back under raw field names.
    def test_old_shape_handback_uses_readable_labels(self):
        manager = make_user("boss@oxlip.test")
        make_staff("boss@oxlip.test")
        report = make_staff("report@oxlip.test", line_manager_email="boss@oxlip.test")
        self.client.force_login(manager)
        response = self.client.post(
            reverse("line_management:meeting_create", args=[report.pk]),
            {
                "meeting_date": "2026-02-01",
                "actions_from_last_meeting": "Old carried",
                "upcoming": "",
                "rotation_update": "",
                "main_matters": "Old notes",
                "actions_from_meeting": "Old agreed",
            },
        )
        self.assertEqual(response.status_code, 409)
        for label in ("Main matters to discuss", "Actions from the last meeting", "Actions from this meeting"):
            self.assertContains(response, label, status_code=409)
        self.assertNotContains(response, "Actions from last meeting<", status_code=409)


# ---------------------------------------------------------------------------
# Leg 3 of docs/chart/line-meeting-preparation.md: a meeting is started Being
# prepared and marked Held; carry-forward reads Held meetings only.
# ---------------------------------------------------------------------------

from django.db import connection  # noqa: E402
from django.db.migrations.executor import MigrationExecutor  # noqa: E402
from django.test import TransactionTestCase  # noqa: E402

from .services import carried_forward_candidates, carry_forward_source  # noqa: E402

PREPARING = LineMeeting.State.PREPARING
HELD = LineMeeting.State.HELD


def state_of(meeting):
    return LineMeeting.objects.values_list("state", flat=True).get(pk=meeting.pk)


def notes_of(meeting):
    return LineMeeting.objects.values_list("main_matters", flat=True).get(pk=meeting.pk)


class _Leg3Mixin(_ManagerCreateMixin):
    """A manager, a report (with a login) and a stranger; helpers for the meeting page."""

    def setUp(self):
        super().setUp()
        self.report_user = User.objects.get(email="report@oxlip.test")
        self.stranger_user = make_user("stranger@oxlip.test")
        make_staff("stranger@oxlip.test")
        self.super_user = make_user("root@oxlip.test", is_superuser=True)

    def preparing(self, notes="Prepared so far", meeting_date=date(2026, 2, 1)):
        meeting = make_meeting(
            self.report, state=PREPARING, meeting_date=meeting_date, created_by_email=self.manager_email
        )
        LineMeeting.objects.filter(pk=meeting.pk).update(main_matters=notes)
        return meeting

    def save_url(self, meeting):
        return reverse("line_management:meeting_save", args=[meeting.pk])

    def page(self, meeting, *, stamp="current", meeting_date="2026-02-01", hold=False,
             existing=(), new=(), carried=(), **notes):
        """What a meeting page posts. ``stamp="current"`` sends the live version."""
        payload = meeting_payload(meeting_date=meeting_date, **notes)
        payload.update(agreed_rows(existing=existing, new=new))
        payload.update(carried_rows(carried))
        if hold:
            payload["hold"] = "1"
        if stamp == "current":
            return versioned(payload, meeting)
        if stamp is not None:
            payload["meeting_version"] = stamp
        return payload


class HoldAccessControlTests(_Leg3Mixin, TestCase):
    """Gauntlet stage 3: only the current line manager (or super) may hold, and
    nothing returns a Held meeting to Being prepared."""

    # Catches the hold gate being removed: a report marking their own meeting held.
    # (Leg 4: the report may edit a meeting being prepared, so their save lands — but
    # the posted hold is ignored and the meeting stays being prepared.)
    def test_report_posting_hold_on_save_saves_but_stays_preparing(self):
        meeting = self.preparing()
        self.client.force_login(self.report_user)
        response = self.client.post(
            self.save_url(meeting), self.page(meeting, hold=True, main_matters="Report wrote this")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(state_of(meeting), PREPARING)
        self.assertEqual(notes_of(meeting), "Report wrote this")

    # Catches IDOR on the hold: an unrelated user holding a meeting by guessing its pk.
    def test_stranger_cannot_mark_meeting_held(self):
        meeting = self.preparing()
        self.client.force_login(self.stranger_user)
        response = self.client.post(
            self.save_url(meeting), self.page(meeting, hold=True, main_matters="Stranger wrote this")
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "Prepared so far", status_code=403)
        self.assertEqual(state_of(meeting), PREPARING)
        self.assertEqual(notes_of(meeting), "Prepared so far")

    # Catches an un-hold: a crafted ``state`` on the page or the admin moving a Held
    # meeting back to Being prepared (a successor may already have pinned its actions).
    def test_held_meeting_cannot_be_returned_to_preparing_by_posting_state(self):
        held = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        LineMeeting.objects.filter(pk=held.pk).update(main_matters="Held notes")

        for extra in ({"state": "PREPARING"}, {"state": "PREPARING", "hold": "1"}):
            with self.subTest(extra=extra):
                payload = self.page(held, meeting_date="2026-01-10", main_matters="Edited after held")
                payload.update(extra)
                response = self.client.post(self.save_url(held), payload)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(state_of(held), HELD)
                self.assertEqual(notes_of(held), "Edited after held")

        self.client.force_login(self.super_user)
        admin_url = reverse("admin:line_management_linemeeting_change", args=[held.pk])
        response = self.client.post(admin_url, {
            "created_by_email": "",
            "meeting_date": "2026-01-10",
            "state": "PREPARING",
            "actions_from_last_meeting": "",
            "upcoming": "",
            "rotation_update": "",
            "main_matters": "Edited in admin",
            "actions_from_meeting": "",
            "meeting_version": meeting_version(LineMeeting.objects.get(pk=held.pk)),
            **management_form("agreed_actions"),
            "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(notes_of(held), "Edited in admin")
        self.assertEqual(state_of(held), HELD)

    # Catches meeting_new's redirect running before the chokepoint, which would hand a
    # stranger (or the report) the pk of someone's meeting being prepared.
    def test_stranger_meeting_new_is_403_even_when_a_meeting_is_being_prepared(self):
        meeting = self.preparing()
        url = reverse("line_management:meeting_new", args=[self.report.pk])
        for user in (self.stranger_user, self.report_user):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(
                    reverse("line_management:meeting_detail", args=[meeting.pk]),
                    response.get("Location", ""),
                )

    # Catches the "already being prepared" hand-back echoing the other meeting's
    # notes or actions instead of only what this page posted.
    def test_refused_create_while_preparing_does_not_show_other_meetings_text(self):
        other = self.preparing(notes="Private preparing notes")
        make_action(other, "Private preparing action")
        response = self.create(main_matters="My newly typed notes", agreed=["My new action"])
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "My newly typed notes", status_code=409)
        self.assertContains(response, "My new action", status_code=409)
        self.assertNotContains(response, "Private preparing notes", status_code=409)
        self.assertNotContains(response, "Private preparing action", status_code=409)
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)


class StateMigrationTests(TransactionTestCase):
    """0003 adds ``state``: every meeting that existed before it was held."""

    before = [("line_management", "0002_meetingaction")]
    after = [("line_management", "0003_linemeeting_state")]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    # Catches 0003 putting existing meetings into "Being prepared" (and the
    # one-preparing constraint then aborting the deploy for anyone with two).
    def test_existing_meetings_migrate_to_held_not_preparing(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        old_apps = executor.loader.project_state(self.before).apps
        Staff = old_apps.get_model("core", "StaffMember")
        OldMeeting = old_apps.get_model("line_management", "LineMeeting")
        person = Staff.objects.create(email="person@oxlip.test")
        text = "Long notes — “quoted” café ✅\r\nsecond line \U0001f600"
        first = OldMeeting.objects.create(staff=person, meeting_date=date(2026, 1, 10), main_matters=text)
        second = OldMeeting.objects.create(staff=person, meeting_date=date(2026, 2, 1), upcoming="Next")

        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        NewMeeting = executor.loader.project_state(self.after).apps.get_model("line_management", "LineMeeting")

        rows = {m.pk: m for m in NewMeeting.objects.all()}
        self.assertEqual(rows[first.pk].state, "HELD")
        self.assertEqual(rows[second.pk].state, "HELD")
        self.assertEqual(rows[first.pk].main_matters, text)
        self.assertEqual(rows[second.pk].upcoming, "Next")


class PreparingConstraintTests(TestCase):
    """The database itself allows one meeting Being prepared per report."""

    # Catches the partial unique constraint missing: two meetings being prepared for one report.
    def test_second_preparing_meeting_for_same_report_is_refused_by_database(self):
        report = make_staff("report@oxlip.test")
        make_meeting(report, state=PREPARING)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_meeting(report, state=PREPARING, meeting_date=date(2026, 3, 1))
        # Held meetings, and another person's meeting being prepared, are not limited.
        make_meeting(report, meeting_date=date(2026, 3, 1))
        make_meeting(make_staff("other@oxlip.test"), state=PREPARING)
        self.assertEqual(LineMeeting.objects.filter(staff=report).count(), 2)

    # Catches an app writer that omits ``state`` (the previous release, mid-deploy)
    # inserting a meeting as Being prepared rather than Held.
    def test_insert_omitting_state_is_held(self):
        report = make_staff("report@oxlip.test")
        make_meeting(report, state=PREPARING)
        now = connection.ops.adapt_datetimefield_value(timezone.now())
        table = connection.ops.quote_name(LineMeeting._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} (staff_id, created_by_email, meeting_date, "
                "actions_from_last_meeting, upcoming, rotation_update, main_matters, "
                "actions_from_meeting, created_at, updated_at) "
                "VALUES (%s, '', %s, '', '', '', %s, '', %s, %s)",
                [report.pk, connection.ops.adapt_datefield_value(date(2026, 3, 1)), "Old release", now, now],
            )
        inserted = LineMeeting.objects.get(staff=report, main_matters="Old release")
        self.assertEqual(inserted.state, HELD)


class PreparingWorkflowTests(_Leg3Mixin, TestCase):
    """Create, carry-forward and hold for a meeting Being prepared."""

    # Catches carry-forward reading a meeting still being prepared: its actions
    # would be pinned before the meeting took place, and the held one's skipped.
    def test_carry_forward_ignores_meeting_being_prepared(self):
        m1 = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(m1, "A")
        self.assertEqual(
            self.create("2026-02-01", carried=[(a, "GREEN", "Done")], agreed=["B"], main_matters="Prep").status_code,
            302,
        )
        m2 = self.newest()
        b = MeetingAction.objects.get(description="B")
        self.assertEqual(m2.state, PREPARING)
        self.assertEqual(carry_forward_source(self.report), m1)
        self.assertEqual(list(carried_forward_candidates(carry_forward_source(self.report))), [])

        response = self.client.post(self.save_url(m2), self.page(
            m2, hold=True, main_matters="Prep", existing=[(b, "B", False)], carried=[(a, "GREEN", "Done")],
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(carry_forward_source(self.report), m2)
        self.assertEqual(list(carried_forward_candidates(m2)), [b])

    # Catches _bind crashing (or refusing new actions) on a report whose only
    # meeting is being prepared, with no Held meeting to be "the latest".
    def test_can_add_actions_on_first_meeting_being_prepared_with_no_held_meeting(self):
        self.assertEqual(self.create(agreed=["First action"], main_matters="First").status_code, 302)
        meeting = self.newest()
        self.assertEqual(meeting.state, PREPARING)
        self.assertEqual(self.client.get(reverse("line_management:meeting_detail", args=[meeting.pk])).status_code, 200)

        first = MeetingAction.objects.get(description="First action")
        response = self.client.post(self.save_url(meeting), self.page(
            meeting, main_matters="First", existing=[(first, "First action", False)], new=["Second action"],
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(meeting.agreed_actions.order_by("pk").values_list("description", flat=True)),
            ["First action", "Second action"],
        )

    # Catches "New meeting" offering a blank form while one is already being prepared.
    def test_new_meeting_redirects_to_meeting_being_prepared(self):
        meeting = self.preparing()
        response = self.client.get(reverse("line_management:meeting_new", args=[self.report.pk]))
        self.assertRedirects(response, reverse("line_management:meeting_detail", args=[meeting.pk]))
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)

    # Catches a create from a stale "New meeting" page losing its text (or a 500 from
    # the constraint) when another meeting is already being prepared.
    def test_create_while_one_is_being_prepared_hands_text_back_409(self):
        meeting = self.preparing()
        notes = "Typed while the other was started — “careful” café\r\nline two \U0001f600"
        response = self.create(main_matters=notes, agreed=["Action ✅ to keep"])
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, escape(notes), status_code=409)
        self.assertContains(response, escape("Action ✅ to keep"), status_code=409)
        self.assertContains(
            response, reverse("line_management:meeting_detail", args=[meeting.pk]), status_code=409
        )
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)
        self.assertFalse(MeetingAction.objects.exists())
        self.assertEqual(notes_of(meeting), "Prepared so far")

    # Catches the second click of a double-clicked "Save" being answered with the
    # "already being prepared" 409 instead of folding into the meeting it made.
    def test_double_click_create_being_prepared_folds_not_409(self):
        first = self.create(main_matters="Clicked twice", agreed=["Once"])
        second = self.create(main_matters="Clicked twice", agreed=["Once"])
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(first["Location"], second["Location"])
        meeting = LineMeeting.objects.get(staff=self.report)
        self.assertEqual(meeting.state, PREPARING)
        self.assertEqual(MeetingAction.objects.count(), 1)

    # Catches a double-clicked "Save and mark as held" creating two Held meetings.
    def test_double_click_save_and_hold_create_folds(self):
        first = self.create(main_matters="Held twice", hold=True)
        second = self.create(main_matters="Held twice", hold=True)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(first["Location"], second["Location"])
        meeting = LineMeeting.objects.get(staff=self.report)
        self.assertEqual(meeting.state, HELD)

    # Catches a back-dated meeting taking the one Being-prepared slot (it can review
    # nothing), or the refusal discarding what was typed.
    def test_back_dated_create_cannot_be_left_being_prepared(self):
        make_meeting(self.report, meeting_date=date(2026, 1, 10))
        notes = "Back-dated notes — “kept” ✅"
        response = self.create("2026-01-01", main_matters=notes)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "before the last held meeting")
        self.assertContains(response, escape(notes))
        self.assertFalse(LineMeeting.objects.filter(staff=self.report, state=PREPARING).exists())
        self.assertEqual(LineMeeting.objects.filter(staff=self.report).count(), 1)

    # Catches "Save and mark as held" holding without saving the notes, or altering them.
    def test_save_and_hold_marks_held_and_keeps_notes(self):
        meeting = self.preparing()
        notes = "Final notes — “quoted” café naïve\r\n\r\nSecond paragraph \U0001f389"
        response = self.client.post(self.save_url(meeting), self.page(meeting, hold=True, main_matters=notes),
                                    follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Meeting saved and marked as held.")
        self.assertEqual(state_of(meeting), HELD)
        self.assertEqual(notes_of(meeting), notes)

        # Re-saved unchanged from the rendered page, the text is still identical.
        resave = self.client.post(self.save_url(meeting), self.page(meeting, main_matters=notes))
        self.assertEqual(resave.status_code, 302)
        self.assertEqual(notes_of(meeting), notes)

    # Catches a stale page holding a meeting (bypassing the version check), or the
    # refusal not saying the hold did not happen.
    def test_stale_page_hold_is_refused_and_text_handed_back(self):
        meeting = self.preparing()
        age_meetings(meeting)
        v0 = meeting_version(LineMeeting.objects.get(pk=meeting.pk))
        self.assertEqual(
            self.client.post(self.save_url(meeting), self.page(meeting, stamp=v0, main_matters="Tab one")).status_code,
            302,
        )
        before = db_state()
        typed = "Tab two — “mine” \U0001f600"
        response = self.client.post(self.save_url(meeting), self.page(meeting, stamp=v0, hold=True, main_matters=typed))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "It was not marked as held.", status_code=409)
        self.assertContains(response, escape(typed), status_code=409)
        self.assertEqual(db_state(), before)
        self.assertEqual(state_of(meeting), PREPARING)

    # Catches a stale "Save and mark as held" of an unchanged page being folded as a
    # repeat — answered "saved" while the meeting is never held.
    def test_stale_hold_of_unchanged_page_is_not_folded_while_still_preparing(self):
        meeting = self.preparing(notes="Unchanged")
        age_meetings(meeting)
        v0 = meeting_version(LineMeeting.objects.get(pk=meeting.pk))
        LineMeeting.objects.filter(pk=meeting.pk).update(updated_at=timezone.now())

        response = self.client.post(self.save_url(meeting), self.page(meeting, stamp=v0, hold=True, main_matters="Unchanged"))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "It was not marked as held.", status_code=409)
        self.assertEqual(state_of(meeting), PREPARING)

        # Without the hold, the same stale unchanged page is a harmless repeat.
        plain = self.client.post(self.save_url(meeting), self.page(meeting, stamp=v0, main_matters="Unchanged"))
        self.assertEqual(plain.status_code, 302)

    # Catches a meeting with nothing in it being marked held — and the refused
    # hold still saving the cleared fields.
    def test_blank_meeting_cannot_be_marked_held(self):
        meeting = self.preparing(notes="Only note")
        response = self.client.post(self.save_url(meeting), self.page(meeting, hold=True, main_matters=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "blank meeting can")
        self.assertEqual(state_of(meeting), PREPARING)
        self.assertEqual(notes_of(meeting), "Only note")

    # Catches holding locking the meeting: the manager must still be able to edit it.
    def test_held_meeting_still_editable_by_manager(self):
        meeting = self.preparing()
        self.assertEqual(
            self.client.post(self.save_url(meeting), self.page(meeting, hold=True, main_matters="At the meeting")).status_code,
            302,
        )
        detail = self.client.get(reverse("line_management:meeting_detail", args=[meeting.pk]))
        self.assertTrue(detail.context["can_edit"])
        self.assertFalse(detail.context["can_hold"])
        response = self.client.post(self.save_url(meeting), self.page(meeting, main_matters="Corrected afterwards"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(notes_of(meeting), "Corrected afterwards")
        self.assertEqual(state_of(meeting), HELD)

    # Catches an IntegrityError on create with no meeting being prepared (some other
    # race or constraint) becoming a 500 that loses the typed text.
    def test_create_integrity_error_without_preparing_meeting_hands_text_back(self):
        def insert_then_clash(meeting, **kwargs):
            meeting.save()
            raise IntegrityError("simulated constraint clash")

        notes = "Typed during a race — “keep me” \U0001f600"
        with mock.patch("line_management.views.start_meeting", side_effect=insert_then_clash):
            response = self.create(main_matters=notes, agreed=["Raced action"])
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, escape(notes), status_code=409)
        self.assertContains(response, "Raced action", status_code=409)
        self.assertFalse(LineMeeting.objects.exists())
        self.assertFalse(MeetingAction.objects.exists())


class PurgePreparingTests(TestCase):
    """purge_empty_line_meetings deletes legacy empty Held meetings only."""

    # Catches the purge deleting a blank meeting being prepared (work in progress).
    def test_purge_never_deletes_a_meeting_being_prepared(self):
        report = make_staff("report@oxlip.test")
        held_empty = make_meeting(report, meeting_date=date(2026, 1, 10))
        preparing_empty = make_meeting(report, state=PREPARING, meeting_date=date(2026, 2, 1))
        call_command("purge_empty_line_meetings")
        self.assertFalse(LineMeeting.objects.filter(pk=held_empty.pk).exists())
        self.assertTrue(LineMeeting.objects.filter(pk=preparing_empty.pk).exists())


# ---------------------------------------------------------------------------
# Leg 4 of docs/chart/line-meeting-preparation.md: the report prepares their own
# next meeting while it is Being prepared. Access control first (Gauntlet stage 3).
# ---------------------------------------------------------------------------

from django.conf import settings  # noqa: E402
from django.shortcuts import resolve_url  # noqa: E402

from .permissions import SCOPE_PREPARE  # noqa: E402
from .permissions import get_meeting_or_403 as real_get_meeting_or_403  # noqa: E402

# Text that has to survive every path untouched: curly quotes, accents, emoji, CRLF.
AWKWARD = "Café naïve — “quoted” ‘single’ \U0001f600 ✅\r\nSecond line"


class _Leg4Mixin(_Leg3Mixin):
    """Leg 3's cast (manager, report, stranger, superuser), signed in as the report."""

    def setUp(self):
        super().setUp()
        self.prepare_new_url = reverse("line_management:prepare_new")
        self.prepare_create_url = reverse("line_management:prepare_create")
        self.my_meetings_url = reverse("line_management:my_meetings")
        self.staff_meetings_url = reverse("line_management:staff_meetings", args=[self.report.pk])
        self.client.force_login(self.report_user)

    def prepare(self, meeting_date="2026-02-01", carried=(), agreed=(), hold=False, url=None, **notes):
        """POST the report's "prepare your next meeting" page."""
        payload = meeting_payload(meeting_date=meeting_date, **notes)
        payload.update(carried_rows(carried))
        payload.update(agreed_rows(new=agreed))
        if hold:
            payload["hold"] = "1"
        return self.client.post(url or self.prepare_create_url, payload)

    def detail_url(self, meeting):
        return reverse("line_management:meeting_detail", args=[meeting.pk])

    def back_link(self, url):
        # The hand-back page's own "open ..." button (the nav has plain links only).
        return f'href="{url}" target="_blank"'


class ReportPrepareAccessTests(_Leg4Mixin, TestCase):
    """Gauntlet stage 3: the prepare views act only on the viewer's own record,
    and preparing widens nothing else the report can reach."""

    # Catches the prepare views missing @login_required (an anonymous POST creating a meeting).
    def test_anonymous_prepare_redirects_to_login(self):
        self.client.logout()
        login = resolve_url(settings.LOGIN_URL)
        get = self.client.get(self.prepare_new_url)
        post = self.prepare(main_matters="Anonymous")
        for response in (get, post):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(login), response["Location"])
        self.assertFalse(LineMeeting.objects.exists())

    # Catches a user with no StaffMember (even a superuser) reaching the prepare
    # workflow and creating a meeting attached to nobody, or to someone else.
    def test_user_without_staff_member_gets_403_on_prepare_new_and_create(self):
        for user in (make_user("nostaff@oxlip.test"), self.super_user):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.prepare_new_url).status_code, 403)
                self.assertEqual(self.prepare(main_matters="No staff record").status_code, 403)
        self.assertFalse(LineMeeting.objects.exists())

    # Catches someone with no line manager starting a meeting nobody could ever hold.
    # (The create POST is the "lost their line manager while typing" 409 hand-back by
    # design — it cannot tell the two apart — so it refuses without a 403.)
    def test_staff_with_no_line_manager_gets_403_on_prepare_new_and_nothing_saved_on_create(self):
        self.client.force_login(self.manager_user)  # boss@ has no line manager
        self.assertEqual(self.client.get(self.prepare_new_url).status_code, 403)
        response = self.prepare(main_matters="Nobody can hold this")
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Nobody can hold this", status_code=409)
        self.assertFalse(LineMeeting.objects.exists())

    # Catches a self-managed record (line_manager_email == own email, any case)
    # letting someone prepare a meeting only a superuser could ever hold.
    def test_staff_whose_line_manager_is_themselves_cannot_prepare(self):
        user = make_user("self@oxlip.test")
        own = make_staff("self@oxlip.test")
        StaffMember.objects.filter(pk=own.pk).update(line_manager_email="Self@OXLIP.test")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.prepare_new_url).status_code, 403)
        self.assertNotEqual(self.prepare(main_matters="Self-managed").status_code, 302)
        self.assertFalse(LineMeeting.objects.filter(staff=own).exists())
        self.assertNotContains(self.client.get(self.my_meetings_url), f'href="{self.prepare_new_url}"')

    # Catches IDOR on the prepare views: a posted or query-string staff id choosing
    # whose record the meeting is created on.
    def test_prepare_create_ignores_posted_staff_and_creates_for_viewer_only(self):
        other = make_staff("other@oxlip.test", line_manager_email=self.manager_email)
        url = f"{self.prepare_create_url}?staff={other.pk}&staff_pk={other.pk}"
        response = self.prepare(
            url=url, staff=str(other.pk), staff_pk=str(other.pk), main_matters="Mine only"
        )
        self.assertEqual(response.status_code, 302)
        meeting = LineMeeting.objects.get(main_matters="Mine only")
        self.assertEqual(meeting.staff, self.report)
        self.assertEqual(meeting.created_by_email, "report@oxlip.test")
        self.assertFalse(LineMeeting.objects.filter(staff=other).exists())

    # Catches leg 4 widening the manager-only views: the report using the manager's
    # start/list URLs on their own record (which would allow holding on create).
    def test_report_still_403_on_manager_new_and_create_urls(self):
        self.assertEqual(
            self.client.get(reverse("line_management:meeting_new", args=[self.report.pk])).status_code, 403
        )
        self.assertEqual(self.create(main_matters="Via the manager URL", hold=True).status_code, 403)
        self.assertEqual(self.client.get(self.staff_meetings_url).status_code, 403)
        self.assertFalse(LineMeeting.objects.exists())

    # Catches the prepare scope leaking across reports: one report reading or writing
    # a colleague's meeting being prepared by guessing its pk.
    def test_report_cannot_open_or_save_another_reports_meeting_being_prepared(self):
        other = make_staff("other@oxlip.test", line_manager_email=self.manager_email)
        theirs = make_meeting(other, state=PREPARING, meeting_date=date(2026, 2, 1))
        LineMeeting.objects.filter(pk=theirs.pk).update(main_matters="Other report's private prep")
        before = db_state()

        response = self.client.get(self.detail_url(theirs))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "private prep", status_code=403)
        response = self.client.post(self.save_url(theirs), self.page(theirs, main_matters="Overwritten"))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "private prep", status_code=403)
        self.assertEqual(db_state(), before)

    # Catches a crafted carried-action id letting the report rate (or read the wording
    # of) another person's action, on the prepare create and on a save.
    def test_report_crafted_carried_id_cannot_rate_another_persons_action(self):
        own_held = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        own = make_action(own_held, "Own action")
        other = make_staff("other@oxlip.test", line_manager_email=self.manager_email)
        other_held = make_meeting(other, meeting_date=date(2026, 1, 10))
        other_prep = make_meeting(other, state=PREPARING, meeting_date=date(2026, 2, 1))
        foreign = make_action(other_held, "Other person's secret action", reviewed_in=other_prep)
        foreign_before = snapshot(foreign)

        for carried in ([(own, "", ""), (foreign, "RED", "crafted")], [(foreign, "RED", "crafted")]):
            with self.subTest(create=len(carried)):
                response = self.prepare(carried=carried, main_matters="Prep")
                self.assertNotEqual(response.status_code, 302)
                self.assertNotContains(response, "secret action", status_code=response.status_code)
        self.assertFalse(LineMeeting.objects.filter(staff=self.report, state=PREPARING).exists())

        mine = make_meeting(self.report, state=PREPARING, meeting_date=date(2026, 2, 1))
        MeetingAction.objects.filter(pk=own.pk).update(reviewed_in=mine)
        own_before = snapshot(own)
        response = self.client.post(
            self.save_url(mine), self.page(mine, main_matters="Prep", carried=[(foreign, "RED", "crafted")])
        )
        self.assertNotEqual(response.status_code, 302)
        self.assertNotContains(response, "secret action", status_code=response.status_code)
        self.assertEqual(snapshot(foreign), foreign_before)
        self.assertEqual(snapshot(own), own_before)
        self.assertEqual(notes_of(mine), "")

    # Catches the REPORT_FIELDS allowlist failing: the report writing the line
    # manager's Rotation update by posting it, on create and on save.
    def test_report_crafted_rotation_update_is_ignored_on_create_and_save(self):
        response = self.prepare(main_matters="Prep", rotation_update="Report crafted rotation")
        self.assertEqual(response.status_code, 302)
        meeting = self.newest()
        self.assertEqual(meeting.rotation_update, "")

        LineMeeting.objects.filter(pk=meeting.pk).update(rotation_update="Manager's rotation")
        response = self.client.post(
            self.save_url(meeting),
            self.page(meeting, main_matters="Prep 2", rotation_update="Crafted again"),
        )
        self.assertEqual(response.status_code, 302)
        meeting.refresh_from_db()
        self.assertEqual(meeting.rotation_update, "Manager's rotation")
        self.assertEqual(meeting.main_matters, "Prep 2")

    # Catches the report's save clearing the manager's Rotation update — a browser
    # posts no key for a disabled field, so a full-scope bind would read it as blank.
    def test_report_save_keeps_managers_rotation_update_intact(self):
        meeting = self.preparing()
        rotation = "Manager’s R2 notes — “staffing” café\r\nline two \U0001f600"
        LineMeeting.objects.filter(pk=meeting.pk).update(rotation_update=rotation)
        for i, posted in enumerate(("<absent>", "Crafted by report", "")):
            with self.subTest(posted=posted):
                payload = self.page(meeting, main_matters=f"Report case {i}")
                if posted == "<absent>":
                    del payload["rotation_update"]
                else:
                    payload["rotation_update"] = posted
                self.assertEqual(self.client.post(self.save_url(meeting), payload).status_code, 302)
                meeting.refresh_from_db()
                self.assertEqual(meeting.rotation_update, rotation)
                self.assertEqual(meeting.main_matters, f"Report case {i}")

    # Catches the hold gate missing on the shared create path: the report creating
    # their own meeting already Held by posting "hold".
    def test_report_posting_hold_on_create_creates_preparing_not_held(self):
        response = self.prepare(main_matters="Prep with a crafted hold", hold=True)
        self.assertEqual(response.status_code, 302)
        meeting = LineMeeting.objects.get(staff=self.report)
        self.assertEqual(meeting.state, PREPARING)
        self.assertEqual(meeting.main_matters, "Prep with a crafted hold")


class ReportHeldBoundaryTests(_Leg4Mixin, TestCase):
    """Once Held, the record is read-only to the report — however the save races."""

    # Catches a report's save landing on a meeting the manager held while they typed,
    # or the refusal discarding their text.
    def test_report_save_after_meeting_held_gets_409_handback_and_nothing_written(self):
        meeting = self.preparing()
        age_meetings(meeting)
        v0 = meeting_version(LineMeeting.objects.get(pk=meeting.pk))
        self.client.force_login(self.manager_user)
        self.assertEqual(
            self.client.post(self.save_url(meeting), self.page(meeting, hold=True, main_matters="Held")).status_code,
            302,
        )
        before = db_state()

        self.client.force_login(self.report_user)
        response = self.client.post(
            self.save_url(meeting), self.page(meeting, stamp=v0, main_matters=AWKWARD, new=["Late action"])
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "This meeting has been marked as held", status_code=409)
        self.assertContains(response, escape(AWKWARD), status_code=409)
        self.assertContains(response, "Late action", status_code=409)
        self.assertContains(response, self.back_link(self.detail_url(meeting)), status_code=409)
        self.assertNotContains(response, self.staff_meetings_url, status_code=409)
        self.assertEqual(db_state(), before)

    # Catches the report's write resting only on the version check: a Held meeting at
    # an unchanged version must still refuse the report's conditional UPDATE.
    def test_report_save_racing_hold_cannot_write_to_held_meeting(self):
        prepared = self.preparing()
        age_meetings(prepared)
        meeting = LineMeeting.objects.get(pk=prepared.pk)
        data = self.page(meeting, stamp=None, main_matters="Racing report text", new=["Racing action"])
        forms = _bind(meeting, meeting.reviewed_actions.all(), scope=SCOPE_PREPARE, data=data)
        for f in forms:
            self.assertTrue(f.is_valid(), getattr(f, "errors", None))
        LineMeeting.objects.filter(pk=meeting.pk).update(state=HELD)  # held, version not advanced
        before = db_state()
        with self.assertRaises(MeetingChanged):
            save_meeting_page(meeting, meeting.updated_at, *forms, as_report=True)
        self.assertEqual(db_state(), before)

    # Catches the view answering that raced refusal with the generic stale page (or a
    # 500) instead of the held hand-back with the report's text.
    def test_report_save_when_held_between_check_and_update_gets_held_handback(self):
        meeting = self.preparing()
        age_meetings(meeting)
        stamp = stamp_of(meeting)

        def load_then_hold(request, pk):
            result = real_get_meeting_or_403(request, pk)
            LineMeeting.objects.filter(pk=pk).update(state=HELD)  # no version advance
            return result

        payload = self.page(meeting, main_matters=AWKWARD, new=["Raced action"])
        with mock.patch("line_management.views.get_meeting_or_403", side_effect=load_then_hold):
            response = self.client.post(self.save_url(meeting), payload)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "This meeting has been marked as held", status_code=409)
        self.assertContains(response, escape(AWKWARD), status_code=409)
        self.assertContains(response, "Raced action", status_code=409)
        self.assertEqual(notes_of(meeting), "Prepared so far")
        self.assertFalse(MeetingAction.objects.filter(description="Raced action").exists())
        self.assertEqual(stamp_of(meeting), stamp)

    # Catches a Held meeting rendering editable to the report (or carrying a version
    # stamp that would let a crafted save through the version check).
    def test_report_page_on_held_meeting_has_no_version_stamp_and_disabled_fields(self):
        held = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        LineMeeting.objects.filter(pk=held.pk).update(main_matters="Held notes")
        make_action(held, "Agreed at the held meeting")
        earlier = make_meeting(self.report, meeting_date=date(2025, 12, 1))
        make_action(earlier, "Reviewed at the held meeting", reviewed_in=held, rag="GREEN")
        response = self.client.get(self.detail_url(held))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="meeting_version"')
        self.assertNotContains(response, '<button type="submit"')
        self.assertFalse(response.context["can_edit"])
        self.assertFalse(response.context["can_hold"])
        self.assertFalse(response.context["is_report_preparing"])
        self.assertTrue(all(f.disabled for f in response.context["form"].fields.values()))
        agreed = response.context["agreed_formset"]
        self.assertEqual(len(agreed.forms), 1)  # no blank rows to add actions
        for f in agreed.forms:
            self.assertNotIn("DELETE", f.fields)
            # "id"/"agreed_at" are the formset's hidden keys; the FK is forced from the parent.
            self.assertTrue(f.fields["description"].disabled)
        self.assertEqual(len(response.context["carried_formset"].forms), 1)
        self.assertTrue(all(
            fld.disabled for f in response.context["carried_formset"].forms for fld in
            (f.fields["rag"], f.fields["review_comment"])
        ))


class ReportPrepareInheritanceTests(_Leg4Mixin, TestCase):
    """The live line-manager rule holds for a meeting the report started."""

    def _report_prepares(self):
        self.assertEqual(self.prepare(main_matters="Report's prep", agreed=["Report's action"]).status_code, 302)
        return self.newest()

    def _repoint(self, new_manager_email):
        StaffMember.objects.filter(pk=self.report.pk).update(line_manager_email=new_manager_email)

    # Catches a report-started meeting being stranded when the line manager changes:
    # the successor must be able to complete and hold it.
    def test_successor_manager_inherits_meeting_being_prepared_by_report(self):
        meeting = self._report_prepares()
        action = MeetingAction.objects.get(description="Report's action")
        successor = make_user("successor@oxlip.test")
        make_staff("successor@oxlip.test")
        self._repoint("successor@oxlip.test")

        self.client.force_login(successor)
        detail = self.client.get(self.detail_url(meeting))
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.context["can_edit"])
        self.assertTrue(detail.context["can_hold"])
        response = self.client.post(self.save_url(meeting), self.page(
            meeting, hold=True, main_matters="Report's prep", rotation_update="Successor's rotation",
            existing=[(action, "Report's action", False)],
        ))
        self.assertEqual(response.status_code, 302)
        meeting.refresh_from_db()
        self.assertEqual(meeting.state, HELD)
        self.assertEqual(meeting.main_matters, "Report's prep")
        self.assertEqual(meeting.rotation_update, "Successor's rotation")
        self.assertEqual(meeting.created_by_email, "report@oxlip.test")

    # Catches the outgoing manager keeping access to a meeting the report is preparing.
    def test_outgoing_manager_gets_403_on_report_prepared_meeting(self):
        meeting = self._report_prepares()
        make_staff("successor@oxlip.test")
        self._repoint("successor@oxlip.test")
        before = db_state()

        self.client.force_login(self.manager_user)
        response = self.client.get(self.detail_url(meeting))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "Report's prep", status_code=403)
        response = self.client.post(
            self.save_url(meeting), self.page(meeting, hold=True, main_matters="Outgoing overwrite")
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(db_state(), before)


class ReportPrepareWorkflowTests(_Leg4Mixin, TestCase):
    """The report's create/edit flow produces the right records and loses nothing."""

    # Catches the report's create skipping carry-forward (or pinning from the wrong
    # meeting), so last time's actions are never reviewed.
    def test_report_prepare_pins_last_held_meetings_actions(self):
        held = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a, b = make_action(held, "Chase supplier A"), make_action(held, "Chase supplier B")
        page = self.client.get(self.prepare_new_url)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["is_report_preparing"])
        self.assertContains(page, "Chase supplier A")
        self.assertEqual(LineMeeting.objects.count(), 1)  # GET persists nothing

        comment = "Started — “half” done"
        response = self.prepare(carried=[(a, "AMBER", comment), (b, "", "")], main_matters="Report prep")
        self.assertEqual(response.status_code, 302)
        meeting = self.newest()
        self.assertEqual(meeting.state, PREPARING)
        self.assertEqual(meeting.created_by_email, "report@oxlip.test")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.reviewed_in_id, a.rag, a.review_comment), (meeting.pk, "AMBER", comment))
        self.assertEqual((b.reviewed_in_id, b.rag), (meeting.pk, ""))

    # Catches the manager's page dropping (or a save overwriting) the ratings and notes
    # the report prepared, through to the hold.
    def test_report_ratings_survive_manager_save_and_hold(self):
        held = make_meeting(self.report, meeting_date=date(2026, 1, 10))
        a = make_action(held, "Chase supplier")
        comment = "Report’s view — “nearly” \U0001f600"
        self.prepare(carried=[(a, "AMBER", comment)], agreed=["Report's action"], main_matters="Report prep")
        meeting = self.newest()
        report_action = MeetingAction.objects.get(description="Report's action")

        self.client.force_login(self.manager_user)
        detail = self.client.get(self.detail_url(meeting))
        carried_form = detail.context["carried_formset"].forms[0]
        self.assertEqual((carried_form.initial["rag"], carried_form.initial["review_comment"]), ("AMBER", comment))
        self.assertEqual(detail.context["form"].initial["main_matters"], "Report prep")

        response = self.client.post(self.save_url(meeting), self.page(
            meeting, hold=True, main_matters="Report prep", rotation_update="R1 update",
            existing=[(report_action, "Report's action", False)], carried=[(a, "AMBER", comment)],
        ))
        self.assertEqual(response.status_code, 302)
        meeting.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual(meeting.state, HELD)
        self.assertEqual((meeting.main_matters, meeting.rotation_update), ("Report prep", "R1 update"))
        self.assertEqual((a.rag, a.review_comment), ("AMBER", comment))
        self.assertTrue(MeetingAction.objects.filter(pk=report_action.pk, agreed_at=meeting).exists())

    # Catches the report's action rights being narrower than agreed: they may reword
    # or delete any action agreed at the meeting being prepared, the manager's too.
    def test_report_can_reword_and_delete_actions_agreed_at_meeting_being_prepared(self):
        meeting = self.preparing()
        by_manager = make_action(meeting, "Manager typed this")
        by_report = make_action(meeting, "Report typed this")
        response = self.client.post(self.save_url(meeting), self.page(
            meeting, main_matters="Prepared so far",
            existing=[(by_manager, "Manager typed this — clarified", False), (by_report, "Report typed this", True)],
            new=["Report's new action"],
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(meeting.agreed_actions.order_by("pk").values_list("description", flat=True)),
            ["Manager typed this — clarified", "Report's new action"],
        )
        self.assertFalse(MeetingAction.objects.filter(pk=by_report.pk).exists())

    # Catches a back-dated report prepare taking the being-prepared slot, or the
    # refusal discarding what was typed.
    def test_report_back_dated_prepare_is_refused_with_text_kept(self):
        make_meeting(self.report, meeting_date=date(2026, 1, 10))
        response = self.prepare("2026-01-01", main_matters=AWKWARD, agreed=["Back-dated action ✅"])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "before your last held meeting")
        self.assertContains(response, escape(AWKWARD))
        self.assertContains(response, escape("Back-dated action ✅"))
        self.assertFalse(LineMeeting.objects.filter(state=PREPARING).exists())
        self.assertEqual(LineMeeting.objects.count(), 1)

    # Catches "Prepare your next meeting" offering a blank form while one is already
    # being prepared (the report's create would then be refused).
    def test_prepare_new_redirects_to_meeting_already_being_prepared(self):
        meeting = self.preparing()
        response = self.client.get(self.prepare_new_url)
        self.assertRedirects(response, self.detail_url(meeting))
        self.assertTrue(self.client.get(self.detail_url(meeting)).context["is_report_preparing"])
        self.assertEqual(LineMeeting.objects.count(), 1)

    # Catches the report's refused create losing their text, leaking the other
    # meeting's notes, or linking them to the manager-only staff list (a 403).
    def test_report_create_while_manager_preparing_hands_text_back_409_with_report_links(self):
        self.assertEqual(self.client.get(self.prepare_new_url).status_code, 200)
        meeting = self.preparing()  # the manager starts one while the report types
        response = self.prepare(main_matters=AWKWARD, agreed=["Report action ✅"])
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, escape(AWKWARD), status_code=409)
        self.assertContains(response, escape("Report action ✅"), status_code=409)
        self.assertContains(response, self.back_link(self.detail_url(meeting)), status_code=409)
        self.assertNotContains(response, "/line-management/staff/", status_code=409)
        self.assertNotContains(response, "Prepared so far", status_code=409)
        self.assertEqual(LineMeeting.objects.count(), 1)
        self.assertFalse(MeetingAction.objects.exists())
        self.assertEqual(notes_of(meeting), "Prepared so far")

    # Catches a double-clicked prepare answering the second click with a 409.
    def test_report_double_click_prepare_folds(self):
        first = self.prepare(main_matters="Clicked twice", agreed=["Once"])
        second = self.prepare(main_matters="Clicked twice", agreed=["Once"])
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(LineMeeting.objects.get(staff=self.report).state, PREPARING)
        self.assertEqual(MeetingAction.objects.count(), 1)

    # Catches the repeat check binding the report's POST at full scope: with no
    # Rotation key posted, a non-blank Rotation reads as changed and a harmless
    # double-click becomes a 409.
    def test_report_double_click_save_folds_when_rotation_has_text(self):
        meeting = self.preparing()
        LineMeeting.objects.filter(pk=meeting.pk).update(rotation_update="Manager’s rotation notes")
        age_meetings(meeting)
        v0 = meeting_version(LineMeeting.objects.get(pk=meeting.pk))
        payload = self.page(meeting, stamp=v0, main_matters="Report saved once", new=["Report once"])
        del payload["rotation_update"]  # a browser never posts a disabled field
        first = self.client.post(self.save_url(meeting), payload)
        second = self.client.post(self.save_url(meeting), payload)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(second["Location"], self.detail_url(meeting))
        meeting.refresh_from_db()
        self.assertEqual(meeting.rotation_update, "Manager’s rotation notes")
        self.assertEqual(meeting.main_matters, "Report saved once")
        self.assertEqual(list(meeting.agreed_actions.values_list("description", flat=True)), ["Report once"])

    # Catches the report's stale page overwriting the manager's save, or the refusal
    # dropping their text or pointing them at a page they cannot open.
    def test_report_stale_page_after_manager_save_is_409_handback(self):
        meeting = self.preparing()
        age_meetings(meeting)
        v0 = meeting_version(LineMeeting.objects.get(pk=meeting.pk))
        self.client.force_login(self.manager_user)
        self.assertEqual(self.client.post(self.save_url(meeting), self.page(
            meeting, main_matters="Manager's edit", rotation_update="Manager's rotation",
        )).status_code, 302)
        before = db_state()

        self.client.force_login(self.report_user)
        response = self.client.post(self.save_url(meeting), self.page(meeting, stamp=v0, main_matters=AWKWARD))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "This meeting was changed while you were working", status_code=409)
        self.assertContains(response, escape(AWKWARD), status_code=409)
        self.assertContains(response, self.back_link(self.detail_url(meeting)), status_code=409)
        self.assertNotContains(response, "/line-management/staff/", status_code=409)
        self.assertEqual(db_state(), before)

    # Catches a report whose line manager is cleared mid-typing losing everything
    # behind a bare 403 on their own record.
    def test_report_loses_line_manager_mid_typing_gets_text_back_not_403(self):
        self.assertEqual(self.client.get(self.prepare_new_url).status_code, 200)
        StaffMember.objects.filter(pk=self.report.pk).update(line_manager_email="")
        response = self.prepare(main_matters=AWKWARD, upcoming="Diary dates", agreed=["Kept action"])
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, escape(AWKWARD), status_code=409)
        self.assertContains(response, "Diary dates", status_code=409)
        self.assertContains(response, "Kept action", status_code=409)
        self.assertContains(response, self.back_link(self.my_meetings_url), status_code=409)
        self.assertNotContains(response, "/line-management/staff/", status_code=409)
        self.assertFalse(LineMeeting.objects.exists())

    # Catches the "Prepare" button showing to someone whose click would 403, or
    # missing for someone who may prepare.
    def test_my_meetings_offers_prepare_only_when_a_line_manager_is_recorded(self):
        link = f'href="{self.prepare_new_url}"'
        self.assertContains(self.client.get(self.my_meetings_url), link)

        self.client.force_login(self.manager_user)  # no line manager recorded
        response = self.client.get(self.my_meetings_url)
        self.assertNotContains(response, link)
        self.assertContains(response, "No line manager is recorded")

    # Catches my_meetings offering a fresh prepare (which only redirects) instead of
    # the meeting already being prepared — and not offering it again once held.
    def test_my_meetings_offers_continue_when_a_meeting_is_being_prepared(self):
        meeting = self.preparing()
        response = self.client.get(self.my_meetings_url)
        self.assertContains(response, "Continue preparing your next meeting")
        self.assertContains(response, f'href="{self.detail_url(meeting)}"')
        self.assertNotContains(response, f'href="{self.prepare_new_url}"')

        LineMeeting.objects.filter(pk=meeting.pk).update(state=HELD)
        response = self.client.get(self.my_meetings_url)
        self.assertNotContains(response, "Continue preparing your next meeting")
        self.assertContains(response, f'href="{self.prepare_new_url}"')

    # Catches any loss on the report's path (article 6): long text with CRLF, curly
    # quotes, accents and emoji must survive create, the edit form, an unchanged
    # re-save, and an invalid submit.
    def test_report_text_round_trips_emoji_crlf_and_curly_quotes(self):
        long_text = "\r\n\r\n".join([AWKWARD] * 40)
        upcoming = "Diary: Tuesday — “INSET” \U0001f389\r\nThen Friday"
        action_text = "Report’s action — “by Friday” \U0001f680\r\nsecond line"
        self.assertEqual(
            self.prepare(main_matters=long_text, upcoming=upcoming, agreed=[action_text]).status_code, 302
        )
        meeting = self.newest()
        action = MeetingAction.objects.get(agreed_at=meeting)

        def assert_stored():
            meeting.refresh_from_db()
            action.refresh_from_db()
            self.assertEqual(meeting.main_matters, long_text)
            self.assertEqual(meeting.upcoming, upcoming)
            self.assertEqual(action.description, action_text)

        assert_stored()
        page = self.client.get(self.detail_url(meeting))
        for text in (long_text, upcoming, action_text):
            self.assertContains(page, escape(text))

        unchanged = dict(main_matters=long_text, upcoming=upcoming, existing=[(action, action_text, False)])
        self.assertEqual(self.client.post(self.save_url(meeting), self.page(meeting, **unchanged)).status_code, 302)
        assert_stored()

        edited = long_text + "\r\nEdited \U0001f600"
        response = self.client.post(self.save_url(meeting), self.page(
            meeting, meeting_date="not-a-date", main_matters=edited, upcoming=upcoming,
            existing=[(action, action_text, False)],
        ))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(edited))
        assert_stored()


# ---------------------------------------------------------------------------
# Meeting-page guidance polish: the "Review actions from the last meeting" card's
# empty states, the bullet helper (data-bullets / bullets-hint / bullets.js) offered
# only on boxes the viewer can type in, and the dated "already being prepared" message.
# No access path changed; Gauntlet stage 3 is pinned by
# ManagedStaffChokepointTests.test_non_manager_gets_403_on_meeting_new and
# MeetingRoleMatrixTests.test_unrelated_user_gets_403_on_view.
# ---------------------------------------------------------------------------

from django.utils.formats import date_format  # noqa: E402


def textareas(html):
    """Map each rendered <textarea>'s id to its opening-tag attribute string."""
    found = {}
    for match in re.finditer(r"<textarea\b([^>]*)>", html):
        attrs = match.group(1)
        id_match = re.search(r'\bid="([^"]*)"', attrs)
        if id_match:
            found[id_match.group(1)] = attrs
    return found


def attr_value(attrs, name):
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', attrs)
    return match.group(1) if match else None


def has_bullets(attrs):
    return re.search(r"\bdata-bullets\b", attrs) is not None


class MeetingPageGuidanceTests(_Leg3Mixin, TestCase):
    """The carried-actions card explains itself, and the bullet helper reaches only
    boxes the viewer can actually edit."""

    def setUp(self):
        super().setUp()
        self.new_url = reverse("line_management:meeting_new", args=[self.report.pk])

    def held_source(self, **fields):
        meeting = make_meeting(self.report, meeting_date=date(2026, 1, 15), created_by_email=self.manager_email)
        if fields:
            LineMeeting.objects.filter(pk=meeting.pk).update(**fields)
        return meeting

    def detail_url(self, meeting):
        return reverse("line_management:meeting_detail", args=[meeting.pk])

    def messages_of(self, response):
        return [str(m) for m in response.context["messages"]]

    # Catches a first meeting showing an empty "review" card that reads as a broken text box.
    def test_first_meeting_card_explains_there_is_nothing_to_review_yet(self):
        response = self.client.get(self.new_url)
        self.assertEqual(response.status_code, 200)
        for text in (
            "Review actions from the last meeting",
            "Actions agreed at the previous meeting appear here",
            "Nothing to review yet",
            "This is the first meeting recorded here",
        ):
            self.assertContains(response, text)

    # Catches the empty-state message showing alongside real actions waiting to be rated.
    def test_unreviewed_action_is_listed_without_a_nothing_to_review_message(self):
        make_action(self.held_source(), "Draft the Year 9 scheme")
        response = self.client.get(self.new_url)
        self.assertContains(response, "Draft the Year 9 scheme")
        self.assertContains(response, "Actions agreed at the previous meeting appear here")
        self.assertNotContains(response, "Nothing to review")

    # Catches a manager with a Held history being told this is the first meeting.
    def test_held_source_without_actions_is_not_called_the_first_meeting(self):
        self.held_source()
        response = self.client.get(self.new_url)
        self.assertContains(response, "Nothing to review at this meeting")
        self.assertNotContains(response, "This is the first meeting recorded here")

    # Catches a Held meeting telling its manager "once this meeting is marked as held",
    # which reads as if the hold had not taken.
    def test_held_meeting_guidance_does_not_ask_for_it_to_be_held(self):
        held = self.held_source()
        self.assertTrue(held.is_held)
        response = self.client.get(self.detail_url(held))
        self.assertContains(response, "Each action comes up at the next meeting for a RAG rating.")
        self.assertNotContains(response, "marked as held, each action")
        self.assertNotContains(response, "once this meeting is marked as held")
        preparing = self.client.get(self.new_url)
        self.assertContains(preparing, "Once this meeting is marked as held, each action")

    # Catches legacy action prose disappearing, or being offered as if it could be rated.
    def test_legacy_action_prose_is_shown_and_marked_unratable(self):
        self.held_source(actions_from_meeting="Old-style action written as prose")
        response = self.client.get(self.new_url)
        self.assertContains(response, "Old-style action written as prose")
        self.assertContains(response, "can’t be given a RAG rating")
        self.assertNotContains(response, "Nothing to review")

    # Catches a read-only viewer being told to rate or add, or handed the bullet helper.
    def test_read_only_report_gets_no_edit_instructions_or_bullet_helper(self):
        meeting = self.held_source(main_matters="Held notes")
        self.client.force_login(self.report_user)
        response = self.client.get(self.detail_url(meeting))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No actions were carried forward to this meeting.")
        for text in ("rate Red, Amber or Green with a comment", "Nothing to review", "bullets-hint",
                     "data-bullets", "core/bullets.js"):
            self.assertNotContains(response, text)

    # Catches the bullet helper or its hint missing from an editable box, or the
    # shared hint being repeated on every action row (screen-reader noise).
    def test_manager_page_offers_bullets_on_every_editable_box_with_the_right_hints(self):
        make_action(self.held_source(), "Carried action")
        response = self.client.get(self.new_url)
        boxes = textareas(response.content.decode())
        for box_id in ("id_upcoming", "id_rotation_update", "id_main_matters",
                       "id_agreed-0-description", "id_carried-0-review_comment"):
            self.assertIn(box_id, boxes)
            self.assertTrue(has_bullets(boxes[box_id]), box_id)
        self.assertEqual(attr_value(boxes["id_upcoming"], "aria-describedby"), "upcoming-hint bullets-hint")
        self.assertEqual(
            attr_value(boxes["id_rotation_update"], "aria-describedby"), "rotation-guidance bullets-hint"
        )
        self.assertIn("bullets-hint", attr_value(boxes["id_main_matters"], "aria-describedby").split())
        self.assertNotIn("bullets-hint", attr_value(boxes["id_agreed-0-description"], "aria-describedby") or "")
        self.assertEqual(attr_value(boxes["id_carried-0-review_comment"], "aria-describedby"), "carried-0-text")
        self.assertEqual(response.content.decode().count('id="bullets-hint"'), 1)
        self.assertContains(response, "core/bullets.js")

    # Catches the bullet helper being attached to the Rotation update the report may not write.
    def test_report_preparing_gets_bullets_on_their_fields_but_not_rotation_update(self):
        self.client.force_login(self.report_user)
        response = self.client.get(reverse("line_management:prepare_new"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        boxes = textareas(html)
        self.assertNotIn("id_rotation_update", boxes)
        self.assertNotIn('id="id_rotation_update"', html)
        for box_id in ("id_upcoming", "id_main_matters"):
            self.assertTrue(has_bullets(boxes[box_id]), box_id)

    # Catches the bullet helper being offered on a settled (pinned) action's wording.
    def test_pinned_action_description_is_disabled_and_has_no_bullets(self):
        older = self.held_source()
        later = make_meeting(self.report, meeting_date=date(2026, 2, 1), created_by_email=self.manager_email)
        make_action(older, "Settled wording", reviewed_in=later, rag="GREEN")
        response = self.client.get(self.detail_url(older))
        self.assertEqual(response.status_code, 200)
        box = textareas(response.content.decode())["id_agreed-0-description"]
        self.assertContains(response, "Settled wording")
        self.assertRegex(box, r"\bdisabled\b")
        self.assertFalse(has_bullets(box))

    # Catches the "already being prepared" redirect leaving the user unsure which meeting opened.
    def test_already_preparing_message_names_the_meeting_date(self):
        meeting = self.preparing(meeting_date=date(2026, 10, 3))
        expected_date = date_format(meeting.meeting_date, "j M Y")

        response = self.client.get(self.new_url, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], self.detail_url(meeting))
        [message] = self.messages_of(response)
        self.assertIn(expected_date, message)
        self.assertIn("Save and mark as held", message)

        self.client.force_login(self.report_user)
        response = self.client.get(reverse("line_management:prepare_new"), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], self.detail_url(meeting))
        [message] = self.messages_of(response)
        self.assertIn(expected_date, message)
        self.assertIn("marks it as held", message)

    # Catches bullets.js breaking the house rule: assigning .value can rewrite stored text.
    def test_bullets_js_never_assigns_textarea_value(self):
        source = (settings.BASE_DIR / "core" / "static" / "core" / "bullets.js").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\.value\s*=(?!=)", source))
        self.assertIn("execCommand", source)
