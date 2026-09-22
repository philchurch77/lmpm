"""Support-staff wording on the Goals / Last Year / Summary tabs.

Covers the change that rewords Goal 1 and Goal 3 for support-staff owners and
removes the Upper Pay Range question for them. The data-safety tests come
first: removing a yes/no field from a form is only safe if a stored "Yes" can
never be silently turned into "No" by the next coach save, and a forged key can
never write one in.

Wording is always decided by the appraisal's OWNER (``Appraisal.teacher``),
never by whoever is viewing — the same rule that bit the self-review variant.

The reword command is a data migration in all but name, so its skip rules are
each tested against a real row, and every field it must not touch is compared
byte-for-byte before and after.
"""
from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from core.models import StaffMember

from .models import (
    DEFAULT_STANDARDS_GOAL,
    SUPPORT_STANDARDS_GOAL,
    Appraisal,
    Goal,
)
from .tests import make_appraisal, make_staff, make_user, make_year

# Deliberately awkward text: accents, curly quotes, emoji, CRLF and blank lines.
AWKWARD_TEXT = (
    "Café review — “agreed” with Zoë.\r\n\r\n"
    "Second paragraph: naïve résumé ✔ 😀\n"
    "Third line with trailing spaces   \n"
) * 20 + "Final line."
# Ends on text: Django form CharFields strip leading/trailing whitespace
# (strip=True). That is pre-existing behaviour, reported separately.

UPR_QUESTION = "applying to be or is paid on the Upper Pay Range"
UPR_INPUT = 'name="on_upper_pay_range"'


def _summary_payload(appraisal, **overrides):
    payload = {
        "cpd_requirements": appraisal.cpd_requirements,
        "summary_teacher_comment": appraisal.summary_teacher_comment,
        "summary_coach_comment": appraisal.summary_coach_comment,
        "self_review_form_completed": "false",
        "engaged_with_professional_growth": "false",
        "coach_supports_pay_award": "",
        "job_description_review_needed": "false",
        "status": appraisal.status,
    }
    payload.update(overrides)
    return payload


class SupportUpperPayRangeSaveTests(TestCase):
    """The UPR question removed for support staff must never lose or forge a value."""

    def setUp(self):
        self.coach_email = "coach@oxlip.test"
        self.coach_user = make_user(self.coach_email)
        make_staff(self.coach_email, staff_type=StaffMember.StaffType.TEACHING)
        self.year = make_year()

    def _appraisal_for(self, email, staff_type, *, upr):
        owner = make_staff(
            email, performance_manager_email=self.coach_email, staff_type=staff_type
        )
        appraisal = make_appraisal(owner, self.year, coach_email=self.coach_email)
        Appraisal.objects.filter(pk=appraisal.pk).update(on_upper_pay_range=upr)
        appraisal.refresh_from_db()
        return appraisal

    def _summary_page(self, appraisal):
        return self.client.get(
            reverse("appraisals:detail_tab", args=[appraisal.pk, "summary"])
        )

    # Catches a coach save on a support appraisal turning a stored UPR "Yes"
    # into "No", and the stored "Yes" being hidden from the page.
    def test_coach_save_keeps_stored_upr_yes_on_support_appraisal(self):
        appraisal = self._appraisal_for(
            "support@oxlip.test", StaffMember.StaffType.SUPPORT, upr=True
        )
        self.client.force_login(self.coach_user)

        response = self._summary_page(appraisal)
        self.assertContains(response, UPR_QUESTION)
        self.assertContains(response, UPR_INPUT)

        self.client.post(
            reverse("appraisals:summary_save", args=[appraisal.pk]),
            _summary_payload(
                appraisal,
                on_upper_pay_range="true",
                summary_coach_comment="coach note",
            ),
        )
        appraisal.refresh_from_db()
        self.assertTrue(appraisal.on_upper_pay_range)
        self.assertEqual(appraisal.summary_coach_comment, "coach note")

    # Catches a forged on_upper_pay_range="true" writing a UPR answer onto a
    # support appraisal that was never asked it, and the rest of the same save
    # being thrown away with it.
    def test_forged_upr_yes_ignored_on_support_appraisal_but_other_fields_save(self):
        appraisal = self._appraisal_for(
            "support@oxlip.test", StaffMember.StaffType.SUPPORT, upr=False
        )
        self.client.force_login(self.coach_user)

        response = self._summary_page(appraisal)
        self.assertNotContains(response, UPR_QUESTION)
        self.assertNotContains(response, UPR_INPUT)

        self.client.post(
            reverse("appraisals:summary_save", args=[appraisal.pk]),
            _summary_payload(
                appraisal,
                on_upper_pay_range="true",
                summary_coach_comment=AWKWARD_TEXT,
                job_description_review_needed="true",
            ),
        )
        appraisal.refresh_from_db()
        self.assertFalse(appraisal.on_upper_pay_range)
        self.assertEqual(appraisal.summary_coach_comment, AWKWARD_TEXT)
        self.assertTrue(appraisal.job_description_review_needed)

    # Catches the UPR removal leaking onto teaching appraisals (control).
    def test_teaching_appraisal_still_asks_and_saves_upr(self):
        appraisal = self._appraisal_for(
            "teacher@oxlip.test", StaffMember.StaffType.TEACHING, upr=False
        )
        self.client.force_login(self.coach_user)

        response = self._summary_page(appraisal)
        self.assertContains(response, UPR_QUESTION)
        self.assertContains(response, UPR_INPUT)

        self.client.post(
            reverse("appraisals:summary_save", args=[appraisal.pk]),
            _summary_payload(appraisal, on_upper_pay_range="true"),
        )
        appraisal.refresh_from_db()
        self.assertTrue(appraisal.on_upper_pay_range)


class SupportWordingFollowsOwnerTests(TestCase):
    """Wording is chosen from the appraisal's owner, never from the viewer."""

    def setUp(self):
        self.year = make_year(2025)
        self.prior_year = make_year(2024, is_current=False)

    def _goal_labels(self, response):
        return [form.type_label for form in response.context["goal_formset"]]

    def _assert_all_three_goal_cards(self, response, goal_3_label):
        # Nothing is dropped: every seeded goal row renders, including Goal 3.
        forms = list(response.context["goal_formset"])
        self.assertEqual([f.instance.order for f in forms], [1, 2, 3])
        self.assertContains(response, "Goal 1 — ")
        self.assertContains(response, "Goal 2 — ")
        self.assertContains(response, f"Goal 3 — {goal_3_label}")

    # Catches a support-staff coach's own type rewording a TEACHING coachee's
    # page (Goal 3 label and the UPR question must stay the teacher version).
    def test_support_coach_sees_teacher_wording_on_teaching_coachee(self):
        coach_email = "supportcoach@oxlip.test"
        coach_user = make_user(coach_email)
        make_staff(coach_email, staff_type=StaffMember.StaffType.SUPPORT)
        teacher = make_staff(
            "teacher@oxlip.test",
            performance_manager_email=coach_email,
            staff_type=StaffMember.StaffType.TEACHING,
        )
        appraisal = make_appraisal(teacher, self.year, coach_email=coach_email)

        self.client.force_login(coach_user)
        response = self.client.get(
            reverse("appraisals:detail_tab", args=[appraisal.pk, "goals"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._goal_labels(response)[2], "Leadership / UPR")
        self.assertNotContains(response, "Leadership / Senior Support Staff")
        self.assertContains(response, UPR_QUESTION)
        self.assertTrue(response.context["summary_form"].shows_upper_pay_range)
        self._assert_all_three_goal_cards(response, "Leadership / UPR")

    # Catches a TEACHING coach's own type giving a SUPPORT coachee the teacher
    # wording, on both This Year and Last Year goals, and the UPR question.
    def test_teaching_coach_sees_support_wording_on_support_coachee(self):
        coach_email = "teachcoach@oxlip.test"
        coach_user = make_user(coach_email)
        make_staff(coach_email, staff_type=StaffMember.StaffType.TEACHING)
        support = make_staff(
            "support@oxlip.test",
            performance_manager_email=coach_email,
            staff_type=StaffMember.StaffType.SUPPORT,
        )
        make_appraisal(support, self.prior_year, coach_email=coach_email)
        appraisal = make_appraisal(support, self.year, coach_email=coach_email)

        self.client.force_login(coach_user)
        response = self.client.get(
            reverse("appraisals:detail_tab", args=[appraisal.pk, "goals"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._goal_labels(response)[2], "Leadership / Senior Support Staff"
        )
        last_year_labels = [
            form.type_label for form in response.context["last_year_formset"]
        ]
        self.assertEqual(last_year_labels[2], "Leadership / Senior Support Staff")
        self.assertNotContains(response, "Leadership / UPR")
        self.assertNotContains(response, UPR_QUESTION)
        self.assertFalse(response.context["summary_form"].shows_upper_pay_range)
        self._assert_all_three_goal_cards(response, "Leadership / Senior Support Staff")

    # Catches a superuser (no StaffMember, no staff type) being shown the
    # teacher version of a support member's appraisal.
    def test_superuser_sees_support_wording_on_support_appraisal(self):
        super_user = make_user("admin@oxlip.test", is_superuser=True)
        support = make_staff(
            "support@oxlip.test", staff_type=StaffMember.StaffType.SUPPORT
        )
        appraisal = make_appraisal(support, self.year)

        self.client.force_login(super_user)
        response = self.client.get(
            reverse("appraisals:detail_tab", args=[appraisal.pk, "goals"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._goal_labels(response)[2], "Leadership / Senior Support Staff"
        )
        self.assertNotContains(response, UPR_QUESTION)
        self._assert_all_three_goal_cards(response, "Leadership / Senior Support Staff")


class SeedGoalsSupportWordingTests(TestCase):
    """seed_goals() picks Goal 1's wording from the owner's staff type."""

    def setUp(self):
        self.year = make_year()

    def _goal_1(self, appraisal):
        return appraisal.goals.get(order=1)

    # Catches support staff being seeded with the teacher-standards Goal 1.
    def test_support_owner_is_seeded_with_support_goal_1(self):
        owner = make_staff("s@oxlip.test", staff_type=StaffMember.StaffType.SUPPORT)
        appraisal = make_appraisal(owner, self.year)
        self.assertEqual(self._goal_1(appraisal).title, SUPPORT_STANDARDS_GOAL)
        self.assertEqual(appraisal.goals.count(), 3)

    # Catches teaching, leader or unclassified staff getting the support wording.
    def test_non_support_owners_are_seeded_with_default_goal_1(self):
        for i, staff_type in enumerate(
            [StaffMember.StaffType.TEACHING, StaffMember.StaffType.LEADER, ""]
        ):
            with self.subTest(staff_type=staff_type or "blank"):
                owner = make_staff(f"o{i}@oxlip.test", staff_type=staff_type)
                appraisal = make_appraisal(owner, self.year)
                self.assertEqual(self._goal_1(appraisal).title, DEFAULT_STANDARDS_GOAL)

    # Catches a second seed_goals() call overwriting a title someone edited.
    def test_reseeding_does_not_overwrite_an_edited_goal_1(self):
        owner = make_staff("s@oxlip.test", staff_type=StaffMember.StaffType.SUPPORT)
        appraisal = make_appraisal(owner, self.year)
        goal = self._goal_1(appraisal)
        goal.title = AWKWARD_TEXT
        goal.save()

        appraisal.seed_goals()

        self.assertEqual(appraisal.goals.count(), 3)
        self.assertEqual(self._goal_1(appraisal).title, AWKWARD_TEXT)

    # Catches the start flow seeding goals before the self-selected type is
    # saved, which would give a new support member the teacher Goal 1.
    def test_self_selected_support_starts_with_support_goal_1(self):
        email = "newbie@oxlip.test"
        user = make_user(email)
        staff = make_staff(email)  # unclassified
        self.client.force_login(user)

        self.client.post(reverse("appraisals:start_appraisal"), {"staff_type": "SUPPORT"})

        appraisal = Appraisal.objects.get(teacher=staff, academic_year=self.year)
        self.assertEqual(self._goal_1(appraisal).title, SUPPORT_STANDARDS_GOAL)


class RewordSupportStandardsGoalCommandTests(TestCase):
    """reword_support_standards_goal: report by default, rewrite only the untouched."""

    def setUp(self):
        self.year = make_year(2025)
        self.prior_year = make_year(2024, is_current=False)
        self.year.refresh_from_db()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.backup = Path(self.tmpdir.name) / "reword.json"
        self.counter = 0

    def _goal_1(self, staff_type, *, year=None, status=Appraisal.Status.DRAFT, **fields):
        """A Goal 1 holding the old teacher default, as pre-change rows do."""
        self.counter += 1
        owner = make_staff(f"o{self.counter}@oxlip.test", staff_type=staff_type)
        appraisal = make_appraisal(owner, year or self.year, status=status)
        goal = appraisal.goals.get(order=1)
        Goal.objects.filter(pk=goal.pk).update(title=DEFAULT_STANDARDS_GOAL, **fields)
        goal.refresh_from_db()
        return goal

    def _snapshot(self):
        return {
            g.pk: (
                g.title,
                g.steps_to_success,
                g.success_criteria,
                g.teacher_review_comment,
                g.coach_review_comment,
            )
            for g in Goal.objects.all()
        }

    def _run(self, *args):
        out = StringIO()
        call_command("reword_support_standards_goal", *args, stdout=out)
        return out.getvalue()

    # Catches report mode writing anything (titles or a backup file).
    def test_report_mode_writes_nothing(self):
        self._goal_1(StaffMember.StaffType.SUPPORT)
        before = self._snapshot()

        output = self._run()

        self.assertEqual(self._snapshot(), before)
        self.assertIn("would be reworded", output)
        self.assertFalse(self.backup.exists())

    # Catches --apply running with no backup record, or with one inside the
    # repository (where a commit would deploy it).
    def test_apply_refuses_without_backup_or_with_backup_inside_repo(self):
        self._goal_1(StaffMember.StaffType.SUPPORT)
        before = self._snapshot()

        with self.assertRaises(CommandError):
            self._run("--apply")
        inside = Path(settings.BASE_DIR) / "reword-backup-test.json"
        with self.assertRaises(CommandError):
            self._run("--apply", "--backup-file", str(inside))

        self.assertFalse(inside.exists())
        self.assertEqual(self._snapshot(), before)

    # Catches the rewrite missing an eligible goal, touching anything but its
    # title, or not recording which goal it changed.
    def test_apply_rewords_eligible_goal_only_and_records_it(self):
        eligible = self._goal_1(StaffMember.StaffType.SUPPORT)
        # Goals 2 and 3 of the same appraisal carry awkward text that must survive.
        siblings = eligible.appraisal.goals.exclude(pk=eligible.pk)
        siblings.update(
            steps_to_success=AWKWARD_TEXT,
            success_criteria=AWKWARD_TEXT,
            teacher_review_comment=AWKWARD_TEXT,
            coach_review_comment=AWKWARD_TEXT,
        )
        before = self._snapshot()

        self._run("--apply", "--backup-file", str(self.backup))

        after = self._snapshot()
        eligible.refresh_from_db()
        self.assertEqual(eligible.title, SUPPORT_STANDARDS_GOAL)
        # Every other field on the reworded goal, and every other goal, unchanged.
        self.assertEqual(after[eligible.pk][1:], before[eligible.pk][1:])
        for pk, fields in before.items():
            if pk != eligible.pk:
                self.assertEqual(after[pk], fields)

        record = json.loads(self.backup.read_text(encoding="utf-8"))
        self.assertEqual([g["goal_pk"] for g in record["goals"]], [eligible.pk])

    # Catches the command rewriting history, signed-off records, a person's
    # edit, a goal already worked against, or a non-support owner's goal.
    def test_apply_skips_every_ineligible_goal(self):
        Support = StaffMember.StaffType.SUPPORT
        skipped = {
            "prior year": self._goal_1(Support, year=self.prior_year),
            "signed off": self._goal_1(Support, status=Appraisal.Status.SIGNED_OFF),
            "edited title": self._goal_1(Support),
            "steps written": self._goal_1(Support, steps_to_success=AWKWARD_TEXT),
            "coach review written": self._goal_1(
                Support, coach_review_comment=AWKWARD_TEXT
            ),
            "teaching owner": self._goal_1(StaffMember.StaffType.TEACHING),
            "leader owner": self._goal_1(StaffMember.StaffType.LEADER),
            "blank owner": self._goal_1(""),
        }
        edited_title = DEFAULT_STANDARDS_GOAL + " “and the school’s” café rules 😀\r\n"
        Goal.objects.filter(pk=skipped["edited title"].pk).update(title=edited_title)
        before = self._snapshot()

        self._run("--apply", "--backup-file", str(self.backup))

        after = self._snapshot()
        for reason, goal in skipped.items():
            with self.subTest(reason=reason):
                self.assertEqual(after[goal.pk], before[goal.pk])
        self.assertEqual(
            Goal.objects.get(pk=skipped["edited title"].pk).title, edited_title
        )

    # Catches a re-run rewriting again or writing a fresh record.
    def test_second_apply_changes_nothing(self):
        self._goal_1(StaffMember.StaffType.SUPPORT)
        self._run("--apply", "--backup-file", str(self.backup))
        self.backup.unlink()
        before = self._snapshot()

        output = self._run("--apply", "--backup-file", str(self.backup))

        self.assertEqual(self._snapshot(), before)
        self.assertIn("Nothing to reword", output)
        self.assertFalse(self.backup.exists())
