# notifications/urls.py
from django.urls import path
from .views import NotificationStreamView, NotificationListView, NotificationMarkReadView

urlpatterns = [
    path("notifications/user", NotificationListView.as_view(), name="notifications-list"),
    path("notifications/mark-read/", NotificationMarkReadView.as_view(), name="notifications-mark-read"),
    path("notifications/stream/", NotificationStreamView.as_view(), name="notifications-stream"),
]