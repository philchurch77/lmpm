# Bulk import — blank CSV templates

Five header-only CSVs to fill in and upload via **Overview → Bulk Import** (`/import/`,
superuser only). Full column reference and rules: [`../import_templates.md`](../import_templates.md).

## Fill in and upload in this order
1. `staff.csv` — the people (always first).
2. `appraisal_summaries.csv` — creates each `Appraisal`.
3. `goals.csv` and `line_meetings.csv` — both need step 1 (goals also needs step 2).
4. `self_review.csv` — needs steps 1 and 2.

## Formatting rules (apply to every file)
- Keep the header row exactly as given; column order doesn't matter, names are case-insensitive.
- **Emails**: any case (stored lower-case).
- **Booleans**: `yes`/`no`, `true`/`false`, or `1`/`0`. Blank = `false`/unset.
- **`academic_year`**: the start-year **integer**, e.g. `2023` for 2023/24. The year must already
  exist in the app — the import never creates academic years.
- **Dates** (`meeting_date`): `YYYY-MM-DD`.
- **Blank cell** = "leave existing value untouched" — it never overwrites data on re-import.
- Re-uploading a corrected file is safe: rows are matched and updated, not duplicated.

## Per-file gotchas
- **staff.csv** — `staff_type` is `TEACHING` or `SUPPORT` (blank = unclassified). `school` must
  match an existing school name exactly, or the whole row is skipped. Set `staff_type` correctly
  *before* uploading `self_review.csv`, because it selects the descriptor form.
- **appraisal_summaries.csv** — `status` is `DRAFT`/`SHARED`/`SIGNED_OFF`;
  `coach_supports_pay_award` is `YES`/`NO`/`NOT_APPLICABLE`.
- **goals.csv** — `goal_type` is `STANDARDS`/`PERSONAL`/`LEADERSHIP` (one row each per teacher/year).
- **self_review.csv** — one row per bullet.
  - `item_code`: teaching = `TS1`–`TS8`, `PART2`, `PART3`; support = `1`–`9`.
  - `bullet_order`: 1-based position within that item (validated against the app's template).
  - `evidence`: shared per item — only filled on the `bullet_order = 1` row for each `item_code`.
  - `score`: `1`, `2`, `3`, or blank.
  - Senior leaders (`staff_type = LEADER`) use a different form and are **not** covered by this file.
- **line_meetings.csv** — a row with all five note columns blank is skipped (no date-only records).
  `created_by_email` is display-only; blank defaults to the uploading admin.

## Every upload is preview-first
Upload → **preview** (each row shown as CREATE / UPDATE / SKIP, with skip reasons) → **confirm**.
Nothing is written until you confirm. Read the skip reasons before confirming.
