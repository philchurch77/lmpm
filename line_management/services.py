"""Cross-model workflows for line meetings: carrying actions forward and
recognising a repeated create submission.

Carry-forward is **pinned, not derived**: when a meeting is created, the
unreviewed actions agreed at the report's latest **Held** meeting get ``reviewed_in`` set
to the new meeting, once, inside the create transaction. Deriving "the previous
meeting by date" at read time was rejected — a back-dated or imported meeting
would silently move actions (and their ratings) onto a different meeting.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from .models import LineMeeting, MeetingAction


class CarryForwardChanged(Exception):
    """The actions to carry forward changed while the create was being saved."""


class MeetingChanged(Exception):
    """The meeting (or one of its actions) changed since the page was loaded."""


# --- The meeting version -----------------------------------------------------
#
# A meeting page carries ``LineMeeting.updated_at`` as a hidden version stamp, and
# a save is a compare-and-swap on it: refused (and the typed text handed back) if
# anything changed since the page loaded. So **every write that changes what a
# meeting page may edit must advance that meeting's ``updated_at``** — a page
# save, pinning its actions into the next meeting, an admin edit to one of its
# actions, and an import update. ``touch_meetings`` is the helper for the ones
# that do not go through ``LineMeeting.save()``.


def meeting_version(meeting) -> str:
    """The version stamp a page carries: exact UTC isoformat, microseconds included."""
    return meeting.updated_at.astimezone(dt_timezone.utc).isoformat()


def parse_version(raw):
    """The posted stamp as an aware datetime, or ``None`` if missing or malformed.

    ``None`` is treated as stale by callers — never as "skip the check".
    """
    try:
        value = datetime.fromisoformat(raw or "")
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _next_version(current):
    # Always moves forward, even if the clock has not (coarse clocks, skew).
    return max(timezone.now(), current + timedelta(microseconds=1))


def touch_meetings(*pks):
    """Advance the version of the given meetings (``None`` entries ignored).

    Strictly forward even when the clock has not moved (a raw ``now()`` could
    write back the same stamp and leave an open page valid). Per-row
    compare-and-swap: if a row moved meanwhile, another writer already advanced
    it, which is all this needs.
    """
    ids = {pk for pk in pks if pk is not None}
    for pk, current in LineMeeting.objects.filter(pk__in=ids).values_list("pk", "updated_at"):
        LineMeeting.objects.filter(pk=pk, updated_at=current).update(
            updated_at=_next_version(current)
        )


@transaction.atomic
def save_meeting_page(meeting, version, form, agreed, carried, *, hold=False):
    """Save a meeting page only if the meeting is still at ``version``.

    ``hold`` also marks the meeting Held, inside the same conditional UPDATE, so
    a hold inherits the version check: a stale page can never hold a meeting.
    The caller decides whether the viewer may hold (``can_hold_meeting``).

    One conditional UPDATE writes the new version and the note fields this user
    may edit (never ``form.save()``: that is a full-row write of an instance
    loaded at request start, and ``auto_now`` would restamp the row anyway). On
    Postgres a concurrent writer blocks on the row lock and the WHERE clause is
    re-checked after it commits, so no ``select_for_update`` is needed. Only
    then do the action formsets write; anything they find changed raises
    ``MeetingChanged`` too, rolling the whole save back.
    """
    editable = {
        name: getattr(meeting, name) for name, field in form.fields.items() if not field.disabled
    }
    if hold:
        editable["state"] = LineMeeting.State.HELD
    new_version = _next_version(version)
    updated = LineMeeting.objects.filter(pk=meeting.pk, updated_at=version).update(
        updated_at=new_version, **editable
    )
    if updated != 1:
        raise MeetingChanged
    meeting.updated_at = new_version
    if hold:
        meeting.state = LineMeeting.State.HELD
    agreed.save()
    carried.save()


_LATEST_FIRST = ("-meeting_date", "-created_at", "-pk")


def latest_meeting(member):
    """``member``'s latest meeting in any state (by date, then creation order), or ``None``.

    The only meeting that may record new actions: one added to an older meeting
    would never be carried forward, so it would never come up for review.
    """
    return LineMeeting.objects.filter(staff=member).order_by(*_LATEST_FIRST).first()


def carry_forward_source(member):
    """The meeting whose unreviewed actions a new meeting for ``member`` reviews.

    The latest **Held** meeting by date (ties broken by creation order), or
    ``None``. A meeting still being prepared is never a source: its actions are
    not settled until it has been held.
    """
    return (
        LineMeeting.objects.filter(staff=member, state=LineMeeting.State.HELD)
        .order_by(*_LATEST_FIRST)
        .first()
    )


def held_meeting_summary():
    """Annotations for a ``StaffMember`` queryset: ``meeting_count`` and
    ``last_meeting`` over **Held** meetings only.

    Shared by the My Team and overview dashboards, which report meetings that
    have taken place; a meeting still being prepared has not.
    """
    held = Q(line_meetings__state=LineMeeting.State.HELD)
    return {
        "meeting_count": Count("line_meetings", filter=held),
        "last_meeting": Max("line_meetings__meeting_date", filter=held),
    }


def preparing_meeting(member):
    """``member``'s meeting being prepared (at most one, by constraint), or ``None``."""
    return LineMeeting.objects.filter(staff=member, state=LineMeeting.State.PREPARING).first()


def page_would_be_blank(meeting, agreed, carried) -> bool:
    """Whether a meeting page, as submitted, holds nothing worth keeping.

    The one rule shared by create (a blank meeting is not saved) and hold (a
    blank meeting is not held): no note, no agreed action left after this save,
    and no rating or comment on a carried action. Pinning last meeting's actions
    alone is not content. Call after the forms have validated (``is_valid()``
    has applied the posted notes to ``meeting``).
    """
    return not (meeting.has_notes or carried.has_rating() or agreed.keeps_any_action())


def would_strand_actions(member, meeting_date) -> bool:
    """Whether adding a Held meeting for ``member`` on ``meeting_date`` would strand actions.

    A Held meeting dated on or after a meeting with actions still to review
    becomes the carry-forward source in that meeting's place, so those actions
    would never come up for review. Actions agreed at a meeting being prepared
    count too: once it is held, the later-dated meeting would still be the source.
    """
    return MeetingAction.objects.filter(
        agreed_at__staff=member,
        agreed_at__meeting_date__lte=meeting_date,
        reviewed_in__isnull=True,
    ).exists()


def carried_forward_candidates(source):
    """The actions a new meeting would carry forward from ``source``."""
    if source is None:
        return MeetingAction.objects.none()
    return (
        source.agreed_actions.filter(reviewed_in__isnull=True)
        .select_related("agreed_at")
        .order_by("pk")
    )


def may_carry_from(source, meeting_date) -> bool:
    """A meeting dated before ``source`` must not review its actions."""
    return source is not None and meeting_date >= source.meeting_date


@transaction.atomic
def start_meeting(meeting, *, source, pin, shown_ids=()):
    """Save a new meeting and, if ``pin``, carry ``source``'s unreviewed actions into it.

    ``shown_ids`` are the carried actions the user saw (and may have rated) on
    the page. The pin must carry exactly those: if another request pinned one of
    them first, this request's rating would otherwise be written onto an action
    now reviewed elsewhere; if one was added after the page loaded, it would be
    carried without the user ever seeing it. Either way ``CarryForwardChanged``
    is raised, the caller's transaction rolls back, and ``meeting`` is left
    unsaved again so the page can be re-rendered with the user's text.
    """
    meeting.save()
    if pin and source is not None:
        # The source meeting's open page may no longer change or delete these
        # actions, so its version moves. Touched *before* the actions, so this
        # path and ``save_meeting_page`` both lock the meeting row before the
        # action rows — the other order can deadlock on Postgres.
        touch_meetings(source.pk)
        MeetingAction.objects.filter(agreed_at=source, reviewed_in__isnull=True).update(
            reviewed_in=meeting, updated_at=timezone.now()
        )
        pinned = {str(pk) for pk in meeting.reviewed_actions.values_list("pk", flat=True)}
        if pinned != {str(pk) for pk in shown_ids}:
            meeting.pk = None
            meeting._state.adding = True
            raise CarryForwardChanged
    return meeting


def find_repeat_submission(meeting, agreed_texts, carried_formset_for):
    """An already-saved meeting that this unsaved one exactly repeats, or ``None``.

    Replaces the old notes-only double-submit guard, which would have folded two
    genuine same-day meetings holding only actions into one and lost the
    second's actions. A match needs the same report, date and all five note
    fields, the same agreed-action descriptions in order, and carried-action
    ratings/comments identical to what that meeting already holds.

    ``carried_formset_for(existing)`` binds the submitted carried-action data to
    ``existing.reviewed_actions`` — on a second submit, the first request has
    already pinned those actions to ``existing``.
    """
    candidates = LineMeeting.objects.filter(
        staff=meeting.staff, meeting_date=meeting.meeting_date
    )
    for field in LineMeeting.NOTE_FIELDS:
        candidates = candidates.filter(**{field: getattr(meeting, field)})
    for existing in candidates:
        existing_texts = list(
            existing.agreed_actions.order_by("pk").values_list("description", flat=True)
        )
        if existing_texts != agreed_texts:
            continue
        carried = carried_formset_for(existing)
        if carried.is_valid() and not carried.has_changed():
            return existing
    return None
