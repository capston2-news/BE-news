# weather/urls.py
from django.urls import path
from .views import LocationSearchView, ForecastView, VietnamHeatmapView

urlpatterns = [
    path("locations/", LocationSearchView.as_view(), name="weather-locations"),
    path("forecast/", ForecastView.as_view(), name="weather-forecast"),
    path("vn-heatmap/", VietnamHeatmapView.as_view()),
]
