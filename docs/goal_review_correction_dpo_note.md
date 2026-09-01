# Data correction: misplaced goal review comments

**Status:** awaiting DPO sign-off before the correction is run
**System:** LMPM (Goal Setting and Review), Copleston High School / OXLIP trust
**Prepared:** 1 September 2026

This note records a one-off correction to staff performance data, so that the
decision, the rationale and the safeguards are documented *before* the change
rather than reconstructed afterwards. It exists to satisfy UK GDPR Art 5(2)
(accountability) and to give a defensible answer if a member of staff makes a
subject access request.

---

## 1. What is wrong

The trust's first performance-management system was a PowerApps app backed by a
SharePoint list, holding one row per teacher per academic year. Each row carried
**two separate blocks** of goal review columns:

| Block | Columns | What it holds |
|---|---|---|
| A | `Review of Goal 1/2/3` and `Review of Goal N Teacher Comments` | The performance manager's review of the **previous** year's goals, written at the start of the year |
| B | `Goal 1/2/3 Review` and `Goal N Teacher Review` | The **interim** review of *that* row's own goals, written from December onwards |

When the 2025/26 data was bulk-loaded into LMPM, **Block A was mapped onto the
2025/26 goals.** The result is that a review written in September 2025 about the
**2024/25** goals is stored against the **2025/26** goals it does not describe.

Nothing is structurally malformed, which is why the error is invisible in the
Django admin — a goal with review text looks entirely normal. It is wrong only
in meaning. It was identified by reading the text: the comments do not describe
the goals they are attached to, and their tense places them at the start of
2025/26 rather than the end.

**Effect on individuals:** a member of staff opening their 2026/27 record sees,
under "Review — coach comments" for last year's goals, commentary about goals
from two years earlier. It is their own genuine performance data, but
misattributed to the wrong objectives and the wrong year.

## 2. What the correction does

Two steps, both run by a superuser through the browser:

1. **Move.** The Block A comments are moved from the 2025/26 goals onto 2024/25
   goals — the year they actually review. This requires creating a 2024/25
   academic year and a 2024/25 appraisal record for each affected member of
   staff (see §4).
2. **Replace.** A corrected CSV, generated from the same SharePoint export, is
   imported so the 2025/26 goals carry **Block B** — the interim reviews that
   genuinely describe them, and which were never loaded. Where no interim review
   exists, the field is left empty for the coach to complete.

## 3. Lawful basis and proportionality

- **Basis:** the same basis as the original processing — performance management
  of employees under the employment contract, and the trust's legitimate
  interests in maintaining accurate staff records.
- **No new data is collected.** Every value involved already exists in LMPM or
  in the SharePoint list the trust already holds.
- **No new recipients.** The correction does not widen who can see any comment.
  Access remains governed by the existing rules: a member of staff sees their
  own record; their named performance manager sees theirs. Both the moved and
  the replacement records sit behind the same permission checks.
- **Art 5(1)(d) — accuracy.** This correction exists *because of* the accuracy
  principle. Leaving performance commentary attached to the wrong year's
  objectives is the less compliant option.

## 4. The decision that needs sign-off

**Creating 2024/25 appraisal records for a year in which no appraisal took place
in this system.**

LMPM was built at the end of 2024/25, so it holds no 2024/25 goals. To give the
moved reviews a home, the correction creates a 2024/25 appraisal per affected
person, with goal rows titled *"Goal not recorded — this year predates the
system."*

**The argument for:** the review text is genuine, it genuinely describes 2024/25
goals, and the placeholder is honest — it states the goal was not recorded
rather than leaving a blank that implies data was lost. This is more accurate
than the status quo.

**The argument against:** it creates a record of a "Goal Setting and Review" that
did not happen in this system. A member of staff making a subject access request
will be shown a 2024/25 record they never took part in, and will need it
explained.

**Mitigations applied:**

- The 2024/25 goal titles say plainly that the goal was not recorded.
- The record is created as **signed off**, so it is read-only and cannot be
  mistaken for a live appraisal.
- `coach_email` is left **blank** on the created record rather than copied from
  the 2025/26 appraisal. Copying it would assert that the later year's
  performance manager wrote the earlier year's review, which may not be true —
  an accuracy risk under Art 5(1)(d).
- This note is the written rationale, available if a SAR is received.

> **DPO decision required:** approve the creation of 2024/25 records on the
> above terms, or direct that the Block A comments be left in SharePoint only
> and cleared from LMPM.
>
> Signed: ............................ Date: ....................

## 5. Safeguards on the correction itself

- **Work already done by coaches is never overwritten.** Each goal's stored text
  is compared against the audit trail of what the import actually wrote
  (`ImportRow.raw_json`). If it differs — or if the goal was created in the app
  rather than imported — the whole appraisal is skipped and listed for a human
  to resolve. The check is repeated inside the database transaction, under a row
  lock, in case a coach saves mid-run.
- **Scope is limited to the 2025/26 records.** The current year's in-progress
  appraisals are never read or written.
- **Preview before write.** The operator sees exactly which staff and which
  goals will change, and confirms before anything is written.
- **A before/after record** of every value moved is produced at the point of
  confirmation and downloaded to the operator. It is *not* written to the
  server, deliberately: it contains named performance commentary and the Azure
  working directory is not a controlled location.
- **Reversibility.** A database backup is taken before the run, and the source
  data remains in the SharePoint list, which is unchanged throughout.

## 6. Retention of the extract

The before/after record is a bulk extract of staff performance commentary and is
a processing activity in its own right.

| | |
|---|---|
| **Held by** | the operator running the correction |
| **Location** | to be a controlled trust location, not personal storage, not the repository |
| **Retention** | until the correction is verified, and **no longer than 3 months** |
| **Deletion owner** | ......................................... |
| **Deletion due** | ......................................... |

## 7. Sensitive content

Appraisal free text routinely refers to health, absence, caring responsibilities
or disability. Spot-checking the source data confirms such references are
present (for example, commentary on long-term absence and on staff health
affecting targets). Where present, this is **Article 9 special category data**
and the extract in §6 must be handled accordingly. This is the main reason the
record is downloaded to a controlled location rather than written to the
application server or echoed to a console.

## 8. Record of the run

To be completed after the correction:

| | |
|---|---|
| Date and time run | |
| Run by | |
| Staff records moved | |
| Records skipped for manual review | |
| Before/after record filename | |
| Verified by | |
