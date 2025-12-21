from bson import ObjectId
from datetime import datetime, timezone
import re

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from api.db import get_db


def _now():
    return datetime.now(timezone.utc)


def _oid(s: str):
    try:
        return ObjectId(str(s))
    except Exception:
        return None


def ser_author(x: dict):
    if not x:
        return None
    return {
        "_id": str(x.get("_id")) if x.get("_id") else None,
        "name": x.get("name", ""),
        "created_at": x.get("created_at"),
        "updated_at": x.get("updated_at"),
        "is_deleted": bool(x.get("is_deleted", False)),

    }


def ensure_author_indexes(col):
    try:
        col.create_index([("name", 1)])
        col.create_index([("created_at", -1)])
        col.create_index([("is_deleted", 1)])
    except Exception:
        pass


class AdminAuthors(APIView):
    """
    GET  /api/admin/authors/?q=
         -> trả về ALL authors (không pagination, không lọc is_deleted)
    POST /api/admin/authors/  body: { "name": "..." }
    """
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        col = db["authors"]
        ensure_author_indexes(col)

        q = (request.GET.get("q") or "").strip()

        flt = {}
        if q:
            flt["name"] = {"$regex": re.escape(q), "$options": "i"}

        cur = col.find(
            flt,
            {"_id": 1, "name": 1, "created_at": 1, "updated_at": 1, "is_deleted": 1, "deleted_at": 1},
        ).sort("created_at", -1)

        results = [ser_author(x) for x in cur]
        return Response({"results": results}, status=200)

    def post(self, request):
        db = get_db()
        col = db["authors"]
        ensure_author_indexes(col)

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Missing name"}, status=400)

        doc = {"name": name, "created_at": _now(), "is_deleted": False}
        ins = col.insert_one(doc)
        doc["_id"] = ins.inserted_id

        return Response({"author": ser_author(doc)}, status=201)


class AdminAuthorDetail(APIView):
    """
    PUT    /api/admin/authors/<id>/   body: { "name": "..." }
    DELETE /api/admin/authors/<id>/   (soft delete)
    GET    /api/admin/authors/<id>/
    """
    permission_classes = [AllowAny]

    def get(self, request, id):
        db = get_db()
        col = db["authors"]
        oid = _oid(id)
        if not oid:
            return Response({"detail": "Invalid id"}, status=400)

        doc = col.find_one(
            {"_id": oid},
            {"_id": 1, "name": 1, "created_at": 1, "updated_at": 1, "is_deleted": 1},
        )
        if not doc:
            return Response({"detail": "Not found"}, status=404)

        return Response({"author": ser_author(doc)}, status=200)

    def put(self, request, id):
        db = get_db()
        col = db["authors"]
        oid = _oid(id)
        if not oid:
            return Response({"detail": "Invalid id"}, status=400)

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Missing name"}, status=400)

        res = col.update_one(
            {"_id": oid},
            {"$set": {"name": name, "updated_at": _now()}},
        )
        if res.matched_count == 0:
            return Response({"detail": "Not found"}, status=404)

        doc = col.find_one({"_id": oid})
        return Response({"author": ser_author(doc)}, status=200)

    def delete(self, request, id):
        db = get_db()
        col = db["authors"]
        oid = _oid(id)
        if not oid:
            return Response({"detail": "Invalid id"}, status=400)

        res = col.update_one(
            {"_id": oid, "is_deleted": {"$ne": True}},
            {"$set": {"is_deleted": True}},
        )
        if res.matched_count == 0:
            return Response({"detail": "Not found or already deleted"}, status=404)

        return Response({"ok": True}, status=200)


class AdminAuthorRestore(APIView):
    """
    POST /api/admin/authors/<id>/restore/
    """
    permission_classes = [AllowAny]

    def post(self, request, id):
        db = get_db()
        col = db["authors"]
        oid = _oid(id)
        if not oid:
            return Response({"detail": "Invalid id"}, status=400)

        res = col.update_one(
            {"_id": oid, "is_deleted": True},
            {"$set": {"is_deleted": False}},
        )
        if res.matched_count == 0:
            return Response({"detail": "Not found or not deleted"}, status=404)

        doc = col.find_one({"_id": oid})
        return Response({"author": ser_author(doc)}, status=200)
