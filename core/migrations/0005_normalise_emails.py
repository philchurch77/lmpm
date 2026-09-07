"""Lower-case the email addresses identity is matched on.

Identity in this project is an email string compared across tables with no
foreign key between them (see ``core.identity``). ``StaffMember`` and the
appraisal/line-meeting models normalise in ``save()``, but ``auth.User`` is a
third-party model that normalises nowhere — so a hand-created or legacy account
could sit in the database as ``A.Green@School.uk`` while every record the app
holds for that person said ``a.green@school.uk``. Reads compensate with
``iexact`` / ``Lower()``, and a single query that forgot to was a real bug.

This normalises the stored data once. Writes are kept normalised by
``core.admin.NormalisingUserAdmin`` and by ``core.provisioning``.

Not touched:
* ``User.username`` — identity is by email, the username is cosmetic, and it
  carries a unique constraint that rewriting could collide with.
* allauth's ``EmailAddress`` rows — a third-party app's data, which it already
  matches case-insensitively itself.
"""
from django.db import migrations
from django.db.models.functions import Lower


def normalise(apps, schema_editor):
    User = apps.get_model("auth", "User")
    StaffMember = apps.get_model("core", "StaffMember")

    # auth.User.email has no unique constraint, so a bulk update is safe. Two
    # accounts differing only by case become visibly identical — but they were
    # already duplicates as far as the SSO gate was concerned (it matches with
    # iexact), so this reveals an existing ambiguity rather than creating one.
    # check_readiness reports it as a blocker.
    User.objects.exclude(email="").exclude(email=Lower("email")).update(
        email=Lower("email")
    )

    # StaffMember.email IS unique, so a blanket lower() could raise
    # IntegrityError mid-migration and abort a deploy. These rows should already
    # be normalised (save() does it); only ones written by a bulk .update() or a
    # raw import can be off. Skip any row whose lower-cased form is already
    # taken and leave it for check_readiness to surface, rather than failing the
    # whole migration over a pre-existing duplicate.
    taken = set(StaffMember.objects.values_list("email", flat=True))
    for staff in StaffMember.objects.exclude(email=Lower("email")):
        lowered = (staff.email or "").strip().lower()
        if lowered and lowered not in taken:
            taken.discard(staff.email)
            taken.add(lowered)
            StaffMember.objects.filter(pk=staff.pk).update(email=lowered)

    # Manager links are plain (non-unique) email columns pointing at the above,
    # so they must be normalised too or the reporting relationships they drive
    # would stop resolving.
    for field in ("line_manager_email", "performance_manager_email"):
        StaffMember.objects.exclude(**{field: ""}).exclude(
            **{field: Lower(field)}
        ).update(**{field: Lower(field)})


def noop(apps, schema_editor):
    """Irreversible in substance: the original casing is not recorded anywhere.

    Deliberately a no-op rather than absent, so the migration can still be
    reversed past without blocking an unrelated rollback.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_alter_staffmember_staff_type"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(normalise, noop)]
