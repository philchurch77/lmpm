# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

LMPM (Line & Performance Management) is a Django 6 app for the OXLIP trust. Its purpose is to
manage staff professional development (PD) within a line-management hierarchy:

- **Staff** log in securely (Microsoft SSO) to create and edit their own professional development
  records.
- **Managers** can view the PD data of the people they manage. Who manages whom is defined by the
  line-management and performance-management relationships on each staff member (see `StaffMember`
  below) — so access to another person's PD data is determined by these reporting relationships.

It is built on the same platform as a sibling project, OSED: Microsoft SSO via django-allauth,
WhiteNoise static serving, and Azure App Service + Postgres deployment. The review-specific
functionality from OSED has been removed; this repo starts from a shared platform layer (the `core`
app) onto which the PD / line-management / performance-management features are added.

## Commands

All commands assume the virtualenv is active. On this Windows machine the interpreter is
`.venv\Scripts\python.exe` (note: the Windows Store `python` shim may fail to launch — call the
venv interpreter directly if so).

```bash
.venv/Scripts/python.exe manage.py runserver          # run the dev server
.venv/Scripts/python.exe manage.py makemigrations      # after changing models
.venv/Scripts/python.exe manage.py migrate             # apply migrations
.venv/Scripts/python.exe manage.py check               # system checks (run after model/admin edits)
.venv/Scripts/python.exe manage.py createsuperuser     # local admin login
.venv/Scripts/python.exe manage.py seed_schools        # populate the trust's schools (idempotent)
.venv/Scripts/python.exe manage.py seed_branding       # populate the single branding row (idempotent)
.venv/Scripts/python.exe manage.py seed_testdata       # appraisals: a local test cohort (teacher/coach/head logins, all @test.local)
.venv/Scripts/python.exe manage.py provision_users     # give imported StaffMembers a login: create matching User + SchoolProfile (idempotent; --dry-run to preview). Saving a StaffMember in the admin does this too — see "Granting access"
.venv/Scripts/python.exe manage.py check_readiness     # read-only audit of onboarding dead-ends (unclassified staff, no login, no active year, dangling manager links); non-zero exit on any blocker
.venv/Scripts/python.exe manage.py start_next_year     # advance to the next academic year: create the year after the current one (if needed) and mark it current (idempotent; backs the admin "Start next academic year" button)
.venv/Scripts/python.exe manage.py purge_empty_line_meetings           # delete legacy line meetings with no note content
.venv/Scripts/python.exe manage.py purge_empty_line_meetings --dry-run # preview what would be deleted
.venv/Scripts/python.exe manage.py move_prior_year_goal_reviews --dry-run  # preview the one-off goal-review correction (see "Goal review correction")
.venv/Scripts/python.exe manage.py move_prior_year_goal_reviews --backup-file <path outside the repo>  # run it; --backup-file is REQUIRED and refused inside BASE_DIR
.venv/Scripts/python.exe manage.py purge_misseeded_self_reviews          # report self-reviews seeded against the wrong staff type (see "Owner vs viewer")
.venv/Scripts/python.exe manage.py purge_misseeded_self_reviews --delete  # remove the provably-blank ones; reviews with content are always kept

# Tests (Django test runner; every app has a suite — core covers the SSO auth gate + readiness command)
.venv/Scripts/python.exe manage.py test                # all tests
.venv/Scripts/python.exe manage.py test line_management  # one app (has a full role/IDOR test suite)
.venv/Scripts/python.exe manage.py test appraisals       # one app (role/IDOR + self-review scoring test suite)
.venv/Scripts/python.exe manage.py test data_import      # one app (bulk import gate + idempotency tests)
.venv/Scripts/python.exe manage.py test core.tests.SomeTest.test_method   # a single test
```

First-time local setup is in README.md. Production runs `startup.sh` under gunicorn (Azure).

## Architecture

**`lmpm/`** — project package: `settings.py`, `urls.py`, `wsgi.py`/`asgi.py`. New feature apps are
mounted in `lmpm/urls.py` at the marked spot.

**`core/`** — shared platform layer reused by every feature: `School`, `SchoolProfile`,
`StaffMember`, `Branding`; the Microsoft login adapter; the branding context processor; the base
template (sidebar nav) and home page. `core/identity.py` holds `current_staff_member(request)` —
the **shared** email→`StaffMember` resolver (request-cached) that every feature app imports rather
than reimplementing. New line-management / performance features should each be their **own Django
app**, added to `INSTALLED_APPS` and mounted in `lmpm/urls.py`.

**`appraisals/`** — the first feature app: annual teacher/support-staff appraisals. See
"Appraisals app" below.

**`line_management/`** — the second feature app: line-management meeting notes. See
"Line management app" below.

**`team/`** — owns no models; composes the appraisals and line-management permission helpers into a
single read-only "My Team" page. See "Team app" below.

**`overview/`** — owns no models; superuser-only, trust-wide dashboards composed from the appraisals
and line-management apps. See "Overview app" below.

**`data_import/`** — superuser-only bulk CSV importer for migrating staff and historical PD data
into the app. See "Bulk import app" below.

### Auth & authorization (the key design point)

Authentication and authorization are deliberately split:

- **Authentication** is delegated entirely to Microsoft (Entra) SSO via django-allauth.
- **Authorization** is enforced in-app by `core/allauth_adapters.py` (`RestrictMicrosoftLoginAdapter`,
  wired via `SOCIALACCOUNT_ADAPTER`). On every social login it requires the email to match an
  existing active Django `User`; non-superusers must additionally have a `SchoolProfile`, which is
  both the school link **and** the access gate. `SOCIALACCOUNT_AUTO_SIGNUP = False` means accounts
  are never auto-created — users must be pre-provisioned. Superusers bypass the `SchoolProfile`
  requirement.

So "give a user access" = create a Django `User` with the right email (+ a `SchoolProfile` for
non-superusers). There is no self-service signup.

### Email normalisation (identity depends on it)

Identity is an email string compared across tables with no FK between them, so the spelling of that
string is load-bearing. `core.identity.normalise_email` is the one definition — stripped, lower case.

`StaffMember` (and `Appraisal.coach_email`, `LineMeeting.created_by_email`) normalise in their own
`save()`. **`auth.User` is third-party and normalises nowhere** — Django's `create_user` calls
`normalize_email`, which lower-cases only the *domain* — so an account could sit in the database as
`A.Green@School.uk` while every record the app held for that person said `a.green@school.uk`. Two
things now hold the line:

- **Writes**: `core.admin.NormalisingUserAdmin` (a `UserAdmin` subclass, registered over Django's)
  lower-cases the email on save, and `core.provisioning` only ever creates lower-cased ones. Migration
  `core/0005_normalise_emails` normalised the existing rows once. `User.username` is deliberately left
  alone — identity is by email, the username is cosmetic, and it has a unique constraint.
- **Reads stay case-insensitive anyway** (`iexact`, or `Lower()` on both sides). That is defence in
  depth, not redundancy: a bulk `.update()`, a fixture or raw SQL can still bypass the write paths.
  `check_readiness` reports any un-normalised email it finds, because nothing is *broken* by one until
  the next query that compares exactly — which is precisely how the last such bug got in.

> The unique constraint on `StaffMember.email` means lower-casing can collide. Migration 0005 skips
> any row whose lower-cased form is already taken rather than raising `IntegrityError` and aborting a
> deploy; the survivor is reported by `check_readiness` for a human to merge.

### Granting access: `core/provisioning.py`

All the rules for turning a `StaffMember` into a working login live in **`core/provisioning.py`**,
and three front ends share them so they cannot drift: `StaffMemberAdmin.save_model`, the
**"Give selected staff a login"** admin action, and `manage.py provision_users`. Every decision is a
named `ProvisionOutcome` with a matching message in `OUTCOME_MESSAGES`, so **no front end ever skips
silently** — that was the original bug.

**Saving a `StaffMember` in the admin provisions them**, on *every* save, not just creation. That is
load-bearing: a person added before their school is known is reported as not-yet-provisionable, and
setting the school later and saving again finishes the job. The previous CLI-only flow skipped a
school-less row and never revisited it, so assigning the school afterwards left them permanently
unable to sign in with nothing on screen to say why — the fault this module was written to remove.

**Both the save hook and the action are superuser-gated, and the gate on `save_model` is the
load-bearing one.** `list_editable` makes Django's changelist POST call `save_model` once per changed
row checking only `has_change_permission`, so gating the action alone would be decorative: a user
with plain change rights could tick every row, nudge a dropdown, and mint a live SSO account for each.

Rules that are deliberate and separately tested:
- **An existing user is never reactivated.** Deactivating a `User` is how a leaver is offboarded, and
  provisioning must not quietly undo it. An inactive match returns `SKIPPED_INACTIVE_USER`.
- **Two active users sharing an email is refused, not resolved.** `auth.User.email` is not unique and
  the SSO gate can only pick one row (it now does so with an explicit `.order_by("id")`, so the choice
  is at least deterministic); guessing here would decide someone's access by accident.
- **`SchoolProfile.school` is re-pointed when the staff member's school changes**, but the `schools`
  M2M is deliberately left alone — adding without removing would make it a monotonic union, which is
  the wrong thing to inherit when multi-school scoping arrives.
- Grants are written to the admin log (`log_change`), so "who gave this person access, and when?" is
  answerable later.

> **Offboarding — deactivate, never delete.** To revoke access, set the `User` to inactive. Do **not**
> delete the `User` while leaving the `StaffMember`: provisioning would then see no account at all and
> mint a fresh one on the next save of that staff row. Deleting the **`StaffMember`** does not revoke
> anything either — the `User` and `SchoolProfile` survive and still pass the SSO gate — so
> `StaffMemberAdmin.delete_model` / `delete_queryset` warn and name the accounts to deactivate.
> `check_readiness` reports deactivated staff as INFO ("access deliberately revoked"), not as a
> blocker to fix.

**Non-superusers** can still add and edit staff, but never provision: `save_model` returns early with
an INFO message telling them who to ask, and `get_actions` removes the bulk action entirely rather
than offering a button that only 403s. `templates/403.html` renders permission denials inside the app
shell with the raising `PermissionDenied` message, instead of Django's bare unstyled page.

`annotate_login_state` / `login_state_label` drive the **"Can sign in?"** column and filter on the
staff changelist, via correlated `Exists`/`Count` subqueries so the cost is flat at any roll size. The
labels are module constants (`LABEL_*`) imported by the filter, so the two agree by construction.
Each names the **actionable** problem rather than the nearest one — in particular a staff member with
no school reads *"No — no school on this record"*, not *"no login account"*, because the latter sends
the administrator to the bulk action which then refuses. The join uses `Lower()` on both sides rather
than `email__iexact=OuterRef(...)` — `iexact` compiles to `LIKE`, and Django cannot escape a *column*
on the right-hand side, so an ordinary address like `first_last@school.uk` would act as a wildcard and
match the wrong user.

Bulk saves are grouped: `list_editable` makes Django call `save_model` once per changed row, so
outcomes are buffered on the request and `changelist_view` flushes one banner per distinct reason —
otherwise 200 edited rows meant 200 banners with the summary off-screen.


### Data model notes (`core/models.py`)

- `StaffMember` stores line/performance management relationships in one row: the staff member's
  `email` plus `line_manager_email` and `performance_manager_email`. These manager links are stored
  **as emails, not foreign keys**, so a staff row can be imported before the manager's own record
  exists. Emails are normalised to lower case in `save()`. `school` is an FK to `School`.
  `staff_type` (`TEACHING`/`SUPPORT`/`LEADER`/blank) selects which self-review form applies
  (`LEADER` = senior leaders, who get the Headteacher-Standards variant — see "Appraisals app").
  **There is no FK from `StaffMember` to the Django `User`** — they are linked by matching email
  (case-insensitive). This is the identity model the whole app relies on.
- `Branding` is a forced single-row table (always `pk=1`); the `branding` context processor exposes
  it to all templates.
- `SchoolProfile` doubles as the SSO authorization gate (see above).

## Appraisals app (`appraisals/`)

Digitises the school's annual appraisal: a teacher (or support-staff member) completes a self-review
and goals; their **coach** (= the `performance_manager_email` on their `StaffMember`) reviews and
signs off.

> **Client-facing terminology (important):** the feature is presented to users as **"Goal Setting and
> Review"** — every heading, nav label, page title, and Django-admin label (the latter via
> `AppraisalsConfig.verbose_name` and `Appraisal.Meta.verbose_name`). **All code deliberately keeps the
> term "appraisal"** — the `appraisals` app, the `Appraisal` model/table, URL names (`appraisals:…`),
> and the `appraisal-sub` CSS class. Change display *strings* only; never rename the identifiers. The
> client dislikes "appraisal" for political reasons but the code term is load-bearing, so the two are
> intentionally decoupled — don't "tidy" the code to match the UI wording.

> **Admin "Review at a glance":** `SelfReviewAdmin` renders a read-only Section / Criterion / Score /
> Evidence table (`render_self_review_table` in `admin.py`), because a self-review's 1-3 score lives on
> the child `SelfReviewBullet`, which Django's default inlines never surface next to the parent item's
> shared `evidence` field. The item inline/changelist also expose a `scores` column. The table is
> `mark_safe`, so staff-entered evidence/criterion text is HTML-escaped.

### Models (`appraisals/models.py`)
- `AcademicYear` — `start_year` (unique int, e.g. 2025 → "2025/26"); one row may be `is_current`
  (enforced single-current in `save()`). Drives the current/previous split.
- `Appraisal` — one per `(teacher, academic_year)`. FK `teacher` → `StaffMember`; **snapshots**
  `coach_email` at creation (stable if the manager later changes); `status`
  (DRAFT/SHARED/SIGNED_OFF) with an `is_locked` property (read-only once signed off); summary fields
  + the eligibility toggles and pay-award dropdown from the paper form. `previous()` returns the
  prior year's appraisal (drives the "Last Year" view); `seed_goals()` creates the 3 standard goals.
- `Goal` — one record carries **both** a goal's setup (title/steps/criteria) **and** its end-of-cycle
  review comments (teacher + coach), so the same goal is "This Year" when set and "Last Year" when
  reviewed. Types: STANDARDS/PERSONAL/LEADERSHIP. Goal 1's wording defaults to the module constant
  `DEFAULT_STANDARDS_GOAL`. Because one row spans two years, the review comments are editable from
  **two** places: the current appraisal's Goals tab (its own goals) and the Last Year tab (the
  *previous* appraisal's goals) — see "Reviewing last year's goals" below.
- `SelfReview` (OneToOne with `Appraisal`) seeds a two-level descriptor tree via `seed_items()`:
  `SelfReviewItem` is a TS-group/numbered-row container (`heading` + one shared `evidence` field per
  group) and `SelfReviewBullet` (FK `self_review_item`, `related_name="bullets"`) is one individually
  scorable descriptor statement within that group — `score` is `null` (Not Answered) or 1-3. The
  fixed Copleston descriptor content lives in `appraisals/self_review_templates.py`
  (`TEACHING_ITEMS`, `SUPPORT_ITEMS`, each a `(code, heading, bullets)` tuple); `seed_items()`
  bulk-creates both levels matching `kind`. **Seeding (`seed_goals`/`seed_items`) is called from
  views, never from `save()`.** Redesigned from a single per-group Yes/No `met` field to per-bullet
  scoring; the old `met`/`descriptor` fields were dropped by migration without attempting to map
  Yes/No answers onto per-bullet scores (there's no valid mapping) — see
  `appraisals/migrations/0005_backfill_selfreviewbullets.py`.
- `LeaderReview` (OneToOne with `Appraisal`, `related_name="leader_review"`) — the **senior-leader
  self-review variant**, seeded by `seed_standards()` from `appraisals/leader_standards_templates.py`:
  Section 1 from `ETHICS_CONTENT` (a `(heading, bullets)` list — the 3 Ethics/Professional-Conduct
  sub-sections) and Section 2 from `HEADTEACHER_STANDARDS` (a `(number, title, descriptors)` list — the
  10 standards). Deliberately a **separate model set** rather than a third `SelfReview.Kind`, because
  the shape differs: scoring is **per-standard** not per-bullet. `LeaderStandard` (FK `leader_review`,
  `related_name="standards"`) holds **both** section 1 and section 2 rows, distinguished by a `section`
  field (`ETHICS`/`STANDARDS`); each row carries its `score` (`null`/1-3, same scale as
  `SelfReviewBullet`), free-text `examples`, and a newline-joined `descriptors` snapshot (read-only
  prompts, exposed as `descriptor_list`). Standards rows additionally carry a `not_applicable` ("Not in
  job role") flag, rendered as a plain **tick box** (`LeaderStandardForm` overrides it as a
  `forms.BooleanField`; it is deliberately *not* in `segmented_fields`, since that conversion turns the
  initial into a choice string and the string `"false"` is truthy to a `BooleanField`). `save()` forces
  `score=None` when set (so an N/A standard is never scored regardless of the client-side greying).
  Ethics rows leave `not_applicable` at its default (the tick box isn't rendered for them). Uniqueness is `(leader_review, section, number)`; default ordering is
  `(section, order)` so Ethics sorts before Standards. `seed_standards()` guards **per row** (by
  `(section, number)`), so re-running back-fills any missing rows — e.g. adds the Ethics rows to a
  review that was seeded before Section 1 became scored. Seeding is called from views like the others.
  A `StaffMember` with `staff_type=LEADER` gets this variant **instead of** `SelfReview` — the two are
  never both created for one appraisal. (Section 1 was originally static read-only reference text and
  a "Section 3" free-form-goals model existed; both were dropped at the client's request — Section 1 is
  now scored, Section 3 removed — see migration `0008`.)

### UI & access control
- **Identity**: `appraisals/permissions.py` resolves the logged-in `User`'s role for an appraisal
  (teacher / coach / super / none) and exposes `get_appraisal_or_403` — **every** detail/save view
  routes through it to prevent IDOR. The `User`→`StaffMember` lookup itself comes from the shared
  `core.identity.current_staff_member`.
- **Owner vs viewer — which record's data is shown is never a function of who is
  looking.** `_build_section_forms` deliberately takes only `(appraisal, role)`: the
  viewer's *authority* arrives as `role`, and the record's *identity* comes from
  `appraisal.teacher`. It used to receive the viewer's `StaffMember` and branch
  `_is_leader()` on it, so a senior-leader coach opening a teacher's appraisal built the
  leader variant against that teacher's record — the coach saw a blank Headteacher-
  Standards form and the teacher's real self-review never rendered. The same mistake made
  `_ensure_self_review` snapshot `kind` from the viewer, which is the dangerous half:
  `kind` is stored and `seed_items()` no-ops once items exist, so a support-staff coach
  who opened a teaching coachee first seeded the *support* descriptor tree onto that
  teacher permanently and invisibly. Hence the parameters on that path are named `owner`,
  not `staff` (`staff` means the viewer everywhere else in this codebase — that naming is
  what produced the bug). `manage.py purge_misseeded_self_reviews` repairs the residue.
  `start_appraisal` / `my_appraisal` legitimately use the viewer, because there the viewer
  *is* the owner by construction.
  **Note the seeding still happens on GET**, so a coach's or superuser's page view creates
  the (now correct) review rows on someone else's record — a known wart, not yet fixed.
- **Field-level gating is the security boundary, not template hiding**: forms
  (`appraisals/forms.py`) set `field.disabled=True` for fields the current role may not edit (Django
  then ignores any submitted value), and disable everything when `is_locked`. Teacher-owned vs
  coach-owned fields are split there. The self-review `score` field (`SelfReviewBulletForm`) is
  teacher-only like every other teacher-owned field — the coach can view but never edit it.
- **Views** (`appraisals/views.py`, function-based, `@login_required`): one tabbed GET
  (`Self-review | Last Year | Goals | Summary`, panels rendered server-side so it works without JS)
  plus per-section POST save endpoints (Post/Redirect/Get). The self-review tab binds **two**
  formsets in one `<form>` — `SelfReviewItemFormSet` (evidence; inline formset off `SelfReview`,
  default prefix `items` derived from its FK's `related_name`) and `SelfReviewBulletFormSet` (score;
  a flat `modelformset_factory` explicitly bound with `prefix="bullets"`, since `SelfReviewBullet`'s
  parent is `SelfReviewItem` not `SelfReview` and `inlineformset_factory` only supports one FK hop).
  The view zips the two formsets into per-group row dicts for the template (same "build rows in
  Python" idiom as `team/views.py`). Boolean fields (and now the 4-state score field) render as
  segmented "pill" controls reusing the `.seg-pill` CSS pattern
  (`appraisals/templates/appraisals/_widgets/yesno.html` and its sibling `_widgets/score_pill.html`),
  not iOS switches. **The self-review tab has two variants**: `_build_section_forms` branches on
  `_is_leader(staff)` (i.e. `staff_type == LEADER`) and builds **either** the teaching/support
  item+bullet forms (above) **or** the leader forms — a single `LeaderStandardFormSet` (inline off
  `LeaderReview`, so no bullet second-hop) covering all 13 scored rows (3 Ethics + 10 Standards).
  `_tab_leader_review.html` iterates that one formset twice, filtering on `sform.instance.section` to
  render Section 1 (Ethics: score + examples, no N/A tick box) and Section 2 (Standards: N/A tick box
  + score + examples) separately. `_tab_self_review.html` switches on the `is_leader` context flag to include
  `_tab_leader_review.html`; `self_review_save` picks its `form_keys` dynamically via
  `_self_review_form_keys` (same endpoint, same teacher-only gate). The Standards N/A greying is
  progressive enhancement (`core/static/core/leader_standards.js`, loaded from `detail.html` only when
  `is_leader`).
- **Reviewing last year's goals (the Last Year tab)**: because one `Goal` row spans two years, the
  Last Year tab edits the **previous** appraisal's goals from **this** year's page. `LastYearGoalForm`
  exposes only the two review-comment fields (the goal's own wording is settled and rendered as
  read-only context); `LastYearGoalFormSet` is inline off `Appraisal` like `GoalFormSet` but bound to
  `appraisal.previous()` and given an explicit `prefix="lastyear"` — both are inline off `Appraisal`,
  so both would otherwise default to prefix `goals`, and every panel is rendered into the same page.
  **The gate is `_can_edit_last_year`, which checks the *current* appraisal's role and lock, not last
  year's** — a deliberate decision: last year's appraisal is normally already SIGNED_OFF (and so
  locked), and honouring that lock would make the review impossible to write. No new IDOR surface: the
  previous appraisal is always derived server-side via `appraisal.previous()`, never taken from the
  request, so `last_year_save` still routes through `get_appraisal_or_403` on the URL's pk. The gate
  also returns False when there is no previous appraisal, so posting to the endpoint 403s instead of
  crashing on a `None` formset.
- **Nav**: `appraisals/context_processors.py` exposes `user_is_coach` so `core/.../base.html` shows
  "My Team" (now the combined `team:my_team` page — see "Team app" below) for coaches **or** line
  managers. Mounted at `/appraisals/`.

### Operational prerequisites
Before anyone can start an appraisal: an `AcademicYear` must be marked `is_current`, the person needs
a matching Django `User` (same email, for SSO) and a `StaffMember` with `staff_type` set (`LEADER`
for the Headteacher-Standards variant).

### Goal review correction (one-off remediation)

`appraisals/goal_review_fix.py` holds the core logic for a **one-off data correction**, shared by two
front ends so the decision rules exist once: the `move_prior_year_goal_reviews` management command
and the **"Move misplaced goal reviews"** action on the `AcademicYear` admin changelist. Keep both
thin — all rules belong in the module.

**The fault it corrects.** The trust's first system was a PowerApps/SharePoint list with one row per
teacher per year, carrying **two** parallel blocks of review columns: `Review of Goal N` (the
performance manager's review of the *previous* year's goals, written each September) and
`Goal N Review` (the interim review of *that* year's goals, written from December onwards). The
original bulk import mapped the **first** block onto the goals of the year the row belonged to — so a
September 2025 review of the 2024/25 goals was stored on the **2025/26** `Goal` rows. Nothing is
structurally malformed, which is why this is **invisible in the Django admin**: a `Goal` with review
text looks entirely normal, and is wrong only in meaning. Don't go looking for a template bug.

**Shape: plan, then apply** — the same split `data_import` uses. Every decision is made once in
`build_plan()`; both the preview and the write consume that same plan, so a dry run cannot describe
something different from what runs. `restrict_to_approved()` narrows a rebuilt plan to the goals the
operator actually saw on the admin preview page, so a plan that changed between preview and confirm
cannot quietly widen.

**Safety rules (all load-bearing):**
- **Live coach work is never overwritten.** `edit_reason()` compares each goal's stored text against
  the `ImportRow.raw_json` that wrote it; a mismatch (`TEXT_DIFFERS`) or no import row at all
  (`NO_IMPORT_ROW`) means a human has been in there since, so the whole appraisal is flagged and left
  untouched. Re-checked inside the transaction under a row lock where the DB supports one. Note the
  limit of the proof: it shows the text still equals what the last recorded import wrote, **not** that
  no human ever edited it.
- The two fields being cleared are the same two the *following* year's "Last Year" tab writes into —
  which is why that guard is load-bearing rather than belt-and-braces.
- `check_years()` enforces adjacency, because `Appraisal.previous()` looks exactly one year back.
- Idempotent: once moved the source fields are blank, so a second run is a no-op — and `apply_plan()`
  returns early **before** `get_or_create` on the target `AcademicYear`, so a no-op run cannot
  fabricate an empty year.
- Appraisals created for the target year get `coach_email=""` (deliberately **not** copied — it would
  misattribute) and `status=SIGNED_OFF`. Goals with no recorded title get `PLACEHOLDER_TITLE`.

**Both admin entry points are superuser-gated explicitly.** The action carries
`permissions=["change"]` *and* an `is_superuser` check, because Django appends actions without
`allowed_permissions` unconditionally and `admin_view` only checks `is_active and is_staff` — so
gating on the `AcademicYear` model alone would have let any staff user with *view* permission run it
and download every staff member's review text. `start_next_year_view` carries the same check for the
same reason.

The record of a run is returned as a **JSON download** (counts and goal PKs, never comment text) and
is never written to the server filesystem; the command's `--backup-file` is required and refused
inside `BASE_DIR`. `docs/goal_review_correction_dpo_note.md` is the GDPR note for the exercise.

`scripts/sharepoint_to_goals_csv.py` is the companion **standalone** helper (not imported by the app):
it converts the legacy SharePoint export into a `goals.csv` carrying the *second* block — the interim
reviews that were never imported — for upload at `/import/`. It emits **only** the review columns, so
the import cannot touch titles/steps/criteria, and refuses to write inside the repo.

> **Order matters:** run the move **before** importing the interim reviews. Once the import runs,
> `raw_json` holds the new values and the guard above would treat the *correct* interim reviews as
> movable.

### Known follow-ups
- `appraisals/tests.py` covers: the `get_appraisal_or_403` role matrix (incl. a
  dedicated test for the coach-email *snapshot* vs. line-management's live lookup), self-review save
  permissions for the per-bullet scoring, `seed_items()` correctness (item/bullet counts computed
  from the template data itself), goals/summary field-level gating, IDOR across every tab and
  save endpoint, and the senior-leader variant (`seed_standards()` correctness for **both** the Ethics
  and Standards sections, LEADER→`LeaderReview` selection, the N/A-clears-score rule, and the leader
  save role matrix); plus the first-time self-classify flow (an unclassified staff member
  self-selecting Teaching/Support to start, LEADER never self-selectable, existing type never
  overwritten); plus the admin "Review at a glance" summary (`render_self_review_table`: score +
  evidence rendered together per criterion, and staff-entered HTML escaped); plus the Last Year goal
  review (the current-year-lock-not-last-year's rule in both directions, the teacher/coach field split,
  IDOR, and the no-previous-appraisal 403) and the "Not in job role" tick box (that unticking clears
  the flag — an unticked box submits no key at all — and that the widget really is a checkbox).
  `core/tests.py` covers the SSO auth gate and the `check_readiness` command — no app is now without a
  test suite. The goal-review correction has its own three classes:
  `MovePriorYearGoalReviewsTests` (the command + `build_plan`/`edit_reason` rules),
  `MoveGoalReviewsAdminActionTests` (the superuser gate, the approved-set pinning, the JSON record),
  and `ApplyPlanNoOpTests` (a no-op run must not fabricate an `AcademicYear` — a real bug this caught).
- Senior-leader open items (deferred, not blocking): there is no overall/average score roll-up across
  the standards yet (`not_applicable` is modelled so an N/A-excluding average can be added later); and
  a leader still sees the appraisal's fixed **Goals** tab — hiding it for leaders is a possible
  follow-up.

## Line management app (`line_management/`)

Digitises recurring 1:1 line-management meetings: a **line manager** (= the `line_manager_email` on
a person's `StaffMember`) records structured notes for the people they line-manage; the managed
person (the **report**) views their own records read-only. Mounted at `/line-management/`. Mirrors
the appraisals app's patterns (email identity, role-gated form, `get_*_or_403` IDOR chokepoint,
`reflection-card` styling, a context processor driving a conditional nav link) but is deliberately
simpler — one editor, no status/lock, no field split.

### Model (`line_management/models.py`)
- `LineMeeting` — **many per staff member** (one per meeting, not one-per-year like `Appraisal`).
  FK `staff` → `StaffMember` (`PROTECT`); `meeting_date`; the five note sections
  (`actions_from_last_meeting`, `upcoming`, `rotation_update`, `main_matters`,
  `actions_from_meeting`); `created_by_email`. Ordered newest-first with a composite index on
  `(staff, -meeting_date)`. The single rotation field carries the R1/R2/R3 guidance from the module
  constant `ROTATION_GUIDANCE` (surfaced as helper text in the template).
- **`created_by_email` is provenance/display only** — it records who actually wrote each meeting
  (stamped server-side from the acting user, lowercased in `save()`) and is **never** used for
  authorization. Every view surfaces it so an inherited note stays attributed to its real author.
- `NOTE_FIELDS` lists the five note-section field names; the `is_empty` property is true when all of
  them are blank/whitespace-only (i.e. the record holds only a date). Used by the create flow's
  empty-save guard and by `purge_empty_line_meetings` (see "Views & nav" and "Known follow-ups").

### Access control — a LIVE lookup, not a snapshot (the key design difference from appraisals)
- `line_management/permissions.py` resolves the viewer's role (super / manager / report / none) via
  `meeting_role`, exposes `get_meeting_or_403` (detail/save chokepoint) and
  `get_managed_staff_or_403` (manager-only list/create chokepoint). `current_staff_member` is the
  shared `core.identity` helper.
- **"Manager" is recomputed every request** from the staff member's *current* `line_manager_email`,
  not snapshotted (contrast `Appraisal.coach_email`). Consequence — confirmed as a deliberate
  governance decision: when a person changes line manager, the **successor inherits read+edit of the
  whole history** and the previous manager loses access. The single comparison rule lives in
  `is_current_line_manager(member, staff)` (case-insensitive) so it can never drift between the two
  chokepoints.
- **Only the current line manager (or super) edits**; the report is read-only. `LineMeetingForm`
  takes `can_edit=` and sets `field.disabled=True` on every field when False (the real security
  boundary), and `meeting_save` re-checks `can_edit_meeting` and 403s otherwise. There is no
  status/lock — the line manager can always edit.

### Views & nav
- **Views** (`line_management/views.py`, function-based, `@login_required`, P/R/G): `my_meetings`
  renders two sections — the viewer's own records (read-only, `staff == viewer`) and
  `hosted_meetings` (meetings for everyone the viewer **currently** line-manages, via
  `line_managed_staff`, the same live lookup the access rule uses, so nothing shown is a dead link);
  `staff_meetings` (one report's meetings + "New meeting"); `meeting_new` (GET: renders a blank form,
  **persists nothing**); `meeting_create` (POST: create-on-save — refuses to save a record with no
  note content via `LineMeeting.is_empty`, so abandoning the form leaves no record); `meeting_detail`,
  `meeting_save` (POST).
- **Nav**: `line_management/context_processors.py` exposes `user_is_line_manager` so
  `core/.../base.html` shows "My Reports" only to line managers; "My Line Meetings" shows for
  everyone.

### Operational prerequisites
The report needs a matching Django `User` (same email, for SSO) and a `StaffMember`; their
`line_manager_email` must point at the manager's email for the manager to gain access.

### Known follow-ups
- `line_management/tests.py` has a 34-test suite covering the role matrix (report/manager/super/
  stranger), the manager-change inheritance rule, case-insensitive email matching, the two-section
  `my_meetings` view, the create-on-save / empty-save-guard flow, and `is_empty` / the purge command.
  `appraisals/tests.py` has its own suite (see "Appraisals app" → "Known follow-ups" above);
  `core/tests.py` covers the SSO auth gate and the `check_readiness` audit command.
- Any pre-existing blank records from the old "create-then-fill" flow can be cleared with
  `manage.py purge_empty_line_meetings` (`--dry-run` to preview first).
- `_messages.html` / `no_staff.html` are now duplicated across `appraisals/`, `line_management/`,
  and `team/` (plus an inlined copy in `templates/account/login.html`) — still pending promotion into
  `core/templates/`.

## Team app (`team/`)

A single read-only "My Team" page (`team/views.py` `my_team`, mounted at `/team/`) that lists every
person the signed-in user manages — the union of who they **performance-manage** (coach, via
`appraisals.permissions.coached_staff`) and who they **line-manage** (via
`line_management.permissions.line_managed_staff`), each person shown once with their role(s) and a
role-appropriate "Open" link into the existing appraisal/meeting views.

**Owns no models and adds no new access path.** It composes the two feature apps' already-gated query
helpers rather than re-deriving the email-matching rules, and the per-row links point at views that
keep their own `get_*_or_403` chokepoints. Nav: shown when `appraisals.user_is_coach` **or**
`line_management.user_is_line_manager` is true (see both apps' context processors).

## Overview app (`overview/`)

Superuser-only, trust-wide dashboards (`overview/views.py`, mounted at `/overview/`): an appraisal
status page and a line-management engagement page. **Owns no models**; like `team/`, it composes the
feature apps rather than re-deriving permission logic — a single `_require_superuser` gate (403
otherwise) is the only access check these views need, since each is a flat read across *every*
`StaffMember`. `classify()` / `classify_line()` are pure functions mapping a staff member (+ their
current appraisal / meeting count) to a status bucket, kept separate from the views so they're
unit-testable. A shared school/email filter (`overview/_filters.html`) narrows the *view*; the
underlying scope is deliberately trust-wide, including staff with no appraisal, no line manager, or no
login account, because surfacing who has **not** engaged is the point. The per-row "Open" links reuse
the appraisals/line-management detail views' own `get_*_or_403` chokepoints (which treat a superuser
as `ROLE_SUPER`), so these pages add no new read path.

## Bulk import app (`data_import/`)

Superuser-only CSV migration tool, mounted at `/import/`: lets an administrator bulk-load
`StaffMember` rows and historical PD data (`Appraisal` summaries, `Goal`s, self-review
scores/evidence, `LineMeeting`s) via five separate CSV uploads, each going through an
**upload → preview → confirm** flow so nothing is written until a superuser reviews exactly what
will change. The exact column contract for each of the five CSVs is in `docs/import_templates.md`.

**The importer creates `StaffMember` rows, not login accounts.** Because identity is by email with
no FK between `StaffMember` and `User` (see "Auth & authorization"), imported staff cannot sign in
until each also has a matching Django `User` + `SchoolProfile`. The required post-import step is to
run the rules in `core/provisioning.py` over the imported rows — **the importer itself deliberately
does not provision**, so a CSV upload can never silently mint hundreds of live logins. Two ways:

- **In the admin (usual):** select the imported staff on the `StaffMember` changelist and run
  **"Give selected staff a login"**. Filter by *Can sign in? → Anyone who cannot sign in* first to see
  exactly who needs it. Superuser-only. Confirming a **staff** import says this on screen, with a link
  to that filtered changelist — otherwise "Import confirmed" reads as job-done while nobody imported
  can actually sign in, which is the same silent dead end this feature exists to remove.
- **On the server (bulk/scriptable):** `manage.py provision_users` (idempotent, `--dry-run` to
  preview). Better than the admin action above a few hundred rows, which the action says so itself.

Either way a `User` is created (username/email = the staff email, unusable local password since auth
is SSO-only) plus a `SchoolProfile` from `StaffMember.school`, for every `StaffMember` lacking one —
skipping-and-reporting anyone with no `school`, never touching superusers, and never reactivating a
deactivated leaver. The command must be run against the same database the import went into (i.e. the
Azure DB for a production import, not local SQLite). No per-person permission setup follows: once
`User` + `SchoolProfile` exist, the imported `line_manager_email` / `performance_manager_email`
relationships drive all view/edit rights.

This is the one app that **does** own models for a purely administrative reason — `overview/` and
`team/` deliberately own none, but an import audit trail (what was uploaded, what it would do, what
it actually did) has to be persisted somewhere, and bolting it onto `overview/` would break that
app's "read-only, no models" invariant. `ImportBatch` (one per upload, status
PENDING/CONFIRMED/DISCARDED) and `ImportRow` (one per parsed CSV row, outcome
CREATE/UPDATE/SKIP, the raw parsed row as JSON, and — once confirmed — which object it
created/updated) are append-only: nothing is ever deleted, so the audit trail is permanent. Access is
gated by `permissions.require_importer` (today identical to `overview`'s `_require_superuser`, but
named for *what* it gates so a future narrower "data admin" role only changes one function).

`services.py` holds all the parse/validate/apply logic (the first `services.py` in this codebase —
justified here by five interdependent models and multi-step apply logic, not introduced casually).
Four of the five import types upsert on a real model uniqueness constraint (teacher+year for
`Appraisal`, item-code+order for `SelfReviewItem`/`SelfReviewBullet`, etc.), which makes re-running
a batch naturally idempotent. `LineMeeting` has no such constraint (multiple genuine meetings can
share a staff+date), so its dedupe instead hashes each row's natural fields
(`ImportRow.source_row_hash`) and checks for a match against **every previous batch** of that import
type, not just the current one — re-uploading the same export as a fresh batch updates the
previously-created meeting instead of duplicating it.

Self-review import is the trickiest case: one CSV row is one scorable bullet (`item_code` +
`bullet_order`), and `confirm_batch` groups rows by `(teacher_email, academic_year)` so
`SelfReview.seed_items()` runs exactly once per group (its own `transaction.atomic()` step) before
any bullet in that group is applied — never per-row, since `seed_items()` bulk-creates the whole
item+bullet tree in one shot and must not be interleaved with per-bullet updates. `evidence` is a
shared per-item field on the real form, so it's only read from the row where `bullet_order == 1` for
a given `item_code`.

Every `apply_*` function runs inside its own `transaction.atomic()` block scoped to one logical unit
of work (one row, or one self-review group's seed step) — never the whole file — so one bad row
can't roll back hundreds of good ones. `confirm_batch` also **re-validates each row immediately
before applying it**, not just at upload time, since real time passes between preview and confirm; a
row that resolved fine at upload but fails at confirm (e.g. its `AcademicYear` was deleted in the
interim) is recorded as a fresh `SKIP` with an error message rather than raising.

### The one destructive option: `clear_blank_fields`

The standing rule everywhere else is **"a blank CSV cell never overwrites"** (`_set_if_present`).
`ImportBatch.clear_blank_fields` is the single opt-in exception, offered on the **goals upload only**
and applied to **only** the two review-comment fields — never `title` / `steps_to_success` /
`success_criteria`. It exists so a bad import's comments can be *erased*, not just overwritten.

Three things hold it safe, and each is separately tested:
- The form field is **popped, not hidden**, for every other import type (`CsvUploadForm.__init__`), so
  a hand-crafted POST cannot switch on destructive behaviour where the form never offered it.
- The flag is read from the **stored batch**, not the request, at apply time.
- `clearing_preview()` names on the preview page exactly which goals will genuinely lose text, so the
  warning states a scale rather than a principle.

`upload.html` must render the checkbox behind the view's `allow_clear_blanks` context flag — an
explicit flag, not a truthiness test on the bound field, since the field is absent from the form
entirely for other types. This regressed once: the template rendered only `{{ form.csv_file }}`, so
the option was unreachable and every goals import silently ran with blanks-leave-alone. There is now a
test asserting the checkbox reaches the **rendered page**, not just `form.fields`.

**Ragged-row caveat:** a short CSV row is padded to empty by the reader, so a missing trailing column
counts as present-and-empty and *will* clear. See `docs/import_templates.md`.

### Known follow-ups
- `data_import/tests.py` covers the superuser-only access gate, the create/update/skip-and-report
  behaviour per import type, the self-review seed-once-per-group + evidence-on-bullet-order-1 rules,
  and the cross-batch `LineMeeting` dedupe (the central idempotency guarantee of this feature).

## Data safety — invariants that must not be regressed

The client's assurance is that **no staff-entered text is ever silently lost**. These rules are load-
bearing for that claim; each was written to close a defect that had actually shipped.

- **Client-side helpers MEASURE, they never MUTATE.** `core/static/core/word_limit.js` shows a word
  counter and nothing else. It used to call an `enforce()` helper that rewrote `textarea.value` — and
  did so once at page load, before the user had touched anything — so opening a record longer than the
  limit truncated it on screen, and the next save persisted the deletion under a green "saved" message.
  It was destructive twice over: the rebuild used `words.join(" ")`, flattening every paragraph break
  in the *surviving* text. Setting `.value` in script fires no `input` event, so `unsaved_changes.js`
  never saw it and never warned. **Never reintroduce a client-side truncation.** The limit is guidance;
  every narrative field is an unbounded `TextField` and there is no server-side cap to match.
  (The cap was 300 words from the initial commit until 2026-08-28, applied to every appraisal textarea —
  only `line_management` opted out via `data-max-words="0"` — so historic imported text may already
  have been truncated in production during that window.)
- **Every form's errors must reach the page.** Include `core/_error_summary.html` (a single form) or
  `core/_formset_errors.html` (a formset) at the top of each `<form>`. `_tab_summary.html` once rendered
  no error output at all and `_tab_self_review.html` never rendered `self_review_form`'s errors, so a
  `signed_name` over its 200-char limit failed the entire save behind "Please correct the errors below."
  with nothing below it — and because `_save_section` requires *all* target forms to validate before
  saving *any*, a whole self-review went unsaved with no visible cause. Wire the summary include rather
  than per-field `{% if %}`s, so a field added later cannot fail invisibly.
- **A refused save hands the text back; it does not discard it.** `_save_section` distinguishes "you
  were never allowed to edit this" (403) from "this was signed off while you were typing" (409 +
  `core/recovery.py` → `save_blocked.html`). Each gate in `appraisals/permissions.py` carries its
  role-only half as `.role_only` so the two reasons stay distinguishable. Note **re-rendering the bound
  page does not work** for this: once locked, every field is built `disabled`, and Django reads a
  disabled field from its initial, so the user's words would be replaced by the stored ones. Only the
  raw POST is echoed — never anything read from the record — which is what makes it safe to show to
  someone who has just lost access.
  > `line_management.meeting_save` deliberately does **not** do this. There, losing the line-management
  > link removes the viewer's role entirely, which is indistinguishable from an IDOR probe, and
  > `ManagerChangeInheritanceTests` fixes the rule that the outgoing manager loses access *even as the
  > record's author*. Answering a probe with anything but a 403 is the worse trade.
- **One save is one transaction.** `_save_section` wraps its formset saves in `transaction.atomic()`.
  The self-review tab writes three separate formsets; without this, a failure part-way committed the
  evidence but not the scores, and the page gave the user no way to tell what had landed.
- **The deploy fails loudly.** `startup.sh` lets `migrate` and `collectstatic` failures abort the boot.
  They used to be `|| echo "(continuing)"`, which defeated `set -e` on purpose: the GitHub Actions run
  reports on package upload rather than boot, so a deploy went green while the app served a stale schema
  and every save 500'd. `collectstatic` is the more dangerous of the two — `DEBUG=0` selects
  `CompressedManifestStaticFilesStorage` and `staticfiles/` is gitignored, so a missing manifest takes
  the whole site down while still answering the port, passing Azure's warm-up probe. Only the seeds stay
  tolerant, since the app is usable without them.
- **Sessions refresh on activity.** `SESSION_SAVE_EVERY_REQUEST = True` with a 12-hour
  `SESSION_COOKIE_AGE`. Django's default expiry is measured from *login* and never extended, so a user
  was logged out mid-sentence; the POST body was then discarded by `@login_required`, and
  `unsaved_changes.js` could not warn because it stands down on submit.
- **Deleting staff text in the admin is superuser-only** (`core/admin_mixins.SuperuserOnlyDeleteMixin`).
  One `Appraisal` delete cascades its goals, self-review, items, per-bullet scores, leader review and
  standards. The mixin also drops `delete_selected`, which Django offers independently of
  `has_delete_permission`.
- **A blank import cell never overwrites** — including `coach_email`, which used to be the one field in
  `apply_appraisal_summary_row` bypassing `_set_if_present`. Re-importing with that column blank *or
  absent* rewrote every matched appraisal's coach snapshot to the staff member's current performance
  manager, silently moving edit rights. The create-time fallback now lives in `create_defaults`.
- **The one destructive import option remains `clear_blank_fields`** — goals upload only, two review-
  comment fields only. See "The one destructive option" above.

**Known remaining gap (not fixed, deliberately):** saves are last-write-wins with no version check, so
one person with the same record open in two browser tabs can overwrite themselves. Cross-*role*
clobbering is already prevented — fields the poster may not edit are `disabled`, and Django then reads
them from the freshly-loaded instance rather than the stale POST. Closing the remaining case properly
needs an `updated_at` on `Goal` / `SelfReviewItem` / `SelfReviewBullet` (none have one) plus per-row
version checks threaded through three formsets, which is a real change to the save paths that currently
work correctly.

**Backup is infrastructure, not code.** Nothing in this repo creates or verifies one. See
"Backup and restore" in AZURE_DEPLOYMENT.md — it must be confirmed on the Azure server, and a restore
must actually have been tested, before anyone tells the client the data is safe.

## Configuration & deployment

- `settings.py` reads everything from environment variables (loaded from a local `.env` in dev via
  python-dotenv; absent on Azure). When `DEBUG=0` (production) it **hard-fails** at import if
  `SECRET_KEY` or `DATABASE_URL` is missing — a misconfigured deploy crashes loudly rather than
  running insecurely. Local dev uses SQLite; production requires Postgres.
- Azure host/CSRF handling derives from the `WEBSITE_HOSTNAME` env var that Azure injects.
- **No CSV is tracked** except the blank column templates under `docs/import_templates/`
  (`.gitignore`: `*.csv` with a negation). Exports and generated import files hold named staff
  performance commentary, and a commit here **is** a production deploy.
- `requirements.txt` gates `gunicorn` and `psycopg[binary]` to non-Windows so local Windows installs
  stay clean while Azure Linux gets the production server + Postgres driver.
- Full Azure deployment procedure (App Settings, startup command, Postgres vs SQLite rationale,
  SSO redirect URIs) is in `AZURE_DEPLOYMENT.md`. Media is served either from Azure Blob
  (`USE_AZURE_MEDIA_STORAGE=1`) or as WhiteNoise static (`MEDIA_AS_STATIC=1`).
- The repo is published to GitHub (`github.com/philchurch77/lmpm`) and **deployed live to Azure**
  (App Service `lmpm`). `.github/workflows/azure-deploy.yml` triggers on every push to `main` and
  deploys automatically via the `AZURE_WEBAPP_PUBLISH_PROFILE` secret — so `git push` to `main` is a
  live deploy. Wait for the GitHub **Actions** run to go green before relying on the new code being
  on the server.
- **Running management commands on the live server (Azure SSH):** the deploy is an **Oryx compressed
  build** — `/home/site/wwwroot` holds only `output.tar.zst` (+ `oryx-manifest.toml`,
  `requirements.txt`, `hostingstart.html`), **not** the app code. At startup the package is extracted
  to a temp dir and run from there, so `manage.py` lives under `/tmp/<hash>/`, not in `wwwroot`. To
  run a command (e.g. `provision_users` after a bulk import): open App Service → Development Tools →
  SSH, then `find / -name manage.py 2>/dev/null` to locate the extracted app root (or `which python`,
  whose `antenv` parent is that root), `cd` there, and run `python manage.py <command>`. The
  production `DATABASE_URL` is already in the process env, so the command hits the live Postgres. The
  `/tmp/<hash>` path is regenerated on every deploy/restart — locate it fresh each time, never
  hard-code it.
