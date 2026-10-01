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

## Legs
| # | Leg | Blocked by | Delivers | Crew | Status |
|---|---|---|---|---|---|
| 1 | Line manager records actions and RAG-rates last meeting's actions | — | `MeetingAction` + migration; `start_meeting` pinning carried actions; agreed-actions and carried-actions (RAG + comment) formsets on create/detail; legacy action text read-only; extended `is_empty` + purge; admin inline (superuser-only delete, `GATED_MODELS`); RAG pill widget; formset error includes; `NOTE_FIELDS` pin test | Quartermaster, Carpenter, Bosun, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | **done 2026-09-30** (not yet committed/deployed) |
| 2 | A stale save is refused and the typed text handed back | 1 | hidden `updated_at` token; compare-and-swap save service; action-only saves touch the meeting; 409 via `render_save_blocked`; two-session tests | Quartermaster, Carpenter, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | **done 2026-10-01** (not yet committed/deployed) |
| 3 | Line manager starts a meeting to prepare and marks it Held; dashboards count Held only | 1 | `state` migration (existing → Held); partial unique constraint; "Mark as held"; `_existing_duplicate` retired; carry-forward from latest Held; overview/team counts filter Held; importer creates Held; import UPDATE path becomes compare-and-swap (skip + report if notes changed since the prior import); state badge | Quartermaster, Carpenter, Bosun, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | open |
| 4 | The member of staff prepares their next line meeting | 2, 3 | report-start view/URL + chokepoint (non-blank `line_manager_email`); `can_prepare_meeting`; per-field and per-row gating in forms; read-only once Held; "Prepare next meeting" on My Line Meetings; role-matrix + IDOR tests (crafted POST cannot touch Rotation; stranger 403 on every new endpoint; successor manager inherits mid-preparation) | Quartermaster, Carpenter, Bosun, Master-at-Arms, Purser, Gunner, Lookout; Gauntlet; Purser | open |

## Not yet charted
- **Importer overwrite (fold into leg 3)**: `data_import/services.py` re-applies all five note fields
  with `.update()` on a `source_row_hash` match, overwriting in-app edits to imported meetings. Older
  than this chart; leg 3 already plans the compare-and-swap.
- Colour contrast of `--rag-green` / `--rag-red` with white 13px text (~3.4:1 / ~4.4:1) — shared
  app-wide tokens, so a separate tidy, not this chart.
- Read-only view for the report renders disabled inputs; a plain-text read view (RAG badges) would
  read better. Revisit in leg 4, where the report's view changes anyway.
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
