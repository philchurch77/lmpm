"""Views for the line-management meeting UI.

All views are function-based and require login. Access to a specific meeting
always routes through ``get_meeting_or_403`` (and manager-only actions through
``get_managed_staff_or_403``) to prevent IDOR, and field-level editing is gated
inside the forms by the ``can_edit`` flag.

A meeting page is three forms posted together (see ``_bind``): ``LineMeetingForm``
(date and prose notes), the agreed-actions formset (prefix ``agreed``) and the
carried-actions formset (prefix ``carried``, RAG + comment). Each formset's
queryset is derived server-side from the meeting, so a crafted row id outside it
is ignored — it can never write to an action outside this meeting.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.identity import current_staff_member
from core.recovery import render_save_blocked

from .forms import AgreedActionFormSet, CarriedActionFormSet, LineMeetingForm
from .models import ROTATION_GUIDANCE, LineMeeting, MeetingAction
from .permissions import (
    ROLE_MANAGER,
    ROLE_SUPER,
    can_edit_meeting,
    can_hold_meeting,
    get_managed_staff_or_403,
    get_meeting_or_403,
    line_managed_staff,
)
from .services import (
    CarryForwardChanged,
    MeetingChanged,
    carried_forward_candidates,
    carry_forward_source,
    find_repeat_submission,
    latest_meeting,
    may_carry_from,
    meeting_version,
    page_would_be_blank,
    parse_version,
    preparing_meeting,
    save_meeting_page,
    start_meeting,
)

AGREED_PREFIX = "agreed"
CARRIED_PREFIX = "carried"
# The name of the "Save and mark as held" submit button.
HOLD_FIELD = "hold"


@login_required
def my_meetings(request):
    """The signed-in user's line-meeting records.

    Two sections: meetings about the user themselves (read-only, as the report)
    and meetings for the people they *currently* line-manage (editable). The
    second list uses the same live line-manager lookup as the access rule, so
    every meeting shown is one the user can open and edit right now.
    """
    staff = current_staff_member(request)
    if staff is None:
        return render(request, "line_management/no_staff.html")

    own_meetings = LineMeeting.objects.filter(staff=staff)
    hosted_meetings = LineMeeting.objects.filter(
        staff__in=line_managed_staff(staff)
    ).select_related("staff")
    return render(
        request,
        "line_management/my_meetings.html",
        {"meetings": own_meetings, "hosted_meetings": hosted_meetings},
    )


@login_required
def staff_meetings(request, staff_pk):
    """One line-managed person's meetings, with a New meeting action."""
    member, _staff = get_managed_staff_or_403(request, staff_pk)
    meetings = LineMeeting.objects.filter(staff=member)
    return render(
        request,
        "line_management/staff_meetings.html",
        {"member": member, "meetings": meetings, "preparing": preparing_meeting(member)},
    )


def _carried_formset(queryset, *, can_edit, data=None):
    return CarriedActionFormSet(
        data, queryset=queryset, prefix=CARRIED_PREFIX, can_edit=can_edit
    )


def _bind(meeting, carried_queryset, *, can_edit, data=None):
    """The three forms of a meeting page: (notes form, agreed formset, carried formset)."""
    form = LineMeetingForm(data, instance=meeting, can_edit=can_edit)
    # New actions only on the latest meeting: one added to an older meeting would
    # never be carried forward, so it would never come up for review.
    # Any state: a meeting being prepared is the latest, and records new actions.
    is_latest = meeting.pk is None or latest_meeting(meeting.staff).pk == meeting.pk
    agreed = AgreedActionFormSet(
        data,
        instance=meeting,
        prefix=AGREED_PREFIX,
        queryset=MeetingAction.objects.select_related("reviewed_in").order_by("pk"),
        can_edit=can_edit,
        can_add=is_latest,
    )
    carried = _carried_formset(carried_queryset, can_edit=can_edit, data=data)
    return form, agreed, carried


def _typed_ratings(carried, member):
    """What the user typed against carried actions on a page that has gone stale.

    Echoed back so nothing typed is lost when the carried list has to be rebuilt.
    Only the submitted text is echoed; an action's wording is shown only when it
    belongs to ``member`` (whom the viewer already manages), so a crafted id can
    never reveal someone else's action.
    """
    labels = dict(MeetingAction.Rag.choices)
    rows = []
    for f in carried.forms:
        rag = f.data.get(f.add_prefix("rag"), "")
        comment = f.data.get(f.add_prefix("review_comment"), "").strip()
        if rag or comment:
            rows.append({"id": f.data.get(f.add_prefix("id")), "rag": labels.get(rag, ""), "comment": comment})
    own = {
        a.pk: a
        for a in MeetingAction.objects.filter(
            pk__in=[r["id"] for r in rows if str(r["id"]).isdigit()], agreed_at__staff=member
        ).select_related("reviewed_in")
    }
    for r in rows:
        action = own.get(int(r["id"])) if str(r["id"]).isdigit() else None
        r["description"] = action.description if action else None
        # Where it went: so the user knows which rating now counts.
        r["reviewed_on"] = action.reviewed_in.meeting_date if action and action.reviewed_in else None
    return rows


def _render(request, *, meeting, member, role, can_edit, forms, form_action, is_new, source=None,
            typed_ratings=()):
    form, agreed, carried = forms
    # "Save and mark as held" is offered while the meeting is new or being
    # prepared, and only to someone who may hold it. A Held meeting is never
    # returned to preparing, so it shows a single Save.
    can_hold = can_hold_meeting(role) and (is_new or not meeting.is_held)
    return render(
        request,
        "line_management/meeting_detail.html",
        {
            "meeting": None if is_new else meeting,
            "member": member,
            "role": role,
            "can_edit": can_edit,
            "can_hold": can_hold,
            "form": form,
            "agreed_formset": agreed,
            "carried_formset": carried,
            "source": source,
            "typed_ratings": typed_ratings,
            "form_action": form_action,
            "is_new": is_new,
            # The meeting's current version. For an invalid re-render in
            # meeting_save that is only safe because the version pre-check already
            # proved the posted stamp *was* current — without that pre-check a
            # stale page would be re-rendered carrying the fresh stamp, and its next
            # Save would pass the compare-and-swap and overwrite the other writer.
            "meeting_version": "" if is_new else meeting_version(meeting),
            "rotation_guidance": ROTATION_GUIDANCE,
        },
    )


def _render_new(request, member, source, forms, typed_ratings=()):
    """Render the create form. Only managers/superusers reach the create views."""
    return _render(
        request,
        typed_ratings=typed_ratings,
        meeting=None,
        member=member,
        role=ROLE_SUPER if request.user.is_superuser else ROLE_MANAGER,
        can_edit=True,
        forms=forms,
        form_action=reverse("line_management:meeting_create", args=[member.pk]),
        is_new=True,
        source=source,
    )


def _render_existing(request, meeting, role, can_edit, forms):
    return _render(
        request,
        meeting=meeting,
        member=meeting.staff,
        role=role,
        can_edit=can_edit,
        forms=forms,
        form_action=reverse("line_management:meeting_save", args=[meeting.pk]),
        is_new=False,
    )


def _reviewed_actions(meeting):
    return meeting.reviewed_actions.select_related("agreed_at").order_by("pk")


@login_required
def meeting_new(request, staff_pk):
    """Render a blank meeting form for a line-managed person.

    Nothing is persisted here — the record is only written when the manager
    submits the form (see ``meeting_create``), so abandoning a "New meeting"
    click leaves no record behind and pins no actions.
    """
    member, _staff = get_managed_staff_or_403(request, staff_pk)
    preparing = preparing_meeting(member)
    if preparing is not None:
        # At most one meeting per person is being prepared; carry on with it.
        messages.info(
            request,
            "A meeting is already being prepared for this person, so it has been opened "
            "instead of a new one.",
        )
        return redirect("line_management:meeting_detail", pk=preparing.pk)
    source = carry_forward_source(member)
    meeting = LineMeeting(staff=member, meeting_date=timezone.localdate())
    forms = _bind(meeting, carried_forward_candidates(source), can_edit=True)
    return _render_new(request, member, source, forms)


@login_required
@require_POST
def meeting_create(request, staff_pk):
    """Persist a new meeting from the submitted forms (create-on-save).

    Saving creates the meeting, carries the last Held meeting's unreviewed
    actions into it, stores their ratings and stores the newly agreed actions —
    all in one transaction. "Save" leaves it being prepared; "Save and mark as
    held" (``HOLD_FIELD``) creates it Held.
    """
    member, _staff = get_managed_staff_or_403(request, staff_pk)
    if f"{AGREED_PREFIX}-TOTAL_FORMS" not in request.POST:
        return _refuse_out_of_date(request, member)
    # Only managers and superusers reach this view, and both may hold.
    hold = HOLD_FIELD in request.POST
    source = carry_forward_source(member)
    candidates = carried_forward_candidates(source)
    meeting = LineMeeting(
        staff=member,
        created_by_email=request.user.email or "",
        state=LineMeeting.State.HELD if hold else LineMeeting.State.PREPARING,
    )
    forms = _bind(meeting, candidates, can_edit=True, data=request.POST)
    form, agreed, carried = forms

    def refuse(message):
        messages.error(request, message)
        return _render_new(request, member, source, forms)

    def refuse_stale():
        # The last meeting's actions are no longer the ones this page showed
        # (another meeting was created, or an action added, since it loaded).
        # Rebuild that list from the current state — re-rendering the stale rows
        # would fail the same way on every save — keep the notes and new actions
        # bound, and echo back any rating or comment typed on the old list.
        messages.error(
            request,
            "The actions from the last meeting changed while you were writing (another "
            "meeting may have been saved), so nothing was saved. The list below is up to "
            "date and the rest of your text is kept. Check what you typed against it, then "
            "save again.",
        )
        current = carry_forward_source(member)
        fresh = (form, agreed, _carried_formset(carried_forward_candidates(current), can_edit=True))
        return _render_new(request, member, current, fresh, _typed_ratings(carried, member))

    def find_repeat():
        # Double-submit guard. Checked before the carried formset is validated:
        # on the second request of a double-click, the first request has already
        # pinned the carried actions to its new meeting, so they no longer belong
        # to this request's candidates. Only an exact repeat (notes, agreed
        # actions and carried ratings) is folded in; a genuine second meeting is
        # still created. A "Save and mark as held" folds only into a meeting
        # that is already Held — otherwise the hold would be dropped in silence.
        if not (form.is_valid() and agreed.is_valid()):
            return None
        duplicate = find_repeat_submission(
            meeting,
            agreed.new_descriptions(),
            lambda existing: _carried_formset(
                existing.reviewed_actions.all(), can_edit=True, data=request.POST
            ),
        )
        if duplicate is not None and hold and not duplicate.is_held:
            return None
        return duplicate

    duplicate = find_repeat()
    if duplicate is not None:
        return _fold(request, duplicate)

    preparing = preparing_meeting(member)
    if preparing is not None:
        return _refuse_preparing(request, preparing, member)

    if carried.posted_ids() != {str(pk) for pk in candidates.values_list("pk", flat=True)}:
        return refuse_stale()

    if not (form.is_valid() and agreed.is_valid() and carried.is_valid()):
        return refuse("Please correct the errors below.")

    # is_valid() has applied the cleaned notes to ``meeting``.
    if page_would_be_blank(meeting, agreed, carried):
        return refuse("Add at least one note or action before saving the meeting.")

    pin = may_carry_from(source, meeting.meeting_date)
    back_dated = source is not None and not pin
    if back_dated and not hold:
        # Dated before the last held meeting, it could never review anything,
        # yet it would take the one being-prepared slot.
        form.add_error(
            "meeting_date",
            f"This date is before the last held meeting ({source.meeting_date:%d/%m/%Y}). "
            "A meeting dated before it can only be recorded as already held: use “Save and "
            "mark as held”, or change the date.",
        )
        return refuse("Please correct the errors below.")
    if back_dated and (carried.has_rating() or agreed.new_descriptions()):
        form.add_error(
            "meeting_date",
            f"This date is before the last meeting ({source.meeting_date:%d/%m/%Y}). Its "
            "actions can't be reviewed here, and new actions recorded here would never come "
            "up for review. Change the date, or keep this meeting to notes only.",
        )
        return refuse("Please correct the errors below.")

    try:
        with transaction.atomic():
            start_meeting(meeting, source=source, pin=pin, shown_ids=carried.posted_ids())
            if pin:
                carried.save()
            agreed.save()
    except CarryForwardChanged:
        return refuse_stale()
    except IntegrityError:
        # The one-preparing-per-person constraint: another request started a
        # meeting between the checks above and this insert (a racing
        # double-click, or the other party). Nothing was saved, so fold into an
        # exact repeat or hand the text back.
        duplicate = find_repeat()
        if duplicate is not None:
            return _fold(request, duplicate)
        preparing = preparing_meeting(member)
        if preparing is None:
            # The racing meeting has gone again, or this was some other
            # constraint. Nothing was saved either way: hand the text back.
            return render_save_blocked(
                request,
                heading="This meeting could not be saved",
                explanation=(
                    "Something changed for this person while you were writing (another meeting "
                    "may have been saved), so nothing from this page was saved. Everything you "
                    "typed is below: open their meetings and add it back in."
                ),
                back_url=reverse("line_management:staff_meetings", args=[member.pk]),
                back_label="Open this person's meetings",
                labels=_handback_labels(request.POST, _member_actions(member)),
            )
        return _refuse_preparing(request, preparing, member)

    messages.success(request, _saved_message(meeting.is_held))
    if back_dated and carried.forms:
        messages.info(
            request,
            f"This meeting is dated before the meeting of {source.meeting_date:%d/%m/%Y}, so "
            "that meeting's actions were not reviewed here. They will appear on the next meeting.",
        )
    return redirect("line_management:meeting_detail", pk=meeting.pk)


@login_required
def meeting_detail(request, pk):
    meeting, _staff, role = get_meeting_or_403(request, pk)
    can_edit = can_edit_meeting(role)
    forms = _bind(meeting, _reviewed_actions(meeting), can_edit=can_edit)
    return _render_existing(request, meeting, role, can_edit, forms)


@login_required
@require_POST
def meeting_save(request, pk):
    # NOTE: a manager who loses the line-management link mid-edit gets a 403 and
    # their unsaved notes are discarded. That is deliberate and is left alone:
    # once the link is repointed the viewer has no role at all, which is
    # indistinguishable at this point from a stranger probing a guessed pk, and
    # ManagerChangeInheritanceTests fixes the rule that the outgoing manager
    # loses access *even though they authored the record*. Handing text back here
    # would mean answering an IDOR probe with something other than a 403, which
    # is the worse trade. The equivalent appraisal case is handled (see
    # _save_section) because there the viewer keeps their role and only the lock
    # changes — a conflict, not a revocation.
    meeting, _staff, role = get_meeting_or_403(request, pk)
    if not can_edit_meeting(role):
        raise PermissionDenied("You may not edit this meeting record.")

    # Only a viewer who may edit reaches the version check, so the hand-back
    # (409) is never shown to someone who has just lost access (403 above).
    # This pre-check is NOT redundant with the compare-and-swap in
    # save_meeting_page: it is what stops an invalid stale POST re-rendering with
    # the current stamp (see _render), which would launder a stale page.
    # Holding is honoured only for someone who may hold, on a meeting still being
    # prepared. There is no way back from Held, and no posted value can return a
    # meeting to preparing: ``state`` is on no form.
    hold = HOLD_FIELD in request.POST and can_hold_meeting(role) and not meeting.is_held
    version = parse_version(request.POST.get("meeting_version"))
    if version is None or version != meeting.updated_at:
        if _is_exact_repeat(meeting, request.POST):
            # A double-clicked Save, or a stale page with nothing changed: what
            # was posted is already what is stored, so nothing can be lost. No
            # second "Meeting saved." — the first submit already queued one.
            return redirect("line_management:meeting_detail", pk=meeting.pk)
        return _stale_save(request, meeting)

    forms = _bind(meeting, _reviewed_actions(meeting), can_edit=True, data=request.POST)
    form, agreed, carried = forms
    valid = form.is_valid() and agreed.is_valid() and carried.is_valid()
    if valid and hold and page_would_be_blank(meeting, agreed, carried):
        form.add_error(
            None,
            "A blank meeting can't be marked as held. Add at least one note or action, "
            "then save again.",
        )
        valid = False
    if valid:
        # One save is one transaction: never the notes without the actions.
        try:
            save_meeting_page(meeting, version, form, agreed, carried, hold=hold)
        except (MeetingChanged, OperationalError):
            # OperationalError: e.g. Postgres aborting this transaction as a
            # deadlock victim against a concurrent writer. The save rolled back
            # either way, so hand the text back rather than a 500 that loses it.
            return _stale_save(request, meeting)
        messages.success(request, _saved_message(hold))
        return redirect("line_management:meeting_detail", pk=meeting.pk)

    messages.error(request, "Please correct the errors below.")
    return _render_existing(request, meeting, role, True, forms)


def _is_exact_repeat(meeting, post):
    """Whether ``post`` would change nothing on the meeting as it is stored now.

    Binds the three forms against the current record. Notes, carried ratings and
    the existing action rows must be valid and unchanged. New actions typed into
    blank rows count as a repeat only if they are exactly the newest actions now
    stored on this meeting, in order — i.e. the previous submit of this same page
    created them. Anything else (a changed note, a different rating, a delete
    tick, a row that no longer validates) is not a repeat. Nor is a "Save and
    mark as held" of a meeting not yet Held: folding it would drop the hold.
    """
    if HOLD_FIELD in post and not meeting.is_held:
        return False
    form, agreed, carried = _bind(meeting, _reviewed_actions(meeting), can_edit=True, data=post)
    if not all(f.is_valid() for f in (form, agreed, carried)):
        return False
    if form.has_changed() or carried.has_changed():
        return False
    if any(f.has_changed() for f in agreed.initial_forms):
        return False
    typed = agreed.new_descriptions()
    if not typed:
        return True
    newest = list(
        meeting.agreed_actions.order_by("-pk").values_list("description", flat=True)[: len(typed)]
    )
    return newest[::-1] == typed


_NOTE_LABELS = {
    "meeting_date": "Date of meeting",
    "upcoming": "Upcoming events / tasks / actions",
    "rotation_update": "Rotation update",
    "main_matters": "Main matters to discuss",
}


def _saved_message(held):
    return "Meeting saved and marked as held." if held else "Meeting saved."


def _fold(request, duplicate):
    """Answer a repeated create with the meeting it repeats (nothing new is saved).

    No second "Meeting saved." — the first submit already queued one, as in
    ``meeting_save``'s repeat path.
    """
    return redirect("line_management:meeting_detail", pk=duplicate.pk)


def _refuse_out_of_date(request, member):
    # A page from before actions were rows (e.g. open across a deploy) posts
    # fields no form reads any more; a bound re-render would drop them.
    return render_save_blocked(
        request,
        heading="This page is out of date",
        explanation=(
            "The meeting form changed after you opened this page, so nothing was saved. "
            "Everything you typed is below: start the new meeting again and add it back in."
        ),
        back_url=reverse("line_management:meeting_new", args=[member.pk]),
        back_label="Start the meeting again",
        labels={
            **_NOTE_LABELS,
            "actions_from_last_meeting": "Actions from the last meeting",
            "actions_from_meeting": "Actions from this meeting",
        },
    )


def _member_actions(member):
    """``member``'s actions: used only to label a refused create's ratings by wording.

    Only managers and superusers reach the create view, and they may read these.
    """
    return MeetingAction.objects.filter(agreed_at__staff=member)


def _refuse_preparing(request, preparing, member):
    """Refuse a create while another meeting for the person is being prepared.

    The text is handed back rather than merged in: the other meeting may hold
    different notes and ratings, and guessing which wins would lose some. Only
    managers and superusers reach the create view, so linking to it is safe.
    """
    return render_save_blocked(
        request,
        heading="A meeting is already being prepared",
        explanation=(
            "A meeting for this person is already being prepared — possibly by you, in "
            "another tab or window — so nothing from this page was saved. Everything you "
            "typed is below: open the meeting being prepared and add anything that is "
            "missing from it."
        ),
        back_url=reverse("line_management:meeting_detail", args=[preparing.pk]),
        back_label="Open the meeting being prepared",
        labels=_handback_labels(request.POST, _member_actions(member)),
    )


def _handback_labels(post, actions):
    """Readable labels for the hand-back page.

    Reads action wording from the record, which the recovery page otherwise never
    does — safe here because only a viewer who may edit the meeting gets this far.
    Wording is looked up only among ``actions`` (this meeting's reviewed actions,
    or for a refused create the person's own), so a crafted id labels nothing.
    """
    wording = {str(pk): text for pk, text in actions.values_list("pk", "description")}
    labels = dict(_NOTE_LABELS)
    for key in post:
        parts = key.split("-")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        prefix, index, field = parts
        row = int(index) + 1
        if prefix == AGREED_PREFIX and field == "description":
            # Tell apart rows that were already saved from new ones to add back.
            if post.get(f"{prefix}-{index}-id"):
                labels[key] = f"Action from this meeting {row} (already saved — check for changes)"
            else:
                labels[key] = f"New action from this meeting (row {row})"
        elif prefix == CARRIED_PREFIX and field in ("rag", "review_comment"):
            text = wording.get(post.get(f"{prefix}-{index}-id", ""))
            what = "Rating" if field == "rag" else "Comment"
            labels[key] = f"{what} for: {text}" if text else f"{what} (row {row})"
    return labels


def _stale_save(request, meeting):
    explanation = (
        "Someone saved this meeting after you opened it — possibly you, in another tab "
        "or window. To avoid overwriting their changes, nothing from this page was saved. "
        "Everything you typed is below: open the meeting again to see the latest version, "
        "then add your changes back in."
    )
    if HOLD_FIELD in request.POST:
        explanation += " It was not marked as held."
    return render_save_blocked(
        request,
        heading="This meeting was changed while you were working",
        explanation=explanation,
        back_url=reverse("line_management:meeting_detail", args=[meeting.pk]),
        back_label="Open the latest version of this meeting",
        labels=_handback_labels(request.POST, meeting.reviewed_actions.all()),
    )
