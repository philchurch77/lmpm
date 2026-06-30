from django.db import models
from django.contrib.auth.models import User


class School(models.Model):
    """A school within the trust. Staff are scoped to one or more schools."""

    class Phase(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        SECONDARY = "SECONDARY", "Secondary"

    name = models.CharField(max_length=200)
    phase = models.CharField(max_length=20, choices=Phase.choices, blank=True, default="")
    logo = models.ImageField(upload_to="school_logos/", blank=True, null=True)

    def __str__(self):
        return self.name


class SchoolProfile(models.Model):
    """Links a Django User to the school(s) they belong to.

    Existence of a SchoolProfile is also the authorisation gate for Microsoft
    SSO sign-in (see core.allauth_adapters.RestrictMicrosoftLoginAdapter).
    """

    school = models.ForeignKey(School, on_delete=models.CASCADE)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    schools = models.ManyToManyField(
        School,
        blank=True,
        related_name="school_profiles",
    )

    def __str__(self):
        return f"{self.user.username} - {self.school.name}"


class StaffMember(models.Model):
    """A member of staff and their line/performance management relationships.

    Management relationships are stored by email rather than as foreign keys so
    that a staff row can be imported (e.g. from a CSV) before the manager's own
    record exists. Emails are normalised to lower case on save.
    """

    class StaffType(models.TextChoices):
        TEACHING = "TEACHING", "Teaching"
        SUPPORT = "SUPPORT", "Support"

    email = models.EmailField(unique=True)
    line_manager_email = models.EmailField(blank=True, default="")
    performance_manager_email = models.EmailField(blank=True, default="")

    department = models.CharField(max_length=200, blank=True, default="")
    job_title = models.CharField(max_length=200, blank=True, default="")
    # Drives which self-review form applies. Blank = unclassified.
    staff_type = models.CharField(
        max_length=20,
        choices=StaffType.choices,
        blank=True,
        default="",
    )
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="staff_members",
        null=True,
        blank=True,
    )

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        self.line_manager_email = self.line_manager_email.strip().lower()
        self.performance_manager_email = self.performance_manager_email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class Branding(models.Model):
    """Single-row branding record (trust emblem)."""

    trust_emblem = models.ImageField(upload_to="branding/", blank=True, null=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "Branding"
