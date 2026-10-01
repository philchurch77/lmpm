from django import forms
from django.contrib import admin

from core.admin_mixins import SuperuserOnlyDeleteMixin

from .models import LineMeeting, MeetingAction
from .services import meeting_version, parse_version, touch_meetings


class MeetingActionAdminForm(forms.ModelForm):
    """Validates the "rating needs a review meeting" rule before the database does.

    ``reviewed_in`` is read-only in the admin, so Django's constraint validation
    skips the check constraint that references it and the save would otherwise
    end in an IntegrityError (a 500 that discards the whole admin submission).
    """

    class Meta:
        model = MeetingAction
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if self.instance.reviewed_in_id is None and (
            cleaned.get("rag") or (cleaned.get("review_comment") or "").strip()
        ):
            raise forms.ValidationError(
                "An action can only be rated once it has been carried into a later meeting."
            )
        return cleaned


def _lock_meetings(*pks):
    """Lock these meetings' rows until the admin's transaction ends.

    The admin change view runs in one transaction, so a version checked in
    ``clean()`` under this lock cannot be overtaken by a page save before the
    admin writes (check-then-write would otherwise leave a gap). Meeting rows
    are locked in pk order, before any action row — the same order as every
    other writer, so no deadlock. (A no-op on SQLite, which serialises writes.)
    """
    ids = sorted({pk for pk in pks if pk is not None})
    list(LineMeeting.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


_STALE_ADMIN_FORM = (
    "was changed after you opened this page (perhaps on the meeting page), so nothing "
    "was saved. Copy anything you need from below, then reload the page."
)


class MeetingActionChangeForm(MeetingActionAdminForm):
    """The standalone action admin form, refused when stale.

    Its stamp is the action's own ``updated_at``, which every page write to the
    action (reword, rating, comment) advances.
    """

    action_version = forms.CharField(widget=forms.HiddenInput, required=False)
    _stale_subject = "This action"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["action_version"].initial = self.instance.updated_at.isoformat()

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            _lock_meetings(self.instance.agreed_at_id, self.instance.reviewed_in_id)
            posted = parse_version(self.data.get(self.add_prefix("action_version")))
            current = MeetingAction.objects.filter(pk=self.instance.pk).values_list(
                "updated_at", flat=True
            ).first()
            if posted is None or posted != current:
                raise forms.ValidationError(f"{self._stale_subject} {_STALE_ADMIN_FORM}")
        return cleaned


class MeetingActionInlineFormSet(forms.BaseInlineFormSet):
    """A reviewed action is settled here too: no delete box, wording read-only.

    ``has_delete_permission`` on an inline receives the parent meeting, not the
    action, so it cannot express this per row — the formset does.
    """

    def add_fields(self, form, index):
        super().add_fields(form, index)
        if form.instance.pk and form.instance.is_pinned:
            # Disabled rather than removed: the admin template reads every row's
            # DELETE field. A disabled field ignores whatever is posted for it.
            if "DELETE" in form.fields:
                form.fields["DELETE"].disabled = True
            form.fields["description"].disabled = True


class MeetingActionInline(SuperuserOnlyDeleteMixin, admin.TabularInline):
    """Actions agreed at this meeting.

    The rating and comment are shown, not editable: they belong to the meeting
    that *reviews* the action, and are written on that meeting's page under that
    meeting's version. Editable here, a stale form for this meeting would
    silently overwrite them (edit them on the action's own admin page instead).
    """

    model = MeetingAction
    form = MeetingActionAdminForm
    formset = MeetingActionInlineFormSet
    fk_name = "agreed_at"
    extra = 0
    fields = ("description", "reviewed_in", "rag", "review_comment")
    readonly_fields = ("reviewed_in", "rag", "review_comment")


class LineMeetingAdminForm(forms.ModelForm):
    """Refuses a stale admin form, so it cannot overwrite a newer page save.

    The same version stamp the meeting page carries; checked against the stored
    row in ``clean()``. On refusal the admin re-renders with the typed values
    still in the form, to copy before reloading.
    """

    meeting_version = forms.CharField(widget=forms.HiddenInput, required=False)
    _stale_subject = "This meeting"

    class Meta:
        model = LineMeeting
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["meeting_version"].initial = meeting_version(self.instance)

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            # This meeting and every meeting reviewing its actions (save_model
            # touches those), locked together in pk order before any write.
            _lock_meetings(
                self.instance.pk,
                *self.instance.agreed_actions.values_list("reviewed_in_id", flat=True),
            )
            posted = parse_version(self.data.get(self.add_prefix("meeting_version")))
            current = LineMeeting.objects.filter(pk=self.instance.pk).values_list(
                "updated_at", flat=True
            ).first()
            if posted is None or posted != current:
                raise forms.ValidationError(f"{self._stale_subject} {_STALE_ADMIN_FORM}")
        return cleaned


@admin.register(LineMeeting)
class LineMeetingAdmin(SuperuserOnlyDeleteMixin, admin.ModelAdmin):
    form = LineMeetingAdminForm
    list_display = ("staff", "meeting_date", "created_by_email")
    list_filter = ("meeting_date",)
    search_fields = ("staff__email", "created_by_email")
    autocomplete_fields = ("staff",)
    date_hierarchy = "meeting_date"
    inlines = (MeetingActionInline,)

    def get_readonly_fields(self, request, obj=None):
        # An action spans two meetings (agreed at / reviewed in), so moving a saved
        # meeting to another person would put one person's actions, ratings and
        # comments on another person's record.
        if obj is not None:
            return ("staff",)
        return ()

    def save_model(self, request, obj, form, change):
        # Inline action edits change what the reviewing meetings' pages show, so
        # their versions move too — an open page then refuses to save over them.
        # Touched BEFORE anything is written: every writer locks meeting rows
        # before action rows (see services.start_meeting), or two of them can
        # deadlock on Postgres. This meeting's own version moves via auto_now.
        if obj.pk:
            touch_meetings(
                *obj.agreed_actions.exclude(reviewed_in=None).values_list("reviewed_in_id", flat=True)
            )
        super().save_model(request, obj, form, change)


@admin.register(MeetingAction)
class MeetingActionAdmin(SuperuserOnlyDeleteMixin, admin.ModelAdmin):
    form = MeetingActionChangeForm
    # No description text in the changelist: it is staff performance commentary.
    list_display = ("__str__", "agreed_at", "reviewed_in", "rag")
    list_filter = ("rag",)
    list_select_related = ("agreed_at__staff", "reviewed_in__staff")
    readonly_fields = ("agreed_at", "reviewed_in")

    def has_add_permission(self, request):
        # Actions are created from a meeting page (or the inline), never loose.
        return False

    # Both meetings' open pages show an action; neither may save over an admin
    # edit to it. Touched BEFORE the write, so meeting rows lock before action
    # rows as in every other writer (Postgres deadlock otherwise).
    def save_model(self, request, obj, form, change):
        touch_meetings(obj.agreed_at_id, obj.reviewed_in_id)
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        touch_meetings(obj.agreed_at_id, obj.reviewed_in_id)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # The bulk "Delete selected" path bypasses delete_model.
        touch_meetings(
            *[m for pair in queryset.values_list("agreed_at_id", "reviewed_in_id") for m in pair]
        )
        super().delete_queryset(request, queryset)

    def get_readonly_fields(self, request, obj=None):
        # The review was written against this wording.
        if obj is not None and obj.is_pinned:
            return (*self.readonly_fields, "description")
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        # A reviewed action carries a rating and comment written at a later
        # meeting; deleting it here would lose that without touching the meeting.
        if obj is not None and obj.is_pinned:
            return False
        return super().has_delete_permission(request, obj)
