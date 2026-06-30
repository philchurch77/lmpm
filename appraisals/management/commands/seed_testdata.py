"""Seed a self-contained test cohort for exercising the appraisal flows locally.

Creates a small line-management hierarchy with logins, so you can sign in as an
ordinary teacher and an ordinary coach (not just as a superuser, who bypasses the
role checks) and walk the real teacher / coach / "My Team" journeys.

Identity is by email (a Django ``User`` is matched to a ``StaffMember`` via email;
the coach link is ``performance_manager_email``). All users get the same password
so you can log in at ``/accounts/login/`` with email + password — Microsoft SSO is
not required locally.

Safe to run repeatedly: every object is keyed by email and re-running resets the
passwords back to the known value.

Hierarchy created (all @test.local):
    head      coaches  coach
    coach     coaches  teacher, support
    teacher   (TEACHING)   — current + a signed-off previous appraisal
    support   (SUPPORT)    — current appraisal
    coach     (TEACHING)   — current appraisal (so the coach is also appraised)
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import School, SchoolProfile, StaffMember
from appraisals.models import AcademicYear, Appraisal, SelfReview

DOMAIN = "test.local"


class Command(BaseCommand):
    help = (
        "Seed a test cohort (school, current/previous academic years, users with "
        "logins, staff hierarchy and appraisals) for exercising the appraisal flows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default="testpass123",
            help="Password set on every seeded user (default: testpass123).",
        )
        parser.add_argument(
            "--year",
            type=int,
            default=2025,
            help="Start year of the current academic year, e.g. 2025 for 2025/26.",
        )

    def handle(self, *args, **options):
        password = options["password"]
        current_year = options["year"]

        school = self._school()
        previous, current = self._years(current_year)

        # email -> (staff_type, performance_manager_email)
        head = f"head@{DOMAIN}"
        coach = f"coach@{DOMAIN}"
        teacher = f"teacher@{DOMAIN}"
        support = f"support@{DOMAIN}"

        people = [
            (head, StaffMember.StaffType.TEACHING, "", "Headteacher"),
            (coach, StaffMember.StaffType.TEACHING, head, "Head of Department"),
            (teacher, StaffMember.StaffType.TEACHING, coach, "Classroom Teacher"),
            (support, StaffMember.StaffType.SUPPORT, coach, "Teaching Assistant"),
        ]

        for email, staff_type, manager, job_title in people:
            self._user(email, password, school)
            self._staff(email, staff_type, manager, job_title, school)

        # Current-year appraisals (these are the ones you'll fill in when testing).
        self._appraisal(teacher, current, coach)
        self._appraisal(support, current, coach)
        self._appraisal(coach, current, head)

        # A signed-off previous-year appraisal for the teacher, so the "Last Year"
        # tab has content to display.
        self._appraisal(
            teacher,
            previous,
            coach,
            status=Appraisal.Status.SIGNED_OFF,
            review_comments=True,
        )

        self.stdout.write(self.style.SUCCESS("\nTest cohort ready."))
        self.stdout.write(
            "Log in at /accounts/login/ with these emails "
            f"(all password '{password}'):"
        )
        for email, _type, manager, _title in people:
            role = "coach + teacher" if manager and email == coach else "teacher"
            if email == head:
                role = "coach (top of tree)"
            self.stdout.write(f"  {email:22} — {role}")
        self.stdout.write(
            "\nTip: sign in as teacher@ to test the teacher view, coach@ to test "
            '"My Team" and coach sign-off. Avoid testing only as your superuser '
            "account — superusers bypass the role checks."
        )

    # --- helpers ---------------------------------------------------------------

    def _school(self) -> School:
        school, _ = School.objects.get_or_create(
            name="Copleston High School",
            defaults={"phase": School.Phase.SECONDARY},
        )
        return school

    def _years(self, current_start: int):
        previous, _ = AcademicYear.objects.get_or_create(start_year=current_start - 1)
        current, _ = AcademicYear.objects.get_or_create(start_year=current_start)
        # save() enforces single-current; this makes the seeded year the live one.
        current.is_current = True
        current.save()
        return previous, current

    def _user(self, email: str, password: str, school: School) -> User:
        user, _ = User.objects.get_or_create(
            username=email,
            defaults={"email": email},
        )
        user.email = email
        user.is_staff = False
        user.is_superuser = False
        user.set_password(password)
        user.save()

        profile, _ = SchoolProfile.objects.get_or_create(
            user=user,
            defaults={"school": school},
        )
        profile.schools.add(school)
        return user

    def _staff(
        self,
        email: str,
        staff_type: str,
        manager_email: str,
        job_title: str,
        school: School,
    ) -> StaffMember:
        staff, _ = StaffMember.objects.get_or_create(email=email)
        # Line management and performance management point at the same person here.
        staff.line_manager_email = manager_email
        staff.performance_manager_email = manager_email
        staff.staff_type = staff_type
        staff.job_title = job_title
        staff.school = school
        staff.save()
        return staff

    def _appraisal(
        self,
        teacher_email: str,
        year: AcademicYear,
        coach_email: str,
        *,
        status: str = Appraisal.Status.DRAFT,
        review_comments: bool = False,
    ) -> Appraisal:
        teacher = StaffMember.objects.get(email=teacher_email)
        appraisal, created = Appraisal.objects.get_or_create(
            teacher=teacher,
            academic_year=year,
            defaults={"coach_email": coach_email, "status": status},
        )
        if not created:
            appraisal.coach_email = coach_email
            appraisal.status = status
            appraisal.save()

        # Seed the child rows the views would otherwise create on first visit.
        appraisal.seed_goals()
        kind = (
            SelfReview.Kind.SUPPORT
            if teacher.staff_type == StaffMember.StaffType.SUPPORT
            else SelfReview.Kind.TEACHING
        )
        self_review = getattr(appraisal, "self_review", None)
        if self_review is None:
            self_review = SelfReview.objects.create(appraisal=appraisal, kind=kind)
        self_review.seed_items()

        if review_comments:
            for goal in appraisal.goals.all():
                if not goal.teacher_review_comment:
                    goal.teacher_review_comment = (
                        "Made good progress against this goal over the year."
                    )
                    goal.coach_review_comment = "Agreed — well evidenced."
                    goal.save(
                        update_fields=[
                            "teacher_review_comment",
                            "coach_review_comment",
                        ]
                    )

        return appraisal
