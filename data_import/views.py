"""Views for the bulk import wizard.

Three views, parametrised by ``import_type`` (one of the five
``ImportType`` slugs below) rather than five near-identical view sets, since
upload -> preview -> confirm really is the same shape for all five — only the
parse/validate/apply functions underneath differ (see ``services.py``).

Every view is gated by ``permissions.require_importer`` (superuser-only).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import CsvUploadForm
from .models import ImportBatch, ImportRow, ImportType
from .parsers import ImportFileError, parse_csv
from .permissions import require_importer
from .services import build_rows_for_batch, confirm_batch

# URL-facing slugs for each import type, in upload order.
SLUGS = {
    "staff": ImportType.STAFF,
    "appraisal-summaries": ImportType.APPRAISAL_SUMMARY,
    "goals": ImportType.GOALS,
    "self-review": ImportType.SELF_REVIEW,
    "line-meetings": ImportType.LINE_MEETINGS,
}

PREVIEW_ROW_LIMIT = 50


def _import_type_or_404(slug: str) -> str:
    import_type = SLUGS.get(slug)
    if import_type is None:
        raise Http404("Unknown import type.")
    return import_type


@login_required
def import_hub(request):
    require_importer(request)

    steps = []
    for slug, import_type in SLUGS.items():
        latest = ImportBatch.objects.filter(import_type=import_type).first()
        steps.append({"slug": slug, "label": ImportType(import_type).label, "latest": latest})

    return render(request, "data_import/hub.html", {"steps": steps})


@login_required
def import_upload(request, slug):
    require_importer(request)
    import_type = _import_type_or_404(slug)
    label = ImportType(import_type).label

    if request.method == "POST":
        form = CsvUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                parsed_rows = parse_csv(form.cleaned_data["csv_file"], import_type)
            except ImportFileError as exc:
                form.add_error("csv_file", str(exc))
            else:
                batch = ImportBatch.objects.create(
                    import_type=import_type,
                    uploaded_by=request.user,
                    original_filename=form.cleaned_data["csv_file"].name,
                )
                build_rows_for_batch(batch, parsed_rows)
                return redirect("data_import:preview", batch_id=batch.pk)
    else:
        form = CsvUploadForm()

    return render(
        request,
        "data_import/upload.html",
        {"form": form, "slug": slug, "import_type": import_type, "label": label},
    )


@login_required
def import_preview(request, batch_id):
    require_importer(request)
    batch = get_object_or_404(ImportBatch, pk=batch_id)

    if request.method == "POST":
        if batch.status != ImportBatch.Status.PENDING:
            messages.error(request, "This batch has already been decided.")
            return redirect("data_import:preview", batch_id=batch.pk)

        action = request.POST.get("action")
        if action == "confirm":
            confirm_batch(batch)
            messages.success(request, "Import confirmed.")
        elif action == "discard":
            batch.status = ImportBatch.Status.DISCARDED
            batch.save(update_fields=["status"])
            messages.success(request, "Import discarded — nothing was written.")
        else:
            messages.error(request, "Unrecognised action.")
        return redirect("data_import:preview", batch_id=batch.pk)

    rows = batch.rows.all()[:PREVIEW_ROW_LIMIT]
    total_rows = batch.rows.count()

    return render(
        request,
        "data_import/preview.html",
        {
            "batch": batch,
            "rows": rows,
            "total_rows": total_rows,
            "truncated": total_rows > PREVIEW_ROW_LIMIT,
            "outcomes": ImportRow.Outcome,
        },
    )
