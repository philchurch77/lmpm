"""Upload form for the bulk import subsystem.

One form class for all five CSV types — the only difference between types is
which columns are required, which ``parsers.parse_csv`` checks after upload,
not here. Keeping a single form avoids five near-identical classes for a
single ``FileField``.
"""
from __future__ import annotations

from django import forms


class CsvUploadForm(forms.Form):
    csv_file = forms.FileField(label="CSV file")

    # Only meaningful for the goals import, and only for its two review-comment
    # columns — see ImportBatch.clear_blank_fields and services.apply_goals_row.
    # The field is removed entirely for other types rather than merely hidden,
    # so a posted value cannot switch on destructive behaviour where the form
    # never offered it.
    clear_blank_fields = forms.BooleanField(
        required=False,
        initial=False,
        label="Blank review cells clear the stored comment",
        help_text=(
            "Normally a blank cell leaves the existing text alone. Tick this to "
            "make a blank 'teacher_review_comment' or 'coach_review_comment' "
            "erase what is stored. Use it only to remove comments that should "
            "not be there — goal titles, steps and criteria are never cleared."
        ),
    )

    def __init__(self, *args, allow_clear_blanks: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_clear_blanks:
            self.fields.pop("clear_blank_fields")
