"""Tests for the data_import bulk CSV importer.

This is a flat superuser-only subsystem with no per-row ownership, so the
access-control surface is simpler than appraisals/line_management's role
matrices: every view just needs require_importer to actually gate it. The
real risk here is in services.py — wrong data landing in real StaffMember /
Appraisal / Goal / SelfReview / LineMeeting rows, duplicate writes on
re-upload, or a crash mid-batch leaving a partial write. Those are the
priority: the self-review seed-once-per-group behaviour, the LineMeeting
cross-batch dedupe-by-hash (its only uniqueness guarantee), blank-cell
non-clobber on update, and confirm-time drift degrading to a SKIP rather than
a crash or a half-applied batch.

Identity is by email only (no FK from StaffMember to User), so every fixture
creates BOTH a Django ``User`` (to log in, where needed) and a ``StaffMember``
with the same email, mirroring appraisals/tests.py and
line_management/tests.py. ``PermissionDenied`` surfaces as HTTP 403 through
the test client.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from appraisals.models import (
    AcademicYear,
    Appraisal,
    Goal,
    SelfReview,
    SelfReviewBullet,
    SelfReviewItem,
)
from appraisals.self_review_templates import SUPPORT_ITEMS, TEACHING_ITEMS
from core.models import School, StaffMember
from line_management.models import LineMeeting

from .models import ImportBatch, ImportRow, ImportType
from .parsers import ImportFileError, parse_csv
from .services import confirm_batch


def make_user(email, *, is_superuser=False):
    """A Django User keyed by email (username mirrors it for uniqueness)."""
    return User.objects.create_user(
        username=email,
        email=email,
        password="pw",
        is_superuser=is_superuser,
        is_staff=is_superuser,
    )


def make_staff(email, **kwargs):
    return StaffMember.objects.create(email=email, **kwargs)


def make_academic_year(start_year=2025, *, is_current=True):
    return AcademicYear.objects.create(start_year=start_year, is_current=is_current)


def make_appraisal(teacher, year, *, coach_email="", status=Appraisal.Status.DRAFT):
    appraisal = Appraisal.objects.create(
        teacher=teacher, academic_year=year, coach_email=coach_email, status=status
    )
    appraisal.seed_goals()
    return appraisal


def make_csv(header_row, *data_rows):
    """Build a SimpleUploadedFile CSV from a header string and data row strings."""
    content = "\n".join([header_row, *data_rows]) + "\n"
    return SimpleUploadedFile(
        "upload.csv", content.encode("utf-8"), content_type="text/csv"
    )


def upload_and_get_batch(client, slug, uploaded_file):
    """POST a file to the upload view; returns the response (redirected to
    preview on success). Callers fetch the created ImportBatch separately."""
    url = reverse("data_import:upload", args=[slug])
    response = client.post(url, {"csv_file": uploaded_file}, follow=True)
    return response


class AccessControlTests(TestCase):
    """Every data_import view is superuser-only. No per-row ownership exists
    here, so this is a flat gate check rather than a role matrix — but it's
    the only thing standing in front of bulk writes, so it still needs
    explicit, direct coverage.
    """

    def setUp(self):
        self.staff_user = make_user("staffuser@oxlip.test")
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)

        batch = ImportBatch.objects.create(
            import_type=ImportType.STAFF, uploaded_by=self.super_user
        )
        self.batch = batch

        self.hub_url = reverse("data_import:hub")
        self.upload_url = reverse("data_import:upload", args=["staff"])
        self.preview_url = reverse("data_import:preview", args=[batch.pk])

    # Catches a non-superuser reaching the hub page listing every import type.
    def test_non_superuser_gets_403_on_hub(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(self.hub_url)
        self.assertEqual(response.status_code, 403)

    # Catches a non-superuser reaching the upload form for a valid slug.
    def test_non_superuser_gets_403_on_upload_get(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(self.upload_url)
        self.assertEqual(response.status_code, 403)

    # Catches a non-superuser being able to POST a file and trigger a write.
    def test_non_superuser_gets_403_on_upload_post(self):
        self.client.force_login(self.staff_user)
        csv_file = make_csv("email", "new.person@oxlip.test")
        response = self.client.post(self.upload_url, {"csv_file": csv_file})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            StaffMember.objects.filter(email="new.person@oxlip.test").exists()
        )

    # Catches IDOR: a non-superuser reaching a preview page via a guessed batch_id.
    def test_non_superuser_gets_403_on_preview_with_guessed_batch_id(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, 403)

    # Catches a non-superuser confirming/discarding a batch via a guessed batch_id.
    def test_non_superuser_cannot_confirm_via_guessed_batch_id(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(self.preview_url, {"action": "confirm"})
        self.assertEqual(response.status_code, 403)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, ImportBatch.Status.PENDING)

    # Catches the login gate being removed from the hub.
    def test_anonymous_user_is_redirected_to_login_on_hub(self):
        response = self.client.get(self.hub_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url.lower())

    # Catches superuser access regressing across all three views.
    def test_superuser_can_reach_hub_upload_and_preview(self):
        self.client.force_login(self.super_user)
        self.assertEqual(self.client.get(self.hub_url).status_code, 200)
        self.assertEqual(self.client.get(self.upload_url).status_code, 200)
        self.assertEqual(self.client.get(self.preview_url).status_code, 200)

    # Catches an unknown slug not 404ing (and so silently mapping to nothing).
    def test_unknown_slug_is_404_even_for_superuser(self):
        self.client.force_login(self.super_user)
        response = self.client.get(reverse("data_import:upload", args=["bogus-type"]))
        self.assertEqual(response.status_code, 404)


class ParseCsvFileLevelValidationTests(TestCase):
    """parse_csv: file-level problems raise ImportFileError, independent of
    any view. Row-content problems are NOT this layer's job — see services.py
    tests below for that.
    """

    # Catches a missing required column not being detected before any row
    # validation runs.
    def test_missing_required_column_raises_import_file_error(self):
        csv_file = make_csv("not_email,department", "x,Science")
        with self.assertRaises(ImportFileError):
            parse_csv(csv_file, ImportType.STAFF)

    # Catches a non-UTF-8 file being silently mis-decoded instead of rejected.
    def test_bad_encoding_raises_import_file_error(self):
        # latin-1 bytes that are not valid UTF-8 (0xe9 = 'é' in latin-1).
        bad_bytes = b"email\nuser\xe9@oxlip.test\n"
        csv_file = SimpleUploadedFile("bad.csv", bad_bytes, content_type="text/csv")
        with self.assertRaises(ImportFileError):
            parse_csv(csv_file, ImportType.STAFF)

    # Catches a header-only file (no data rows) being treated as valid.
    def test_empty_file_raises_import_file_error(self):
        csv_file = SimpleUploadedFile("empty.csv", b"", content_type="text/csv")
        with self.assertRaises(ImportFileError):
            parse_csv(csv_file, ImportType.STAFF)

    # Catches a header row with no data rows under it slipping through.
    def test_header_only_file_raises_import_file_error(self):
        csv_file = make_csv("email")
        with self.assertRaises(ImportFileError):
            parse_csv(csv_file, ImportType.STAFF)

    # Catches column-name matching becoming case-sensitive (header_lookup is
    # lower-cased; required columns must still be found against e.g. "Email").
    def test_column_names_are_matched_case_insensitively(self):
        csv_file = make_csv("Email,Department", "person@oxlip.test,Science")
        rows = parse_csv(csv_file, ImportType.STAFF)
        self.assertEqual(rows[0][1]["email"], "person@oxlip.test")

    # Catches whitespace in cells not being stripped, which would break every
    # downstream email/exact-match lookup in services.py.
    def test_cell_values_are_stripped_of_whitespace(self):
        csv_file = make_csv("email,department", "  person@oxlip.test  ,  Science  ")
        rows = parse_csv(csv_file, ImportType.STAFF)
        self.assertEqual(rows[0][1]["email"], "person@oxlip.test")
        self.assertEqual(rows[0][1]["department"], "Science")


class StaffImportTests(TestCase):
    """validate_staff_row / apply_staff_row via the real upload+confirm flow."""

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.school = School.objects.create(name="Copleston High School")

    def _upload(self, header, *rows):
        response = upload_and_get_batch(
            self.client, "staff", make_csv(header, *rows)
        )
        self.assertEqual(response.status_code, 200)
        return ImportBatch.objects.filter(import_type=ImportType.STAFF).latest(
            "uploaded_at"
        )

    # Catches new StaffMember rows not being created at all, or created with
    # the wrong outcome (UPDATE instead of CREATE).
    def test_new_email_creates_staff_member(self):
        batch = self._upload(
            "email,department,job_title,staff_type",
            "new.teacher@oxlip.test,Maths,Teacher,TEACHING",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.CREATE)

        confirm_batch(batch)
        staff = StaffMember.objects.get(email="new.teacher@oxlip.test")
        self.assertEqual(staff.department, "Maths")
        self.assertEqual(staff.job_title, "Teacher")
        self.assertEqual(staff.staff_type, StaffMember.StaffType.TEACHING)

    # Catches re-importing an existing email creating a duplicate row instead
    # of updating in place.
    def test_existing_email_updates_staff_member_not_duplicate(self):
        make_staff("existing@oxlip.test", department="Old Dept")
        batch = self._upload(
            "email,department", "existing@oxlip.test,New Dept"
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.UPDATE)

        confirm_batch(batch)
        self.assertEqual(
            StaffMember.objects.filter(email="existing@oxlip.test").count(), 1
        )
        staff = StaffMember.objects.get(email="existing@oxlip.test")
        self.assertEqual(staff.department, "New Dept")

    # The headline data-safety guarantee for re-uploads: a blank CSV cell must
    # never clobber an existing non-blank field value.
    def test_blank_cell_does_not_clobber_existing_value_on_update(self):
        make_staff(
            "existing@oxlip.test",
            department="Keep This",
            job_title="Keep This Too",
        )
        batch = self._upload(
            "email,department,job_title",
            "existing@oxlip.test,,",  # both blank
        )
        confirm_batch(batch)
        staff = StaffMember.objects.get(email="existing@oxlip.test")
        self.assertEqual(staff.department, "Keep This")
        self.assertEqual(staff.job_title, "Keep This Too")

    # Catches a partial row write: an unmatched school name should skip the
    # WHOLE row, not import the other fields and silently drop the school.
    def test_unmatched_school_skips_whole_row_not_partial(self):
        batch = self._upload(
            "email,department,school",
            "new.person@oxlip.test,Science,Nonexistent School",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        self.assertIn("Nonexistent School", row.error_message)

        confirm_batch(batch)
        self.assertFalse(
            StaffMember.objects.filter(email="new.person@oxlip.test").exists()
        )

    # Catches school matching becoming case-sensitive against the real School
    # row, and confirms a valid school name does get linked.
    def test_matched_school_name_is_case_insensitive(self):
        batch = self._upload(
            "email,school",
            "new.person@oxlip.test,copleston high school",
        )
        confirm_batch(batch)
        staff = StaffMember.objects.get(email="new.person@oxlip.test")
        self.assertEqual(staff.school, self.school)

    # Catches an invalid staff_type value being silently coerced or applied.
    def test_invalid_staff_type_is_skipped(self):
        batch = self._upload(
            "email,staff_type",
            "new.person@oxlip.test,BOGUS",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        confirm_batch(batch)
        self.assertFalse(
            StaffMember.objects.filter(email="new.person@oxlip.test").exists()
        )


class AppraisalSummaryImportTests(TestCase):
    """validate_appraisal_summary_row / apply_appraisal_summary_row."""

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.teacher = make_staff(
            "teacher@oxlip.test",
            performance_manager_email="coach@oxlip.test",
            staff_type=StaffMember.StaffType.TEACHING,
        )
        self.year = make_academic_year(2025)

    def _upload(self, header, *rows):
        response = upload_and_get_batch(
            self.client, "appraisal-summaries", make_csv(header, *rows)
        )
        self.assertEqual(response.status_code, 200)
        return ImportBatch.objects.filter(
            import_type=ImportType.APPRAISAL_SUMMARY
        ).latest("uploaded_at")

    # Catches a new appraisal row not creating the Appraisal AND not seeding
    # its goals (seed_goals() is only called when `created` is True).
    def test_new_row_creates_appraisal_and_seeds_goals(self):
        batch = self._upload(
            "teacher_email,academic_year,status",
            "teacher@oxlip.test,2025,DRAFT",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.CREATE)

        confirm_batch(batch)
        appraisal = Appraisal.objects.get(teacher=self.teacher, academic_year=self.year)
        self.assertEqual(appraisal.status, Appraisal.Status.DRAFT)
        self.assertEqual(appraisal.goals.count(), 3)

    # Catches coach_email defaulting incorrectly: blank cell should use the
    # teacher's CURRENT performance_manager_email at import time.
    def test_blank_coach_email_defaults_to_current_performance_manager(self):
        batch = self._upload(
            "teacher_email,academic_year",
            "teacher@oxlip.test,2025",
        )
        confirm_batch(batch)
        appraisal = Appraisal.objects.get(teacher=self.teacher, academic_year=self.year)
        self.assertEqual(appraisal.coach_email, "coach@oxlip.test")

    # Catches an explicit coach_email cell being ignored in favour of the default.
    def test_explicit_coach_email_overrides_default(self):
        batch = self._upload(
            "teacher_email,academic_year,coach_email",
            "teacher@oxlip.test,2025,other.coach@oxlip.test",
        )
        confirm_batch(batch)
        appraisal = Appraisal.objects.get(teacher=self.teacher, academic_year=self.year)
        self.assertEqual(appraisal.coach_email, "other.coach@oxlip.test")

    # Catches updating an existing appraisal re-seeding (and so duplicating)
    # its goals, or losing already-entered goal data via a re-seed.
    def test_existing_appraisal_update_does_not_reseed_goals(self):
        appraisal = make_appraisal(self.teacher, self.year, status=Appraisal.Status.DRAFT)
        goal = appraisal.goals.order_by("order").first()
        goal.title = "Already customised goal"
        goal.save()

        batch = self._upload(
            "teacher_email,academic_year,status",
            "teacher@oxlip.test,2025,SHARED",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.UPDATE)

        confirm_batch(batch)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, Appraisal.Status.SHARED)
        self.assertEqual(appraisal.goals.count(), 3)
        goal.refresh_from_db()
        self.assertEqual(goal.title, "Already customised goal")

    # Catches an unmatched teacher_email producing a write instead of a
    # reported skip.
    def test_unmatched_teacher_email_is_skipped(self):
        batch = self._upload(
            "teacher_email,academic_year",
            "nobody@oxlip.test,2025",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        confirm_batch(batch)
        self.assertFalse(Appraisal.objects.filter(teacher__email="nobody@oxlip.test").exists())

    # Catches an unmatched academic_year producing a write instead of a
    # reported skip (and never creating an AcademicYear as a side effect).
    def test_unmatched_academic_year_is_skipped(self):
        batch = self._upload(
            "teacher_email,academic_year",
            "teacher@oxlip.test,1999",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        confirm_batch(batch)
        self.assertFalse(AcademicYear.objects.filter(start_year=1999).exists())
        self.assertFalse(
            Appraisal.objects.filter(teacher=self.teacher, academic_year__start_year=1999).exists()
        )


class GoalsImportTests(TestCase):
    """validate_goals_row / apply_goals_row."""

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.teacher = make_staff(
            "teacher@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.year = make_academic_year(2025)
        self.appraisal = make_appraisal(self.teacher, self.year)

    def _upload(self, header, *rows):
        response = upload_and_get_batch(self.client, "goals", make_csv(header, *rows))
        self.assertEqual(response.status_code, 200)
        return ImportBatch.objects.filter(import_type=ImportType.GOALS).latest(
            "uploaded_at"
        )

    # Catches goal_type -> order mapping drifting from GOAL_TYPE_ORDER.
    def test_goal_type_maps_to_correct_fixed_order(self):
        batch = self._upload(
            "teacher_email,academic_year,goal_type,title",
            "teacher@oxlip.test,2025,PERSONAL,My personal goal",
        )
        confirm_batch(batch)
        goal = Goal.objects.get(appraisal=self.appraisal, goal_type=Goal.GoalType.PERSONAL)
        self.assertEqual(goal.order, 2)
        self.assertEqual(goal.title, "My personal goal")

    # Catches a row for a teacher/year with no existing Appraisal writing
    # anyway instead of skip+report (goals.csv must not create an Appraisal).
    def test_missing_appraisal_is_skipped_and_reported(self):
        other_teacher = make_staff(
            "other@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        batch = self._upload(
            "teacher_email,academic_year,goal_type",
            "other@oxlip.test,2025,PERSONAL",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        self.assertIn("No appraisal exists", row.error_message)
        confirm_batch(batch)
        self.assertFalse(Goal.objects.filter(appraisal__teacher=other_teacher).exists())

    # Catches re-importing the same (teacher, year, goal_type) duplicating the
    # Goal row instead of updating the existing one (order is the real key).
    def test_reimport_updates_same_goal_not_duplicate(self):
        batch1 = self._upload(
            "teacher_email,academic_year,goal_type,title",
            "teacher@oxlip.test,2025,STANDARDS,First title",
        )
        confirm_batch(batch1)
        first_goal = Goal.objects.get(appraisal=self.appraisal, order=1)
        first_pk = first_goal.pk

        batch2 = self._upload(
            "teacher_email,academic_year,goal_type,title",
            "teacher@oxlip.test,2025,STANDARDS,Updated title",
        )
        row = batch2.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.UPDATE)
        confirm_batch(batch2)

        self.assertEqual(Goal.objects.filter(appraisal=self.appraisal, order=1).count(), 1)
        first_goal.refresh_from_db()
        self.assertEqual(first_goal.pk, first_pk)
        self.assertEqual(first_goal.title, "Updated title")


class SelfReviewImportTests(TestCase):
    """validate_self_review_row / apply_self_review_row / ensure_self_review_seeded.

    The trickiest part of the importer: rows are grouped by (teacher_email,
    academic_year) and the review is seeded exactly once per group before any
    bullet in that group is applied.
    """

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.teacher = make_staff(
            "teacher@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.support_staff = make_staff(
            "support@oxlip.test", staff_type=StaffMember.StaffType.SUPPORT
        )
        self.year = make_academic_year(2025)
        self.appraisal = make_appraisal(self.teacher, self.year)
        self.support_appraisal = make_appraisal(self.support_staff, self.year)

        # TS1 (teaching) has exactly 3 bullets; "1" (support) has exactly 1.
        self.ts1_code, _, self.ts1_bullets = TEACHING_ITEMS[0]
        self.support_code, _, self.support_bullets = SUPPORT_ITEMS[0]

    def _upload(self, header, *rows):
        response = upload_and_get_batch(
            self.client, "self-review", make_csv(header, *rows)
        )
        self.assertEqual(response.status_code, 200)
        return ImportBatch.objects.filter(import_type=ImportType.SELF_REVIEW).latest(
            "uploaded_at"
        )

    # Catches seed_items() being called once per bullet row instead of once
    # per (teacher, year) group — which would either duplicate items/bullets
    # or throw on the second seed attempt if not properly guarded.
    def test_many_bullet_rows_for_same_teacher_year_seed_review_exactly_once(self):
        rows = [
            f"teacher@oxlip.test,2025,{self.ts1_code},{i + 1},,2"
            for i in range(len(self.ts1_bullets))
        ]
        batch = self._upload(
            "teacher_email,academic_year,item_code,bullet_order,evidence,score",
            *rows,
        )
        confirm_batch(batch)

        self.assertEqual(SelfReview.objects.filter(appraisal=self.appraisal).count(), 1)
        self_review = SelfReview.objects.get(appraisal=self.appraisal)
        self.assertEqual(self_review.items.count(), len(TEACHING_ITEMS))
        self.assertEqual(
            SelfReviewBullet.objects.filter(
                self_review_item__self_review=self_review
            ).count(),
            sum(len(b) for _, _, b in TEACHING_ITEMS),
        )

    # Catches evidence being applied from a row other than bullet_order == 1
    # for the same item_code (it must be shared-per-item, set only once).
    def test_evidence_only_applied_from_bullet_order_one_row(self):
        rows = [
            f"teacher@oxlip.test,2025,{self.ts1_code},1,First row evidence,1",
            f"teacher@oxlip.test,2025,{self.ts1_code},2,Ignored evidence,2",
            f"teacher@oxlip.test,2025,{self.ts1_code},3,Also ignored,3",
        ]
        batch = self._upload(
            "teacher_email,academic_year,item_code,bullet_order,evidence,score",
            *rows,
        )
        confirm_batch(batch)

        item = SelfReviewItem.objects.get(
            self_review__appraisal=self.appraisal, code=self.ts1_code
        )
        self.assertEqual(item.evidence, "First row evidence")

        bullets = list(item.bullets.order_by("order"))
        self.assertEqual(bullets[0].score, 1)
        self.assertEqual(bullets[1].score, 2)
        self.assertEqual(bullets[2].score, 3)

    # Catches an invalid item_code blocking the WHOLE batch (or the whole
    # teacher's other rows) instead of skipping just that one row.
    def test_invalid_item_code_skips_only_that_row(self):
        rows = [
            "teacher@oxlip.test,2025,BOGUS_CODE,1,,1",
            f"teacher@oxlip.test,2025,{self.ts1_code},1,Real evidence,2",
        ]
        batch = self._upload(
            "teacher_email,academic_year,item_code,bullet_order,evidence,score",
            *rows,
        )
        bad_row = batch.rows.get(row_number=1)
        good_row = batch.rows.get(row_number=2)
        self.assertEqual(bad_row.outcome, ImportRow.Outcome.SKIP)
        self.assertEqual(good_row.outcome, ImportRow.Outcome.UPDATE)

        confirm_batch(batch)
        bad_row.refresh_from_db()
        good_row.refresh_from_db()
        self.assertEqual(bad_row.outcome, ImportRow.Outcome.SKIP)
        self.assertEqual(good_row.outcome, ImportRow.Outcome.UPDATE)

        item = SelfReviewItem.objects.get(
            self_review__appraisal=self.appraisal, code=self.ts1_code
        )
        self.assertEqual(item.evidence, "Real evidence")

    # Catches score values 1/2/3 and a blank ("Not answered") score not all
    # being handled correctly — blank must leave score as None, not error.
    def test_blank_score_leaves_bullet_not_answered(self):
        batch = self._upload(
            "teacher_email,academic_year,item_code,bullet_order,evidence,score",
            f"teacher@oxlip.test,2025,{self.ts1_code},1,,",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.UPDATE)
        confirm_batch(batch)

        bullet = SelfReviewBullet.objects.get(
            self_review_item__self_review__appraisal=self.appraisal,
            self_review_item__code=self.ts1_code,
            order=1,
        )
        self.assertIsNone(bullet.score)

    # Catches a SUPPORT staff member's item_code being validated against
    # TEACHING_ITEMS instead of SUPPORT_ITEMS.
    def test_support_staff_validates_against_support_items_not_teaching(self):
        batch = self._upload(
            "teacher_email,academic_year,item_code,bullet_order,evidence,score",
            f"support@oxlip.test,2025,{self.support_code},1,Support evidence,3",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.UPDATE)

        confirm_batch(batch)
        self_review = SelfReview.objects.get(appraisal=self.support_appraisal)
        self.assertEqual(self_review.kind, SelfReview.Kind.SUPPORT)
        self.assertEqual(self_review.items.count(), len(SUPPORT_ITEMS))

    # Catches a TEACHING-only item_code being incorrectly accepted for a
    # SUPPORT staff member (the codes overlap in shape, e.g. "1" vs "TS1").
    def test_support_staff_rejects_teaching_only_item_code(self):
        batch = self._upload(
            "teacher_email,academic_year,item_code,bullet_order,evidence,score",
            f"support@oxlip.test,2025,{self.ts1_code},1,,1",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)


class LineMeetingImportTests(TestCase):
    """validate_line_meeting_row / apply_line_meeting_row — the central
    idempotency guarantee: dedupe via source_row_hash across ALL batches of
    this import type, since LineMeeting has no real uniqueness constraint.
    """

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.staff = make_staff("report@oxlip.test")

    def _upload(self, header, *rows):
        response = upload_and_get_batch(
            self.client, "line-meetings", make_csv(header, *rows)
        )
        self.assertEqual(response.status_code, 200)
        return ImportBatch.objects.filter(import_type=ImportType.LINE_MEETINGS).latest(
            "uploaded_at"
        )

    # Catches the first upload of a row not creating a LineMeeting at all.
    def test_first_upload_creates_line_meeting(self):
        batch = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-01-15,Discussed timetable.",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.CREATE)
        confirm_batch(batch)
        meeting = LineMeeting.objects.get(staff=self.staff, meeting_date="2026-01-15")
        self.assertEqual(meeting.main_matters, "Discussed timetable.")

    # docs/import_templates.md: a blank created_by_email cell defaults to the
    # importing superuser's email (provenance only — never an access check).
    def test_blank_created_by_email_defaults_to_uploading_superuser(self):
        batch = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-01-15,Discussed timetable.",
        )
        confirm_batch(batch)
        meeting = LineMeeting.objects.get(staff=self.staff, meeting_date="2026-01-15")
        self.assertEqual(meeting.created_by_email, "admin@oxlip.test")

    # An explicit created_by_email cell overrides the uploader default.
    def test_explicit_created_by_email_overrides_uploader_default(self):
        batch = self._upload(
            "staff_email,meeting_date,main_matters,created_by_email",
            "report@oxlip.test,2026-01-15,Discussed timetable.,manager@oxlip.test",
        )
        confirm_batch(batch)
        meeting = LineMeeting.objects.get(staff=self.staff, meeting_date="2026-01-15")
        self.assertEqual(meeting.created_by_email, "manager@oxlip.test")

    # The headline idempotency guarantee: re-uploading the EXACT same content
    # as a brand-new ImportBatch must update the existing LineMeeting, not
    # create a duplicate — dedupe must look across batches, not just within one.
    def test_reuploading_identical_content_as_new_batch_updates_not_duplicates(self):
        batch1 = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-01-15,Discussed timetable.",
        )
        confirm_batch(batch1)
        self.assertEqual(LineMeeting.objects.filter(staff=self.staff).count(), 1)
        original_pk = LineMeeting.objects.get(staff=self.staff).pk

        # A second, independent batch with byte-identical row content.
        batch2 = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-01-15,Discussed timetable.",
        )
        self.assertNotEqual(batch1.pk, batch2.pk)
        row2 = batch2.rows.get(row_number=1)
        self.assertEqual(row2.outcome, ImportRow.Outcome.UPDATE)

        confirm_batch(batch2)
        self.assertEqual(LineMeeting.objects.filter(staff=self.staff).count(), 1)
        self.assertEqual(LineMeeting.objects.get(staff=self.staff).pk, original_pk)

    # Catches a genuinely different row (different date) being treated as a
    # duplicate of an existing meeting instead of a new create.
    def test_different_meeting_date_creates_new_record(self):
        batch1 = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-01-15,First meeting.",
        )
        confirm_batch(batch1)

        batch2 = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-02-15,Second meeting.",
        )
        row2 = batch2.rows.get(row_number=1)
        self.assertEqual(row2.outcome, ImportRow.Outcome.CREATE)
        confirm_batch(batch2)

        self.assertEqual(LineMeeting.objects.filter(staff=self.staff).count(), 2)

    # Catches different note content on the same date being treated as a
    # duplicate of an existing meeting instead of a new, genuinely separate one.
    def test_different_note_content_same_date_creates_new_record(self):
        batch1 = self._upload(
            "staff_email,meeting_date,main_matters",
            "report@oxlip.test,2026-01-15,First meeting.",
        )
        confirm_batch(batch1)

        batch2 = self._upload(
            "staff_email,meeting_date,main_matters",
            'report@oxlip.test,2026-01-15,"A different, later conversation."',
        )
        row2 = batch2.rows.get(row_number=1)
        self.assertEqual(row2.outcome, ImportRow.Outcome.CREATE)
        confirm_batch(batch2)

        self.assertEqual(LineMeeting.objects.filter(staff=self.staff).count(), 2)

    # Catches a row with all five note fields blank being saved as a
    # content-free record (matches LineMeeting.is_empty's guard).
    def test_row_with_all_note_fields_blank_is_skipped(self):
        batch = self._upload(
            "staff_email,meeting_date",
            "report@oxlip.test,2026-01-15",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        confirm_batch(batch)
        self.assertFalse(LineMeeting.objects.filter(staff=self.staff).exists())

    # Catches an unmatched staff_email producing a write instead of skip+report.
    def test_unmatched_staff_email_is_skipped(self):
        batch = self._upload(
            "staff_email,meeting_date,main_matters",
            "nobody@oxlip.test,2026-01-15,Some notes.",
        )
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.outcome, ImportRow.Outcome.SKIP)
        confirm_batch(batch)
        self.assertFalse(LineMeeting.objects.filter(meeting_date="2026-01-15").exists())


class ConfirmTimeDriftTests(TestCase):
    """A row that validates fine at upload must degrade to a graceful SKIP —
    not raise, and not roll back the rest of the batch — if the data it
    depends on disappears before confirm.
    """

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.teacher = make_staff(
            "teacher@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.other_teacher = make_staff(
            "other@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.year = make_academic_year(2025)

    # Catches a row valid at upload-time becoming a crash (rather than a
    # SKIP) when its referenced AcademicYear is deleted before confirm, and
    # confirms it doesn't roll back a sibling row in the same batch.
    def test_academic_year_deleted_before_confirm_degrades_to_skip_not_crash(self):
        url = reverse("data_import:upload", args=["appraisal-summaries"])
        csv_file = make_csv(
            "teacher_email,academic_year",
            "teacher@oxlip.test,2025",
            "other@oxlip.test,2025",
        )
        response = self.client.post(url, {"csv_file": csv_file}, follow=True)
        self.assertEqual(response.status_code, 200)
        batch = ImportBatch.objects.filter(
            import_type=ImportType.APPRAISAL_SUMMARY
        ).latest("uploaded_at")

        row1 = batch.rows.get(row_number=1)
        row2 = batch.rows.get(row_number=2)
        self.assertEqual(row1.outcome, ImportRow.Outcome.CREATE)
        self.assertEqual(row2.outcome, ImportRow.Outcome.CREATE)

        # Drift: the year is deleted between upload-time validation and confirm.
        self.year.delete()

        # Must not raise.
        confirm_batch(batch)

        row1.refresh_from_db()
        row2.refresh_from_db()
        self.assertEqual(row1.outcome, ImportRow.Outcome.SKIP)
        self.assertEqual(row2.outcome, ImportRow.Outcome.SKIP)
        self.assertFalse(Appraisal.objects.filter(teacher=self.teacher).exists())
        self.assertFalse(Appraisal.objects.filter(teacher=self.other_teacher).exists())

        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.CONFIRMED)

    # Catches one row's apply-time failure rolling back or blocking a
    # sibling row's successful apply within the same batch.
    def test_one_row_failing_at_confirm_does_not_block_sibling_row(self):
        url = reverse("data_import:upload", args=["appraisal-summaries"])
        csv_file = make_csv(
            "teacher_email,academic_year",
            "teacher@oxlip.test,2025",
            "other@oxlip.test,2025",
        )
        response = self.client.post(url, {"csv_file": csv_file}, follow=True)
        self.assertEqual(response.status_code, 200)
        batch = ImportBatch.objects.filter(
            import_type=ImportType.APPRAISAL_SUMMARY
        ).latest("uploaded_at")

        # Remove just one teacher's StaffMember row before confirm so only
        # that row drifts; the other should still apply successfully.
        self.other_teacher.delete()

        confirm_batch(batch)

        row1 = batch.rows.get(row_number=1)
        row2 = batch.rows.get(row_number=2)
        self.assertEqual(row1.outcome, ImportRow.Outcome.CREATE)
        self.assertEqual(row2.outcome, ImportRow.Outcome.SKIP)
        self.assertTrue(Appraisal.objects.filter(teacher=self.teacher).exists())


class DiscardAndDoubleDecisionTests(TestCase):
    """Discarding writes nothing; confirming/discarding an already-decided
    batch is a no-op, not a second write.
    """

    def setUp(self):
        self.super_user = make_user("admin@oxlip.test", is_superuser=True)
        self.client.force_login(self.super_user)
        self.teacher = make_staff(
            "teacher@oxlip.test", staff_type=StaffMember.StaffType.TEACHING
        )
        self.year = make_academic_year(2025)

    def _upload_appraisal_summary_batch(self):
        url = reverse("data_import:upload", args=["appraisal-summaries"])
        csv_file = make_csv(
            "teacher_email,academic_year", "teacher@oxlip.test,2025"
        )
        response = self.client.post(url, {"csv_file": csv_file}, follow=True)
        self.assertEqual(response.status_code, 200)
        return ImportBatch.objects.filter(
            import_type=ImportType.APPRAISAL_SUMMARY
        ).latest("uploaded_at")

    # Catches a discard action writing data anyway.
    def test_discarding_batch_writes_nothing(self):
        batch = self._upload_appraisal_summary_batch()
        preview_url = reverse("data_import:preview", args=[batch.pk])
        response = self.client.post(preview_url, {"action": "discard"}, follow=True)
        self.assertEqual(response.status_code, 200)

        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.DISCARDED)
        self.assertFalse(Appraisal.objects.filter(teacher=self.teacher).exists())

    # Catches re-confirming an already-confirmed batch applying its rows a
    # second time (e.g. double-creating data via a duplicate browser submit).
    def test_confirming_already_confirmed_batch_is_a_noop(self):
        batch = self._upload_appraisal_summary_batch()
        preview_url = reverse("data_import:preview", args=[batch.pk])

        self.client.post(preview_url, {"action": "confirm"}, follow=True)
        self.assertEqual(Appraisal.objects.filter(teacher=self.teacher).count(), 1)

        # Try to confirm it again via the same endpoint.
        response = self.client.post(preview_url, {"action": "confirm"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appraisal.objects.filter(teacher=self.teacher).count(), 1)

        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.CONFIRMED)

    # Catches discarding an already-confirmed batch silently flipping its
    # status (which would misrepresent an audit trail of data that was written).
    def test_discarding_already_confirmed_batch_does_not_change_status(self):
        batch = self._upload_appraisal_summary_batch()
        preview_url = reverse("data_import:preview", args=[batch.pk])

        self.client.post(preview_url, {"action": "confirm"}, follow=True)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.CONFIRMED)

        response = self.client.post(preview_url, {"action": "discard"}, follow=True)
        self.assertEqual(response.status_code, 200)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.CONFIRMED)
