---
name: line-meeting-polish-bullets
description: RAG guidance copy + "- " bullet helper (core/static/core/bullets.js, forms._offer_bullets); what was reviewed and raised
metadata:
  type: project
---

Reviewed 2026-10-03 (reported, not edited). bullets.js is opt-in via data-bullets set by forms._offer_bullets after disable logic; keydown-only, execCommand with setRangeText fallback; respects the "measure, never mutate" rule. Sound.
Raised: carried-actions template has two near-identical "Nothing to review" panels (Low); `describedby` param on _offer_bullets used by one caller (Low); hint id "bullets-hint" lives in meeting_detail but is referenced from forms.py, so the two must stay in step (Low); static `?v=` cache-bust is hand-edited.
**How to apply:** do not re-raise unless the helper grows beyond Enter handling.
