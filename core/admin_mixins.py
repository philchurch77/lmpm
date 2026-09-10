"""Shared admin behaviour for models holding staff-entered text."""
from __future__ import annotations


class SuperuserOnlyDeleteMixin:
    """Restrict deletion of a staff-text model to superusers.

    Deleting one ``Appraisal`` in the admin cascades every piece of writing
    underneath it — goals, the self-review, its items and per-bullet scores, the
    leader review and its standards — in a single confirm, with no undo and
    (today) no database backup behind it.

    Reaching the admin at all needs ``is_staff`` plus an explicit delete
    permission, and ``core.provisioning`` creates plain non-staff users, so no
    imported staff member can get near it. But non-superusers are deliberately
    given staff-admin rights over ``StaffMember``, so the surface is intended to
    exist and this narrows what can be destroyed from it. Deletion of a year of
    someone's PD record should be a superuser's deliberate act.

    Mixed in before ``ModelAdmin`` so it takes precedence; it also removes the
    bulk "Delete selected" action, which Django otherwise offers independently of
    ``has_delete_permission`` on the changelist.
    """

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("delete_selected", None)
        return actions
