"""CSV parsing for the bulk import subsystem.

Parsing only checks file-level concerns (encoding, size, row count, required
headers) and raises ``ImportFileError`` for those — anything wrong with an
individual row's *content* is a row-level concern, reported per row by the
validators in ``data_import.services`` instead of failing the whole upload.
"""
from __future__ import annotations

import csv
import io

from .models import ImportType

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5MB
MAX_ROWS = 5000

REQUIRED_COLUMNS = {
    ImportType.STAFF: ["email"],
    ImportType.APPRAISAL_SUMMARY: ["teacher_email", "academic_year"],
    ImportType.GOALS: ["teacher_email", "academic_year", "goal_type"],
    ImportType.SELF_REVIEW: ["teacher_email", "academic_year", "item_code", "bullet_order"],
    ImportType.LINE_MEETINGS: ["staff_email", "meeting_date"],
}


class ImportFileError(Exception):
    """A file-level problem (too large, bad encoding, missing columns)."""


def parse_csv(uploaded_file, import_type: str) -> list[tuple[int, dict]]:
    """Parse an uploaded CSV into ``(row_number, normalised_row_dict)`` pairs.

    Row numbers are 1-based and count data rows only (the header is not
    counted). Column names are matched case-insensitively; every row dict uses
    the lower-cased column name as its key, with whitespace stripped from
    every cell.
    """
    if uploaded_file.size > MAX_FILE_BYTES:
        raise ImportFileError(
            f"File is too large (max {MAX_FILE_BYTES // (1024 * 1024)}MB)."
        )

    raw = uploaded_file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError("File must be UTF-8 encoded CSV.") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportFileError("File has no header row.")

    header_lookup = {(name or "").strip().lower() for name in reader.fieldnames}
    missing = [col for col in REQUIRED_COLUMNS[import_type] if col not in header_lookup]
    if missing:
        raise ImportFileError(f"Missing required column(s): {', '.join(missing)}.")

    rows: list[tuple[int, dict]] = []
    for row_number, raw_row in enumerate(reader, start=1):
        if row_number > MAX_ROWS:
            raise ImportFileError(f"File has more than {MAX_ROWS} data rows.")
        normalised = {
            (key or "").strip().lower(): (value or "").strip()
            for key, value in raw_row.items()
            if key is not None
        }
        rows.append((row_number, normalised))

    if not rows:
        raise ImportFileError("File has no data rows.")

    return rows
