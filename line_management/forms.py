"""Form for the line-meeting record.

A single ModelForm over the meeting date and the five note sections. Only the
current line manager may edit: when ``can_edit`` is False every field is set
``disabled`` so Django ignores any submitted value (the real security boundary,
not template hiding). There is no teacher/coach field split, so this is simpler
than the appraisals ``RoleGatedForm``.
"""
from __future__ import annotations

from django import forms

from .models import LineMeeting


class LineMeetingForm(forms.ModelForm):
    class Meta:
        model = LineMeeting
        fields = (
            "meeting_date",
            "actions_from_last_meeting",
            "upcoming",
            "rotation_update",
            "main_matters",
            "actions_from_meeting",
        )
        # Line-management meetings can be very in-depth, so these notes are not
        # word-capped. data-max-words="0" opts each textarea out of the shared
        # client-side word limit guard (core/static/core/word_limit.js).
        widgets = {
            "meeting_date": forms.DateInput(attrs={"type": "date"}),
            "actions_from_last_meeting": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
            "upcoming": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
            "rotation_update": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
            "main_matters": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
            "actions_from_meeting": forms.Textarea(attrs={"rows": 4, "data-max-words": "0"}),
        }

    def __init__(self, *args, can_edit=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_edit:
            for field in self.fields.values():
                field.disabled = True
