from django.urls import path

from . import views

app_name = "overview"

urlpatterns = [
    path("appraisals/", views.appraisals_overview, name="appraisals"),
    path("line-management/", views.line_management_overview, name="line_management"),
]
