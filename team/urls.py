from django.urls import path

from . import views

app_name = "team"

urlpatterns = [
    path("", views.my_team, name="my_team"),
]
