"""Forms for the line-meeting record.

``LineMeetingForm`` covers the meeting date and the three note sections still
written as prose. Actions are rows (``MeetingAction``) edited through two
formsets: ``AgreedActionFormSet`` (actions agreed at this meeting) and
``CarriedActionFormSet`` (actions carried in from the last meeting, RAG-rated
here).

Only the current line manager may edit: when ``can_edit`` is False every field is
set ``disabled`` so Django ignores any submitted value (the real security
boundary, not template hiding).

The legacy free-text ``actions_from_last_meeting`` / ``actions_from_meeting``
fields are deliberately not on any form, so no save can ever overwrite them; the
template shows stored text read-only.

Each formset saves **only the columns it owns** (``update_fields``). The rows are
loaded when the request starts; saving the whole row would write back a stale
``reviewed_in`` and could undo a pin made in the same request, or wipe a rating
written at a later meeting.
"""
from __future__ import annotations

from django import forms
from django.forms import BaseInlineFormSet, BaseModelFormSet
from django.utils import timezone

from .models import LineMeeting, MeetingAction
from .services import MeetingChanged


def _disable_all(form):
    for field in form.fields.values():
        field.disabled = True


def _normalise(text):
    return text.replace("\r\n", "\n").strip()


def _restrict_id_to_queryset(formset, form):
    # Django builds a model formset's hidden ``id`` field over every row in the
    # table. A posted id outside this formset's queryset then validates, gets a
    # blank instance and is skipped on save — silently dropping what was typed
    # against it. Restricting the field makes such a row a visible error.
    if "id" in form.fields:
        form.fields["id"].queryset = formset.get_queryset()
        form.fields["id"].label = "Action"
        form.fields["id"].error_messages["invalid_choice"] = (
            "An action on this page was removed or moved since you opened it. "
            "Reload the meeting to see the current actions; your text is kept below."
        )


class LineMeetingForm(forms.ModelForm):
    class Meta:
        model = LineMeeting
        fields = (
            "meeting_date",
            "upcoming",
            "rotation_update",
            "main_matters",
        )
        # Line-management meetings can be very in-depth, so these notes are not
        # word-capped. data-max-words="0" opts each textarea out of the shared
        # client-side word limit guard (core/static/core/word_limit.js).
        widgets = {
            "meeting_date": forms.DateInput(attrs={"type": "date"}),
            "upcoming": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
            "rotation_update": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
            "main_matters": forms.Textarea(
                attrs={"rows": 4, "data-max-words": "0", "aria-labelledby": "main-matters-heading"}
            ),
        }

    def __init__(self, *args, can_edit=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_edit:
            _disable_all(self)

    def clean_meeting_date(self):
        """A saved meeting's date may not move so that its actions leave the review cycle.

        (Create-time back-dating is checked in ``meeting_create``, which knows the
        carry-forward source.)
        """
        new_date = self.cleaned_data["meeting_date"]
        meeting = self.instance
        if meeting.pk is None or new_date == meeting.meeting_date:
            return new_date
        agreed_dates = meeting.reviewed_actions.values_list("agreed_at__meeting_date", flat=True)
        latest_agreed = max(agreed_dates, default=None)
        if latest_agreed is not None and new_date < latest_agreed:
            raise forms.ValidationError(
                f"This meeting reviews actions agreed on {latest_agreed:%d/%m/%Y}, so it "
                "can't be dated before then."
            )
        unreviewed = meeting.agreed_actions.filter(reviewed_in__isnull=True).exists()
        later = (
            LineMeeting.objects.filter(staff=meeting.staff, meeting_date__gt=new_date)
            .exclude(pk=meeting.pk)
            .exists()
        )
        if unreviewed and later:
            raise forms.ValidationError(
                "This meeting has actions still to be reviewed. Dating it before a later "
                "meeting would mean they never come up for review."
            )
        return new_date


class AgreedActionForm(forms.ModelForm):
    class Meta:
        model = MeetingAction
        fields = ("description",)
        labels = {"description": "Action"}
        widgets = {
            "description": forms.Textarea(
                attrs={"rows": 2, "data-max-words": "0", "placeholder": "What / by whom / by when"}
            ),
        }

    def __init__(self, *args, can_edit=False, **kwargs):
        super().__init__(*args, **kwargs)
        # A carried-forward action's wording is settled: it was reviewed against it.
        if not can_edit or self.instance.is_pinned:
            _disable_all(self)


class BaseAgreedActionFormSet(BaseInlineFormSet):
    """Actions agreed at this meeting: add, reword or delete — until carried forward."""

    def __init__(self, *args, can_edit=False, can_add=True, **kwargs):
        """``can_add`` is False on a meeting that is no longer the latest: an action
        added there would never be carried forward, so it would never be reviewed."""
        super().__init__(*args, **kwargs)
        self.can_add = can_add
        self.extra = 3 if can_edit and can_add else 0
        self.can_delete = can_edit
        self.form_kwargs = {**self.form_kwargs, "can_edit": can_edit}

    def add_fields(self, form, index):
        super().add_fields(form, index)
        _restrict_id_to_queryset(self, form)
        # With the field gone, a crafted DELETE for a pinned row is never read.
        if form.instance.is_pinned:
            form.fields.pop("DELETE", None)

    def clean(self):
        """Refuse, visibly, a reword or removal of an action carried forward since
        the page was loaded. Its field is disabled now, so the edit would otherwise
        be dropped in silence while the page said "Meeting saved"."""
        super().clean()
        # A page left open after a later meeting was created still posts its
        # blank rows; ``extra`` only governs what is offered, not what is saved.
        typed_new = [f.data.get(f.add_prefix("description"), "").strip() for f in self.extra_forms]
        typed_new = [t for t in typed_new if t]
        if typed_new and not self.can_add:
            raise forms.ValidationError(
                "A later meeting has been added since this page was opened, so new actions "
                "can't be recorded here — they would never come up for review. Add them to "
                "the latest meeting instead. You typed: " + "; ".join(f"“{t}”" for t in typed_new)
            )
        for form in self.initial_forms:
            if not form.instance.is_pinned:
                continue
            typed = form.data.get(form.add_prefix("description"))
            stored = form.instance.description
            reworded = typed is not None and _normalise(typed) != _normalise(stored)
            if reworded or form.data.get(form.add_prefix("DELETE")):
                when = form.instance.reviewed_in.meeting_date.strftime("%d/%m/%Y")
                message = (
                    f"An action was reviewed at the meeting of {when} while you were editing, "
                    "so it can no longer be changed or removed."
                )
                if reworded:
                    message += f" Your wording was: “{typed.strip()}”"
                raise forms.ValidationError(message)

    def new_descriptions(self):
        """The action texts typed into the blank rows (after ``is_valid()``)."""
        return [f.cleaned_data["description"] for f in self.extra_forms if f.has_changed()]

    def save_existing(self, form, obj, commit=True):
        obj = form.save(commit=False)
        if commit:
            # Compare-and-swap: an action carried forward since the page loaded is
            # settled, and the whole page save rolls back rather than reword it.
            updated = MeetingAction.objects.filter(pk=obj.pk, reviewed_in__isnull=True).update(
                description=obj.description, updated_at=timezone.now()
            )
            if updated != 1:
                raise MeetingChanged
        return obj

    def delete_existing(self, obj, commit=True):
        # Compare-and-swap: if another request carried this action into a later
        # meeting after this page was loaded, the delete is skipped rather than
        # destroying the rating that meeting now holds.
        if commit:
            deleted, _ = MeetingAction.objects.filter(pk=obj.pk, reviewed_in__isnull=True).delete()
            if not deleted:
                # Refuse visibly: the whole page save rolls back (409), never a
                # "Meeting saved" over a delete that did not happen.
                raise MeetingChanged


AgreedActionFormSet = forms.inlineformset_factory(
    LineMeeting,
    MeetingAction,
    fk_name="agreed_at",
    form=AgreedActionForm,
    formset=BaseAgreedActionFormSet,
    extra=3,
    can_delete=True,
    can_delete_extra=False,
)


class CarriedActionForm(forms.ModelForm):
    rag = forms.ChoiceField(
        label="RAG rating",
        choices=[("", "Not rated"), *MeetingAction.Rag.choices],
        required=False,
        widget=forms.RadioSelect,
    )

    class Meta:
        model = MeetingAction
        fields = ("rag", "review_comment")
        labels = {"review_comment": "Comment"}
        widgets = {
            "review_comment": forms.Textarea(attrs={"rows": 2, "data-max-words": "0"}),
        }

    def __init__(self, *args, can_edit=False, **kwargs):
        super().__init__(*args, **kwargs)
        # Tie the comment to its action's text (rendered with id "<prefix>-text").
        self.fields["review_comment"].widget.attrs["aria-describedby"] = f"{self.prefix}-text"
        if not can_edit:
            _disable_all(self)


class BaseCarriedActionFormSet(BaseModelFormSet):
    """Actions carried in from the last meeting: RAG + comment only, no add/delete."""

    def __init__(self, *args, can_edit=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_kwargs = {**self.form_kwargs, "can_edit": can_edit}

    def add_fields(self, form, index):
        super().add_fields(form, index)
        _restrict_id_to_queryset(self, form)

    def save_existing(self, form, obj, commit=True):
        obj = form.save(commit=False)
        if commit:
            obj.save(update_fields=["rag", "review_comment", "updated_at"])
        return obj

    def has_rating(self) -> bool:
        """Whether any row carries a rating or comment (after ``is_valid()``)."""
        return any(
            f.cleaned_data.get("rag") or f.cleaned_data.get("review_comment", "").strip()
            for f in self.forms
        )

    def posted_ids(self):
        """The action ids the submitted page showed, as posted (strings).

        Read from the raw data, not the form instances: a posted id that is no
        longer in the queryset (e.g. carried into another meeting meanwhile)
        yields a pk-less instance, and its typed rating would be dropped silently.
        """
        return {
            str(f.data[f.add_prefix("id")])
            for f in self.forms
            if f.data.get(f.add_prefix("id"))
        }


# edit_only: an inflated TOTAL_FORMS cannot create rows through this formset.
CarriedActionFormSet = forms.modelformset_factory(
    MeetingAction,
    form=CarriedActionForm,
    formset=BaseCarriedActionFormSet,
    extra=0,
    edit_only=True,
    can_delete=False,
)
