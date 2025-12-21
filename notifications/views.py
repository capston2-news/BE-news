# notifications/views.py
import queue as qmod
from datetime import datetime, timezone
from bson import ObjectId, json_util

from django.http import StreamingHttpResponse, JsonResponse
from rest_framework.views import APIView
from api.permissions import AllowAny, IsAuthenticated, RoleRequired

from api.db import get_db
import json
from .realtime import subscribe, unsubscribe


def _iso(dt):
    if not dt:
        return None
    if isinstance(dt, dict) and "$date" in dt:
        return dt["$date"]
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(dt)


def _to_oid(v):
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    s = str(v).strip()
    return ObjectId(s) if ObjectId.is_valid(s) else None


def _ser(doc):
    return json.loads(json_util.dumps(doc))

def to_oid(v):
    """Nhận string / ObjectId / dict {$oid} / dict {_id:{$oid}} và trả về ObjectId hoặc None."""
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v

    # dict kiểu {"$oid":"..."}
    if isinstance(v, dict):
        if "$oid" in v and ObjectId.is_valid(str(v["$oid"])):
            return ObjectId(str(v["$oid"]))
        # dict kiểu {"_id":{"$oid":"..."}}
        _id = v.get("_id")
        if isinstance(_id, dict) and "$oid" in _id and ObjectId.is_valid(str(_id["$oid"])):
            return ObjectId(str(_id["$oid"]))

    # string
    s = str(v).strip()
    return ObjectId(s) if ObjectId.is_valid(s) else None

def to_oid2(v):
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, dict) and "$oid" in v and ObjectId.is_valid(str(v["$oid"])):
        return ObjectId(str(v["$oid"]))
    s = str(v).strip()
    return ObjectId(s) if ObjectId.is_valid(s) else None

class NotificationStreamView(APIView):
    """
    GET /api/notifications/stream/
    """
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def get(self, request):
        raw_uid = getattr(request.user, "id", None) or getattr(request.user, "_id", None)
        user_oid = to_oid2(raw_uid)
        if not user_oid:
            return JsonResponse({"detail": "Unauthorized"}, status=401)

        q = subscribe(user_oid)  # ✅ đưa ObjectId vào, realtime.py sẽ normalize

        def gen():
            yield "event: ping\ndata: {}\n\n"
            yield "retry: 2000\n"
            try:
                while True:
                    try:
                        msg = q.get(timeout=15)
                        yield msg
                    except qmod.Empty:
                        yield "event: ping\ndata: {}\n\n"
            except GeneratorExit:
                pass
            finally:
                unsubscribe(user_oid, q)  # ✅ khớp key

        resp = StreamingHttpResponse(gen(), content_type="text/event-stream; charset=utf-8")
        resp["Cache-Control"] = "no-cache, no-transform"
        resp["Connection"] = "keep-alive"
        resp["X-Accel-Buffering"] = "no"
        return resp


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def get(self, request):
        db = get_db()

        user_oid = _to_oid(getattr(request.user, "id", None))
        if not user_oid:
            return JsonResponse({"detail": "Unauthorized"}, status=401)

        try:
            limit = int(request.query_params.get("limit", 30))
            limit = max(1, min(limit, 100))
        except Exception:
            limit = 30

        pipeline = [
            {"$match": {"user_id": user_oid}},
            {"$sort": {"created_at": -1}},
            {"$limit": limit},

            # comment_id -> ObjectId (nếu là string)
            {
                "$addFields": {
                    "_comment_oid": {
                        "$switch": {
                            "branches": [
                                {"case": {"$eq": [{"$type": "$comment_id"}, "objectId"]}, "then": "$comment_id"},
                                {
                                    "case": {"$eq": [{"$type": "$comment_id"}, "string"]},
                                    "then": {
                                        "$convert": {
                                            "input": "$comment_id",
                                            "to": "objectId",
                                            "onError": None,
                                            "onNull": None,
                                        }
                                    },
                                },
                            ],
                            "default": None,
                        }
                    }
                }
            },

            # join comments
            {
                "$lookup": {
                    "from": "comments",
                    "localField": "_comment_oid",
                    "foreignField": "_id",
                    "as": "comment",
                }
            },
            {"$unwind": {"path": "$comment", "preserveNullAndEmptyArrays": True}},

            # comment.article_id -> ObjectId (nếu là string)
            {
                "$addFields": {
                    "_article_oid": {
                        "$switch": {
                            "branches": [
                                {
                                    "case": {"$eq": [{"$type": "$comment.article_id"}, "objectId"]},
                                    "then": "$comment.article_id",
                                },
                                {
                                    "case": {"$eq": [{"$type": "$comment.article_id"}, "string"]},
                                    "then": {
                                        "$convert": {
                                            "input": "$comment.article_id",
                                            "to": "objectId",
                                            "onError": None,
                                            "onNull": None,
                                        }
                                    },
                                },
                            ],
                            "default": None,
                        }
                    }
                }
            },

            # join articles
            {
                "$lookup": {
                    "from": "articles",
                    "localField": "_article_oid",
                    "foreignField": "_id",
                    "as": "article",
                }
            },
            {"$unwind": {"path": "$article", "preserveNullAndEmptyArrays": True}},

            # output
            {
                "$project": {
                    "_id": 1,
                    "user_id": 1,
                    "type": 1,
                    "message": 1,
                    "is_read": 1,
                    "created_at": 1,
                    "comment_id": 1,

                    # enrich theo comment mẫu của bạn
                    "comment_content": "$comment.content",
                    "article_id": "$comment.article_id",
                    "article_title": "$article.title",
                }
            },
        ]

        items = list(db["notifications"].aggregate(pipeline))
        items = [_ser(x) for x in items]

        unread = db["notifications"].count_documents({"user_id": user_oid, "is_read": False})

        return JsonResponse({"items": items, "unread": unread}, status=200)


class NotificationMarkReadView(APIView):
    """
    POST /api/notifications/mark-read/
    body: { ids: ["..."] } hoặc { all: true }
    """
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def post(self, request):
        db = get_db()
        col = db["notifications"]

        # ✅ lấy user id an toàn (tuỳ bạn set ở auth)
        raw_uid = getattr(request.user, "id", None) or getattr(request.user, "_id", None)
        user_oid = to_oid(raw_uid)

        if not user_oid:
            return JsonResponse({"detail": "Unauthorized"}, status=401)

        body = request.data or {}

        # ✅ mark all
        if body.get("all") is True:
            r = col.update_many(
                {"user_id": user_oid, "is_read": False},
                {"$set": {"is_read": True}}
            )
            unread = col.count_documents({"user_id": user_oid, "is_read": False})
            return JsonResponse(
                {
                    "ok": True,
                    "unread": unread,
                    "matched": r.matched_count,
                    "modified": r.modified_count,
                },
                status=200
            )

        # ✅ mark list ids
        ids = body.get("ids") or []
        oids = []
        for x in ids:
            oid = to_oid(x)
            if oid:
                oids.append(oid)

        if oids:
            r = col.update_many(
                {"_id": {"$in": oids}, "user_id": user_oid},
                {"$set": {"is_read": True}}
            )
            matched = r.matched_count
            modified = r.modified_count
        else:
            matched = 0
            modified = 0

        unread = col.count_documents({"user_id": user_oid, "is_read": False})
        return JsonResponse(
            {"ok": True, "unread": unread, "matched": matched, "modified": modified},
            status=200
        )
