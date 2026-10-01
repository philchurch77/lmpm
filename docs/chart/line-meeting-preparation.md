# Chart: line-meeting-preparation

Client request (Andy, September 2026): restore RAG-rating of actions from the previous meeting, and
let the member of staff fill in the meeting in advance so both parties work on the same record.
All round-1 recommendations were accepted by the developer and the client on 2026-09-30.

## Destination
A member of staff (the report) can prepare their next line meeting — RAG-rate and comment on the
actions carried from last time, propose new actions, fill in Upcoming and Main matters — and the line
manager completes the record at or after the meeting and marks it Held, after which it is read-only
to the report.

## Decisions so far
- **Scope**: the report prepares, the line manager completes. Not live co-editing — both parties edit
  the same record at different times.
- **Terms**: "report" in code and `CONTEXT.md`, "member of staff" on screen. RAG: Red = not done /
  blocked, Amber = in progress, Green = done.
- **Actions are rows**: a `MeetingAction` is agreed at meeting N and reviewed (RAG + comment) at
  meeting N+1 — one row spanning two meetings, the same shape as `Goal` spanning two years.
- **Carry-forward is pinned, not derived**: on creating a meeting, every unreviewed action agreed at
  the report's latest Held meeting gets `reviewed_in` set to the new meeting, in one transaction.
  "Previous meeting by date" was rejected — a back-dated or imported meeting would silently move
  actions and their ratings.
- **`on_delete=PROTECT`** on both `MeetingAction` FKs: deleting meeting N must never destroy the
  ratings written at N+1.
- **Legacy free text is never split or dropped**: `actions_from_last_meeting` / `actions_from_meeting`
  are removed from the form only while blank (the UPR pattern); stored text is shown read-only.
- **Either party may start the next meeting**; at most one meeting per report may be Being prepared
  (partial `UniqueConstraint`), which also replaces `_existing_duplicate` as the double-create guard.
- **Two states, `PREPARING` / `HELD`**: the line manager marks Held; the report is read-only after
  that; overview and team count Held only. Existing rows migrate to Held (AddField one-off default
  `HELD`, model default `PREPARING`); the importer creates Held.
- **Report may edit while Being prepared**: meeting date, Upcoming, Main matters, RAG + comment on
  carried-forward actions, add/delete *new* actions. The line manager edits everything, owns the
  Rotation update and has the final say.
- **Stale saves refused, text handed back**: one version token (`LineMeeting.updated_at`); every page
  save touches the meeting row (including action-only saves); compare-and-swap; 409 via
  `core.recovery`.
- **`LineMeeting.NOTE_FIELDS` stays the original five fields permanently** — the import
  `source_row_hash` depends on it. Pinned by a test in leg 1.
- **Access stays a live line-manager lookup**; the report's access follows `meeting.staff == viewer`,
  never `created_by_email`.
- Cross-model workflows (`start_meeting`, the version-checked save) go in a small
  `line_management/services.py`, each in `transaction.atomic()`.
- *(Leg 1)* Interim carry-forward source until leg 3: the report's latest meeting by
  (`meeting_date`, `created_at`, pk). A meeting dated before it pins nothing and may hold notes only.
- *(Leg 1)* **Every action stays in the review cycle**: new actions only on the latest meeting; a
  saved meeting cannot be re-dated before the actions it reviews, or behind a later meeting while it
  holds unreviewed actions. This settled the fog item "actions added after the successor exists".
- *(Leg 1)* **Stale pages refuse visibly**: formset ids are restricted to their queryset; a stale
  create rebuilds the carried list and echoes typed ratings. Leg 2's version token generalises this.
- *(Leg 1)* Admin: a saved meeting's `staff` is read-only; a pinned action cannot be deleted or
  reworded anywhere in the admin.
- *(Leg 2)* **Every writer advances the version** of each meeting whose page it changes: page save,
  the pin (source meeting), admin action edits, the importer (`touch_meetings`, strictly forward).
  Meeting rows are locked before action rows everywhere (Postgres deadlock otherwise).
- *(Leg 2)* **Each column has one owning page**: a meeting's page owns its notes and the wording of
  the actions it agreed; the *reviewing* meeting's page owns their rating and comment. The admin
  meeting inline therefore shows ratings read-only. Leg 4's per-field split for the report must keep
  this ownership.
- *(Leg 2)* A double-click, or a stale page with nothing changed, folds into "Meeting saved"; any
  difference is a 409 hand-back. The pre-check is load-bearing (stops a stale page re-rendering with
  a fresh stamp). Admin forms are stale-checked too, under row locks.
- *(Leg 3)* **`state` has `db_default` HELD** (Python default PREPARING) instead of the planned
  AddField-then-AlterField: existing rows still become Held, and the previous release — still serving
  while a deploy migrates — can keep inserting (its meetings are Held) instead of failing NOT NULL.
- *(Leg 3)* **Button hierarchy**: "Save" is the primary button and first in the DOM; "Save and mark as
  held" is secondary. Holding is once per meeting and irreversible, so it is not the strongest click.
  "Being prepared" is blue (an in-flight state, like `in_progress`), Held green; amber stays for
  problems.
- *(Leg 3)* **My Team shows the meeting being prepared** ("Continue meeting" + badge), and the report
  sees "Your line manager is still preparing this meeting" on one. `meeting_new` redirects to it.
- *(Leg 3)* **Every create refusal hands text back** — including an `IntegrityError` with no preparing
  meeting to point at (generic 409), never a 500. A repeated create folds without a second banner.
- *(Leg 3)* `purge_empty_line_meetings` deletes **Held** empties only.
- *(Leg 4)* **One `edit_scope(role, meeting)`** (ALL / PREPARE / NONE) threaded through every form
  bind; `REPORT_FIELDS` is an allowlist (date, Upcoming, Main matters). Q1 (a): while being prepared
  the report may reword or delete any action agreed at the meeting, the manager's included — no
  per-row ownership, which would have gated on provenance.
- *(Leg 4)* **The report on a Held meeting gets a 409 hand-back, not a 403**: they keep their role,
  so it is a conflict. Lost role is still 403. `save_meeting_page(as_report=True)` also requires
  `state=PREPARING` in the WHERE, so the rule does not rest on every hold advancing the version.
- *(Leg 4)* The report starts through pk-less `prepare/` URLs sharing the manager's create workflow
  (`_new` / `_create`, `_Starter`). `why_cannot_prepare` words each refusal (no record / no line
  manager / self as line manager) so an administrator is sent to the actual fault.
- *(Leg 4)* Accepted: a crafted POST to `prepare/create/` by someone with no line manager gets a 409
  echoing only their own text (nothing saved), the same path as losing eligibility mid-typing.
- *(DPIA, 2026-10-01)* Leg 4 widens who writes these records (the report edits their own meeting
  before it is Held). Put to the head of school, who confirmed it is acceptable. Leg 3 added no new
  data category or audience.
- *(Leg 3)* Importer: an update never writes notes (only `created_by_email`, CAS on the notes); the
  "would strand actions" rule lives in `line_management.services.would_strand_actions`, counting
  actions agreed at a meeting being prepared too.

## Legs
| # | Leg | Blocked by | Delivers | Crew | Status |
|---|---|---|---|---|---|
| 1 | Line manager records actions and RAG-rates last meeting's actions | — | `MeetingAction` + migration; `start_meeting` pinning carried actions; agreed-actions and carried-actions (RAG + comment) formsets on create/detail; legacy action text read-only; extended `is_empty` + purge; admin inline (superuser-only delete, `GATED_MODELS`); RAG pill widget; formset error includes; `NOTE_FIELDS` pin test | Quartermaster, Carpenter, Bosun, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | **done 2026-09-30** (committed 470c790; not yet deployed) |
| 2 | A stale save is refused and the typed text handed back | 1 | hidden `updated_at` token; compare-and-swap save service; action-only saves touch the meeting; 409 via `render_save_blocked`; two-session tests | Quartermaster, Carpenter, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | **done 2026-10-01** (committed 470c790; not yet deployed) |
| 3 | Line manager starts a meeting to prepare and marks it Held; dashboards count Held only | 1 | `state` migration (existing → Held); partial unique constraint; "Save and mark as held" button; repeat guard kept; carry-forward from latest Held; overview/team counts filter Held; importer creates Held; import UPDATE path becomes compare-and-swap (skip + report if notes changed since the prior import); state badge | Quartermaster, Carpenter, Bosun, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | **done 2026-10-01** (not yet committed or deployed) |
| 4 | The member of staff prepares their next line meeting | 2, 3 | report-start view/URL + chokepoint (non-blank `line_manager_email`); `can_prepare_meeting`; per-field and per-row gating in forms; read-only once Held; "Prepare next meeting" on My Line Meetings; role-matrix + IDOR tests (crafted POST cannot touch Rotation; stranger 403 on every new endpoint; successor manager inherits mid-preparation; **the report posting `hold` on a meeting they can edit saves but stays PREPARING** — leg 3's hold tests cannot isolate `can_hold_meeting`, since today `can_edit_meeting` already 403s the report) | Quartermaster, Carpenter, Bosun, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | **done 2026-10-01** (not yet committed or deployed) |

## Leg 3 plan (approved at council 2026-10-01 — built 2026-10-01; kept for the record)

**Two changes from the leg table above, agreed at council:**
- Holding is **not** a separate "Mark as held" action: it is a **"Save and mark as held"** button on
  the save the manager already makes, so `state=HELD` rides in `save_meeting_page`'s one conditional
  UPDATE and inherits leg 2's version check, lock order and hand-back for free.
- `find_repeat_submission` (leg 1's double-click guard) is **kept**. The partial unique constraint
  is the backstop for two racing creates; it cannot replace the guard because a meeting can now be
  created already Held.

**Rules**
- Create page and a Being-prepared meeting's page: two buttons — "Save" (first in the DOM, so Enter
  never holds) keeps it PREPARING; "Save and mark as held" (`name="hold"`) holds it. A Held meeting
  shows "Save meeting" only and stays editable by manager/super as today.
- **No un-hold anywhere** (a successor may already have pinned its actions). Admin: `state` read-only
  on change, initial HELD on add.
- `can_hold_meeting(role)` in `permissions.py` = manager or super — separate from `can_edit_meeting`
  so leg 4 can let the report edit but never hold. `hold` is honoured only if `can_hold_meeting` and
  the meeting is PREPARING.
- A blank page cannot be held: one shared helper (`page_would_be_blank`: no notes, no remaining or new
  agreed action, no carried rating/comment) used by create and hold.
- A back-dated create (before the latest Held meeting) may only be saved Held; "Save" with a back
  date re-renders with a date error, text kept.
- One PREPARING per report: `meeting_new` redirects to it (info message); `meeting_create` runs the
  repeat-fold first, then 409 hand-back (back link → the preparing meeting) if one exists; an
  `IntegrityError` from the constraint → re-check the fold, else hand back (re-raise if no preparing
  meeting exists). `staff_meetings` offers "Continue the meeting being prepared" instead of "New".
- `_is_exact_repeat`: a posted `hold` counts as a repeat only if the stored state is already HELD
  (else a stale hold of an unchanged page folds and the meeting is never held). `_stale_save` adds
  "It was not marked as held." when `hold` was posted.
- Holding does nothing to actions. Success message: "Meeting saved and marked as held."

**Services**
- Rename today's `carry_forward_source` body to `latest_meeting(member)` (all states) — used by
  `_bind`'s `is_latest`/`can_add`. **Critical:** `_bind` currently does
  `carry_forward_source(meeting.staff).pk`, which becomes an AttributeError for a report whose only
  meeting is PREPARING once the source is Held-only.
- New `carry_forward_source(member)` = latest **HELD** by (date, created_at, pk).
- `preparing_meeting(member)`. `save_meeting_page(..., hold=False)` adds `state=HELD` to the CAS update.
- `clean_meeting_date` stays state-blind.

**Migration `0003_linemeeting_state` — hand-edit the generated file before applying:**
```python
AddField("linemeeting", "state", CharField(choices=..., default="HELD", max_length=9), preserve_default=False)
AlterField("linemeeting", "state", CharField(choices=..., default="PREPARING", max_length=9))
AddConstraint(UniqueConstraint(fields=["staff"], condition=Q(state="PREPARING"), name="linemeeting_one_preparing_per_staff"))
AddConstraint(CheckConstraint(condition=Q(state__in=["PREPARING", "HELD"]), name="linemeeting_state_valid"))
```
`makemigrations` will not prompt (the model has a default) and would put PREPARING on every existing
row. Verify with `sqlmigrate line_management 0003` (Postgres: `ADD COLUMN … DEFAULT 'HELD'` then
`DROP DEFAULT`) and `makemigrations --check --dry-run`. Model: `State(TextChoices)` PREPARING
"Being prepared" / HELD "Held"; `state` default PREPARING; both constraints in `Meta`. `NOTE_FIELDS`
untouched.

**Dashboards:** `overview.views.line_management_overview` and `team.views.my_team` use
`Count("line_meetings", filter=Q(line_meetings__state=HELD))` and the same filter on `Max(meeting_date)`;
fix `classify_line`'s "no status field" docstring; `my_team.html` column label "Held meetings".

**Importer** (`data_import/services.py`; the clean seam to cut at if the passage runs long — but
`state=HELD` on creates must ship regardless):
- Creates pass `state=HELD` explicitly.
- A hash match already proves the incoming notes equal what the prior import wrote, so the CAS is
  "stored notes == incoming notes": `validate_line_meeting_row` → SKIP "Edited in the app since it was
  imported — left unchanged" when they differ; `apply_line_meeting_row` updates with
  `filter(pk=…, **notes)`, writing only `created_by_email` when it differs, `touch_meetings` only if a
  row changed, and a named exception (recorded as SKIP) when 0 rows match. Notes are never written on
  the update path.
- Skip a CREATE that would strand unreviewed actions (agreed at a meeting dated on/before the row).
- Exact compare: a browser re-save differing only in CRLF reports a harmless skip (Low, safe).

**UI (Bosun):** state badge in `meeting_detail.html` header and a status column in `my_meetings.html`
(both tables) and `staff_meetings.html`; the button pair above. Article 7 comment syntax.

**Admin:** `state` in `list_display`/`list_filter`; read-only on change (with `staff`); initial HELD on
add, editable only on add so a constraint clash is a form error, not a 500.

**Test fixtures:** `make_meeting` defaults `state=HELD`; likewise `team/tests.py:148-149` and
`overview/tests.py:173` — else IntegrityErrors and zero counts.

**Tests (Gunner):** `test_existing_meetings_migrate_to_held_not_preparing` (migration executor 0002→0003),
`test_second_preparing_meeting_for_same_report_is_refused_by_database`,
`test_carry_forward_ignores_meeting_being_prepared`,
`test_can_add_actions_on_first_meeting_being_prepared_with_no_held_meeting`,
`test_new_meeting_redirects_to_meeting_being_prepared`,
`test_create_while_one_is_being_prepared_hands_text_back_409`,
`test_double_click_create_being_prepared_folds_not_409`, `test_double_click_save_and_hold_create_folds`,
`test_back_dated_create_cannot_be_left_being_prepared`, `test_save_and_hold_marks_held_and_keeps_notes`,
`test_report_cannot_mark_meeting_held`, `test_stranger_cannot_mark_meeting_held`,
`test_stale_page_hold_is_refused_and_text_handed_back`,
`test_stale_hold_of_unchanged_page_is_not_folded_while_still_preparing`,
`test_blank_meeting_cannot_be_marked_held`,
`test_held_meeting_cannot_be_returned_to_preparing_by_posting_state` (crafted POST and admin),
`test_held_meeting_still_editable_by_manager`, `test_overview_counts_only_held_meetings`,
`test_team_counts_only_held_meetings`, `test_import_creates_held_meeting`,
`test_reimport_skips_meeting_edited_in_app_and_keeps_edit`, `test_reimport_of_untouched_meeting_still_updates`,
`test_import_skip_reason_shown_on_preview`, `test_import_refuses_meeting_that_would_strand_unreviewed_actions`,
`test_reimport_edit_between_preview_and_confirm_is_skipped`.

**Docs drift to fix when the leg makes port:** CLAUDE.md line-management ("no status/lock"),
overview ("no status field"), and import (hash-match overwrite) sections; note that
`purge_empty_line_meetings` will also delete an empty Being-prepared meeting.

**Crew:** Carpenter, Bosun, Master-at-Arms (hold role gate, importer reads, crafted-POST probe),
Purser (reads `0003` and the importer update path), Gunner, Lookout. Gauntlet applies.

## Not yet charted
- Leg 3's new tests have not been mutation-checked (shown to fail with their guard removed). Leg 4's
  key guards were (allowlist, report hold gate, `as_report` filter): each test failed with its guard
  removed.
- **Who changed what**: either party can now clear text the other wrote on a meeting being prepared
  (the Purser's point); only "Started by" is kept. A last-edited-by on notes and ratings may be
  wanted — not a defect, since the version check stops blind overwrites.
- A report whose line manager is cleared while a meeting is being prepared can keep editing it, but
  only a superuser can then hold it — joins the "left Being prepared, never held" item below.
- Hand-back pages show RAG ratings as stored codes ("GREEN"), not labels.
- The next prepared meeting defaults to today, which a future-dated Held meeting refuses (clear
  error, text kept).
- Colour contrast of `--rag-green` / `--rag-red` with white 13px text (~3.4:1 / ~4.4:1) — shared
  app-wide tokens, so a separate tidy, not this chart.
- Read-only view for the report renders disabled inputs; a plain-text read view (RAG badges) would
  read better. Leg 4 kept it (display polish over a path that loses nothing); only the Rotation
  update is shown as text to the report while preparing.
- Whether an unfinished (Red/Amber) action rolls on automatically to the meeting after next, or is
  re-agreed by hand. Decide after leg 1 has run a few cycles.
- Showing who last edited a section or rating (`created_by_email` on actions may be enough).
- A meeting left Being prepared and never held blocks the next start (the constraint). Expire,
  delete, or leave to the admin?
- A RAG summary on My Team or overview.
- Pagination of My Line Meetings once weekly meetings accumulate.

## Out of scope
- Live co-editing (Channels + Redis) — a new platform for one form; rejected in round 1.
- Notifications ("your meeting is ready to prepare").
- Full revision history — the stale-save check prevents loss; history is a separate feature.
- Splitting legacy action text into rows — it would guess boundaries and rewrite stored text.
- CSV import of actions or ratings — historic imports stay free text.
- Report edits after a meeting is Held.
