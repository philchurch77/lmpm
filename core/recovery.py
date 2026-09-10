"""Handing a user's typed text back when a save is refused after the fact.

The problem this solves: a coach presses "sign off" (or a line manager is
repointed) while the other party is part-way through writing. The POST then
fails a gate that passed when the page was loaded, ``PermissionDenied`` is
raised *before* anything is re-rendered, and everything the user typed is
discarded behind a 403 that reads as though they never had access.

Re-rendering the normal bound page does not fix it. Once the appraisal locks,
every form field is built with ``disabled=True``, and Django reads a disabled
field from its initial rather than from the submitted data — so the bound
re-render would show the *stored* text and still lose the user's.

So instead we echo back the raw submitted values, and only those. Nothing from
the record is read or shown here, which matters because the person we are
rendering for may be exactly the person who just lost access to that record:
their own keystrokes are theirs to see, the record's current contents are not.
"""
from __future__ import annotations

from django.shortcuts import render

# Posted keys that carry no user-typed prose: CSRF, formset bookkeeping, and the
# hidden primary keys that formsets round-trip.
_SKIP_EXACT = {"csrfmiddlewaretoken"}
_SKIP_SUFFIXES = (
    "-TOTAL_FORMS",
    "-INITIAL_FORMS",
    "-MIN_NUM_FORMS",
    "-MAX_NUM_FORMS",
    "-id",
    "-DELETE",
)

# Everything non-empty is shown. An earlier version skipped values shorter than
# two characters, on the theory that they were dates and radio choices rather
# than prose — but every per-bullet self-review score posts as a single
# character, so a teacher who had worked through forty descriptors got a page
# listing their evidence, headed "Nothing you typed has been thrown away", with
# every score missing. On the one page whose entire job is to be truthful about
# what was lost, a filter that quietly drops real answers is worse than clutter.


def _humanise(key: str) -> str:
    """Turn a form key like ``items-3-evidence`` into ``Evidence (row 4)``."""
    parts = key.split("-")
    field = parts[-1].replace("_", " ").capitalize()
    for part in parts:
        if part.isdigit():
            return f"{field} (row {int(part) + 1})"
    return field


def submitted_text(post) -> list[tuple[str, str]]:
    """The user's typed prose from a POST, as ``(label, value)`` pairs.

    Ordering follows the form, so the result reads down the page the way the
    user wrote it.
    """
    out = []
    for key in post:
        if key in _SKIP_EXACT or key.endswith(_SKIP_SUFFIXES):
            continue
        value = (post.get(key) or "").strip()
        if not value:
            continue
        out.append((_humanise(key), value))
    return out


def render_save_blocked(request, *, heading, explanation, back_url, back_label):
    """Render the user's submitted text back to them after a refused save."""
    return render(
        request,
        "core/save_blocked.html",
        {
            "heading": heading,
            "explanation": explanation,
            "submitted": submitted_text(request.POST),
            "back_url": back_url,
            "back_label": back_label,
        },
        status=409,  # Conflict: the record changed under them, they are not forbidden.
    )
