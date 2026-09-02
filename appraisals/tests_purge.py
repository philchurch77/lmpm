"""Tests for the ``purge_misseeded_self_reviews`` repair command.

This command DELETES rows, so the tests are weighted accordingly: the single
invariant worth proving is that it only ever removes a review that is provably
untouched. Every place a human can leave content — a bullet score, an item's
evidence, a leader standard's score / examples / "not in job role" flag, and
the support and UPR fields on the SelfReview itself — gets its own assertion
that the review SURVIVES a run with --delete.

The false-positive guard matters as much as the true positives: a correctly
seeded review must never be reported at all, or an operator learns to ignore
the output.

Fixtures come from ``appraisals.tests`` so the two suites cannot drift.
"""
from __future__ import annotations

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.models import StaffMember

from .models import (
    LeaderReview,
    LeaderStandard,
    SelfReview,
    SelfReviewBullet,
    SelfReviewItem,
)
from .tests import (
    make_appraisal,
    make_leader_review,
    make_self_review,
    make_staff,
    make_year,
)


def run_purge(*, delete=False):
    """Run the command and return everything it wrote to stdout."""
    out = StringIO()
    args = ["purge_misseeded_self_reviews"]
    if delete:
        args.append("--delete")
    call_command(*args, stdout=out)
    return out.getvalue()


class PurgeMisseededSelfReviewsTests(TestCase):
    """The three residue categories, and the untouched-only delete invariant."""

    def setUp(self):
        self.year = make_year()

    # ---- fixture builders -------------------------------------------------
    # Each returns the review under test, already attached to an owner of the
    # relevant staff type.

    def _stray_leader_review(self, email="teacher@oxlip.test"):
        """A LeaderReview sitting on a TEACHING person's appraisal (category 1)."""
        owner = make_staff(email, staff_type=StaffMember.StaffType.TEACHING)
        appraisal = make_appraisal(owner, self.year)
        return make_leader_review(appraisal)

    def _stray_self_review(self, email="head@oxlip.test"):
        """A SelfReview sitting on a LEADER's appraisal (category 2)."""
        owner = make_staff(email, staff_type=StaffMember.StaffType.LEADER)
        appraisal = make_appraisal(owner, self.year)
        return make_self_review(appraisal, kind=SelfReview.Kind.TEACHING)

    def _wrong_kind_self_review(self, email="misseeded@oxlip.test"):
        """A SUPPORT descriptor tree seeded onto a TEACHING person (category 3)."""
        owner = make_staff(email, staff_type=StaffMember.StaffType.TEACHING)
        appraisal = make_appraisal(owner, self.year)
        return make_self_review(appraisal, kind=SelfReview.Kind.SUPPORT)

    def _first_bullet(self, review):
        return (
            SelfReviewBullet.objects.filter(self_review_item__self_review=review)
            .order_by("self_review_item__order", "order")
            .first()
        )

    # ---- category 1: a LeaderReview on a non-leader ------------------------

    # Catches the invisible residue being left behind: a blank leader shell on a
    # teacher's appraisal is safe to remove and must actually go.
    def test_blank_leader_review_on_a_teacher_is_deleted(self):
        review = self._stray_leader_review()
        output = run_purge(delete=True)
        self.assertIn("strip", output)
        self.assertFalse(LeaderReview.objects.filter(pk=review.pk).exists())
        self.assertFalse(
            LeaderStandard.objects.filter(leader_review_id=review.pk).exists()
        )

    # Catches a scored leader standard being destroyed. This is the assertion
    # standing between the command and a real senior leader's completed review.
    def test_leader_review_with_a_scored_standard_is_kept(self):
        review = self._stray_leader_review()
        standard = review.standards.first()
        standard.score = 2
        standard.save()

        output = run_purge(delete=True)
        self.assertIn("KEEP", output)
        self.assertIn("teacher@oxlip.test", output)
        self.assertTrue(LeaderReview.objects.filter(pk=review.pk).exists())
        standard.refresh_from_db()
        self.assertEqual(standard.score, 2)

    # Catches free-text examples counting as blank because only the score was
    # checked — text with no score attached is still someone's work.
    def test_leader_review_with_examples_is_kept(self):
        review = self._stray_leader_review()
        standard = review.standards.first()
        standard.examples = "Led the reading strategy across the trust"
        standard.save()

        run_purge(delete=True)
        self.assertTrue(LeaderReview.objects.filter(pk=review.pk).exists())
        standard.refresh_from_db()
        self.assertEqual(standard.examples, "Led the reading strategy across the trust")

    # Catches "not in job role" being treated as no content. A ticked N/A box
    # forces score back to null (LeaderStandard.save), so it is the one signal
    # of deliberate input carrying no score and no text at all.
    def test_leader_review_with_only_a_not_applicable_flag_is_kept(self):
        review = self._stray_leader_review()
        standard = review.standards.filter(
            section=LeaderStandard.Section.STANDARDS
        ).first()
        standard.not_applicable = True
        standard.save()
        standard.refresh_from_db()
        # Guard the premise: N/A really does clear the score, so this row has
        # nothing but the flag to distinguish it from an untouched one.
        self.assertIsNone(standard.score)
        self.assertEqual(standard.examples, "")

        run_purge(delete=True)
        self.assertTrue(LeaderReview.objects.filter(pk=review.pk).exists())

    # ---- category 2: a SelfReview on a leader ------------------------------

    # Catches the blank teaching shell left on a senior leader's appraisal.
    def test_blank_self_review_on_a_leader_is_deleted(self):
        review = self._stray_self_review()
        run_purge(delete=True)
        self.assertFalse(SelfReview.objects.filter(pk=review.pk).exists())
        self.assertFalse(
            SelfReviewItem.objects.filter(self_review_id=review.pk).exists()
        )

    # Catches a leader who actually scored the (wrong) teaching form losing it.
    def test_self_review_on_a_leader_with_a_bullet_score_is_kept(self):
        review = self._stray_self_review()
        bullet = self._first_bullet(review)
        bullet.score = 3
        bullet.save()

        output = run_purge(delete=True)
        self.assertIn("KEEP", output)
        self.assertTrue(SelfReview.objects.filter(pk=review.pk).exists())
        bullet.refresh_from_db()
        self.assertEqual(bullet.score, 3)

    # Catches evidence text being destroyed when no bullet was ever scored.
    # Evidence lives on the item, a different table from the score.
    def test_self_review_on_a_leader_with_item_evidence_is_kept(self):
        review = self._stray_self_review()
        item = review.items.order_by("order").first()
        item.evidence = "Chaired the safeguarding review"
        item.save()

        run_purge(delete=True)
        self.assertTrue(SelfReview.objects.filter(pk=review.pk).exists())
        item.refresh_from_db()
        self.assertEqual(item.evidence, "Chaired the safeguarding review")

    # ---- category 3: the wrong descriptor tree -----------------------------

    # Catches the silent case going unrepaired: a blank support tree on a
    # teacher never self-corrects (seed_items no-ops once items exist), so the
    # shell has to go for the next visit to reseed it from the owner's own type.
    def test_blank_wrong_kind_self_review_is_deleted(self):
        review = self._wrong_kind_self_review()
        output = run_purge(delete=True)
        self.assertIn("wrong-kind", output)
        self.assertFalse(SelfReview.objects.filter(pk=review.pk).exists())

    # Catches the worst possible deletion: scores a teacher gave against the
    # WRONG statements. Not blank, and not automatically salvageable — a human
    # has to decide, so the command must keep them and say why.
    def test_wrong_kind_self_review_with_a_score_is_kept_and_flagged_for_a_human(self):
        review = self._wrong_kind_self_review()
        bullet = self._first_bullet(review)
        bullet.score = 1
        bullet.save()

        output = run_purge(delete=True)
        self.assertTrue(SelfReview.objects.filter(pk=review.pk).exists())
        self.assertIn("KEEP", output)
        self.assertIn("WRONG descriptors", output)

    # Catches content held only on the SelfReview row itself (not on its items
    # or bullets) being missed by the untouched check. Each field is exercised
    # on its own review, so one field silently dropping out is still caught.
    def test_wrong_kind_self_review_with_support_or_upr_fields_is_kept(self):
        cases = {
            "job_summary": "Cover supervisor, KS2",
            "level_description": "Level 3",
            "upr_declaration_agreed": True,
            "signed_name": "A Teacher",
            "signed_date": date(2026, 3, 1),
        }
        for index, (field, value) in enumerate(cases.items()):
            with self.subTest(field=field):
                review = self._wrong_kind_self_review(f"upr{index}@oxlip.test")
                setattr(review, field, value)
                review.save()

                run_purge(delete=True)
                self.assertTrue(
                    SelfReview.objects.filter(pk=review.pk).exists(),
                    f"a review carrying {field} was deleted as blank",
                )

    # ---- false positives ---------------------------------------------------

    # The guard that matters as much as the deletions: a correctly seeded
    # review of every shape must not be reported at all, let alone removed.
    def test_correctly_seeded_reviews_are_never_reported(self):
        teacher = make_staff("t@oxlip.test", staff_type=StaffMember.StaffType.TEACHING)
        teaching_review = make_self_review(
            make_appraisal(teacher, self.year), kind=SelfReview.Kind.TEACHING
        )
        support = make_staff("s@oxlip.test", staff_type=StaffMember.StaffType.SUPPORT)
        support_review = make_self_review(
            make_appraisal(support, self.year), kind=SelfReview.Kind.SUPPORT
        )
        leader = make_staff("l@oxlip.test", staff_type=StaffMember.StaffType.LEADER)
        leader_review = make_leader_review(make_appraisal(leader, self.year))

        output = run_purge(delete=True)
        self.assertIn("No mis-seeded self-reviews found.", output)
        self.assertNotIn("KEEP", output)
        self.assertTrue(SelfReview.objects.filter(pk=teaching_review.pk).exists())
        self.assertTrue(SelfReview.objects.filter(pk=support_review.pk).exists())
        self.assertTrue(LeaderReview.objects.filter(pk=leader_review.pk).exists())

    # Catches the mismatch rule drifting from _ensure_self_review: unclassified
    # staff are deliberately given the TEACHING tree, so blank + TEACHING is
    # correct. Flagging it would delete the in-progress review of everyone who
    # started before being classified.
    def test_unclassified_staff_with_teaching_kind_is_not_a_mismatch(self):
        owner = make_staff("new.starter@oxlip.test", staff_type="")
        review = make_self_review(
            make_appraisal(owner, self.year), kind=SelfReview.Kind.TEACHING
        )
        output = run_purge(delete=True)
        self.assertIn("No mis-seeded self-reviews found.", output)
        self.assertTrue(SelfReview.objects.filter(pk=review.pk).exists())

    # The mirror of the above: unclassified staff never get the SUPPORT tree,
    # so blank + SUPPORT really is a mis-seed and must still be caught.
    def test_unclassified_staff_with_support_kind_is_a_mismatch(self):
        owner = make_staff("new.starter@oxlip.test", staff_type="")
        review = make_self_review(
            make_appraisal(owner, self.year), kind=SelfReview.Kind.SUPPORT
        )
        run_purge(delete=True)
        self.assertFalse(SelfReview.objects.filter(pk=review.pk).exists())

    # ---- command behaviour -------------------------------------------------

    # Catches the default run being destructive. An operator's first run is
    # always the default one, so it must be pure reporting.
    def test_default_run_reports_but_deletes_nothing(self):
        leader_review = self._stray_leader_review("a@oxlip.test")
        self_review = self._stray_self_review("b@oxlip.test")
        wrong_kind = self._wrong_kind_self_review("c@oxlip.test")

        output = run_purge()
        self.assertIn("[report only]", output)
        self.assertIn("3 blank shell(s) would be deleted", output)
        self.assertTrue(LeaderReview.objects.filter(pk=leader_review.pk).exists())
        self.assertTrue(SelfReview.objects.filter(pk=self_review.pk).exists())
        self.assertTrue(SelfReview.objects.filter(pk=wrong_kind.pk).exists())

    # Catches a second --delete run erroring, or deleting something it spared
    # the first time. Repair commands get run twice by nervous operators.
    def test_running_twice_with_delete_is_idempotent(self):
        blank = self._stray_leader_review("blank@oxlip.test")
        touched = self._stray_leader_review("touched@oxlip.test")
        standard = touched.standards.first()
        standard.score = 3
        standard.save()

        first = run_purge(delete=True)
        self.assertIn("Deleted 1 blank shell(s)", first)

        second = run_purge(delete=True)
        self.assertIn("Deleted 0 blank shell(s)", second)
        self.assertFalse(LeaderReview.objects.filter(pk=blank.pk).exists())
        self.assertTrue(LeaderReview.objects.filter(pk=touched.pk).exists())
        standard.refresh_from_db()
        self.assertEqual(standard.score, 3)

    # Catches the leader-stray and wrong-kind branches overlapping, which would
    # report and delete the same row twice and overstate the damage.
    #
    # The kind must be SUPPORT here, and that is not incidental. _kind_matches_owner
    # only asks "support vs not-support", so a TEACHING review on a LEADER is not a
    # kind mismatch at all — SUPPORT-on-a-LEADER is the ONLY combination both
    # branches can claim, and therefore the only one that proves the
    # exclude(staff_type=LEADER) on the wrong-kind queryset is load-bearing.
    # It is also a real scenario: a support-staff coach opening a leader's
    # appraisal under the old bug seeded exactly this.
    def test_leader_with_a_support_kind_self_review_is_reported_once(self):
        owner = make_staff("head@oxlip.test", staff_type=StaffMember.StaffType.LEADER)
        review = make_self_review(
            make_appraisal(owner, self.year), kind=SelfReview.Kind.SUPPORT
        )

        report = run_purge()
        self.assertEqual(report.count(f"pk={review.pk})"), 1)
        self.assertIn("1 blank shell(s) would be deleted", report)

        deleted = run_purge(delete=True)
        self.assertIn("Deleted 1 blank shell(s)", deleted)
        self.assertFalse(SelfReview.objects.filter(pk=review.pk).exists())

    # The same overlap on the KEEP path, which is the dangerous half: if both
    # branches claim a touched review it is merely reported twice, but if the
    # counts disagree with reality an operator cannot trust the summary.
    def test_touched_support_kind_review_on_a_leader_is_kept_and_counted_once(self):
        owner = make_staff("head@oxlip.test", staff_type=StaffMember.StaffType.LEADER)
        review = make_self_review(
            make_appraisal(owner, self.year), kind=SelfReview.Kind.SUPPORT
        )
        bullet = self._first_bullet(review)
        bullet.score = 2
        bullet.save()

        report = run_purge()
        self.assertEqual(report.count(f"pk={review.pk})"), 1)
        self.assertIn("1 need a human decision", report)
        run_purge(delete=True)
        self.assertTrue(SelfReview.objects.filter(pk=review.pk).exists())
