# api/urls.py
from django.urls import path

from .admin_bookmark import GetAllBookmarksOfUsers
from .admin_comments import AdminListComments, AdminDeleteComment, AdminPendingCommentsCount, AdminRestoreComment
from .admin_dashboard import AdminDashboardKPIView, AdminViewsSeriesView, AdminCommentsSeriesView, AdminTopArticlesView, AdminViewsByCategoryView, AdminViewsBySiteView
from .auth_views import RegisterMongo, LoginMongo, RefreshMongo, LogoutMongo, MeMongo
from .api_views import PublicApi, ApiCreate, ApiDelete
from .admin_users import AdminCreateUser, AdminSetRole,  AdminUpdateUser
from . import views
from .admin_authors import AdminAuthors, AdminAuthorDetail, AdminAuthorRestore

app_name = "api"

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

    path("articles/history/viewed/", views.UserViewedHistory.as_view()),
    path("articles/all/", views.PublicGetAllArticles.as_view(), name="get-all-articles" ),
    path("articles/category/<str:slug>/", views.PublicGetArticlesByCategory.as_view(), name="get-articles-by-category" ),
    path("category/all/child/<str:category_slug>/", views.GetAllCategoryChildOfCategory.as_view(), name="get-all-category-child-of-category"),

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

    path("categories/all/", views.GetAllCategory.as_view(), name="get_all_category"),

    path("articles/month/", views.TopArticlesThisMonth.as_view(), name ="top_10_articles_this_month"),

    path("article/bookmark/", views.GatBookmarkOfUser.as_view(), name = "get_article_bookmark"),

    path("article/search/", views.SearchArticleByTitle.as_view(), name = "search_article"),

    path("<str:comment_id>/lookup-article/", views.CommentLookupArticleView.as_view()),

    path("admin/comments/<str:comment_id>/checked/", views.AdminApproveComment.as_view()),




    path("categories/<slug:category_slug>/children/", views.AdminCreateCategoryChild.as_view()),
    path("categories/<slug:category_slug>/children/<str:child_id>/", views.AdminUpdateCategoryChild.as_view()),  # PATCH
    path("categories/<slug:category_slug>/children/<str:child_id>/delete/", views.AdminDeleteCategoryChild.as_view()),
    path("admin/comments/", AdminListComments.as_view()),
    path("admin/comments/<str:comment_id>/delete/", AdminDeleteComment.as_view()),
    path("admin/comments/pending-count/", AdminPendingCommentsCount.as_view()),
    path("admin/comments/<str:comment_id>/restore/", AdminRestoreComment.as_view()),

    path("admin/authors/", AdminAuthors.as_view()),
    path("admin/authors/<str:id>/", AdminAuthorDetail.as_view()),
    path("admin/authors/<str:id>/restore/", AdminAuthorRestore.as_view()),  # ✅

    path("dashboard/kpis", AdminDashboardKPIView.as_view()),
    path("dashboard/views-series", AdminViewsSeriesView.as_view()),
    path("dashboard/comments-series", AdminCommentsSeriesView.as_view()),
    path("dashboard/top-articles", AdminTopArticlesView.as_view()),
    path("dashboard/views-by-category", AdminViewsByCategoryView.as_view()),
    path("dashboard/views-by-site", AdminViewsBySiteView.as_view()),

    path("create/categories/", views.AdminCreateCategory.as_view()),
    path("categories/<str:category_id>/", views.AdminUpdateCategory.as_view()),          # PATCH
    path("categories/<str:category_id>/delete/", views.AdminDeleteCategory.as_view()),  # DELETE

    path("getall/users/",views.GetAllUsers.as_view(), name="getall-user"),
    path("users/<str:user_id>/", AdminUpdateUser.as_view(), name="admin-users-detail"),
    path("admin/users/<str:user_id>/bookmarks/", GetAllBookmarksOfUsers.as_view()),
]
