# api/urls.py
from django.urls import path
from .auth_views import RegisterMongo, LoginMongo, RefreshMongo, LogoutMongo, MeMongo
from .api_views import PublicApi, ApiCreate, ApiDelete
from .admin_users import AdminCreateUser, AdminSetRole

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
    path("users/<str:oid>/role/",AdminSetRole.as_view(), name="admin-users-setrole"),    # POST (admin)
]
