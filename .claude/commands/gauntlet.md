# The Gauntlet — Pupil Data Review

> **Note — carried over from the sibling OSED project.** This workflow targets
> apps that do **not** exist in LMPM (`tolerance`, `sdq`, `flashcards`,
> `evaluation`) and an OpenAI integration LMPM does not have. It will not run
> meaningfully here as written. For an LMPM review use **`/fitness-review`**
> (which runs Victor for the security/GDPR pass); keep this file only if you
> intend to re-tailor it to LMPM's staff-data model.

You are running The Gauntlet: the mandatory review for any change that touches pupil data, permissions, or the AI apps.

This workflow exists because this platform holds Article 9 special category data about children in care — names, emotional states, observed behaviours. No change to the `tolerance` app, `sdq` app, `flashcards`, or `evaluation` apps ships without passing this workflow.

---

## Stage 1 — Victor: GDPR and security audit

Spawn Victor on all files changed in this feature. Ask Victor to confirm:

- No pupil names or identifying details are sent to the OpenAI API
- All querysets that return pupil-linked data filter by the logged-in user — no cross-user or cross-school access possible
- No pupil data appears in URLs, logs, or error messages
- Any new field on `Observation`, `WeeklyMap`, or `SDQResponse` has a clear, minimal purpose with no unnecessary data collection
- Permissions are enforced in views and querysets — not only in templates

---

## Stage 2 — Vera: End-to-end QA

Spawn Vera to test the changed feature as a real user would. Vera should specifically check:

- The core workflow completes without errors
- A logged-in user cannot accidentally reach another user's records by guessing a URL or PK
- The feature behaves correctly from a logged-out state, a wrong-user state, and the correct-user state

---

## Report

Summarise:
- What Victor flagged and how it was resolved
- What Vera found
- Whether the change is safe to ship

**If either agent raises an unresolved concern, do not mark the task complete.** Surface it clearly to the developer for a human decision — especially anything that may require a DPIA check before shipping.
