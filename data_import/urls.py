from django.urls import path

from . import views

app_name = "data_import"

urlpatterns = [
    path("", views.import_hub, name="hub"),
    path("<slug:slug>/upload/", views.import_upload, name="upload"),
    path("batch/<int:batch_id>/", views.import_preview, name="preview"),
]
