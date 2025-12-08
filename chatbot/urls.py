# chatbot/urls.py
from django.urls import path
from .views import ChatbotView, NewConversationView

urlpatterns = [     # GET: trang chat
    path("chat/", ChatbotView.as_view(), name="chatbot_api"),  # POST: API
    path("chat/new-conversation/", NewConversationView.as_view(), name="chatbot-new-conversation"),
]
