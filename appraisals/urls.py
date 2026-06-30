from django.urls import path

from . import views

app_name = "appraisals"

urlpatterns = [
    path("", views.my_appraisal, name="my_appraisal"),
    path("start/", views.start_appraisal, name="start_appraisal"),
    path("<int:pk>/", views.appraisal_detail, name="detail"),
    path("<int:pk>/<slug:tab>/", views.appraisal_detail, name="detail_tab"),
    path("<int:pk>/self-review/save/", views.self_review_save, name="self_review_save"),
    path("<int:pk>/goals/save/", views.goals_save, name="goals_save"),
    path("<int:pk>/summary/save/", views.summary_save, name="summary_save"),
]
