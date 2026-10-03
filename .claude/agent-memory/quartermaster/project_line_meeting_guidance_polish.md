---
name: project-line-meeting-guidance-polish
description: Post-leg-4 polish (2026-10-03) — carried-actions empty-state copy, already_preparing message, bullets.js opt-in helper; decisions and rejected options
metadata:
  type: project
---

Follow-up passage after legs 3+4 (client confusion: no RAG on first meeting, no bullets). No model/migration.

Decisions:
- Empty-state on carried card: two variants only, decided in template from existing context. New page + `source` None = "first meeting recorded here"; everything else = one combined message. No per-case view flag/queries.
- "Previous meeting not marked as held" is NOT an empty-state reason: an unheld previous meeting is PREPARING, so meeting_new/prepare_new redirect to it — the user never sees an empty carried card for that reason. The held rule goes in the intro line, the agreed-actions hint and the already_preparing message instead.
- Legacy-prose panel gets "can't be given a RAG rating" wording (that was one of the reported confusions).
- already_preparing becomes a format string with the preparing meeting's date (`_Starter`, views.py).
- bullets.js (core/static/core/): opt-in `data-bullets`, loaded only from meeting_detail.html `{% block scripts %}` (template needs `{% load static %}`). execCommand('insertText') first, setRangeText + dispatched input event as fallback. Acts only on Enter, collapsed caret at end of line, no modifiers, not isComposing. Appraisals NOT in this passage.
- data-bullets + aria-describedby "bullets-hint" set in forms via one helper AFTER disabling, only on enabled fields; CarriedActionForm must APPEND to its existing aria-describedby (`<prefix>-text`).
- Folded Bosun Mediums: "Add an action" x3 -> "New action N"; Rotation guidance moved from <label> to muted <p id> + aria-describedby. tests.py ~1295 assertNotContains("Add an action") would go vacuous — must be updated.

**Why:** article 6 (helpers measure, never mutate), article 7, keep logic in one home.
**How to apply:** if appraisals later want bullets, reuse bullets.js + the forms helper pattern; do not re-litigate the empty-state variants.
