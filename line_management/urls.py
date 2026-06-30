from django.urls import path

from . import views

app_name = "line_management"

urlpatterns = [
    path("", views.my_meetings, name="my_meetings"),
    path("staff/<int:staff_pk>/", views.staff_meetings, name="staff_meetings"),
    path("staff/<int:staff_pk>/new/", views.meeting_new, name="meeting_new"),
    path("staff/<int:staff_pk>/create/", views.meeting_create, name="meeting_create"),
    path("meeting/<int:pk>/", views.meeting_detail, name="meeting_detail"),
    path("meeting/<int:pk>/save/", views.meeting_save, name="meeting_save"),
]
