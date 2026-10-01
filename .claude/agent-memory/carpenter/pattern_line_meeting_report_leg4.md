---
name: line-meeting-report-leg4
description: Leg-4 report-prepares-meeting design in line_management (edit_scope, _StartUrls, shared _new/_create); findings raised so they are not re-raised blindly
metadata:
  type: project
---

Leg 4: permissions.edit_scope(role, meeting) -> SCOPE_ALL/PREPARE/NONE threaded through views._bind; LineMeetingForm(report_scope) disables all but REPORT_FIELDS; prepare_new/prepare_create behind get_own_staff_to_prepare_or_403; manager's create body is shared _new/_create with a _StartUrls carrier; save_meeting_page(as_report) adds state=PREPARING to the CAS WHERE.

Reviewed 2026-10-01 (reported, not edited). Verified sound: gate order in meeting_save, hold gated before the repeat fold, scope passed on every bind, editable fields derived from form.fields disabled. Raised: _StartUrls fine but a frozen dataclass or a role-keyed helper would be one line shorter (Low); _create still ~150 lines, closures refuse/refuse_stale/find_repeat unchanged (Medium, carried from leg 3); can_edit_meeting and can_prepare_meeting now only called by edit_scope (fold or underscore); can_hold_meeting same body as can_edit_meeting, now diverges only in intent; _render_new builds LineMeeting(staff=member) just to call edit_scope (acceptable, but a role-only answer is clearer); _refuse_preparing/_new/back-dated message each fork on role with near-identical prose (Low); _held_while_preparing and _stale_save share the render_save_blocked shape (acceptable, wording differs); implicit string concatenation inside a conditional expression in _new is a precedence trap.

**How to apply:** check whether taken before re-raising.
