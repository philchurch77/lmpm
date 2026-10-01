---
name: line-meeting-actions-leg1
description: Leg-1 MeetingAction design in line_management and the review findings raised, so they are not re-raised blindly
metadata:
  type: project
---

Leg 1 of docs/chart/line-meeting-preparation.md: MeetingAction rows, pinned via reviewed_in in line_management/services.py (start_meeting CAS). Three forms bound per page (LineMeetingForm + agreed/carried formsets) in views.py.

Raised 2026-09-30 (reported, not edited): private formset._should_delete_form used in views; override in forms is redundant because add_fields pops DELETE on pinned rows; can_delete_extra=False would remove the need; `_has_content` re-states the note-field rule (also in LineMeeting.is_empty and purge command); three-form binding repeated in 4 views; `agreed.instance = meeting` is a no-op; unpinned-delete race (delete should filter reviewed_in__isnull=True).

**Why:** article 3 (one home for a rule) and article 6.
**How to apply:** on the next review of this app, check whether these were taken before re-raising. Watch leg 3 (carry_forward_source narrowing to Held meetings).
