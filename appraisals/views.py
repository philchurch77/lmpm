"""Views for the appraisal UI.

All views are function-based and require login. Access to a specific appraisal
always routes through ``get_appraisal_or_403`` to prevent IDOR, and field-level
editing is gated by role inside the forms.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import StaffMember

from .forms import (
    AppraisalSummaryForm,
    GoalFormSet,
    LeaderStandardFormSet,
    SelfReviewBulletFormSet,
    SelfReviewForm,
    SelfReviewItemFormSet,
)
from .models import AcademicYear, Appraisal, LeaderReview, SelfReview, SelfReviewBullet
from .self_review_templates import UPR_DECLARATION_TEXT
from core.identity import current_staff_member

from .permissions import (
    can_edit_coach_fields,
    can_edit_teacher_fields,
    get_appraisal_or_403,
)

TABS = ("self-review", "last-year", "goals", "summary")


def _can_edit_either(appraisal, role):
    return can_edit_teacher_fields(appraisal, role) or can_edit_coach_fields(
        appraisal, role
    )


def _is_leader(staff):
    """True when the staff member takes the senior-leader self-review variant."""
    return bool(staff) and staff.staff_type == StaffMember.StaffType.LEADER


def _ensure_self_review(appraisal, staff):
    """Get or create the appraisal's self-review and seed its items."""
    self_review = getattr(appraisal, "self_review", None)
    if self_review is None:
        kind = (
            SelfReview.Kind.SUPPORT
            if staff and staff.staff_type == StaffMember.StaffType.SUPPORT
            else SelfReview.Kind.TEACHING
        )
        self_review = SelfReview.objects.create(appraisal=appraisal, kind=kind)
    self_review.seed_items()
    return self_review


def _ensure_leader_review(appraisal):
    """Get or create the appraisal's leader self-review and seed its standards."""
    leader_review = getattr(appraisal, "leader_review", None)
    if leader_review is None:
        leader_review = LeaderReview.objects.create(appraisal=appraisal)
    leader_review.seed_standards()
    return leader_review


@login_required
def my_appraisal(request):
    """Redirect to the signed-in user's current-year appraisal, or an empty state."""
    staff = current_staff_member(request)
    if staff is None:
        return render(request, "appraisals/no_staff.html")

    year = AcademicYear.objects.filter(is_current=True).first()
    if year is None:
        return render(request, "appraisals/empty_state.html", {"no_year": True})

    appraisal = Appraisal.objects.filter(teacher=staff, academic_year=year).first()
    if appraisal is not None:
        return redirect("appraisals:detail", pk=appraisal.pk)

    return render(
        request,
        "appraisals/empty_state.html",
        {"year": year, "needs_type": not staff.staff_type},
    )


@login_required
@require_POST
def start_appraisal(request):
    """Create the signed-in user's appraisal for the current year and seed it."""
    staff = current_staff_member(request)
    if staff is None:
        raise PermissionDenied("No staff record for your account.")
    if not staff.staff_type:
        # Let an unclassified staff member self-select Teaching or Support inline
        # (see empty_state.html) rather than dead-ending on "contact an admin".
        # Only ever fills a blank; never overwrites an imported/admin value, and
        # LEADER is deliberately not self-selectable (admin/import only).
        chosen = (request.POST.get("staff_type") or "").strip().upper()
        if chosen in (StaffMember.StaffType.TEACHING, StaffMember.StaffType.SUPPORT):
            staff.staff_type = chosen
            staff.save(update_fields=["staff_type"])
        else:
            messages.error(
                request,
                "Please choose whether you are Teaching or Support staff to begin. "
                "If neither applies (for example you are a senior leader), please "
                "contact an administrator.",
            )
            return redirect("appraisals:my_appraisal")

    year = AcademicYear.objects.filter(is_current=True).first()
    if year is None:
        messages.error(request, "There is no active appraisal cycle yet.")
        return redirect("appraisals:my_appraisal")

    appraisal, created = Appraisal.objects.get_or_create(
        teacher=staff,
        academic_year=year,
        defaults={"coach_email": staff.performance_manager_email},
    )
    if created:
        appraisal.seed_goals()
    if _is_leader(staff):
        _ensure_leader_review(appraisal)
    else:
        _ensure_self_review(appraisal, staff)
    return redirect("appraisals:detail", pk=appraisal.pk)


def _build_leader_self_review(appraisal, can_teacher, bound):
    """Build the senior-leader self-review tab forms (Ethics + Standards)."""
    leader_review = _ensure_leader_review(appraisal)
    return {
        "is_leader": True,
        "leader_review": leader_review,
        "leader_standard_formset": LeaderStandardFormSet(
            bound("self-review"),
            instance=leader_review,
            form_kwargs={"can_teacher": can_teacher, "can_coach": False},
        ),
    }


def _build_standard_self_review(appraisal, staff, can_teacher, bound):
    """Build the teaching/support self-review tab forms (items + bullets)."""
    self_review = _ensure_self_review(appraisal, staff)

    self_review_items = SelfReviewItemFormSet(
        bound("self-review"),
        instance=self_review,
        form_kwargs={"can_teacher": can_teacher, "can_coach": False},
    )
    bullets_qs = SelfReviewBullet.objects.filter(
        self_review_item__self_review=self_review
    ).select_related("self_review_item").order_by("self_review_item__order", "order")
    self_review_bullets = SelfReviewBulletFormSet(
        bound("self-review"),
        queryset=bullets_qs,
        prefix="bullets",
        form_kwargs={"can_teacher": can_teacher, "can_coach": False},
    )

    bullets_by_item = {}
    for bullet_form in self_review_bullets:
        bullets_by_item.setdefault(bullet_form.instance.self_review_item_id, []).append(
            bullet_form
        )
    self_review_rows = [
        {"item_form": item_form, "bullet_forms": bullets_by_item.get(item_form.instance.pk, [])}
        for item_form in self_review_items
    ]

    return {
        "is_leader": False,
        "self_review": self_review,
        "self_review_form": SelfReviewForm(
            bound("self-review"), instance=self_review, can_teacher=can_teacher
        ),
        "self_review_items": self_review_items,
        "self_review_bullets": self_review_bullets,
        "self_review_rows": self_review_rows,
    }


def _build_section_forms(appraisal, staff, role, *, section=None, data=None):
    """Build forms/formsets for every tab; bind only ``section`` when posting."""
    can_teacher = can_edit_teacher_fields(appraisal, role)
    can_coach = can_edit_coach_fields(appraisal, role)
    staff = staff or appraisal.teacher

    def bound(name):
        return data if section == name else None

    # The self-review tab renders the leader variant or the teaching/support
    # variant; the Goals and Summary tabs (below) are the same for both.
    if _is_leader(staff):
        section_forms = _build_leader_self_review(appraisal, can_teacher, bound)
    else:
        section_forms = _build_standard_self_review(
            appraisal, staff, can_teacher, bound
        )

    return {
        **section_forms,
        "goal_formset": GoalFormSet(
            bound("goals"),
            instance=appraisal,
            form_kwargs={"can_teacher": can_teacher, "can_coach": can_coach},
        ),
        "summary_form": AppraisalSummaryForm(
            bound("summary"),
            instance=appraisal,
            can_teacher=can_teacher,
            can_coach=can_coach,
        ),
    }


def _render_detail(request, appraisal, role, forms_ctx, active_tab):
    context = {
        "appraisal": appraisal,
        "role": role,
        "locked": appraisal.is_locked,
        "active_tab": active_tab,
        "tabs": TABS,
        "previous": appraisal.previous(),
        "upr_text": UPR_DECLARATION_TEXT,
        **forms_ctx,
    }
    return render(request, "appraisals/detail.html", context)


@login_required
def appraisal_detail(request, pk, tab="self-review"):
    appraisal, staff, role = get_appraisal_or_403(request, pk)
    active_tab = tab if tab in TABS else "self-review"
    forms_ctx = _build_section_forms(appraisal, staff, role)
    return _render_detail(request, appraisal, role, forms_ctx, active_tab)


def _stamp_signoff(appraisal):
    """Keep signed_off_at in step with the status field."""
    if appraisal.status == Appraisal.Status.SIGNED_OFF and not appraisal.signed_off_at:
        appraisal.signed_off_at = timezone.now()
        appraisal.save(update_fields=["signed_off_at"])
    elif appraisal.status != Appraisal.Status.SIGNED_OFF and appraisal.signed_off_at:
        appraisal.signed_off_at = None
        appraisal.save(update_fields=["signed_off_at"])


def _save_section(request, pk, section, can_check, form_keys, success_msg):
    """Shared POST handler: re-auth, gate, validate the section, save or re-render."""
    appraisal, staff, role = get_appraisal_or_403(request, pk)
    if not can_check(appraisal, role):
        raise PermissionDenied("You may not edit this section.")

    forms_ctx = _build_section_forms(
        appraisal, staff, role, section=section, data=request.POST
    )
    keys = form_keys(forms_ctx) if callable(form_keys) else form_keys
    targets = [forms_ctx[key] for key in keys]
    if all(t.is_valid() for t in targets):
        for t in targets:
            t.save()
        if section == "summary":
            _stamp_signoff(appraisal)
        messages.success(request, success_msg)
        return redirect("appraisals:detail_tab", pk=appraisal.pk, tab=section)

    messages.error(request, "Please correct the errors below.")
    return _render_detail(request, appraisal, role, forms_ctx, section)


def _self_review_form_keys(forms_ctx):
    """The forms to validate/save differ between the leader and standard tabs."""
    if forms_ctx.get("is_leader"):
        return ["leader_standard_formset"]
    return ["self_review_form", "self_review_items", "self_review_bullets"]


@login_required
@require_POST
def self_review_save(request, pk):
    return _save_section(
        request,
        pk,
        "self-review",
        can_edit_teacher_fields,
        _self_review_form_keys,
        "Self-review saved.",
    )


@login_required
@require_POST
def goals_save(request, pk):
    return _save_section(
        request, pk, "goals", _can_edit_either, ["goal_formset"], "Goals saved."
    )


@login_required
@require_POST
def summary_save(request, pk):
    return _save_section(
        request, pk, "summary", _can_edit_either, ["summary_form"], "Summary saved."
    )
