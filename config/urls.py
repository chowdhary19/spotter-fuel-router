from django.urls import path

from fuel import views

urlpatterns = [
    path("", views.home, name="home"),
    path("health/", views.health, name="health"),
    path("api/route/", views.route, name="route"),
    path("routes/<str:route_id>/map/", views.route_map, name="route-map"),
]
