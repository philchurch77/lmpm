---
name: project-support-staff-variant
description: Decisions for the support-staff variant of the appraisal Goals/Summary tabs (planned 2026-09-21) — owner flag, UPR field removal, Goal 1 rewording command
metadata:
  type: project
---

Plan of 2026-09-21: support staff saw teacher wording on Goals/Summary tabs.

Decisions:
- One owner-derived rule: `Appraisal.owner_is_support` property (teacher.staff_type == SUPPORT). LEADER and blank staff_type get the teacher version (matches `_ensure_self_review`). Context flag `is_support` from `_build_section_forms`.
- `AppraisalSummaryForm.__init__` DELETES `on_upper_pay_range` from fields for support owners (reads its own instance). Template-hiding alone would save False over a stored True, because `_yesno_field` has empty_value=False.
- Goal 1 support wording: `SUPPORT_STANDARDS_GOAL` beside `DEFAULT_STANDARDS_GOAL` in appraisals/models.py; `seed_goals()` picks by owner. GoalType choices NOT relabelled (would add a migration and is wrong for teachers); per-owner label via `Goal.type_label`.
- Existing rows: management command (report by default, `--apply`), NOT a data migration — wording awaited client confirmation and the rule must be re-runnable (data_import seeds teacher wording when staff_type blank at import). Exact-match title, support owner, not SIGNED_OFF, compare-and-swap `.update()`.
- goal_review_fix `edit_reason()` compares only REVIEW_FIELDS, not title — title rewrite is safe there.
- "Teacher comments" labels go neutral for everyone ("Staff member comments").

**Why:** data-safety invariants (no stored value silently overwritten); owner-not-viewer rule from the earlier self-review mis-seeding bug.
**How to apply:** future variant work on appraisals builds on `owner_is_support`; do not re-open migration-vs-command. Push to main is a live deploy — hold until client confirms placeholder wording. See [[project-open-client-questions]] if written.
