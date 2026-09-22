---
name: project-appraisals-data-map
description: Appraisals app stores/cascades map - free-text fields, bounded fields, on_delete decisions, UPR boolean semantics, Goal 1 reword command scope (audited 2026-09-21)
metadata:
  type: project
---

Audited 2026-09-21 (support-staff wording change). No migration in that change; `makemigrations --check` clean.

**Free text (TextField, unbounded):** Goal title/steps_to_success/success_criteria/teacher_review_comment/coach_review_comment; Appraisal cpd_requirements/summary_teacher_comment/summary_coach_comment; SelfReviewItem.evidence; SelfReview job_summary/level_description; LeaderStandard.examples.
**Deliberately bounded:** SelfReview.signed_name CharField(200), browser maxlength matches the model on purpose (a name, not prose).

**Cascades:** Appraisal.teacher and Appraisal.academic_year are PROTECT, so deleting a StaffMember/year cannot take history. Appraisal -> Goal/SelfReview/items/bullets/LeaderReview/standards are CASCADE from the record itself; accepted via SuperuserOnlyDeleteMixin (superuser-only delete, delete_selected removed). No written retention rule yet.

**Boolean yes/no fields** (`_yesno_field`, empty_value=False): a bound field with no key in POST saves False, because False is not in empty_values, so construct_instance does not skip it. Any yes/no field left bound but not rendered silently clobbers a stored True. Verified by running it.

**UPR on support appraisals:** the field is removed from AppraisalSummaryForm only while the stored value is False. A stale page (loaded while False) posted after True was written elsewhere saves False. That is the documented last-write-wins gap, not a new regression.

**reword_support_standards_goal:** matches only an exact DEFAULT_STANDARDS_GOAL title, so round-trips stay safe (the form strips whitespace). As audited it had NO year filter, so it reworded prior-year DRAFT goals that carry review comments. No backup record, and it prints staff emails. goal_review_fix.edit_reason compares REVIEW_FIELDS only, never title.

**Why:** so the next count starts from the known map.
**How to apply:** re-verify each of these against the current code before relying on it. The working tree was edited mid-audit once, so hash the files at the start and re-check before reporting. The guard hook blocks scratch files: pipe checks into python via stdin against the in-memory test DB (`setup_databases()`); never manage.py shell.
