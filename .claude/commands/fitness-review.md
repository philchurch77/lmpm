# Fitness Review — Is LMPM Fit for Purpose?

You are running the Fitness Review: an end-to-end check of the whole LMPM app,
biased toward the **staff login and first-time data-editing experience**. Use it
before relying on the live app for a new cohort, after a round of fixes, or
whenever you want a ship/no-ship read.

The real apps in this repo are `core`, `appraisals`, `line_management`, `team`,
`overview`, and `data_import`. Do not review apps that don't exist here.

Work through each stage in order. Surface failures as you go; don't stop at the
first one — collect everything, then give a single verdict at the end.

---

## Stage 1 — Live-data readiness

Run the data audit and summarise it:

```
.venv/Scripts/python.exe manage.py check_readiness
```

This lists onboarding dead-ends in the *actual data*: no active `AcademicYear`,
staff with no `staff_type`, staff with no login, logins with no `StaffMember`,
missing `SchoolProfile`s, staff with no school, and dangling manager emails. A
non-zero exit means at least one **blocker** — a real staff member cannot get
started. Report the blockers and warnings plainly. (For a production read, run
this against the Azure DB via SSH, not local SQLite — see CLAUDE.md.)

---

## Stage 2 — Correctness gate

1. `.venv/Scripts/python.exe manage.py check` — Django system checks, clean.
2. `.venv/Scripts/python.exe manage.py migrate --check` — no unapplied migrations.
3. Run the full test suite and report pass/fail:
   `.venv/Scripts/python.exe manage.py test appraisals line_management data_import core`

Any failure here is a no-ship until fixed.

---

## Stage 3 — Security & access (Victor)

Spawn **Victor** to audit permissions, IDOR, GDPR/data-exposure, and deployment
safety. LMPM's identity model is email-based with no FK between `User` and
`StaffMember` — pay special attention to the `get_*_or_403` chokepoints, the
coach-email *snapshot* vs line-manager *live lookup* distinction, and the SSO
authorization gate in `core/allauth_adapters.py`. Fix any High findings before
shipping; record Medium/Low as follow-ups.

---

## Stage 4 — Onboarding UX (Stella)

Spawn **Stella** focused on the first-time journey templates: the login page
(`templates/account/login.html`), the landing page (`core/home.html`), and the
empty states (`appraisals/empty_state.html`, `appraisals/no_staff.html`,
`line_management/no_staff.html`, `line_management/my_meetings.html`,
`team/my_team.html`). The bar: a newly-provisioned staff member should always
have a clear next action and never hit a dead-end.

---

## Stage 5 — End-to-end QA (Vera)

Spawn **Vera** to walk the whole first-time journey as a real user:
provision → log in → land on Home → open **My Appraisal** → start and edit it →
(for a manager) open **My Team** and create a line meeting. Test the awkward
cases too: unclassified `staff_type`, no current academic year, a login with no
`StaffMember`, and a rejected login (unprovisioned email). Confirm every path
ends somewhere useful.

---

## Finish

Give a single verdict: **Fit for purpose** or **Not yet — fix these first**,
with:
- Data blockers from Stage 1 (who can't get started, and why)
- Any failed checks/tests from Stage 2
- Victor's High findings and whether they're fixed
- Stella's and Vera's dead-ends / friction, ranked
- A short prioritised follow-up list for anything deferred
