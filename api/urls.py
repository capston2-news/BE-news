# api/urls.py
from django.urls import path
from .auth_views import RegisterMongo, LoginMongo, RefreshMongo, LogoutMongo, MeMongo
from .api_views import PublicApi, ApiCreate, ApiDelete
from .admin_users import AdminCreateUser, AdminSetRole
from . import views

app_name = "api"  # để dùng namespace: 'api:login', ...

urlpatterns = [
    path("auth/register/", RegisterMongo.as_view(), name="register"),
    path("auth/login/",    LoginMongo.as_view(), name="login"),
    path("auth/refresh/",  RefreshMongo.as_view(), name="refresh"),
    path("auth/logout/",   LogoutMongo.as_view(), name="logout"),
    path("auth/me/",       MeMongo.as_view(), name="me"),

    path("public/",          PublicApi.as_view(), name="articles-public"),
    path("create/",                 ApiCreate.as_view(), name="articles-create"),             # POST (employee/admin)
    path("<str:oid>/",       ApiDelete.as_view(), name="articles-delete"),             # DELETE (admin)

    path("users/",               AdminCreateUser.as_view(), name="admin-users-create"),     # POST (admin) -> luôn employee
    path("users/<str:oid>/role/",AdminSetRole.as_view(), name="admin-users-setrole"), 
       
    path("articles/all/", views.PublicGetAllArticles.as_view(), name="get-all-articles" ),
    path("articles/category/<str:slug>/", views.PublicGetArticlesByCategory.as_view(), name="get-articles-by-category" ),

    path("article/child/<str:slug>/", views.FindArticleByCategoryChild.as_view(), name = "get-articles-by-category-children"),
    path("articles/category/<str:category_slug>/child/<str:child_slug>/", views.PublicGetArticlesByCategoryChild.as_view(), name="get-articles-by-category-child" ),
    # path("articles/search-title/", views.FindArticlesByTitle.as_view(), name="find-articles-by-title" ),
    path("bookmark/articles/user/<str:article_id>", views.BookmarkArticle.as_view(), name="bookmark_article_by_id"),
    path("bookmark/user", views.GetAllBookmarksOfUser.as_view(), name="get_all_bookmarks_of_user"),
    path("article/comments/<str:article_id>", views.CommentOfUser.as_view(), name="article_comments_by_article"),
    path("article/comments", views.CommentOfUser.as_view(), name="article_comments_bulk"),
    path("article/<str:article_id>", views.GetArticleById.as_view(), name = "get_article_by_id"),
    path("article/expect/<str:article_id>", views.GetArticleExpectForArticleById.as_view(), name="article_expect_for_article_by_id"),

    path("article/text/sound", views.TextToSpeech.as_view(), name = "text-to-speech"),
    path("tts/audio/<str:filename>/", views.tts_audio_stream, name="tts_audio_stream"),
]
