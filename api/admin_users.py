from datetime import datetime, timezone
from django.contrib.auth.hashers import make_password
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, serializers
from bson import ObjectId
from .db import get_db
from .permissions import IsAuthenticated, RoleRequired, AllowAny

ALLOWED_ROLES = {"user", "employee", "admin"}

class AdminCreateUserSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=3)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)
    fullname = serializers.CharField()
    age = serializers.IntegerField(min_value=0)
    role = serializers.ChoiceField(
        choices=["user", "employee", "admin"],
        required=False,
        default="employee",
    )

    # (tuỳ chọn) FE có gửi is_active/is_deleted thì khai báo luôn
    is_active = serializers.BooleanField(required=False, default=True)
    is_deleted = serializers.BooleanField(required=False, default=False)



class AdminCreateUser(APIView):
    """
    Chỉ admin mới tạo được user với role tuỳ ý (kể cả employee/admin).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        s = AdminCreateUserSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        db = get_db()
        users = db["users"]

        users.create_index(
            "username",
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_users_username_active",
        )
        users.create_index(
            "email",
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_users_email_active",
        )

        if users.find_one({"username": d["username"], "is_deleted": False}):
            return Response({"detail": "Username exists"}, status=400)
        if users.find_one({"email": d["email"], "is_deleted": False}):
            return Response({"detail": "Email exists"}, status=400)

        role = d["role"]

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
class AdminUpdateUserSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, max_length=150)
    email    = serializers.EmailField(required=False)
    password = serializers.CharField(required=False, min_length=6, write_only=True)

    fullname = serializers.CharField(required=False, allow_blank=True)
    age      = serializers.IntegerField(required=False, min_value=0)

    role     = serializers.ChoiceField(required=False, choices=["user", "employee", "admin"])
    is_active = serializers.BooleanField(required=False)
    is_deleted = serializers.BooleanField(required=False)

class AdminUpdateUser(APIView):
    permission_classes = [AllowAny]

    def _ensure_indexes(self, users):
        users.create_index(
            "username", unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_users_username_active",
        )
        users.create_index(
            "email", unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_users_email_active",
        )

    def patch(self, request, user_id):
        return self._update(request, user_id)

    def _update(self, request, user_id):
        try:
            oid = ObjectId(user_id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        s = AdminUpdateUserSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        db = get_db()
        users = db["users"]
        self._ensure_indexes(users)

        # ✅ vẫn cho update cả user đã deleted để "restore"
        cur = users.find_one({"_id": oid})
        if not cur:
            return Response({"detail": "User not found"}, status=404)

        # ✅ nếu đang deleted mà không gửi is_deleted để restore -> chặn update
        if cur.get("is_deleted") is True and "is_deleted" not in d:
            return Response({"detail": "User is deleted. Set is_deleted=false to restore first."}, status=400)

        # unique checks (exclude current user) - chỉ check khi record đang active (is_deleted=False)
        # và chỉ check nếu field đó được update
        if "username" in d:
            if users.find_one({"_id": {"$ne": oid}, "username": d["username"], "is_deleted": False}):
                return Response({"detail": "Username exists"}, status=400)

        if "email" in d:
            if users.find_one({"_id": {"$ne": oid}, "email": d["email"], "is_deleted": False}):
                return Response({"detail": "Email exists"}, status=400)

        set_doc = {}

        if "username" in d:  set_doc["username"] = d["username"]
        if "email" in d:     set_doc["email"] = d["email"]
        if "fullname" in d:  set_doc["fullname"] = d["fullname"]
        if "age" in d:       set_doc["age"] = int(d["age"])
        if "role" in d:      set_doc["role"] = d["role"]

        if "password" in d:
            set_doc["password"] = make_password(d["password"])

        # ✅ soft delete / restore bằng is_deleted
        if "is_deleted" in d:
            is_del = bool(d["is_deleted"])
            set_doc["is_deleted"] = is_del
            set_doc["is_active"] = (not is_del)   # deleted => inactive, restore => active
            if is_del:
                set_doc["deleted_at"] = datetime.now(timezone.utc)
            else:
                set_doc["deleted_at"] = None

        # ✅ nếu chỉ muốn khoá/mở bằng is_active
        if "is_active" in d:
            set_doc["is_active"] = bool(d["is_active"])

        if not set_doc:
            return Response({"detail": "No fields to update"}, status=400)

        set_doc["updated_at"] = datetime.now(timezone.utc)

        users.update_one({"_id": oid}, {"$set": set_doc})

        out = users.find_one({"_id": oid}, {"password": 0})
        out["_id"] = str(out["_id"])
        return Response(out, status=200)