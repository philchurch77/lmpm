# How to change a coaching or line-management relationship

**Audience:** LMPM administrators (Django admin access required)

Who coaches whom, and who line-manages whom, is stored on the **staff member's own
record** — there is no separate "relationships" screen. Both are plain email
fields on `StaffMember`:

| Relationship | Field on the staff record | Gives access to |
|---|---|---|
| Coach (performance manager) | **Performance manager email** | Goal Setting and Review |
| Line manager | **Line manager email** | Line meetings |

The two are independent — changing one does not change the other.

---

## Changing a line manager (the simple case)

1. Django admin → **Core → Staff members**
2. Search for the person's email address, open their record
3. Set **Line manager email** to the new manager's address
4. Save

That is the whole job. Line-management access is looked up **live** on every
page view, so the change takes effect immediately.

**Be aware:** the new line manager inherits **read and edit access to the whole
meeting history**, including notes written by the previous manager, and the
previous manager loses access at the same moment. Each meeting still displays
who actually wrote it, so nothing is misattributed — but the history does move
across. This is deliberate; raise it with the people involved if the handover is
a sensitive one.

---

## Changing a coach (two steps, not one)

### Step 1 — the staff record

1. Django admin → **Core → Staff members**
2. Search for the person's email address, open their record
3. Set **Performance manager email** to the new coach's address
4. Save

This controls who appears on the new coach's **My Team** page, and who is given
the coach role on any goal-setting record created **from this point on**.

### Step 2 — the current year's record (if one already exists)

**Step 1 on its own does not move a record that has already been started.** Each
year's record stores the coach's email at the moment it was created, and access
is checked against that stored value. So after step 1 alone, the *old* coach
still has access and the new coach gets "You do not have access to this goal
setting and review."

To move a live record:

1. Django admin → **Goal Setting and Review → Goal Setting and Reviews**
2. Search for the teacher's email address, open the row for the current year
3. Set **Coach email** to the new coach's address
4. Save

Repeat for any earlier year the new coach also needs to see. If you only want
them to have this year, only change this year.

### Why it works this way

It is deliberate, not a bug. A completed record should keep showing who actually
did the review and sign-off, rather than silently re-attributing itself to
someone new months or years later. The trade-off is this second manual step when
a change genuinely happens mid-cycle.

---

## Things that catch people out

**The record is locked.** If its status is **Signed off** it is read-only for
everyone, so the new coach will be able to open it but not type anything. If
they still need to complete it, set the status back to **Shared** on the same
admin screen.

**The new coach has no record of their own.** A coach needs their own **Staff
member** row *and* a matching login account (same email address) before any of
this works. If they are brand new to the system, they will also need
`provision_users` running to create the login — ask whoever manages the server.

**Capitals and spaces don't matter.** Email addresses are tidied and lower-cased
automatically when saved.

**Check it worked.** Ask the new coach to open **My Team** — the person should
be listed, with an "Open" link that works.

---

## Changing several at once

If a reshuffle affects more than a handful of people, upload a staff CSV at
`/import/` instead of editing one record at a time. It only needs two columns:

```
email,performance_manager_email
```

A blank cell never overwrites an existing value, so the file can contain **only**
the people who have changed, and only the columns you are changing. The upload
shows you a preview of exactly what will change before anything is written.

One thing the CSV cannot do is **empty** a field — a blank cell is always
treated as "leave alone". To remove a manager entirely, edit the staff record
directly.

Note that this updates staff records only — it is the equivalent of step 1. Any
records already started for the current year still need the step 2 edit above.
