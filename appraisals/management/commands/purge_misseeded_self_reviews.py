"""Repair self-reviews seeded against the wrong person's staff type.

Until the fix in ``_build_section_forms``, the appraisal detail view chose the
self-review variant from the *viewer's* ``staff_type`` rather than the appraisal
owner's, and seeded it during a GET. Three residues are possible, and only the
third is visible to the person affected:

1. A blank ``LeaderReview`` (13 standards) seeded onto a non-leader's appraisal
   by a senior-leader coach opening their page. Invisible after the fix, but
   still in the database and the admin.
2. A blank ``SelfReview`` seeded onto a leader's appraisal by a teaching or
   support coach.
3. **A ``SelfReview`` carrying the wrong ``kind``** — a support-staff coach who
   was first to open a teaching coachee's appraisal seeded the *support*
   descriptor tree onto that teacher. ``kind`` is a stored snapshot and
   ``seed_items()`` no-ops once items exist, so this does not self-correct now
   that the code is fixed: the teacher goes on scoring against the wrong
   professional standards with nothing on screen to say so.

A review is only ever deleted when it is provably **untouched** — no scores, no
N/A flags, no evidence or examples, no support/UPR fields set. Deleting an
untouched shell is safe because the next visit re-seeds it correctly from the
owner's own type.

Anything carrying content is reported as KEEP and left alone. Two reasons: a
staff member reclassified after filling a review in is indistinguishable from a
mis-seeded shell by type alone; and a wrong-kind review with scores in it must
never be silently rebuilt, because those scores were given against different
statements. Someone has to decide what they meant.

Reports by default; pass --delete to remove. Safe to re-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import StaffMember

from appraisals.models import LeaderReview, SelfReview, SelfReviewBullet

LEADER = StaffMember.StaffType.LEADER
SUPPORT = StaffMember.StaffType.SUPPORT


def _is_untouched_leader_review(review) -> bool:
    """True when no standard row carries a score, an N/A flag or any examples."""
    return not review.standards.exclude(
        score__isnull=True, not_applicable=False, examples=""
    ).exists()


def _is_untouched_self_review(review) -> bool:
    """True when nothing has been entered anywhere in the review."""
    if review.job_summary.strip() or review.level_description.strip():
        return False
    if review.upr_declaration_agreed or review.signed_name.strip() or review.signed_date:
        return False
    if review.items.exclude(evidence="").exists():
        return False
    return not SelfReviewBullet.objects.filter(
        self_review_item__self_review=review, score__isnull=False
    ).exists()


def _kind_matches_owner(review) -> bool:
    """Whether a SelfReview's snapshotted kind agrees with its owner's type.

    Mirrors the rule in ``_ensure_self_review``: SUPPORT staff get the support
    tree, everyone else (including an unclassified staff member) gets teaching.
    So a blank ``staff_type`` paired with TEACHING is correct, not a mismatch.
    """
    owner_is_support = review.appraisal.teacher.staff_type == SUPPORT
    return (review.kind == SelfReview.Kind.SUPPORT) == owner_is_support


class Command(BaseCommand):
    help = "Repair self-reviews seeded against the wrong staff type."

    def add_arguments(self, parser):
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Actually delete the untouched shells (default is report only).",
        )

    def _scan(self, reviews, label, is_untouched, keep_note):
        removable, kept = [], []
        for review in reviews:
            (removable if is_untouched(review) else kept).append(review)

        for review in kept:
            self.stdout.write(
                self.style.ERROR(
                    f"  KEEP  {review.appraisal.teacher.email} "
                    f"{review.appraisal.academic_year} - {label} (pk={review.pk}) "
                    f"has content. {keep_note}"
                )
            )
        for review in removable:
            self.stdout.write(
                f"  strip {review.appraisal.teacher.email} "
                f"{review.appraisal.academic_year} - blank {label} (pk={review.pk})"
            )
        return removable, kept

    def handle(self, *args, **options):
        # A LeaderReview belongs only on a LEADER's appraisal; a SelfReview only
        # on a non-LEADER's. Anything else was seeded from the viewer's type.
        stray_leader = list(
            LeaderReview.objects.exclude(appraisal__teacher__staff_type=LEADER)
            .select_related("appraisal__teacher", "appraisal__academic_year")
            .prefetch_related("standards")
        )
        stray_self = list(
            SelfReview.objects.filter(appraisal__teacher__staff_type=LEADER)
            .select_related("appraisal__teacher", "appraisal__academic_year")
        )
        # The silent case: right model, wrong descriptor tree. Leaders are
        # excluded here because stray_self already covers them.
        wrong_kind = [
            review
            for review in SelfReview.objects.exclude(
                appraisal__teacher__staff_type=LEADER
            ).select_related("appraisal__teacher", "appraisal__academic_year")
            if not _kind_matches_owner(review)
        ]

        leader_rm, leader_keep = self._scan(
            stray_leader,
            "LeaderReview",
            _is_untouched_leader_review,
            "Someone typed into it — find out who before deleting.",
        )
        self_rm, self_keep = self._scan(
            stray_self,
            "SelfReview on a leader",
            _is_untouched_self_review,
            "Someone typed into it — find out who before deleting.",
        )
        kind_rm, kind_keep = self._scan(
            wrong_kind,
            "wrong-kind SelfReview",
            _is_untouched_self_review,
            "Scores were given against the WRONG descriptors — do not rebuild "
            "silently; agree with the staff member what happens to this review.",
        )

        removable = leader_rm + self_rm + kind_rm
        total_keep = len(leader_keep) + len(self_keep) + len(kind_keep)

        if not removable and not total_keep:
            self.stdout.write(self.style.SUCCESS("No mis-seeded self-reviews found."))
            return

        if not options["delete"]:
            self.stdout.write(
                self.style.WARNING(
                    f"[report only] {len(removable)} blank shell(s) would be deleted; "
                    f"{total_keep} need a human decision. Re-run with --delete."
                )
            )
            return

        with transaction.atomic():
            for review in removable:
                review.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {len(removable)} blank shell(s); "
                f"{total_keep} need a human decision."
            )
        )
