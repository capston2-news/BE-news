from datetime import datetime, timezone
from django.contrib.auth.hashers import make_password
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, serializers
from bson import ObjectId
from .db import get_db
from .permissions import IsAuthenticated, RoleRequired

ALLOWED_ROLES = {"user", "employee", "admin"}

class AdminCreateUserSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=3)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)
    fullname = serializers.CharField()
    age = serializers.IntegerField(min_value=0)

class AdminCreateUser(APIView):
    """
    Chỉ admin mới tạo được user với role tuỳ ý (kể cả employee/admin).
    """
    permission_classes = [IsAuthenticated, RoleRequired.any_of("admin")]

    def post(self, request):
        s = AdminCreateUserSerializer(data=request.data); s.is_valid(raise_exception=True)
        d = s.validated_data
        db = get_db(); users = db["users"]

        users.create_index("username", unique=True, partialFilterExpression={"is_deleted": False}, name="ux_users_username_active")
        users.create_index("email",    unique=True, partialFilterExpression={"is_deleted": False}, name="ux_users_email_active")

        if users.find_one({"username": d["username"], "is_deleted": False}):
            return Response({"detail": "Username exists"}, status=400)
        if users.find_one({"email": d["email"], "is_deleted": False}):
            return Response({"detail": "Email exists"}, status=400)

        role = "employee"

        doc = {
            "username": d["username"],
            "email": d["email"],
            "password": make_password(d["password"]),
            "fullname": d["fullname"],
            "age": int(d["age"]),
            "created_at": datetime.now(timezone.utc),
            "is_active": True,
            "is_deleted": False,
            "role": role,
        }
        res = users.insert_one(doc)
        return Response({"_id": str(res.inserted_id), "role": role}, status=201)

class AdminSetRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=["user", "employee", "admin"])

class AdminSetRole(APIView):
    """
    Admin nâng/giáng role user hiện có.
    """
    permission_classes = [IsAuthenticated, RoleRequired.any_of("admin")]

    def post(self, request, oid: str):
        s = AdminSetRoleSerializer(data=request.data); s.is_valid(raise_exception=True)
        role = s.validated_data["role"]
        db = get_db()
        try:
            _id = ObjectId(oid)
        except Exception:
            return Response({"detail": "invalid user id"}, status=400)

        res = db["users"].update_one({"_id": _id, "is_deleted": {"$ne": True}},
                                     {"$set": {"role": role}})
        if res.matched_count == 0:
            return Response({"detail": "not found"}, status=404)
        return Response({"message": "role updated", "role": role})
