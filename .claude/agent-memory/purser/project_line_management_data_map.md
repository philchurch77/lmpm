---
name: project-line-management-data-map
description: Line-management stores map - LineMeeting/MeetingAction free text, PROTECT cascades, legacy action prose, formset id-queryset trap, importer overwrite (audited 2026-09-30)
metadata:
  type: project
---

Audited 2026-09-30 (leg 1 of the line-meeting actions chart; migrations were uncommitted at the time).

**Migrations:** 0001_initial (LineMeeting). 0002_meetingaction: CreateModel MeetingAction only, three CheckConstraints, additive, no RunPython. `makemigrations --check` clean.

**Free text (TextField):** LineMeeting upcoming/rotation_update/main_matters, plus legacy actions_from_last_meeting/actions_from_meeting (off every form since 0002, rendered read-only; NOTE_FIELDS still lists all five because the importer's source_row_hash uses it). MeetingAction description/review_comment. rag is CharField(5) choices, fine.

**Cascades:** LineMeeting.staff PROTECT; MeetingAction.agreed_at and reviewed_in both PROTECT. Admin delete of a meeting holding actions (single and delete_selected) gives Django's clean "Cannot delete" page - verified by running. Deleting a MeetingAction itself (formset DELETE on unpinned, admin for superuser) is a hard delete.

**Trap - model formset id field:** Django builds the `id` ModelChoiceField over the model's default manager, NOT the formset queryset. A POSTed id outside the queryset binds a blank new instance and save_existing_objects skips it: the user's input is silently dropped under a success message. Verified: stale new-meeting page after a concurrent create lost the RAG + comment with a 302. Check this on any edit_only / inline formset.

**Admin:** rating (rag/comment) an unpinned action in admin raises IntegrityError 500 (constraint references reviewed_in, which is readonly/excluded so validate_constraints skips it).

**Importer:** apply_line_meeting_row `.update(**notes)` on a hash match rewrites all five note fields + created_by_email, clobbering in-app edits made since the import. Pre-existing; never touches MeetingAction.

**Leg 2 (version token, audited 2026-09-30, no migration):** LineMeeting.updated_at is the page version; save_meeting_page is a conditional .update() then formset saves in one atomic. Verified by running: stale/tokenless/malformed save writes nothing and 409 echoes every value (CRLF, emoji, RAG raw values); fresh save + resave identical; pin from N+1, admin action edit/inline and import update all advance the version.
Open at audit: (a) admin LineMeeting change form has no version check, so a stale admin form silently overwrites a page save (verified); (b) admin paths lock action rows before meeting rows (reverse of page save / start_meeting) - Postgres deadlock risk, page loser gets a 500 and loses text; (c) create page open across the leg-1/2 deploy re-renders without the legacy actions_from_* text it posted (verified); detail page across deploy is fine (409 echo).
Recovery filters skip -id, -DELETE, meeting_version; a ticked DELETE is not echoed. Inline fk key "agreed-N-agreed_at" is echoed as noise.

**Leg 3 (state PREPARING/HELD, audited 2026-10-01):** 0003_linemeeting_state hand-edited: AddField default HELD preserve_default=False, AlterField default PREPARING, partial UniqueConstraint one-preparing-per-staff, CheckConstraint. Additive; verified by executor on throwaway SQLite DB that every existing row became HELD with all text byte-identical, and reverse drops `state` only (re-forward turns every PREPARING into HELD). No psycopg locally, so Postgres SQL could not be printed. Old code still serving during deploy inserts without `state` and has no DB default -> NOT NULL 500 in the swap window (fix: db_default=HELD).
Importer UPDATE path now never writes notes: validate SKIPs when stored notes != incoming; apply is CAS filter(pk, **notes) writing created_by_email only. Verified by running (edit before preview and between preview/confirm both SKIP, text kept). CREATE refused by would_strand_actions (unreviewed actions on/before the date).
meeting_create refusals verified by running: preparing-exists 409 echo, back-dated Save bound re-render, IntegrityError race 409 echo; but IntegrityError with no preparing meeting found re-raises (500, text lost). Stale "Save and mark as held" never folds into a non-held meeting (verified). Hand-back for a refused create labels carried ratings by row only (no action wording).
purge_empty_line_meetings does not filter on state: it lists/deletes an emptied PREPARING meeting (verified dry-run).
Code was edited mid-audit again (09:47 refactor: helpers moved to services/forms); re-diff before reporting.

**Why:** so the next count starts from the known map.
**How to apply:** re-verify against current code. Files were edited mid-audit again (cosmetic); check mtimes before reporting. Probe via stdin script with DiscoverRunner.setup_databases(); set PYTHONIOENCODING=utf-8 or emoji output crashes on Windows.
