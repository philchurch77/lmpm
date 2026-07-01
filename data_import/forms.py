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
