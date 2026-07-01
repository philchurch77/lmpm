from django.contrib import admin

from .models import ImportBatch, ImportRow


class ImportRowInline(admin.TabularInline):
    model = ImportRow
    extra = 0
    fields = ("row_number", "outcome", "error_message", "created_object_model", "created_object_pk")
    readonly_fields = fields
    can_delete = False


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "uploaded_by",
        "uploaded_at",
        "create_count",
        "update_count",
        "skip_count",
    )
    list_filter = ("import_type", "status")
    search_fields = ("original_filename", "uploaded_by__username", "uploaded_by__email")
    inlines = (ImportRowInline,)


@admin.register(ImportRow)
class ImportRowAdmin(admin.ModelAdmin):
    list_display = ("batch", "row_number", "outcome", "created_object_model", "created_object_pk")
    list_filter = ("import_type", "outcome")
    search_fields = ("raw_json",)
