---
name: project-line-meeting-enforcement
description: How LMPM line_management enforces access (live manager lookup, chokepoints, MeetingAction formset scoping) and what was audited clean in the actions change
metadata:
  type: project
---

Enforcement pattern in `line_management/` (verified 2026-09-30, leg-1 actions change):
- Chokepoints: `permissions.get_meeting_or_403` (detail/save) and `get_managed_staff_or_403` (list/new/create). Manager = live compare via `is_current_line_manager`. `meeting_save` 403s unless `can_edit_meeting(role)`.
- `MeetingAction` rows are edited via two formsets in `views.py`: `_agreed_formset` (inline, fk `agreed_at`, Django filters queryset to the parent meeting) and `_carried_formset` (edit_only modelformset; queryset = `meeting.reviewed_actions` on save, `services.carried_forward_candidates(carry_forward_source(member))` on create).
- Django behaviour relied on (checked in .venv django/forms/models.py): the `id` ModelChoiceField spans the WHOLE table, but `_existing_object` looks up in the scoped queryset; an out-of-scope id yields an unsaved instance that `save_existing_objects` skips (silently, no form error). InlineForeignKeyField rejects a mismatched `agreed_at`. Probed empirically with crafted POSTs 2026-09-30: no cross-report read/write/delete/re-parent/pin.
- `save_existing` overrides use `update_fields`, so `reviewed_in` is never written from a form; only `services.start_meeting` pins (compare-and-swap `.update()`).
- Admin: `MeetingActionAdmin` + `MeetingActionInline` both carry `SuperuserOnlyDeleteMixin`; `__str__` deliberately excludes description. core/tests.py `GATED_MODELS` checks registered ModelAdmins only, not inlines.

Open points raised (check whether fixed next audit): `LineMeetingAdmin.staff` editable on change -> re-parenting a meeting breaks the "both meetings of an action belong to one staff member" invariant; no IDOR tests for action formsets yet; create-path race where carried formset can write rag onto an action pinned elsewhere between validation and `start_meeting`.

**How to apply:** on any line_management change, re-check the two formset querysets derive from the gated meeting/member, and that no form field list includes `agreed_at`/`reviewed_in`. See [[project-appraisal-enforcement]] for the appraisals equivalent.

Leg 2 stale-save refusal (audited 2026-09-30, uncommitted at the time):
- `views.meeting_save` order is load-bearing: `get_meeting_or_403` -> `can_edit_meeting` 403 -> `parse_version` compare -> 409 via `core.recovery.render_save_blocked`. Role loss must stay a 403 (CLAUDE.md "Data safety" rule); re-check the order on any edit.
- `_handback_labels` reads action wording only from `meeting.reviewed_actions` keyed by posted id; foreign id -> generic "(row N)" label. Probed with crafted POSTs: no cross-report wording disclosed; stranger/report/outgoing manager all 403 with valid, stale and malformed tokens.
- Version token = `LineMeeting.updated_at` UTC isoformat in a POST hidden field only; `meeting_version` is in `core.recovery._SKIP_EXACT`. Malformed/naive/extreme tokens -> 409, never a save or 500. No app-level logging in line_management.
- Writers that must bump the meeting version: `services.save_meeting_page` (CAS), `start_meeting` (touch source), admin inline/standalone action save+delete (`touch_meetings`), `data_import.apply_line_meeting_row` update. A new `.update()` on LineMeeting/MeetingAction that skips this is a Purser matter, not an access one.
- Probe script pattern: run via `manage.py shell -c "exec(open(path).read())"` (piping into shell breaks multi-line blocks).

Leg 3 Held/Preparing state (audited 2026-10-01, uncommitted at the time):
- `LineMeeting.state` (PREPARING/HELD) is on NO form (`LineMeetingForm` fields = date + 3 notes); only writers are `services.save_meeting_page(hold=True)` (inside the version CAS), `meeting_create` (constructor), importer create (HELD), admin add (readonly on change). Re-check no form ever gains `state`.
- `meeting_save`: 403 (`can_edit_meeting`) precedes `hold`; `hold` = posted key AND `can_hold_meeting(role)` AND not held. `can_hold_meeting` is deliberately separate from `can_edit_meeting` for leg 4 (report edits, never holds) — when leg 4 widens can_edit, re-probe that a report's `hold=1` is ignored, not honoured.
- `meeting_create` `refuse_preparing` 409 uses `_handback_labels(post, None)` (positional labels, reads nothing). `meeting_new` redirect sits after `get_managed_staff_or_403`.
- Importer skip messages `EDITED_IN_APP` / `STRANDS_ACTIONS` are module constants; no record text reaches `ImportRow.error_message`.
- Dashboards: `services.held_meeting_summary()` changes annotations only; row scoping still `line_managed_staff` (team) / `_require_superuser` (overview).
- Probed with an inline `manage.py shell -c "$PROBE"` (heredoc into a shell var, create_test_db/destroy_test_db) — no file written. Report/stranger hold -> 403; crafted `state=PREPARING` on a Held meeting -> ignored.
- Leg-3 access tests were not yet in the suite at audit time (hold role gate, un-hold, refuse_preparing no-leak); check whether added.

Leg 4 report-prepares (audited 2026-10-01, uncommitted at the time):
- `permissions.edit_scope(role, meeting)` -> SCOPE_ALL / SCOPE_PREPARE (report, not Held) / SCOPE_NONE; every `views._bind` takes `scope`. `LineMeetingForm(report_scope=True)` disables all but `REPORT_FIELDS` (allowlist: date, upcoming, main_matters). `save_meeting_page` writes only non-disabled fields, and `as_report=True` adds `state=PREPARING` to the CAS WHERE.
- Report start views `prepare_new` / `prepare_create` (pk-less) go through `get_own_staff_to_prepare_or_403` -> `may_start_preparing` (blank or self line_manager_email -> 403; superuser without StaffMember -> 403). Shared workflow `_new`/`_create`; `hold` gated by `can_hold_meeting(role)` before anything reads it.
- `meeting_save` order now: get_meeting_or_403 (403) -> scope NONE: report -> 409 `_held_while_preparing` (POST echo + labels from that meeting's reviewed_actions only), others 403 -> hold gate -> version 409.
- Probed (inline shell -c probe, `read -r -d '' X <<'EOF'` form; the `$(cat <<EOF)` form broke on long scripts): crafted rotation/hold/state/staff ids, foreign carried/agreed ids on create and save, report on Held (current/stale/missing stamp), outgoing vs successor manager mid-preparation — all held. Crafted foreign agreed id WITH DELETE is silently ignored (Django skips deleted extra forms), without DELETE is a form error; foreign row untouched either way.
- Guidance-polish pass (audited 2026-10-03): `forms._offer_bullets` only adds widget attrs (`data-bullets`, `aria-describedby`) to fields NOT disabled, called last in each form `__init__`; widget attrs are per-instance (Field deepcopy), so no cross-form leak. `core/static/core/bullets.js` is keydown-only, no network, no `.value` assignment, re-checks `disabled`/`readOnly`. Script load gated on template `can_edit` — cosmetic only. `_new` `already_preparing` message now formats the preparing meeting's date; it runs after `get_managed_staff_or_403` / `get_own_staff_to_prepare_or_403` and redirects to that same meeting, so nothing new disclosed. If a future change disables a field AFTER `_offer_bullets` (e.g. in a view), the attr would be stale but harmless — `disabled` still governs POST.
- At audit time there were NO direct tests of the prepare chokepoint, the rotation-disabled rule or report crafted-id isolation — check whether added.
