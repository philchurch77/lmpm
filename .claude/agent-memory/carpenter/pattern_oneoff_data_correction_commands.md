---
name: pattern-oneoff-data-correction-commands
description: house style for one-off data-correction management commands in appraisals (report-by-default, --apply/--delete, compare-and-swap)
metadata:
  type: project
---

Established via `purge_misseeded_self_reviews.py` and `appraisals/goal_review_fix.py`, and followed
correctly by `reword_support_standards_goal.py` (2026-09): a one-off remediation command

- reports by default, requires an explicit `--apply`/`--delete` flag to write;
- never overwrites anything a human may have since edited — compares stored text against a known
  default and leaves anything that doesn't match exactly (compare-and-swap on the actual `.update()`
  call, re-filtered by the same title check, so a save landing between report and write is never
  clobbered);
- wraps the write in `transaction.atomic()`;
- prints the pks of everything it touches for auditability;
- is safe to re-run (idempotent once applied).

`reword_support_standards_goal.py` conforms to this house style well — no over-engineering, no
duplicated logic worth flagging. Its docstring and the `SUPPORT_STANDARDS_GOAL` constant's comment
in `models.py` both warn not to deploy/run `--apply` before the client confirms the wording, but
nothing in code enforces that — `seed_goals()` writes the placeholder text to every new support
appraisal the moment this ships, since `git push` to `main` is a live deploy (per CLAUDE.md). Worth
checking on any future review of this same file whether the wording has since been confirmed/removed
as a placeholder.

Related: [[pattern-appraisals-owner-vs-viewer]].
