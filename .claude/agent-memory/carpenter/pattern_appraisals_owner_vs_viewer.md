---
name: pattern-appraisals-owner-vs-viewer
description: appraisals app's owner-vs-viewer naming convention and where per-row FK-walking model properties have caused N+1 in formsets
metadata:
  type: project
---

The appraisals app has an established, deliberate pattern (see CLAUDE.md "Owner vs viewer") where
display/seeding decisions must be driven by the appraisal's **owner** (`appraisal.teacher`), never
the current viewer. `Appraisal.owner_is_support` (added 2026-09, alongside `_is_leader(staff)`) is
the latest instance of this pattern, used to select support-staff wording for Goal 1 and the
"Leadership/UPR" label, and to drop the Upper-Pay-Range summary question for support staff. This is
the right pattern to reach for — do not re-flag it as unnecessary abstraction.

**Watch point:** `Goal.type_label` (a model property) re-derives `self.appraisal.owner_is_support`
per `Goal` row. `GoalFormSet`/`LastYearGoalFormSet` are `inlineformset_factory` over `Appraisal`;
Django does not cache the parent instance onto rows returned by `.filter(appraisal=instance)`, so
`goal.appraisal` (and then `.teacher`) is a fresh query per goal row — a real but small-scale N+1
(3 goals per formset, two formsets per page). `_build_section_forms` already computes
`appraisal.owner_is_support` once at zero extra cost (teacher is `select_related` in
`get_appraisal_or_403`) and passes it into context as `is_support` — the model property duplicates
that work per-row instead of reusing it. General lesson for this codebase: when a per-row model
property on a formset's child model walks back up an FK the parent view already resolved, check
whether the parent's already-known value could be threaded through instead (e.g. via `form_kwargs`,
the same mechanism already used for `can_teacher`/`can_coach`) rather than adding a walking property.

Related: [[pattern-oneoff-data-correction-commands]].
