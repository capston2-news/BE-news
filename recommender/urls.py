from django.urls import path
from .views import (
    ReindexView, HybridRecommendView, LogActivityView,
    SimilarByArticleView, UserTopicFeedView, ChromaDebugView,
)

urlpatterns = [
    path("reindex/", ReindexView.as_view()),
    path("hybrid/", HybridRecommendView.as_view()),
    path("log/", LogActivityView.as_view()),
    path("similar-by-article/", SimilarByArticleView.as_view()),
    path("user-topic-feed/", UserTopicFeedView.as_view()),
    path("debug/", ChromaDebugView.as_view()),
]
