# Bulk import CSV templates

Five separate CSV files, imported in this order (later files depend on staff/
appraisal rows created by earlier ones — see "Import order" below). All
uploads are superuser-only, under Overview → Bulk Import, and go through an
upload → preview → confirm flow before anything is written.

General rules for every file:

- Header row required; column names below are matched case-insensitively.
- Emails are matched/stored case-insensitively (lower-cased on save, matching
  `StaffMember`/`Appraisal`/`LineMeeting` conventions elsewhere in the app).
- Boolean columns accept `true`/`false`, `yes`/`no`, `1`/`0` (case-insensitive);
  blank = `false`.
- Blank cells leave the corresponding field blank/default — they do not
  overwrite an existing non-blank value with blank on re-import (update-on-
  conflict only replaces a field when the CSV cell is non-empty).
- Re-running the same file (or a corrected version) is safe: rows are matched
  to existing records and updated, not duplicated.

## 1. `staff.csv`

One row per `StaffMember`. Matched/updated on `email`.

| Column | Required | Notes |
|---|---|---|
| `email` | Yes | Unique key. |
| `line_manager_email` | No | Plain email string — does not need to already exist as a `StaffMember`; resolves once that person is also imported. |
| `performance_manager_email` | No | Same as above — this is the "coach" link. |
| `department` | No | Free text. |
| `job_title` | No | Free text. |
| `staff_type` | No | `TEACHING` or `SUPPORT` (blank = unclassified). |
| `school` | No | Matched by exact `School.name` (case-insensitive). Unmatched school name → row is skipped and reported; the rest of the row's fields are not imported either (whole-row skip, not partial). |

## 2. `appraisal_summaries.csv`

One row per `(teacher, academic_year)`. Matched/updated on that pair.

| Column | Required | Notes |
|---|---|---|
| `teacher_email` | Yes | Must match an existing `StaffMember.email`; unmatched → skip + report. |
| `academic_year` | Yes | The `start_year` integer, e.g. `2023` for 2023/24. Must match an existing `AcademicYear`; unmatched → skip + report. Import never creates `AcademicYear` rows or touches `is_current`. |
| `coach_email` | No | Snapshot value for `Appraisal.coach_email`. If blank, defaults to the teacher's current `performance_manager_email` at import time. |
| `status` | No | `DRAFT` / `SHARED` / `SIGNED_OFF` (default `DRAFT` for new rows; blank leaves an existing row's status untouched). |
| `cpd_requirements` | No | Free text. |
| `summary_teacher_comment` | No | Free text. |
| `summary_coach_comment` | No | Free text. |
| `on_upper_pay_range` | No | Boolean. |
| `self_review_form_completed` | No | Boolean. |
| `engaged_with_professional_growth` | No | Boolean. |
| `coach_supports_pay_award` | No | `YES` / `NO` / `NOT_APPLICABLE` (blank = unselected). |
| `job_description_review_needed` | No | Boolean. |

Importing a summary row for a teacher/year that doesn't have an `Appraisal`
yet **creates** one (and seeds its goals via `seed_goals()`, matching the
normal create-appraisal flow) rather than requiring goals.csv to run first —
but goals.csv and self_review.csv rows still need the appraisal to exist,
which this step provides.

## 3. `goals.csv`

One row per `(teacher, academic_year, goal_type)`. Matched/updated on that
triple — **not** on row order, since historical exports won't reliably agree
with the app's fixed `order` 1/2/3 = Standards/Personal/Leadership mapping.

| Column | Required | Notes |
|---|---|---|
| `teacher_email` | Yes | Must resolve to an `Appraisal` for that teacher+year (via `academic_year` below); unmatched → skip + report. |
| `academic_year` | Yes | Same `start_year` matching as above. |
| `goal_type` | Yes | `STANDARDS` / `PERSONAL` / `LEADERSHIP`. Determines `order` (1/2/3 respectively) — fixed mapping, not read from the CSV. |
| `title` | No | Free text. |
| `steps_to_success` | No | Free text. |
| `success_criteria` | No | Free text. |
| `teacher_review_comment` | No | Free text. |
| `coach_review_comment` | No | Free text. |

## 4. `self_review.csv`

One row per scorable bullet: `(teacher, academic_year, item_code,
bullet_order)`. This is the file that has to line up with the **current**
descriptor template in `appraisals/self_review_templates.py`.

| Column | Required | Notes |
|---|---|---|
| `teacher_email` | Yes | Must resolve to an `Appraisal`; unmatched → skip + report. |
| `academic_year` | Yes | Same matching as above. |
| `item_code` | Yes | Must match a `code` in `TEACHING_ITEMS`/`SUPPORT_ITEMS` for that teacher's staff_type (e.g. `TS1`...`TS8`, `PART2`, `PART3` for teaching; `1`...`9` for support). Unmatched code → skip + report **that row only** (other bullets for the same teacher still import). |
| `bullet_order` | Yes | 1-based position of the bullet within the item group (matches the order bullets are listed in `self_review_templates.py`). Out-of-range for that item → skip + report. |
| `evidence` | No | Shared per item group, not per bullet. Only read from the row where `bullet_order` is `1` for a given `item_code`; ignored on other rows for that same item (a non-blank value on a later row is a validation warning, not an error, surfaced in the preview). |
| `score` | No | `1`, `2`, `3`, or blank (Not Answered). |

The importer creates the `SelfReview` (calling `seed_items()`) for the
appraisal if it doesn't exist yet, using the teacher's current `staff_type` to
pick TEACHING vs SUPPORT — so a `staff.csv` row with the right `staff_type`
should be imported before this file.

## 5. `line_meetings.csv`

One row per `LineMeeting`. **No business-field unique key** — multiple
meetings for the same staff member on the same date are legitimately
possible, so dedupe on re-run relies entirely on the import system's own
row-tracking (`ImportRow`), keyed on `(batch import_type, staff_email,
meeting_date, row_number-within-original-file)`. Re-uploading the exact same
file updates the previously-created rows rather than duplicating them;
inserting a new genuine meeting on the same date just needs a new row in a
fresh upload, which is treated as a new row to create.

| Column | Required | Notes |
|---|---|---|
| `staff_email` | Yes | Must match an existing `StaffMember.email`; unmatched → skip + report. |
| `meeting_date` | Yes | `YYYY-MM-DD`. |
| `created_by_email` | No | Provenance only (display), not an access-control field — see `LineMeeting.created_by_email` docs in `line_management/models.py`. Blank defaults to the importing superuser's email. |
| `actions_from_last_meeting` | No | Free text. |
| `upcoming` | No | Free text. |
| `rotation_update` | No | Free text. |
| `main_matters` | No | Free text. |
| `actions_from_meeting` | No | Free text. |

A row where all five note columns are blank is skipped (matches
`LineMeeting.is_empty` — the app doesn't create date-only records).

## Import order

1. `staff.csv` — always first.
2. `appraisal_summaries.csv` — creates the `Appraisal` rows that 3 and 4 attach to.
3. `goals.csv` and `line_meetings.csv` — independent of each other, both need 1 (and 3 needs 2).
4. `self_review.csv` — needs 1 and 2.

The upload hub shows each step's status but does not hard-block running them
out of order; rows that depend on missing data are skipped and reported
rather than erroring the whole batch.

## After importing: give staff a login (required)

**The import creates `StaffMember` rows, not login accounts.** Identity in this
app is by email with no FK between `StaffMember` and Django `User` (see
`core/identity.py`), so imported staff cannot sign in until each email also has
a matching `User` **and** a `SchoolProfile` (the SSO access gate — see
`core/allauth_adapters.py`). Accounts are never auto-created on first login
(`SOCIALACCOUNT_AUTO_SIGNUP = False`); everyone must be pre-provisioned.

Run this **once after each import**, against the **same database the import went
into** (e.g. the Azure Postgres DB, not local SQLite):

```bash
.venv/Scripts/python.exe manage.py provision_users --dry-run   # preview
.venv/Scripts/python.exe manage.py provision_users             # create User + SchoolProfile
```

For every `StaffMember` it creates a `User` (username/email = the staff email,
unusable local password since auth is SSO-only) and a `SchoolProfile` from
`StaffMember.school`. It is idempotent (re-running provisions nobody new),
skips-and-reports anyone with no `school` (the `SchoolProfile.school` FK is
mandatory), and never touches a superuser. No per-person permission setup is
needed: once the `User` + `SchoolProfile` exist, the email relationships already
imported (`line_manager_email` / `performance_manager_email`) drive all
view/edit rights automatically.

Note the person's email must also exist as a real mailbox in the Microsoft
(Entra) tenant — that is who actually authenticates them; `provision_users` only
creates the app-side gate.
