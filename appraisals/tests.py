"""Access-control tests for the appraisals app.

This is the project's biggest test gap (appraisals/tests.py was empty) — see
CLAUDE.md. These focus on the security boundary, not cosmetics: the
get_appraisal_or_403 role matrix, the *snapshotted* coach_email (the deliberate
architectural contrast with line_management's live line-manager lookup),
field-level save gating (the real boundary, not template hiding), the
SIGNED_OFF lock, IDOR via guessed primary keys, and the newly-redesigned
per-bullet self-review scoring (SelfReviewItem + SelfReviewBullet, seeded by
SelfReview.seed_items()).

Identity is by email only (no FK from StaffMember to User), so every fixture
creates BOTH a Django ``User`` (to log in) and a ``StaffMember`` with the same
email. ``PermissionDenied`` surfaces as HTTP 403 through the test client.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import StaffMember
from data_import.models import ImportBatch, ImportedModel, ImportRow, ImportType

from .admin import render_self_review_table
from .leader_standards_templates import ETHICS_CONTENT, HEADTEACHER_STANDARDS
from .goal_review_fix import PLACEHOLDER_TITLE, build_plan
from .models import (
    AcademicYear,
    Appraisal,
    Goal,
    LeaderReview,
    LeaderStandard,
    SelfReview,
    SelfReviewBullet,
)
from .self_review_templates import SUPPORT_ITEMS, TEACHING_ITEMS


def make_user(email, *, is_superuser=False):
    """A Django User keyed by email (username mirrors it for uniqueness)."""
    return User.objects.create_user(
        username=email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
    )


def make_staff(email, *, performance_manager_email="", staff_type=""):
    return StaffMember.objects.create(
        email=email,
        performance_manager_email=performance_manager_email,
        staff_type=staff_type,
    )


def make_year(start_year=2025, *, is_current=True):
    return AcademicYear.objects.create(start_year=start_year, is_current=is_current)


def make_appraisal(teacher, year, *, coach_email="", status=Appraisal.Status.DRAFT):
    appraisal = Appraisal.objects.create(
        teacher=teacher,
        academic_year=year,
        coach_email=coach_email,
        status=status,
    )
    appraisal.seed_goals()
    return appraisal


def make_self_review(appraisal, *, kind=SelfReview.Kind.TEACHING):
    self_review = SelfReview.objects.create(appraisal=appraisal, kind=kind)
    self_review.seed_items()
    return self_review


def make_leader_review(appraisal):
    leader_review = LeaderReview.objects.create(appraisal=appraisal)
    leader_review.seed_standards()
    return leader_review


class AppraisalRoleMatrixTests(TestCase):
    """get_appraisal_or_403's role resolution: teacher / coach / super / stranger."""

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.coach_email = "coach@oxlip.test"
        self.stranger_email = "stranger@oxlip.test"

        self.teacher_user = make_user(self.teacher_email)
        self.coach_user = make_user(self.coach_email)
        self.stranger_user = make_user(self.stranger_email)
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)

        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        self.coach = make_staff(self.coach_email)
        self.stranger = make_staff(self.stranger_email)

        self.year = make_year()
        self.appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.coach_email
        )
        self.detail_url = reverse("appraisals:detail", args=[self.appraisal.pk])

    # Catches the teacher being locked out of their own appraisal.
    def test_teacher_can_view_own_appraisal(self):
        self.client.force_login(self.teacher_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches the coach being locked out of an appraisal they coach.
    def test_coach_can_view_appraisal(self):
        self.client.force_login(self.coach_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches superuser oversight access regressing.
    def test_superuser_can_view_appraisal(self):
        self.client.force_login(self.super_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

    # Catches IDOR: an unrelated user reaching an appraisal by guessing its PK.
    def test_stranger_gets_403_on_view(self):
        self.client.force_login(self.stranger_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)

    # Catches a logged-in user with no StaffMember row gaining access.
    def test_user_without_staff_member_gets_403_on_view(self):
        make_user("ghost@oxlip.test")  # User exists, but no StaffMember.
        self.client.force_login(User.objects.get(email="ghost@oxlip.test"))
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)

    # Catches the login gate being removed from the detail view.
    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url.lower())

    # The headline architectural contrast with line_management: coach access is
    # a SNAPSHOT (coach_email) taken at creation, not a live lookup against the
    # teacher's current performance_manager_email. Catches a regression that
    # makes coach access live (which would silently change who can see history).
    def test_original_coach_keeps_access_after_performance_manager_changes(self):
        # Reassign the teacher to a new performance manager after the
        # appraisal was created — coach_email on the appraisal is unaffected.
        new_coach_email = "new.coach@oxlip.test"
        new_coach_user = make_user(new_coach_email)
        make_staff(new_coach_email)
        self.teacher.performance_manager_email = new_coach_email
        self.teacher.save()

        # The ORIGINAL coach (snapshotted) still has access.
        self.client.force_login(self.coach_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)

        self.client.logout()

        # The NEW performance manager has no access — coach role is not
        # recomputed live from the StaffMember relationship.
        self.client.force_login(new_coach_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)


class SelfReviewSavePermissionTests(TestCase):
    """Save permissions for the redesigned per-bullet self-review section.

    Only the teacher (or a superuser) may save score/evidence changes, and
    never when the appraisal is locked. A disabled field is dropped by Django
    form validation, so it must never persist a non-teacher's submitted value.
    """

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.coach_email = "coach@oxlip.test"
        self.stranger_email = "stranger@oxlip.test"

        self.teacher_user = make_user(self.teacher_email)
        self.coach_user = make_user(self.coach_email)
        self.stranger_user = make_user(self.stranger_email)
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)

        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        self.coach = make_staff(self.coach_email)
        self.stranger = make_staff(self.stranger_email)

        self.year = make_year()
        self.appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.coach_email
        )
        self.self_review = make_self_review(self.appraisal)

        self.detail_url = reverse(
            "appraisals:detail_tab", args=[self.appraisal.pk, "self-review"]
        )
        self.save_url = reverse(
            "appraisals:self_review_save", args=[self.appraisal.pk]
        )

    def _build_payload(self, *, score="2", evidence="Saved by test"):
        """A full, valid POST payload for both formsets bound to the seeded data.

        Mirrors the real form: the items inline formset (inlineformset_factory
        derives its default prefix from the FK's related_name, "items", since
        no explicit prefix= is passed in views.py) carries one row per
        SelfReviewItem (evidence only), and the flat "bullets" formset
        (explicit prefix="bullets" in views.py) carries one row per
        SelfReviewBullet (score only). Every bullet is set to the same score
        for simplicity.
        """
        items = list(self.self_review.items.all())
        bullets = list(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.self_review
            ).order_by("self_review_item__order", "order")
        )

        payload = {
            # SelfReviewForm (non-formset) fields — TEACHING kind fields.
            "job_summary": "",
            "level_description": "",
            "upr_declaration_agreed": "",
            "signed_name": "",
            "signed_date": "",
            # Items inline formset management form.
            "items-TOTAL_FORMS": str(len(items)),
            "items-INITIAL_FORMS": str(len(items)),
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            # Bullets flat formset management form.
            "bullets-TOTAL_FORMS": str(len(bullets)),
            "bullets-INITIAL_FORMS": str(len(bullets)),
            "bullets-MIN_NUM_FORMS": "0",
            "bullets-MAX_NUM_FORMS": "1000",
        }
        for index, item in enumerate(items):
            payload[f"items-{index}-id"] = str(item.pk)
            payload[f"items-{index}-evidence"] = evidence
        for index, bullet in enumerate(bullets):
            payload[f"bullets-{index}-id"] = str(bullet.pk)
            payload[f"bullets-{index}-score"] = score
        return payload

    # Catches the teacher's own scores/evidence silently failing to persist.
    def test_teacher_can_save_scores_and_evidence(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            self.save_url, self._build_payload(score="3", evidence="All good"), follow=True
        )
        self.assertEqual(response.status_code, 200)

        bullets = SelfReviewBullet.objects.filter(
            self_review_item__self_review=self.self_review
        )
        self.assertTrue(bullets.exists())
        self.assertTrue(all(b.score == 3 for b in bullets))

        items = self.self_review.items.all()
        self.assertTrue(all(i.evidence == "All good" for i in items))

    # Catches the coach being able to write into teacher-only fields server-side.
    def test_coach_cannot_save_self_review(self):
        self.client.force_login(self.coach_user)
        response = self.client.post(self.save_url, self._build_payload())
        self.assertEqual(response.status_code, 403)

        bullets = SelfReviewBullet.objects.filter(
            self_review_item__self_review=self.self_review
        )
        self.assertTrue(all(b.score is None for b in bullets))

    # Catches the score radios losing their disabled state for a non-teacher GET.
    def test_coach_sees_disabled_score_fields_on_get(self):
        self.client.force_login(self.coach_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "disabled")

    # Catches superuser oversight on the self-review save path regressing.
    def test_superuser_can_save_self_review(self):
        self.client.force_login(self.super_user)
        response = self.client.post(
            self.save_url, self._build_payload(score="1", evidence="Super edit"), follow=True
        )
        self.assertEqual(response.status_code, 200)
        bullets = SelfReviewBullet.objects.filter(
            self_review_item__self_review=self.self_review
        )
        self.assertTrue(all(b.score == 1 for b in bullets))

    # Catches IDOR: a stranger must be denied before even reaching the formset.
    def test_stranger_gets_403_on_view_before_save(self):
        self.client.force_login(self.stranger_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 403)

    # Catches IDOR on the write path independently of the read path.
    def test_stranger_cannot_save_self_review(self):
        self.client.force_login(self.stranger_user)
        response = self.client.post(self.save_url, self._build_payload())
        self.assertEqual(response.status_code, 403)
        bullets = SelfReviewBullet.objects.filter(
            self_review_item__self_review=self.self_review
        )
        self.assertTrue(all(b.score is None for b in bullets))


class AppraisalLockingTests(TestCase):
    """Once SIGNED_OFF (is_locked), even the owning teacher may not edit."""

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.teacher_user = make_user(self.teacher_email)
        self.teacher = make_staff(
            self.teacher_email, staff_type=StaffMember.StaffType.TEACHING
        )
        self.year = make_year()
        self.appraisal = make_appraisal(
            self.teacher,
            self.year,
            status=Appraisal.Status.SIGNED_OFF,
        )
        self.self_review = make_self_review(self.appraisal)
        self.save_url = reverse(
            "appraisals:self_review_save", args=[self.appraisal.pk]
        )

    def _build_payload(self):
        items = list(self.self_review.items.all())
        bullets = list(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.self_review
            ).order_by("self_review_item__order", "order")
        )
        payload = {
            "job_summary": "",
            "level_description": "",
            "upr_declaration_agreed": "",
            "signed_name": "",
            "signed_date": "",
            "items-TOTAL_FORMS": str(len(items)),
            "items-INITIAL_FORMS": str(len(items)),
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "bullets-TOTAL_FORMS": str(len(bullets)),
            "bullets-INITIAL_FORMS": str(len(bullets)),
            "bullets-MIN_NUM_FORMS": "0",
            "bullets-MAX_NUM_FORMS": "1000",
        }
        for index, item in enumerate(items):
            payload[f"items-{index}-id"] = str(item.pk)
            payload[f"items-{index}-evidence"] = "should not save"
        for index, bullet in enumerate(bullets):
            payload[f"bullets-{index}-id"] = str(bullet.pk)
            payload[f"bullets-{index}-score"] = "2"
        return payload

    # Catches a signed-off appraisal still being editable by its own teacher —
    # mirrors can_edit_teacher_fields's explicit `not appraisal.is_locked` check.
    def test_teacher_cannot_save_self_review_once_signed_off(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(self.save_url, self._build_payload())
        # 409, not 403: the teacher still holds their role and only the lock
        # changed under them, so this is a conflict. Nothing is saved (asserted
        # below, unchanged) but their typing is handed back rather than binned.
        self.assertEqual(response.status_code, 409)

        bullets = SelfReviewBullet.objects.filter(
            self_review_item__self_review=self.self_review
        )
        self.assertTrue(all(b.score is None for b in bullets))
        items = self.self_review.items.all()
        self.assertTrue(all(i.evidence == "" for i in items))


class SeedItemsTests(TestCase):
    """SelfReview.seed_items(): bulk creation of items + bullets per kind."""

    def setUp(self):
        self.teacher = make_staff(
            "teacher@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.year = make_year()
        self.appraisal = make_appraisal(self.teacher, self.year)

    # Catches the TEACHING template's item/bullet counts drifting from the
    # actual descriptor content in self_review_templates.py.
    def test_seed_items_teaching_creates_expected_item_and_bullet_counts(self):
        self_review = SelfReview.objects.create(
            appraisal=self.appraisal, kind=SelfReview.Kind.TEACHING
        )
        self_review.seed_items()

        expected_item_count = len(TEACHING_ITEMS)
        expected_bullet_count = sum(len(bullets) for _, _, bullets in TEACHING_ITEMS)

        self.assertEqual(self_review.items.count(), expected_item_count)
        self.assertEqual(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self_review
            ).count(),
            expected_bullet_count,
        )

    # Catches the SUPPORT template's (smaller, different) counts drifting.
    def test_seed_items_support_creates_expected_item_and_bullet_counts(self):
        self_review = SelfReview.objects.create(
            appraisal=self.appraisal, kind=SelfReview.Kind.SUPPORT
        )
        self_review.seed_items()

        expected_item_count = len(SUPPORT_ITEMS)
        expected_bullet_count = sum(len(bullets) for _, _, bullets in SUPPORT_ITEMS)

        self.assertEqual(self_review.items.count(), expected_item_count)
        self.assertEqual(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self_review
            ).count(),
            expected_bullet_count,
        )

    # Catches calling seed_items() twice (e.g. via repeated _ensure_self_review
    # calls on every GET) duplicating items or bullets.
    def test_seed_items_called_twice_does_not_duplicate(self):
        self_review = SelfReview.objects.create(
            appraisal=self.appraisal, kind=SelfReview.Kind.TEACHING
        )
        self_review.seed_items()
        first_item_count = self_review.items.count()
        first_bullet_count = SelfReviewBullet.objects.filter(
            self_review_item__self_review=self_review
        ).count()

        self_review.seed_items()

        self.assertEqual(self_review.items.count(), first_item_count)
        self.assertEqual(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self_review
            ).count(),
            first_bullet_count,
        )

    # Catches the order/text/code fields round-tripping incorrectly from the
    # template tuples (e.g. an off-by-one in the enumerate(), or fields swapped).
    def test_seed_items_round_trips_code_heading_order_and_bullet_text(self):
        self_review = SelfReview.objects.create(
            appraisal=self.appraisal, kind=SelfReview.Kind.TEACHING
        )
        self_review.seed_items()

        first_code, first_heading, first_bullets = TEACHING_ITEMS[0]
        item = self_review.items.get(code=first_code)
        self.assertEqual(item.order, 1)
        self.assertEqual(item.heading, first_heading)

        bullets = list(item.bullets.order_by("order"))
        self.assertEqual(len(bullets), len(first_bullets))
        for index, expected_text in enumerate(first_bullets):
            self.assertEqual(bullets[index].order, index + 1)
            self.assertEqual(bullets[index].text, expected_text)
            self.assertIsNone(bullets[index].score)


class GoalsSectionGatingTests(TestCase):
    """GoalForm: teacher_fields vs coach_fields, gated by can_edit_*_fields."""

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.coach_email = "coach@oxlip.test"

        self.teacher_user = make_user(self.teacher_email)
        self.coach_user = make_user(self.coach_email)

        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        self.coach = make_staff(self.coach_email)

        self.year = make_year()
        self.appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.coach_email
        )
        self.save_url = reverse("appraisals:goals_save", args=[self.appraisal.pk])

    # GoalFormSet's default prefix is derived by inlineformset_factory from the
    # FK's related_name ("goals" on Appraisal.goals), not a flat "form" — confirm
    # that assumption directly against the actual save behaviour rather than
    # guessing.
    def test_goal_formset_uses_default_form_prefix(self):
        self.client.force_login(self.teacher_user)
        goal = self.appraisal.goals.order_by("order").first()
        payload = {
            "goals-TOTAL_FORMS": "3",
            "goals-INITIAL_FORMS": "3",
            "goals-MIN_NUM_FORMS": "0",
            "goals-MAX_NUM_FORMS": "1000",
        }
        for index, g in enumerate(self.appraisal.goals.order_by("order")):
            payload[f"goals-{index}-id"] = str(g.pk)
            payload[f"goals-{index}-title"] = (
                "Teacher edited goal" if g.pk == goal.pk else g.title
            )
            payload[f"goals-{index}-steps_to_success"] = g.steps_to_success
            payload[f"goals-{index}-success_criteria"] = g.success_criteria
            payload[f"goals-{index}-teacher_review_comment"] = g.teacher_review_comment
            payload[f"goals-{index}-coach_review_comment"] = g.coach_review_comment

        response = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        goal.refresh_from_db()
        self.assertEqual(goal.title, "Teacher edited goal")

    # Catches the teacher being able to write into coach_review_comment
    # server-side despite the field being disabled for their role.
    def test_teacher_cannot_set_coach_review_comment(self):
        self.client.force_login(self.teacher_user)
        goal = self.appraisal.goals.order_by("order").first()
        payload = {
            "goals-TOTAL_FORMS": "3",
            "goals-INITIAL_FORMS": "3",
            "goals-MIN_NUM_FORMS": "0",
            "goals-MAX_NUM_FORMS": "1000",
        }
        for index, g in enumerate(self.appraisal.goals.order_by("order")):
            payload[f"goals-{index}-id"] = str(g.pk)
            payload[f"goals-{index}-title"] = g.title
            payload[f"goals-{index}-steps_to_success"] = g.steps_to_success
            payload[f"goals-{index}-success_criteria"] = g.success_criteria
            payload[f"goals-{index}-teacher_review_comment"] = g.teacher_review_comment
            payload[f"goals-{index}-coach_review_comment"] = (
                "smuggled coach comment" if g.pk == goal.pk else g.coach_review_comment
            )

        self.client.post(self.save_url, payload, follow=True)
        goal.refresh_from_db()
        self.assertNotEqual(goal.coach_review_comment, "smuggled coach comment")

    # Catches the coach being able to write into teacher-owned goal fields
    # server-side despite the field being disabled for their role.
    def test_coach_cannot_set_teacher_owned_goal_fields(self):
        self.client.force_login(self.coach_user)
        goal = self.appraisal.goals.order_by("order").first()
        payload = {
            "goals-TOTAL_FORMS": "3",
            "goals-INITIAL_FORMS": "3",
            "goals-MIN_NUM_FORMS": "0",
            "goals-MAX_NUM_FORMS": "1000",
        }
        for index, g in enumerate(self.appraisal.goals.order_by("order")):
            payload[f"goals-{index}-id"] = str(g.pk)
            payload[f"goals-{index}-title"] = (
                "smuggled teacher title" if g.pk == goal.pk else g.title
            )
            payload[f"goals-{index}-steps_to_success"] = g.steps_to_success
            payload[f"goals-{index}-success_criteria"] = g.success_criteria
            payload[f"goals-{index}-teacher_review_comment"] = g.teacher_review_comment
            payload[f"goals-{index}-coach_review_comment"] = (
                "legit coach comment" if g.pk == goal.pk else g.coach_review_comment
            )

        response = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        goal.refresh_from_db()
        self.assertNotEqual(goal.title, "smuggled teacher title")
        self.assertEqual(goal.coach_review_comment, "legit coach comment")


class SummarySectionGatingTests(TestCase):
    """AppraisalSummaryForm: teacher_fields vs coach_fields (incl. `status`)."""

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.coach_email = "coach@oxlip.test"

        self.teacher_user = make_user(self.teacher_email)
        self.coach_user = make_user(self.coach_email)

        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        self.coach = make_staff(self.coach_email)

        self.year = make_year()
        self.appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.coach_email
        )
        self.save_url = reverse("appraisals:summary_save", args=[self.appraisal.pk])

    def _base_payload(self, **overrides):
        payload = {
            "cpd_requirements": self.appraisal.cpd_requirements,
            "summary_teacher_comment": self.appraisal.summary_teacher_comment,
            "summary_coach_comment": self.appraisal.summary_coach_comment,
            "on_upper_pay_range": "false",
            "self_review_form_completed": "false",
            "engaged_with_professional_growth": "false",
            "coach_supports_pay_award": "",
            "job_description_review_needed": "false",
            "status": self.appraisal.status,
        }
        payload.update(overrides)
        return payload

    # Catches the teacher being able to sign off their own appraisal — `status`
    # is coach-only per AppraisalSummaryForm.coach_fields.
    def test_teacher_cannot_change_status(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            self.save_url,
            self._base_payload(
                status=Appraisal.Status.SIGNED_OFF,
                summary_teacher_comment="my comment",
            ),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.appraisal.refresh_from_db()
        self.assertEqual(self.appraisal.status, Appraisal.Status.DRAFT)
        self.assertEqual(self.appraisal.summary_teacher_comment, "my comment")

    # Catches the coach being able to write into the teacher-owned comment
    # field, and confirms the coach's own status change does take effect.
    def test_coach_can_change_status_but_not_teacher_comment(self):
        self.client.force_login(self.coach_user)
        response = self.client.post(
            self.save_url,
            self._base_payload(
                status=Appraisal.Status.SIGNED_OFF,
                summary_teacher_comment="smuggled teacher comment",
                summary_coach_comment="coach signoff comment",
            ),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.appraisal.refresh_from_db()
        self.assertEqual(self.appraisal.status, Appraisal.Status.SIGNED_OFF)
        self.assertNotEqual(
            self.appraisal.summary_teacher_comment, "smuggled teacher comment"
        )
        self.assertEqual(self.appraisal.summary_coach_comment, "coach signoff comment")

    # Catches signed_off_at not being stamped when the coach signs off via the
    # summary form (_stamp_signoff is only invoked from this save path).
    def test_signing_off_stamps_signed_off_at(self):
        self.client.force_login(self.coach_user)
        self.assertIsNone(self.appraisal.signed_off_at)
        self.client.post(
            self.save_url,
            self._base_payload(status=Appraisal.Status.SIGNED_OFF),
            follow=True,
        )
        self.appraisal.refresh_from_db()
        self.assertIsNotNone(self.appraisal.signed_off_at)


class IDORAcrossSectionsTests(TestCase):
    """A stranger must be denied on every section's view and save endpoint."""

    def setUp(self):
        self.teacher = make_staff(
            "teacher@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.stranger_user = make_user("stranger@oxlip.test")
        make_staff("stranger@oxlip.test")

        self.year = make_year()
        self.appraisal = make_appraisal(self.teacher, self.year)
        make_self_review(self.appraisal)

        self.pk = self.appraisal.pk

    # Catches a stranger reaching the goals tab content via a guessed PK.
    def test_stranger_gets_403_on_goals_tab(self):
        self.client.force_login(self.stranger_user)
        url = reverse("appraisals:detail_tab", args=[self.pk, "goals"])
        self.assertEqual(self.client.get(url).status_code, 403)

    # Catches a stranger reaching the summary tab content via a guessed PK.
    def test_stranger_gets_403_on_summary_tab(self):
        self.client.force_login(self.stranger_user)
        url = reverse("appraisals:detail_tab", args=[self.pk, "summary"])
        self.assertEqual(self.client.get(url).status_code, 403)

    # Catches a stranger being able to POST to the goals save endpoint.
    def test_stranger_cannot_save_goals(self):
        self.client.force_login(self.stranger_user)
        url = reverse("appraisals:goals_save", args=[self.pk])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 403)

    # Catches a stranger being able to POST to the summary save endpoint.
    def test_stranger_cannot_save_summary(self):
        self.client.force_login(self.stranger_user)
        url = reverse("appraisals:summary_save", args=[self.pk])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 403)


class SeedStandardsTests(TestCase):
    """LeaderReview.seed_standards(): the 3 Ethics rows + 10 Standards, once."""

    def setUp(self):
        self.leader = make_staff(
            "head@oxlip.test", staff_type=StaffMember.StaffType.LEADER
        )
        self.year = make_year()
        self.appraisal = make_appraisal(self.leader, self.year)

    # Catches the row counts drifting from the template constants.
    def test_seed_creates_ethics_and_standards_rows(self):
        leader_review = make_leader_review(self.appraisal)
        ethics = leader_review.standards.filter(
            section=LeaderStandard.Section.ETHICS
        )
        standards = leader_review.standards.filter(
            section=LeaderStandard.Section.STANDARDS
        )
        self.assertEqual(ethics.count(), len(ETHICS_CONTENT))
        self.assertEqual(ethics.count(), 3)
        self.assertEqual(standards.count(), len(HEADTEACHER_STANDARDS))
        self.assertEqual(standards.count(), 10)

    # Catches number/title/descriptor content or ordering drifting from the
    # template tuples (e.g. an off-by-one in enumerate, or fields swapped).
    def test_seed_round_trips_number_title_and_descriptors(self):
        leader_review = make_leader_review(self.appraisal)
        first_number, first_title, first_descriptors = HEADTEACHER_STANDARDS[0]
        standard = leader_review.standards.get(
            section=LeaderStandard.Section.STANDARDS, number=first_number
        )
        self.assertEqual(standard.order, 1)
        self.assertEqual(standard.title, first_title)
        self.assertEqual(standard.descriptor_list, list(first_descriptors))
        self.assertIsNone(standard.score)
        self.assertFalse(standard.not_applicable)

    # Catches the Ethics section not being seeded as scored rows.
    def test_seed_round_trips_ethics_heading_and_bullets(self):
        leader_review = make_leader_review(self.appraisal)
        first_heading, first_bullets = ETHICS_CONTENT[0]
        ethic = leader_review.standards.get(
            section=LeaderStandard.Section.ETHICS, number=1
        )
        self.assertEqual(ethic.title, first_heading)
        self.assertEqual(ethic.descriptor_list, list(first_bullets))

    # Catches repeated _ensure_leader_review calls (every GET) duplicating rows,
    # and confirms the per-row guard back-fills without double-seeding.
    def test_seed_called_twice_does_not_duplicate(self):
        leader_review = make_leader_review(self.appraisal)
        leader_review.seed_standards()
        self.assertEqual(leader_review.standards.count(), 13)


class LeaderReviewSelectionTests(TestCase):
    """A LEADER staff member gets the leader variant, not a SelfReview."""

    def setUp(self):
        self.leader_email = "head@oxlip.test"
        self.leader_user = make_user(self.leader_email)
        self.leader = make_staff(
            self.leader_email, staff_type=StaffMember.StaffType.LEADER
        )
        self.year = make_year()
        self.appraisal = make_appraisal(self.leader, self.year)
        self.detail_url = reverse(
            "appraisals:detail_tab", args=[self.appraisal.pk, "self-review"]
        )

    # Catches the leader variant not rendering (falling back to the teaching
    # self-review), and confirms the tab shows the standards content.
    def test_leader_sees_headteacher_standards_tab(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Headteacher")
        self.assertContains(response, "School Culture")

    # Catches a SelfReview being created for a leader (the two variants must not
    # both be seeded), and confirms the LeaderReview is created + seeded on GET.
    def test_leader_get_builds_leader_review_not_self_review(self):
        self.client.force_login(self.leader_user)
        self.client.get(self.detail_url)
        self.assertFalse(SelfReview.objects.filter(appraisal=self.appraisal).exists())
        leader_review = LeaderReview.objects.get(appraisal=self.appraisal)
        self.assertEqual(leader_review.standards.count(), 13)


class LeaderReviewSaveTests(TestCase):
    """Save permissions and behaviour for the senior-leader self-review."""

    def setUp(self):
        self.leader_email = "head@oxlip.test"
        self.coach_email = "chair@oxlip.test"
        self.stranger_email = "stranger@oxlip.test"

        self.leader_user = make_user(self.leader_email)
        self.coach_user = make_user(self.coach_email)
        self.stranger_user = make_user(self.stranger_email)
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)

        self.leader = make_staff(
            self.leader_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.LEADER,
        )
        make_staff(self.coach_email)
        make_staff(self.stranger_email)

        self.year = make_year()
        self.appraisal = make_appraisal(
            self.leader, self.year, coach_email=self.coach_email
        )
        self.leader_review = make_leader_review(self.appraisal)

        self.detail_url = reverse(
            "appraisals:detail_tab", args=[self.appraisal.pk, "self-review"]
        )
        self.save_url = reverse(
            "appraisals:self_review_save", args=[self.appraisal.pk]
        )

    def _payload(self, *, score="2", examples="", na_index=None):
        """A full, valid POST payload for the scored-rows inline formset.

        Covers all 13 rows (3 Ethics + 10 Standards) under the default prefix
        "standards" (derived from LeaderReview.standards), in the same
        section-then-order sequence the formset renders them.
        """
        standards = list(
            self.leader_review.standards.order_by("section", "order")
        )
        payload = {
            "standards-TOTAL_FORMS": str(len(standards)),
            "standards-INITIAL_FORMS": str(len(standards)),
            "standards-MIN_NUM_FORMS": "0",
            "standards-MAX_NUM_FORMS": "1000",
        }
        for index, standard in enumerate(standards):
            payload[f"standards-{index}-id"] = str(standard.pk)
            payload[f"standards-{index}-score"] = score
            # "Not in job role" is a tick box: an unticked box submits no key
            # at all, so only the N/A row carries one.
            if na_index == index:
                payload[f"standards-{index}-not_applicable"] = "on"
            payload[f"standards-{index}-examples"] = examples
        return payload

    # Catches the leader's own scores/examples silently failing to persist.
    def test_leader_can_save_scores_and_examples(self):
        self.client.force_login(self.leader_user)
        response = self.client.post(
            self.save_url, self._payload(score="3", examples="Evidence here"), follow=True
        )
        self.assertEqual(response.status_code, 200)
        standards = self.leader_review.standards.all()
        self.assertTrue(all(s.score == 3 for s in standards))
        self.assertTrue(all(s.examples == "Evidence here" for s in standards))

    # Catches the "Not in Job Role" rule not clearing a submitted score — a
    # standard marked N/A must never carry a score (model.save enforces this).
    # Index 3 is the first Standards row (the 3 Ethics rows sort first).
    def test_not_applicable_clears_score_on_save(self):
        self.client.force_login(self.leader_user)
        response = self.client.post(
            self.save_url, self._payload(score="3", na_index=3), follow=True
        )
        self.assertEqual(response.status_code, 200)
        standards = list(self.leader_review.standards.order_by("section", "order"))
        self.assertTrue(standards[3].not_applicable)
        self.assertIsNone(standards[3].score)
        # Every other row keeps its score.
        others = standards[:3] + standards[4:]
        self.assertTrue(all(s.score == 3 for s in others))

    # "Not in job role" is a tick box, not a Yes/No pair. Unticking must clear
    # a previously-set flag — the classic checkbox trap, since an unticked box
    # submits nothing at all and a naive read leaves the old value in place.
    def test_unticking_not_in_job_role_clears_the_flag(self):
        self.client.force_login(self.leader_user)
        self.client.post(self.save_url, self._payload(score="3", na_index=3), follow=True)
        standards = list(self.leader_review.standards.order_by("section", "order"))
        self.assertTrue(standards[3].not_applicable)

        # Post again with no N/A row at all (every box unticked).
        self.client.post(self.save_url, self._payload(score="2"), follow=True)
        standards = list(self.leader_review.standards.order_by("section", "order"))
        self.assertFalse(standards[3].not_applicable)
        self.assertEqual(standards[3].score, 2)

    # Guards the widget swap itself: a checkbox input, not the old radio pair.
    def test_not_in_job_role_renders_as_a_tick_box(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(self.detail_url)
        self.assertContains(
            response, 'type="checkbox" name="standards-3-not_applicable"'
        )
        self.assertNotContains(
            response, 'type="radio" name="standards-3-not_applicable"'
        )

    # Catches the coach being able to write into the leader's own fields.
    def test_coach_cannot_save_leader_review(self):
        self.client.force_login(self.coach_user)
        response = self.client.post(self.save_url, self._payload(score="3"))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(all(s.score is None for s in self.leader_review.standards.all()))

    # Catches the score/na radios losing their disabled state for a coach GET.
    def test_coach_sees_disabled_fields_on_get(self):
        self.client.force_login(self.coach_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "disabled")

    # Catches superuser oversight on the leader save path regressing.
    def test_superuser_can_save_leader_review(self):
        self.client.force_login(self.super_user)
        response = self.client.post(
            self.save_url, self._payload(score="1"), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(s.score == 1 for s in self.leader_review.standards.all()))

    # Catches IDOR on the leader write path.
    def test_stranger_cannot_save_leader_review(self):
        self.client.force_login(self.stranger_user)
        response = self.client.post(self.save_url, self._payload(score="2"))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(all(s.score is None for s in self.leader_review.standards.all()))

    # Catches a signed-off leader appraisal remaining editable by the leader.
    def test_leader_cannot_save_once_signed_off(self):
        self.appraisal.status = Appraisal.Status.SIGNED_OFF
        self.appraisal.save()
        self.client.force_login(self.leader_user)
        response = self.client.post(self.save_url, self._payload(score="3"))
        # Conflict, not denial — see AppraisalLockingTests.
        self.assertEqual(response.status_code, 409)
        self.assertTrue(all(s.score is None for s in self.leader_review.standards.all()))


class SelfReviewVariantFollowsOwnerTests(TestCase):
    """Regression: the self-review VARIANT is a property of the appraisal's owner.

    ``_build_section_forms`` used to branch on the StaffMember returned by
    ``get_appraisal_or_403`` — i.e. the *viewer*. A senior-leader coach opening
    a teaching coachee's appraisal therefore took the leader branch, which
    called ``_ensure_leader_review`` and CREATED a blank LeaderReview plus 13
    seeded LeaderStandard rows on the coachee's appraisal, and rendered those
    blank rows instead of the teacher's real self-review. The teacher's own
    page still looked fine, so nothing else caught it.

    Every existing coach test on this tab was a POST, and ``self_review_save``
    is teacher-gated, so it 403s before reaching the branch. These are GETs:
    the actual user path.
    """

    def setUp(self):
        self.leader_coach_email = "head@oxlip.test"
        self.teaching_coach_email = "chair@oxlip.test"
        self.support_coach_email = "office.manager@oxlip.test"
        self.teacher_email = "teacher@oxlip.test"
        self.leader_email = "deputy@oxlip.test"

        self.leader_coach_user = make_user(self.leader_coach_email)
        self.teaching_coach_user = make_user(self.teaching_coach_email)
        self.support_coach_user = make_user(self.support_coach_email)
        self.teacher_user = make_user(self.teacher_email)
        self.leader_user = make_user(self.leader_email)

        # The coaches. Their OWN staff_type is what the bug leaked through.
        self.leader_coach = make_staff(
            self.leader_coach_email, staff_type=StaffMember.StaffType.LEADER
        )
        self.teaching_coach = make_staff(
            self.teaching_coach_email, staff_type=StaffMember.StaffType.TEACHING
        )
        self.support_coach = make_staff(
            self.support_coach_email, staff_type=StaffMember.StaffType.SUPPORT
        )

        # A teaching coachee, performance-managed by the LEADER coach.
        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.leader_coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        # A senior-leader coachee, performance-managed by a TEACHING coach.
        self.leader = make_staff(
            self.leader_email,
            performance_manager_email=self.teaching_coach_email,
            staff_type=StaffMember.StaffType.LEADER,
        )

        self.year = make_year()

        self.teacher_appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.leader_coach_email
        )
        self.teacher_self_review = make_self_review(self.teacher_appraisal)
        # Real teacher-entered content, so we can prove the coach is looking at
        # the teacher's record rather than a freshly seeded blank one.
        self.evidence_text = "Evidence written by the teacher for Year 6 maths"
        self.teacher_self_review.items.update(evidence=self.evidence_text)
        SelfReviewBullet.objects.filter(
            self_review_item__self_review=self.teacher_self_review
        ).update(score=3)

        self.leader_appraisal = make_appraisal(
            self.leader, self.year, coach_email=self.teaching_coach_email
        )
        self.leader_review = make_leader_review(self.leader_appraisal)
        self.examples_text = "Examples written by the deputy head"
        self.leader_review.standards.update(examples=self.examples_text)

        self.teacher_url = reverse(
            "appraisals:detail_tab", args=[self.teacher_appraisal.pk, "self-review"]
        )
        self.leader_url = reverse(
            "appraisals:detail_tab", args=[self.leader_appraisal.pk, "self-review"]
        )

    def _checked_score_inputs(self, response, field_name):
        """The rendered score radio inputs for one bullet that carry `checked`."""
        html = response.content.decode()
        tags = re.findall(r'<input[^>]*name="%s"[^>]*>' % re.escape(field_name), html)
        return [tag for tag in tags if "checked" in tag]

    # REGRESSION. Catches the viewer's own LEADER staff_type selecting the
    # self-review variant: a leader coach must see the teaching coachee's
    # teaching self-review, not blank Headteacher's Standards.
    def test_leader_coach_viewing_teaching_coachee_gets_the_teaching_variant(self):
        self.client.force_login(self.leader_coach_user)
        response = self.client.get(self.teacher_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_leader"])
        self.assertEqual(response.context["self_review"], self.teacher_self_review)

        rows = response.context["self_review_rows"]
        expected_items = list(self.teacher_self_review.items.order_by("order"))
        self.assertTrue(expected_items)
        self.assertEqual(len(rows), len(expected_items))
        rendered_bullets = [
            bullet_form.instance
            for row in rows
            for bullet_form in row["bullet_forms"]
        ]
        self.assertTrue(rendered_bullets)
        # Every bullet rendered must belong to THIS teacher's self-review.
        self.assertTrue(
            all(
                bullet.self_review_item.self_review_id == self.teacher_self_review.pk
                for bullet in rendered_bullets
            )
        )
        self.assertEqual(
            len(rendered_bullets),
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.teacher_self_review
            ).count(),
        )

    # REGRESSION. Catches the coachee's real answers being replaced on screen by
    # a blank leader form — the half of the bug the coach actually saw.
    def test_leader_coach_sees_the_teachers_saved_evidence_and_scores(self):
        self.client.force_login(self.leader_coach_user)
        response = self.client.get(self.teacher_url)
        self.assertContains(response, self.evidence_text)
        checked = self._checked_score_inputs(response, "bullets-0-score")
        self.assertEqual(len(checked), 1)
        self.assertIn('value="3"', checked[0])
        # The leader form must not be on the page at all.
        self.assertNotContains(response, "Headteacher")

    # REGRESSION (data pollution). Catches a mere GET by a leader coach writing
    # a blank LeaderReview + 13 LeaderStandard rows onto the coachee's appraisal.
    def test_leader_coach_get_creates_no_leader_review_on_coachees_appraisal(self):
        self.assertEqual(
            LeaderReview.objects.filter(appraisal=self.teacher_appraisal).count(), 0
        )
        self.client.force_login(self.leader_coach_user)
        response = self.client.get(self.teacher_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            LeaderReview.objects.filter(appraisal=self.teacher_appraisal).count(), 0
        )
        self.assertFalse(
            LeaderStandard.objects.filter(
                leader_review__appraisal=self.teacher_appraisal
            ).exists()
        )

    # REGRESSION (mirror case). Catches a non-leader coach being shown a blank
    # teaching self-review instead of the senior leader's real standards.
    def test_teaching_coach_viewing_leader_coachee_gets_the_leader_variant(self):
        self.client.force_login(self.teaching_coach_user)
        response = self.client.get(self.leader_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_leader"])
        self.assertEqual(response.context["leader_review"], self.leader_review)
        self.assertContains(response, self.examples_text)

    # REGRESSION (mirror data pollution). Catches a GET by a non-leader coach
    # creating a SelfReview + seeded item/bullet tree on a leader's appraisal.
    def test_teaching_coach_get_creates_no_self_review_on_leaders_appraisal(self):
        self.assertEqual(
            SelfReview.objects.filter(appraisal=self.leader_appraisal).count(), 0
        )
        self.client.force_login(self.teaching_coach_user)
        response = self.client.get(self.leader_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            SelfReview.objects.filter(appraisal=self.leader_appraisal).count(), 0
        )

    # REGRESSION (kind pollution). An appraisal with no SelfReview yet is seeded
    # on first GET; the KIND must come from the owner, not the viewer, or a
    # SUPPORT coach's visit would give a teacher the support-staff descriptors.
    def test_support_coach_get_seeds_the_teachers_kind_not_the_coachs(self):
        appraisal = make_appraisal(
            self.teacher,
            make_year(2026, is_current=False),
            coach_email=self.support_coach_email,
        )
        self.assertEqual(SelfReview.objects.filter(appraisal=appraisal).count(), 0)

        self.client.force_login(self.support_coach_user)
        url = reverse("appraisals:detail_tab", args=[appraisal.pk, "self-review"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        self_review = SelfReview.objects.get(appraisal=appraisal)
        self.assertEqual(self_review.kind, SelfReview.Kind.TEACHING)
        self.assertEqual(self_review.items.count(), len(TEACHING_ITEMS))
        self.assertEqual(LeaderReview.objects.filter(appraisal=appraisal).count(), 0)

    # COVERAGE. The owner's own GET must be unaffected by the fix (teaching).
    def test_teacher_still_sees_own_self_review_on_get(self):
        self.client.force_login(self.teacher_user)
        response = self.client.get(self.teacher_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_leader"])
        self.assertEqual(response.context["self_review"], self.teacher_self_review)
        self.assertContains(response, self.evidence_text)
        self.assertEqual(
            LeaderReview.objects.filter(appraisal=self.teacher_appraisal).count(), 0
        )

    # COVERAGE. The owner's own GET must be unaffected by the fix (leader).
    def test_leader_still_sees_own_standards_on_get(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(self.leader_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_leader"])
        self.assertContains(response, self.examples_text)
        self.assertEqual(
            SelfReview.objects.filter(appraisal=self.leader_appraisal).count(), 0
        )

    # COVERAGE. A superuser has no StaffMember, so the old code fell back to
    # ``appraisal.teacher`` and happened to be correct; pin that it stays so
    # now that the fallback has gone.
    def test_superuser_get_follows_each_owners_variant(self):
        admin = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(admin)

        response = self.client.get(self.teacher_url)
        self.assertFalse(response.context["is_leader"])
        response = self.client.get(self.leader_url)
        self.assertTrue(response.context["is_leader"])

        self.assertEqual(
            LeaderReview.objects.filter(appraisal=self.teacher_appraisal).count(), 0
        )
        self.assertEqual(
            SelfReview.objects.filter(appraisal=self.leader_appraisal).count(), 0
        )


class StartAppraisalSelfClassifyTests(TestCase):
    """An unclassified staff member can self-select Teaching/Support to start.

    This removes the first-time dead-end where a provisioned-but-unclassified
    staff member hit 'contact an administrator' with no way forward. The rule:
    the posted staff_type only ever *fills a blank* (never overwrites), and only
    TEACHING/SUPPORT are self-selectable (LEADER stays admin/import-only).
    """

    def setUp(self):
        self.email = "newbie@oxlip.test"
        self.user = make_user(self.email)
        self.staff = make_staff(self.email)  # staff_type blank
        self.year = make_year()
        self.start_url = reverse("appraisals:start_appraisal")
        self.client.force_login(self.user)

    def test_blank_type_shows_choice_not_deadend(self):
        response = self.client.get(reverse("appraisals:my_appraisal"))
        self.assertContains(response, "Teaching")
        self.assertContains(response, "Support")
        self.assertContains(response, "Start my goal setting and review")

    def test_posting_teaching_sets_type_and_seeds_self_review(self):
        response = self.client.post(self.start_url, {"staff_type": "TEACHING"})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.staff_type, StaffMember.StaffType.TEACHING)
        appraisal = Appraisal.objects.get(teacher=self.staff, academic_year=self.year)
        self.assertTrue(SelfReview.objects.filter(appraisal=appraisal).exists())
        self.assertRedirects(
            response, reverse("appraisals:detail", args=[appraisal.pk])
        )

    def test_posting_support_sets_support_type(self):
        self.client.post(self.start_url, {"staff_type": "SUPPORT"})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.staff_type, StaffMember.StaffType.SUPPORT)

    # LEADER must not be self-selectable — it drives a different, admin-set form.
    def test_posting_leader_is_rejected_and_type_stays_blank(self):
        response = self.client.post(self.start_url, {"staff_type": "LEADER"})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.staff_type, "")
        self.assertFalse(Appraisal.objects.filter(teacher=self.staff).exists())
        self.assertRedirects(response, reverse("appraisals:my_appraisal"))

    def test_posting_garbage_is_rejected(self):
        self.client.post(self.start_url, {"staff_type": "banana"})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.staff_type, "")

    def test_posting_no_type_is_rejected(self):
        self.client.post(self.start_url, {})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.staff_type, "")

    # A staff member already classified must never have their type overwritten
    # by a posted value (defence against a crafted POST changing the form used).
    def test_existing_type_is_not_overwritten(self):
        self.staff.staff_type = StaffMember.StaffType.SUPPORT
        self.staff.save()
        self.client.post(self.start_url, {"staff_type": "TEACHING"})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.staff_type, StaffMember.StaffType.SUPPORT)


class StartNextYearTests(TestCase):
    """AcademicYear.start_next() / the start_next_year command advance the cycle."""

    def test_creates_next_year_and_makes_it_current(self):
        prev = AcademicYear.objects.create(start_year=2025, is_current=True)

        year, created = AcademicYear.start_next()

        self.assertTrue(created)
        self.assertEqual(year.start_year, 2026)
        self.assertTrue(year.is_current)
        prev.refresh_from_db()
        self.assertFalse(prev.is_current)

    def test_empty_table_falls_back_to_current_calendar_year(self):
        year, created = AcademicYear.start_next()

        self.assertTrue(created)
        self.assertEqual(year.start_year, date.today().year)
        self.assertTrue(year.is_current)

    def test_activates_pre_created_next_year_without_skipping_or_duplicating(self):
        # A future year pre-created but not yet current: start_next() should make
        # it current rather than skip to 2027 or create a duplicate 2026.
        AcademicYear.objects.create(start_year=2025, is_current=True)
        AcademicYear.objects.create(start_year=2026, is_current=False)

        year, created = AcademicYear.start_next()

        self.assertFalse(created)
        self.assertEqual(year.start_year, 2026)
        self.assertTrue(year.is_current)
        self.assertEqual(AcademicYear.objects.count(), 2)
        self.assertEqual(AcademicYear.objects.filter(is_current=True).count(), 1)

    def test_management_command_advances_year(self):
        AcademicYear.objects.create(start_year=2025, is_current=True)

        call_command("start_next_year")

        self.assertTrue(
            AcademicYear.objects.filter(start_year=2026, is_current=True).exists()
        )
        self.assertEqual(AcademicYear.objects.filter(is_current=True).count(), 1)


class SelfReviewAdminSummaryTests(TestCase):
    """The admin 'Review at a glance' table (render_self_review_table).

    A school admin needs to see each criterion's score and comment together;
    the score lives on the child SelfReviewBullet, which the default admin
    inlines never surface next to the item's evidence. Also guards the
    HTML-escaping of staff-entered text (the table is rendered mark_safe).
    """

    def setUp(self):
        self.staff = make_staff(
            "support@oxlip.test", staff_type=StaffMember.StaffType.SUPPORT
        )
        self.year = make_year()
        self.appraisal = make_appraisal(self.staff, self.year)
        self.self_review = make_self_review(
            self.appraisal, kind=SelfReview.Kind.SUPPORT
        )

    def _bullets(self):
        return list(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.self_review
            ).order_by("self_review_item__order")
        )

    def test_table_shows_scores_and_evidence_together(self):
        bullets = self._bullets()
        bullets[0].score = 2
        bullets[0].save()
        bullets[1].score = 3
        bullets[1].save()
        item1 = self.self_review.items.order_by("order").first()
        item1.evidence = "My evidence for section one."
        item1.save()

        html = render_self_review_table(self.self_review)

        self.assertIn("My evidence for section one.", html)
        # Colour-coded score cells (distinct from the section-code cell, which
        # for support items would also read ">2</td>").
        self.assertIn("color:#b7791f'>2</td>", html)  # amber = 2
        self.assertIn("color:#217a3b'>3</td>", html)  # green = 3
        # A bullet left unanswered shows the muted em-dash, not a number.
        self.assertIn(";color:#999'>—</td>", html)

    def test_staff_entered_html_is_escaped(self):
        item1 = self.self_review.items.order_by("order").first()
        item1.evidence = "<script>alert('x')</script>"
        item1.save()
        bullet = SelfReviewBullet.objects.filter(self_review_item=item1).first()
        bullet.text = "Comply with <b>policy</b> & procedures"
        bullet.save()

        html = render_self_review_table(self.self_review)

        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;policy&lt;/b&gt;", html)
        self.assertIn("&amp;", html)


class LastYearGoalReviewTests(TestCase):
    """Reviewing last year's goals from this year's appraisal page.

    The goal row carries both its setup and its end-of-cycle review, so the
    Last Year tab edits the *previous* appraisal's Goal rows while being gated
    by the *current* appraisal's role and lock. These tests pin that split (the
    one non-obvious rule here) plus the usual teacher/coach field boundary and
    IDOR chokepoint.
    """

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.coach_email = "coach@oxlip.test"
        self.stranger_email = "stranger@oxlip.test"

        self.teacher_user = make_user(self.teacher_email)
        self.coach_user = make_user(self.coach_email)
        self.stranger_user = make_user(self.stranger_email)

        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        make_staff(self.coach_email)
        make_staff(self.stranger_email)

        self.last_year = make_year(2024, is_current=False)
        self.year = make_year(2025)
        self.previous = make_appraisal(
            self.teacher, self.last_year, coach_email=self.coach_email
        )
        self.appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.coach_email
        )

        self.detail_url = reverse(
            "appraisals:detail_tab", args=[self.appraisal.pk, "last-year"]
        )
        self.save_url = reverse("appraisals:last_year_save", args=[self.appraisal.pk])

    def _payload(self, *, teacher_comment="", coach_comment=""):
        """A full, valid POST payload for the last-year formset (prefix "lastyear")."""
        goals = list(self.previous.goals.order_by("order"))
        payload = {
            "lastyear-TOTAL_FORMS": str(len(goals)),
            "lastyear-INITIAL_FORMS": str(len(goals)),
            "lastyear-MIN_NUM_FORMS": "0",
            "lastyear-MAX_NUM_FORMS": "1000",
        }
        for index, goal in enumerate(goals):
            payload[f"lastyear-{index}-id"] = str(goal.pk)
            payload[f"lastyear-{index}-teacher_review_comment"] = teacher_comment
            payload[f"lastyear-{index}-coach_review_comment"] = coach_comment
        return payload

    def _comments(self):
        return [
            (g.teacher_review_comment, g.coach_review_comment)
            for g in self.previous.goals.order_by("order")
        ]

    # The whole point of the feature: the boxes must actually be on the page.
    def test_last_year_tab_renders_editable_comment_boxes(self):
        self.client.force_login(self.teacher_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "lastyear-0-teacher_review_comment")
        self.assertContains(response, self.save_url)

    def test_teacher_can_save_review_of_last_years_goals(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            self.save_url, self._payload(teacher_comment="Met in full."), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(t == "Met in full." for t, _ in self._comments()))

    def test_coach_can_save_review_of_last_years_goals(self):
        self.client.force_login(self.coach_user)
        response = self.client.post(
            self.save_url, self._payload(coach_comment="Agreed."), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(c == "Agreed." for _, c in self._comments()))

    # Field-level gating is the security boundary: a disabled field must ignore
    # whatever the other role posts into it.
    def test_teacher_cannot_write_coach_comment(self):
        self.client.force_login(self.teacher_user)
        self.client.post(
            self.save_url,
            self._payload(teacher_comment="Mine.", coach_comment="Forged."),
            follow=True,
        )
        self.assertTrue(all(t == "Mine." for t, _ in self._comments()))
        self.assertTrue(all(c == "" for _, c in self._comments()))

    def test_coach_cannot_write_teacher_comment(self):
        self.client.force_login(self.coach_user)
        self.client.post(
            self.save_url,
            self._payload(teacher_comment="Forged.", coach_comment="Mine."),
            follow=True,
        )
        self.assertTrue(all(t == "" for t, _ in self._comments()))
        self.assertTrue(all(c == "Mine." for _, c in self._comments()))

    # The deliberate design decision: last year's appraisal is normally already
    # signed off, so honouring *its* lock would make the review unwritable.
    def test_signed_off_previous_year_does_not_block_the_review(self):
        self.previous.status = Appraisal.Status.SIGNED_OFF
        self.previous.save(update_fields=["status"])
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            self.save_url, self._payload(teacher_comment="Reviewed."), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(t == "Reviewed." for t, _ in self._comments()))

    # ...but this year's lock does, like every other section.
    def test_locked_current_appraisal_blocks_the_review(self):
        self.appraisal.status = Appraisal.Status.SIGNED_OFF
        self.appraisal.save(update_fields=["status"])
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            self.save_url, self._payload(teacher_comment="Too late.")
        )
        # Conflict, not denial — see AppraisalLockingTests. Nothing is written,
        # but the comment is echoed back so it can be copied out.
        self.assertEqual(response.status_code, 409)
        self.assertTrue(all(t == "" for t, _ in self._comments()))
        self.assertContains(response, "Too late.", status_code=409)

    # IDOR: the previous appraisal is derived server-side, never taken from the
    # request, so a stranger is stopped at the current appraisal's chokepoint.
    def test_stranger_cannot_save_review(self):
        self.client.force_login(self.stranger_user)
        response = self.client.post(
            self.save_url, self._payload(teacher_comment="Not mine.")
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(all(t == "" for t, _ in self._comments()))

    def test_no_previous_appraisal_is_forbidden_not_a_crash(self):
        other = make_staff(
            "solo@oxlip.test",
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        make_user("solo@oxlip.test")
        first = make_appraisal(other, self.year, coach_email=self.coach_email)
        self.client.force_login(User.objects.get(email="solo@oxlip.test"))

        detail = self.client.get(
            reverse("appraisals:detail_tab", args=[first.pk, "last-year"])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "No previous goal setting and review on record.")

        response = self.client.post(
            reverse("appraisals:last_year_save", args=[first.pk]), {}
        )
        self.assertEqual(response.status_code, 403)


class MovePriorYearGoalReviewsTests(TestCase):
    """The one-off ``move_prior_year_goal_reviews`` data correction.

    This command runs once, against live production data holding named staff
    performance commentary, and moves text between rows. There is no undo
    beyond the backup file, so the tests here are the only safety net it gets.

    The load-bearing rule is ``_edit_reason``: a goal may only move if its
    stored text still equals the ``ImportRow.raw_json`` that wrote it. Anything
    else - a human edit, or no import row at all - is live work, and flags the
    **whole appraisal**. Every fixture below therefore builds real
    ``ImportBatch`` / ``ImportRow`` rows so that comparison is exercised for
    real rather than stubbed.
    """

    def setUp(self):
        self.super_user = make_user("importadmin@oxlip.test", is_superuser=True)

        # 2025/26 is the year whose goals wrongly hold the reviews; 2026/27 is
        # the live current year the containment guarantee protects. 2024/25
        # (the destination) deliberately does NOT exist - the command fabricates
        # it, and the dry-run test asserts it does not.
        self.source_year = make_year(2025, is_current=False)
        self.current_year = make_year(2026, is_current=True)

        self.coach_email = "coach@oxlip.test"
        self.teacher = make_staff(
            "teacher@oxlip.test",
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        self.source = make_appraisal(
            self.teacher, self.source_year, coach_email=self.coach_email
        )

    # --- fixtures ---------------------------------------------------------

    def _confirmed_batch(self):
        """A GOALS batch that has actually been confirmed.

        ``_imported_row`` orders by ``batch__confirmed_at``, so an unconfirmed
        batch would sort unpredictably; set it explicitly.
        """
        return ImportBatch.objects.create(
            import_type=ImportType.GOALS,
            uploaded_by=self.super_user,
            status=ImportBatch.Status.CONFIRMED,
            confirmed_at=timezone.now(),
        )

    def _imported_goal(
        self,
        appraisal,
        order,
        *,
        teacher_text,
        coach_text,
        stored_teacher=None,
        stored_coach=None,
    ):
        """Set a goal's review text and record the ImportRow that "wrote" it.

        Pass ``stored_*`` to make the row on disk differ from what the import
        wrote - i.e. to simulate a human having edited the goal since.
        """
        goal = appraisal.goals.get(order=order)
        goal.teacher_review_comment = (
            teacher_text if stored_teacher is None else stored_teacher
        )
        goal.coach_review_comment = coach_text if stored_coach is None else stored_coach
        goal.save()

        ImportRow.objects.create(
            batch=self._confirmed_batch(),
            import_type=ImportType.GOALS,
            row_number=order,
            raw_json={
                "teacher_email": appraisal.teacher.email,
                "academic_year": str(appraisal.academic_year.start_year),
                "goal_type": goal.goal_type,
                "title": goal.title,
                "teacher_review_comment": teacher_text,
                "coach_review_comment": coach_text,
            },
            outcome=ImportRow.Outcome.UPDATE,
            source_row_hash="hash-%s-%s" % (appraisal.pk, order),
            created_object_model=ImportedModel.GOAL,
            created_object_pk=goal.pk,
        )
        return goal

    def _unimported_goal(self, appraisal, order, *, teacher_text="", coach_text=""):
        """A goal carrying review text with no ImportRow - created in the app."""
        goal = appraisal.goals.get(order=order)
        goal.teacher_review_comment = teacher_text
        goal.coach_review_comment = coach_text
        goal.save()
        return goal

    def _temp_backup(self):
        """A backup path in a temp dir - never inside the repo."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return str(Path(tmp.name) / "goal-review-move.json")

    def _run(
        self,
        *,
        dry_run=False,
        teacher_email="",
        from_year=2025,
        to_year=2024,
        backup_file=None,
    ):
        if backup_file is None:
            backup_file = "" if dry_run else self._temp_backup()
        out, err = StringIO(), StringIO()
        call_command(
            "move_prior_year_goal_reviews",
            dry_run=dry_run,
            from_year=from_year,
            to_year=to_year,
            teacher_email=teacher_email,
            backup_file=backup_file,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    # --- the move itself --------------------------------------------------

    # Catches the correction silently not happening: the review must land on the
    # prior-year goal, the source must be emptied, and the fabricated goal must
    # NOT inherit the 2025/26 goal's title (that would invent a goal for a year
    # the system never held).
    def test_clean_imported_goal_moves_to_prior_year_and_clears_source(self):
        self._imported_goal(
            self.source,
            1,
            teacher_text="I met the standards.",
            coach_text="Agreed, met.",
        )

        self._run()

        target_appraisal = Appraisal.objects.get(
            teacher=self.teacher, academic_year__start_year=2024
        )
        target_goal = target_appraisal.goals.get(order=1)
        self.assertEqual(target_goal.teacher_review_comment, "I met the standards.")
        self.assertEqual(target_goal.coach_review_comment, "Agreed, met.")
        self.assertEqual(target_goal.title, PLACEHOLDER_TITLE)

        source_goal = self.source.goals.get(order=1)
        self.assertEqual(source_goal.teacher_review_comment, "")
        self.assertEqual(source_goal.coach_review_comment, "")

        # The whole point of the move: previous() must now find the reviews.
        self.assertEqual(self.source.previous(), target_appraisal)

    # Catches the fabricated prior-year appraisal asserting the LATER year's
    # coach as the author of an earlier year's review, and catches it being
    # created editable (DRAFT) for a year that predates the system.
    def test_created_target_appraisal_does_not_inherit_coach_email(self):
        self._imported_goal(
            self.source, 1, teacher_text="Teacher text", coach_text="Coach text"
        )

        self._run()

        target = Appraisal.objects.get(
            teacher=self.teacher, academic_year__start_year=2024
        )
        self.assertEqual(target.coach_email, "")
        self.assertNotEqual(target.coach_email, self.source.coach_email)
        self.assertEqual(target.status, Appraisal.Status.SIGNED_OFF)

    # --- the edit guard ---------------------------------------------------

    # Catches the guard being applied per goal instead of per appraisal: one
    # edited goal casts doubt on the whole imported row, so its CLEAN siblings
    # must not move either.
    def test_edited_goal_freezes_its_whole_appraisal_including_clean_siblings(self):
        edited_teacher_text = "The coach rewrote this in the app."
        self._imported_goal(
            self.source,
            1,
            teacher_text="Original imported text",
            coach_text="Original coach text",
            stored_teacher=edited_teacher_text,
        )
        self._imported_goal(
            self.source,
            2,
            teacher_text="Clean sibling",
            coach_text="Clean coach sibling",
        )

        self._run()

        edited = self.source.goals.get(order=1)
        self.assertEqual(edited.teacher_review_comment, edited_teacher_text)
        self.assertEqual(edited.coach_review_comment, "Original coach text")

        sibling = self.source.goals.get(order=2)
        self.assertEqual(sibling.teacher_review_comment, "Clean sibling")
        self.assertEqual(sibling.coach_review_comment, "Clean coach sibling")

        # Nothing was fabricated for the flagged appraisal.
        self.assertFalse(
            Appraisal.objects.filter(academic_year__start_year=2024).exists()
        )
        self.assertFalse(AcademicYear.objects.filter(start_year=2024).exists())

    # Catches live in-app work being treated as import residue: a goal with no
    # ImportRow was typed by a human, so it must never be moved or blanked.
    def test_goal_with_no_import_row_is_left_untouched(self):
        self._unimported_goal(
            self.source,
            1,
            teacher_text="Typed straight into the app.",
            coach_text="Also typed in the app.",
        )

        self._run()

        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Typed straight into the app.")
        self.assertEqual(goal.coach_review_comment, "Also typed in the app.")
        self.assertFalse(
            Appraisal.objects.filter(academic_year__start_year=2024).exists()
        )

    # --- idempotency and containment --------------------------------------

    # Catches a second run duplicating, re-blanking or overwriting the target:
    # after the first run the source is empty, and an empty source must be a
    # complete no-op rather than a move of "".
    def test_second_run_is_a_no_op_and_does_not_disturb_the_moved_review(self):
        self._imported_goal(
            self.source, 1, teacher_text="Moved once.", coach_text="Coach moved once."
        )

        self._run()
        goal_count_after_first = Goal.objects.count()
        appraisal_count_after_first = Appraisal.objects.count()

        self._run()

        self.assertEqual(Goal.objects.count(), goal_count_after_first)
        self.assertEqual(Appraisal.objects.count(), appraisal_count_after_first)

        target_goal = Goal.objects.get(
            appraisal__teacher=self.teacher,
            appraisal__academic_year__start_year=2024,
            order=1,
        )
        self.assertEqual(target_goal.teacher_review_comment, "Moved once.")
        self.assertEqual(target_goal.coach_review_comment, "Coach moved once.")

        source_goal = self.source.goals.get(order=1)
        self.assertEqual(source_goal.teacher_review_comment, "")

    # Catches the worst possible outcome: source text deleted after the write to
    # an already-occupied target was declined, losing the review entirely.
    def test_occupied_target_goal_does_not_blank_the_source(self):
        target_year = make_year(2024, is_current=False)
        target = make_appraisal(
            self.teacher, target_year, coach_email="oldcoach@oxlip.test"
        )
        occupied = target.goals.get(order=1)
        occupied.teacher_review_comment = "Genuine 2024/25 review, written in the app."
        occupied.coach_review_comment = "Genuine 2024/25 coach review."
        occupied.save()

        self._imported_goal(
            self.source,
            1,
            teacher_text="Misfiled teacher text",
            coach_text="Misfiled coach text",
        )
        # A second, unblocked goal so the appraisal is still movable and the
        # apply path really runs.
        self._imported_goal(
            self.source,
            2,
            teacher_text="Movable teacher text",
            coach_text="Movable coach text",
        )

        self._run()

        source_blocked = self.source.goals.get(order=1)
        self.assertEqual(source_blocked.teacher_review_comment, "Misfiled teacher text")
        self.assertEqual(source_blocked.coach_review_comment, "Misfiled coach text")

        occupied.refresh_from_db()
        self.assertEqual(
            occupied.teacher_review_comment,
            "Genuine 2024/25 review, written in the app.",
        )

        # The unblocked sibling still moved.
        self.assertEqual(self.source.goals.get(order=2).teacher_review_comment, "")
        self.assertEqual(
            target.goals.get(order=2).teacher_review_comment, "Movable teacher text"
        )

    # Catches --dry-run writing: the whole point of previewing a one-off
    # correction on production data is that it cannot touch anything.
    def test_dry_run_writes_nothing_at_all(self):
        self._imported_goal(
            self.source,
            1,
            teacher_text="Preview only.",
            coach_text="Preview coach only.",
        )

        before = (
            AcademicYear.objects.count(),
            Appraisal.objects.count(),
            Goal.objects.count(),
        )

        self._run(dry_run=True)

        after = (
            AcademicYear.objects.count(),
            Appraisal.objects.count(),
            Goal.objects.count(),
        )
        self.assertEqual(before, after)
        self.assertFalse(AcademicYear.objects.filter(start_year=2024).exists())

        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Preview only.")
        self.assertEqual(goal.coach_review_comment, "Preview coach only.")

    # Catches the containment guarantee failing: the two fields this command
    # clears are the same two the CURRENT year's "Last Year" tab writes into, so
    # a scoping slip would delete coaches' in-progress work.
    #
    # The current-year goals here are deliberately given matching ImportRows, so
    # the edit guard would happily let them through. The ONLY thing standing
    # between them and a blanking is the --from-year filter - which is exactly
    # what this test is for. They also use goal orders the source does not, so a
    # scope slip cannot be masked by the target-occupied guard instead.
    def test_goals_outside_the_from_year_are_never_written(self):
        current = make_appraisal(
            self.teacher, self.current_year, coach_email=self.coach_email
        )
        live_teacher_text = "In-progress 2026/27 review of the 2025/26 goals."
        live_coach_text = "In-progress 2026/27 coach comment."
        for order in (2, 3):
            self._imported_goal(
                current,
                order,
                teacher_text="%s (%s)" % (live_teacher_text, order),
                coach_text="%s (%s)" % (live_coach_text, order),
            )

        self._imported_goal(
            self.source,
            1,
            teacher_text="Misfiled text",
            coach_text="Misfiled coach text",
        )

        self._run()

        for order in (2, 3):
            goal = current.goals.get(order=order)
            self.assertEqual(
                goal.teacher_review_comment, "%s (%s)" % (live_teacher_text, order)
            )
            self.assertEqual(
                goal.coach_review_comment, "%s (%s)" % (live_coach_text, order)
            )

        # And nothing was fabricated against the current year either.
        self.assertEqual(
            Appraisal.objects.filter(academic_year=self.current_year).count(), 1
        )

    # --- argument guards --------------------------------------------------

    # Catches a wider-than-one-year move "succeeding": previous() only looks one
    # year back, so the reviews would land somewhere no view can reach them.
    def test_non_adjacent_years_are_refused(self):
        self._imported_goal(
            self.source, 1, teacher_text="Text", coach_text="Coach text"
        )

        with self.assertRaises(CommandError):
            self._run(from_year=2025, to_year=2023)
        with self.assertRaises(CommandError):
            self._run(from_year=2025, to_year=2025)

        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Text")

    # Catches the before-state either not being recorded at all, or being
    # written into the repo - the file holds named staff performance commentary
    # and a commit of this repo is a live deploy.
    def test_real_run_requires_a_backup_file_outside_the_repository(self):
        self._imported_goal(
            self.source, 1, teacher_text="Text", coach_text="Coach text"
        )

        with self.assertRaises(CommandError):
            self._run(backup_file="")

        inside_repo = str(Path(settings.BASE_DIR) / "goal-review-move.json")
        with self.assertRaises(CommandError):
            self._run(backup_file=inside_repo)
        self.assertFalse(Path(inside_repo).exists())

        # Nothing moved on either refusal.
        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Text")

        # A path outside the repo is accepted and the record is written.
        outside = self._temp_backup()
        self._run(backup_file=outside)
        self.assertTrue(Path(outside).exists())

    # Catches --teacher-email being ignored, which would turn a cautious
    # one-person trial run into a full trust-wide write.
    def test_teacher_email_limits_the_run_to_that_teacher(self):
        other = make_staff(
            "other@oxlip.test",
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        other_source = make_appraisal(
            other, self.source_year, coach_email=self.coach_email
        )

        self._imported_goal(
            self.source,
            1,
            teacher_text="Trialled teacher text",
            coach_text="Trialled coach text",
        )
        self._imported_goal(
            other_source,
            1,
            teacher_text="Untouched teacher text",
            coach_text="Untouched coach text",
        )

        self._run(teacher_email=self.teacher.email)

        self.assertEqual(self.source.goals.get(order=1).teacher_review_comment, "")
        self.assertEqual(
            other_source.goals.get(order=1).teacher_review_comment,
            "Untouched teacher text",
        )
        self.assertEqual(
            other_source.goals.get(order=1).coach_review_comment,
            "Untouched coach text",
        )
        self.assertFalse(
            Appraisal.objects.filter(
                teacher=other, academic_year__start_year=2024
            ).exists()
        )


class MoveGoalReviewsAdminActionTests(TestCase):
    """The browser front end for the same one-off correction.

    ``AcademicYearAdmin.move_misplaced_goal_reviews`` is the second front end
    onto ``goal_review_fix`` (the management command above is the first). The
    core decision rules are tested there; what needs its own coverage here is
    everything the admin adds — the staff/superuser gate, the exactly-one-year
    guard, and above all the fact that reaching the preview page writes
    nothing. That preview is the operator's only chance to change their mind
    about an irreversible move of named performance commentary.

    The ImportBatch/ImportRow fixtures are borrowed wholesale from
    ``MovePriorYearGoalReviewsTests`` rather than re-invented, so both front
    ends are exercised against identical data.
    """

    _confirmed_batch = MovePriorYearGoalReviewsTests._confirmed_batch
    _imported_goal = MovePriorYearGoalReviewsTests._imported_goal
    _unimported_goal = MovePriorYearGoalReviewsTests._unimported_goal

    def setUp(self):
        MovePriorYearGoalReviewsTests.setUp(self)
        self.changelist_url = reverse("admin:appraisals_academicyear_changelist")

    # --- helpers ----------------------------------------------------------

    def _counts(self):
        return (
            AcademicYear.objects.count(),
            Appraisal.objects.count(),
            Goal.objects.count(),
        )

    def _post_action(self, *years, confirm=False, approved=None, follow=False):
        """POST the action exactly as the changelist / confirmation page does.

        Without ``confirm`` this is the changelist dropdown submission (which
        carries ``index``); with it, it is the hidden-field form on the
        confirmation page (which does not, but does carry one
        ``approved_goal`` per goal the operator was shown).
        """
        data = {
            "action": "move_misplaced_goal_reviews",
            ACTION_CHECKBOX_NAME: [str(year.pk) for year in years],
        }
        if confirm:
            data["confirm"] = "yes"
            data["approved_goal"] = [str(goal.pk) for goal in (approved or [])]
        else:
            data["index"] = "0"
        return self.client.post(self.changelist_url, data, follow=follow)

    def _staff_user(self, email, codename):
        """An admin-site user with exactly one permission on AcademicYear."""
        user = make_user(email)
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        user.user_permissions.add(
            Permission.objects.get(
                codename=codename, content_type__app_label="appraisals"
            )
        )
        return user

    def _approve_everything(self, year):
        """The goal pks the confirmation page would render for this year."""
        return [
            move.goal
            for plan in build_plan(year, year.start_year - 1)
            for move in plan.moves
        ]

    # --- the gate ---------------------------------------------------------

    # Catches the admin gate being the only thing standing in front of an
    # irreversible bulk rewrite of performance commentary: a logged-in but
    # non-staff user must not reach the action at all, let alone run it.
    def test_non_superuser_cannot_reach_the_admin_action(self):
        ordinary = make_user("classroom@oxlip.test")
        self._imported_goal(
            self.source, 1, teacher_text="Misfiled text", coach_text="Misfiled coach"
        )
        before = self._counts()

        self.client.force_login(ordinary)
        response = self._post_action(
            self.source_year, confirm=True, approved=self.source.goals.all()
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)
        self.assertEqual(self._counts(), before)
        self.assertEqual(
            self.source.goals.get(order=1).teacher_review_comment, "Misfiled text"
        )

    # --- the exactly-one-year guard ---------------------------------------

    # Catches a multi-year selection running anyway: the action moves reviews
    # back exactly one year, so a two-year selection has no coherent meaning
    # and must refuse rather than silently pick one.
    def test_selecting_more_than_one_year_refuses_and_writes_nothing(self):
        self._imported_goal(
            self.source, 1, teacher_text="Misfiled text", coach_text="Misfiled coach"
        )
        before = self._counts()

        self.client.force_login(self.super_user)
        response = self._post_action(
            self.source_year,
            self.current_year,
            confirm=True,
            approved=self.source.goals.all(),
            follow=True,
        )

        # Nothing moved, and nothing was fabricated to move it into.
        self.assertEqual(self._counts(), before)
        self.assertFalse(AcademicYear.objects.filter(start_year=2024).exists())
        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Misfiled text")
        self.assertEqual(goal.coach_review_comment, "Misfiled coach")

        # And the operator was told why, rather than left guessing.
        self.assertEqual(response.status_code, 200)
        notes = [str(message) for message in response.context["messages"]]
        self.assertTrue(
            any("Select exactly one academic year" in note for note in notes), notes
        )

    # --- the preview ------------------------------------------------------

    # The most important test in this class. Catches the preview writing: an
    # operator opening the confirmation page to read what WOULD happen must not
    # thereby have done it. The admin equivalent of the command's --dry-run
    # guarantee, and the only reason the two-step flow exists.
    def test_preview_without_confirm_writes_absolutely_nothing(self):
        self._imported_goal(
            self.source,
            1,
            teacher_text="Preview only.",
            coach_text="Preview coach only.",
        )
        before = self._counts()

        self.client.force_login(self.super_user)
        response = self._post_action(self.source_year)

        # The confirmation page rendered, rather than redirecting back.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Move misplaced goal reviews")

        self.assertEqual(self._counts(), before)
        self.assertFalse(AcademicYear.objects.filter(start_year=2024).exists())
        self.assertFalse(
            Appraisal.objects.filter(academic_year__start_year=2024).exists()
        )

        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Preview only.")
        self.assertEqual(goal.coach_review_comment, "Preview coach only.")

    # --- confirming -------------------------------------------------------

    # Catches the confirmed move either not happening or happening without a
    # record: the JSON download is the operator's only "before" copy of text
    # this move blanks at source, so it must arrive as an attachment, parse,
    # and actually contain the moved wording.
    def test_confirming_moves_the_reviews_and_returns_the_json_record(self):
        self._imported_goal(
            self.source,
            1,
            teacher_text="I met the standards.",
            coach_text="Agreed, met.",
        )

        approved = self._approve_everything(self.source_year)
        self.assertEqual(len(approved), 1)

        self.client.force_login(self.super_user)
        response = self._post_action(
            self.source_year, confirm=True, approved=approved
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(".json", response["Content-Disposition"])

        record = json.loads(response.content.decode("utf-8"))
        self.assertEqual(record["moved_goals"], 1)
        self.assertEqual(record["moved_appraisals"], 1)
        moved = record["appraisals"][0]["goals"][0]
        self.assertEqual(moved["teacher_review_comment"], "I met the standards.")
        self.assertEqual(moved["coach_review_comment"], "Agreed, met.")

        # And the database really changed, not merely the download.
        target_goal = Goal.objects.get(
            appraisal__teacher=self.teacher,
            appraisal__academic_year__start_year=2024,
            order=1,
        )
        self.assertEqual(target_goal.teacher_review_comment, "I met the standards.")
        self.assertEqual(target_goal.coach_review_comment, "Agreed, met.")

        source_goal = self.source.goals.get(order=1)
        self.assertEqual(source_goal.teacher_review_comment, "")
        self.assertEqual(source_goal.coach_review_comment, "")

    # Catches the edit guard living in the command rather than in the shared
    # logic: an appraisal edited since the import may be a coach's own work,
    # and the admin path must skip the whole appraisal exactly as the command
    # does — including its clean siblings.
    def test_flagged_appraisal_is_skipped_by_the_admin_action_too(self):
        edited_text = "The coach rewrote this in the app."
        self._imported_goal(
            self.source,
            1,
            teacher_text="Original imported text",
            coach_text="Original coach text",
            stored_teacher=edited_text,
        )
        self._imported_goal(
            self.source, 2, teacher_text="Clean sibling", coach_text="Clean coach"
        )

        self.client.force_login(self.super_user)
        response = self._post_action(
            self.source_year, confirm=True, approved=self.source.goals.all()
        )

        record = json.loads(response.content.decode("utf-8"))
        self.assertEqual(record["moved_goals"], 0)
        self.assertEqual(record["moved_appraisals"], 0)
        self.assertEqual(record["appraisals"], [])

        edited = self.source.goals.get(order=1)
        self.assertEqual(edited.teacher_review_comment, edited_text)
        self.assertEqual(edited.coach_review_comment, "Original coach text")
        sibling = self.source.goals.get(order=2)
        self.assertEqual(sibling.teacher_review_comment, "Clean sibling")
        self.assertEqual(sibling.coach_review_comment, "Clean coach")

        # Nothing was fabricated to hold them.
        self.assertFalse(
            Appraisal.objects.filter(academic_year__start_year=2024).exists()
        )
        self.assertEqual(Goal.objects.filter(appraisal=self.source).count(), 3)

    # Catches the action being offered to a view-only staff user. Django's
    # ``permissions=["change"]`` keeps it out of the dropdown, so the POST is
    # not recognised as an action at all — the UI tidy-up half of the gate.
    def test_view_only_staff_user_is_not_offered_the_action(self):
        readonly = self._staff_user("officeadmin@oxlip.test", "view_academicyear")
        self._imported_goal(
            self.source, 1, teacher_text="Misfiled text", coach_text="Misfiled coach"
        )
        before = self._counts()

        self.client.force_login(readonly)
        response = self._post_action(
            self.source_year,
            confirm=True,
            approved=self.source.goals.all(),
            follow=True,
        )

        notes = [str(message) for message in response.context["messages"]]
        self.assertIn("No action selected.", notes)
        self.assertEqual(self._counts(), before)
        self.assertEqual(
            self.source.goals.get(order=1).teacher_review_comment, "Misfiled text"
        )

    # Catches the real gap: ``permissions=["change"]`` is only a dropdown
    # filter, so a staff user who legitimately edits Academic years reaches the
    # action — and change permission on AcademicYear is the wrong model to gate
    # on, because the action rewrites Appraisal and Goal across the whole trust
    # and hands back every affected staff member's commentary as a download.
    # The explicit superuser check is the security boundary; this is its test.
    def test_staff_user_with_change_permission_is_still_refused(self):
        editor = self._staff_user("yearadmin@oxlip.test", "change_academicyear")
        self._imported_goal(
            self.source, 1, teacher_text="Misfiled text", coach_text="Misfiled coach"
        )
        before = self._counts()

        self.client.force_login(editor)
        response = self._post_action(
            self.source_year, confirm=True, approved=self.source.goals.all()
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._counts(), before)
        goal = self.source.goals.get(order=1)
        self.assertEqual(goal.teacher_review_comment, "Misfiled text")
        self.assertEqual(goal.coach_review_comment, "Misfiled coach")
        # No commentary leaked into the refusal either.
        self.assertNotContains(response, "Misfiled", status_code=403)

    # Catches the applied set being wider than the approved one. The plan is
    # rebuilt on confirm, so a goal can ENTER it between preview and confirm
    # (e.g. a goals import confirmed in the interim gives a previously
    # unimported goal an ImportRow). The operator approved a named list; only
    # that list may be written, and the rest must be reported rather than moved.
    def test_goal_that_appeared_after_the_preview_is_reported_not_moved(self):
        approved_goal = self._imported_goal(
            self.source, 1, teacher_text="Seen at preview.", coach_text="Coach seen."
        )
        # Not in the approved list: it became movable after the page was drawn.
        self._imported_goal(
            self.source,
            2,
            teacher_text="Appeared afterwards.",
            coach_text="Coach appeared afterwards.",
        )

        self.client.force_login(self.super_user)
        response = self._post_action(
            self.source_year, confirm=True, approved=[approved_goal]
        )

        record = json.loads(response.content.decode("utf-8"))
        self.assertEqual(record["moved_goals"], 1)
        self.assertEqual(
            record["not_applied_new_since_preview"],
            [{"teacher_email": self.teacher.email, "goal_order": 2}],
        )

        self.assertEqual(self.source.goals.get(order=1).teacher_review_comment, "")
        unapproved = self.source.goals.get(order=2)
        self.assertEqual(unapproved.teacher_review_comment, "Appeared afterwards.")
        self.assertEqual(
            unapproved.coach_review_comment, "Coach appeared afterwards."
        )
        self.assertFalse(
            Goal.objects.filter(
                appraisal__academic_year__start_year=2024, order=2
            ).exists()
        )


class ApplyPlanNoOpTests(TestCase):
    """A run with nothing to move must not leave a stray academic year behind.

    ``apply_plan`` used to create the target year before checking whether
    anything was movable. The management command short-circuits earlier so never
    reached it, but the admin action does — and a plan narrowed to nothing (every
    appraisal failing the re-check, or restrict_to_approved filtering it empty)
    is an ordinary way to get there. AcademicYear drives the current/previous
    split and check_readiness, so a spurious empty year is not harmless.
    """

    def test_empty_plan_creates_no_academic_year(self):
        from .goal_review_fix import apply_plan

        self.assertFalse(AcademicYear.objects.filter(start_year=2024).exists())

        record = apply_plan([], 2024)

        self.assertFalse(
            AcademicYear.objects.filter(start_year=2024).exists(),
            "a no-op run fabricated an AcademicYear",
        )
        self.assertEqual(record["moved_goals"], 0)
        self.assertEqual(record["moved_appraisals"], 0)
        self.assertFalse(record["target_year_created"])


class SignOffConflictHandBackTests(TestCase):
    """A save refused by the LOCK is a 409 hand-back; refused by ROLE is a 403.

    A coach can sign off while the teacher is still typing. The teacher's POST
    then fails a gate that passed when the page was loaded, and the old
    behaviour raised PermissionDenied before anything was re-rendered: an
    afternoon of writing was discarded behind a page that read as though the
    teacher had never had access to their own record.

    The fix hands the submitted text back at 409 instead. The risk it creates
    is that the new branch widens access, so both halves are pinned here: the
    conflict path is reachable only by someone who still holds a role and is
    blocked purely by the lock, and everybody else — a stranger, or a
    role-holder posting into the *other* role's section — still gets a plain
    403 with none of their text echoed. Nothing is written on either path.
    """

    CONFLICT_STATUS = 409

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.coach_email = "coach@oxlip.test"
        self.stranger_email = "stranger@oxlip.test"

        self.teacher_user = make_user(self.teacher_email)
        self.coach_user = make_user(self.coach_email)
        self.stranger_user = make_user(self.stranger_email)

        self.teacher = make_staff(
            self.teacher_email,
            performance_manager_email=self.coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        make_staff(self.coach_email)
        make_staff(self.stranger_email)

        self.last_year = make_year(2024, is_current=False)
        self.year = make_year(2025)
        self.previous = make_appraisal(
            self.teacher, self.last_year, coach_email=self.coach_email
        )
        self.appraisal = make_appraisal(
            self.teacher, self.year, coach_email=self.coach_email
        )
        self.self_review = make_self_review(self.appraisal)

        self.self_review_url = reverse(
            "appraisals:self_review_save", args=[self.appraisal.pk]
        )
        self.goals_url = reverse("appraisals:goals_save", args=[self.appraisal.pk])
        self.summary_url = reverse("appraisals:summary_save", args=[self.appraisal.pk])
        self.last_year_url = reverse(
            "appraisals:last_year_save", args=[self.appraisal.pk]
        )

    def _sign_off(self):
        """Simulate the coach signing off after the other party loaded the page."""
        self.appraisal.status = Appraisal.Status.SIGNED_OFF
        self.appraisal.save(update_fields=["status"])

    def _self_review_payload(self, evidence):
        items = list(self.self_review.items.all())
        bullets = list(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.self_review
            ).order_by("self_review_item__order", "order")
        )
        payload = {
            "job_summary": "",
            "level_description": "",
            "upr_declaration_agreed": "",
            "signed_name": "",
            "signed_date": "",
            "items-TOTAL_FORMS": str(len(items)),
            "items-INITIAL_FORMS": str(len(items)),
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "bullets-TOTAL_FORMS": str(len(bullets)),
            "bullets-INITIAL_FORMS": str(len(bullets)),
            "bullets-MIN_NUM_FORMS": "0",
            "bullets-MAX_NUM_FORMS": "1000",
        }
        for index, item in enumerate(items):
            payload[f"items-{index}-id"] = str(item.pk)
            payload[f"items-{index}-evidence"] = evidence
        for index, bullet in enumerate(bullets):
            payload[f"bullets-{index}-id"] = str(bullet.pk)
            payload[f"bullets-{index}-score"] = "3"
        return payload

    def _goals_payload(self, title):
        goals = list(self.appraisal.goals.order_by("order"))
        payload = {
            "goals-TOTAL_FORMS": str(len(goals)),
            "goals-INITIAL_FORMS": str(len(goals)),
            "goals-MIN_NUM_FORMS": "0",
            "goals-MAX_NUM_FORMS": "1000",
        }
        for index, goal in enumerate(goals):
            payload[f"goals-{index}-id"] = str(goal.pk)
            payload[f"goals-{index}-title"] = title
            payload[f"goals-{index}-steps_to_success"] = ""
            payload[f"goals-{index}-success_criteria"] = ""
            payload[f"goals-{index}-teacher_review_comment"] = ""
            payload[f"goals-{index}-coach_review_comment"] = ""
        return payload

    def _summary_payload(self, coach_comment):
        return {
            "cpd_requirements": "",
            "summary_teacher_comment": "",
            "summary_coach_comment": coach_comment,
            "on_upper_pay_range": "false",
            "self_review_form_completed": "false",
            "engaged_with_professional_growth": "false",
            "coach_supports_pay_award": "",
            "job_description_review_needed": "false",
            "status": Appraisal.Status.SIGNED_OFF,
        }

    def _last_year_payload(self, teacher_comment):
        goals = list(self.previous.goals.order_by("order"))
        payload = {
            "lastyear-TOTAL_FORMS": str(len(goals)),
            "lastyear-INITIAL_FORMS": str(len(goals)),
            "lastyear-MIN_NUM_FORMS": "0",
            "lastyear-MAX_NUM_FORMS": "1000",
        }
        for index, goal in enumerate(goals):
            payload[f"lastyear-{index}-id"] = str(goal.pk)
            payload[f"lastyear-{index}-teacher_review_comment"] = teacher_comment
            payload[f"lastyear-{index}-coach_review_comment"] = ""
        return payload

    def _bullet_scores(self):
        return [
            b.score
            for b in SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.self_review
            )
        ]

    # (a) Catches the conflict being reported as a flat permission denial, which
    # is both untrue and the shape that threw the text away.
    def test_self_review_save_after_sign_off_returns_409_not_403(self):
        self._sign_off()
        self.client.force_login(self.teacher_user)

        response = self.client.post(
            self.self_review_url, self._self_review_payload("Moderated in November.")
        )

        self.assertEqual(response.status_code, self.CONFLICT_STATUS)

    # (b) Catches the hand-back page rendering without the user's own words on
    # it — a 409 that loses the text is no better than the 403 it replaced.
    def test_self_review_conflict_response_shows_the_submitted_text(self):
        self._sign_off()
        self.client.force_login(self.teacher_user)

        response = self.client.post(
            self.self_review_url,
            self._self_review_payload("Book scrutiny evidence from the autumn term."),
        )

        self.assertContains(
            response,
            "Book scrutiny evidence from the autumn term.",
            status_code=self.CONFLICT_STATUS,
        )

    # (c) Catches the hand-back becoming a write: the record is signed off, so
    # echoing the text back must not also persist any of it.
    def test_self_review_conflict_writes_nothing_to_the_database(self):
        self._sign_off()
        self.client.force_login(self.teacher_user)

        self.client.post(
            self.self_review_url, self._self_review_payload("Must not be stored.")
        )

        self.assertTrue(all(i.evidence == "" for i in self.self_review.items.all()))
        self.assertTrue(all(score is None for score in self._bullet_scores()))

    # (d) THE ONE THAT MATTERS: the conflict branch must not have widened
    # access. A stranger has no role at all, so every save endpoint still stops
    # them at get_appraisal_or_403 with a plain 403 — and, since the hand-back
    # page is rendered for someone who may have just lost access, nothing they
    # posted is echoed back to them either.
    def test_stranger_still_gets_403_from_every_save_endpoint_when_locked(self):
        self._sign_off()
        self.client.force_login(self.stranger_user)
        probe = "Text typed by somebody with no role on this record."

        for url in (
            self.self_review_url,
            self.goals_url,
            self.summary_url,
            self.last_year_url,
        ):
            with self.subTest(url=url):
                response = self.client.post(url, {"summary_teacher_comment": probe})
                self.assertEqual(response.status_code, 403)
                self.assertNotContains(response, probe, status_code=403)

    # ...and the same for a role-holder posting into the OTHER role's section:
    # the coach never had the teacher's self-review fields, so the lock is not
    # the reason they are refused and this stays a 403.
    def test_coach_still_gets_403_from_the_teacher_only_self_review_when_locked(self):
        self._sign_off()
        self.client.force_login(self.coach_user)

        response = self.client.post(
            self.self_review_url, self._self_review_payload("Coach writing here.")
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(all(i.evidence == "" for i in self.self_review.items.all()))

    # Catches the goals endpoint keeping the old discard-behind-403 behaviour.
    def test_goals_save_after_sign_off_hands_the_text_back_and_saves_nothing(self):
        self._sign_off()
        self.client.force_login(self.teacher_user)
        original = [g.title for g in self.appraisal.goals.order_by("order")]

        response = self.client.post(
            self.goals_url, self._goals_payload("Rewritten goal title from the teacher.")
        )

        self.assertContains(
            response,
            "Rewritten goal title from the teacher.",
            status_code=self.CONFLICT_STATUS,
        )
        self.assertEqual(
            [g.title for g in self.appraisal.goals.order_by("order")], original
        )

    # Catches the summary endpoint keeping it — the coach's own sign-off locks
    # the record under their next keystroke, so they hit this too.
    def test_summary_save_after_sign_off_hands_the_text_back_and_saves_nothing(self):
        self._sign_off()
        self.client.force_login(self.coach_user)

        response = self.client.post(
            self.summary_url,
            self._summary_payload("Final coach comment written after signing off."),
        )

        self.assertContains(
            response,
            "Final coach comment written after signing off.",
            status_code=self.CONFLICT_STATUS,
        )
        self.appraisal.refresh_from_db()
        self.assertEqual(self.appraisal.summary_coach_comment, "")

    # Catches the last-year endpoint keeping it. Note this tab writes into the
    # PREVIOUS appraisal's goals, so "saves nothing" is asserted there.
    def test_last_year_save_after_sign_off_hands_the_text_back_and_saves_nothing(self):
        self._sign_off()
        self.client.force_login(self.teacher_user)

        response = self.client.post(
            self.last_year_url,
            self._last_year_payload("Reflection on last year written too late."),
        )

        self.assertContains(
            response,
            "Reflection on last year written too late.",
            status_code=self.CONFLICT_STATUS,
        )
        self.assertTrue(
            all(
                g.teacher_review_comment == ""
                for g in self.previous.goals.order_by("order")
            )
        )


class ValidationErrorVisibilityTests(TestCase):
    """A rejected save must show WHY on the page it re-renders.

    The original fault: posting a signed_name over its 200-character limit
    failed the whole self-review save, and the page came back saying "Please
    correct the errors below." with no error anywhere below it. Because
    _save_section requires every target form to be valid before saving any of
    them, a whole self-review's evidence and scores went unsaved with nothing
    on screen to say what was wrong — so the user's only recourse was to press
    Save again, and lose it again.

    These assert on the RENDERED RESPONSE, not on form.errors. form.errors was
    always populated; the bug was that the template never printed it.
    """

    def setUp(self):
        self.teacher_email = "teacher@oxlip.test"
        self.teacher_user = make_user(self.teacher_email)
        self.teacher = make_staff(
            self.teacher_email, staff_type=StaffMember.StaffType.TEACHING
        )
        self.year = make_year()
        self.appraisal = make_appraisal(self.teacher, self.year)
        self.self_review = make_self_review(self.appraisal)
        self.save_url = reverse("appraisals:self_review_save", args=[self.appraisal.pk])
        self.client.force_login(self.teacher_user)

    def _payload(self, **overrides):
        items = list(self.self_review.items.all())
        bullets = list(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self.self_review
            ).order_by("self_review_item__order", "order")
        )
        payload = {
            "job_summary": "",
            "level_description": "",
            "upr_declaration_agreed": "",
            "signed_name": "",
            "signed_date": "",
            "items-TOTAL_FORMS": str(len(items)),
            "items-INITIAL_FORMS": str(len(items)),
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "bullets-TOTAL_FORMS": str(len(bullets)),
            "bullets-INITIAL_FORMS": str(len(bullets)),
            "bullets-MIN_NUM_FORMS": "0",
            "bullets-MAX_NUM_FORMS": "1000",
        }
        for index, item in enumerate(items):
            payload[f"items-{index}-id"] = str(item.pk)
            payload[f"items-{index}-evidence"] = "Evidence the user typed."
        for index, bullet in enumerate(bullets):
            payload[f"bullets-{index}-id"] = str(bullet.pk)
            payload[f"bullets-{index}-score"] = "2"
        payload.update(overrides)
        return payload

    # Catches the exact reported bug: an invisible signed_name length error.
    def test_over_long_signed_name_error_is_rendered_on_the_page(self):
        response = self.client.post(self.save_url, self._payload(signed_name="x" * 201))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "200 characters")

    # Catches the "correct the errors below" banner being shown with nothing
    # below it — the two must not be able to drift apart again.
    def test_error_banner_is_never_shown_without_a_visible_error(self):
        response = self.client.post(self.save_url, self._payload(signed_name="x" * 201))

        self.assertContains(response, "Please correct the errors below.")
        self.assertContains(response, "form-error-summary")

    # Catches a failure buried inside a formset ROW being invisible — the
    # formset half of the same fix (core/_formset_errors.html). The summary
    # block is asserted by name as well as by message, because the score widget
    # happens to print its own errors inline: without that second assertion the
    # test would pass with the shared include deleted, which is exactly the
    # regression it exists to catch on every OTHER field in the formset.
    def test_formset_row_error_is_rendered_in_the_summary_block(self):
        response = self.client.post(self.save_url, self._payload(**{"bullets-0-score": "9"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertContains(response, "form-error-summary")

    # The reason the error has to be visible: one bad field blocks the whole
    # tab, so a user with no error on screen loses everything else they typed.
    def test_a_single_invalid_field_blocks_the_whole_self_review_save(self):
        self.client.post(self.save_url, self._payload(signed_name="x" * 201))

        self.assertTrue(all(i.evidence == "" for i in self.self_review.items.all()))
        self.assertTrue(
            all(
                b.score is None
                for b in SelfReviewBullet.objects.filter(
                    self_review_item__self_review=self.self_review
                )
            )
        )
