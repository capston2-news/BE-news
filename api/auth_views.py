import re
from bson import ObjectId
from datetime import datetime, timezone
from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from rest_framework import serializers, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .db import get_db
from .mongo_auth import make_tokens
from .permissions import IsAuthenticated

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# def set_access_cookie(resp, token):
#     resp.set_cookie(
#         settings.ACCESS_COOKIE_NAME, token,
#         httponly=True, secure=settings.COOKIE_SECURE, samesite=settings.COOKIE_SAMESITE,
#         domain=settings.COOKIE_DOMAIN, max_age=settings.JWT_ACCESS_MINUTES * 60
#     )

# def set_refresh_cookie(resp, token):
#     resp.set_cookie(
#         settings.REFRESH_COOKIE_NAME, token,
#         httponly=True, secure=settings.COOKIE_SECURE, samesite=settings.COOKIE_SAMESITE,
#         domain=settings.COOKIE_DOMAIN, max_age=settings.JWT_REFRESH_DAYS * 24 * 3600
#     )

# def clear_auth_cookies(resp):
#     resp.delete_cookie(settings.ACCESS_COOKIE_NAME, domain=settings.COOKIE_DOMAIN, samesite=settings.COOKIE_SAMESITE)
#     resp.delete_cookie(settings.REFRESH_COOKIE_NAME, domain=settings.COOKIE_DOMAIN, samesite=settings.COOKIE_SAMESITE)
#     # cookie thường (chỉ để FE dựng UI, không dùng authorize)
#     resp.delete_cookie("role", domain=settings.COOKIE_DOMAIN, samesite=settings.COOKIE_SAMESITE)


def _cookie_base_kwargs():
    return {
        "secure": getattr(settings, "COOKIE_SECURE", False),
        "samesite": getattr(settings, "COOKIE_SAMESITE", "Lax"),
    }

def _apply_domain(kwargs: dict):
    domain = getattr(settings, "COOKIE_DOMAIN", None)
    if domain:
        kwargs["domain"] = domain
    return kwargs

def set_access_cookie(resp, token: str):
    kwargs = _cookie_base_kwargs()
    kwargs.update({
        "httponly": True,
        "max_age": int(getattr(settings, "JWT_ACCESS_MINUTES", 30)) * 60,
    })
    _apply_domain(kwargs)
    name = getattr(settings, "ACCESS_COOKIE_NAME", "access_token")
    resp.set_cookie(name, token, **kwargs)

def set_refresh_cookie(resp, token: str):
    kwargs = _cookie_base_kwargs()
    kwargs.update({
        "httponly": True,
        "max_age": int(getattr(settings, "JWT_REFRESH_DAYS", 7)) * 24 * 3600,
    })
    _apply_domain(kwargs)
    name = getattr(settings, "REFRESH_COOKIE_NAME", "refresh_token")
    resp.set_cookie(name, token, **kwargs)

def set_plain_cookie(resp, name: str, value: str, max_age: int):
    """Cookie thường cho FE dựng UI (ví dụ cookie 'role'), KHÔNG dùng authorize."""
    kwargs = _cookie_base_kwargs()
    kwargs.update({
        "httponly": False,
        "max_age": max_age,
    })
    _apply_domain(kwargs)
    resp.set_cookie(name, value, **kwargs)

def clear_auth_cookies(resp):
    access_name  = getattr(settings, "ACCESS_COOKIE_NAME", "access_token")
    refresh_name = getattr(settings, "REFRESH_COOKIE_NAME", "refresh_token")
    samesite = getattr(settings, "COOKIE_SAMESITE", "Lax")
    domain = getattr(settings, "COOKIE_DOMAIN", None)

    if domain:
        resp.delete_cookie(access_name,  domain=domain, samesite=samesite)
        resp.delete_cookie(refresh_name, domain=domain, samesite=samesite)
        resp.delete_cookie("role",      domain=domain, samesite=samesite)
    else:
        resp.delete_cookie(access_name)
        resp.delete_cookie(refresh_name)
        resp.delete_cookie("role")


class RegisterMongoSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=3)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)
    fullname = serializers.CharField()
    age = serializers.IntegerField(min_value=0)

class RegisterMongo(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        s = RegisterMongoSerializer(data=request.data); s.is_valid(raise_exception=True)
        d = s.validated_data
        db = get_db(); users = db["users"]

        users.create_index("username", unique=True, partialFilterExpression={"is_deleted": False}, name="ux_users_username_active")
        users.create_index("email",    unique=True, partialFilterExpression={"is_deleted": False}, name="ux_users_email_active")

        if users.find_one({"username": d["username"], "is_deleted": False}):
            return Response({"detail": "Username exists"}, status=400)
        if users.find_one({"email": d["email"], "is_deleted": False}):
            return Response({"detail": "Email exists"}, status=400)

        doc = {
            "username": d["username"],
            "email": d["email"],
            "password": make_password(d["password"]),
            "fullname": d["fullname"],
            "age": int(d["age"]),
            "created_at": datetime.now(timezone.utc),
            "is_active": True,
            "is_deleted": False,
            "role": "user",  # <-- luôn là user, bỏ mọi input role từ FE
        }
        res = users.insert_one(doc)
        return Response({"_id": str(res.inserted_id), "role": "user"}, status=201)


class LoginMongoSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

class LoginMongo(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        s = LoginMongoSerializer(data=request.data); s.is_valid(raise_exception=True)
        d = s.validated_data
        db = get_db(); users = db["users"]

        u = users.find_one({"username": d["username"], "is_deleted": False})
        if not u or not check_password(d["password"], u["password"]):
            return Response({"detail": "Invalid credentials"}, status=401)

        access, refresh = make_tokens(u)
        resp = Response({
            "message": "ok", 
            "username": u["username"], 
            "role": u.get("role", "user"),
            "access": access,
            "refresh": refresh,
        })
        set_access_cookie(resp, access)
        set_refresh_cookie(resp, refresh)

        # cookie thường để FE dựng UI (KHÔNG dùng để authorize)
        set_plain_cookie(resp, "role", u.get("role", "user"), int(getattr(settings, "JWT_ACCESS_MINUTES", 30)) * 60)
        return resp

class RefreshMongo(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        import jwt
        token = request.COOKIES.get(settings.REFRESH_COOKIE_NAME) or request.data.get("refresh")
        if not token:
            return Response({"detail": "Missing refresh"}, status=400)
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Refresh expired"}, status=401)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid refresh"}, status=401)
        if payload.get("type") != "refresh":
            return Response({"detail": "Wrong token type"}, status=401)

        db = get_db()
        try:
            oid = ObjectId(payload["sub"])
        except Exception:
            return Response({"detail":"Bad subject"}, status=401)
        u = db["users"].find_one({"_id": oid, "is_deleted": {"$ne": True}})
        if not u or not u.get("is_active", True):
            return Response({"detail": "User disabled"}, status=401)

        access, refresh = make_tokens(u)
        resp = Response({"message": "refreshed"})
        set_access_cookie(resp, access)
        set_refresh_cookie(resp, refresh)
        set_plain_cookie(resp, "role", u.get("role", "user"), int(getattr(settings, "JWT_ACCESS_MINUTES", 30)) * 60)
        return resp

class LogoutMongo(APIView):
    def post(self, request):
        resp = Response({"message": "logged out"})
        clear_auth_cookies(resp)
        return resp

class MeMongo(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        db = get_db()
        u = db["users"].find_one({"_id": ObjectId(request.user.id)}, {"password": 0})
        if not u:
            return Response({"detail": "Not found"}, status=404)
        u["_id"] = str(u["_id"])
        return Response(u)
