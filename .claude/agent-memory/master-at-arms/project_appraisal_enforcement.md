---
name: project-appraisal-enforcement
description: How LMPM appraisals enforce access (chokepoint, field gating, owner-not-viewer variant selection) and what was audited clean
metadata:
  type: project
---

Enforcement pattern in `appraisals/` (verified 2026-09-21):
- Every detail/save view goes through `appraisals/permissions.py:get_appraisal_or_403` (select_related teacher, academic_year). POST saves share `views._save_section`, which re-runs the gate and rebuilds forms via `_build_section_forms(appraisal, role)`.
- Field-level security = `forms.RoleGatedForm.__init__` sets `disabled=True` on every field not in the role's teacher_fields/coach_fields. Subclass `__init__` code runs AFTER that loop, so deleting a field there only shrinks surface; it cannot re-enable anything.
- Variant selection (leader / support / teaching) must come from `appraisal.teacher` (owner), never `current_staff_member`. `Appraisal.owner_is_support` is the support switch; `_is_leader(owner)` the leader one. Grep `staff_type` in views/forms/templates to re-check.
- `AppraisalSummaryForm` drops `on_upper_pay_range` for support owners only while unset (keys off DB instance, not POST) — reviewed clean.
- Management commands under `appraisals/management/commands/` have no admin/web entry point unless an admin action imports the logic (goal_review_fix pattern does; `reword_support_standards_goal` does not). Its stdout prints staff email + goal pk only, no review text.

**Why:** "Owner vs viewer" bug previously seeded the wrong self-review tree onto records.
**How to apply:** on any appraisal change, check these three layers first; see [[settings-decisions]] when settings are audited (not yet recorded).
