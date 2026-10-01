---
name: line-meeting-version-token-leg2
description: Leg-2 stale-save design in line_management (updated_at as version token) and review findings raised, so they are not re-raised blindly
metadata:
  type: project
---

Leg 2 of docs/chart/line-meeting-preparation.md: LineMeeting.updated_at is a hidden `meeting_version`; `services.save_meeting_page` does a conditional UPDATE (version + editable note fields) then the action formsets; 409 via core/recovery.render_save_blocked. Rule: every writer of what a page edits must advance updated_at (`touch_meetings`): start_meeting (source), admin save_formset/save_model/delete_*, importer update.

Reviewed 2026-09-30 (reported, not edited). Verified fine: aware-datetime equality on SQLite and Postgres (microseconds kept), editable dict = construct_instance values, bypassing save()/full_clean safe (ModelForm already validated; save() only normalises created_by_email), carried rating saves at N need not move N-1 (N-1 page shows only the reviewed date, read-only). Raised: pre-check is required (not redundant with CAS) but its view comment misstates why; leg-1 per-row guards (clean pinned branch, save_existing CAS, delete_existing silent skip) are now mostly unreachable via meeting_save; delete_existing skips silently while save_existing raises; double-click Save yields 409 on second POST; admin touches meeting AFTER action write (lock order opposite to page save, deadlock risk on Postgres, rare); no leg-2 two-session tests existed yet; "agreed" prefix literal in _handback_labels vs CARRIED_PREFIX constant; date edit on meeting N changes which meeting is latest without moving other meetings' version (leg-1 can_add clean branch still catches it, so keep that branch).

**How to apply:** check whether these were taken before re-raising. Watch leg 3 (Held meetings, importer CAS).
