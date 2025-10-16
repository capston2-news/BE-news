from bson import ObjectId
from datetime import datetime, timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .db import get_db
from .permissions import AllowAny, IsAuthenticated, RoleRequired

def to_public(doc):
    doc["_id"] = str(doc["_id"])
    return doc

class PublicApi(APIView):
    permission_classes = [AllowAny]  # ai cũng xem được
    def get(self, request):
        db = get_db()
        docs = list(db["api"].find({"is_deleted": {"$ne": True}}).sort("created_at", -1))
        return Response([to_public(d) for d in docs])

class ApiCreate(APIView):
    # employee & admin được tạo
    permission_classes = [IsAuthenticated, RoleRequired.any_of("employee", "admin")]
    def post(self, request):
        data = request.data or {}
        title = (data.get("title") or "").strip()
        content = (data.get("content") or "").strip()
        if not title or not content:
            return Response({"detail":"title/content required"}, status=400)

        db = get_db()
        doc = {
            "title": title,
            "content": content,
            "created_at": datetime.now(timezone.utc),
            "is_deleted": False,
            "author_username": request.user.username,
        }
        res = db["api"].insert_one(doc)
        doc["_id"] = str(res.inserted_id)
        return Response(doc, status=201)

class ApiDelete(APIView):
    # chỉ admin được xóa
    permission_classes = [IsAuthenticated, RoleRequired.any_of("admin")]
    def delete(self, request, oid: str):
        try:
            _id = ObjectId(oid)
        except Exception:
            return Response({"detail":"Invalid id"}, status=400)
        db = get_db()
        res = db["api"].update_one({"_id": _id}, {"$set": {"is_deleted": True}})
        if res.matched_count == 0:
            return Response({"detail":"Not found"}, status=404)
        return Response(status=204)
