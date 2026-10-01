---
name: project-line-meeting-collab-chart
description: Line-management RAG actions + report pre-fill chart (drawn 2026-09-30) — settled decisions, model shape, four legs, leg-1 plan rules
metadata:
  type: project
---

Chart drawn 2026-09-30 (round 1 all accepted), docs/chart/line-meeting-preparation.md. Destination: report prepares the next line meeting (RAG + comment on carried actions, proposes actions, Upcoming, Main matters); manager completes and marks Held.

Settled (do not re-open):
- `MeetingAction`: FK `agreed_at` -> LineMeeting (related_name agreed_actions, PROTECT); FK `reviewed_in` -> LineMeeting nullable (related_name reviewed_actions, PROTECT) PINNED at creation of the next meeting — not derived from dates. RAG on the action row (one row spans two meetings, like Goal). `rag` choices RED/AMBER/GREEN, "" = not rated. CheckConstraint reviewed_in != agreed_at.
- `LineMeeting.state` PREPARING/HELD (leg 3); AddField one-off default HELD, model default PREPARING; importer creates HELD. Partial UniqueConstraint one PREPARING per staff replaces `_existing_duplicate`.
- `NOTE_FIELDS` stays the five legacy fields forever (import `source_row_hash`). `is_empty` counts actions; purge respects it.
- Legacy `actions_from_last_meeting` / `actions_from_meeting`: never editable again; shown read-only when stored.
- Version check (leg 2): token `LineMeeting.updated_at`; CAS; 409 via core/recovery.
- Legs: 1 actions+RAG (manager), 2 stale-save refusal, 3 Held state + counts + import CAS, 4 report prepares.

Leg-1 plan decisions (2026-09-30):
- Interim carry-forward source = report's latest meeting by (meeting_date, created_at, pk). New meeting dated earlier than source pins nothing; if typed ratings on carried rows with a back-dated date -> refuse with date error (text kept). Pin ALL unreviewed source actions (not just the posted ones) via CAS `.update()`; count mismatch -> rollback + re-render.
- Column ownership: agreed formset saves update_fields=[description, updated_at]; carried formset saves update_fields=[rag, review_comment, updated_at]. Reason: formset instances loaded before the pin carry reviewed_in=None and would unpin / wipe ratings on full save.
- Extra CheckConstraint: rating/comment only when reviewed_in set. Django 6 CheckConstraint uses `condition=`.
- Pinned action (reviewed_in set): description read-only, not deletable (override `_should_delete_form`).
- Carried formset: modelformset edit_only=True, extra=0; agreed: inline fk_name agreed_at, extra 3 server-side, no JS add-row.
- Double-submit guard extended: fold only when notes + agreed descriptions + carried ratings all equal (else actions-only meetings on same day would fold = loss).
- Admin: MeetingAction registered (needed for core/tests GATED_MODELS registry lookup) + inline, both SuperuserOnlyDeleteMixin; no description in list_display.
- Existing line_management tests need a management-form payload helper once formsets are bound.

Leg-2 plan decisions (2026-09-30):
- Token = separate hidden input `meeting_version` (not a ModelForm DateTimeField: DateTimeInput drops microseconds and localises). Format UTC isoformat; parse fromisoformat; missing/garbled/naive -> treated as stale (409), never as "skip the check".
- meeting_save order: get_meeting_or_403 -> can_edit 403 -> version pre-check 409 -> validate (invalid = re-render bound, same token) -> atomic CAS. The CAS `.update()` writes the token AND the non-disabled LineMeetingForm fields in one statement (no form.save(); auto_now would re-stamp). New version = max(now, old+1us).
- Pin bumps the SOURCE meeting's version (start_meeting), and does so BEFORE pinning actions: consistent lock order (meeting row, then action rows) with meeting_save, avoids Postgres deadlock. Reason: pinning changes what N's page may edit (can_add, settled rows).
- Agreed save_existing becomes CAS on reviewed_in null; 0 rows -> MeetingChanged -> rollback -> 409.
- Conflict UX = core.recovery.render_save_blocked (409), with optional `labels=` override (section names; carried rows labelled with wording from THIS meeting's reviewed_actions only). `meeting_version` added to recovery skip set.
- Admin MeetingAction saves touch agreed_at + reviewed_in meetings; importer UPDATE adds updated_at=now (closes .update() bypass until leg 3 CAS).
- meeting_create out of leg 2 (leg-1 refuse_stale + pin CAS cover it). No migration.

**Why:** article 6 (nothing lost), import dedupe hash stability, owner-vs-viewer and live-lookup rules already in the app.
**How to apply:** plan legs 2-4 inside these; leg 3 narrows the source filter to state=HELD and retires the extended duplicate guard.
