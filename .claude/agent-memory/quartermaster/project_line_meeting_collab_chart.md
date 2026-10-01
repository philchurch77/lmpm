---
name: project-line-meeting-collab-chart
description: Line-management RAG actions + report pre-fill chart (drawn 2026-09-30) — settled decisions, model shape, four legs, leg-1/2/3 plan rules
metadata:
  type: project
---

Chart drawn 2026-09-30 (round 1 all accepted), docs/chart/line-meeting-preparation.md. Destination: report prepares the next line meeting (RAG + comment on carried actions, proposes actions, Upcoming, Main matters); manager completes and marks Held.

Settled (do not re-open):
- `MeetingAction`: FK `agreed_at` -> LineMeeting (related_name agreed_actions, PROTECT); FK `reviewed_in` -> LineMeeting nullable (related_name reviewed_actions, PROTECT) PINNED at creation of the next meeting — not derived from dates. RAG on the action row (one row spans two meetings, like Goal). `rag` choices RED/AMBER/GREEN, "" = not rated. CheckConstraint reviewed_in != agreed_at.
- `LineMeeting.state` PREPARING/HELD (leg 3); AddField one-off default HELD, model default PREPARING; importer creates HELD. Partial UniqueConstraint one PREPARING per staff.
- `NOTE_FIELDS` stays the five legacy fields forever (import `source_row_hash`). `is_empty` counts actions; purge respects it.
- Legacy `actions_from_last_meeting` / `actions_from_meeting`: never editable again; shown read-only when stored.
- Version check (leg 2): token `LineMeeting.updated_at`; CAS; 409 via core/recovery.
- Legs: 1 actions+RAG (manager), 2 stale-save refusal, 3 Held state + counts + import CAS, 4 report prepares.

Leg-1 plan decisions (2026-09-30):
- Interim carry-forward source = report's latest meeting by (meeting_date, created_at, pk). New meeting dated earlier than source pins nothing; if typed ratings on carried rows with a back-dated date -> refuse with date error (text kept). Pin ALL unreviewed source actions via CAS `.update()`; count mismatch -> rollback + re-render.
- Column ownership: agreed formset saves description only; carried formset saves rag, review_comment only. Reason: instances loaded before the pin carry reviewed_in=None.
- Extra CheckConstraint: rating/comment only when reviewed_in set. Django 6 CheckConstraint uses `condition=`.
- Pinned action: description read-only, not deletable.
- Double-submit guard `find_repeat_submission`: fold only when notes + agreed descriptions + carried ratings all equal.
- Admin: MeetingAction registered + inline, SuperuserOnlyDeleteMixin; no description in list_display.

Leg-2 plan decisions (2026-09-30):
- Token = hidden input `meeting_version` (UTC isoformat); missing/garbled -> stale (409), never skip.
- meeting_save order: get_meeting_or_403 -> can_edit 403 -> version pre-check 409 (or fold via _is_exact_repeat) -> validate -> atomic CAS `.update()` writing token + editable fields in one statement.
- Lock order everywhere: meeting rows before action rows (Postgres deadlock). Pin touches SOURCE meeting first.
- Admin forms stale-checked under select_for_update; importer UPDATE touches meeting.

Leg-3 plan decisions (2026-10-01):
- Hold is NOT a separate endpoint: "Save and mark as held" second submit button (`hold=1`) on create and on a PREPARING meeting's page; state=HELD written in the same CAS UPDATE. First button in DOM = plain Save (Enter key must not trigger irreversible hold). No un-hold anywhere (admin `state` read-only on change).
- `can_hold_meeting(role)` separate from `can_edit_meeting` (leg 4 widens edit to report, never hold).
- find_repeat_submission STAYS (chart said constraint replaces it — wrong once a create can be HELD; the constraint only guards PREPARING creates and is the race backstop). No new create of any state while a PREPARING exists: meeting_new redirects to it, meeting_create 409 hand-back (after the repeat-fold check).
- Split services: `latest_meeting(member)` = old unfiltered body, used for can_add; `carry_forward_source` = latest HELD. _bind's `carry_forward_source(...).pk` would AttributeError on None otherwise.
- Back-dated create (before latest Held) may only be saved Held (date error otherwise).
- Hold refused when the page would be blank by the create rule (shared helper).
- Importer: hash includes the notes, so a hash match means incoming == what the prior import wrote; CAS = stored notes equal incoming, else SKIP "edited in the app since import" (in validate for preview, and conditional .update in apply). No raw_json lookup needed. Importer CREATE skips if the staff member has unreviewed actions agreed on/before the row date (would strand them).
- Test fixtures (make_meeting, team/overview tests) must pass state=HELD — default PREPARING + constraint breaks multi-meeting fixtures.

Leg-4 plan decisions (2026-10-01, pending developer answer on Q1 action scope):
- No migration if Q1(a) accepted. Per-row ownership of actions by `created_by_email` rejected: provenance never gates access in this app.
- permissions.py: `can_prepare_meeting(role, meeting)` = REPORT and not held; `edit_scope(role, meeting)` -> ALL/PREPARE/NONE. Hold stays `can_hold_meeting`.
- forms: only LineMeetingForm differs by role — report gets an ALLOWLIST (`REPORT_FIELDS` = meeting_date, upcoming, main_matters); everything else disabled, so a new field is manager-only by default. Agreed/carried formsets unchanged (report = manager on a PREPARING meeting).
- Report start = separate pk-less URLs `prepare/` + `prepare/create/` with chokepoint on viewer's own StaffMember (non-blank line_manager_email, not self). Create body extracted into one shared helper used by meeting_create and prepare_create; role passed in; hold ignored for report; back-dated report create = date error (no hold option).
- meeting_save gate order: 403 no role -> report on Held = 409 hand-back (conflict, keeps role; like appraisals lock) -> hold computed -> version 409/fold -> validate -> CAS (add state=PREPARING to the filter for report saves).
- `_is_exact_repeat` and `_bind` must take the scope (browsers don't post disabled inputs; full-scope binding makes every report double-click a 409).
- Plain-text read view stays fog; "still preparing" banner removed (unreachable now).
- Fits one passage.

**Why:** article 6 (nothing lost), import dedupe hash stability, owner-vs-viewer and live-lookup rules already in the app.
**How to apply:** leg 4 widens can_edit to the report while PREPARING, keeps hold manager-only, reuses the one-PREPARING refusal for the report-start view.
