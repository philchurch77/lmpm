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

from datetime import date

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import StaffMember

from .leader_standards_templates import HEADTEACHER_STANDARDS
from .models import (
    AcademicYear,
    Appraisal,
    LeaderGoal,
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
        self.assertEqual(response.status_code, 403)

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
    """LeaderReview.seed_standards(): the 10 Headteacher Standards, seeded once."""

    def setUp(self):
        self.leader = make_staff(
            "head@oxlip.test", staff_type=StaffMember.StaffType.LEADER
        )
        self.year = make_year()
        self.appraisal = make_appraisal(self.leader, self.year)

    # Catches the standard count drifting from the template constant.
    def test_seed_creates_ten_standards(self):
        leader_review = make_leader_review(self.appraisal)
        self.assertEqual(leader_review.standards.count(), len(HEADTEACHER_STANDARDS))
        self.assertEqual(leader_review.standards.count(), 10)

    # Catches number/title/descriptor content or ordering drifting from the
    # template tuples (e.g. an off-by-one in enumerate, or fields swapped).
    def test_seed_round_trips_number_title_and_descriptors(self):
        leader_review = make_leader_review(self.appraisal)
        first_number, first_title, first_descriptors = HEADTEACHER_STANDARDS[0]
        standard = leader_review.standards.get(number=first_number)
        self.assertEqual(standard.order, 1)
        self.assertEqual(standard.title, first_title)
        self.assertEqual(standard.descriptor_list, list(first_descriptors))
        self.assertIsNone(standard.score)
        self.assertFalse(standard.not_applicable)

    # Catches repeated _ensure_leader_review calls (every GET) duplicating rows.
    def test_seed_called_twice_does_not_duplicate(self):
        leader_review = make_leader_review(self.appraisal)
        leader_review.seed_standards()
        self.assertEqual(leader_review.standards.count(), 10)


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
        self.assertEqual(leader_review.standards.count(), 10)


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

    def _payload(self, *, score="2", examples="", na_index=None, goals=None):
        """A full, valid POST payload for the standards + leader-goals formsets.

        The standards inline formset uses the default prefix "standards"
        (derived from LeaderReview.standards); the goals formset is bound with
        the explicit prefix "leadergoals" in the view (to avoid colliding with
        the appraisal's own "goals" formset on the same page). ``goals`` is a
        list of field dicts; any dict carrying an "id" is treated as an existing
        (INITIAL) row.
        """
        standards = list(self.leader_review.standards.order_by("order"))
        payload = {
            "standards-TOTAL_FORMS": str(len(standards)),
            "standards-INITIAL_FORMS": str(len(standards)),
            "standards-MIN_NUM_FORMS": "0",
            "standards-MAX_NUM_FORMS": "1000",
        }
        for index, standard in enumerate(standards):
            payload[f"standards-{index}-id"] = str(standard.pk)
            payload[f"standards-{index}-score"] = score
            payload[f"standards-{index}-not_applicable"] = (
                "true" if na_index == index else "false"
            )
            payload[f"standards-{index}-examples"] = examples

        goals = goals or []
        initial = sum(1 for goal in goals if goal.get("id"))
        payload.update(
            {
                "leadergoals-TOTAL_FORMS": str(len(goals)),
                "leadergoals-INITIAL_FORMS": str(initial),
                "leadergoals-MIN_NUM_FORMS": "0",
                "leadergoals-MAX_NUM_FORMS": "1000",
            }
        )
        for index, goal in enumerate(goals):
            for field, value in goal.items():
                payload[f"leadergoals-{index}-{field}"] = value
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
    def test_not_applicable_clears_score_on_save(self):
        self.client.force_login(self.leader_user)
        response = self.client.post(
            self.save_url, self._payload(score="3", na_index=0), follow=True
        )
        self.assertEqual(response.status_code, 200)
        standards = list(self.leader_review.standards.order_by("order"))
        self.assertTrue(standards[0].not_applicable)
        self.assertIsNone(standards[0].score)
        # The rest keep their score.
        self.assertTrue(all(s.score == 3 for s in standards[1:]))

    # Catches the leader being unable to add a free-form goal.
    def test_leader_can_add_goal(self):
        self.client.force_login(self.leader_user)
        payload = self._payload(
            goals=[
                {
                    "goal": "Improve attendance",
                    "evidence_and_discussion": "Weekly tracking",
                    "achieved": "false",
                }
            ]
        )
        response = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.leader_review.goals.count(), 1)
        goal = self.leader_review.goals.get()
        self.assertEqual(goal.goal, "Improve attendance")
        self.assertFalse(goal.achieved)

    # Catches the can_delete path failing to remove a goal via the DELETE flag.
    def test_leader_can_delete_goal(self):
        goal = LeaderGoal.objects.create(
            leader_review=self.leader_review, order=1, goal="Old goal"
        )
        self.client.force_login(self.leader_user)
        payload = self._payload(
            goals=[
                {
                    "id": str(goal.pk),
                    "goal": "Old goal",
                    "evidence_and_discussion": "",
                    "achieved": "",
                    "DELETE": "on",
                }
            ]
        )
        response = self.client.post(self.save_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.leader_review.goals.count(), 0)

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
        self.assertEqual(response.status_code, 403)
        self.assertTrue(all(s.score is None for s in self.leader_review.standards.all()))


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
        self.assertContains(response, "Start my appraisal")

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
