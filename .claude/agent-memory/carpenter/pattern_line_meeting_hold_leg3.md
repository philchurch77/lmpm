---
name: line-meeting-hold-leg3
description: Leg-3 PREPARING/HELD state in line_management and review findings raised, so they are not re-raised blindly
metadata:
  type: project
---

Leg 3 of docs/chart/line-meeting-preparation.md: LineMeeting.state (PREPARING/HELD), "hold" submit button, partial unique constraint, migration 0003 hand-edited (verified sound). Shared helpers in line_management/services.py: latest_meeting, carry_forward_source (Held only), preparing_meeting, page_would_be_blank, held_meeting_summary (used by overview/views.py and team/views.py; services is the right home because core cannot import a feature app).

Reviewed 2026-10-01 (reported, not edited). Raised: meeting_create is ~170 lines with 5 closures; only fold/refuse_preparing/out-of-date block are pure and movable to module level; page_would_be_blank reads private formset._should_delete_form (use public formset.deleted_forms, or a keeps_any_action() method on BaseAgreedActionFormSet beside new_descriptions) and takes an awkward `not meeting.has_notes` (pass meeting); IntegrityError-branch pk/_state reset is a no-op when the INSERT itself failed (pk never assigned) and only matters if agreed.save() raised; importer's "would strand actions" rule is a raw query in data_import/services.py that belongs in line_management/services.py beside may_carry_from; its check counts actions agreed at PREPARING meetings too and is not re-checked in apply; validate (Python compare) and apply (SQL CAS) of notes is deliberate double check, could share one helper; status-badge span copied 4x in templates (want _state_badge.html include); .status-badge--preparing/held and .hold-hint inserted mid import-badge block in styles.css; can_hold_meeting body identical to can_edit_meeting (deliberate, plan).

**How to apply:** check whether taken before re-raising. Leg 4 (report edits PREPARING) will make can_hold_meeting vs can_edit_meeting diverge for real.
