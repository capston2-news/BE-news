import jwt
from datetime import datetime, timedelta, timezone
from django.conf import settings
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework import exceptions
from .db import get_db
from bson import ObjectId

def now_utc(): return datetime.now(timezone.utc)

def make_tokens(user_doc):
    """
    sub = str(ObjectId) ; role được nhúng vào payload
    """
    base = {
        "sub": str(user_doc["_id"]),
        "username": user_doc["username"],
        "role": user_doc.get("role", "user"),
        "iat": int(now_utc().timestamp()),
    }
    access = jwt.encode(
        {**base, "type": "access", "exp": now_utc() + timedelta(minutes=settings.JWT_ACCESS_MINUTES)},
        settings.JWT_SECRET, algorithm=settings.JWT_ALG
    )
    refresh = jwt.encode(
        {**base, "type": "refresh", "exp": now_utc() + timedelta(days=settings.JWT_REFRESH_DAYS)},
        settings.JWT_SECRET, algorithm=settings.JWT_ALG
    )
    return access, refresh

class SimpleUser:
    def __init__(self, _id, username, role):
        self.id = _id
        self.username = username
        self.role = role
        self.is_authenticated = True

class JWTAuthentication(BaseAuthentication):
    """
    Đọc token ưu tiên từ cookie HttpOnly 'access_token'.
    Nếu không có, thử header 'Authorization: Bearer <token>'
    """
    def authenticate(self, request):
        token = request.COOKIES.get(settings.ACCESS_COOKIE_NAME)
        if not token:
            auth = get_authorization_header(request).decode("utf-8")
            if auth and auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1].strip()
        if not token:
            return None

        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Access token expired")
        except jwt.InvalidTokenError:
            raise exceptions.AuthenticationFailed("Invalid token")
        if payload.get("type") != "access":
            raise exceptions.AuthenticationFailed("Access token required")

        # xác thực user còn tồn tại/active
        db = get_db()
        try:
            oid = ObjectId(payload["sub"])
        except Exception:
            raise exceptions.AuthenticationFailed("Bad subject")

        user = db["users"].find_one({"_id": oid, "is_deleted": {"$ne": True}})
        if not user or not user.get("is_active", True):
            raise exceptions.AuthenticationFailed("User disabled or not found")

        return SimpleUser(str(user["_id"]), user["username"], user.get("role", "user")), None
